"""本机 ~/.xlt 目录：名单、权限、路由、窗口、信箱文件。"""

from __future__ import annotations

import json
import os
import platform
import shutil
from pathlib import Path

from xlt.common import atomic_text, exclusive, now_iso, read_json, write_json
from xlt.crypto import fingerprint_cert, load_cert
from xlt.yamlutil import dumps, loads

AI_TOOLS = ["claude-code", "deepseek-harness", "codex", "grok-bot", "pi"]

POLICY_TEMPLATE = """\
# 只有本机主人能改。没写允许的，一律拒绝。
version: 1
owner: {person}
default: deny
mode: normal
away:
  from: null
  until: null
grants: []
limits:
  rate_per_minute: 30
  max_concurrent: 4
audit:
  retention_days: 90
"""

ROUTES_TEMPLATE = """\
# 只看信封，不看信纸。default 必须是已经登记的窗口。
version: 1
default: ""
on_window_lost: fallback_default
busy_timeout_s: 600
rules: []
"""


class Home:
    def __init__(self, path: str | Path):
        self.root = Path(path).expanduser().resolve()

    def d(self, *parts: str) -> Path:
        return self.root.joinpath(*parts)

    @property
    def tls(self) -> Path:
        return self.root / "tls"

    @property
    def peers_dir(self) -> Path:
        return self.root / "peers"

    @property
    def inbox(self) -> Path:
        return self.root / "inbox"

    @property
    def pending(self) -> Path:
        return self.root / "pending"

    @property
    def outbox(self) -> Path:
        return self.root / "outbox"

    @property
    def lock_path(self) -> Path:
        return self.root / ".lock"

    def ensure(self) -> None:
        for rel in (
            "tls",
            "peers",
            "inbox",
            "inbox/_hold",
            "inbox/_expired",
            "pending",
            "pending/expired",
            "pending/failed",
            "outbox",
            "channels",
            "secrets",
            "workdir",
            "bridges/pi/inbox",
            "bridges/pi/outbox",
            "bridges/pi/inbox-taken",
            "popups",
            "spool",
        ):
            self.d(rel).mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(self.root, 0o700)

    def lock(self):
        return exclusive(self.lock_path)


def config_path(home: Home) -> Path:
    return home.root / "config.json"


def default_config(me: str, person: str, owner: str) -> dict:
    system = "windows" if platform.system().lower().startswith("win") else "macos" if platform.system() == "Darwin" else "linux"
    return {
        "me": me,
        "person": person,
        "owner": owner,
        "os": system,
        "tcp_port": 47800,
        "udp_port": 47801,
        "panel_port": 47321,
        "bind_ip": "",
        "announce_ip": "",
        "announce_targets": [],
        "heartbeat_s": 180,
        "ip_auto": True,
        "mdns": False,
    }


def load_config(home: Home) -> dict:
    data = read_json(config_path(home), {})
    base = default_config(data.get("me", ""), data.get("person", ""), data.get("owner", ""))
    base.update(data)
    return base


def save_config(home: Home, cfg: dict) -> None:
    write_json(config_path(home), cfg)


def person_from_machine(name: str) -> str:
    for suffix in ("-mac", "-win"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def init_home(home: Home, me: str, person: str, owner: str) -> str:
    home.ensure()
    from xlt.crypto import generate_cert

    _key, crt, fp = generate_cert(me, home.tls)
    shutil.copyfile(crt, home.peers_dir / f"{me}.crt")
    cfg = load_config(home)
    cfg.update(default_config(me, person, owner))
    cfg["me"] = me
    cfg["person"] = person
    cfg["owner"] = owner
    save_config(home, cfg)
    agents = [member_record(me, person, owner, cfg["os"], fp, primary=True)]
    save_directory(home, agents)
    policy = home.root / "policy.yaml"
    if not policy.exists():
        atomic_text(policy, POLICY_TEMPLATE.format(person=person))
    routes = home.root / "routes.yaml"
    if not routes.exists():
        atomic_text(routes, ROUTES_TEMPLATE)
    if not (home.root / "windows.json").exists():
        write_json(home.root / "windows.json", {"windows": []})
    return fp


def member_record(name, person, owner, system, fp, lan_ip="", port=47800, tailscale_ip="", primary=False) -> dict:
    rec = {
        "name": name,
        "owner": owner,
        "person": person,
        "os": system,
        "lan_ip": lan_ip or "",
        "port": port,
        "cert_sha256": fp,
        "tailscale_ip": tailscale_ip or "",
        "ai_tools": list(AI_TOOLS),
    }
    if primary:
        rec["primary"] = True
    return rec


def directory_path(home: Home) -> Path:
    return home.root / "xlt-directory.yaml"


def load_directory(home: Home) -> list[dict]:
    path = directory_path(home)
    if not path.exists():
        return []
    data = loads(path.read_text(encoding="utf-8")) or {}
    agents = data.get("agents") or []
    ready = []
    for agent in agents:
        fp = str(agent.get("cert_sha256") or "")
        if not fp or "待补" in fp or len(fp) < 16:
            continue
        ready.append(agent)
    return ready


def save_directory(home: Home, agents: list[dict]) -> None:
    doc = {"version": 1, "updated": now_iso(), "agents": agents}
    atomic_text(directory_path(home), dumps(doc))


def trust_member(home: Home, crt: Path, name: str, person: str, owner: str, system: str, lan_ip: str, port: int, tailscale_ip: str, primary: bool) -> str:
    cert = load_cert(crt)
    fp = fingerprint_cert(cert)
    dest = home.peers_dir / f"{name}.crt"
    dest.write_bytes(crt.read_bytes())
    if os.name != "nt":
        os.chmod(dest, 0o600)
    agents = []
    raw = loads(directory_path(home).read_text(encoding="utf-8")) if directory_path(home).exists() else {}
    for agent in (raw or {}).get("agents") or []:
        if agent.get("name") != name:
            agents.append(agent)
    agents.append(member_record(name, person, owner, system, fp, lan_ip, port, tailscale_ip, primary))
    save_directory(home, agents)
    return fp


def peer_cert(home: Home, name: str):
    path = home.peers_dir / f"{name}.crt"
    if not path.exists():
        return None
    return load_cert(path)


def member_by_name(home: Home, name: str) -> dict | None:
    for agent in load_directory(home):
        if agent.get("name") == name:
            return agent
    return None


def member_by_fp(home: Home, fp: str) -> dict | None:
    want = fp.upper()
    for agent in load_directory(home):
        if str(agent.get("cert_sha256", "")).upper() == want:
            return agent
    return None


def resolve_person(home: Home, target: str) -> dict | None:
    """`--to haonian` 或 `haonian@haonian-win`。一人多台时认 primary。"""
    if "@" in target:
        person, _sep, machine = target.partition("@")
        found = member_by_name(home, machine)
        if found and found.get("person") in (person, machine):
            return found
        return found if found and found.get("name") == machine else None
    matches = [a for a in load_directory(home) if a.get("person") == target or a.get("name") == target]
    if not matches:
        return None
    primary = [a for a in matches if a.get("primary")]
    if primary:
        return primary[0]
    if len(matches) == 1:
        return matches[0]
    return None


def load_policy(home: Home) -> dict:
    path = home.root / "policy.yaml"
    if not path.exists():
        return {"version": 1, "default": "deny", "mode": "normal", "grants": [], "limits": {"rate_per_minute": 30}}
    data = loads(path.read_text(encoding="utf-8")) or {}
    data.setdefault("default", "deny")
    data.setdefault("mode", "normal")
    data.setdefault("grants", [])
    data.setdefault("limits", {})
    return data


def load_routes(home: Home) -> dict:
    path = home.root / "routes.yaml"
    if not path.exists():
        return {"version": 1, "default": "", "on_window_lost": "fallback_default", "busy_timeout_s": 600, "rules": []}
    data = loads(path.read_text(encoding="utf-8")) or {}
    data.setdefault("default", "")
    data.setdefault("on_window_lost", "fallback_default")
    data.setdefault("busy_timeout_s", 600)
    data.setdefault("rules", [])
    return data


def save_routes(home: Home, routes: dict) -> None:
    path = home.root / "routes.yaml"
    if path.exists():
        shutil.copyfile(path, home.root / "routes.yaml.bak")
    atomic_text(path, dumps(routes))


def load_windows(home: Home) -> list[dict]:
    data = read_json(home.root / "windows.json", {"windows": []})
    return list(data.get("windows") or [])


def save_windows(home: Home, windows: list[dict]) -> None:
    write_json(home.root / "windows.json", {"windows": windows})


def find_window(home: Home, name: str) -> dict | None:
    for item in load_windows(home):
        if item.get("window") == name:
            return item
    return None


def window_ref(agent: str, window: str) -> str:
    return f"{agent}/{window}"


def split_ref(ref: str) -> tuple[str, str]:
    agent, sep, window = (ref or "").partition("/")
    if not sep or not agent or not window:
        raise ValueError(f"窗口要写成「agent/窗口名」，例如 claude-code/main，收到的是 {ref!r}")
    return agent, window


def append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False) + "\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)


def load_cache(home: Home) -> dict:
    data = read_json(home.root / "peers.cache.json", {"updated": None, "peers": {}})
    data.setdefault("peers", {})
    return data


def save_cache(home: Home, cache: dict) -> None:
    cache["updated"] = now_iso()
    write_json(home.root / "peers.cache.json", cache)


def runtime_path(home: Home) -> Path:
    return home.root / "runtime.json"


def load_sent(home: Home) -> list[dict]:
    path = home.root / "sent.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def remember_sent(home: Home, row: dict) -> None:
    append_jsonl(home.root / "sent.jsonl", row)
