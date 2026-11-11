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


# 普通写信（自动登记或 xlt trust 时授予，含这些信上的附件）。读文件 / 列目录 / 搜索仍须本机主人另行授权；广播或来信不能给自己加权限。
LETTER_OPS = ("ask", "notify", "reply")
FILE_OPS = ("list", "read", "search")
AUTO_GRANT_PREFIX = "g-trust-"


def auto_grant_id(person: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", str(person or "").strip()) or "member"
    return f"{AUTO_GRANT_PREFIX}{slug}"


def _ops(grant: dict) -> set[str]:
    return {str(item) for item in _as_list(grant.get("operations"))}


def letter_ops_granted(policy: dict, person: str, member: str = "") -> bool:
    have: set[str] = set()
    for grant in policy.get("grants") or []:
        if not grantee_ok(grant, person, member):
            continue
        have |= _ops(grant)
    return set(LETTER_OPS) <= have


def _format_auto_grant(person: str) -> str:
    return (
        f"  - id: {auto_grant_id(person)}\n"
        f"    description: \"登记同事时自动授予的普通写信权限\"\n"
        f"    grantees: [{person}]\n"
        f"    operations: [ask, notify, reply]\n"
    )


def _grants_insert_at(text: str) -> tuple[int, bool]:
    """Return the character index at which to insert a new grant item, and whether grants is currently []."""
    empty = re.search(r"(?m)^grants:\s*\[\s*\]\s*$", text)
    if empty:
        return empty.start(), True
    key = re.search(r"(?m)^grants:\s*$", text)
    if not key:
        return -1, False
    # walk lines after 'grants:' until a new top-level key
    after = text[key.end():]
    offset = key.end()
    last_in_section = offset
    pos = 0
    for line in after.splitlines(keepends=True):
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and not line.startswith((" ", "\t", "\n", "\r")):
            # next top-level key
            return offset + pos, False
        if stripped:
            last_in_section = offset + pos + len(line)
        pos += len(line)
    return last_in_section, False


def _grant_block_span(text: str, grant_id: str) -> tuple[int, int] | None:
    match = re.search(rf"(?m)^(\s*)- id:\s*{re.escape(grant_id)}\s*$", text)
    if not match:
        return None
    start = match.start()
    pos = match.end()
    end = len(text)
    for line in text[match.end():].splitlines(keepends=True):
        if re.match(r"\s*- id:", line) or (
            line.strip() and not line[:1].isspace() and not line.lstrip().startswith("#")
        ):
            end = pos
            break
        pos += len(line)
    return start, end


def _replace_yaml_list(block: str, key: str, values: list[str]) -> str:
    line = f"{key}: [{', '.join(values)}]"
    patched, n = re.subn(rf"(?m)^(\s*){re.escape(key)}:\s*\[[^\]]*\]", rf"\1{line}", block, count=1)
    return patched if n else block


def ensure_letter_grant(path, person: str) -> str:
    """Idempotently grant ask/notify/reply to person. Returns added / already / updated.

    Only inserts or lightly completes an auto-managed grant (id g-trust-<person>).
    Does not rewrite the rest of policy.yaml, so comments and hand-edited grants stay.
    """
    from pathlib import Path
    from xlt.yamlutil import loads

    path = Path(path)
    person = str(person or "").strip()
    if not person:
        return "already"
    if not path.exists():
        from xlt.store import POLICY_TEMPLATE

        path.write_text(POLICY_TEMPLATE.format(person=person), encoding="utf-8")
    text = path.read_text(encoding="utf-8")
    policy = loads(text) or {}
    if letter_ops_granted(policy, person, person):
        return "already"

    auto_id = auto_grant_id(person)
    existing = next((g for g in (policy.get("grants") or []) if g.get("id") == auto_id), None)
    if existing is not None:
        span = _grant_block_span(text, auto_id)
        if span:
            start, end = span
            block = text[start:end]
            ops = [str(item) for item in _as_list(existing.get("operations"))]
            for op in LETTER_OPS:
                if op not in ops:
                    ops.append(op)
            grantees = [str(item) for item in _as_list(existing.get("grantees"))]
            if person not in grantees:
                grantees.append(person)
            block = _replace_yaml_list(block, "operations", ops)
            block = _replace_yaml_list(block, "grantees", grantees)
            path.write_text(text[:start] + block + text[end:], encoding="utf-8")
            return "updated"
    return _append_auto_grant(path, text, person)


def _append_auto_grant(path, text: str, person: str) -> str:
    block = _format_auto_grant(person)
    at, empty = _grants_insert_at(text)
    if empty:
        match = re.search(r"(?m)^grants:\s*\[\s*\]\s*$", text)
        new = text[: match.start()] + "grants:\n" + block + text[match.end():]
    elif at >= 0:
        insert = block if text[:at].endswith("\n") else "\n" + block
        new = text[:at] + insert + text[at:]
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        new = text + "grants:\n" + block
    if text.endswith("\n") and not new.endswith("\n"):
        new += "\n"
    path.write_text(new, encoding="utf-8")
    return "added"


def backfill_trusted_letter_grants(home) -> list[tuple[str, str]]:
    """Ensure every trusted colleague (not myself) has letter grants. Returns (person, status) list."""
    from xlt.store import load_config, load_directory

    cfg = load_config(home)
    me = cfg.get("me")
    results = []
    path = home.root / "policy.yaml"
    seen = set()
    for agent in load_directory(home):
        if agent.get("name") == me:
            continue
        person = str(agent.get("person") or agent.get("name") or "").strip()
        if not person or person in seen:
            continue
        seen.add(person)
        results.append((person, ensure_letter_grant(path, person)))
    return results
