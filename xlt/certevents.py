"""证书被替换的记录。自动登记照旧替换（主人接受了这个风险），但要让人第一时间看到。

记录写在 cert-events.json，点「知道了」才算看过；通讯员重启也还在。
"""

from __future__ import annotations

import threading
import uuid

from xlt.common import now_iso, read_json, write_json

_mu = threading.Lock()


def _path(home):
    return home.root / "cert-events.json"


def _load(home) -> dict:
    data = read_json(_path(home), {"events": []})
    data.setdefault("events", [])
    return data


def short_fp(fp: str) -> str:
    """指纹太长，认人只看头尾各 4 个字节。"""
    parts = [p for p in str(fp or "").upper().split(":") if p]
    if len(parts) <= 8:
        return ":".join(parts) or "—"
    return ":".join(parts[:4]) + " … " + ":".join(parts[-4:])


def record_replacement(home, name: str, owner: str, old_fp: str, new_fp: str, ip: str) -> dict:
    with _mu:
        data = _load(home)
        for item in data["events"]:
            if not item.get("dismissed") and item.get("name") == name and item.get("new_fp") == new_fp:
                return item
        event = {
            "id": "cert-" + uuid.uuid4().hex[:10],
            "name": name,
            "owner": owner or name,
            "ts": now_iso(),
            "old_fp": old_fp,
            "new_fp": new_fp,
            "ip": ip,
            "dismissed": False,
        }
        data["events"].append(event)
        data["events"] = data["events"][-50:]
        write_json(_path(home), data)
        return event


def active(home) -> list[dict]:
    return [dict(item) for item in _load(home)["events"] if not item.get("dismissed")]


def dismiss(home, event_id: str) -> int:
    """event_id 写 "all" 就全部点掉。返回点掉了几条。"""
    from xlt.store import load_cache, save_cache

    count = 0
    names = set()
    with _mu:
        data = _load(home)
        for item in data["events"]:
            if item.get("dismissed"):
                continue
            if event_id == "all" or item.get("id") == event_id:
                item["dismissed"] = True
                item["dismissed_at"] = now_iso()
                names.add(item.get("name"))
                count += 1
        if count:
            write_json(_path(home), data)
    if names:
        still = {item.get("name") for item in active(home)}
        cache = load_cache(home)
        changed = False
        for name in names - still:
            row = (cache.get("peers") or {}).get(name)
            if row and row.get("cert_warning"):
                row["cert_warning"] = ""
                changed = True
        if changed:
            save_cache(home, cache)
    return count
