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


CSS = """
:root {
  --bg: #ffffff;
  --bg-secondary: #f5f5f5;
  --bg-hover: #f5f5f5;
  --bg-selected: #e6e6e6;
  --bg-pressed: #e6e6e6;
  --text: #000000e5;
  --text-secondary: #00000080;
  --text-tertiary: #0000004d;
  --border: #e6e6e6;
  --border-strong: #2c2c2c;
  --brand-ink: #2c2c2c;
  --link: #0768cf;
  --success-bg: #e0f6e5;
  --success-fg: #008043;
  --warning-bg: #fff1c2;
  --warning-fg: #b86200;
  --danger-bg: #ffe2e0;
  --danger-fg: #bd2915;
  --neutral-bg: #f5f5f5;
  --neutral-fg: #444444;
  --radius-s: 2px;
  --radius-m: 5px;
  --radius-l: 13px;
  --radius-full: 9999px;
  --space-1: 4px;
  --space-2: 8px;
  --space-2-5: 12px;
  --space-3: 16px;
  --space-4: 24px;
  --space-5: 32px;
  --font: Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", "Roboto", "Helvetica Neue", "Noto Sans SC", "PingFang SC", "Microsoft YaHei", "Noto Sans", sans-serif;
  --font-mono: "Roboto Mono", ui-monospace, "SFMono-Regular", Menlo, Consolas, "Courier New", monospace;
  --weight: 450;
  --weight-strong: 550;
  --shadow-100: 0 1px 3px #00000026, 0 0 0.5px #0000004d;
}
*, *::before, *::after { box-sizing: border-box; }
html, body { margin: 0; padding: 0; height: 100%; }
html { color-scheme: light; }
body {
  background: var(--bg);
  color: var(--text);
  font-family: var(--font);
  font-size: 13px;
  font-weight: var(--weight);
  line-height: 22px;
  letter-spacing: -0.002rem;
  -webkit-font-smoothing: antialiased;
}
a { color: inherit; text-decoration: none; }
a:focus-visible, button:focus-visible {
  outline: 2px solid var(--brand-ink);
  outline-offset: 2px;
}
.app {
  display: flex;
  min-height: 100vh;
  align-items: stretch;
}
.sidebar {
  width: 240px;
  flex: 0 0 240px;
  background: var(--bg-secondary);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  padding: var(--space-3) var(--space-2) var(--space-3);
  position: sticky;
  top: 0;
  height: 100vh;
  overflow: auto;
}
.brand {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-1) var(--space-2) var(--space-3);
}
.mark {
  width: 24px;
  height: 24px;
  border-radius: var(--radius-m);
  background: var(--brand-ink);
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  font-weight: var(--weight-strong);
  letter-spacing: 0;
  flex: 0 0 24px;
}
.brand-text { min-width: 0; }
.product {
  font-size: 13px;
  font-weight: var(--weight-strong);
  line-height: 16px;
}
.who {
  padding: var(--space-1) var(--space-2) var(--space-3);
  border-bottom: 1px solid var(--border);
  margin-bottom: var(--space-2);
}
.who-name {
  font-weight: var(--weight-strong);
  line-height: 16px;
}
nav {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
nav a {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  min-height: 32px;
  padding: 0 var(--space-2);
  border-radius: var(--radius-m);
  color: var(--text);
  font-size: 13px;
  line-height: 16px;
}
nav a:hover { background: #ececec; }
nav a.active, nav a:target { background: var(--bg-selected); font-weight: var(--weight-strong); }
nav .count {
  color: var(--text-secondary);
  font-size: 11px;
  font-weight: var(--weight);
  font-variant-numeric: tabular-nums;
}
.sidebar-foot {
  margin-top: auto;
  padding: var(--space-3) var(--space-2) 0;
  border-top: 1px solid var(--border);
  font-size: 11px;
  line-height: 16px;
  color: var(--text-secondary);
}
.sidebar-foot .wake {
  color: var(--text);
  margin-bottom: var(--space-2);
}
main.workspace {
  flex: 1 1 auto;
  min-width: 0;
  padding: var(--space-3) var(--space-4) var(--space-5);
}
.page-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-3);
  margin-bottom: var(--space-3);
}
.page-head h1 {
  margin: 0;
  font-size: 24px;
  font-weight: var(--weight-strong);
  line-height: 32px;
  letter-spacing: -0.0255rem;
}
.page-head .sub {
  margin: var(--space-1) 0 0;
  color: var(--text-secondary);
  font-size: 13px;
  line-height: 22px;
}
.stats {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
}
.stat {
  min-width: 112px;
  padding: var(--space-2) var(--space-2-5);
  border: 1px solid var(--border);
  border-radius: var(--radius-m);
  background: var(--bg);
}
.stat b {
  display: block;
  font-size: 15px;
  font-weight: var(--weight-strong);
  line-height: 25px;
  letter-spacing: -0.00469rem;
}
.stat span {
  display: block;
  color: var(--text-secondary);
  font-size: 11px;
  line-height: 16px;
}
section {
  margin-top: var(--space-4);
}
section > h2 {
  margin: 0 0 var(--space-2);
  font-size: 13px;
  font-weight: var(--weight-strong);
  line-height: 22px;
  letter-spacing: -0.002rem;
}
section .hint {
  margin: calc(var(--space-2) * -1) 0 var(--space-2);
  color: var(--text-secondary);
  font-size: 11px;
  line-height: 16px;
}
.table-wrap {
  border: 1px solid var(--border);
  border-radius: var(--radius-m);
  overflow: auto;
  background: var(--bg);
}
table {
  width: 100%;
  border-collapse: collapse;
}
th, td {
  text-align: left;
  vertical-align: top;
  padding: 8px 12px;
  border-bottom: 1px solid var(--border);
}
th {
  font-size: 11px;
  font-weight: var(--weight-strong);
  line-height: 16px;
  color: var(--text-secondary);
  background: var(--bg);
  white-space: nowrap;
}
tr:last-child td { border-bottom: 0; }
tbody tr:hover td { background: var(--bg-hover); }
td { font-size: 13px; line-height: 16px; }
.muted { color: var(--text-secondary); }
.tiny {
  color: var(--text-secondary);
  font-size: 11px;
  line-height: 16px;
  margin-top: 2px;
}
.mono {
  font-family: var(--font-mono);
  font-size: 11px;
  line-height: 16px;
  font-weight: 400;
  word-break: break-all;
}
.badge {
  display: inline-block;
  padding: 1px 8px;
  border-radius: var(--radius-full);
  background: var(--neutral-bg);
  color: var(--neutral-fg);
  font-size: 11px;
  font-weight: var(--weight-strong);
  line-height: 16px;
  white-space: nowrap;
}
.badge.online, .badge.replied, .badge.done, .badge.accepted { background: var(--success-bg); color: var(--success-fg); }
.badge.offline, .badge.no_reply { background: var(--bg-selected); color: var(--neutral-fg); }
.badge.awaiting_manual, .badge.queued, .badge.pending, .badge.delivering, .badge.receiving { background: var(--warning-bg); color: var(--warning-fg); }
.badge.failed, .badge.egress_blocked, .badge.unreachable, .badge.declined, .badge.expired { background: var(--danger-bg); color: var(--danger-fg); }
.warn {
  background: var(--warning-bg);
  color: var(--warning-fg);
  font-size: 11px;
  line-height: 16px;
}
tr.warn-row td { background: var(--warning-bg); }
tr.warn-row:hover td { background: #ffe8a3; }
.foot {
  margin-top: var(--space-4);
  color: var(--text-tertiary);
  font-size: 11px;
  line-height: 16px;
}
@media (max-width: 860px) {
  .app { display: block; }
  .sidebar {
    width: auto;
    flex: none;
    height: auto;
    position: static;
    border-right: 0;
    border-bottom: 1px solid var(--border);
  }
  nav { flex-direction: row; flex-wrap: wrap; }
  .sidebar-foot { margin-top: var(--space-3); }
  main.workspace { padding: var(--space-3); }
  .page-head { flex-direction: column; }
  table { min-width: 640px; }
}
"""


def _nav_link(anchor: str, title: str, count: str | None = None, active: bool = False) -> str:
    badge = f'<span class="count">{_esc(count)}</span>' if count is not None else ""
    cls = ' class="active"' if active else ""
    return f'<a href="#{_esc(anchor)}"{cls}><span>{_esc(title)}</span>{badge}</a>'


def render(snap: dict) -> str:
    peer_rows = []
    for peer in snap["peers"]:
        peer_rows.append(
            "<tr>"
            f"<td>{_esc(peer['owner'])}<div class='tiny'>{_esc(peer['person'])} · {_esc(peer['name'])}</div></td>"
            f"<td>{_badge(peer['state'])}</td>"
            f"<td>{_esc(peer['ip'] or '—')}</td>"
            f"<td class='tiny'>{_esc(peer['last_seen'] or '—')}</td>"
            f"<td>{_esc(peer['pending'])}</td>"
            f"<td class='tiny'>{_esc(peer['source'])}</td>"
            "</tr>"
        )
        if peer.get("cert_warning"):
            peer_rows.append(
                f"<tr class='warn-row'><td colspan='6' class='warn'>{_esc(peer['cert_warning'])}</td></tr>"
            )
    if not peer_rows:
        peer_rows.append(
            "<tr><td colspan='6' class='muted'>成员名单里还没有其他同事。开着网关等广播即可，也可以用 xlt trust 把他们的证书加进来。</td></tr>"
        )

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
        win_rows.append(
            "<tr><td colspan='6' class='muted'>还没有登记收信窗口。xlt register 之后，信才知道交给谁。</td></tr>"
        )

    rej_rows = []
    for item in reversed(snap["rejects"]):
        rej_rows.append(
            "<tr>"
            f"<td class='tiny'>{_esc(item.get('ts'))}</td>"
            f"<td>{_esc(item.get('caller') or item.get('from'))}</td>"
            f"<td>{_esc(item.get('action') or item.get('type'))}</td>"
            f"<td>{_esc(item.get('reason') or item.get('code'))}</td>"
            "</tr>"
        )
    if not rej_rows:
        rej_rows.append("<tr><td colspan='4' class='muted'>没有被拒记录。</td></tr>")

    online = sum(1 for p in snap["peers"] if p["state"] == "online")
    owner = snap.get("owner") or snap.get("me")
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="3">
<title>小灵通通讯员 · {_esc(owner)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Inter:wght@400;450;500;550;600&amp;family=Roboto+Mono:wght@400;500&amp;display=swap">
<style>
{CSS}
</style>
</head>
<body>
<div class="app">
  <aside class="sidebar">
    <div class="brand">
      <div class="mark" aria-hidden="true">灵</div>
      <div class="brand-text">
        <div class="product">小灵通</div>
        <div class="tiny">通讯员</div>
      </div>
    </div>
    <div class="who">
      <div class="who-name">{_esc(snap.get('owner'))}</div>
      <div class="tiny">{_esc(snap.get('person'))} · {_esc(snap.get('me'))}</div>
    </div>
    <nav>
      {_nav_link('peers', '同事', str(online))}
      {_nav_link('pending', '待发箱', str(len(snap['pending'])))}
      {_nav_link('windows', '收信窗口', str(len(snap['windows'])))}
      {_nav_link('inbox', '收到的信', str(len(snap['inbox'])))}
      {_nav_link('rejects', '被拒记录', str(len(snap['rejects'])))}
    </nav>
    <div class="sidebar-foot">
      <div class="wake">{_esc(snap.get('wake_note') or '睡醒检测说明还没写上。')}</div>
      <div>TCP {_esc(snap.get('tcp_port'))} · UDP {_esc(snap.get('udp_port'))}</div>
      <div>本页只在这台电脑上</div>
      <div class="mono">127.0.0.1:{_esc(snap.get('panel_port'))}</div>
    </div>
  </aside>
  <main class="workspace">
    <header class="page-head">
      <div>
        <h1>小灵通通讯员</h1>
        <p class="sub">{_esc(snap.get('owner'))} 的这台电脑（{_esc(snap.get('me'))}）· 只显示信封，不显示正文</p>
      </div>
      <div class="stats">
        <div class="stat"><b>{_esc(snap.get('person'))}</b><span>人名</span></div>
        <div class="stat"><b>{_esc(len(snap['pending']))}</b><span>待发</span></div>
        <div class="stat"><b>{_esc(online)}</b><span>同事在线</span></div>
        <div class="stat"><b>{_esc(snap.get('routes_default'))}</b><span>默认窗口</span></div>
      </div>
    </header>
    <section id="peers">
      <h2>同事</h2>
      <div class="table-wrap">
        <table>
          <thead><tr><th>谁</th><th>状态</th><th>地址</th><th>最后见到</th><th>待发</th><th>来源</th></tr></thead>
          <tbody>{''.join(peer_rows)}</tbody>
        </table>
      </div>
    </section>
    <section id="pending">
      <h2>待发箱</h2>
      <div class="table-wrap">
        <table>
          <thead><tr><th>消息</th><th>发给</th><th>类型</th><th>标题</th><th>状态</th><th>尝试</th><th>大小</th></tr></thead>
          <tbody>{''.join(pend_rows)}</tbody>
        </table>
      </div>
    </section>
    <section id="windows">
      <h2>收信窗口</h2>
      <p class="hint">窗口失效时：{_esc(snap.get('on_window_lost'))}。同名再登记会接管，未送达的信重新排队。</p>
      <div class="table-wrap">
        <table>
          <thead><tr><th>窗口</th><th>投递</th><th>状态</th><th>还没处理</th><th>第几次接管</th><th>说明</th></tr></thead>
          <tbody>{''.join(win_rows)}</tbody>
        </table>
      </div>
    </section>
    <section id="inbox">
      <h2>收到的信</h2>
      <div class="table-wrap">
        <table>
          <thead><tr><th>消息</th><th>来自</th><th>类型</th><th>标题</th><th>交给</th><th>状态</th><th>大小</th></tr></thead>
          <tbody>{''.join(in_rows)}</tbody>
        </table>
      </div>
    </section>
    <section id="rejects">
      <h2>被拒记录</h2>
      <div class="table-wrap">
        <table>
          <thead><tr><th>时间</th><th>谁</th><th>做什么</th><th>原因</th></tr></thead>
          <tbody>{''.join(rej_rows)}</tbody>
        </table>
      </div>
    </section>
    <p class="foot">TCP {_esc(snap.get('tcp_port'))} · UDP {_esc(snap.get('udp_port'))} · 本页只在这台电脑上（127.0.0.1:{_esc(snap.get('panel_port'))}）。启动于 {_esc(snap.get('started') or '—')}</p>
  </main>
</div>
</body>
</html>
"""
