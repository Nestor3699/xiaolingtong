"""xlt doctor：同事最常卡住的几件事，一项一项查，每项不好就写清楚怎么修。

查：通讯员进程、端口 47800/47801/47321 有没有被别的程序占、Windows 防火墙（macOS 应用防火墙）、
VPN/虚拟网卡会不会吞掉局域网广播、本机发出的广播能不能被本机收到、同事能不能直连。
所有系统相关的命令都先判断平台，并且有超时；命令不存在或超时就报「没法判断」，不会崩。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass

from xlt.common import parse_time, read_json
from xlt.netinfo import bind_udp_to_iface, choose_lan, is_loopback_ip, is_vpn_ip
from xlt.store import Home, load_cache, load_config, load_directory, runtime_path

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"
MARK = {OK: "正常", WARN: "注意", FAIL: "有问题", SKIP: "跳过"}


@dataclass
class Check:
    key: str
    title: str
    status: str
    detail: str
    fix: str = ""


def current_system() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def run_cmd(cmd: list[str], timeout: float = 4.0) -> str:
    """跑一条系统命令，拿标准输出。命令不存在、超时、报错都返回空串。"""
    try:
        done = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    raw = done.stdout or b""
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if os.name == "nt":
        out = run_cmd(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"])
        return f'"{pid}"' in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ------------------------------------------------------------------ 端口

def port_free(proto: str, port: int) -> tuple[bool, str]:
    """能不能绑上去。有别的程序在占就应该失败。

    TCP 在 macOS/Linux 上要设 SO_REUSEADDR，不然刚关掉的通讯员留下的 TIME_WAIT 连接会被误报成「被占用」
    （有真的在监听的程序时，设了它照样绑不上）。Windows 上这个选项会允许抢端口，所以不设。
    UDP 不设 SO_REUSEADDR/SO_REUSEPORT：发现端口本来就是靠这两个选项共享的，这里要查的是有没有别人占着。
    """
    kind = socket.SOCK_STREAM if proto == "tcp" else socket.SOCK_DGRAM
    sock = socket.socket(socket.AF_INET, kind)
    try:
        if proto == "tcp" and os.name != "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", int(port)))
        return True, ""
    except OSError as exc:
        return False, str(exc)
    finally:
        sock.close()


def port_owner(system: str, proto: str, port: int, runner=run_cmd) -> str:
    """谁占着这个端口。返回「进程名（pid 123）」，查不到返回空串。"""
    if system in ("macos", "linux"):
        flag = f"-i{proto.upper()}:{port}"
        args = ["lsof", "-nP", flag] + (["-sTCP:LISTEN"] if proto == "tcp" else [])
        for line in runner(args).splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2:
                return f"{parts[0]}（pid {parts[1]}）"
        return ""
    if system == "windows":
        out = runner(["netstat", "-ano", "-p", proto])
        pid = ""
        for line in out.splitlines():
            parts = line.split()
            if not parts or parts[0].upper() != proto.upper() or len(parts) < 4:
                continue
            if parts[1].endswith(f":{port}"):
                pid = parts[-1]
                break
        if not pid.isdigit():
            return ""
        name = ""
        row = runner(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"]).strip()
        if row.startswith('"'):
            name = row.split('","')[0].strip('"')
        return f"{name or '某个程序'}（pid {pid}）"
    return ""


def check_ports(cfg: dict, runtime: dict, running: bool, system: str, runner=run_cmd) -> list[Check]:
    own_pid = runtime.get("pid") if running else None
    specs = [
        ("tcp", int(cfg.get("tcp_port") or 47800), "TCP", "收信发信", True),
        ("udp", int(cfg.get("udp_port") or 47801), "UDP", "互相发现", True),
        ("tcp", int(cfg.get("panel_port") or 47321), "TCP", "本机状态页", False),
    ]
    out = []
    for proto, port, label, what, must_match in specs:
        key = f"port-{proto}-{port}"
        title = f"端口 {label} {port} · {what}"
        free, why = port_free(proto, port)
        if free:
            if running:
                out.append(Check(key, title, WARN, "通讯员在运行，但这个端口是空的，它可能没绑上。", "看通讯员日志 ~/.xlt/messenger.log（Windows 是 %USERPROFILE%\\.xlt\\messenger.log）里有没有「绑定失败」。"))
            else:
                out.append(Check(key, title, OK, "空闲，通讯员可以用。"))
            continue
        owner = port_owner(system, proto, port, runner)
        mine = bool(own_pid) and owner.endswith(f"pid {own_pid}）")
        if running and (mine or not owner):
            out.append(Check(key, title, OK, "被本机的小灵通通讯员占着（正常）。"))
            continue
        who = owner or "查不到是哪个程序"
        if system == "windows":
            how = f"任务管理器里结束它，或在 PowerShell 里：Stop-Process -Id <pid>。"
        else:
            how = "终端里 kill <pid>，或在「活动监视器」里退出它。"
        extra = ""
        if must_match:
            extra = "不要改小灵通的端口：同事之间的广播和连接都按 47800/47801 找，只有你一个人改了会找不到人。"
        else:
            extra = "状态页端口可以换：xlt serve --panel-port 47322。"
        out.append(Check(key, title, FAIL, f"被占用了：{who}。{why}", f"先关掉占用它的程序（{how}）再启动通讯员。{extra}"))
    return out


# ------------------------------------------------------------------ 防火墙

WIN_FW_SCRIPT = (
    "$ErrorActionPreference='Stop';"
    "$p=@(Get-NetConnectionProfile | ForEach-Object { [pscustomobject]@{Alias=[string]$_.InterfaceAlias;Name=[string]$_.Name;Category=[int]$_.NetworkCategory} });"
    "$fw=@(Get-NetFirewallProfile | ForEach-Object { [pscustomobject]@{Name=[string]$_.Name;Enabled=[string]$_.Enabled} });"
    "$r=@(Get-NetFirewallRule -Direction Inbound -Enabled True -Action Allow -ErrorAction SilentlyContinue | ForEach-Object {"
    "$pf=$_ | Get-NetFirewallPortFilter;$af=$_ | Get-NetFirewallApplicationFilter;"
    "[pscustomobject]@{Name=[string]$_.DisplayName;Profile=[int]$_.Profile;Protocol=[string]$pf.Protocol;LocalPort=(@($pf.LocalPort) -join ',');Program=[string]$af.Program} }"
    " | Where-Object { $_.LocalPort -match '47800|47801' -or $_.Program -match 'python|xlt' });"
    "[pscustomobject]@{Profiles=$p;Firewall=$fw;Rules=$r} | ConvertTo-Json -Depth 4 -Compress"
)

# 网络类别（Get-NetConnectionProfile）→ 防火墙配置文件位：Public=0→4，Private=1→2，Domain=2→1
_CATEGORY_BIT = {0: 4, 1: 2, 2: 1}
_CATEGORY_NAME = {0: "公用", 1: "专用", 2: "域"}
_PROFILE_NAME = {"Public": 0, "Private": 1, "Domain": 2}


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _port_covers(spec: str, port: int) -> bool:
    spec = (spec or "").strip()
    if not spec or spec.lower() in ("any", "*"):
        return True
    for piece in spec.split(","):
        piece = piece.strip()
        if "-" in piece:
            lo, _sep, hi = piece.partition("-")
            if lo.isdigit() and hi.isdigit() and int(lo) <= port <= int(hi):
                return True
        elif piece.isdigit() and int(piece) == port:
            return True
    return False


def evaluate_windows_firewall(data: dict, tcp_port: int, udp_port: int) -> list[Check]:
    """把 PowerShell 给的 JSON 翻成检查结果。容错：字段缺了当作没有。"""
    profiles = _as_list(data.get("Profiles"))
    rules = _as_list(data.get("Rules"))
    fw_enabled = {}
    for item in _as_list(data.get("Firewall")):
        name = str(item.get("Name") or "")
        text = str(item.get("Enabled") or "").lower()
        if name in _PROFILE_NAME:
            fw_enabled[_PROFILE_NAME[name]] = text in ("true", "1")
    out = []
    if not profiles:
        categories = []
    else:
        categories = [int(item.get("Category")) for item in profiles if str(item.get("Category")).lstrip("-").isdigit()]
    public = [item for item in profiles if str(item.get("Category")) == "0"]
    if public:
        names = "、".join(f"{item.get('Alias') or '网卡'}（{item.get('Name') or '网络'}）" for item in public)
        out.append(Check(
            "win-network-category", "Windows 网络类别", WARN,
            f"当前网络是「公用」：{names}。公用网络默认挡掉别人连进来，同事发来的信和广播会被拦。",
            "把办公室网络设成「专用」。管理员 PowerShell：Set-NetConnectionProfile -InterfaceAlias \"<上面的网卡名>\" -NetworkCategory Private",
        ))
    elif categories:
        names = "、".join(sorted({_CATEGORY_NAME.get(c, str(c)) for c in categories}))
        out.append(Check("win-network-category", "Windows 网络类别", OK, f"当前网络类别：{names}。"))
    active_bits = {_CATEGORY_BIT[c] for c in categories if c in _CATEGORY_BIT} or {2}

    def enabled_for(bit: int) -> bool:
        for cat, bitv in _CATEGORY_BIT.items():
            if bitv == bit:
                return fw_enabled.get(cat, True)
        return True

    if all(not enabled_for(bit) for bit in active_bits):
        out.append(Check("win-firewall", "Windows 防火墙放行", OK, "当前网络类别的防火墙是关着的，不会拦小灵通。"))
        return out

    def allowed(proto: str, port: int) -> str:
        for rule in rules:
            protocol = str(rule.get("Protocol") or "").upper()
            if protocol not in (proto.upper(), "ANY", ""):
                continue
            profile = int(rule.get("Profile") or 0)
            if profile != 0 and not (profile & sum(active_bits)):
                continue
            program = str(rule.get("Program") or "").lower()
            by_port = _port_covers(str(rule.get("LocalPort") or ""), port) and str(rule.get("LocalPort") or "").lower() not in ("", "any")
            by_program = ("python" in program or "xlt" in program) and _port_covers(str(rule.get("LocalPort") or ""), port)
            if by_port or by_program:
                return str(rule.get("Name") or "")
        return ""

    missing = []
    for proto, port in (("TCP", tcp_port), ("UDP", udp_port)):
        name = allowed(proto, port)
        if not name:
            missing.append((proto, port))
    if not missing:
        out.append(Check("win-firewall", "Windows 防火墙放行", OK, f"已有放行 {tcp_port}/TCP 和 {udp_port}/UDP 的入站规则。"))
    else:
        lack = "、".join(f"{port}/{proto}" for proto, port in missing)
        cmds = "；".join(
            f'New-NetFirewallRule -DisplayName "Xiaolingtong {proto} {port}" -Direction Inbound -Protocol {proto} -LocalPort {port} -Action Allow -Profile Private -RemoteAddress LocalSubnet'
            for proto, port in missing
        )
        out.append(Check(
            "win-firewall", "Windows 防火墙放行", FAIL,
            f"没有找到放行 {lack} 的入站规则（专用网络）。别人发来的信和广播会被拦在门外，同事会一直显示你离线。",
            f"用「以管理员身份运行」的 PowerShell 执行：{cmds}。也可以运行仓库里的 install/windows-startup.ps1。",
        ))
    return out


def check_firewall(cfg: dict, system: str, runner=run_cmd) -> list[Check]:
    tcp_port = int(cfg.get("tcp_port") or 47800)
    udp_port = int(cfg.get("udp_port") or 47801)
    if system == "windows":
        raw = runner(["powershell", "-NoProfile", "-NonInteractive", "-Command", WIN_FW_SCRIPT], 30).strip()
        data = None
        if raw:
            start = raw.find("{")
            try:
                data = json.loads(raw[start:]) if start >= 0 else None
            except json.JSONDecodeError:
                data = None
        if not isinstance(data, dict):
            return [Check(
                "win-firewall", "Windows 防火墙放行", WARN,
                "没法自动读出防火墙规则（PowerShell 没返回结果，或超时）。",
                f"请手动确认：「Windows Defender 防火墙」→「允许应用通过防火墙」里 Python 在「专用」打了勾；或用管理员 PowerShell 放行 {tcp_port}/TCP 和 {udp_port}/UDP（见 install/windows-startup.ps1）。",
            )]
        return evaluate_windows_firewall(data, tcp_port, udp_port)
    if system == "macos":
        tool = "/usr/libexec/ApplicationFirewall/socketfilterfw"
        state = runner([tool, "--getglobalstate"]).lower()
        if not state:
            return [Check("mac-firewall", "macOS 防火墙", SKIP, "没法读取防火墙状态。")]
        if "disabled" in state or "state = 0" in state:
            return [Check("mac-firewall", "macOS 防火墙", OK, "防火墙是关着的，不会拦小灵通。")]
        block_all = runner([tool, "--getblockall"]).lower()
        if "block all" in block_all and "enabled" in block_all:
            return [Check("mac-firewall", "macOS 防火墙", FAIL, "防火墙开着，并且勾了「阻止所有传入连接」。", "系统设置 → 网络 → 防火墙 → 选项，关掉「阻止所有传入连接」。")]
        blocked = runner([tool, "--getappblocked", sys.executable]).lower()
        if "is blocked" in blocked and "not blocked" not in blocked:
            return [Check("mac-firewall", "macOS 防火墙", FAIL, f"防火墙开着，并且拦了 {sys.executable}。", f"sudo {tool} --unblockapp {sys.executable}")]
        return [Check("mac-firewall", "macOS 防火墙", OK, "防火墙开着，但没有拦这个 Python。第一次运行若弹出「是否允许接受传入连接」，请点「允许」。")]
    return [Check("firewall", "防火墙", SKIP, "Linux 不检查防火墙。")]


# ------------------------------------------------------------------ 广播 / VPN

def vpn_advice(reason: str, name: str) -> str:
    text = f"{reason} {name}".lower()
    if "tailscale" in text or "100.64" in text:
        return (
            "Tailscale 一般不影响局域网广播；只有开了「Exit node」时才会。若同事一直离线，"
            "在 Tailscale 里关掉 Exit node，或打开「Allow local network access」。"
        )
    if "clash" in text or "198.18" in text or "surge" in text or "meta" in text:
        return (
            "代理软件的 TUN 模式会把局域网流量也吞进去。在 Clash/Surge 里打开「绕过局域网」，"
            "或在 TUN 配置里把 192.168.0.0/16、10.0.0.0/8、172.16.0.0/12 加进直连/排除路由；实在不行就先关掉 TUN 再试。"
        )
    return "这是虚拟网卡（VPN 或代理的 TUN 模式）。如果同事一直显示离线，先暂时断开 VPN/关掉 TUN 再试；能通就把办公室网段加到它的直连/排除列表。"


def broadcast_selftest(choice, wait: float = 1.2) -> tuple[bool, str]:
    """本机从办公室网卡发一个广播，看本机能不能收到。收不到多半是被虚拟网卡截走了。"""
    ip = choice.ip
    if not ip or is_loopback_ip(ip):
        return True, "地址是 127.0.0.1（测试用），跳过。"
    recv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    send = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        recv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        recv.bind(("", 0))
        recv.settimeout(0.3)
        port = recv.getsockname()[1]
        send.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        send.bind((ip, 0))
        bind_udp_to_iface(send, ip, choice.iface.name if choice.iface else "")
        token = f"xlt-doctor-{os.getpid()}-{time.time_ns()}".encode()
        sent_to = []
        for dest in choice.broadcasts:
            try:
                send.sendto(token, (dest, port))
                sent_to.append(dest)
            except OSError:
                continue
        if not sent_to:
            return False, "广播一个都发不出去（系统拒绝）。"
        end = time.time() + wait
        while time.time() < end:
            try:
                data, _src = recv.recvfrom(256)
            except socket.timeout:
                continue
            except OSError:
                break
            if data == token:
                return True, f"从 {ip} 发往 {'、'.join(sent_to)} 的广播，本机收到了。"
        return False, f"从 {ip} 发往 {'、'.join(sent_to)} 的广播，{wait:.0f} 秒内本机没收到。"
    except OSError as exc:
        return False, f"广播自测没能做起来：{exc}"
    finally:
        recv.close()
        send.close()


def check_network(home: Home, cfg: dict, running: bool, system: str, choice=None) -> list[Check]:
    choice = choice or choose_lan()
    out = []
    if not choice.ip or (choice.iface is None and not is_loopback_ip(choice.ip)):
        out.append(Check("lan-iface", "办公室网卡", FAIL, "没有找到办公室网卡（没连 Wi-Fi/网线？）。", "确认这台电脑已经连上办公室的 Wi-Fi 或网线，再重新运行 xlt doctor。"))
    elif is_vpn_ip(choice.ip):
        out.append(Check("lan-iface", "办公室网卡", FAIL, f"选中的地址 {choice.ip} 是 VPN/虚拟网卡的地址，发现包会发错网卡。", "断开 VPN 后重启通讯员，它会自动改用办公室网卡；不要用 --bind 指定这个地址。"))
    else:
        iface = choice.iface.name if choice.iface else "—"
        out.append(Check("lan-iface", "办公室网卡", OK, f"{iface}  {choice.ip}  广播发往 {'、'.join(choice.broadcasts)}"))
    if choice.vpn_ifaces:
        for item in choice.vpn_ifaces:
            out.append(Check(
                f"vpn-{item.name}", f"检测到虚拟/VPN 网卡 {item.name}", WARN,
                f"{item.name}  {item.ip}  {item.vpn_reason}",
                vpn_advice(item.vpn_reason, item.name),
            ))
    else:
        out.append(Check("vpn", "VPN / 代理 TUN", OK, "没有检测到 Clash、Tailscale 一类的虚拟网卡。"))
    if not is_loopback_ip(choice.ip or ""):
        ok, note = broadcast_selftest(choice)
        if ok:
            out.append(Check("broadcast-selftest", "局域网广播自测", OK, note + "（只能证明本机没拦，不能证明路由器放行。）"))
        else:
            tip = vpn_advice(choice.vpn_ifaces[0].vpn_reason, choice.vpn_ifaces[0].name) if choice.vpn_ifaces else "检查是不是连在访客 Wi-Fi/开了「AP 隔离」；这些网络不让设备互相看到。"
            out.append(Check("broadcast-selftest", "局域网广播自测", WARN, note, tip))
    out.extend(check_peers(home, cfg, running))
    return out


def _peer_age(row: dict) -> float | None:
    try:
        return (time.time() - parse_time(row["last_seen"]).timestamp())
    except Exception:
        return None


def check_peers(home: Home, cfg: dict, running: bool) -> list[Check]:
    from xlt.mtls import FingerprintMismatch, request

    me = cfg.get("me")
    members = [m for m in load_directory(home) if m.get("name") != me]
    if not members:
        return [Check("peers", "同事能不能看到", SKIP, "名单里还没有其他同事。等他们也启动通讯员，或用 xlt trust 手工加。")]
    cache = load_cache(home).get("peers") or {}
    fresh = []
    for member in members:
        row = cache.get(member["name"]) or {}
        age = _peer_age(row)
        if row.get("state") == "online" and age is not None and age < 600:
            fresh.append(member["name"])
    out = []
    if running:
        if fresh:
            out.append(Check("peers-seen", "同事能不能看到", OK, f"{len(members)} 位同事里，{len(fresh)} 位最近 10 分钟发过广播：{'、'.join(fresh)}。"))
        else:
            out.append(Check(
                "peers-seen", "同事能不能看到", WARN,
                f"名单里有 {len(members)} 位同事，最近 10 分钟一位的广播都没收到。（他们没开机/没启动也会这样。）",
                "先确认对方也在运行小灵通、和你在同一个办公室 Wi-Fi；再看上面「广播自测」和「虚拟/VPN 网卡」两项；Windows 同事还要看防火墙那一项。",
            ))
    else:
        out.append(Check("peers-seen", "同事能不能看到", SKIP, "通讯员没在运行，没法看广播。"))

    targets = []
    for member in members:
        row = cache.get(member["name"]) or {}
        ip = row.get("ip") or member.get("lan_ip")
        if ip:
            targets.append((member, ip, int(row.get("port") or member.get("port") or 47800)))
    if not targets:
        return out

    def probe(item):
        member, ip, port = item
        try:
            trust = (home.peers_dir / f"{member['name']}.crt").read_text(encoding="utf-8")
            code, _body = request(ip, port, "GET", "/xlt/v1/health", b"", home.tls / f"{cfg['me']}.crt", home.tls / f"{cfg['me']}.key", member.get("cert_sha256"), trust, 2)
            return member["name"], ip, code == 200, "" if code == 200 else f"HTTP {code}"
        except FingerprintMismatch:
            return member["name"], ip, False, "证书指纹对不上（对方换过证书？）"
        except Exception as exc:  # 自检不能因为一位同事的数据坏了就整个崩掉
            return member["name"], ip, False, exc.__class__.__name__

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(probe, targets[:12]))
    good = [f"{name}（{ip}）" for name, ip, ok, _why in results if ok]
    bad = [(name, ip, why) for name, ip, ok, why in results if not ok]
    if good:
        out.append(Check("peers-direct", "能不能直连同事（TCP）", OK, "连得上：" + "、".join(good)))
    if bad:
        lines = "；".join(f"{name}（{ip}）{why}" for name, ip, why in bad)
        out.append(Check(
            "peers-direct-bad", "能不能直连同事（TCP）", WARN,
            "连不上：" + lines + "。对方关机、睡眠或换了地址时也会这样。",
            "对方开着的话：多半是对方的防火墙没放行 47800/TCP（Windows 看对方的 xlt doctor），或你们不在同一个网段。",
        ))
    return out


# ------------------------------------------------------------------ 总入口

def check_gateway(home: Home, runtime: dict, running: bool, system: str) -> Check:
    if running:
        return Check("gateway", "通讯员进程", OK, f"运行中（进程 {runtime.get('pid')}，启动于 {runtime.get('started') or '—'}）。")
    if system == "macos":
        fix = "重启：launchctl kickstart -k gui/$(id -u)/com.xiaolingtong.gateway ；没装自启就前台运行 xlt serve。"
    elif system == "windows":
        fix = "在 PowerShell 里：Start-ScheduledTask -TaskName \"Xiaolingtong Gateway\" ；没装自启就前台运行 xlt serve。"
    else:
        fix = "前台运行 xlt serve。"
    return Check("gateway", "通讯员进程", WARN, "没有在运行。没人收信，也没人能看到你在线。", fix)


def run_checks(home: Home, system: str | None = None, runner=run_cmd, choice=None) -> list[Check]:
    system = system or current_system()
    cfg = load_config(home)
    runtime = read_json(runtime_path(home), {})
    running = pid_alive(runtime.get("pid"))
    checks = [check_gateway(home, runtime, running, system)]
    checks.extend(check_ports(cfg, runtime if running else {}, running, system, runner))
    checks.extend(check_firewall(cfg, system, runner))
    checks.extend(check_network(home, cfg, running, system, choice))
    return checks


def format_checks(checks: list[Check], system: str) -> str:
    names = {"macos": "macOS", "windows": "Windows", "linux": "Linux"}
    lines = [f"小灵通自检（{names.get(system, system)}）", ""]
    for item in checks:
        lines.append(f"[{MARK[item.status]}] {item.title}")
        lines.append(f"    {item.detail}")
        if item.fix and item.status in (WARN, FAIL):
            lines.append(f"    怎么修：{item.fix}")
    count = {key: sum(1 for c in checks if c.status == key) for key in (OK, WARN, FAIL, SKIP)}
    lines.append("")
    lines.append(f"共 {len(checks)} 项：正常 {count[OK]}，注意 {count[WARN]}，有问题 {count[FAIL]}，跳过 {count[SKIP]}。")
    if count[FAIL]:
        lines.append("先把「有问题」的修掉，再看「注意」。修完重新运行 xlt doctor。")
    return "\n".join(lines)


def to_json(checks: list[Check]) -> str:
    return json.dumps([asdict(c) for c in checks], ensure_ascii=False, indent=2)
