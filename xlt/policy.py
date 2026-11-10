"""第一层权限。程序硬检查，不读信纸，不叫 AI。方案 §5.5。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from xlt.common import now, parse_time

NAME_BLOCK = (
    ".env",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
    "config.json",
)
SUFFIX_BLOCK = (".pem", ".key", ".p12", ".pfx", ".keystore")
PREFIX_BLOCK = ("id_rsa", "id_ed25519", "id_ecdsa")
PART_BLOCK = {".ssh", ".aws", ".gnupg", ".kube", ".xlt", ".docker"}
WORD_BLOCK = ("credentials", "secret", "password", "token")
PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
AWS_KEY_RE = re.compile(r"\bAKIA[0-9A-Z]{16}\b")


@dataclass
class Decision:
    allow: bool
    code: str
    message: str
    grant: str | None = None
    http: int = 200


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def grantee_ok(grant: dict, person: str, member: str) -> bool:
    names = [str(item) for item in _as_list(grant.get("grantees"))]
    if "*team" in names or "*" in names:
        return True
    return person in names or member in names


def _in_window(policy: dict, moment: datetime) -> bool:
    away = policy.get("away") or {}
    start = away.get("from")
    end = away.get("until")
    if not start or not end:
        return False
    try:
        start_d = datetime.fromisoformat(str(start)).date() if "T" not in str(start) else parse_time(str(start)).date()
        end_d = datetime.fromisoformat(str(end)).date() if "T" not in str(end) else parse_time(str(end)).date()
    except ValueError:
        return False
    today = moment.date()
    return start_d <= today <= end_d


def grant_fresh(grant: dict, moment: datetime) -> bool:
    until = grant.get("valid_until")
    if not until:
        return True
    try:
        return moment <= parse_time(str(until))
    except ValueError:
        return False


def blocked_path(path: Path) -> bool:
    name = path.name.lower()
    if name == ".env" or name.startswith(".env."):
        return True
    if name in NAME_BLOCK:
        return True
    if name.endswith(SUFFIX_BLOCK):
        return True
    if name.startswith(PREFIX_BLOCK):
        return True
    if "credentials" in name or "secret" in name or "password" in name or "token" in name:
        return True
    parts = [part.lower() for part in path.parts]
    if any(part in PART_BLOCK for part in parts):
        return True
    if name == "config" and ".git" in parts:
        return True
    if len(parts) >= 2 and parts[-2] == ".git" and parts[-1] == "config":
        return True
    if name == "config.json" and ".docker" in parts:
        return True
    return False


def _root_of(pattern: str) -> str:
    text = pattern.rstrip("/")
    if text.endswith("/**"):
        text = text[:-3]
    elif text.endswith("/*"):
        text = text[:-2]
    return text


def path_under(path: Path, patterns) -> bool:
    for pattern in _as_list(patterns):
        root = Path(_root_of(str(pattern)))
        try:
            path.relative_to(root)
            return True
        except ValueError:
            if path == root:
                return True
    return False


def egress_blocked(text: str) -> str | None:
    if PRIVATE_KEY_RE.search(text or ""):
        return "正文里有私钥块"
    if AWS_KEY_RE.search(text or ""):
        return "正文里有云访问密钥格式"
    if len((text or "").encode("utf-8")) > 64 * 1024:
        return "回信超过 64KB"
    return None


def evaluate(policy: dict, person: str, member: str, action: str, path: str | None = None, moment: datetime | None = None) -> Decision:
    moment = moment or now()
    if str(policy.get("default", "deny")) != "deny":
        return Decision(False, "NO_GRANT", "权限清单的 default 必须是 deny", http=403)
    mode = str(policy.get("mode") or "normal")
    if mode == "paused":
        return Decision(False, "PAUSED", "对方暂停了一切查询", http=403)
    if mode == "away" and not _in_window(policy, moment):
        return Decision(False, "EXPIRED", "不在座位的授权时间已经过了", http=403)

    real = None
    if path:
        real = Path(os.path.realpath(path))
        if blocked_path(real) or blocked_path(Path(path)):
            return Decision(False, "BLOCKED_SENSITIVE", "该路径属于永远不可访问的类型", http=403)

    chosen = None
    for grant in policy.get("grants") or []:
        if not grantee_ok(grant, person, member):
            continue
        if action not in _as_list(grant.get("operations")):
            continue
        if not grant_fresh(grant, moment):
            continue
        chosen = grant
        break
    if chosen is None:
        return Decision(False, "NO_GRANT", "没有被授权做这件事", http=403)

    if path and real is not None:
        allow_paths = chosen.get("allow_paths") or []
        deny_paths = chosen.get("deny_paths") or []
        if allow_paths and not path_under(real, allow_paths):
            return Decision(False, "OUT_OF_SCOPE", "路径不在允许的目录里", http=403, grant=str(chosen.get("id") or ""))
        if deny_paths and path_under(real, deny_paths):
            return Decision(False, "OUT_OF_SCOPE", "路径在排除目录里", http=403, grant=str(chosen.get("id") or ""))
        suffix = real.suffix.lower()
        allow_ext = [str(item).lower() for item in _as_list(chosen.get("allow_file_types"))]
        deny_ext = [str(item).lower() for item in _as_list(chosen.get("deny_file_types"))]
        if allow_ext and suffix not in allow_ext:
            return Decision(False, "OUT_OF_SCOPE", "这种文件类型不在允许范围内", http=403, grant=str(chosen.get("id") or ""))
        if suffix and suffix in deny_ext:
            return Decision(False, "OUT_OF_SCOPE", "这种文件类型被排除了", http=403, grant=str(chosen.get("id") or ""))
        limit = chosen.get("max_file_size_kb")
        if limit and real.is_file():
            if real.stat().st_size > int(limit) * 1024:
                return Decision(False, "TOO_LARGE", "文件超过这条授权允许的大小", http=413, grant=str(chosen.get("id") or ""))
    return Decision(True, "OK", "放行", grant=str(chosen.get("id") or ""), http=200)


def check_file(policy_doc: dict) -> list[str]:
    problems = []
    if str(policy_doc.get("default", "deny")) != "deny":
        problems.append("default 必须是 deny")
    if policy_doc.get("mode") not in ("normal", "away", "paused", None):
        problems.append("mode 只能是 normal、away、paused")
    for grant in policy_doc.get("grants") or []:
        gid = grant.get("id") or "?"
        for pattern in _as_list(grant.get("allow_paths")):
            sample = Path(_root_of(str(pattern))) / "x"
            if blocked_path(sample) or any(part.lower() in PART_BLOCK for part in Path(str(pattern)).parts):
                problems.append(f"授权 {gid} 的目录踩了硬黑名单：{pattern}")
        if not grant.get("grantees"):
            problems.append(f"授权 {gid} 没有写 grantees")
        if not grant.get("operations"):
            problems.append(f"授权 {gid} 没有写 operations")
        if grant.get("valid_until"):
            try:
                parse_time(str(grant["valid_until"]))
            except ValueError:
                problems.append(f"授权 {gid} 的 valid_until 不是时间")
    return problems
