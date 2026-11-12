"""回执：已送达（对方通讯员存下了）和已读（对方的 AI 真的取走看了）。

不动信封，不动广播。收信方多一个只读的 GET /xlt/v1/receipt?id=…，只有原发信人的证书能问；
发信方定时去问。老版本收信方没有这个地址，会回 404 NOT_FOUND，发信方就记下「它不支持回执」，
停止询问，这封信一直停在「已送达」。
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from pathlib import Path

from xlt.common import now_iso, parse_time, read_json, write_json

# 对方这封信的状态走到下面任何一个，都说明信已经交到了 AI 手里：
# delivered = 频道推进了会话 / 常驻进程收下 / webhook 通了；replied、done、declined = AI 已经处理完。
READ_STATES = ("delivered", "replied", "done", "declined")

_mu = threading.Lock()


def _path(home) -> Path:
    return home.root / "receipts.json"


def _load(home) -> dict:
    data = read_json(_path(home), {"items": {}})
    data.setdefault("items", {})
    return data


def _save(home, data: dict) -> None:
    cutoff = time.time() - 8 * 86400
    items = data.get("items") or {}
    for key in [k for k, v in items.items() if float(v.get("t") or 0) < cutoff]:
        items.pop(key, None)
    write_json(_path(home), data)


# ---------------------------------------------------------------- 发信方

def record_delivered(home, env: dict) -> None:
    """信已经被对方通讯员收下。第一次记；已经有的不动（可能已经是已读）。"""
    msg_id = env.get("id")
    if not msg_id:
        return
    with _mu:
        data = _load(home)
        if msg_id in data["items"]:
            return
        data["items"][msg_id] = {
            "to_member": env.get("to_member") or env.get("to"),
            "delivered_at": now_iso(),
            "state": "stored",
            "t": time.time(),
            "created": env.get("created"),
            "next_check": time.time() + 1,
            "polls": 0,
        }
        _save(home, data)


def mark_read(home, msg_id: str, to_member: str = "", read_at: str | None = None, via: str = "") -> None:
    with _mu:
        data = _load(home)
        rec = data["items"].get(msg_id)
        if rec is None:
            rec = {"to_member": to_member, "delivered_at": read_at or now_iso(), "t": time.time(), "polls": 0}
            data["items"][msg_id] = rec
        if rec.get("state") == "read":
            return
        rec["state"] = "read"
        rec["read_at"] = read_at or now_iso()
        rec["via"] = via
        rec["next_check"] = None
        _save(home, data)


def get(home, msg_id: str) -> dict | None:
    return (_load(home)["items"]).get(msg_id)


def all_items(home) -> dict:
    return _load(home)["items"]


def due(home) -> list[tuple[str, dict]]:
    now_s = time.time()
    out = []
    for msg_id, rec in _load(home)["items"].items():
        if rec.get("state") != "stored" or rec.get("no_receipts"):
            continue
        nxt = rec.get("next_check")
        if nxt is None or now_s >= float(nxt):
            out.append((msg_id, rec))
    return out


def _interval(rec: dict) -> float:
    """刚送到时问得勤，之后越问越稀，最长一分钟。"""
    age = time.time() - float(rec.get("t") or time.time())
    if age < 60:
        return 3
    if age < 600:
        return 10
    return 60


def note_poll(home, msg_id: str, *, state: str | None = None, read_at: str | None = None,
              no_receipts: bool = False, error: str = "", retry_in: float | None = None) -> None:
    with _mu:
        data = _load(home)
        rec = data["items"].get(msg_id)
        if rec is None:
            return
        rec["polls"] = int(rec.get("polls") or 0) + 1
        rec["last_poll"] = now_iso()
        if error:
            rec["last_error"] = error
        if no_receipts:
            rec["no_receipts"] = True
            rec["next_check"] = None
        elif state == "read":
            rec["state"] = "read"
            rec["read_at"] = read_at or now_iso()
            rec["via"] = rec.get("via") or "poll"
            rec["next_check"] = None
        else:
            rec["next_check"] = time.time() + (retry_in if retry_in is not None else _interval(rec))
        if _expired_rec(rec):
            rec["next_check"] = None
            rec["no_receipts"] = True
        _save(home, data)


def _expired_rec(rec: dict) -> bool:
    try:
        return time.time() - float(rec.get("t") or 0) > 86400 + 3600
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------- 收信方

def find_env_path(home, msg_id: str) -> Path | None:
    if not msg_id:
        return None
    for path in home.inbox.rglob(f"{msg_id}.env.json"):
        return path
    return None


def read_info(home, env_path: Path, env: dict) -> tuple[bool, str | None, str]:
    """(是否已读, 读的时间, 依据)。"""
    state = env.get("state") or {}
    if state.get("read_at"):
        return True, state["read_at"], state.get("read_via") or "stamp"
    if state.get("status") in READ_STATES:
        return True, state.get("updated"), "status"
    taken = home.d("bridges", "pi", "inbox-taken", f"{env.get('id')}.letter.md")
    if taken.exists():
        stamp = datetime.fromtimestamp(taken.stat().st_mtime).astimezone().isoformat(timespec="seconds")
        return True, stamp, "pi"
    return False, None, ""


def receipt_view(home, env_path: Path, env: dict) -> dict:
    read, read_at, _via = read_info(home, env_path, env)
    return {
        "id": env.get("id"),
        "state": "read" if read else "stored",
        "stored_at": (env.get("state") or {}).get("received"),
        "read_at": read_at,
        "status": (env.get("state") or {}).get("status"),
    }


def mark_read_local(home, msg_id: str, via: str = "xlt-read") -> bool:
    """xlt read 用。信还在本机信箱里就打上「已读」。多进程安全。"""
    path = find_env_path(home, msg_id)
    if path is None:
        return False
    with home.lock():
        env = read_json(path, None)
        if not isinstance(env, dict):
            return False
        state = env.setdefault("state", {})
        if not state.get("read_at"):
            state["read_at"] = now_iso()
            state["read_via"] = via
            write_json(path, env)
    return True


def stamp_read_if_needed(state: dict, status: str | None) -> None:
    """引擎改状态时顺手盖章：第一次走到「已交给 AI」的那一刻就是已读时间。"""
    if status in READ_STATES and not state.get("read_at"):
        state["read_at"] = state.get("updated") or now_iso()
        state["read_via"] = "status"
