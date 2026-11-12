"""本机状态页。只渲染信封，不读信纸。"""

from __future__ import annotations

import html
import json
from pathlib import Path

from xlt import certevents, receipts
from xlt.common import envelope_summary, read_json
from xlt.store import Home, load_cache, load_config, load_directory, load_routes, load_windows

LABELS = {
    "online": "在线",
    "offline": "离线",
    "unreachable": "在线但连不上",
    "queued": "排队",
    "pending": "待送达",
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
    "receipt_stored": "已送达",
    "receipt_read": "已读",
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


def _clock(epoch) -> str:
    """下次重试的钟点。写绝对时间，页面每 5 秒刷新时内容才不会天天在变。"""
    try:
        import time as _time

        return _time.strftime("%H:%M:%S", _time.localtime(float(epoch)))
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


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
        item["next_try_at"] = env.get("next_try_at")
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
        read, read_at, _via = receipts.read_info(home, path, env)
        item["read"] = read
        item["read_at"] = read_at
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


def _sent_rows(home: Home, all_pending: list[dict], limit: int = 40) -> list[dict]:
    """已发出的信：待送达 / 已送达 / 已读 / 失败 / 过期。信封信息来自 sent.jsonl，状态来自待发箱和回执表。"""
    boxes = {row.get("id"): row for row in all_pending}
    marks = receipts.all_items(home)
    rows = []
    for row in reversed(_jsonl_tail(home.root / "sent.jsonl", limit)):
        msg_id = row.get("id")
        item = {
            "id": msg_id,
            "to_member": row.get("to_member"),
            "type": row.get("type"),
            "subject": row.get("subject"),
            "created": row.get("created") or row.get("ts"),
            "detail": "",
        }
        waiting = boxes.get(msg_id)
        if waiting:
            item["status"] = "pending" if waiting["box"] == "pending" else waiting["box"]
            if waiting.get("attach_progress"):
                item["detail"] = waiting["attach_progress"]
                item["attach_bytes"] = waiting.get("attach_bytes")
        else:
            mark = marks.get(msg_id) or {}
            if mark.get("state") == "read":
                item["status"] = "receipt_read"
                item["detail"] = f"已读 {_short_time(mark.get('read_at'))}"
            else:
                item["status"] = "receipt_stored"
                if mark.get("delivered_at"):
                    item["detail"] = f"送达 {_short_time(mark.get('delivered_at'))}"
                if mark.get("no_receipts"):
                    item["detail"] += "（对方版本较旧，没有已读回执）" if item["detail"] else "对方版本较旧，没有已读回执"
        rows.append(item)
    return rows


def _short_time(value) -> str:
    text = str(value or "")
    return text[5:16].replace("T", " ") if len(text) >= 16 else text


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
        "sent": _sent_rows(home, pending),
        "cert_alerts": [
            dict(item, old_short=certevents.short_fp(item.get("old_fp")), new_short=certevents.short_fp(item.get("new_fp")))
            for item in certevents.active(home)
        ],
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
  --bg-selected: #e5f4ff;
  --bg-pressed: #e6e6e6;
  --text: #000000e5;
  --text-secondary: #00000080;
  --text-tertiary: #0000004d;
  --text-meta: #808080;
  --border: #e6e6e6;
  --border-strong: #2c2c2c;
  --brand-ink: #2c2c2c;
  --link: #0768cf;
  --outline-selected: #328dd5;
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
  --radius-row: 8px;
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
  --sidebar: 240px;
  --topbar: 48px;
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
a:focus-visible, button:focus-visible, tbody tr:focus-visible {
  outline: 2px solid var(--outline-selected);
  outline-offset: 2px;
  border-radius: var(--radius-row);
}
.app {
  display: flex;
  min-height: 100vh;
  align-items: stretch;
}
.sidebar {
  width: var(--sidebar);
  flex: 0 0 var(--sidebar);
  background: var(--bg);
  border-right: 1px solid var(--border);
  display: flex;
  flex-direction: column;
  padding: 0 var(--space-2) var(--space-3);
  position: sticky;
  top: 0;
  height: 100vh;
  overflow: auto;
}
.brand {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  height: var(--topbar);
  padding: 0 4px;
  flex: 0 0 var(--topbar);
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
nav.side-nav {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-top: var(--space-2);
}
nav.side-nav a {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  height: 28px;
  padding: 0 var(--space-2);
  border-radius: var(--radius-row);
  color: var(--text);
  font-size: 13px;
  line-height: 16px;
}
nav.side-nav a:hover { background: var(--bg-secondary); }
nav.side-nav a.active,
.app:not(:has(:target)) nav.side-nav a[href="#peers"],
.app:has(#peers:target) nav.side-nav a[href="#peers"],
.app:has(#pending:target) nav.side-nav a[href="#pending"],
.app:has(#sent:target) nav.side-nav a[href="#sent"],
.app:has(#windows:target) nav.side-nav a[href="#windows"],
.app:has(#inbox:target) nav.side-nav a[href="#inbox"],
.app:has(#rejects:target) nav.side-nav a[href="#rejects"] {
  background: var(--bg-selected);
  font-weight: var(--weight-strong);
}
.app:has(#pending:target) nav.side-nav a[href="#peers"],
.app:has(#sent:target) nav.side-nav a[href="#peers"],
.app:has(#windows:target) nav.side-nav a[href="#peers"],
.app:has(#inbox:target) nav.side-nav a[href="#peers"],
.app:has(#rejects:target) nav.side-nav a[href="#peers"] {
  background: transparent;
  font-weight: var(--weight);
}
nav.side-nav .count {
  color: var(--text-secondary);
  font-size: 11px;
  font-weight: var(--weight);
  font-variant-numeric: tabular-nums;
}
.sidebar-foot {
  margin-top: auto;
  padding: var(--space-3) var(--space-2) 0;
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
  display: flex;
  flex-direction: column;
}
.page-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  height: var(--topbar);
  padding: 0 var(--space-5);
  flex: 0 0 var(--topbar);
  background: var(--bg);
}
.page-head h1 {
  margin: 0;
  font-size: 15px;
  font-weight: var(--weight-strong);
  line-height: 25px;
  letter-spacing: -0.00469rem;
}
.page-head .sub {
  margin: 0;
  color: var(--text-secondary);
  font-size: 11px;
  line-height: 16px;
}
.page-title { min-width: 0; }
.stats {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2);
  justify-content: flex-end;
}
.stat {
  display: flex;
  align-items: baseline;
  gap: 6px;
  height: 32px;
  padding: 0 12px;
  border-radius: var(--radius-full);
  background: var(--bg-secondary);
}
.stat b {
  font-size: 13px;
  font-weight: var(--weight-strong);
  line-height: 16px;
  letter-spacing: 0;
}
.stat span {
  color: var(--text-secondary);
  font-size: 11px;
  line-height: 16px;
}
.tabs {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: var(--space-2) var(--space-5);
  flex-wrap: wrap;
}
.tabs a {
  display: inline-flex;
  align-items: center;
  height: 24px;
  padding: 0 12px;
  border-radius: var(--radius-full);
  font-size: 13px;
  line-height: 16px;
  color: var(--text);
}
.tabs a:hover { color: var(--text); }
.workspace:not(:has(:target)) .tabs a[href="#peers"],
.workspace:has(#peers:target) .tabs a[href="#peers"],
.workspace:has(#pending:target) .tabs a[href="#pending"],
.workspace:has(#sent:target) .tabs a[href="#sent"],
.workspace:has(#windows:target) .tabs a[href="#windows"],
.workspace:has(#inbox:target) .tabs a[href="#inbox"],
.workspace:has(#rejects:target) .tabs a[href="#rejects"] {
  background: var(--bg-secondary);
  font-weight: var(--weight-strong);
}
.workspace:has(#pending:target) .tabs a[href="#peers"],
.workspace:has(#sent:target) .tabs a[href="#peers"],
.workspace:has(#windows:target) .tabs a[href="#peers"],
.workspace:has(#inbox:target) .tabs a[href="#peers"],
.workspace:has(#rejects:target) .tabs a[href="#peers"] {
  background: transparent;
  font-weight: var(--weight);
}
.page-body {
  padding: 0 var(--space-5) var(--space-5);
}
section {
  margin-top: var(--space-4);
}
section:first-of-type { margin-top: var(--space-2); }
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
  background: var(--bg);
}
table {
  width: 100%;
  border-collapse: collapse;
}
th, td {
  text-align: left;
  vertical-align: middle;
  padding: 0 12px;
  border-bottom: 0;
}
th {
  height: 32px;
  font-size: 11px;
  font-weight: var(--weight-strong);
  line-height: 16px;
  color: var(--text-meta);
  background: var(--bg);
  white-space: nowrap;
}
td { height: 48px; font-size: 13px; line-height: 16px; }
tbody tr { cursor: default; }
tbody tr:hover td { background: transparent; }
tbody tr:focus {
  outline: 2px solid var(--outline-selected);
  outline-offset: -4px;
  border-radius: var(--radius-row);
}
tbody tr:focus td { background: var(--bg); }
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
.badge.online, .badge.replied, .badge.done, .badge.accepted, .badge.receipt_read { background: var(--success-bg); color: var(--success-fg); }
.badge.receipt_stored { background: var(--bg-selected); color: var(--link); }
.badge.offline, .badge.no_reply { background: var(--bg-secondary); color: var(--neutral-fg); }
.badge.awaiting_manual, .badge.queued, .badge.pending, .badge.delivering, .badge.receiving { background: var(--warning-bg); color: var(--warning-fg); }
.badge.failed, .badge.egress_blocked, .badge.unreachable, .badge.declined, .badge.expired { background: var(--danger-bg); color: var(--danger-fg); }
.progress {
  width: 160px;
  height: 6px;
  margin-top: 6px;
  border-radius: var(--radius-full);
  background: var(--bg-pressed);
  overflow: hidden;
}
.progress > span {
  display: block;
  height: 100%;
  border-radius: var(--radius-full);
  background: var(--outline-selected);
}
.warn {
  background: var(--warning-bg);
  color: var(--warning-fg);
  font-size: 11px;
  line-height: 16px;
}
tr.warn-row td { background: var(--warning-bg); height: auto; padding: 8px 12px; }
tr.warn-row:hover td { background: var(--warning-bg); }
tr.warn-row:focus { outline-color: var(--warning-fg); }
.foot {
  margin-top: var(--space-4);
  color: var(--text-tertiary);
  font-size: 11px;
  line-height: 16px;
}
.live-stamp {
  color: var(--text-tertiary);
  font-size: 11px;
  line-height: 16px;
  font-weight: var(--weight);
  margin-left: 8px;
  white-space: nowrap;
}
.alert-stack { display: flex; flex-direction: column; }
.alert-bar {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: 8px var(--space-5);
  background: var(--danger-bg);
  color: var(--danger-fg);
  border-bottom: 1px solid #f5b8b0;
  font-size: 13px;
  line-height: 20px;
}
.alert-bar .alert-body { flex: 1 1 auto; min-width: 0; }
.alert-bar b { font-weight: var(--weight-strong); }
.alert-bar .alert-sub { font-size: 11px; line-height: 16px; margin-top: 2px; color: #7a1c0f; }
.alert-bar .fp { font-family: var(--font-mono); font-size: 11px; }
.alert-bar button {
  flex: 0 0 auto;
  height: 28px;
  padding: 0 12px;
  border: 1px solid #e3a49b;
  border-radius: var(--radius-full);
  background: #fff;
  color: var(--danger-fg);
  font: inherit;
  font-size: 12px;
  font-weight: var(--weight-strong);
  cursor: pointer;
}
.alert-bar button:hover { background: #fff5f4; }
@media (max-width: 860px) {
  .app { display: block; }
  .sidebar {
    width: auto;
    flex: none;
    height: auto;
    position: static;
    border-right: 0;
    border-bottom: 1px solid var(--border);
    padding: var(--space-2);
  }
  .brand { height: auto; padding: var(--space-2) 4px; flex: none; }
  nav.side-nav { flex-direction: row; flex-wrap: wrap; }
  .sidebar-foot { margin-top: var(--space-3); }
  .page-head {
    height: auto;
    flex-direction: column;
    align-items: flex-start;
    padding: var(--space-3) var(--space-3) var(--space-2);
    flex: none;
  }
  .tabs, .page-body { padding-left: var(--space-3); padding-right: var(--space-3); }
  table { min-width: 640px; }
}
"""



def _nav_link(anchor: str, title: str, count: str | None = None, active: bool = False) -> str:
    badge = f'<span class="count">{_esc(count)}</span>' if count is not None else ""
    cls = ' class="active"' if active else ""
    return f'<a href="#{_esc(anchor)}"{cls}><span>{_esc(title)}</span>{badge}</a>'


def _progress_bar(item: dict) -> str:
    """附件传输进度条。没有附件的信不画。"""
    info = item.get("attach_bytes")
    if not info or not info.get("total"):
        return ""
    pct = int(info.get("pct") or 0)
    text = _esc(item.get("attach_progress") or "")
    return (
        f'<div class="progress" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="{pct}">'
        f'<span style="width:{pct}%"></span></div>'
        f'<div class="tiny">{text}（{pct}%）</div>'
    )


def _retry_note(item: dict) -> str:
    bits = []
    if item.get("last_error"):
        bits.append(_esc(item["last_error"]))
    when = _clock(item.get("next_try_at")) if item.get("next_try_at") else ""
    if when:
        bits.append(f"{when} 再试")
    if not bits:
        return ""
    return "<div class='tiny'>" + " · ".join(bits) + "</div>"


def _tr(key: str, css: str = "") -> str:
    cls = f" class='{css}'" if css else ""
    return f"<tr{cls} tabindex='-1' data-row='{_esc(key)}'>"


LIVE_JS = """(function () {
  var INTERVAL = 5000;
  var lastOk = Date.now();
  var busy = false;
  window.__xltLiveMarker = window.__xltLiveMarker || Date.now();

  function selecting() {
    if (document.hidden) return true;
    var sel = window.getSelection();
    return !!(sel && !sel.isCollapsed && String(sel));
  }

  function snapshot() {
    var active = document.activeElement;
    var row = active && active.closest ? active.closest("tbody tr") : null;
    var side = document.querySelector(".sidebar");
    return {
      x: window.scrollX,
      y: window.scrollY,
      side: side ? side.scrollTop : 0,
      key: row ? row.getAttribute("data-row") : null,
      hash: location.hash
    };
  }

  function restore(s) {
    if (s.hash && location.hash !== s.hash) {
      location.hash = s.hash;
    }
    if (s.key) {
      var rows = document.querySelectorAll("tbody tr[data-row]");
      for (var i = 0; i < rows.length; i++) {
        if (rows[i].getAttribute("data-row") === s.key) {
          if (rows[i].focus) rows[i].focus({preventScroll: true});
          break;
        }
      }
    }
    var side = document.querySelector(".sidebar");
    if (side) side.scrollTop = s.side;
    window.scrollTo(s.x, s.y);
  }

  function markup(root) {
    var clone = root.cloneNode(true);
    var stamp = clone.querySelector("#live-stamp");
    if (stamp) stamp.textContent = "";
    return clone.innerHTML;
  }

  function paintStamp() {
    var el = document.getElementById("live-stamp");
    if (!el) return;
    var sec = Math.max(0, Math.floor((Date.now() - lastOk) / 1000));
    el.textContent = sec <= 0 ? "刚刚更新" : (sec + " 秒前更新");
  }

  function tick(force) {
    if (busy || (!force && selecting())) return;
    busy = true;
    var url = location.pathname || "/";
    fetch(url, {cache: "no-store", headers: {Accept: "text/html"}})
      .then(function (res) {
        if (!res.ok) throw new Error(String(res.status));
        return res.text();
      })
      .then(function (text) {
        if (!force && selecting()) return;
        var doc = new DOMParser().parseFromString(text, "text/html");
        var next = doc.querySelector(".app");
        var cur = document.querySelector(".app");
        if (!next || !cur) return;
        lastOk = Date.now();
        if (markup(cur) === markup(next)) {
          paintStamp();
          return;
        }
        var s = snapshot();
        cur.replaceWith(next);
        if (doc.title) document.title = doc.title;
        restore(s);
        paintStamp();
      })
      .catch(function () {})
      .then(function () { busy = false; });
  }

  document.addEventListener("click", function (ev) {
    var btn = ev.target && ev.target.closest ? ev.target.closest("[data-dismiss]") : null;
    if (!btn) return;
    btn.disabled = true;
    fetch("/local/cert-dismiss", {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-Xlt-Panel": "1"},
      body: JSON.stringify({id: btn.getAttribute("data-dismiss")})
    }).then(function () { tick(true); }).catch(function () { btn.disabled = false; });
  });

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) tick(false);
  });
  setInterval(function () { tick(false); }, INTERVAL);
  setInterval(paintStamp, 1000);
  paintStamp();
})();
"""


def render(snap: dict) -> str:
    peer_rows = []
    for peer in snap["peers"]:
        peer_rows.append(
            f"{_tr('peer:' + str(peer.get('name') or peer.get('owner')))}"
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
                f"{_tr('peer-warn:' + str(peer.get('name') or peer.get('owner')), 'warn-row')}<td colspan='6' class='warn'>{_esc(peer['cert_warning'])}</td></tr>"
            )
    if not peer_rows:
        peer_rows.append(
            f"{_tr('empty-peers')}<td colspan='6' class='muted'>成员名单里还没有其他同事。开着网关等广播即可，也可以用 xlt trust 把他们的证书加进来。</td></tr>"
        )

    pend_rows = []
    for item in snap["pending"]:
        pend_rows.append(
            f"{_tr('pend:' + str(item.get('id') or ''))}"
            f"<td class='mono'>{_esc(item.get('id'))}</td>"
            f"<td>{_esc(item.get('to_member') or item.get('to'))}</td>"
            f"<td>{_esc(item.get('type'))}</td>"
            f"<td>{_esc(item.get('subject') or '—')}</td>"
            f"<td>{_badge((item.get('state') or {}).get('status') or 'pending')}"
            f"{_retry_note(item)}{_progress_bar(item)}</td>"
            f"<td>{_esc(item.get('attempts'))}</td>"
            f"<td>{_esc(item.get('letter_bytes'))} 字节</td>"
            "</tr>"
        )
    if not pend_rows:
        pend_rows.append(f"{_tr('empty-pending')}<td colspan='7' class='muted'>没有待送达的信。</td></tr>")

    sent_rows = []
    for item in snap.get("sent") or []:
        sent_rows.append(
            f"{_tr('sent:' + str(item.get('id') or ''))}"
            f"<td class='mono'>{_esc(item.get('id'))}</td>"
            f"<td>{_esc(item.get('to_member'))}</td>"
            f"<td>{_esc(item.get('type'))}</td>"
            f"<td>{_esc(item.get('subject') or '—')}</td>"
            f"<td>{_badge(item.get('status'))}"
            f"{('<div class=tiny>' + _esc(item['detail']) + '</div>') if item.get('detail') and not item.get('attach_bytes') else ''}"
            f"{_progress_bar(dict(item, attach_progress=item.get('detail'))) if item.get('attach_bytes') else ''}</td>"
            f"<td class='tiny'>{_esc(_short_time(item.get('created')))}</td>"
            "</tr>"
        )
    if not sent_rows:
        sent_rows.append(f"{_tr('empty-sent')}<td colspan='6' class='muted'>还没有发出过信。</td></tr>")

    in_rows = []
    for item in snap["inbox"]:
        state = (item.get("state") or {}).get("status")
        in_rows.append(
            f"{_tr('inbox:' + str(item.get('id') or ''))}"
            f"<td class='mono'>{_esc(item.get('id'))}</td>"
            f"<td>{_esc(item.get('from_person'))}</td>"
            f"<td>{_esc(item.get('type'))}</td>"
            f"<td>{_esc(item.get('subject') or '—')}</td>"
            f"<td>{_esc((item.get('state') or {}).get('routed_to') or item.get('folder'))}</td>"
            f"<td>{_badge(state)}"
            f"{('<div class=tiny>已读 ' + _esc(_short_time(item.get('read_at'))) + '</div>') if item.get('read') else '<div class=tiny>还没读</div>'}"
            f"{_progress_bar(item) if (item.get('state') or {}).get('status') == 'receiving' else ''}</td>"
            f"<td>{_esc(item.get('letter_bytes'))} 字节</td>"
            "</tr>"
        )
    if not in_rows:
        in_rows.append(f"{_tr('empty-inbox')}<td colspan='7' class='muted'>还没有收到信。</td></tr>")

    win_rows = []
    for window in snap["windows"]:
        win_rows.append(
            f"{_tr('win:' + str(window.get('ref') or ''))}"
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
            f"{_tr('empty-windows')}<td colspan='6' class='muted'>还没有登记收信窗口。xlt register 之后，信才知道交给谁。</td></tr>"
        )

    rej_rows = []
    for item in reversed(snap["rejects"]):
        rej_rows.append(
            f"{_tr('rej:' + '|'.join(str(item.get(k) or '') for k in ('ts', 'caller', 'from', 'action', 'type')))}"
            f"<td class='tiny'>{_esc(item.get('ts'))}</td>"
            f"<td>{_esc(item.get('caller') or item.get('from'))}</td>"
            f"<td>{_esc(item.get('action') or item.get('type'))}</td>"
            f"<td>{_esc(item.get('reason') or item.get('code'))}</td>"
            "</tr>"
        )
    if not rej_rows:
        rej_rows.append(f"{_tr('empty-rejects')}<td colspan='4' class='muted'>没有被拒记录。</td></tr>")

    alert_bars = []
    for item in (snap.get("cert_alerts") or [])[:5]:
        alert_bars.append(
            f"<div class=\"alert-bar\" role=\"alert\" data-alert=\"{_esc(item.get('id'))}\">"
            f"<div class=\"alert-body\"><b>证书被替换：{_esc(item.get('owner') or item.get('name'))}（{_esc(item.get('name'))}）</b>"
            f" 在 {_esc(_short_time(item.get('ts')))} 换了证书（来自 {_esc(item.get('ip') or '—')}）。"
            f"<div class=\"alert-sub\">旧指纹 <span class=\"fp\">{_esc(item.get('old_short'))}</span> → "
            f"新指纹 <span class=\"fp\">{_esc(item.get('new_short'))}</span>。"
            "如果对方没有重装过小灵通或换过电脑，请当面确认，可能有人冒充。</div></div>"
            f"<button type=\"button\" data-dismiss=\"{_esc(item.get('id'))}\">知道了</button></div>"
        )
    extra_alerts = len(snap.get("cert_alerts") or []) - 5
    if extra_alerts > 0:
        alert_bars.append(
            f"<div class=\"alert-bar\" role=\"alert\"><div class=\"alert-body\">还有 {extra_alerts} 条证书替换提醒。</div>"
            "<button type=\"button\" data-dismiss=\"all\">全部知道了</button></div>"
        )
    alert_html = f'<div class="alert-stack">{"".join(alert_bars)}</div>' if alert_bars else ""
    online = sum(1 for p in snap["peers"] if p["state"] == "online")
    owner = snap.get("owner") or snap.get("me")
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
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
        <div class="tiny">{_esc(snap.get('owner'))} · {_esc(snap.get('person'))} · {_esc(snap.get('me'))}</div>
      </div>
    </div>
    <nav class="side-nav">
      {_nav_link('peers', '同事', str(online))}
      {_nav_link('pending', '待送达', str(len(snap['pending'])))}
      {_nav_link('sent', '已发出', str(len(snap.get('sent') or [])))}
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
    {alert_html}
    <header class="page-head">
      <div class="page-title">
        <h1>小灵通通讯员</h1>
        <p class="sub">{_esc(snap.get('owner'))} 的这台电脑（{_esc(snap.get('me'))}）· 只显示信封，不显示正文 <span id="live-stamp" class="live-stamp"></span></p>
      </div>
      <div class="stats">
        <div class="stat"><b>{_esc(snap.get('person'))}</b><span>人名</span></div>
        <div class="stat"><b>{_esc(len(snap['pending']))}</b><span>待送达</span></div>
        <div class="stat"><b>{_esc(online)}</b><span>同事在线</span></div>
        <div class="stat"><b>{_esc(snap.get('routes_default'))}</b><span>默认窗口</span></div>
      </div>
    </header>
    <nav class="tabs" aria-label="页面分区">
      {_nav_link('peers', '同事', None)}
      {_nav_link('pending', '待送达', None)}
      {_nav_link('sent', '已发出', None)}
      {_nav_link('windows', '收信窗口', None)}
      {_nav_link('inbox', '收到的信', None)}
      {_nav_link('rejects', '被拒记录', None)}
    </nav>
    <div class="page-body">
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
      <h2>待送达</h2>
      <p class="hint">对方离线或网关没开时，信先存在这里，对方一上线就自动补发，不用重发。</p>
      <div class="table-wrap">
        <table>
          <thead><tr><th>消息</th><th>发给</th><th>类型</th><th>标题</th><th>状态</th><th>已试</th><th>大小</th></tr></thead>
          <tbody>{''.join(pend_rows)}</tbody>
        </table>
      </div>
    </section>
    <section id="sent">
      <h2>已发出</h2>
      <p class="hint">已送达 = 对方电脑上的通讯员收下了；已读 = 对方的 AI 真的取走看了。对方是旧版本时没有已读回执，会一直停在已送达。</p>
      <div class="table-wrap">
        <table>
          <thead><tr><th>消息</th><th>发给</th><th>类型</th><th>标题</th><th>状态</th><th>发出时间</th></tr></thead>
          <tbody>{''.join(sent_rows)}</tbody>
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
    </div>
  </main>
</div>
<script>
{LIVE_JS}
</script>
</body>
</html>
"""
