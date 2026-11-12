#!/usr/bin/env python3
"""大附件：状态页有进度条；收信方、发信方各被杀一次后从断点接着传；被改坏的半截文件会被哈希校验发现并重传。"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import PY, Fail, env_with, fresh, pair, say, wait_for  # noqa: E402

WORK = Path(__file__).resolve().parent / "work-resume"
MB = 1024 * 1024


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for piece in iter(lambda: fh.read(MB), b""):
            h.update(piece)
    return h.hexdigest()


def part_size(hao) -> int:
    parts = list((hao.home / "inbox").rglob("*.part"))
    return parts[0].stat().st_size if parts else 0


def final_files(hao) -> list[Path]:
    return [p for p in (hao.home / "inbox").rglob("attachments/*") if not p.name.endswith(".part")]


def send_async(nes, name: str, path: Path):
    return subprocess.Popen(
        [PY, "-m", "xlt", "--home", str(nes.home), "send", "--to", "haonian", "--type", "notify",
         "--subject", name, "--text", name, "--attach", str(path)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env_with(nes.env), text=True,
    )


def main() -> int:
    fresh(WORK)
    nes, hao = pair(WORK, 47300)
    nes.env = {"XLT_ATTACH_CHUNK": str(MB), "XLT_ATTACH_SLEEP": "0.25", "XLT_BACKOFF": "1,2"}
    big1 = WORK / "big1.bin"
    big2 = WORK / "big2.bin"
    big1.write_bytes(os.urandom(20 * MB))
    big2.write_bytes(os.urandom(20 * MB))
    sender = None
    try:
        nes.start()
        hao.start()
        wait_for(lambda: (nes.home / "peers.cache.json").exists(), 15, "没有互相发现")

        # ---- 第一份：进度条；收信方被杀；发信方被杀 ----
        sender = send_async(nes, "大文件一", big1)
        wait_for(lambda: part_size(hao) >= 4 * MB, 30, "半截文件没长起来")
        row = [x for x in nes.api()["pending"] if x.get("attach_bytes")][0]
        pct = row["attach_bytes"]["pct"]
        if not (0 < pct < 100):
            raise Fail(f"发信方待送达里的进度应在 0~100 之间：{row['attach_bytes']}")
        page = nes.page()
        if 'role="progressbar"' not in page or "附件 0/1" not in page:
            raise Fail("发信方状态页没有进度条")
        got = [x for x in hao.api()["inbox"] if x.get("attach_bytes")]
        if not got or not (0 < got[0]["attach_bytes"]["pct"] < 100) or 'role="progressbar"' not in hao.page():
            raise Fail("收信方状态页没有收附件的进度条")
        say(f"传到 {pct}% 时：发信方和收信方的状态页都有进度条（带「附件 0/1」和百分比）")

        hao.crash()
        r1 = part_size(hao)
        if not (0 < r1 < 20 * MB):
            raise Fail(f"收信方被杀时应已收到一部分：{r1}")
        time.sleep(1.5)
        if not nes.pending_files():
            raise Fail("收信方掉线后，发信方的信应还在待送达")
        hao.start()
        wait_for(lambda: part_size(hao) > r1, 30, "收信方回来后没有继续收")
        if part_size(hao) < r1:
            raise Fail("续传没有从断点开始，反而变小了")
        say(f"收信方在 {r1 // MB}MB 处被杀、重启：从断点接着收（{r1 // MB}MB → {part_size(hao) // MB}MB），没有从头来")

        wait_for(lambda: part_size(hao) >= 12 * MB, 30, "没有传到 12MB")
        nes.crash()
        sender.wait(timeout=30)
        r2 = part_size(hao)
        nes.start()
        wait_for(lambda: len(final_files(hao)) == 1, 60, "发信方重启后没有把大文件传完")
        if sha(final_files(hao)[0]) != sha(big1):
            raise Fail("续传完成，但哈希对不上")
        wait_for(lambda: not nes.pending_files(), 15, "传完后待送达应清空")
        ids = [e["id"] for e in hao.inbox_envs()]
        if len(ids) != 1:
            raise Fail(f"收信方应该只有 1 封信，实际 {len(ids)}")
        say(f"发信方在约 {r2 // MB}MB 处被杀、重启：待送达里的信从断点续传完成，哈希一致，收信方只有 1 封")

        # ---- 第二份：半截文件被改坏 → 哈希校验失败 → 自动重传 ----
        sender = send_async(nes, "大文件二", big2)
        wait_for(lambda: part_size(hao) >= 4 * MB and len(hao.inbox_envs()) == 2, 30, "第二份没有开始")
        hao.crash()
        part = list(p for p in (hao.home / "inbox").rglob("*.part"))[0]
        with part.open("r+b") as fh:
            fh.seek(1000)
            old = fh.read(1)
            fh.seek(1000)
            fh.write(bytes([(old[0] + 1) % 256]))
        hao.start()
        wait_for(lambda: len(final_files(hao)) == 2, 90, "第二份没有传完")
        mine = [p for p in final_files(hao) if p.name == big2.name][0]
        if sha(mine) != sha(big2):
            raise Fail("半截文件被改坏后，最终文件和原文件不一致：哈希校验没挡住")
        sender.wait(timeout=30)
        say("半截文件被改坏一个字节：收满后哈希校验不过，自动重传，最终文件和原文件一致")

        print("RESUME E2E 全部通过")
        return 0
    except Fail as exc:
        print("失败", exc)
        for node in (nes, hao):
            print(f"--- {node.me} 日志尾部 ---")
            print(node.log_text()[-2000:])
        return 1
    finally:
        if sender and sender.poll() is None:
            sender.kill()
        for node in (nes, hao):
            node.crash()


if __name__ == "__main__":
    raise SystemExit(main())
