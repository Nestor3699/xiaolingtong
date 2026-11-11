#!/usr/bin/env python3
"""Headless check: the status page updates in place and does not reload."""

from __future__ import annotations

import base64
import copy
import json
import secrets
import shutil
import socket
import struct
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from e2e.panel_render_e2e import SNAP  # noqa: E402
from xlt.panel import render  # noqa: E402

STATE = {"snap": copy.deepcopy(SNAP)}
NEW_SUBJECT = "LIVE_E2E_UPDATED_SUBJECT"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        path = urlparse(self.path).path
        if path not in ("/", "/index.html"):
            data = b'{"error":"not found"}'
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)
            return
        body = render(STATE["snap"]).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)


class SimpleWS:
    def __init__(self, url, timeout=30):
        u = urlparse(url)
        host, port = u.hostname, u.port or 80
        path = u.path + (("?" + u.query) if u.query else "")
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("ws handshake closed")
            buf += chunk
        head, rest = buf.split(b"\r\n\r\n", 1)
        if b"101" not in head.split(b"\r\n", 1)[0]:
            raise RuntimeError("ws handshake failed")
        self._buf = rest

    def send(self, text: str) -> None:
        data = text.encode()
        mask = secrets.token_bytes(4)
        n = len(data)
        if n < 126:
            hdr = bytes([0x81, 0x80 | n])
        elif n < 65536:
            hdr = bytes([0x81, 0x80 | 126]) + struct.pack("!H", n)
        else:
            hdr = bytes([0x81, 0x80 | 127]) + struct.pack("!Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        self.sock.sendall(hdr + mask + masked)

    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(max(n - len(self._buf), 4096))
            if not chunk:
                raise RuntimeError("ws closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv(self):
        while True:
            hdr = self._recv_exact(2)
            opcode = hdr[0] & 0x0F
            masked = hdr[1] & 0x80
            n = hdr[1] & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._recv_exact(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if masked else b""
            data = self._recv_exact(n)
            if masked:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 0x8:
                raise RuntimeError("ws close")
            if opcode == 0x9:
                continue
            if opcode in (0x1, 0x2, 0x0):
                return data.decode() if opcode != 0x2 else data

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def fail(msg: str) -> None:
    print("FAIL:", msg, file=sys.stderr)
    raise SystemExit(1)


def eval_js(cdp, expression: str, await_promise: bool = False):
    result = cdp(
        "Runtime.evaluate",
        {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": await_promise,
        },
    )
    if result.get("exceptionDetails"):
        fail(f"js error: {result['exceptionDetails']}")
    return result.get("result", {}).get("value")


def main() -> int:
    import subprocess

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    page_url = f"http://127.0.0.1:{port}/"

    ud = Path("/tmp/xlt-panel-live-e2e")
    if ud.exists():
        shutil.rmtree(ud)
    ud.mkdir()
    dbg = 9224
    chrome = subprocess.Popen(
        [
            "/opt/google/chrome/chrome",
            "--headless=new",
            f"--remote-debugging-port={dbg}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={ud}",
            "--password-store=basic",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--window-size=1440,900",
            "--noerrdialogs",
            "--ozone-platform=headless",
            "--ozone-override-screen-size=1440,900",
            "--use-angle=swiftshader-webgl",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{dbg}/json/version", timeout=0.3)
                break
            except Exception:
                time.sleep(0.1)
        else:
            fail("headless Chrome did not start")

        req = urllib.request.Request(f"http://127.0.0.1:{dbg}/json/new?{page_url}", method="PUT")
        with urllib.request.urlopen(req, timeout=5) as resp:
            target = json.load(resp)

        ws = SimpleWS(target["webSocketDebuggerUrl"], timeout=40)
        box = {"id": 0}

        def cdp(method, params=None, timeout=30):
            box["id"] += 1
            mid = box["id"]
            ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
            deadline = time.time() + timeout
            while time.time() < deadline:
                raw = ws.recv()
                if isinstance(raw, bytes):
                    continue
                data = json.loads(raw)
                if data.get("id") == mid:
                    if "error" in data:
                        fail(f"cdp {method}: {data['error']}")
                    return data.get("result", {})
            fail(f"cdp timeout: {method}")

        cdp("Page.enable")
        cdp("Runtime.enable")
        for _ in range(40):
            if eval_js(cdp, "document.readyState") == "complete":
                break
            time.sleep(0.1)

        if eval_js(cdp, "document.querySelector('meta[http-equiv=\"refresh\"]') !== null"):
            fail("meta refresh is still in the page")
        if not eval_js(cdp, "typeof window.__xltLiveMarker === 'number'"):
            fail("live marker was not set")

        eval_js(cdp, "location.hash = '#inbox'; true")
        eval_js(
            cdp,
            """
            (function () {
              var row = document.querySelector('tbody tr[data-row="peer:runda-mac"]');
              if (row) row.focus();
              window.scrollTo(0, 120);
              window.__xltProbe = 'survive';
              return true;
            })()
            """,
        )
        marker = eval_js(cdp, "window.__xltLiveMarker")
        nav0 = eval_js(cdp, "performance.getEntriesByType('navigation').length")
        if nav0 != 1:
            fail(f"expected 1 navigation entry at start, got {nav0}")

        time.sleep(12)

        marker2 = eval_js(cdp, "window.__xltLiveMarker")
        probe = eval_js(cdp, "window.__xltProbe")
        nav1 = eval_js(cdp, "performance.getEntriesByType('navigation').length")
        hash1 = eval_js(cdp, "location.hash")
        focused = eval_js(cdp, "document.activeElement && document.activeElement.getAttribute('data-row')")
        stamp = eval_js(cdp, "document.getElementById('live-stamp') && document.getElementById('live-stamp').textContent")
        body = eval_js(cdp, "document.body.innerText")

        if marker2 != marker:
            fail(f"live marker changed ({marker} -> {marker2}); page navigated or reloaded")
        if probe != "survive":
            fail("probe variable did not survive; page reloaded")
        if nav1 != 1:
            fail(f"navigation count became {nav1}; page navigated")
        if hash1 != "#inbox":
            fail(f"hash was lost: {hash1}")
        if focused != "peer:runda-mac":
            fail(f"focused row was lost: {focused}")
        if not stamp or ("秒前更新" not in stamp and stamp != "刚刚更新"):
            fail(f"missing live stamp text: {stamp!r}")
        if "今天下午的接口文档" not in body:
            fail("original subject missing after 12s")
        if NEW_SUBJECT in body:
            fail("new subject appeared before the snapshot changed")

        # Selecting text should skip the in-place swap.
        eval_js(
            cdp,
            """
            (function () {
              var el = document.querySelector('#pending tbody td');
              var range = document.createRange();
              range.selectNodeContents(el);
              var sel = window.getSelection();
              sel.removeAllRanges();
              sel.addRange(range);
              return String(sel);
            })()
            """,
        )
        STATE["snap"] = copy.deepcopy(SNAP)
        STATE["snap"]["pending"][0]["subject"] = NEW_SUBJECT
        time.sleep(5.8)
        body_sel = eval_js(cdp, "document.body.innerText")
        if NEW_SUBJECT in body_sel:
            fail("page updated while text was selected")
        if "今天下午的接口文档" not in body_sel:
            fail("original subject disappeared while selection should have blocked the update")

        eval_js(cdp, "window.getSelection().removeAllRanges(); true")
        time.sleep(5.8)
        body2 = eval_js(cdp, "document.body.innerText")
        marker3 = eval_js(cdp, "window.__xltLiveMarker")
        probe3 = eval_js(cdp, "window.__xltProbe")
        nav2 = eval_js(cdp, "performance.getEntriesByType('navigation').length")
        hash2 = eval_js(cdp, "location.hash")
        focused2 = eval_js(cdp, "document.activeElement && document.activeElement.getAttribute('data-row')")
        stamp2 = eval_js(cdp, "document.getElementById('live-stamp') && document.getElementById('live-stamp').textContent")

        if NEW_SUBJECT not in body2:
            fail("content did not update after the snapshot changed")
        if "今天下午的接口文档" in body2:
            fail("old subject remained after the update")
        if marker3 != marker or probe3 != "survive" or nav2 != 1:
            fail("page navigated when content updated")
        if hash2 != "#inbox":
            fail(f"hash was lost after content update: {hash2}")
        if focused2 != "peer:runda-mac":
            fail(f"focused row was lost after content update: {focused2}")
        if not stamp2 or ("秒前更新" not in stamp2 and stamp2 != "刚刚更新"):
            fail(f"stamp missing after update: {stamp2!r}")

        ws.close()
        print("ok: panel live-updates in place, no full reload, selection is preserved")
        return 0
    finally:
        chrome.terminate()
        try:
            chrome.wait(timeout=5)
        except Exception:
            chrome.kill()
        httpd.shutdown()
        try:
            shutil.rmtree(ud)
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
