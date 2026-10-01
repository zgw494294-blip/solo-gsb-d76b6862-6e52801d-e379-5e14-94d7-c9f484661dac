"""Dual-slot firmware trial-boot HTTP API.

Simulates an A/B firmware update flow on top of SQLite, without any hardware:

* A device is created with one confirmed initial image (<= 64 KiB per image).
* Updates are declared (length + SHA-256) and only ever written to the slot
  opposite the confirmed one, as Base64 chunks submitted at byte offsets.
* Identical retransmissions at the same position are accepted; if a chunk
  overlaps existing data with different bytes, the whole chunk is rejected.
* A slot only becomes a bootable candidate when it is fully covered and its
  digest matches the declared one.
* Restarting a device with a valid candidate trial-boots it and returns a
  unique boot token; the "trial booted" marker is persisted before responding.
* Confirming with that token promotes the candidate. Restarting again while a
  trial is pending falls back to the old confirmed image and discards the
  candidate; a late (or wrong) confirmation gets HTTP 409.
* No slot may be written while a trial is pending.
* Service restart does NOT reboot the device: chunks, slots and trial state
  all live in SQLite on the persistent volume.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import secrets
import sqlite3
import threading
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator

MAX_IMAGE_SIZE = 64 * 1024  # 64 KiB hard limit for every image
DB_PATH = os.environ.get("FIRMWARE_DB_PATH", "/data/firmware.db")

_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# All state-changing requests take this lock, so concurrent API calls are
# equivalent to some serial execution. The app runs with a single uvicorn
# worker, so one in-process lock is sufficient.
_write_lock = threading.RLock()


# --------------------------------------------------------------------------- #
# Database helpers
# --------------------------------------------------------------------------- #

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _init_db() -> None:
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = _connect()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS device (
                device_id        TEXT PRIMARY KEY,
                confirmed_slot   TEXT NOT NULL CHECK (confirmed_slot IN ('a','b')),
                running_slot     TEXT NOT NULL CHECK (running_slot   IN ('a','b')),
                trial_pending    INTEGER NOT NULL DEFAULT 0,
                pending_token    TEXT,
                a_status         TEXT NOT NULL DEFAULT 'empty'
                                   CHECK (a_status IN ('empty','uploading','complete')),
                a_declared_len   INTEGER,
                a_declared_sha   TEXT,
                a_actual_sha     TEXT,
                a_verified       INTEGER NOT NULL DEFAULT 0,
                a_error          TEXT,
                b_status         TEXT NOT NULL DEFAULT 'empty'
                                   CHECK (b_status IN ('empty','uploading','complete')),
                b_declared_len   INTEGER,
                b_declared_sha   TEXT,
                b_actual_sha     TEXT,
                b_verified       INTEGER NOT NULL DEFAULT 0,
                b_error          TEXT
            );

            CREATE TABLE IF NOT EXISTS chunk (
                device_id TEXT NOT NULL,
                slot      TEXT NOT NULL CHECK (slot IN ('a','b')),
                offset    INTEGER NOT NULL CHECK (offset >= 0),
                length    INTEGER NOT NULL CHECK (length > 0),
                data      BLOB NOT NULL,
                sha256    TEXT NOT NULL,
                PRIMARY KEY (device_id, slot, offset)
            );
            CREATE INDEX IF NOT EXISTS idx_chunk_device_slot
                ON chunk (device_id, slot);
            """
        )
        conn.commit()
    finally:
        conn.close()


@asynccontextmanager
async def lifespan(_: FastAPI):
    _init_db()
    yield


app = FastAPI(
    title="Dual-slot firmware trial-boot API",
    version="1.0.0",
    lifespan=lifespan,
)


# Per-slot column names.
SLOT_COLS: dict[str, dict[str, str]] = {
    "a": {
        "status": "a_status",
        "declared_len": "a_declared_len",
        "declared_sha": "a_declared_sha",
        "actual_sha": "a_actual_sha",
        "verified": "a_verified",
        "error": "a_error",
    },
    "b": {
        "status": "b_status",
        "declared_len": "b_declared_len",
        "declared_sha": "b_declared_sha",
        "actual_sha": "b_actual_sha",
        "verified": "b_verified",
        "error": "b_error",
    },
}


def _other_slot(slot: str) -> str:
    return "b" if slot == "a" else "a"


def _get_device(conn: sqlite3.Connection, device_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM device WHERE device_id = ?", (device_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="device not found")
    return row


def _reset_slot(conn: sqlite3.Connection, device_id: str, slot: str) -> None:
    """Wipe a slot completely (chunks and session metadata)."""
    c = SLOT_COLS[slot]
    conn.execute("DELETE FROM chunk WHERE device_id = ? AND slot = ?",
                 (device_id, slot))
    conn.execute(
        f"""UPDATE device SET
                {c['status']}       = 'empty',
                {c['declared_len']} = NULL,
                {c['declared_sha']} = NULL,
                {c['actual_sha']}   = NULL,
                {c['verified']}     = 0,
                {c['error']}        = NULL
            WHERE device_id = ?""",
        (device_id,),
    )


def _set_slot_session(
    conn: sqlite3.Connection,
    device_id: str,
    slot: str,
    declared_len: int,
    declared_sha: str,
) -> None:
    c = SLOT_COLS[slot]
    conn.execute(
        f"""UPDATE device SET
                {c['status']}       = 'uploading',
                {c['declared_len']} = ?,
                {c['declared_sha']} = ?,
                {c['actual_sha']}   = NULL,
                {c['verified']}     = 0,
                {c['error']}        = NULL
            WHERE device_id = ?""",
        (declared_len, declared_sha, device_id),
    )


def _chunks(conn: sqlite3.Connection, device_id: str, slot: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT offset, length, data, sha256 FROM chunk "
        "WHERE device_id = ? AND slot = ? ORDER BY offset",
        (device_id, slot),
    ).fetchall()


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[list[int]] = []
    for start, end in sorted(ranges):
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(s, e) for s, e in merged]


def _coverage(chunks: list[sqlite3.Row]) -> list[tuple[int, int]]:
    return _merge_ranges([(r["offset"], r["offset"] + r["length"]) for r in chunks])


def _received_bytes(chunks: list[sqlite3.Row]) -> int:
    return sum(end - start for start, end in _coverage(chunks))


def _reassemble(chunks: list[sqlite3.Row], declared_len: int) -> bytes:
    buf = bytearray(declared_len)
    for r in chunks:
        buf[r["offset"]:r["offset"] + r["length"]] = r["data"]
    return bytes(buf)


def _finalize_if_complete(
    conn: sqlite3.Connection, row: sqlite3.Row, slot: str
) -> None:
    """If the slot is fully covered, verify digest and mark complete/bad."""
    c = SLOT_COLS[slot]
    declared_len = row[c["declared_len"]]
    declared_sha = row[c["declared_sha"]]
    if declared_len is None:
        return
    chunks = _chunks(conn, row["device_id"], slot)
    covered = _coverage(chunks)
    full = (declared_len == 0 and covered == []) or covered == [(0, declared_len)]
    if not full:
        return

    actual = _reassemble(chunks, declared_len)
    digest = hashlib.sha256(actual).hexdigest()
    if digest == declared_sha:
        conn.execute(
            f"""UPDATE device SET
                    {c['status']}     = 'complete',
                    {c['actual_sha']} = ?,
                    {c['verified']}   = 1,
                    {c['error']}      = NULL
                WHERE device_id = ?""",
            (digest, row["device_id"]),
        )
    else:
        # Fully received but digest wrong: never bootable. The session must be
        # re-declared (which resets the slot) before it can be retried.
        conn.execute(
            f"""UPDATE device SET
                    {c['status']}     = 'uploading',
                    {c['actual_sha']} = NULL,
                    {c['verified']}   = 0,
                    {c['error']}      = 'sha256_mismatch'
                WHERE device_id = ?""",
            (row["device_id"],),
        )


def _slot_view(
    conn: sqlite3.Connection, row: sqlite3.Row, slot: str
) -> dict[str, Any]:
    c = SLOT_COLS[slot]
    chunks = _chunks(conn, row["device_id"], slot)
    if row["trial_pending"]:
        role = "trial" if slot == row["running_slot"] else "confirmed"
    else:
        role = "confirmed" if slot == row["confirmed_slot"] else "standby"
    return {
        "slot": slot,
        "role": role,
        "status": row[c["status"]],          # empty | uploading | complete
        "verified": bool(row[c["verified"]]),
        "declared_length": row[c["declared_len"]],
        "declared_sha256": row[c["declared_sha"]],
        "sha256": row[c["actual_sha"]],      # digest once verified, else null
        "received_bytes": _received_bytes(chunks),
        "error": row[c["error"]],
    }


def _device_view(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    running_sha = row[SLOT_COLS[row["running_slot"]]["actual_sha"]]
    return {
        "device_id": row["device_id"],
        "trial_pending": bool(row["trial_pending"]),
        "confirmed_slot": row["confirmed_slot"],
        "running_slot": row["running_slot"],
        "running_image": {"slot": row["running_slot"], "sha256": running_sha},
        "pending_confirmation_token": row["pending_token"],
        "slots": {
            "a": _slot_view(conn, row, "a"),
            "b": _slot_view(conn, row, "b"),
        },
    }


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #

class CreateDeviceRequest(BaseModel):
    device_id: Optional[str] = Field(
        default=None, description="Optional explicit id; a random one otherwise."
    )
    initial_image_base64: str
    initial_image_sha256: Optional[str] = None

    @field_validator("device_id")
    @classmethod
    def _id_ok(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not _DEVICE_ID_RE.fullmatch(v):
            raise ValueError("allowed: [A-Za-z0-9_-]{1,64}")
        return v

    @field_validator("initial_image_sha256")
    @classmethod
    def _sha_norm(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.lower()
        if not _SHA256_RE.fullmatch(v):
            raise ValueError("must be 64 hex chars")
        return v


class StartUpdateRequest(BaseModel):
    declared_length: int = Field(ge=0, le=MAX_IMAGE_SIZE)
    sha256: str

    @field_validator("sha256")
    @classmethod
    def _sha_ok(cls, v: str) -> str:
        v = v.lower()
        if not _SHA256_RE.fullmatch(v):
            raise ValueError("must be 64 hex chars")
        return v


class ChunkRequest(BaseModel):
    offset: int = Field(ge=0)
    data: str = Field(description="Base64 encoded chunk bytes")

    @field_validator("data")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v:
            raise ValueError("data must not be empty")
        return v


class ConfirmRequest(BaseModel):
    token: str = Field(min_length=1)


# --------------------------------------------------------------------------- #
# Helpers shared by endpoints
# --------------------------------------------------------------------------- #

def _b64decode(value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail="invalid base64 data")


def _guard_no_trial(row: sqlite3.Row) -> None:
    if row["trial_pending"]:
        raise HTTPException(
            status_code=409,
            detail="trial boot is pending; both slots are read-only until "
                   "confirm or rollback (restart)",
        )


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #

@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/devices", status_code=201)
def create_device(body: CreateDeviceRequest) -> dict[str, Any]:
    image = _b64decode(body.initial_image_base64)
    if len(image) > MAX_IMAGE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"initial image exceeds {MAX_IMAGE_SIZE} bytes",
        )
    digest = hashlib.sha256(image).hexdigest()
    if body.initial_image_sha256 is not None and \
            body.initial_image_sha256 != digest:
        raise HTTPException(
            status_code=422,
            detail="initial_image_sha256 does not match provided image",
        )

    device_id = body.device_id or secrets.token_hex(8)
    with _write_lock:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            exists = conn.execute(
                "SELECT 1 FROM device WHERE device_id = ?", (device_id,)
            ).fetchone()
            if exists is not None:
                raise HTTPException(status_code=409, detail="device already exists")

            # The confirmed initial image lives in slot a.
            conn.execute(
                """INSERT INTO device (
                       device_id, confirmed_slot, running_slot, trial_pending,
                       pending_token,
                       a_status, a_declared_len, a_declared_sha, a_actual_sha,
                       a_verified, a_error,
                       b_status, b_declared_len, b_declared_sha, b_actual_sha,
                       b_verified, b_error
                   ) VALUES (?, 'a', 'a', 0, NULL,
                             'complete', ?, NULL, ?, 1, NULL,
                             'empty', NULL, NULL, NULL, 0, NULL)""",
                (device_id, len(image), digest),
            )
            if image:
                conn.execute(
                    "INSERT INTO chunk (device_id, slot, offset, length, data, sha256)"
                    " VALUES (?, 'a', 0, ?, ?, ?)",
                    (device_id, len(image), image, digest),
                )
            conn.commit()
            row = _get_device(conn, device_id)
            return _device_view(conn, row)
        except HTTPException:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


@app.get("/devices")
def list_devices() -> dict[str, list[str]]:
    with _write_lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT device_id FROM device ORDER BY device_id"
            ).fetchall()
            return {"devices": [r["device_id"] for r in rows]}
        finally:
            conn.close()


@app.get("/devices/{device_id}")
def device_status(device_id: str) -> dict[str, Any]:
    with _write_lock:
        conn = _connect()
        try:
            row = _get_device(conn, device_id)
            return _device_view(conn, row)
        finally:
            conn.close()


@app.post("/devices/{device_id}/updates")
def start_update(device_id: str, body: StartUpdateRequest) -> dict[str, Any]:
    """Declare an update session targeting the non-confirmed slot."""
    with _write_lock:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = _get_device(conn, device_id)
            _guard_no_trial(row)

            target = _other_slot(row["confirmed_slot"])
            _reset_slot(conn, device_id, target)
            _set_slot_session(
                conn, device_id, target, body.declared_length, body.sha256
            )
            # A zero-length image is complete the moment it is declared.
            _finalize_if_complete(
                conn, _get_device(conn, device_id), target
            )
            conn.commit()

            row = _get_device(conn, device_id)
            return {
                "target_slot": target,
                "slot": _slot_view(conn, row, target),
            }
        except HTTPException:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


@app.put("/devices/{device_id}/chunks")
def put_chunk(device_id: str, body: ChunkRequest) -> dict[str, Any]:
    """Append/retransmit one Base64 chunk at a byte offset."""
    data = _b64decode(body.data)
    if not data:
        raise HTTPException(status_code=422, detail="empty chunk")

    with _write_lock:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = _get_device(conn, device_id)
            _guard_no_trial(row)

            target = _other_slot(row["confirmed_slot"])
            c = SLOT_COLS[target]
            if row[c["status"]] not in ("uploading", "complete") \
                    or row[c["declared_len"]] is None:
                raise HTTPException(
                    status_code=409,
                    detail="no active update session on the standby slot; "
                           "declare an update first",
                )

            declared_len: int = row[c["declared_len"]]
            offset = body.offset
            end = offset + len(data)
            if end > declared_len:
                raise HTTPException(
                    status_code=416,
                    detail=f"chunk [{offset},{end}) exceeds declared length "
                           f"{declared_len}",
                )

            chunk_digest = hashlib.sha256(data).hexdigest()
            duplicate = False
            for r in _chunks(conn, device_id, target):
                r_start, r_end = r["offset"], r["offset"] + r["length"]
                if r_end <= offset or r_start >= end:
                    continue  # disjoint
                if r_start == offset and r_end == end:
                    # Exact same range: same content is an idempotent
                    # retransmission; different content rejects the block.
                    if r["sha256"] == chunk_digest:
                        duplicate = True
                        break
                    raise HTTPException(
                        status_code=409,
                        detail="different content already stored at same range",
                    )
                # Partial overlap: every shared byte must be identical.
                ov_start, ov_end = max(r_start, offset), min(r_end, end)
                old = bytes(r["data"][ov_start - r_start:ov_end - r_start])
                new = data[ov_start - offset:ov_end - offset]
                if old != new:
                    raise HTTPException(
                        status_code=409,
                        detail=f"overlapping bytes differ at "
                               f"[{ov_start},{ov_end}); whole chunk rejected",
                    )

            if not duplicate:
                conn.execute(
                    "INSERT INTO chunk (device_id, slot, offset, length, data, sha256)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (device_id, target, offset, len(data), data, chunk_digest),
                )
                row = _get_device(conn, device_id)
                _finalize_if_complete(conn, row, target)
            conn.commit()

            row = _get_device(conn, device_id)
            view = _slot_view(conn, row, target)
            return {
                "target_slot": target,
                "accepted": True,
                "duplicate": duplicate,
                "slot": view,
                "complete": view["status"] == "complete",
            }
        except HTTPException:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


@app.post("/devices/{device_id}/restart")
def restart(device_id: str) -> dict[str, Any]:
    """Reboot the simulated device.

    * Trial pending -> roll back to the confirmed image, discard candidate.
    * Complete verified candidate on the standby slot -> trial-boot it and
      return a unique token (persisted before the response is returned).
    * Otherwise -> boot the confirmed image; any incomplete/bad candidate
      session is discarded.
    """
    with _write_lock:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = _get_device(conn, device_id)

            if row["trial_pending"]:
                # Fallback: running becomes the still-confirmed slot and the
                # candidate slot is wiped. The old token stops working.
                candidate_slot = _other_slot(row["confirmed_slot"])
                _reset_slot(conn, device_id, candidate_slot)
                conn.execute(
                    """UPDATE device
                       SET running_slot = confirmed_slot,
                           trial_pending = 0,
                           pending_token = NULL
                     WHERE device_id = ?""",
                    (device_id,),
                )
                conn.commit()
                row = _get_device(conn, device_id)
                result = _device_view(conn, row)
                result.update(
                    action="rollback",
                    token=None,
                )
                return result

            candidate = _other_slot(row["confirmed_slot"])
            c = SLOT_COLS[candidate]
            ready = row[c["status"]] == "complete" and bool(row[c["verified"]])

            if ready:
                # Persist the trial marker + unique token BEFORE returning.
                token = secrets.token_hex(32)
                conn.execute(
                    """UPDATE device
                       SET running_slot = ?,
                           trial_pending = 1,
                           pending_token = ?
                     WHERE device_id = ?""",
                    (candidate, token, device_id),
                )
                conn.commit()
                row = _get_device(conn, device_id)
                result = _device_view(conn, row)
                result.update(action="trial", token=token)
                return result

            # No bootable candidate: stay on (and boot) the confirmed image.
            # An incomplete or digest-bad candidate cannot be booted; drop it.
            if row[c["status"]] != "empty":
                _reset_slot(conn, device_id, candidate)
            conn.commit()
            row = _get_device(conn, device_id)
            result = _device_view(conn, row)
            result.update(action="boot_confirmed", token=None)
            return result
        except HTTPException:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


@app.post("/devices/{device_id}/confirm")
def confirm(device_id: str, body: ConfirmRequest) -> dict[str, Any]:
    """Confirm a trial boot with its token; promotes candidate to confirmed."""
    with _write_lock:
        conn = _connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = _get_device(conn, device_id)

            if not row["trial_pending"]:
                # Late confirmation after rollback (or nothing to confirm).
                raise HTTPException(
                    status_code=409,
                    detail="no trial boot pending; confirmation rejected",
                )
            if row["pending_token"] is None or not secrets.compare_digest(
                row["pending_token"], body.token
            ):
                raise HTTPException(
                    status_code=409, detail="invalid or stale boot token"
                )

            promoted = row["running_slot"]
            old = _other_slot(promoted)
            _reset_slot(conn, device_id, old)
            conn.execute(
                """UPDATE device
                   SET confirmed_slot = ?,
                       trial_pending = 0,
                       pending_token = NULL
                 WHERE device_id = ?""",
                (promoted, device_id),
            )
            conn.commit()
            row = _get_device(conn, device_id)
            result = _device_view(conn, row)
            result.update(action="confirmed")
            return result
        except HTTPException:
            conn.rollback()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
