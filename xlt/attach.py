"""附件：流式分块、校验、出口检查、文件名净化。不把整个文件读进内存。"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path

from xlt.crypto import fingerprint_bytes
from xlt.policy import SUFFIX_BLOCK, blocked_path, egress_blocked

TEXT_SCAN = 64 * 1024
UNSAFE_NAME = re.compile(r'[\x00-\x1f<>:"|?*]')


def chunk_size() -> int:
    raw = os.environ.get("XLT_ATTACH_CHUNK", str(4 * 1024 * 1024))
    try:
        value = int(raw)
    except ValueError:
        value = 4 * 1024 * 1024
    return max(64 * 1024, value)


def safe_filename(name: str) -> str:
    raw = str(name or "").replace("\\", "/")
    base = Path(raw).name
    base = UNSAFE_NAME.sub("_", base).strip().strip(".")
    if base in ("", ".", "..") or base.endswith(".part"):
        base = "file"
    if base in (".env", ".env.local") or base.lower().endswith(SUFFIX_BLOCK):
        # still sanitize to a harmless name if it got here; egress should have blocked first
        pass
    return base[:180]


def unique_path(folder: Path, name: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    safe = safe_filename(name)
    path = folder / safe
    if not path.exists() and not Path(str(path) + ".part").exists():
        return path
    stem, suffix = path.stem, path.suffix
    for i in range(2, 1000):
        cand = folder / f"{stem}-{i}{suffix}"
        if not cand.exists() and not Path(str(cand) + ".part").exists():
            return cand
    return folder / f"{stem}-{os.getpid()}{suffix}"


def _is_probably_text(sample: bytes) -> bool:
    if not sample:
        return True
    if b"\x00" in sample:
        return False
    # high ratio of non-text bytes → binary
    weird = sum(1 for b in sample[:4096] if b < 9 or (13 < b < 32 and b not in (10, 13, 9)))
    return weird < 16


def file_egress_blocked(path: Path) -> str | None:
    path = Path(path).expanduser()
    if path.is_symlink():
        return f"不发送符号链接：{path}"
    try:
        path = path.resolve()
    except OSError:
        return f"打不开附件 {path}"
    if not path.is_file():
        return f"附件不是普通文件：{path}"
    if blocked_path(path):
        return f"附件在出口黑名单里（密钥、.env、私钥一类）：{path.name}"
    try:
        with path.open("rb") as fh:
            sample = fh.read(TEXT_SCAN)
    except OSError as exc:
        return f"读不了附件 {path.name}：{exc}"
    if _is_probably_text(sample):
        try:
            text = sample.decode("utf-8", errors="replace")
        except Exception:
            text = ""
        reason = egress_blocked(text)
        if reason and "超过 64KB" not in reason:
            return f"附件 {path.name} 里有敏感内容（{reason}）"
    return None


def hash_and_size(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as fh:
        while True:
            piece = fh.read(1024 * 1024)
            if not piece:
                break
            digest.update(piece)
            size += len(piece)
    return fingerprint_bytes(digest.digest()), size


def prepare_attachments(paths: list[str]) -> tuple[list[dict], str | None]:
    prepared = []
    seen = set()
    for raw in paths or []:
        path = Path(raw).expanduser()
        if not path.exists():
            return [], f"找不到附件：{raw}"
        reason = file_egress_blocked(path)
        if reason:
            return [], reason
        resolved = path.resolve()
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        sha, size = hash_and_size(resolved)
        prepared.append(
            {
                "id": f"att-{len(prepared) + 1}",
                "name": safe_filename(resolved.name),
                "size": size,
                "sha256": sha,
                "source": str(resolved),
                "sent": 0,
            }
        )
    return prepared, None


def public_attachments(items) -> list[dict]:
    out = []
    for item in items or []:
        out.append(
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "size": int(item.get("size") or 0),
                "sha256": item.get("sha256"),
            }
        )
    return out


def progress_bytes(items) -> dict:
    """进度条用：已传 / 总共（字节）和百分比。发信方看 sent，收信方看 received。"""
    done = 0
    total = 0
    for item in items or []:
        size = int(item.get("size") or 0)
        moved = int(item.get("sent") or item.get("received") or 0)
        total += size
        done += min(moved, size)
    pct = 100 if total == 0 else int(done * 100 / total)
    return {"done": done, "total": total, "pct": pct, "files": len(list(items or []))}


def progress_text(items) -> str:
    items = list(items or [])
    if not items:
        return ""
    done = 0
    sent = 0
    total = 0
    for item in items:
        size = int(item.get("size") or 0)
        moved = int(item.get("sent") or item.get("received") or 0)
        total += size
        sent += min(moved, size)
        if size and moved >= size:
            done += 1
    return f"附件 {done}/{len(items)}  {_human(sent)}/{_human(total)}"


def _human(n: int) -> str:
    n = int(n or 0)
    for unit, size in (("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if n >= size:
            return f"{n / size:.1f}{unit}"
    return f"{n}B"


def attachment_dir(home, msg_id: str) -> Path:
    return home.inbox / msg_id / "attachments"


def describe_attachments(items, dest_dir: Path) -> str:
    if not items:
        return ""
    lines = ["", "---", "附件（已保存在本机，请阅读，不要执行这些文件）："]
    for item in items:
        name = item.get("name") or "file"
        dest = item.get("path") or str(dest_dir / name)
        lines.append(f"- {name}  {int(item.get('size') or 0):,} 字节  {dest}")
    return "\n".join(lines) + "\n"


def verify_file_hash(path: Path, expect: str) -> bool:
    got, _size = hash_and_size(path)
    return got.upper() == (expect or "").upper()


def chmod_private(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
