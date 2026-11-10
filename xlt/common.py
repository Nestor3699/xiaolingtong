"""时间、原子写、文件锁、签名用的 JSON 规范化。"""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

LETTER_MAX = 64 * 1024
WIRE_FIELDS = (
    "envelope_v",
    "id",
    "type",
    "from",
    "from_person",
    "to",
    "created",
    "ttl_s",
    "in_reply_to",
    "subject",
    "tags",
    "letter_bytes",
    "letter_sha256",
)


def now() -> datetime:
    return datetime.now().astimezone()


def now_iso() -> str:
    return now().isoformat(timespec="seconds")


def parse_time(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=now().tzinfo)
    return dt


def canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def wire_view(env: dict) -> dict:
    view = {}
    for key in WIRE_FIELDS:
        view[key] = env.get(key)
    if view["tags"] is None:
        view["tags"] = []
    return view


def envelope_summary(env: dict) -> dict:
    keys = (
        "id",
        "type",
        "from",
        "from_person",
        "to",
        "created",
        "ttl_s",
        "in_reply_to",
        "subject",
        "tags",
        "letter_bytes",
        "letter_sha256",
    )
    out = {key: env.get(key) for key in keys}
    state = env.get("state") or {}
    if state:
        out["state"] = {
            "status": state.get("status"),
            "updated": state.get("updated"),
            "routed_to": state.get("routed_to"),
            "routed_by": state.get("routed_by"),
            "grant": state.get("grant"),
            "taken_over": state.get("taken_over"),
            "binding_gen": state.get("binding_gen"),
            "copy_fallback": state.get("copy_fallback"),
            "last_error": state.get("last_error"),
        }
    return out


def clean_subject(text: str) -> str:
    cleaned = "".join(ch for ch in (text or "") if ch >= " " and ch != "\x7f")
    cleaned = " ".join(cleaned.split())
    return cleaned[:80]


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    if os.name != "nt":
        os.chmod(path, mode)


def atomic_text(path: Path, text: str, mode: int = 0o600) -> None:
    atomic_write(path, text.encode("utf-8"), mode)


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, obj, mode: int = 0o600) -> None:
    atomic_text(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n", mode)


_guard = threading.Lock()
_held: dict[str, threading.RLock] = {}


def _thread_lock(path: Path) -> threading.RLock:
    key = str(path)
    with _guard:
        lock = _held.get(key)
        if lock is None:
            lock = threading.RLock()
            _held[key] = lock
        return lock


@contextmanager
def exclusive(path: Path):
    """同一进程可重入，跨进程用 flock。CLI 和通讯员会同时改信箱。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    thread = _thread_lock(path)
    thread.acquire()
    fh = open(path, "a+")
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()
            thread.release()
