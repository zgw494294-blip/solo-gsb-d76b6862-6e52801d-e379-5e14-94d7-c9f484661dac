"""端到端测试：用 FastAPI TestClient 覆盖全部需求场景。

运行：python test_api.py
"""
import base64
import importlib
import os
import sys
import tempfile
import threading

TMP = tempfile.mkdtemp(prefix="fwapi-")
os.environ["FIRMWARE_DB_PATH"] = os.path.join(TMP, "test.db")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app.main as m  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

m.init_db()  # 生产环境由 lifespan 调用；此处避免 TestClient 未进上下文

PASS = 0


def check(name, cond, extra=""):
    global PASS
    assert cond, f"FAIL: {name} {extra}"
    PASS += 1
    print(f"  ok - {name}")


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def new_client():
    return TestClient(m.app)


client = new_client()

print("== 1. 创建设备：初始镜像确认入库，限制 64 KiB ==")
fw_v1 = bytes(range(256)) * 4  # 1024 字节
r = client.post("/devices", json={
    "device_id": "dev1",
    "initial_image_base64": b64(fw_v1),
    "initial_sha256": __import__("hashlib").sha256(fw_v1).hexdigest(),
})
check("create 201", r.status_code == 201, r.status_code)
st = r.json()
check("initial confirmed slot0", st["confirmed_slot"] == 0 and st["running_slot"] == 0)
check("slot0 confirmed w/ digest",
      st["slots"]["slot0"]["status"] == "confirmed"
      and st["slots"]["slot0"]["image_sha256"] == __import__("hashlib").sha256(fw_v1).hexdigest())
check("slot1 empty", st["slots"]["slot1"]["status"] == "empty"
      and st["pending_token"] is None)

r = client.post("/devices", json={"device_id": "dev1", "initial_image_base64": b64(b"x")})
check("duplicate device 409", r.status_code == 409)

big = bytes(64 * 1024 + 1)
r = client.post("/devices", json={"device_id": "dev-big", "initial_image_base64": b64(big)})
check("image >64KiB 422", r.status_code == 422, r.status_code)

r = client.post("/devices", json={"device_id": "dev-empty", "initial_image_base64": ""})
check("empty image 422", r.status_code == 422)

r = client.post("/devices", json={
    "device_id": "dev-badsum", "initial_image_base64": b64(b"abc"),
    "initial_sha256": "00" * 32})
check("wrong initial sha 422", r.status_code == 422)

r = client.post("/devices", json={
    "device_id": "dev-badsum2", "initial_image_base64": b64(b"abc"),
    "initial_sha256": "not-hex"})
check("malformed initial sha 422", r.status_code == 422)

r = client.put("/devices/dev1/update", json={"length": 10, "sha256": "zz" * 32})
check("malformed declare sha 422", r.status_code == 422)

r = client.post("/devices", json={"device_id": "dev-b64", "initial_image_base64": "not base64!!"})
check("bad base64 422", r.status_code == 422)

r = client.get("/devices/nope")
check("unknown device 404", r.status_code == 404)

print("== 2. 声明更新只写另一槽 ==")
fw_v2 = b"FW2-" + bytes((i * 7 % 256 for i in range(30716)))  # 30720 字节
import hashlib
sha_v2 = hashlib.sha256(fw_v2).hexdigest()

r = client.put("/devices/dev1/update", json={"length": len(fw_v2), "sha256": sha_v2})
check("declare 200", r.status_code == 200, r.status_code)
s1 = r.json()["slots"]["slot1"]
check("slot1 in-progress", s1["status"] == "in-progress"
      and s1["declared_length"] == 30720 and s1["bytes_received"] == 0)
# 未声明设备直接发分块
client.post("/devices", json={"device_id": "dev2", "initial_image_base64": b64(b"hello")})
r = client.put("/devices/dev2/chunks", json={"offset": 0, "data_base64": b64(b"xx")})
check("chunk without declaration 409", r.status_code == 409)

print("== 3. 分块：重传幂等 / 重叠冲突 / 越界 ==")
def put_chunk(off, data):
    return client.put("/devices/dev1/chunks", json={"offset": off, "data_base64": b64(data)})

r = put_chunk(0, fw_v2[:4096])
check("chunk0 accepted", r.status_code == 200 and r.json()["candidate_status"] == "in-progress")
r = put_chunk(0, fw_v2[:4096])
check("identical redelivery 200", r.status_code == 200
      and r.json()["identical_redelivery"] is True)
# 同偏移、前缀相同但短 1 字节：重叠区字节一致（仅更短），允许
r = put_chunk(0, fw_v2[:4095])
check("same prefix shorter chunk allowed", r.status_code == 200
      and r.json()["identical_redelivery"] is False)
# 同偏移、字节不同（重叠区冲突）
bad = bytearray(fw_v2[:4096]); bad[100] ^= 0xFF
r = put_chunk(0, bytes(bad))
check("same offset diff content 409", r.status_code == 409
      and r.json()["detail"]["code"] == "overlap_conflict")
# 部分重叠内容不同
bad2 = bytearray(fw_v2[4090:4110]); bad2[3] ^= 0xFF
r = put_chunk(4090, bytes(bad2))
check("overlapping diff bytes 409", r.status_code == 409)
# 部分重叠内容相同（幂等扩展）
r = put_chunk(4090, fw_v2[4090:4110])
check("overlapping same bytes ok", r.status_code == 200)
# 越界（放在未覆盖区，单纯超声明末端）
r = put_chunk(30719, fw_v2[30719:30720] + b"X")
check("chunk past declared end 422", r.status_code == 422)

print("== 4. 不完整不能启动；摘要错误不能启动，旧镜像仍启动 ==")
r = client.post("/devices/dev1/restart")
j = r.json()
check("incomplete -> old image boots", r.status_code == 200
      and j["result"] == "confirmed" and j["running_slot"] == 0 and j["boot_token"] is None)
st = client.get("/devices/dev1").json()
check("still running confirmed slot0", st["running_slot"] == 0
      and st["pending_token"] is None)

# 用错误摘要声明：传完应得 digest-mismatch
fw_bad = bytes(range(100))
client.put("/devices/dev1/update", json={"length": 100, "sha256": "11" * 32})
r = put_chunk(0, fw_bad)
check("full upload wrong digest -> digest-mismatch",
      r.json()["candidate_status"] == "digest-mismatch" and r.json()["bootable"] is False)
r = client.post("/devices/dev1/restart")
j = r.json()
check("digest-mismatch -> old boots, candidate discarded",
      j["result"] == "confirmed" and j["running_slot"] == 0)
st = client.get("/devices/dev1").json()
check("discarded slot empty", st["slots"]["slot1"]["status"] == "empty"
      and st["slots"]["slot1"]["declared_length"] is None)

print("== 5. 完整且摘要相符 -> 试启，令牌持久化后返回 ==")
client.put("/devices/dev1/update", json={"length": len(fw_v2), "sha256": sha_v2})
for off in range(0, len(fw_v2), 4096):
    r = put_chunk(off, fw_v2[off:off + 4096])
check("last chunk -> complete bootable",
      r.json()["candidate_status"] == "complete" and r.json()["bootable"] is True)

r = client.post("/devices/dev1/restart")
j = r.json()
token = j["boot_token"]
check("trial started on slot1", j["result"] == "trial" and j["running_slot"] == 1
      and isinstance(token, str) and len(token) >= 32 and j["pending_confirmation"] is True)
st = client.get("/devices/dev1").json()
check("state trial-pending, token visible",
      st["trial_state"] == "trial-pending" and st["pending_token"] == token
      and st["running_image"]["slot"] == 1
      and st["running_image"]["sha256"] == sha_v2)

print("== 6. 试启未决：两槽禁止写入 ==")
r = client.put("/devices/dev1/update", json={"length": 10, "sha256": "22" * 32})
check("declare locked 409", r.status_code == 409
      and r.json()["detail"]["code"] == "trial_pending_write_locked")
r = put_chunk(0, fw_v2[:10])
check("chunk locked 409", r.status_code == 409)

print("== 7. 错误令牌 409；正确确认 -> 替换已确认 ==")
r = client.post("/devices/dev1/confirm?token=wrong-token")
check("wrong token 409", r.status_code == 409
      and r.json()["detail"]["code"] == "invalid_token")
r = client.post("/devices/dev1/confirm", params={"token": token})
check("confirm 200", r.status_code == 200, r.status_code)
st = client.get("/devices/dev1").json()
check("confirmed/running slot1, no token",
      st["confirmed_slot"] == 1 and st["running_slot"] == 1
      and st["trial_state"] == "running-confirmed" and st["pending_token"] is None)
check("slot1 confirmed, slot0 superseded",
      st["slots"]["slot1"]["status"] == "confirmed"
      and st["slots"]["slot1"]["image_sha256"] == sha_v2
      and st["slots"]["slot0"]["status"] == "superseded"
      and st["slots"]["slot0"]["image_sha256"] == hashlib.sha256(fw_v1).hexdigest())
r = client.post("/devices/dev1/confirm", params={"token": token})
check("late confirm after success 409", r.status_code == 409)

print("== 8. 更新现在写入另一槽（槽0），试启后不确认再重启 -> 回退废弃 ==")
fw_v3 = b"FW3" + bytes(i % 256 for i in range(4997))
sha_v3 = hashlib.sha256(fw_v3).hexdigest()
client.put("/devices/dev1/update", json={"length": len(fw_v3), "sha256": sha_v3})
st = client.get("/devices/dev1").json()
check("update targets slot0", st["slots"]["slot0"]["status"] == "in-progress"
      and st["slots"]["slot1"]["status"] == "confirmed")
for off in range(0, len(fw_v3), 4096):
    client.put("/devices/dev1/chunks", json={"offset": off, "data_base64": b64(fw_v3[off:off+4096])})
r = client.post("/devices/dev1/restart")
j = r.json()
token3 = j["boot_token"]
check("trial slot0 with token3", j["result"] == "trial" and j["running_slot"] == 0 and token3)

# 未确认再次重启 -> 回退
r = client.post("/devices/dev1/restart")
j = r.json()
check("rollback to slot1", j["result"] == "confirmed" and j["running_slot"] == 1
      and j["boot_token"] is None)
st = client.get("/devices/dev1").json()
check("rollback state clean", st["trial_state"] == "running-confirmed"
      and st["confirmed_slot"] == 1 and st["pending_token"] is None
      and st["slots"]["slot0"]["status"] == "empty")
r = client.post("/devices/dev1/confirm", params={"token": token3})
check("late confirm after rollback 409", r.status_code == 409
      and r.json()["detail"]["code"] == "no_trial_pending")

print("== 9. 服务重启（重新导入模块/重开连接）后状态保留 ==")
# 模拟分块中途服务重启：声明并传一半
fw_v4 = b"FW4" + bytes(i % 256 for i in range(19997))
sha_v4 = hashlib.sha256(fw_v4).hexdigest()
client.put("/devices/dev1/update", json={"length": len(fw_v4), "sha256": sha_v4})
client.put("/devices/dev1/chunks", json={"offset": 0, "data_base64": b64(fw_v4[:8000])})
# 进程重启：丢弃全部连接/模块级状态后重新建 client（DB 文件仍在）
importlib.reload(m)
client2 = TestClient(m.app)  # lifespan 会重新 init_db（CREATE IF NOT EXISTS）
with client2:
    pass
st = client2.get("/devices/dev1").json()
check("chunks survive service restart",
      st["slots"]["slot0"]["status"] == "in-progress"
      and st["slots"]["slot0"]["bytes_received"] == 8000)
# 接着传完并试启
for off in range(8000, len(fw_v4), 4096):
    client2.put("/devices/dev1/chunks", json={"offset": off, "data_base64": b64(fw_v4[off:off+4096])})
r = client2.post("/devices/dev1/restart")
token4 = r.json()["boot_token"]
check("trial after resumed upload", r.json()["result"] == "trial" and token4)
# 再“重启服务”，trial-pending 与令牌必须还在
importlib.reload(m)
client3 = TestClient(m.app)
st = client3.get("/devices/dev1").json()
check("pending trial survives service restart",
      st["trial_state"] == "trial-pending" and st["pending_token"] == token4
      and st["running_slot"] == 0)
# 服务重启后仍禁止写入
r = client3.put("/devices/dev1/update", json={"length": 1, "sha256": "33" * 32})
check("still write-locked after service restart", r.status_code == 409)
r = client3.post("/devices/dev1/confirm", params={"token": token4})
check("confirm works after service restart", r.status_code == 200)
st = client3.get("/devices/dev1").json()
check("now confirmed slot0 fw_v4", st["confirmed_slot"] == 0
      and st["running_image"]["sha256"] == sha_v4)

print("== 10. 并发：多线程同时写声明/分块/重启，不产生损坏状态 ==")
client3.put("/devices/dev1/update", json={"length": 300, "sha256": "aa" * 32})
errors = []
def worker(n):
    try:
        c = TestClient(m.app)
        r = c.put("/devices/dev1/chunks", json={"offset": 100 * n, "data_base64": b64(b"c" * 100)})
        if r.status_code not in (200, 409):
            errors.append((n, r.status_code, r.text))
    except Exception as e:  # 任何序列化失败都不可接受
        errors.append((n, "exc", repr(e)))

ts = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
[t.start() for t in ts]; [t.join() for t in ts]
check("concurrent chunks no 5xx/exception", not errors, errors)
st = client3.get("/devices/dev1").json()
check("300 bytes assembled, digest wrong -> mismatch",
      st["slots"]["slot1"]["bytes_received"] == 300
      and st["slots"]["slot1"]["status"] == "digest-mismatch")

# 并发重启/确认竞争：只能有一个结果方向，且库状态一致
client3.put("/devices/dev1/update", json={"length": 300, "sha256": hashlib.sha256(b"c"*300).hexdigest()})
for i in range(3):
    client3.put("/devices/dev1/chunks", json={"offset": 100*i, "data_base64": b64(b"c"*100)})
st = client3.get("/devices/dev1").json()
check("candidate complete", st["slots"]["slot1"]["status"] == "complete")
tok = {"v": None}
def restart_once():
    c = TestClient(m.app)
    r = c.post("/devices/dev1/restart")
    if r.json().get("boot_token"):
        tok["v"] = r.json()["boot_token"]
def confirm_once():
    # 等令牌出现后确认；restart 与 confirm 并发只可能产生合法终态
    for _ in range(100):
        if tok["v"]:
            break
    if tok["v"]:
        c = TestClient(m.app)
        c.post("/devices/dev1/confirm", params={"token": tok["v"]})
t1 = threading.Thread(target=restart_once)
t2 = threading.Thread(target=confirm_once)
t1.start(); t2.start(); t1.join(); t2.join()
st = client3.get("/devices/dev1").json()
check("post-race state legal",
      st["trial_state"] in ("running-confirmed", "trial-pending"))
if st["trial_state"] == "trial-pending":
    # 确认没赶上：补确认收尾
    r = client3.post("/devices/dev1/confirm", params={"token": st["pending_token"]})
    check("cleanup confirm", r.status_code == 200)

print("== 11. 列表/候选查询/健康 ==")
check("health", client3.get("/health").json()["status"] == "ok")
check("device list", set(client3.get("/devices").json()["devices"]) >= {"dev1", "dev2"})
cand = client3.get("/devices/dev1/candidate").json()
final_st = client3.get("/devices/dev1").json()
expected_candidate = 1 - final_st["confirmed_slot"]
check("candidate view",
      cand["candidate_slot"] == expected_candidate
      and "chunks" in cand and cand["bootable"] is False, cand)

print(f"\nALL {PASS} CHECKS PASSED")
