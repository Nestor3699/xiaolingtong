"""本机状态页。只渲染信封，不读信纸。"""

from __future__ import annotations

import html
import json
from pathlib import Path

from xlt.common import envelope_summary, read_json
from xlt.store import Home, load_cache, load_config, load_directory, load_routes, load_windows

LABELS = {
    "online": "在线",
    "offline": "离线",
    "unreachable": "在线但连不上",
    "queued": "排队",
    "pending": "待发",
    "delivered": "处理中",
    "replied": "已回复",
    "declined": "已拒答",
    "done": "已处理",
    "awaiting_manual": "待手动处理",
    "expired": "已过期",
    "failed": "失败",
    "no_reply": "无回复",
    "egress_blocked": "出口拦截",
    "delivering": "正在交给 AI",
    "accepted": "已收下",
    "receiving": "正在收附件",
}


def label(value: str | None) -> str:
    if not value:
        return "—"
    return LABELS.get(value, value)


def _load_env(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _pending_rows(home: Home) -> list[dict]:
    rows = []
    root = home.pending
    if not root.exists():
        return rows
    for path in sorted(root.rglob("*.env.json")):
        if path.name.endswith(".tmp"):
            continue
        env = _load_env(path)
        if not env:
            continue
        item = envelope_summary(env)
        item["box"] = "expired" if "expired" in path.parts else "failed" if "failed" in path.parts else "pending"
        item["to_member"] = env.get("to_member")
        item["attempts"] = env.get("attempts") or 0
        item["last_error"] = env.get("last_error")
        rows.append(item)
    return rows


def _inbox_rows(home: Home) -> list[dict]:
    rows = []
    root = home.inbox
    if not root.exists():
        return rows
    for path in sorted(root.rglob("*.env.json")):
        env = _load_env(path)
        if not env:
            continue
        item = envelope_summary(env)
        item["folder"] = path.parent.name
        rows.append(item)
    rows.sort(key=lambda item: item.get("created") or "", reverse=True)
    return rows


def _jsonl_tail(path: Path, limit: int) -> list[dict]:
    if not path.exists():
        return []
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = []
    for line in lines[-limit:]:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def window_liveness(home: Home, window: dict) -> str:
    delivery = window.get("delivery")
    if delivery in ("channel", "extension"):
        alive = home.d("channels", f"{window.get('window')}.alive")
        if not alive.exists():
            return "未连接"
        import time

        if time.time() - alive.stat().st_mtime > 90:
            return "失效"
        return "ok"
    return window.get("state") or "ok"


def collect(home: Home) -> dict:
    cfg = load_config(home)
    cache = load_cache(home).get("peers") or {}
    pending = _pending_rows(home)
    active = [row for row in pending if row["box"] == "pending"]
    peers = []
    for agent in load_directory(home):
        if agent.get("name") == cfg.get("me"):
            continue
        row = cache.get(agent["name"]) or {}
        mine = [item for item in active if item.get("to_member") == agent["name"]]
        peers.append(
            {
                "name": agent.get("name"),
                "person": agent.get("person"),
                "owner": agent.get("owner"),
                "state": row.get("state") or "offline",
                "ip": row.get("ip") or agent.get("lan_ip") or "",
                "last_seen": row.get("last_seen"),
                "source": row.get("source") or ("static" if agent.get("lan_ip") else ""),
                "pending": len(mine),
                "note": row.get("last_offline_reason") or "",
                "cert_warning": row.get("cert_warning") or "",
            }
        )
    windows = []
    for window in load_windows(home):
        folder = home.inbox / window.get("window", "")
        queued = 0
        if folder.exists():
            for path in folder.glob("*.env.json"):
                env = _load_env(path)
                if env and (env.get("state") or {}).get("status") in ("queued", "awaiting_manual", "delivering"):
                    queued += 1
        windows.append(
            {
                "ref": f"{window.get('agent')}/{window.get('window')}",
                "delivery": window.get("delivery"),
                "liveness": window_liveness(home, window),
                "generation": window.get("generation") or 1,
                "queued": queued,
                "desc": window.get("desc") or "",
            }
        )
    runtime = read_json(home.root / "runtime.json", {})
    routes = load_routes(home)
    try:
        from xlt.netinfo import choose_lan, format_doctor, unicast_targets

        choice = choose_lan()
        network = format_doctor(
            choice,
            cfg,
            {
                "listen_ip": runtime.get("listen_ip") or choice.listen_ip,
                "bind": runtime.get("bind") or cfg.get("bind_ip"),
                "unicast": unicast_targets(cfg, load_directory(home)),
            },
        )
    except Exception as exc:
        network = f"网络诊断暂时不可用：{exc}"
    return {
        "me": cfg.get("me"),
        "person": cfg.get("person"),
        "owner": cfg.get("owner"),
        "version": "0.1.0",
        "network": network,
        "panel_port": runtime.get("panel_port") or cfg.get("panel_port"),
        "tcp_port": runtime.get("tcp_port") or cfg.get("tcp_port"),
        "udp_port": runtime.get("udp_port") or cfg.get("udp_port"),
        "started": runtime.get("started"),
        "wake_note": (home.root / "wake-note.txt").read_text(encoding="utf-8").strip()
        if (home.root / "wake-note.txt").exists()
        else "",
        "routes_default": routes.get("default") or "（还没设）",
        "on_window_lost": routes.get("on_window_lost") or "fallback_default",
        "peers": peers,
        "pending": active,
        "pending_failed": [row for row in pending if row["box"] != "pending"][-10:],
        "inbox": _inbox_rows(home)[:30],
        "windows": windows,
        "rejects": _jsonl_tail(home.root / "rejects.jsonl", 12),
    }


def _esc(value) -> str:
    return html.escape("" if value is None else str(value))


def _badge(state: str | None) -> str:
    text = label(state)
    kind = state or "offline"
    return f'<span class="badge { _esc(kind) }">{_esc(text)}</span>'


def render(snap: dict) -> str:
    peer_rows = []
    for peer in snap["peers"]:
        peer_rows.append(
            "<tr>"
            f"<td>{_esc(peer['owner'])}<div class='muted'>{_esc(peer['person'])} · {_esc(peer['name'])}</div></td>"
            f"<td>{_badge(peer['state'])}</td>"
            f"<td>{_esc(peer['ip'] or '—')}</td>"
            f"<td>{_esc(peer['last_seen'] or '—')}</td>"
            f"<td>{_esc(peer['pending'])}</td>"
            f"<td>{_esc(peer['source'])}</td>"
            "</tr>"
        )
        if peer.get("cert_warning"):
            peer_rows.append(
                f"<tr><td colspan='6' class='warn'>{_esc(peer['cert_warning'])}</td></tr>"
        )
    if not peer_rows:
        peer_rows.append("<tr><td colspan='6' class='muted'>成员名单里还没有其他同事。用 xlt trust 把他们的证书加进来。</td></tr>")

    pend_rows = []
    for item in snap["pending"]:
        pend_rows.append(
            "<tr>"
            f"<td class='mono'>{_esc(item.get('id'))}</td>"
            f"<td>{_esc(item.get('to_member') or item.get('to'))}</td>"
            f"<td>{_esc(item.get('type'))}</td>"
            f"<td>{_esc(item.get('subject') or '—')}</td>"
            f"<td>{_badge((item.get('state') or {}).get('status') or 'pending')}</td>"
            f"<td>{_esc(item.get('attempts'))}</td>"
            f"<td>{_esc(item.get('letter_bytes'))} 字节</td>"
            "</tr>"
        )
    if not pend_rows:
        pend_rows.append("<tr><td colspan='7' class='muted'>待发箱是空的。</td></tr>")

    in_rows = []
    for item in snap["inbox"]:
        state = (item.get("state") or {}).get("status")
        in_rows.append(
            "<tr>"
            f"<td class='mono'>{_esc(item.get('id'))}</td>"
            f"<td>{_esc(item.get('from_person'))}</td>"
            f"<td>{_esc(item.get('type'))}</td>"
            f"<td>{_esc(item.get('subject') or '—')}</td>"
            f"<td>{_esc((item.get('state') or {}).get('routed_to') or item.get('folder'))}</td>"
            f"<td>{_badge(state)}</td>"
            f"<td>{_esc(item.get('letter_bytes'))} 字节</td>"
            "</tr>"
        )
    if not in_rows:
        in_rows.append("<tr><td colspan='7' class='muted'>还没有收到信。</td></tr>")

    win_rows = []
    for window in snap["windows"]:
        win_rows.append(
            "<tr>"
            f"<td>{_esc(window['ref'])}</td>"
            f"<td>{_esc(window['delivery'])}</td>"
            f"<td>{_esc(window['liveness'])}</td>"
            f"<td>{_esc(window['queued'])}</td>"
            f"<td>{_esc(window['generation'])}</td>"
            f"<td>{_esc(window['desc'])}</td>"
            "</tr>"
        )
    if not win_rows:
        win_rows.append("<tr><td colspan='6' class='muted'>还没有登记收信窗口。xlt register 之后，信才知道交给谁。</td></tr>")

    rej_rows = []
    for item in reversed(snap["rejects"]):
        rej_rows.append(
            "<tr>"
            f"<td>{_esc(item.get('ts'))}</td>"
            f"<td>{_esc(item.get('caller') or item.get('from'))}</td>"
            f"<td>{_esc(item.get('action') or item.get('type'))}</td>"
            f"<td>{_esc(item.get('reason') or item.get('code'))}</td>"
            "</tr>"
        )
    if not rej_rows:
        rej_rows.append("<tr><td colspan='4' class='muted'>没有被拒记录。</td></tr>")

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="3">
<title>小灵通通讯员 · {_esc(snap.get('owner') or snap.get('me'))}</title>
<style>
:root {{ color-scheme: light; }}
body {{ margin: 0; background: #f4f0e6; color: #1f1a14; font: 15px/1.45 "Noto Sans CJK SC", "Noto Sans SC", "PingFang SC", "Microsoft YaHei", sans-serif; }}
main {{ max-width: 1100px; margin: 0 auto; padding: 20px 16px 48px; }}
h1 {{ font-size: 28px; margin: 0 0 4px; letter-spacing: 0.02em; }}
h2 {{ font-size: 16px; margin: 28px 0 8px; }}
.sub {{ color: #5e564b; margin: 0 0 8px; }}
.note {{ background: #fff; border: 1px solid #e3d9c6; border-radius: 10px; padding: 10px 12px; color: #5e564b; }}
table {{ width: 100%; border-collapse: collapse; background: #fff; border-radius: 10px; overflow: hidden; }}
th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #efe6d6; vertical-align: top; }}
th {{ font-size: 12px; color: #6d6458; font-weight: 600; background: #faf7f1; }}
.muted {{ color: #6d6458; }}
.mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; word-break: break-all; }}
.badge {{ display: inline-block; padding: 1px 8px; border-radius: 999px; background: #eee6d8; white-space: nowrap; }}
.badge.online, .badge.replied, .badge.done {{ background: #d9f0e3; color: #14603a; }}
.badge.offline {{ background: #ece7df; color: #5c564c; }}
.badge.awaiting_manual, .badge.queued, .badge.pending, .badge.delivering {{ background: #f8e7c3; color: #7a5410; }}
.badge.failed, .badge.egress_blocked, .badge.unreachable {{ background: #f4d6d2; color: #8a2e26; }}
.cards {{ display: flex; gap: 10px; flex-wrap: wrap; margin-top: 12px; }}
.card {{ background: #fff; border-radius: 10px; padding: 10px 12px; min-width: 140px; border: 1px solid #e3d9c6; }}
.card b {{ display: block; font-size: 18px; }}
@media (max-width: 720px) {{
  h1 {{ font-size: 22px; }}
  table {{ display: block; overflow-x: auto; }}
  th, td {{ white-space: nowrap; }}
}}
</style>
</head>
<body>
<main>
  <h1>小灵通通讯员</h1>
  <p class="sub">{_esc(snap.get('owner'))} 的这台电脑（{_esc(snap.get('me'))}）· 只显示信封，不显示正文</p>
  <div class="cards">
    <div class="card"><b>{_esc(snap.get('person'))}</b><span class="muted">人名</span></div>
    <div class="card"><b>{_esc(len(snap['pending']))}</b><span class="muted">待发</span></div>
    <div class="card"><b>{_esc(sum(1 for p in snap['peers'] if p['state']=='online'))}</b><span class="muted">同事在线</span></div>
    <div class="card"><b>{_esc(snap.get('routes_default'))}</b><span class="muted">默认窗口</span></div>
  </div>
  <p class="note">{_esc(snap.get('wake_note') or '睡醒检测说明还没写上。')}</p>
  <h2>同事</h2>
  <table>
    <tr><th>谁</th><th>状态</th><th>地址</th><th>最后见到</th><th>待发</th><th>来源</th></tr>
    {''.join(peer_rows)}
  </table>
  <h2>待发箱</h2>
  <table>
    <tr><th>消息</th><th>发给</th><th>类型</th><th>标题</th><th>状态</th><th>尝试</th><th>大小</th></tr>
    {''.join(pend_rows)}
  </table>
  <h2>收信窗口</h2>
  <p class="sub">窗口失效时：{_esc(snap.get('on_window_lost'))}。同名再登记会接管，未送达的信重新排队。</p>
  <table>
    <tr><th>窗口</th><th>投递</th><th>状态</th><th>还没处理</th><th>第几次接管</th><th>说明</th></tr>
    {''.join(win_rows)}
  </table>
  <h2>收到的信</h2>
  <table>
    <tr><th>消息</th><th>来自</th><th>类型</th><th>标题</th><th>交给</th><th>状态</th><th>大小</th></tr>
    {''.join(in_rows)}
  </table>
  <h2>被拒记录</h2>
  <table>
    <tr><th>时间</th><th>谁</th><th>做什么</th><th>原因</th></tr>
    {''.join(rej_rows)}
  </table>
  <p class="muted">TCP {_esc(snap.get('tcp_port'))} · UDP {_esc(snap.get('udp_port'))} · 本页只在这台电脑上（127.0.0.1:{_esc(snap.get('panel_port'))}）。启动于 {_esc(snap.get('started') or '—')}</p>
</main>
</body>
</html>
"""
