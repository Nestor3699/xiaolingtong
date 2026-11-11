"""办公室网卡探测：跳过 Clash/Tailscale 等虚拟网卡，按真实掩码算广播地址。

不要用「connect 到 8.8.8.8 / 192.168.1.1 再读 sockname」——Clash TUN 下那会返回 198.18.x。
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import struct
import subprocess
from dataclasses import dataclass, field


CLASH_FAKEIP = ipaddress.ip_network("198.18.0.0/15")
TAILSCALE_CGNAT = ipaddress.ip_network("100.64.0.0/10")
LINK_LOCAL = ipaddress.ip_network("169.254.0.0/16")
LOOPBACK = ipaddress.ip_network("127.0.0.0/8")

VPN_NAME_RE = re.compile(
    r"^(utun|tun|wg|tap|ppp|ipsec|zt)\d*$|"
    r"tailscale|wintun|wireguard|clash|meta|surge|tun2socks|"
    r"zerotier|anyconnect|cisco|openvpn|tap-windows",
    re.I,
)
LAN_NAME_RE = re.compile(
    r"^(en|eth|wl|wlan)\d+$|"
    r"wi-?fi|ethernet|wlan|以太网|本地连接|无线",
    re.I,
)


@dataclass
class Iface:
    name: str
    ip: str
    prefix: int
    broadcast: str = ""
    flags: str = ""
    vpn: bool = False
    vpn_reason: str = ""
    loopback: bool = False
    link_local: bool = False
    up: bool = True


@dataclass
class NetChoice:
    ip: str
    iface: Iface | None
    broadcasts: list[str]
    listen_ip: str
    warnings: list[str] = field(default_factory=list)
    vpn_ifaces: list[Iface] = field(default_factory=list)
    all_ifaces: list[Iface] = field(default_factory=list)
    default_route_if: str = ""
    bound_broadcast: bool = True


def ip_to_int(ip: str) -> int:
    return int(ipaddress.IPv4Address(ip))


def int_to_ip(n: int) -> str:
    return str(ipaddress.IPv4Address(n & 0xFFFFFFFF))


def netmask_to_prefix(mask: str) -> int:
    mask = (mask or "").strip()
    if mask.startswith("0x"):
        return int(mask, 16).bit_count()
    if re.match(r"^\d+\.\d+\.\d+\.\d+$", mask):
        return ip_to_int(mask).bit_count()
    if mask.startswith("/"):
        return int(mask[1:])
    return int(mask)


def prefix_to_netmask(prefix: int) -> int:
    prefix = max(0, min(32, int(prefix)))
    return (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF if prefix else 0


def directed_broadcast(ip: str, prefix: int) -> str:
    addr = ip_to_int(ip)
    mask = prefix_to_netmask(prefix)
    return int_to_ip(addr | (~mask & 0xFFFFFFFF))


def vpn_reason_for(name: str, ip: str) -> str:
    try:
        addr = ipaddress.IPv4Address(ip)
    except Exception:
        return ""
    if addr in LOOPBACK:
        return ""
    if addr in CLASH_FAKEIP:
        return "Clash/Surge Fake-IP（198.18.0.0/15）"
    if addr in TAILSCALE_CGNAT:
        return "Tailscale CGNAT（100.64.0.0/10）"
    if VPN_NAME_RE.search(name or ""):
        return f"虚拟网卡名称 {name}"
    return ""


def classify(iface: Iface) -> Iface:
    try:
        addr = ipaddress.IPv4Address(iface.ip)
    except Exception:
        iface.vpn = True
        iface.vpn_reason = "无效地址"
        return iface
    iface.loopback = addr in LOOPBACK
    iface.link_local = addr in LINK_LOCAL
    reason = vpn_reason_for(iface.name, iface.ip)
    if reason:
        iface.vpn = True
        iface.vpn_reason = reason
    if not iface.broadcast and not iface.loopback and iface.prefix < 32:
        iface.broadcast = directed_broadcast(iface.ip, iface.prefix)
    return iface


def parse_ifconfig(text: str) -> list[Iface]:
    ifaces: list[Iface] = []
    current = ""
    flags = ""
    up = False
    for raw in (text or "").splitlines():
        head = re.match(r"^([A-Za-z0-9._-]+):\s+flags=(\S*)(?:<([^>]*)>)?", raw)
        if head:
            current, flag_bits, flag_names = head.group(1), head.group(2) or "", head.group(3) or ""
            flags = flag_names or flag_bits
            up = "UP" in flag_names.split(",") or bool(re.search(r"<[^>]*UP", raw))
            continue
        inet = re.search(
            r"\binet\s+(\d+\.\d+\.\d+\.\d+)"
            r"(?:\s+-->\s+\S+)?"
            r"\s+netmask\s+(0x[0-9a-fA-F]+|\d+\.\d+\.\d+\.\d+)"
            r"(?:\s+broadcast\s+(\d+\.\d+\.\d+\.\d+))?",
            raw,
        )
        if not inet or not current:
            continue
        prefix = netmask_to_prefix(inet.group(2))
        iface = Iface(
            name=current,
            ip=inet.group(1),
            prefix=prefix,
            broadcast=inet.group(3) or "",
            flags=flags,
            up=up,
        )
        ifaces.append(classify(iface))
    return ifaces


def parse_ip_addr(text: str) -> list[Iface]:
    """`ip -4 addr` 或 `ip -4 -o addr`。"""
    ifaces: list[Iface] = []
    current = ""
    up = True
    for raw in (text or "").splitlines():
        head = re.match(r"^\d+:\s*([A-Za-z0-9._-]+):", raw)
        if head and "inet " not in raw:
            current = head.group(1)
            up = "UP" in raw or "state UP" in raw or "LOWER_UP" in raw
            continue
        inet = re.search(
            r"(?:^|\s)(?:\d+:\s*)?([A-Za-z0-9._-]+)?.*?\binet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)(?:\s+brd\s+(\d+\.\d+\.\d+\.\d+))?",
            raw,
        )
        if not inet:
            continue
        name = (inet.group(1) or current or "").strip()
        if not name:
            continue
        ifaces.append(
            classify(
                Iface(
                    name=name,
                    ip=inet.group(2),
                    prefix=int(inet.group(3)),
                    broadcast=inet.group(4) or "",
                    up=up,
                )
            )
        )
    return ifaces


def parse_ipconfig(text: str) -> list[Iface]:
    ifaces: list[Iface] = []
    current = ""
    ip = ""
    mask = ""
    for raw in (text or "").splitlines():
        adapter = re.match(r"^(?:Ethernet adapter|Wireless LAN adapter|PPP adapter|Unknown adapter|未知适配器|以太网适配器|无线局域网适配器)\s+(.+?):\s*$", raw.strip())
        if not adapter:
            # "Wi-Fi:" / "以太网:" style, or blank line resets after a pair
            generic = re.match(r"^(.+):\s*$", raw)
            if generic and not raw.strip().startswith(("IPv4", "IP Address", "Subnet", "子网", "媒体", "Media", "Connection", "连接")):
                if ip and current:
                    ifaces.append(classify(Iface(name=current, ip=ip, prefix=netmask_to_prefix(mask or "255.255.255.0"))))
                current = generic.group(1).strip()
                ip, mask = "", ""
                continue
        else:
            if ip and current:
                ifaces.append(classify(Iface(name=current, ip=ip, prefix=netmask_to_prefix(mask or "255.255.255.0"))))
            current = adapter.group(1).strip()
            ip, mask = "", ""
            continue
        m_ip = re.search(r"(?:IPv4(?: 地址| Address)|IP(?:v4)? Address|IPv4 地址)[^:]*: *(\d+\.\d+\.\d+\.\d+)", raw, re.I)
        if m_ip:
            ip = m_ip.group(1)
        m_mask = re.search(r"(?:Subnet Mask|子网掩码)[^:]*: *(\d+\.\d+\.\d+\.\d+)", raw, re.I)
        if m_mask:
            mask = m_mask.group(1)
    if ip and current:
        ifaces.append(classify(Iface(name=current, ip=ip, prefix=netmask_to_prefix(mask or "255.255.255.0"))))
    return ifaces


def parse_net_ip_address_json(text: str) -> list[Iface]:
    try:
        data = json.loads(text or "[]")
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]
    ifaces = []
    for row in data or []:
        ip = str(row.get("IPAddress") or row.get("ip") or "")
        name = str(row.get("InterfaceAlias") or row.get("name") or "")
        prefix = int(row.get("PrefixLength") or row.get("prefix") or 0)
        if not ip or not name or prefix <= 0:
            continue
        ifaces.append(classify(Iface(name=name, ip=ip, prefix=prefix)))
    return ifaces


def _run(cmd: list[str], timeout: float = 3) -> str:
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout, text=True)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout or ""


def list_ifaces() -> list[Iface]:
    if os.name == "nt":
        ps = _run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | "
                "Select-Object IPAddress,PrefixLength,InterfaceAlias | ConvertTo-Json -Compress",
            ],
            timeout=5,
        )
        found = parse_net_ip_address_json(ps)
        if found:
            return found
        return parse_ipconfig(_run(["ipconfig"]))
    text = _run(["ifconfig", "-a"]) or _run(["/sbin/ifconfig", "-a"])
    found = parse_ifconfig(text) if text else []
    if not found:
        found = parse_ip_addr(_run(["ip", "-4", "addr"]) or _run(["ip", "-4", "-o", "addr"]))
    return found


def default_route_iface() -> str:
    if os.name == "nt":
        ps = _run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-NetRoute -AddressFamily IPv4 -DestinationPrefix '0.0.0.0/0' "
                "-ErrorAction SilentlyContinue | Sort-Object RouteMetric | "
                "Select-Object -First 1 -ExpandProperty InterfaceAlias",
            ],
            timeout=5,
        )
        return (ps or "").strip().splitlines()[-1].strip() if ps.strip() else ""
    route = _run(["route", "-n", "get", "default"]) or _run(["/sbin/route", "-n", "get", "default"])
    match = re.search(r"interface:\s+(\S+)", route or "", re.I)
    if match:
        return match.group(1)
    # Linux: /proc/net/route, destination 00000000
    try:
        lines = open("/proc/net/route", encoding="ascii").read().splitlines()[1:]
    except OSError:
        lines = []
    best = ""
    best_metric = 10**9
    for line in lines:
        parts = line.split()
        if len(parts) >= 8 and parts[1] == "00000000":
            metric = int(parts[6], 10)
            if metric < best_metric:
                best_metric = metric
                best = parts[0]
    return best


def usable_lan(iface: Iface) -> bool:
    return bool(
        iface.up
        and iface.ip
        and not iface.loopback
        and not iface.link_local
        and not iface.vpn
        and iface.prefix < 32
    )


def choose_lan(ifaces: list[Iface] | None = None, default_if: str | None = None) -> NetChoice:
    ifaces = list(ifaces if ifaces is not None else list_ifaces())
    if default_if is None:
        default_if = default_route_iface()
    vpn_ifaces = [item for item in ifaces if item.vpn and not item.loopback]
    candidates = [item for item in ifaces if usable_lan(item)]
    warnings: list[str] = []
    chosen: Iface | None = None

    def score(item: Iface) -> tuple:
        name = item.name or ""
        default_ok = bool(default_if and name == default_if and not vpn_reason_for(name, item.ip))
        lan_name = bool(LAN_NAME_RE.search(name))
        try:
            addr = ipaddress.IPv4Address(item.ip)
            in_192 = addr in ipaddress.ip_network("192.168.0.0/16")
            in_10 = addr in ipaddress.ip_network("10.0.0.0/8")
            in_172 = addr in ipaddress.ip_network("172.16.0.0/12")
        except Exception:
            in_192 = in_10 = in_172 = False
        # Prefer physical default route, then en0/Wi-Fi, then RFC1918
        return (
            0 if default_ok else 1,
            0 if lan_name else 1,
            0 if in_192 else 1 if in_10 else 2 if in_172 else 3,
            item.prefix,  # narrower later
            name,
        )

    if default_if and vpn_reason_for(default_if, next((i.ip for i in ifaces if i.name == default_if), "0.0.0.0")):
        warnings.append(f"系统默认路由走的是虚拟网卡 {default_if}，已改选办公室物理网卡。")
    if candidates:
        chosen = sorted(candidates, key=score)[0]
        ip = chosen.ip
    else:
        ip = "127.0.0.1"
        warnings.append("没有找到可用的办公室网卡（已跳过 198.18/15、100.64/10、utun/tun/Wintun 和链路本地）。将使用 127.0.0.1，同事会发现不了你。")

    broadcasts = broadcast_addresses(ip, chosen, ifaces)
    if chosen and is_vpn_ip(ip):
        warnings.append(f"选中的地址 {ip} 看起来仍是 VPN，请检查网卡列表。")
    return NetChoice(
        ip=ip,
        iface=chosen,
        broadcasts=broadcasts,
        listen_ip="0.0.0.0" if chosen and not ip.startswith("127.") else ip,
        warnings=warnings,
        vpn_ifaces=vpn_ifaces,
        all_ifaces=ifaces,
        default_route_if=default_if or "",
        bound_broadcast=bool(chosen and not ip.startswith("127.")),
    )


def is_vpn_ip(ip: str) -> bool:
    try:
        addr = ipaddress.IPv4Address(ip)
    except Exception:
        return False
    return addr in CLASH_FAKEIP or addr in TAILSCALE_CGNAT


def is_loopback_ip(ip: str) -> bool:
    try:
        return ipaddress.IPv4Address(ip) in LOOPBACK
    except Exception:
        return (ip or "").startswith("127.")


def broadcast_addresses(ip: str, iface: Iface | None = None, ifaces: list[Iface] | None = None) -> list[str]:
    """真实定向广播 + 255.255.255.255。不再假设 /24。"""
    found: list[str] = []
    match = iface
    if match is None and ifaces:
        match = next((item for item in ifaces if item.ip == ip), None)
    if match and match.broadcast:
        found.append(match.broadcast)
    elif ip and not is_loopback_ip(ip):
        # 未知掩码时不要猜 x.y.z.255（/23 时那一地址可能没有路由）
        pass
    found.append("255.255.255.255")
    unique = []
    for item in found:
        if item and item not in unique:
            unique.append(item)
    return unique


def detect_lan_ip() -> str:
    return choose_lan().ip


def bind_udp_to_iface(sock: socket.socket, ip: str, ifname: str = "") -> None:
    """让 255.255.255.255 从办公室网卡出去，而不是走 Tailscale/Clash 的默认路由。"""
    if not ip or is_loopback_ip(ip):
        return
    if ifname:
        try:
            index = socket.if_nametoindex(ifname)
        except OSError:
            index = 0
        if index:
            packed = struct.pack("I", index)
            for level, opt in (
                (getattr(socket, "IPPROTO_IP", 0), getattr(socket, "IP_BOUND_IF", 25)),
                (getattr(socket, "SOL_SOCKET", 1), getattr(socket, "SO_BINDTODEVICE", 25)),
            ):
                try:
                    if opt == getattr(socket, "SO_BINDTODEVICE", 25) and level == socket.SOL_SOCKET:
                        sock.setsockopt(level, opt, ifname.encode("ascii") + b"\0")
                    else:
                        sock.setsockopt(socket.IPPROTO_IP, opt, packed if os.name != "nt" else index)
                    break
                except (OSError, OverflowError, TypeError):
                    continue


def format_doctor(choice: NetChoice, cfg: dict | None = None, extras: dict | None = None) -> str:
    cfg = cfg or {}
    extras = extras or {}
    lines = ["网络诊断"]
    if choice.iface:
        lines.append(
            f"选中的办公室地址：{choice.ip}  网卡 {choice.iface.name}  "
            f"前缀 /{choice.iface.prefix}  定向广播 {choice.iface.broadcast or '—'}"
        )
    else:
        lines.append(f"选中的地址：{choice.ip}")
    bind_cfg = cfg.get("bind_ip") or extras.get("bind") or ""
    announce = cfg.get("announce_ip") or bind_cfg
    listen = extras.get("listen_ip") or choice.listen_ip
    lines.append(f"配置 bind_ip={bind_cfg or '（空）'}  announce_ip={announce or '（空）'}  监听 {listen}")
    lines.append("广播将发往：" + "、".join(choice.broadcasts) + ("（从办公室网卡发出 255.255.255.255）" if choice.bound_broadcast else ""))
    targets = extras.get("unicast") or []
    if targets:
        lines.append("单播备用：" + "、".join(targets))
    if choice.default_route_if:
        lines.append(f"系统默认路由网卡：{choice.default_route_if}")
    if choice.vpn_ifaces:
        lines.append("检测到的虚拟/VPN 网卡：")
        for item in choice.vpn_ifaces:
            lines.append(f"  {item.name}  {item.ip}  {item.vpn_reason}")
    else:
        lines.append("未检测到 Clash/Tailscale 一类的虚拟网卡。")
    check_ip = announce or bind_cfg or choice.ip
    if is_vpn_ip(check_ip):
        lines.append(
            f"警告：当前 bind/announce 地址 {check_ip} 像是 VPN 地址。"
            "发现包会发错网卡，同事会一直显示离线。"
            "不要用 --bind 指定这个地址；重启通讯员后应自动改用办公室网卡。"
        )
    for warning in choice.warnings:
        lines.append("警告：" + warning)
    return "\n".join(lines)


def unicast_targets(cfg: dict, members: list[dict]) -> list[str]:
    """announce_targets + 成员 lan_ip + 成员 tailscale_ip，写成 host:udp端口。"""
    port = int(cfg.get("udp_port") or 47801)
    me = cfg.get("me")
    found: list[str] = []

    def add(host: str, udp: int | None = None):
        host = (host or "").strip()
        if not host:
            return
        item = f"{host}:{udp or port}"
        if item not in found:
            found.append(item)

    for item in cfg.get("announce_targets") or []:
        text = str(item).strip()
        host, sep, raw = text.rpartition(":")
        if sep and host and raw.isdigit():
            add(host, int(raw))
        else:
            add(text)
    for member in members or []:
        if member.get("name") == me:
            continue
        if member.get("lan_ip"):
            add(str(member["lan_ip"]))
        if member.get("tailscale_ip"):
            add(str(member["tailscale_ip"]))
    return found
