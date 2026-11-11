#!/usr/bin/env python3
"""用固定的 ifconfig/ipconfig 文本检查网卡探测，不访问真实网络。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from xlt.netinfo import (
    choose_lan,
    format_doctor,
    is_vpn_ip,
    parse_ifconfig,
    parse_ipconfig,
    parse_net_ip_address_json,
    unicast_targets,
)

MAC_CLASH = """
lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
	inet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
	ether f6:11:22:33:44:55
	inet 192.168.0.133 netmask 0xfffffe00 broadcast 192.168.1.255
utun2: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 9000
	inet 198.18.0.1 --> 198.18.0.1 netmask 0xfffffff8
utun3: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1280
	inet 100.91.150.79 --> 100.91.150.79 netmask 0xffffffff
"""

WIN_CLASH = """
Windows IP Configuration

Ethernet adapter Ethernet:

   Media State . . . . . . . . . . . : Media disconnected

Wireless LAN adapter Wi-Fi:

   IPv4 Address. . . . . . . . . . . : 10.0.0.42
   Subnet Mask . . . . . . . . . . . : 255.255.255.0
   Default Gateway . . . . . . . . . : 10.0.0.1

Unknown adapter Meta:

   IPv4 Address. . . . . . . . . . . : 198.18.0.1
   Subnet Mask . . . . . . . . . . . : 255.254.0.0

Unknown adapter Tailscale:

   IPv4 Address. . . . . . . . . . . : 100.91.1.2
   Subnet Mask . . . . . . . . . . . : 255.255.255.255
"""

WIN_JSON = """
[
  {"IPAddress":"10.0.0.42","PrefixLength":24,"InterfaceAlias":"Wi-Fi"},
  {"IPAddress":"198.18.0.1","PrefixLength":15,"InterfaceAlias":"Meta"},
  {"IPAddress":"100.91.1.2","PrefixLength":32,"InterfaceAlias":"Tailscale"}
]
"""


class Fail(Exception):
    pass


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise Fail(msg)
    print("ok:", msg)


def main() -> int:
    ifaces = parse_ifconfig(MAC_CLASH)
    names = {item.name: item for item in ifaces}
    check(set(names) == {"lo0", "en0", "utun2", "utun3"}, "解析出四块网卡")
    check(names["en0"].prefix == 23, "en0 前缀是 /23")
    check(names["en0"].broadcast == "192.168.1.255", "定向广播是 192.168.1.255，不是 192.168.0.255")
    check(names["en0"].vpn is False, "en0 不是 VPN")
    check(names["lo0"].loopback, "lo0 是回环")
    check("198.18" in names["utun2"].vpn_reason, "utun2 识别为 Clash Fake-IP")
    check("100.64" in names["utun3"].vpn_reason, "utun3 识别为 Tailscale")
    check(is_vpn_ip("198.18.0.1") and is_vpn_ip("100.91.150.79"), "198.18/15 与 100.64/10 判定为 VPN")
    check(not is_vpn_ip("192.168.0.133") and not is_vpn_ip("10.0.0.1"), "办公室私网地址不是 VPN")

    choice = choose_lan(ifaces, default_if="utun2")
    check(choice.ip == "192.168.0.133", f"默认路由走 utun2 时仍选 en0，实际 {choice.ip}")
    check(choice.iface is not None and choice.iface.name == "en0", "选中网卡是 en0")
    check("192.168.1.255" in choice.broadcasts, "广播列表含真实 /23 广播")
    check("192.168.0.255" not in choice.broadcasts, "广播列表不含错误的 /24 猜测 192.168.0.255")
    check("255.255.255.255" in choice.broadcasts, "广播列表含 255.255.255.255")
    check(len(choice.vpn_ifaces) == 2, "记录两块 VPN 网卡")
    check(any("虚拟网卡" in w or "utun2" in w for w in choice.warnings), "对默认路由走 VPN 给出警告")

    report = format_doctor(choice, {"bind_ip": "198.18.0.1", "announce_ip": "198.18.0.1"})
    check("198.18.0.1" in report and "警告" in report, "bind 为 Fake-IP 时医生报告发出警告")
    check("192.168.0.133" in report and "en0" in report, "医生报告包含选中的办公室地址")

    targets = unicast_targets(
        {"me": "nes-mac", "udp_port": 47801, "announce_targets": ["192.168.0.20", "10.0.0.8:47999"]},
        [
            {"name": "nes-mac", "lan_ip": "192.168.0.133", "tailscale_ip": "100.91.150.79"},
            {"name": "runda-mac", "lan_ip": "192.168.0.50", "tailscale_ip": "100.91.1.9"},
            {"name": "leo-mac"},
        ],
    )
    check("192.168.0.20:47801" in targets, "announce_targets 无端口时补上 UDP 47801")
    check("10.0.0.8:47999" in targets, "announce_targets 可带自定义端口")
    check("192.168.0.50:47801" in targets, "成员 lan_ip 进入单播备用")
    check("100.91.1.9:47801" in targets, "成员 tailscale_ip 进入单播备用")
    check("192.168.0.133:47801" not in targets and "100.91.150.79:47801" not in targets, "不把自己的地址列入单播")

    win = parse_ipconfig(WIN_CLASH)
    win_names = {item.name: item for item in win}
    check("Wi-Fi" in win_names and win_names["Wi-Fi"].ip == "10.0.0.42", "解析 Windows ipconfig 的 Wi-Fi")
    check(win_names["Wi-Fi"].prefix == 24 and win_names["Wi-Fi"].broadcast == "10.0.0.255", "Wi-Fi /24 广播正确")
    check(win_names["Meta"].vpn and win_names["Tailscale"].vpn, "Meta / Tailscale 适配器视为 VPN")
    win_choice = choose_lan(win, default_if="Meta")
    check(win_choice.ip == "10.0.0.42", f"Windows 在 Meta 默认路由下仍选 Wi-Fi，实际 {win_choice.ip}")

    js = parse_net_ip_address_json(WIN_JSON)
    js_choice = choose_lan(js, default_if="Meta")
    check(js_choice.ip == "10.0.0.42" and js_choice.iface.name == "Wi-Fi", "Get-NetIPAddress JSON 同样选中 Wi-Fi")

    print("全部通过")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Fail as exc:
        print("失败：", exc, file=sys.stderr)
        raise SystemExit(1)
