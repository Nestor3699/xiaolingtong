#!/usr/bin/env python3
"""状态页「旧页面 + 新通讯员」：浏览器里开了很久的页面，在通讯员升级之后继续自动刷新，样式不能乱。

背景：状态页每 5 秒只把 .app 里的内容换成新的，样式表在 <head> 里，不会跟着换。
通讯员升级后，开着的旧页面拿到新内容，却还是旧样式，于是图标和文字错位、TCP/UDP 连成一串。
这里用真的通讯员、真的浏览器验证：

1. 旧版本（9d45baf，发给同事的第一版）的页面开着，通讯员换成当前版本：旧页面（旧脚本）也要拿到新样式
2. 当前版本的页面开着，通讯员换成「样式变了」的下一版：页面发现版本变了，自己整页刷新一次
3. 样式表完全不生效时，关键布局（图标大小、本机块的两列、TCP/UDP 分行）靠元素自己带的内联样式也不乱
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from browser import Browser  # noqa: E402
from harness import REPO, Fail, Node, fresh, old_release, say, wait_for  # noqa: E402

WORK = Path(__file__).resolve().parent / "work-panel-stale"

CHECK = """(() => {
  const cs = (sel) => { const el = document.querySelector(sel); return el ? getComputedStyle(el) : null; };
  const nav = cs('.nav-label'), kv = cs('.kv-row'), dd = cs('.kv-row dd'), stack = cs('.stack');
  const btn = document.querySelector('.icon-btn');
  const ic = document.querySelector('.nav-label .ic');
  const summary = cs('.foot-note summary');
  const ports = [...document.querySelectorAll('.kv-row')].find(r => r.textContent.includes('UDP'));
  const spans = ports ? [...ports.querySelectorAll('dd span')] : [];
  const tops = spans.map(s => Math.round(s.getBoundingClientRect().top));
  return {
    nav: nav && nav.display, kv: kv && kv.display, stack: stack && stack.display,
    summary: summary && summary.display,
    icon: ic ? [ic.getBoundingClientRect().width, ic.getBoundingClientRect().height] : null,
    btn: btn ? [btn.getBoundingClientRect().width, btn.getBoundingClientRect().height] : null,
    portLines: tops.length >= 2 && tops[0] !== tops[1],
    kvCols: kv && kv.gridTemplateColumns.split(' ').length,
    build: (document.querySelector('meta[name="xlt-build"]') || {}).content || '',
    marker: window.__marker || 0,
  };
})()"""


def good(info: dict, label: str) -> None:
    problems = []
    if info["nav"] not in ("inline-flex", "flex"):
        problems.append(f"导航里图标和文字没有排成一行（display={info['nav']}）")
    if info["kv"] != "grid" or info["kvCols"] != 2:
        problems.append(f"本机块没有「标签列 + 值」两列（display={info['kv']}，列数={info['kvCols']}）")
    if info["stack"] != "flex":
        problems.append("TCP/UDP 或网关两行没有分行")
    if not info["portLines"]:
        problems.append("TCP 47800 和 UDP 47801 连在了同一行")
    if info["summary"] not in ("flex", "block"):
        problems.append(f"睡醒检测的小三角样式不对（display={info['summary']}）")
    if not info["icon"] or max(info["icon"]) > 20:
        problems.append(f"图标尺寸不对：{info['icon']}")
    if info["btn"] and max(info["btn"]) > 26:
        problems.append(f"复制按钮过大：{info['btn']}")
    if problems:
        raise Fail(f"{label}：" + "；".join(problems))


def main() -> int:
    fresh(WORK)
    old = old_release(WORK, "9d45baf")
    # 「下一版」：样式表多一条注释，版本号（BUILD）就变了
    nxt = WORK / "next"
    shutil.copytree(REPO / "xlt", nxt / "xlt", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(REPO / "pyproject.toml", nxt / "pyproject.toml")
    panel = nxt / "xlt" / "panel.py"
    panel.write_text(panel.read_text(encoding="utf-8").replace("CSS = \"\"\"\n", "CSS = \"\"\"\n/* next release */\n", 1), encoding="utf-8")

    node = Node(WORK, "nes-mac", "nes", "Nes", "macos", 47950, 47951, 47952)
    node.init()
    browser = Browser(9261, 1440, 900)
    try:
        # ---- 1. 旧版本的页面 → 通讯员换成当前版本 ----
        node.use_old(old)
        node.start()
        browser.open(f"http://127.0.0.1:{node.panel}/", 1440, 900)
        time.sleep(1)
        node.crash()
        node.use_new()
        node.start()
        wait_for(lambda: "kv-row" in (browser.js("document.body.innerHTML") or ""), 20, "旧页面没有刷到新内容")
        time.sleep(1)
        good(browser.js(CHECK), "旧页面（旧脚本）在通讯员升级后")
        browser.screenshot(WORK / "stale-old-tab.png", clip={"x": 0, "y": 0, "width": 300, "height": 900})
        say("旧版本的页面开着、通讯员升级：旧页面拿到新内容的同时也拿到新样式，布局不乱")

        # ---- 2. 当前版本的页面 → 通讯员换成样式变了的下一版：自己整页刷新 ----
        browser.open(f"http://127.0.0.1:{node.panel}/", 1440, 900)
        wait_for(lambda: browser.js("document.querySelector('.kv-row') ? 1 : 0"), 10, "页面没打开")
        browser.js("window.__marker = 7; 1")
        before = browser.js(CHECK)["build"]
        node.crash()
        node.env = {"PYTHONPATH": str(nxt)}
        node.cwd = nxt
        node.start()
        wait_for(lambda: browser.js(CHECK)["build"] not in ("", before), 25, "通讯员换了样式后，开着的页面没有自己刷新")
        info = browser.js(CHECK)
        if info["marker"] != 0:
            raise Fail("应该是整页刷新（页面变量被清空），但页面没有刷新")
        good(info, "整页刷新之后")
        say("样式变了的下一版：开着的页面发现版本不同，自己整页刷新一次")

        # ---- 3. 样式表完全不生效 ----
        browser.js("document.querySelectorAll('style, link[rel=stylesheet]').forEach(e => e.remove()); 1")
        time.sleep(0.5)
        info = browser.js("""(() => {
          const r = [...document.querySelectorAll('.kv-row')].find(x => x.textContent.includes('UDP'));
          const sp = [...r.querySelectorAll('dd span')].map(s => Math.round(s.getBoundingClientRect().top));
          const ic = document.querySelector('.nav-label .ic').getBoundingClientRect();
          const btn = document.querySelector('.icon-btn').getBoundingClientRect();
          const first = document.querySelector('.kv-row');
          const dt = first.querySelector('dt').getBoundingClientRect(), dd = first.querySelector('dd').getBoundingClientRect();
          return {split: sp[0] !== sp[1], icon: [ic.width, ic.height], btn: [btn.width, btn.height],
                  sameRow: Math.abs((dt.top + dt.height / 2) - (dd.top + dd.height / 2)) < 6,
                  nav: getComputedStyle(document.querySelector('.nav-label')).display};
        })()""")
        if not info["split"] or max(info["icon"]) > 20 or max(info["btn"]) > 26 or not info["sameRow"] or info["nav"] not in ("inline-flex", "flex"):
            raise Fail("没有样式表时，关键布局靠自带的内联样式也该是对的：" + str(info))
        browser.screenshot(WORK / "no-stylesheet.png", clip={"x": 0, "y": 0, "width": 300, "height": 900})
        say("样式表完全不生效：图标大小、本机块两列、TCP/UDP 分行、复制按钮大小仍然正常")
    finally:
        browser.close()
        node.crash()
    print("PANEL STALE E2E 全部通过")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Fail as exc:
        print("失败：", exc)
        raise SystemExit(1)
