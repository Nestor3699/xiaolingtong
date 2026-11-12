#!/usr/bin/env python3
"""Render the local status page and check labels, warnings, and that letter bodies stay out."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from xlt.panel import label, render  # noqa: E402

CANARY = "LETTER_BODY_SHOULD_NEVER_APPEAR"
SECRET = "xlt_e2e_webhook_key_77"

SNAP = {
    "me": "nes-mac",
    "person": "Nes",
    "owner": "Nes",
    "version": "0.1.0",
    "network": "en0 192.168.0.133",
    "panel_port": 47810,
    "tcp_port": 47800,
    "udp_port": 47801,
    "started": "2026-11-11T13:00:00+08:00",
    "wake_note": "这台电脑睡醒后，通讯员会重新广播自己的地址。",
    "routes_default": "grok-bot/default",
    "on_window_lost": "fallback_default",
    "peers": [
        {
            "name": "runda-mac",
            "person": "润达",
            "owner": "润达",
            "state": "online",
            "ip": "192.168.0.131",
            "last_seen": "2026-11-11T13:40:12+08:00",
            "source": "broadcast",
            "pending": 0,
            "cert_warning": "",
        },
        {
            "name": "haonian-win",
            "person": "浩年",
            "owner": "浩年",
            "state": "offline",
            "ip": "192.168.0.140",
            "last_seen": "2026-11-11T10:02:00+08:00",
            "source": "broadcast",
            "pending": 1,
            "cert_warning": "证书已替换：请当面确认。",
        },
    ],
    "pending": [
        {
            "id": "msg-20261111-134000-nes-mac-0001",
            "to_member": "haonian-win",
            "type": "ask",
            "subject": "今天下午的接口文档",
            "state": {"status": "pending"},
            "attempts": 2,
            "letter_bytes": 128,
            "letter": CANARY,
        }
    ],
    "pending_failed": [],
    "inbox": [
        {
            "id": "msg-20261111-132000-leo-mac-0002",
            "from_person": "Leo",
            "type": "ask",
            "subject": "帮看一下这段报错",
            "state": {"status": "awaiting_manual", "routed_to": "claude/default"},
            "folder": "default",
            "letter_bytes": 256,
            "letter": CANARY,
        }
    ],
    "windows": [
        {
            "ref": "claude/default",
            "delivery": "channel",
            "liveness": "未连接",
            "queued": 1,
            "generation": 2,
            "desc": "本机 Claude",
        }
    ],
    "rejects": [
        {"ts": "2026-11-11T12:10:00+08:00", "caller": "unknown", "action": "files/read", "reason": "NO_GRANT"},
    ],
}


def fail(msg: str) -> None:
    print("FAIL:", msg, file=sys.stderr)
    raise SystemExit(1)


def main() -> int:
    html = render(SNAP)
    required = (
        "小灵通通讯员",
        "只显示信封，不显示正文",
        "同事",
        "待送达",
        "收信窗口",
        "收到的信",
        "被拒记录",
        "待手动处理",
        "在线",
        "离线",
        "证书已替换：请当面确认。",
        "今天下午的接口文档",
        "帮看一下这段报错",
        "fallback_default",
        "127.0.0.1:47810",
        "TCP 47800",
        "UDP 47801",
        "class=\"sidebar\"",
        "睡醒检测说明还没写上。" if False else "这台电脑睡醒后，通讯员会重新广播自己的地址。",
    )
    for item in required:
        if item not in html:
            fail(f"状态页缺少：「{item}」")
    if CANARY in html or SECRET in html:
        fail("状态页泄漏了信纸正文或密钥")
    if "Figma" in html or "figma" in html:
        fail("状态页不应出现外部产品名")
    if "Inter" not in html:
        fail("状态页没有使用 Inter 字体栈")
    import re

    external = re.findall(r"""(?:src|href)=["']https?://|url\(\s*["']?https?://|@import""", html)
    if external:
        fail(f"状态页不应引用外部网址（要能断网使用）：{external}")
    if html.count("<svg") < 8 or 'class="ic"' not in html:
        fail("状态页应该有内联的 Lucide 图标")
    for emoji in ("📬", "✅", "⚠️", "🔔", "❌", "✉️"):
        if emoji in html:
            fail(f"状态页不应使用 emoji：{emoji}")
    for need in ("本机", "机器名", "主人", "地址", "指纹", "网关", "端口", "版本", 'class="kv-row"'):
        if need not in html:
            fail(f"侧栏底部缺少带标签的一行：「{need}」")
    if label("awaiting_manual") != "待手动处理":
        fail("待手动处理标签被改掉了")

    empty = render(
        {
            "me": "solo-mac",
            "person": "Nes",
            "owner": "Nes",
            "panel_port": 1,
            "tcp_port": 2,
            "udp_port": 3,
            "started": None,
            "wake_note": "",
            "routes_default": "（还没设）",
            "on_window_lost": "fallback_default",
            "peers": [],
            "pending": [],
            "inbox": [],
            "windows": [],
            "rejects": [],
        }
    )
    for item in ("成员名单里还没有其他同事", "没有待送达的信", "还没有收到信", "还没有登记收信窗口", "没有被拒记录", "睡醒检测说明还没写上。"):
        if item not in empty:
            fail(f"空状态缺少：「{item}」")
    print("ok: panel render keeps labels, layout, and hides letter bodies")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
