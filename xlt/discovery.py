"""UDP 47801：上线连发 3 次、应答、心跳、下线。签名，不传正文。"""

from __future__ import annotations

import json
import random
import socket
import threading
import time
import uuid
from pathlib import Path

from xlt.common import now_iso, parse_time
from xlt.crypto import load_key, sign_obj, verify_obj
from xlt.store import Home, load_cache, load_config, member_by_name, peer_cert, save_cache

ONLINE_LIMIT = 10
SKEW_SECONDS = 120


def broadcast_addresses(ip: str) -> list[str]:
    found = ["255.255.255.255"]
    parts = (ip or "").split(".")
    if len(parts) == 4 and not ip.startswith("127."):
        found.append(".".join(parts[:3] + ["255"]))
    return found


class Discovery(threading.Thread):
    def __init__(self, home: Home, log, on_peer):
        super().__init__(name="xlt-discovery", daemon=True)
        self.home = home
        self.log = log
        self.on_peer = on_peer
        self.cfg = load_config(home)
        self.stop = threading.Event()
        self.sock: socket.socket | None = None
        self.boot_id = uuid.uuid4().hex[:8]
        self.seq = 0
        self.seq_lock = threading.Lock()
        self.seen_seq: dict[str, tuple[str, int]] = {}
        self.online_times: dict[str, list[float]] = {}
        self.last_reply: dict[str, float] = {}
        self.ready = threading.Event()

    def run(self):
        cfg = self.cfg
        bind = cfg.get("bind_ip") or ""
        port = int(cfg["udp_port"])
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            sock.bind((bind, port))
        except OSError as exc:
            self.log(f"UDP {bind or '0.0.0.0'}:{port} 绑定失败：{exc}")
            sock.close()
            self.ready.set()
            return
        sock.settimeout(0.5)
        self.sock = sock
        self.ready.set()
        self.log(f"发现端口已监听 UDP {bind or '0.0.0.0'}:{port}")
        self.announce("online", times=3)
        interval = max(1, int(cfg.get("heartbeat_s") or 180))
        jitter = 20 if interval >= 60 else max(0, interval // 10)
        next_beat = time.time() + interval + random.uniform(-jitter, jitter)
        while not self.stop.is_set():
            try:
                raw, src = sock.recvfrom(4096)
            except socket.timeout:
                raw = None
                src = None
            except OSError:
                break
            if raw:
                self._handle(raw, src)
            if time.time() >= next_beat:
                self.announce("heartbeat", times=1)
                next_beat = time.time() + interval + random.uniform(-jitter, jitter)
        try:
            sock.close()
        except OSError:
            pass

    def announce(self, kind: str, times: int = 1, reason: str | None = None):
        threading.Thread(target=self._send_many, args=(kind, times, reason), name="xlt-announce", daemon=True).start()

    def _send_many(self, kind: str, times: int, reason: str | None):
        for i in range(times):
            if self.stop.is_set() and kind != "offline":
                return
            packet = self._build(kind, reason)
            if packet is None or self.sock is None:
                return
            for dest in self._destinations():
                try:
                    self.sock.sendto(packet, dest)
                except OSError as exc:
                    self.log(f"广播发往 {dest[0]}:{dest[1]} 失败：{exc}")
            self.log(f"发出 {kind} 第 {i + 1}/{times} 次")
            if i + 1 < times:
                time.sleep(random.uniform(0.3, 1.2))

    def _destinations(self) -> list[tuple[str, int]]:
        cfg = self.cfg
        port = int(cfg["udp_port"])
        ip = cfg.get("announce_ip") or cfg.get("bind_ip") or "127.0.0.1"
        dests = [(host, port) for host in broadcast_addresses(ip)]
        for item in cfg.get("announce_targets") or []:
            host, sep, raw_port = str(item).rpartition(":")
            if sep and host:
                dests.append((host, int(raw_port)))
        unique = []
        seen = set()
        for item in dests:
            if item not in seen:
                seen.add(item)
                unique.append(item)
        return unique

    def _build(self, kind: str, reason: str | None) -> bytes | None:
        cfg = self.cfg
        me = cfg["me"]
        key_path = self.home.tls / f"{me}.key"
        crt = peer_cert(self.home, me)
        if crt is None or not key_path.exists():
            self.log("本机证书不齐，发不出发现包")
            return None
        with self.seq_lock:
            self.seq += 1
            seq = self.seq
        msg = {
            "xlt": 1,
            "type": kind,
            "name": me,
            "owner": cfg.get("owner") or cfg.get("person") or me,
            "host": socket.gethostname(),
            "ip": cfg.get("announce_ip") or cfg.get("bind_ip") or "",
            "port": int(cfg["tcp_port"]),
            "subnet": _subnet(cfg.get("announce_ip") or cfg.get("bind_ip") or ""),
            "cert_sha256": member_by_name(self.home, me).get("cert_sha256") if member_by_name(self.home, me) else "",
            "agents": _agent_hint(self.home),
            "version": "0.1.0",
            "boot_id": self.boot_id,
            "seq": seq,
            "nonce": uuid.uuid4().hex[:8],
            "ts": now_iso(),
        }
        if reason and kind == "offline":
            msg["reason"] = reason
        key = load_key(key_path)
        msg["sig"] = sign_obj(key, msg)
        data = json.dumps(msg, ensure_ascii=False).encode("utf-8")
        if len(data) > 2048:
            self.log("发现包超过 2KB，已丢弃")
            return None
        return data

    def _handle(self, raw: bytes, src):
        msg, why = self._check(raw)
        if msg is None:
            if why and why != "自己":
                self.log(f"忽略 {src[0]}：{why}")
            return
        if msg["name"] == self.cfg["me"]:
            return
        self._remember(msg, src)
        self.log(f"收到 {msg['type']} ← {msg['name']} @ {src[0]}:{msg.get('port')}")
        if msg["type"] == "online":
            now_s = time.time()
            if now_s - self.last_reply.get(msg["name"], 0) > 5:
                self.last_reply[msg["name"]] = now_s
                threading.Thread(target=self._reply_later, args=(src,), daemon=True).start()
        if msg["type"] != "offline":
            try:
                self.on_peer(msg["name"])
            except Exception as exc:
                self.log(f"上线后补发出错：{exc}")

    def _reply_later(self, src):
        time.sleep(random.uniform(0, 0.8))
        packet = self._build("reply", None)
        if packet and self.sock is not None:
            try:
                self.sock.sendto(packet, src)
                self.log(f"已应答 {src[0]}:{src[1]}")
            except OSError as exc:
                self.log(f"应答失败：{exc}")

    def _check(self, raw: bytes):
        if len(raw) > 2048:
            return None, "超过 2KB"
        try:
            msg = json.loads(raw.decode("utf-8"))
        except Exception:
            return None, "不是 JSON"
        if msg.get("xlt") != 1 or msg.get("type") not in ("online", "reply", "offline", "heartbeat"):
            return None, "格式不符"
        name = msg.get("name")
        if name == self.cfg.get("me"):
            return None, "自己"
        member = member_by_name(self.home, name)
        if not member:
            return None, f"陌生成员 {name}"
        if str(msg.get("cert_sha256", "")).upper() != str(member.get("cert_sha256", "")).upper():
            return None, "证书指纹与名单不一致"
        cert = peer_cert(self.home, name)
        if cert is None or not verify_obj(cert, msg):
            return None, "签名无效"
        try:
            skew = abs((parse_time(msg["ts"]) - parse_time(now_iso())).total_seconds())
        except Exception:
            return None, "时间格式不对"
        if skew > SKEW_SECONDS:
            return None, f"时间差 {int(skew)} 秒，请检查两边校时"
        previous = self.seen_seq.get(name)
        if previous and previous[0] == msg.get("boot_id") and int(msg.get("seq") or 0) <= previous[1]:
            return None, "重放或旧包"
        if msg.get("type") == "online":
            stamp = time.time()
            recent = [item for item in self.online_times.get(name, []) if stamp - item < 60]
            if len(recent) >= ONLINE_LIMIT:
                return None, "上线广播太密"
            recent.append(stamp)
            self.online_times[name] = recent
        self.seen_seq[name] = (msg.get("boot_id"), int(msg.get("seq") or 0))
        return msg, None

    def _remember(self, msg: dict, src):
        cache = load_cache(self.home)
        state = "offline" if msg.get("type") == "offline" else "online"
        row = {
            "ip": src[0],
            "port": int(msg.get("port") or 47800),
            "udp_port": src[1],
            "state": state,
            "last_seen": now_iso() if state == "online" else cache.get("peers", {}).get(msg["name"], {}).get("last_seen"),
            "boot_id": msg.get("boot_id"),
            "seq": msg.get("seq"),
            "agents": msg.get("agents") or [],
            "version": msg.get("version"),
            "source": "broadcast",
            "owner": msg.get("owner"),
        }
        if msg.get("type") == "offline":
            row["last_offline_reason"] = msg.get("reason") or "quit"
            row["last_seen"] = now_iso()
        cache["peers"][msg["name"]] = row
        save_cache(self.home, cache)


def _subnet(ip: str) -> str:
    parts = (ip or "").split(".")
    if len(parts) == 4 and not ip.startswith("127."):
        return ".".join(parts[:3]) + ".0/24"
    if ip.startswith("127."):
        return "127.0.0.0/8"
    return ""


def _agent_hint(home: Home) -> list[dict]:
    from xlt.store import load_windows

    hints = []
    for window in load_windows(home):
        hints.append({"kind": window.get("agent"), "mode": window.get("delivery"), "ready": True})
    return hints[:8]


def maybe_mdns(home: Home, log) -> None:
    cfg = load_config(home)
    if not cfg.get("mdns"):
        return
    try:
        from zeroconf import ServiceInfo, Zeroconf
    except Exception:
        log("配置开了 mDNS，但这台没装 zeroconf，跳过。安装：pip install 'xiaolingtong[mdns]'")
        return
    ip = cfg.get("announce_ip") or cfg.get("bind_ip")
    if not ip or ip.startswith("127."):
        log("mDNS 需要办公室网卡的 IP，127.0.0.1 上不注册")
        return
    info = ServiceInfo(
        "_xlt-agent._tcp.local.",
        f"xlt-{cfg['me']}._xlt-agent._tcp.local.",
        addresses=[socket.inet_aton(ip)],
        port=int(cfg["tcp_port"]),
        properties={"agent": cfg["me"], "ver": "1"},
    )
    zc = Zeroconf()
    zc.register_service(info)
    log(f"mDNS 已注册 {cfg['me']} @ {ip}:{cfg['tcp_port']}")
