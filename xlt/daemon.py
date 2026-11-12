"""常驻进程：发现、双向 TLS、本机状态页。"""

from __future__ import annotations

import json
import logging
import socket
import ssl
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from xlt import updater
from xlt.common import now_iso, read_json, write_json
from xlt.discovery import Discovery, maybe_mdns
from xlt.engine import Engine, _err
from xlt.mtls import context, peer_fingerprint, trust_pems
from xlt.panel import collect, render
from xlt.power import start as start_power
from xlt.netinfo import choose_lan, detect_lan_ip, format_doctor, is_loopback_ip, is_vpn_ip, unicast_targets
from xlt.store import Home, load_config, load_directory, member_by_fp, save_config, update_config


def detect_ip() -> str:
    """探测办公室网卡地址。Clash TUN 下不能再用 connect(8.8.8.8) 那一招。"""
    return detect_lan_ip()


def make_logger(home: Home):
    logger = logging.getLogger(f"xlt.{home.root}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(message)s")
    file_handler = logging.FileHandler(home.root / "messenger.log", encoding="utf-8")
    file_handler.setFormatter(fmt)
    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    logger.addHandler(file_handler)
    logger.addHandler(stream)
    return logger.info


class _Reuse(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class PeerServer(_Reuse):
    def __init__(self, addr, engine: Engine, ctx: ssl.SSLContext):
        self.engine = engine
        self.ctx = ctx
        super().__init__(addr, PeerHandler)

    def reload_trust(self) -> None:
        home = self.engine.home
        cfg = self.engine.cfg
        self.ctx = context(self.engine.cert, home.tls / f"{cfg['me']}.key", server=True, trust=trust_pems(home))

    def get_request(self):
        sock, addr = super().get_request()
        try:
            secured = self.ctx.wrap_socket(sock, server_side=True)
            secured.settimeout(180)
            return secured, addr
        except ssl.SSLError:
            try:
                sock.close()
            except OSError:
                pass
            raise OSError("tls")

    def handle_error(self, request, client_address):
        self.engine.log(f"连接中断 {client_address[0]}")


class PeerHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def _send(self, code: int, obj) -> None:
        data = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def _req(self) -> str:
        return self.headers.get("X-小灵通-Request-Id") or uuid.uuid4().hex

    def _member(self):
        try:
            fp = peer_fingerprint(self.connection)
        except Exception:
            return None
        return member_by_fp(self.server.engine.home, fp)

    def _loopback_self(self, member) -> bool:
        host = self.client_address[0]
        return host in ("127.0.0.1", "::1") and member and member.get("name") == self.server.engine.cfg.get("me")

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 90000:
            self._send(413, _err("TOO_LARGE", "请求太大", self._req()))
            return None
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            self._send(400, _err("BAD_ENVELOPE", "需要 JSON", self._req()))
            return None

    def _attach_args(self):
        parsed = urlparse(self.path)
        q = parse_qs(parsed.query or "")
        msg_id = self.headers.get("X-Xlt-Msg-Id") or (q.get("msg_id") or [""])[0]
        att_id = self.headers.get("X-Xlt-Att-Id") or (q.get("att_id") or [""])[0]
        offset = self.headers.get("X-Xlt-Offset") or (q.get("offset") or ["0"])[0]
        length = self.headers.get("X-Xlt-Length") or (q.get("length") or [self.headers.get("Content-Length") or "0"])[0]
        try:
            offset_i = int(offset or 0)
            length_i = int(length or 0)
        except ValueError:
            offset_i, length_i = -1, -1
        return msg_id, att_id, offset_i, length_i

    def do_GET(self):
        member = self._member()
        if member is None:
            self._send(401, _err("NOT_A_MEMBER", "证书不在成员名单里", self._req()))
            return
        path = urlparse(self.path).path
        engine = self.server.engine
        if path == "/xlt/v1/attachments":
            msg_id, att_id, _off, _ln = self._attach_args()
            code, payload = engine.on_attach_status(member, msg_id, att_id, self._req())
            self._send(code, payload)
            return
        if path == "/xlt/v1/receipt":
            msg_id = (parse_qs(urlparse(self.path).query).get("id") or [""])[0]
            code, payload = engine.on_receipt(member, msg_id, self._req())
            self._send(code, payload)
            return
        if path == "/xlt/v1/health":
            self._send(200, engine.health_body())
            return
        if path == "/xlt/v1/status":
            self._send(200, engine.status_body())
            return
        if path == "/xlt/v1/permissions":
            self._send(200, engine.permissions_body(member))
            return
        if path in ("/xlt/v1/outbox", "/xlt/v1/inbox", "/xlt/v1/rejects", "/xlt/v1/audit"):
            if not self._loopback_self(member):
                self._send(403, _err("NO_GRANT", "只允许本机用自己的证书查看", self._req()))
                return
            snap = collect(engine.home)
            if path.endswith("outbox"):
                self._send(200, {"pending": snap["pending"]})
            elif path.endswith("inbox"):
                self._send(200, {"inbox": snap["inbox"], "windows": snap["windows"]})
            elif path.endswith("rejects"):
                self._send(200, {"rejects": snap["rejects"]})
            else:
                self._send(200, {"audit": _tail(engine.home.root / "audit.jsonl")})
            return
        self._send(404, _err("NOT_FOUND", "没有这个地址", self._req()))

    def do_POST(self):
        member = self._member()
        req_id = self._req()
        if member is None:
            self._send(401, _err("NOT_A_MEMBER", "证书不在成员名单里", req_id))
            return
        path = urlparse(self.path).path
        engine = self.server.engine
        if path == "/xlt/v1/attachments":
            msg_id, att_id, offset, length = self._attach_args()
            if offset < 0 or length < 0:
                self._send(400, _err("BAD_ENVELOPE", "附件偏移或长度无效", req_id))
                return
            try:
                code, payload = engine.on_attach_chunk(member, msg_id, att_id, offset, length, self.rfile, req_id)
            except Exception:
                engine.log("附件写入出错\n" + traceback.format_exc())
                self._send(500, _err("BUSY", "通讯员内部出错", req_id))
                return
            self._send(code, payload)
            return
        data = self._read_json()
        if data is None:
            return
        try:
            if path == "/xlt/v1/messages":
                code, payload = engine.on_message(member, data.get("envelope") or {}, data.get("letter") or "", req_id)
                self._send(code, payload)
                return
            if path == "/xlt/v1/files/read":
                code, payload = engine.on_read(member, str(data.get("path") or ""), req_id)
                self._send(code, payload)
                return
            if path == "/xlt/v1/files/list":
                self._send(404, _err("NOT_FOUND", "这一版还没有列目录", req_id))
                return
            if path == "/xlt/v1/files/search":
                self._send(404, _err("NOT_FOUND", "这一版还没有搜索", req_id))
                return
            self._send(404, _err("NOT_FOUND", "没有这个地址", req_id))
        except Exception:
            engine.log("处理请求出错\n" + traceback.format_exc())
            self._send(500, _err("BUSY", "通讯员内部出错", req_id))


class LocalServer(_Reuse):
    def __init__(self, addr, engine: Engine):
        self.engine = engine
        super().__init__(addr, LocalHandler)


class LocalHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        return

    def _remote(self) -> bool:
        if self.client_address[0] not in ("127.0.0.1", "::1"):
            self._send(403, {"error": {"code": "NO_GRANT", "message": "状态页只给本机"}})
            return True
        return False

    def _send(self, code: int, obj, content_type: str = "application/json; charset=utf-8") -> None:
        if isinstance(obj, bytes):
            data = obj
        elif isinstance(obj, str):
            data = obj.encode("utf-8")
        else:
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 90000:
            self._send(413, {"status": "rejected", "reason": "TOO_LARGE", "message": "信超过限制"})
            return None
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            self._send(400, {"status": "rejected", "reason": "BAD_ENVELOPE", "message": "需要 JSON"})
            return None

    def _panel_guard(self) -> bool:
        """只接受状态页自己发的修改请求。别的网页也能向 127.0.0.1 发 POST，
        所以要求自定义头（浏览器对跨站请求会先预检并拒绝），再核对 Host，挡住 DNS 重绑定。"""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        if self.headers.get("X-Xlt-Panel") != "1" or host not in ("127.0.0.1", "localhost", "::1"):
            self._send(403, {"status": "rejected", "reason": "NO_GRANT", "message": "只接受状态页自己发出的请求"})
            return False
        return True

    def do_GET(self):
        if self._remote():
            return
        path = urlparse(self.path).path
        engine = self.server.engine
        if path in ("/", "/index.html"):
            page = render(collect(engine.home))
            self._send(200, page, "text/html; charset=utf-8")
            return
        if path in ("/api/panel", "/local/status"):
            self._send(200, collect(engine.home))
            return
        if path == "/local/panel.css":
            from xlt.panel import CSS

            self._send(200, CSS, "text/css; charset=utf-8")
            return
        if path == "/local/busy":
            self._send(200, engine.busy_info())
            return
        if path == "/local/letter":
            if not self._panel_guard():
                return
            from xlt.panel import letter_view

            q = parse_qs(urlparse(self.path).query)
            box = (q.get("box") or [""])[0]
            msg_id = (q.get("id") or [""])[0]
            view, code = letter_view(engine.home, box, msg_id)
            self._send(code, view)
            return
        self._send(404, {"error": {"code": "NOT_FOUND", "message": "没有这个地址"}})

    def do_POST(self):
        if self._remote():
            return
        data = self._body()
        if data is None:
            return
        path = urlparse(self.path).path
        engine = self.server.engine
        try:
            if path == "/local/send":
                result = engine.local_send(
                    data.get("to") or "",
                    data.get("type") or "ask",
                    data.get("subject") or "",
                    data.get("letter") or "",
                    data.get("tags") or [],
                    data.get("origin_window") or "",
                    data.get("in_reply_to"),
                    data.get("to_member"),
                    data.get("attachments") or [],
                )
                self._send(200, result)
                return
            if path == "/local/reply":
                self._send(200, engine.local_reply(data.get("msg_id") or "", data.get("text") or "", data.get("status") or "replied", attachments=data.get("attachments") or []))
                return
            if path == "/local/cert-dismiss":
                if not self._panel_guard():
                    return
                from xlt import certevents

                count = certevents.dismiss(engine.home, str(data.get("id") or ""))
                self._send(200, {"status": "ok", "dismissed": count})
                return
            if path == "/local/update-dismiss":
                if not self._panel_guard():
                    return
                from xlt import updater

                self._send(200, {"status": "ok", "dismissed": updater.dismiss_notice(engine.home, str(data.get("id") or ""))})
                return
            if path == "/local/shutdown":
                # 自动更新要重启通讯员：先请它自己体面地退出（会发下线广播），不行再强杀。
                if not self._panel_guard():
                    return
                engine.log("收到本机的退出请求（更新或手动重启）")
                self._send(200, {"status": "ok"})
                stop_event = getattr(engine, "stop_event", None)
                if stop_event is not None:
                    stop_event.set()
                return
            if path == "/local/done":
                self._send(200, engine.local_done(data.get("msg_id") or ""))
                return
            self._send(404, {"status": "rejected", "reason": "NOT_FOUND", "message": "没有这个地址"})
        except Exception:
            engine.log("本机接口出错\n" + traceback.format_exc())
            self._send(500, {"status": "rejected", "reason": "INTERNAL", "message": "通讯员内部出错"})


def _tail(path, limit: int = 20) -> list:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _pid_alive(pid) -> bool:
    # Windows 上 os.kill(pid, 0) 会给对方发 Ctrl+C，所以统一走 doctor 里按平台区分的版本。
    from xlt.doctor import pid_alive

    return pid_alive(pid)


def serve(home: Home, args) -> int:
    home.ensure()
    cfg = load_config(home)
    if not cfg.get("me") or not (home.tls / f"{cfg['me']}.key").exists():
        print("还没有本机证书。先运行：xlt init --me 你的机器名 --person 你的人名", flush=True)
        return 1
    explicit_bind = bool(getattr(args, "bind", None))
    explicit_ip = bool(getattr(args, "ip", None))
    if getattr(args, "no_ip_auto", False):
        cfg["ip_auto"] = False
    choice = choose_lan()
    lan_ip = choice.ip
    corrected = ""
    if explicit_bind:
        cfg["bind_ip"] = args.bind
    elif cfg.get("ip_auto", True) and (not cfg.get("bind_ip") or is_vpn_ip(cfg.get("bind_ip") or "")):
        if is_vpn_ip(cfg.get("bind_ip") or ""):
            corrected = cfg["bind_ip"]
        cfg["bind_ip"] = lan_ip
    if explicit_ip:
        cfg["announce_ip"] = args.ip
    elif cfg.get("ip_auto", True) and (not cfg.get("announce_ip") or is_vpn_ip(cfg.get("announce_ip") or "")):
        if is_vpn_ip(cfg.get("announce_ip") or "") and not corrected:
            corrected = cfg.get("announce_ip") or ""
        cfg["announce_ip"] = cfg.get("bind_ip") or lan_ip
    if not cfg.get("bind_ip"):
        cfg["bind_ip"] = cfg.get("announce_ip") or lan_ip
    if not cfg.get("announce_ip"):
        cfg["announce_ip"] = cfg.get("bind_ip") or lan_ip
    if explicit_bind:
        listen_ip = args.bind
    elif is_loopback_ip(cfg["bind_ip"]):
        listen_ip = cfg["bind_ip"]
    else:
        listen_ip = "0.0.0.0"
    if getattr(args, "tcp_port", None):
        cfg["tcp_port"] = args.tcp_port
    if getattr(args, "udp_port", None):
        cfg["udp_port"] = args.udp_port
    if getattr(args, "panel_port", None):
        cfg["panel_port"] = args.panel_port
    if getattr(args, "heartbeat", None):
        cfg["heartbeat_s"] = args.heartbeat
    if getattr(args, "announce", None):
        cfg["announce_targets"] = list(args.announce)
    save_config(home, cfg)

    runtime = read_json(home.root / "runtime.json", {})
    if _pid_alive(runtime.get("pid")) and runtime.get("pid") != __import__("os").getpid():
        print(f"通讯员已经在运行（进程 {runtime.get('pid')}）。状态页 http://127.0.0.1:{runtime.get('panel_port')}", flush=True)
        return 1

    log = make_logger(home)
    if corrected:
        log(f"配置里的地址 {corrected} 像是 VPN，已改用办公室网卡 {lan_ip}")
    log(format_doctor(choice, cfg, {
        "listen_ip": listen_ip,
        "unicast": unicast_targets(cfg, load_directory(home)),
    }))
    engine = Engine(home, log)
    stop = threading.Event()
    engine.stop_event = stop
    engine.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="xlt")
    discovery_holder: dict = {}
    note = start_power(
        lambda: discovery_holder["disc"].announce("online", 3) if discovery_holder.get("disc") else None,
        lambda: discovery_holder["disc"].send_now("offline", "sleep") if discovery_holder.get("disc") else None,
    )
    (home.root / "wake-note.txt").write_text(note + "\n", encoding="utf-8")
    log(note)

    ctx = context(engine.cert, home.tls / f"{cfg['me']}.key", server=True, trust=trust_pems(home))
    try:
        peer = PeerServer((listen_ip, int(cfg["tcp_port"])), engine, ctx)
    except OSError as exc:
        print(f"TCP {listen_ip}:{cfg['tcp_port']} 绑定失败：{exc}", flush=True)
        return 1
    try:
        local = LocalServer(("127.0.0.1", int(cfg["panel_port"])), engine)
    except OSError as exc:
        peer.server_close()
        print(f"状态页端口 127.0.0.1:{cfg['panel_port']} 绑定失败：{exc}", flush=True)
        return 1

    def _on_peer(name):
        engine.executor.submit(engine.flush_member, name)

    disc = Discovery(home, log, _on_peer, on_trust=peer.reload_trust)
    discovery_holder["disc"] = disc

    def send_offline(reason: str):
        packet_reason = reason
        try:
            disc.send_now("offline", packet_reason)
        except Exception as exc:
            log(f"下线广播没发出去：{exc}")

    disc.send_offline = send_offline  # type: ignore[attr-defined]
    # Discovery.announce 是异步的；退出时用同步的一发。
    def _send_now(kind: str, reason: str | None = None):
        packet = disc._build(kind, reason)
        if packet:
            disc.send_packet(packet)
            log(f"发出 {kind}")

    disc.send_now = _send_now  # type: ignore[attr-defined]

    import os
    import signal

    write_json(
        home.root / "runtime.json",
        {
            "pid": os.getpid(),
            "panel_port": int(cfg["panel_port"]),
            "tcp_port": int(cfg["tcp_port"]),
            "udp_port": int(cfg["udp_port"]),
            "bind": cfg["bind_ip"],
            "announce_ip": cfg.get("announce_ip") or cfg["bind_ip"],
            "listen_ip": listen_ip,
            "iface": choice.iface.name if choice.iface else "",
            "broadcasts": list(choice.broadcasts),
            "started": now_iso(),
            "commit": updater.running_commit(),
            "cmd": updater.restart_command(home, sys.argv[1:]),
        },
    )
    threading.Thread(target=peer.serve_forever, kwargs={"poll_interval": 0.3}, name="xlt-tls", daemon=True).start()
    threading.Thread(target=local.serve_forever, kwargs={"poll_interval": 0.3}, name="xlt-panel", daemon=True).start()
    disc.start()
    if not disc.ready.wait(5):
        log("发现端口没有在 5 秒内就绪")
    maybe_mdns(home, log)
    engine.drain_spool()
    log(f"状态页 http://127.0.0.1:{cfg['panel_port']}")
    print(f"通讯员已启动。状态页 http://127.0.0.1:{cfg['panel_port']}", flush=True)

    def _stop(signum, _frame):
        log(f"收到退出信号 {signum}")
        stop.set()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    scheduler = updater.Scheduler(home, log)
    last_house = 0.0
    last_clock = time.time()
    announced_ip = cfg.get("announce_ip")
    try:
        while not stop.is_set():
            engine.drain_outbox()
            now_s = time.time()
            try:
                scheduler.tick(now_s)
            except Exception as exc:
                log(f"更新调度出错（已忽略）：{exc.__class__.__name__}: {exc}")
            if now_s - last_house >= 2:
                engine.housekeeping()
                last_house = now_s
            if now_s - last_clock >= 15:
                gap = now_s - last_clock
                if gap > 45:
                    log(f"时钟跳变 {int(gap)} 秒，当作刚睡醒，重新广播上线")
                    disc.announce("online", 3)
                if cfg.get("ip_auto", True) and not explicit_bind and not is_loopback_ip(cfg.get("bind_ip") or ""):
                    fresh = choose_lan()
                    ip = fresh.ip
                    if ip and ip != "127.0.0.1" and not is_vpn_ip(ip) and announced_ip and ip != announced_ip:
                        log(f"IP 变化 {announced_ip} → {ip}，重新广播上线")
                        announced_ip = ip
                        cfg["announce_ip"] = ip
                        if not explicit_bind:
                            cfg["bind_ip"] = ip
                        disc.cfg["announce_ip"] = ip
                        disc.cfg["bind_ip"] = cfg["bind_ip"]
                        disc.net = fresh
                        update_config(home, bind_ip=cfg["bind_ip"], announce_ip=cfg["announce_ip"])
                        disc.announce("online", 3)
                last_clock = time.time()
            stop.wait(0.3)
    finally:
        try:
            disc.send_now("offline", "quit")
        except Exception:
            pass
        disc.stop.set()
        peer.shutdown()
        local.shutdown()
        engine.executor.shutdown(wait=False, cancel_futures=True)
        for proc in engine.procs.values():
            proc.terminate()
        runtime = read_json(home.root / "runtime.json", {})
        if runtime.get("pid") == os.getpid():
            write_json(home.root / "runtime.json", {})
        log("通讯员已退出")
    return 0
