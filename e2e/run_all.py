#!/usr/bin/env python3
"""把所有端到端脚本按顺序跑一遍，写 e2e/full-report.md（每个脚本的结果、耗时、日志尾部）。

用法：.venv/bin/python e2e/run_all.py [名字 ...]
不带名字就全跑。每个脚本各自用不同的端口和目录，互不抢；这里依次跑，不并发。
有一个失败就返回非 0，但会把剩下的跑完。日志存在 e2e/saved-logs/full/。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOGS = HERE / "saved-logs" / "full"

SUITE = [
    ("run_e2e", "发现 → 发信 → 权限 → 回信 → 离线补发 → 去重 → 同名接管"),
    ("attach_e2e", "附件：传送、哈希校验、断线续传"),
    ("auto_trust_e2e", "自动信任与证书"),
    ("trust_grant_e2e", "信任与授权"),
    ("pi_reply_e2e", "pi 回信"),
    ("panel_render_e2e", "状态页文案、图标、侧栏底部，不显示正文"),
    ("panel_live_e2e", "状态页原地更新、不整页刷新"),
    ("panel_stale_e2e", "开着的旧页面遇到升级后的通讯员：样式跟着更新，布局不乱"),
    ("panel_letter_e2e", "点开一封信：看正文、记已读、深链接、刷新不关掉"),
    ("netinfo_check", "网卡与广播地址选择"),
    ("outbox_e2e", "待送达：重启后继续重试，上线即补发"),
    ("receipts_e2e", "已送达 / 已读回执，旧版对方停在已送达"),
    ("doctor_e2e", "xlt doctor 自检"),
    ("cert_banner_e2e", "证书被替换的提示条：自动替换、可收起、重启后仍在"),
    ("resume_e2e", "附件中断后续传，进度条，哈希错后重传"),
    ("update_e2e", "自动更新：自己更新、跳过本地改动、坏版本退回、传文件时推迟"),
]


def main() -> int:
    os.environ["XLT_UPDATE_DISABLE"] = "1"  # 子脚本里的通讯员不去碰真实 GitHub；update_e2e 自己覆盖
    wanted = set(sys.argv[1:])
    suite = [item for item in SUITE if not wanted or item[0] in wanted]
    LOGS.mkdir(parents=True, exist_ok=True)
    rows = []
    for name, what in suite:
        log = LOGS / f"{name}.log"
        start = time.time()
        with log.open("w", encoding="utf-8") as fh:
            code = subprocess.call([sys.executable, str(HERE / f"{name}.py")], stdout=fh, stderr=subprocess.STDOUT, cwd=HERE.parent, timeout=None)
        took = time.time() - start
        text = log.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        tail = text[-1] if text else ""
        rows.append((name, what, code, took, tail))
        print(f"{'通过' if code == 0 else '失败'}  {name:18s} {took:6.1f}s  {tail[:80]}", flush=True)
    failed = [r for r in rows if r[2] != 0]
    lines = [
        "# 端到端全量验收报告",
        "",
        f"时间：{datetime.now().astimezone().isoformat(timespec='seconds')}（本机时间）",
        f"结果：{len(rows) - len(failed)}/{len(rows)} 通过" + ("" if not failed else "，失败：" + "、".join(r[0] for r in failed)),
        "",
        "| 脚本 | 验什么 | 结果 | 耗时 | 最后一行 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for name, what, code, took, tail in rows:
        lines.append(f"| `{name}` | {what} | {'通过' if code == 0 else '**失败**'} | {took:.0f}s | {tail.replace('|', '/')[:90]} |")
    lines += ["", f"各脚本日志：`e2e/saved-logs/full/`。", "这些都是在一台电脑上、用 127.0.0.1 跑的，没有在真机上验证。", ""]
    (HERE / "full-report.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"报告：{HERE / 'full-report.md'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
