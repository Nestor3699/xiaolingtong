"""独立的无头 Chrome（不是盒子桌面浏览器）：开页面、执行 JS、截图。只用标准库，通过 CDP 说话。"""

from __future__ import annotations

import base64
import json
import secrets
import shutil
import socket
import struct
import subprocess
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

CHROME = "/opt/google/chrome/chrome"


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


class Browser:
    def __init__(self, debug_port: int, width: int = 1440, height: int = 900):
        self.debug_port = debug_port
        self.ud = Path(f"/tmp/xlt-e2e-chrome-{debug_port}")
        if self.ud.exists():
            shutil.rmtree(self.ud)
        self.ud.mkdir(parents=True)
        self.proc = subprocess.Popen(
            [
                CHROME, "--headless=new", f"--remote-debugging-port={debug_port}",
                "--remote-debugging-address=127.0.0.1", f"--user-data-dir={self.ud}",
                "--password-store=basic", "--no-first-run", "--no-default-browser-check",
                "--disable-gpu", "--disable-dev-shm-usage", "--no-sandbox",
                f"--window-size={width},{height}", "--noerrdialogs", "--ozone-platform=headless",
                f"--ozone-override-screen-size={width},{height}", "--use-angle=swiftshader-webgl",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.ws = None
        self._id = 0
        for _ in range(80):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{debug_port}/json/version", timeout=0.3)
                break
            except Exception:
                time.sleep(0.1)
        else:
            raise RuntimeError("headless Chrome 没起来")

    def open(self, url: str, width: int | None = None, height: int | None = None) -> None:
        req = urllib.request.Request(f"http://127.0.0.1:{self.debug_port}/json/new?{url}", method="PUT")
        with urllib.request.urlopen(req, timeout=5) as resp:
            target = json.load(resp)
        self.ws = SimpleWS(target["webSocketDebuggerUrl"], timeout=40)
        self.cdp("Page.enable")
        self.cdp("Runtime.enable")
        if width and height:
            self.cdp("Emulation.setDeviceMetricsOverride", {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False})
        for _ in range(60):
            if self.js("document.readyState") == "complete":
                return
            time.sleep(0.1)

    def cdp(self, method: str, params: dict | None = None, timeout: float = 30):
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        end = time.time() + timeout
        while time.time() < end:
            raw = self.ws.recv()
            if isinstance(raw, bytes):
                continue
            data = json.loads(raw)
            if data.get("id") == mid:
                if "error" in data:
                    raise RuntimeError(f"cdp {method}: {data['error']}")
                return data.get("result", {})
        raise RuntimeError(f"cdp 超时：{method}")

    def js(self, expression: str, await_promise: bool = False):
        res = self.cdp("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": await_promise})
        if res.get("exceptionDetails"):
            raise RuntimeError(f"js 报错：{res['exceptionDetails']}")
        return res.get("result", {}).get("value")

    def screenshot(self, path: Path, clip: dict | None = None, full: bool = False) -> None:
        params: dict = {"format": "png"}
        if clip:
            params["clip"] = dict(clip, scale=1)
        if full:
            params["captureBeyondViewport"] = True
        data = self.cdp("Page.captureScreenshot", params)["data"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(data))

    def close(self) -> None:
        if self.ws:
            self.ws.close()
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
