#!/usr/bin/env python3
"""用夹具数据把状态页截成图（独立的无头 Chrome）。不是验收，是给人看的。

输出到 /workspace/xlt-ui/（可用 XLT_UI_OUT 改）：v2-overview.png、v2-sidebar.png、v2-banner.png、
v2-delivery.png（待送达/已发出/附件进度）、v2-narrow.png。
"""

from __future__ import annotations

import copy
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from browser import Browser  # noqa: E402
from panel_render_e2e import SNAP  # noqa: E402

from xlt.panel import render  # noqa: E402

OUT = Path(os.environ.get("XLT_UI_OUT", "/workspace/xlt-ui"))

FP = "05:1F:C8:58:A2:D0:95:74:34:01:C4:3E:57:96:F3:3E:EA:F4:79:91:38:96:BE:14:33:D2:56:2A:CE:11:F2:F7"
FP_NEW = "C6:31:13:80:A1:7B:3D:FE:F3:2B:1D:ED:12:81:BF:73:78:43:99:18:04:97:01:F0:4F:70:E8:7E:48:BD:F3:21"


def fixture() -> dict:
    snap = copy.deepcopy(SNAP)
    for item in snap["pending"]:
        item.pop("letter", None)
    for item in snap["inbox"]:
        item.pop("letter", None)
    snap.update(
        owner="Nes", person="nes", me="nes-mac", ip="192.168.0.133",
        fingerprint=FP, gateway_running=True, started="2026-11-12T09:41:00+08:00",
        wake_note="睡醒：优先听 macOS 的 NSWorkspace 睡醒通知；没装 pyobjc 时，用时钟跳变和 IP 变化，然后重新广播上线。",
    )
    snap["peers"][1]["cert_warning"] = ""
    snap["pending"] = [
        {"id": "msg-20261112-094300-nes-mac-0007", "to_member": "haonian-win", "type": "notify", "subject": "周五评审改到三点",
         "state": {"status": "pending"}, "attempts": 3, "last_error": "connect failed", "next_try_at": 1794452400, "letter_bytes": 60},
        {"id": "msg-20261112-094500-nes-mac-0008", "to_member": "runda-mac", "type": "notify", "subject": "录屏文件",
         "state": {"status": "pending"}, "attempts": 1, "last_error": "附件中断 BrokenPipeError", "next_try_at": 1794452460,
         "letter_bytes": 40, "attach_progress": "附件 0/1  412.0MB/1.1GB",
         "attach_bytes": {"done": 412 << 20, "total": 1126 << 20, "pct": 36, "files": 1}},
    ]
    snap["sent"] = [
        {"id": "msg-20261112-094500-nes-mac-0008", "to_member": "runda-mac", "type": "notify", "subject": "录屏文件",
         "status": "pending", "detail": "附件 0/1  412.0MB/1.1GB", "created": "2026-11-12T09:45:00+08:00",
         "attach_bytes": {"done": 412 << 20, "total": 1126 << 20, "pct": 36, "files": 1}},
        {"id": "msg-20261112-094300-nes-mac-0007", "to_member": "haonian-win", "type": "notify", "subject": "周五评审改到三点",
         "status": "pending", "detail": "", "created": "2026-11-12T09:43:00+08:00"},
        {"id": "msg-20261112-093000-nes-mac-0006", "to_member": "runda-mac", "type": "ask", "subject": "退款流程",
         "status": "receipt_read", "detail": "已读 11-12 09:31", "created": "2026-11-12T09:30:00+08:00"},
        {"id": "msg-20261112-092000-nes-mac-0005", "to_member": "runda-mac", "type": "notify", "subject": "截图",
         "status": "receipt_stored", "detail": "送达 11-12 09:20", "created": "2026-11-12T09:20:00+08:00"},
        {"id": "msg-20261112-091000-nes-mac-0004", "to_member": "haonian-win", "type": "ask", "subject": "接口文档",
         "status": "receipt_stored", "detail": "送达 11-12 09:10（对方版本较旧，没有已读回执）", "created": "2026-11-12T09:10:00+08:00"},
    ]
    snap["inbox"][0]["read"] = False
    snap["inbox"].append({
        "id": "msg-20261112-090000-runda-mac-0003", "from_person": "润达", "type": "reply", "subject": "回复 退款流程",
        "state": {"status": "replied", "routed_to": "claude/default"}, "folder": "default", "letter_bytes": 310,
        "read": True, "read_at": "2026-11-12T09:02:00+08:00",
    })
    snap["cert_alerts"] = [{
        "id": "cert-demo", "name": "haonian-win", "owner": "浩年", "ts": "2026-11-12T10:58:00+08:00", "ip": "192.168.0.140",
        "old_fp": FP, "new_fp": FP_NEW, "old_short": "05:1F:C8:58 … CE:11:F2:F7", "new_short": "C6:31:13:80 … 48:BD:F3:21",
    }]
    return snap


STATE = {"snap": fixture()}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        return

    def do_GET(self):
        body = render(STATE["snap"]).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    browser = Browser(9241, 1440, 900)
    try:
        browser.open(url, 1440, 900)
        browser.screenshot(OUT / "v2-overview.png")
        browser.screenshot(OUT / "v2-sidebar.png", clip={"x": 0, "y": 0, "width": 260, "height": 900})
        browser.js("document.querySelector('details.foot-note').open = true; window.scrollTo(0, 0); true")
        browser.screenshot(OUT / "v2-sidebar-open.png", clip={"x": 0, "y": 0, "width": 260, "height": 900})
        browser.js("document.querySelector('details.foot-note').open = false; true")
        browser.screenshot(OUT / "v2-banner.png", clip={"x": 240, "y": 0, "width": 1200, "height": 330})
        browser.js("location.hash = '#pending'; true")
        browser.js("document.querySelector('#pending').scrollIntoView(); true")
        browser.screenshot(OUT / "v2-delivery.png")
        browser.cdp("Emulation.setDeviceMetricsOverride", {"width": 780, "height": 1000, "deviceScaleFactor": 1, "mobile": False})
        browser.screenshot(OUT / "v2-narrow.png")
        print("截图已写入", OUT)
        return 0
    finally:
        browser.close()
        httpd.shutdown()


if __name__ == "__main__":
    raise SystemExit(main())
