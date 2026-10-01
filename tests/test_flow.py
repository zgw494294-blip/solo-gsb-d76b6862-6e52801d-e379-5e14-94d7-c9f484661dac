"""End-to-end checks for the dual-slot firmware API (no hardware, no server).

Run:  python3 tests/test_flow.py
Uses FastAPI TestClient against a throwaway SQLite file.
"""

import base64
import hashlib
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading

os.environ["FIRMWARE_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "fw-test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import MAX_IMAGE_SIZE  # noqa: E402
from app.main import app

# Entering the context triggers the lifespan startup (DB initialization).
client = TestClient(app)
client.__enter__()
PASS = 0


def check(cond, msg):
    global PASS
    assert cond, f"FAIL: {msg}"
    PASS += 1
    print(f"  ok - {msg}")


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def create_device(dev_id, image, image_sha=None):
    body = {"device_id": dev_id, "initial_image_base64": b64(image)}
    if image_sha is not None:
        body["initial_image_sha256"] = image_sha
    return client.post("/devices", json=body)


print("== 1. device creation ==")
v1 = bytes(range(256)) * 4  # 1024 bytes
r = create_device("dev1", v1, sha(v1))
check(r.status_code == 201, f"create 201 (got {r.status_code})")
s = r.json()
check(s["confirmed_slot"] == "a" and s["running_slot"] == "a", "initial slot a confirmed/running")
check(s["slots"]["a"]["status"] == "complete" and s["slots"]["a"]["verified"], "slot a complete+verified")
check(s["slots"]["a"]["sha256"] == sha(v1), "slot a digest stored")
check(s["slots"]["b"]["status"] == "empty", "slot b empty")
check(s["pending_confirmation_token"] is None and not s["trial_pending"], "no trial pending")

r = create_device("dev1", v1, sha(v1))
check(r.status_code == 409, "duplicate device id -> 409")

big = b"\x00" * (MAX_IMAGE_SIZE + 1)
r = create_device("dev-too-big", big)
check(r.status_code == 413, f"initial image > 64KiB -> 413 (got {r.status_code() if False else r.status_code})")

r = create_device("dev-bad-sha", v1, sha(b"wrong"))
check(r.status_code == 422, "initial image digest mismatch -> 422")

r = client.post("/devices", json={"device_id": "dev-x", "initial_image_base64": "!!!notb64"})
check(r.status_code == 400, "invalid base64 -> 400")

r = client.get("/devices/nope")
check(r.status_code == 404, "unknown device -> 404")

print("== 2. declare update + chunking rules ==")
v2 = bytearray(2048)
for i in range(2048):
    v2[i] = i % 251
v2 = bytes(v2)

r = client.post("/devices/dev1/updates", json={"declared_length": MAX_IMAGE_SIZE + 1,
                                                "sha256": "a" * 64})
check(r.status_code == 422, "declared_length > 64KiB -> 422")

r = client.post("/devices/dev1/updates", json={"declared_length": len(v2),
                                                "sha256": "xyz"})
check(r.status_code == 422, "bad sha format -> 422")

r = client.post("/devices/dev1/updates", json={"declared_length": len(v2),
                                                "sha256": sha(v2)})
check(r.status_code == 200 and r.json()["target_slot"] == "b", "update targets slot b")
check(r.json()["slot"]["status"] == "uploading", "slot b uploading")

# Chunks out of order: upload second half first, then first half (no overlap).
def put(dev, off, piece):
    return client.put(f"/devices/{dev}/chunks", json={"offset": off, "data": b64(piece)})


r = put("dev1", 1024, v2[1024:])
check(r.status_code == 200 and not r.json()["complete"], "chunk @1024 accepted, not complete")
r = put("dev1", 0, v2[:1024])
check(r.status_code == 200 and r.json()["complete"], "final chunk -> complete")

# Idempotent retransmission of identical range.
r = put("dev1", 1024, v2[1024:])
check(r.status_code == 200 and r.json()["duplicate"] and r.json()["complete"],
      "same range+content retransmitted -> duplicate, still complete")

# Different content at the same range -> whole chunk rejected.
bad = bytearray(v2[1024:])
bad[0] ^= 0xFF
r = put("dev1", 1024, bytes(bad))
check(r.status_code == 409, "same range different content -> 409")

# Overflow beyond declared length.
r = put("dev1", 2001, v2[2001:2048])  # 47 bytes at 2001 -> end 2048 == length
check(r.status_code == 200, "chunk ending exactly at boundary accepted")
r = put("dev1", 2001, v2[2000:2048])  # 48 bytes at 2001 -> end 2049 > 2048
check(r.status_code == 416, "chunk exceeding declared length -> 416")

# Slot b digest must match declared.
s = client.get("/devices/dev1").json()
check(s["slots"]["b"]["status"] == "complete" and s["slots"]["b"]["verified"],
      "slot b complete+verified with correct digest")
check(s["slots"]["b"]["sha256"] == sha(v2), "slot b digest = sha(v2)")

# Partial overlap with identical shared bytes is accepted (new device).
ov_img = bytes(range(100))
create_device("dev-ov", ov_img, sha(ov_img))
new_img = bytes((i * 7) % 256 for i in range(100))
client.post("/devices/dev-ov/updates", json={"declared_length": 100,
                                              "sha256": sha(new_img)})
r = put("dev-ov", 0, new_img[:60])
check(r.status_code == 200, "first overlapping-scenario chunk accepted")
r = put("dev-ov", 40, new_img[40:])  # overlaps [40,60) with identical bytes
check(r.status_code == 200 and r.json()["complete"], "overlapping identical bytes -> accepted, complete")
evil = bytearray(new_img[50:80])
evil[5] ^= 1
r = put("dev-ov", 50, bytes(evil))  # overlaps [50,60) with DIFFERENT byte
check(r.status_code == 409, "overlapping differing bytes -> 409 whole chunk rejected")

print("== 3. trial boot / confirm / writes frozen ==")
r = client.post("/devices/dev1/restart")
check(r.status_code == 200, "restart ok")
j = r.json()
check(j["action"] == "trial" and len(j["token"]) == 64, "trial booted, unique hex token returned")
token = j["token"]
check(j["running_slot"] == "b" and j["trial_pending"], "running b, trial pending")
check(client.get("/devices/dev1").json()["pending_confirmation_token"] == token,
      "token visible in status query")

# Writes frozen during pending trial.
r = client.post("/devices/dev1/updates", json={"declared_length": 1, "sha256": "0" * 64})
check(r.status_code == 409, "declare update during trial -> 409")
r = put("dev1", 0, b"\x00")
check(r.status_code == 409, "chunk write during trial -> 409")

# Wrong token rejected; trial remains pending.
r = client.post("/devices/dev1/confirm", json={"token": "deadbeef"})
check(r.status_code == 409, "wrong token -> 409")
check(client.get("/devices/dev1").json()["trial_pending"], "still pending after wrong token")

# Correct token promotes candidate; old slot wiped.
r = client.post("/devices/dev1/confirm", json={"token": token})
check(r.status_code == 200 and r.json()["action"] == "confirmed", "valid token -> confirmed")
s = client.get("/devices/dev1").json()
check(s["confirmed_slot"] == "b" and s["running_slot"] == "b", "slot b now confirmed/running")
check(s["slots"]["a"]["status"] == "empty" and not s["trial_pending"],
      "old slot a wiped, trial cleared")

# Double-confirm (late) -> 409.
r = client.post("/devices/dev1/confirm", json={"token": token})
check(r.status_code == 409, "late confirm -> 409")

# Next update now targets a.
r = client.post("/devices/dev1/updates", json={"declared_length": len(v1), "sha256": sha(v1)})
check(r.json()["target_slot"] == "a", "next update targets slot a")
put("dev1", 0, v1)
client.post("/devices/dev1/restart")  # trial slot a
# Same token cannot be reused.
r = client.post("/devices/dev1/confirm", json={"token": token})
check(r.status_code == 409, "old token cannot confirm a new trial -> 409")
client.post("/devices/dev1/restart")  # rollback to b

print("== 4. rollback on unconfirmed restart ==")
# dev1 currently: confirmed b (v2). Declare new candidate on a.
v3 = bytes([0xEE]) * 128
r = client.post("/devices/dev1/updates", json={"declared_length": len(v3), "sha256": sha(v3)})
put("dev1", 0, v3)
r = client.post("/devices/dev1/restart")
token3 = r.json()["token"]
check(r.json()["action"] == "trial" and r.json()["running_slot"] == "a", "trial v3 on slot a")

# Restart again without confirming -> rollback.
r = client.post("/devices/dev1/restart")
j = r.json()
check(j["action"] == "rollback" and j["running_slot"] == "b" and not j["trial_pending"],
      "rollback to confirmed slot b")
check(j["slots"]["a"]["status"] == "empty", "candidate slot a discarded")
check(j["pending_confirmation_token"] is None, "token cleared after rollback")

# Late confirmation rejected.
r = client.post("/devices/dev1/confirm", json={"token": token3})
check(r.status_code == 409, "late confirm after rollback -> 409")

print("== 5. incomplete / wrong-digest candidates never boot ==")
inc = b"\x01" * 512
client.post("/devices/dev1/updates", json={"declared_length": len(inc), "sha256": sha(inc)})
put("dev1", 0, inc[:256])  # only half
r = client.post("/devices/dev1/restart")
j = r.json()
check(j["action"] == "boot_confirmed" and j["running_slot"] == "b",
      "incomplete candidate -> old image boots")
check(j["slots"]["a"]["status"] == "empty", "incomplete candidate session discarded on boot attempt")
check(client.get("/devices/dev1").json()["slots"]["b"]["sha256"] == sha(v2),
      "old confirmed image intact")

# Fully covered but wrong digest.
client.post("/devices/dev1/updates", json={"declared_length": 512,
                                            "sha256": "1" * 64})
put("dev1", 0, inc)
s = client.get("/devices/dev1").json()
check(s["slots"]["a"]["status"] == "uploading"
      and s["slots"]["a"]["error"] == "sha256_mismatch"
      and not s["slots"]["a"]["verified"],
      "wrong digest -> not complete, error=sha256_mismatch")
r = client.post("/devices/dev1/restart")
check(r.json()["action"] == "boot_confirmed", "bad-digest candidate -> old image boots")

print("== 6. zero-length image edge case ==")
r = client.post("/devices", json={"device_id": "dev-zero",
                                   "initial_image_base64": b64(b"")})
check(r.status_code == 201, "empty initial image allowed")
r = client.post("/devices/dev-zero/updates", json={"declared_length": 0,
                                                    "sha256": sha(b"")})
check(r.status_code == 200 and r.json()["slot"]["status"] == "complete",
      "zero-length update immediately complete")
r = client.post("/devices/dev-zero/restart")
check(r.json()["action"] == "trial", "zero-length candidate can trial-boot")
check(client.post("/devices/dev-zero/confirm",
                  json={"token": r.json()["token"]}).status_code == 200,
      "zero-length candidate confirmed")

print("== 7. service restart != device restart (state persisted in SQLite) ==")
# Finish a trial on dev1 (confirmed b(v2), candidate a(v3)) then inspect DB
# from a *new process* while trial pending.
client.post("/devices/dev1/updates", json={"declared_length": len(v3), "sha256": sha(v3)})
put("dev1", 0, v3)
client.post("/devices/dev1/restart")
s = client.get("/devices/dev1").json()
check(s["trial_pending"] and s["running_slot"] == "a" and s["pending_confirmation_token"],
      "trial pending in current process")

db_path = os.environ["FIRMWARE_DB_PATH"]
probe = subprocess.run(
    [sys.executable, "-c",
     "import os, sqlite3, json; "
     "c = sqlite3.connect(os.environ['FIRMWARE_DB_PATH']); "
     "r = c.execute(\"SELECT trial_pending, running_slot, pending_token, "
     "a_status, a_verified FROM device WHERE device_id='dev1'\").fetchone(); "
     "n = c.execute(\"SELECT COUNT(*) FROM chunk WHERE device_id='dev1' AND slot='a'\").fetchone()[0]; "
     "print(json.dumps([r[0], r[1], r[2], r[3], r[4], n]))"],
    env={**os.environ}, capture_output=True, text=True)
assert probe.returncode == 0, probe.stderr
import json
tp, rs, tok, st, ver, nchunks = json.loads(probe.stdout.strip())
check(tp == 1 and rs == "a" and tok and st == "complete" and ver == 1 and nchunks == 1,
      "fresh process sees persisted trial token + complete slot + chunks")

# Re-init (simulates app startup) and confirm via API still works after "restart".
from app import main as main_mod
main_mod._init_db()
r = client.post("/devices/dev1/confirm", json={"token": s["pending_confirmation_token"]})
check(r.status_code == 200, "confirmation after simulated service restart succeeds")

print("== 8. concurrency equivalent to serial execution ==")
cv = bytes((i * 13 + 5) % 256 for i in range(8192))  # multiple chunks
r = client.post("/devices/dev1/updates", json={"declared_length": len(cv), "sha256": sha(cv)})
target = r.json()["target_slot"]
CHUNK = 512
errors = []
barrier = threading.Barrier(8)


def worker(t):
    try:
        barrier.wait()
        # 8 threads, disjoint pieces; then each retransmits its piece twice.
        for off in range(t * CHUNK, len(cv), 8 * CHUNK):
            piece = cv[off:off + CHUNK]
            r1 = put("dev1", off, piece)
            r2 = put("dev1", off, piece)
            r3 = put("dev1", off, piece)
            if not (r1.status_code == 200 and r2.status_code == 200
                    and r3.json()["duplicate"]):
                errors.append((off, r1.status_code, r2.status_code))
    except Exception as e:  # noqa: BLE001
        errors.append(repr(e))


threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
[t.start() for t in threads]
[t.join() for t in threads]
check(not errors, f"concurrent disjoint+duplicate chunks all consistent {errors[:3]}")
s = client.get("/devices/dev1").json()
check(s["slots"][target]["status"] == "complete"
      and s["slots"][target]["sha256"] == sha(cv)
      and s["slots"][target]["received_bytes"] == len(cv),
      "concurrent upload yields one complete, digest-correct slot")

# Exactly one of many concurrent confirms wins during a trial.
client.post("/devices/dev1/restart")  # trial of cv
tok = client.get("/devices/dev1").json()["pending_confirmation_token"]
wins, loses = [], []
lock = threading.Lock()
barrier2 = threading.Barrier(10)


def confirmer(i):
    barrier2.wait()
    t = tok if i == 0 else ("nope" if i % 2 else tok)
    r = client.post("/devices/dev1/confirm", json={"token": t})
    with lock:
        (wins if r.status_code == 200 else loses).append(r.status_code)


ths = [threading.Thread(target=confirmer, args=(i,)) for i in range(10)]
[t.start() for t in ths]
[t.join() for t in ths]
check(len(wins) == 1 and len(loses) == 9 and set(loses) == {409},
      f"exactly one concurrent confirm wins ({len(wins)}, {len(loses)})")

print(f"\nALL TESTS PASSED: {PASS} checks")
