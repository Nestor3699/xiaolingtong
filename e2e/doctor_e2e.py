#!/usr/bin/env python3
"""xlt doctor：真跑一遍命令行；端口被占、通讯员自己占端口、同事掉线；再用夹具走一遍 Windows / macOS / VPN 的分支。

Windows 和 macOS 的系统命令在这台 Linux 上不能真跑，用「假的命令输出」验证解析和提示文字；
这一段只能算夹具测试，真机要等同事跑 xlt doctor 才算数。
"""

from __future__ import annotations

import json
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import Fail, fresh, pair, say, wait_for, xlt  # noqa: E402

from xlt import doctor  # noqa: E402
from xlt.netinfo import Iface, choose_lan, classify  # noqa: E402
from xlt.store import Home  # noqa: E402

WORK = Path(__file__).resolve().parent / "work-doctor"


def by_key(checks):
    return {c["key"]: c for c in checks}


def run_json(node_or_home) -> list[dict]:
    home = node_or_home.home if hasattr(node_or_home, "home") else node_or_home
    r = xlt(home, "doctor", "--json")
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        raise Fail("xlt doctor --json 没输出 JSON：" + r.stdout + r.stderr)


def main() -> int:
    fresh(WORK)
    nes, hao = pair(WORK, 47900)
    listeners: list[socket.socket] = []
    try:
        # ---- 1. 端口被别的程序占着（通讯员没开）----
        tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tcp.bind(("0.0.0.0", nes.tcp))
        tcp.listen(1)
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.bind(("0.0.0.0", nes.udp))
        listeners += [tcp, udp]
        cfg = json.loads((nes.home / "config.json").read_text(encoding="utf-8"))
        cfg.update({"tcp_port": nes.tcp, "udp_port": nes.udp, "panel_port": nes.panel})
        (nes.home / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
        checks = by_key(run_json(nes))
        tcp_c, udp_c = checks[f"port-tcp-{nes.tcp}"], checks[f"port-udp-{nes.udp}"]
        if tcp_c["status"] != "fail" or udp_c["status"] != "fail":
            raise Fail(f"被占用的端口应该报「有问题」：{tcp_c} {udp_c}")
        if "不要改小灵通的端口" not in tcp_c["fix"] or "关掉占用" not in tcp_c["fix"]:
            raise Fail("占用端口的提示里没有写清怎么修：" + tcp_c["fix"])
        if checks[f"port-tcp-{nes.panel}"]["status"] != "ok":
            raise Fail("没被占的状态页端口应该是正常")
        r = xlt(nes.home, "doctor")
        if r.returncode != 1 or "怎么修：" not in r.stdout or "[有问题]" not in r.stdout:
            raise Fail("有问题时 xlt doctor 应退出码 1，并打印「怎么修」：" + r.stdout)
        say("端口被别的程序占着：报「有问题」，写明占用者（查得到时）和怎么修，退出码 1")
        for item in listeners:
            item.close()
        listeners.clear()

        # ---- 2. 通讯员自己占端口不算冲突；同事在线 / 掉线 ----
        nes.start()
        hao.start()
        wait_for(lambda: (nes.home / "peers.cache.json").exists(), 15, "没有互相发现")
        time.sleep(1)
        checks = by_key(run_json(nes))
        for key in (f"port-tcp-{nes.tcp}", f"port-udp-{nes.udp}", f"port-tcp-{nes.panel}"):
            if checks[key]["status"] != "ok" or "本机的小灵通" not in checks[key]["detail"]:
                raise Fail(f"通讯员自己占着端口不该当冲突：{checks[key]}")
        if checks["gateway"]["status"] != "ok":
            raise Fail("通讯员在运行，进程这一项应该正常")
        if checks["peers-seen"]["status"] != "ok" or checks["peers-direct"]["status"] != "ok":
            raise Fail(f"同事在线时应能看到并能直连：{checks['peers-seen']} {checks.get('peers-direct')}")
        say("通讯员自己占端口不算冲突；同事在线时「能看到、能直连」两项正常")
        hao.crash()
        checks = by_key(run_json(nes))
        if checks.get("peers-direct-bad", {}).get("status") != "warn":
            raise Fail("同事掉线后直连应该报「注意」：" + json.dumps(checks.get("peers-direct-bad"), ensure_ascii=False))
        if "broadcast-selftest" not in checks:
            raise Fail("没有做广播自测")
        say("同事掉线后：直连一项报「注意」并写明原因；广播自测一项存在（真网卡上：" + checks["broadcast-selftest"]["status"] + "）")
        nes.crash()

        # ---- 3. Windows 分支（夹具：假的 PowerShell / netstat 输出）----
        cfg = {"me": "nes-mac", "tcp_port": 47800, "udp_port": 47801, "panel_port": 47321}

        def win(profiles, rules, firewall=None):
            return json.dumps({
                "Profiles": profiles,
                "Firewall": firewall or [{"Name": "Public", "Enabled": "True"}, {"Name": "Private", "Enabled": "True"}, {"Name": "Domain", "Enabled": "True"}],
                "Rules": rules,
            })

        office = [{"Alias": "WLAN", "Name": "Office-WiFi", "Category": 1}]
        public = [{"Alias": "WLAN", "Name": "Office-WiFi", "Category": 0}]
        both = [
            {"Name": "Xiaolingtong gateway (TCP 47800)", "Profile": 2, "Protocol": "TCP", "LocalPort": "47800", "Program": "Any"},
            {"Name": "Xiaolingtong discovery (UDP 47801)", "Profile": 2, "Protocol": "UDP", "LocalPort": "47801", "Program": "Any"},
        ]
        got = {c.key: c for c in doctor.check_firewall(cfg, "windows", lambda *a, **k: win(office, both))}
        if got["win-firewall"].status != "ok" or got["win-network-category"].status != "ok":
            raise Fail("专用网络 + 两条放行规则应该全正常：" + str(got))
        got = {c.key: c for c in doctor.check_firewall(cfg, "windows", lambda *a, **k: win(public, both))}
        if got["win-network-category"].status != "warn" or "Set-NetConnectionProfile" not in got["win-network-category"].fix:
            raise Fail("公用网络应提示改成专用，并给出命令：" + str(got))
        got = {c.key: c for c in doctor.check_firewall(cfg, "windows", lambda *a, **k: win(office, []))}
        fw = got["win-firewall"]
        if fw.status != "fail" or "New-NetFirewallRule" not in fw.fix or "47800" not in fw.fix or "47801" not in fw.fix:
            raise Fail("没有放行规则应报「有问题」并给出两条放行命令：" + str(fw))
        only_tcp = both[:1]
        fw = {c.key: c for c in doctor.check_firewall(cfg, "windows", lambda *a, **k: win(office, only_tcp))}["win-firewall"]
        if fw.status != "fail" or "47801/UDP" not in fw.detail or "47800/TCP" in fw.detail:
            raise Fail("只缺 UDP 时应只点名 47801/UDP：" + str(fw))
        py_rule = [{"Name": "python.exe", "Profile": 2, "Protocol": "Any", "LocalPort": "Any", "Program": "C:\\Python312\\python.exe"}]
        fw = {c.key: c for c in doctor.check_firewall(cfg, "windows", lambda *a, **k: win(office, py_rule))}["win-firewall"]
        if fw.status != "ok":
            raise Fail("按程序放行（弹窗点「允许」产生的 python.exe 规则）应算已放行：" + str(fw))
        wrong_profile = [{"Name": "x", "Profile": 4, "Protocol": "TCP", "LocalPort": "47800-47801", "Program": "Any"},
                         {"Name": "y", "Profile": 4, "Protocol": "UDP", "LocalPort": "47800-47801", "Program": "Any"}]
        fw = {c.key: c for c in doctor.check_firewall(cfg, "windows", lambda *a, **k: win(office, wrong_profile))}["win-firewall"]
        if fw.status != "fail":
            raise Fail("规则只开在「公用」而当前是专用网络，应报没放行：" + str(fw))
        off = [{"Name": "Public", "Enabled": "True"}, {"Name": "Private", "Enabled": "False"}, {"Name": "Domain", "Enabled": "True"}]
        fw = {c.key: c for c in doctor.check_firewall(cfg, "windows", lambda *a, **k: win(office, [], off))}["win-firewall"]
        if fw.status != "ok":
            raise Fail("专用网络的防火墙关着就不会拦：" + str(fw))
        fw = doctor.check_firewall(cfg, "windows", lambda *a, **k: "")[0]
        if fw.status != "warn" or "手动确认" not in fw.fix:
            raise Fail("PowerShell 没返回时应报「没法判断」并给手动办法：" + str(fw))
        say("Windows 防火墙分支（夹具）：专用/公用网络、缺哪条规则、按程序放行、规则开错网络、防火墙关着、PowerShell 超时")

        netstat = (
            "\n活动连接\n\n  协议  本地地址          外部地址        状态           PID\n"
            "  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       1048\n"
            "  TCP    0.0.0.0:47800          0.0.0.0:0              LISTENING       7788\n"
        )

        def fake_win(cmd, timeout=4.0):
            if cmd[0] == "netstat":
                return netstat
            if cmd[0] == "tasklist":
                return '"clash-verge.exe","7788","Console","1","61,296 K"\n'
            return ""

        owner = doctor.port_owner("windows", "tcp", 47800, fake_win)
        if "clash-verge.exe" not in owner or "7788" not in owner:
            raise Fail("Windows 查端口占用者没解析出来：" + owner)
        say("Windows 端口占用者（netstat + tasklist，中文系统输出）能解析出进程名和 pid")

        # ---- 4. macOS 防火墙（夹具）----
        def fake_mac(state, blockall="Block all DISABLED!", app="is permitted"):
            def run(cmd, timeout=4.0):
                if "--getglobalstate" in cmd:
                    return state
                if "--getblockall" in cmd:
                    return blockall
                if "--getappblocked" in cmd:
                    return f"Application at path ( {sys.executable} ) {app}"
                return ""
            return run

        g = doctor.check_firewall(cfg, "macos", fake_mac("Firewall is enabled. (State = 1)"))[0]
        if g.status != "ok" or "允许" not in g.detail:
            raise Fail("开着但没拦：应正常并提醒首次弹窗点允许：" + str(g))
        g = doctor.check_firewall(cfg, "macos", fake_mac("Firewall is enabled. (State = 1)", "Block all ENABLED!"))[0]
        if g.status != "fail":
            raise Fail("勾了阻止所有传入连接应报有问题")
        g = doctor.check_firewall(cfg, "macos", fake_mac("Firewall is enabled. (State = 1)", app="is blocked"))[0]
        if g.status != "fail" or "unblockapp" not in g.fix:
            raise Fail("拦了 Python 应给出 unblockapp 命令")
        g = doctor.check_firewall(cfg, "macos", fake_mac("Firewall is disabled. (State = 0)"))[0]
        if g.status != "ok":
            raise Fail("防火墙关着应正常")
        say("macOS 防火墙分支（夹具）：关着、开着没拦、阻止所有、拦了 Python")

        # ---- 5. VPN / 代理 TUN 吞广播（夹具网卡表）----
        ifaces = [classify(Iface("en0", "192.168.1.10", 24)), classify(Iface("utun3", "198.18.0.1", 16))]
        choice = choose_lan(ifaces, default_if="utun3")
        checks = {c.key: c for c in doctor.check_network(Home(nes.home), cfg, False, "macos", choice)}
        vpn = checks.get("vpn-utun3")
        if vpn is None or vpn.status != "warn" or "绕过局域网" not in vpn.fix:
            raise Fail("Clash TUN 网卡应报「注意」，并写怎么设直连：" + str(vpn))
        ts = classify(Iface("utun4", "100.101.102.103", 32))
        choice = choose_lan([classify(Iface("en0", "192.168.1.10", 24)), ts], default_if="en0")
        checks = {c.key: c for c in doctor.check_network(Home(nes.home), cfg, False, "macos", choice)}
        if "Exit node" not in checks["vpn-utun4"].fix:
            raise Fail("Tailscale 的提示应提到 Exit node：" + str(checks["vpn-utun4"]))
        say("VPN 分支（夹具网卡表）：Clash TUN 和 Tailscale 各给出对应的修法")

        # ---- 6. run_checks 在三种平台标签下都不抛异常 ----
        for system in ("windows", "macos", "linux"):
            items = doctor.run_checks(Home(nes.home), system=system, runner=lambda *a, **k: "")
            if not items:
                raise Fail(f"{system} 没有任何检查项")
        say("三种平台标签下 run_checks 都不崩（系统命令全部不可用也能给出结果）")
        print("DOCTOR E2E 全部通过")
        return 0
    except Fail as exc:
        print("失败", exc)
        return 1
    finally:
        for item in listeners:
            item.close()
        for node in (nes, hao):
            node.crash()


if __name__ == "__main__":
    raise SystemExit(main())
