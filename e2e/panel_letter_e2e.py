#!/usr/bin/env python3
"""状态页点开一封信：看正文、记已读、深链接、刷新不关掉。"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from browser import Browser  # noqa: E402
from harness import Fail, Node, fresh, say, wait_for  # noqa: E402

WORK = Path(__file__).resolve().parent / "work-panel-letter"
SHOTS = Path("/workspace/xlt-ui")

INBOX_ID = "msg-20261112-150000-leo-mac-0001"
INBOX_BODY = """# 接口报错

请看 **这段** 和 `code`。

<script>alert(1)</script>
<img src=x onerror=alert(1)>

- 第一项
- 第二项

LETTER_BODY_CANARY_INBOX
"""
PENDING_ID = "msg-20261112-151000-nes-mac-0002"
PENDING_BODY = "还没送到的正文 PENDING_BODY_CANARY\n"
SENT_GONE_ID = "msg-20261112-140000-nes-mac-0003"
SENT_KEPT_ID = PENDING_ID  # 还在待发箱里的已发出，正文还在
MISSING_ID = "msg-20261112-152000-leo-mac-0004"


def plant(home: Path) -> Path:
    inbox = home / "inbox" / "default"
    inbox.mkdir(parents=True, exist_ok=True)
    env = {
        "id": INBOX_ID,
        "type": "ask",
        "from": "leo-mac",
        "from_person": "Leo",
        "to": "nes",
        "created": "2026-11-12T15:00:00+08:00",
        "subject": "帮看一下这段报错",
        "letter_bytes": len(INBOX_BODY.encode()),
        "state": {"status": "awaiting_manual", "routed_to": "claude/default", "received": "2026-11-12T15:00:01+08:00"},
        "attachments": [{"id": "att-1", "name": "error.log", "size": 12}],
    }
    (inbox / f"{INBOX_ID}.env.json").write_text(json.dumps(env, ensure_ascii=False), encoding="utf-8")
    (inbox / f"{INBOX_ID}.letter.md").write_text(INBOX_BODY, encoding="utf-8")
    att = home / "inbox" / INBOX_ID / "attachments"
    att.mkdir(parents=True, exist_ok=True)
    (att / "error.log").write_bytes(b"hello world\n")

    missing = {
        "id": MISSING_ID,
        "type": "notify",
        "from": "leo-mac",
        "from_person": "Leo",
        "to": "nes",
        "created": "2026-11-12T15:20:00+08:00",
        "subject": "只有信封没有信纸",
        "state": {"status": "queued", "routed_to": "claude/default"},
    }
    (inbox / f"{MISSING_ID}.env.json").write_text(json.dumps(missing, ensure_ascii=False), encoding="utf-8")

    pend = home / "pending" / "haonian-win"
    pend.mkdir(parents=True, exist_ok=True)
    pend_env = {
        "id": PENDING_ID,
        "type": "ask",
        "from": "nes-mac",
        "from_person": "nes",
        "to": "浩年",
        "to_member": "haonian-win",
        "created": "2026-11-12T15:10:00+08:00",
        "subject": "今天下午的接口文档",
        "letter_bytes": len(PENDING_BODY.encode()),
        "attempts": 1,
        "state": {"status": "pending", "updated": "2026-11-12T15:10:00+08:00"},
    }
    (pend / f"{PENDING_ID}.env.json").write_text(json.dumps(pend_env, ensure_ascii=False), encoding="utf-8")
    (pend / f"{PENDING_ID}.letter.md").write_text(PENDING_BODY, encoding="utf-8")

    sent = home / "sent.jsonl"
    rows = [
        {"id": SENT_GONE_ID, "to": "浩年", "to_member": "haonian-win", "type": "notify", "subject": "昨天已经送到的信", "created": "2026-11-12T14:00:00+08:00", "ts": "2026-11-12T14:00:00+08:00"},
        {"id": SENT_KEPT_ID, "to": "浩年", "to_member": "haonian-win", "type": "ask", "subject": "今天下午的接口文档", "created": "2026-11-12T15:10:00+08:00", "ts": "2026-11-12T15:10:00+08:00"},
    ]
    sent.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    return att / "error.log"


def api_letter(port: int, box: str, msg_id: str, header: bool = True) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/local/letter?box={box}&id={msg_id}",
        headers={"X-Xlt-Panel": "1", "Accept": "application/json"} if header else {"Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = {"raw": body}
        return exc.code, data


def wait_js(browser: Browser, expr: str, timeout: float = 8):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = browser.js(expr)
        if last:
            return last
        time.sleep(0.1)
    raise Fail(f"等了 {timeout}s 还没有：{expr}（最后是 {last!r}）")

DELIVERED_BODY = "# 已送达的信\n\n这封信已经送到浩年那边了，待发箱早就清空。\n\nDELIVERED_BODY_CANARY\n"


def delivered_stage(browser: Browser) -> None:
    """真的发一封带附件的信，等对方收下、待发箱清空，再在发信人的状态页里点开：正文还在。"""
    from harness import pair, xlt

    work = WORK / "pair"
    work.mkdir(parents=True, exist_ok=True)
    nes, hao = pair(work, 47990)
    attach = work / "report.txt"
    attach.write_text("附件内容 hello\n", encoding="utf-8")
    try:
        nes.start()
        hao.start()
        r = xlt(nes.home, "send", "--to", "haonian", "--type", "ask", "--subject", "已送达的那封", "--text", DELIVERED_BODY, "--attach", str(attach))
        out = json.loads(r.stdout)
        msg_id = out.get("id") or out.get("msg_id")
        if not msg_id:
            raise Fail("发信没有返回编号：" + r.stdout)
        wait_for(lambda: len([e for e in hao.inbox_envs() if e["id"] == msg_id]) == 1, 40, "对方没有收到信")
        wait_for(lambda: not nes.pending_files(), 20, "送达后待发箱应清空")
        if (nes.home / "pending" / "haonian-win" / f"{msg_id}.letter.md").exists():
            raise Fail("待发箱里的信纸应已删除（这才是要验证的场景）")
        copy = nes.home / "sent" / f"{msg_id}.letter.md"
        env_copy = nes.home / "sent" / f"{msg_id}.env.json"
        if not copy.exists() or "DELIVERED_BODY_CANARY" not in copy.read_text(encoding="utf-8"):
            raise Fail("发信时没有在 ~/.xlt/sent/ 留下正文副本")
        env = json.loads(env_copy.read_text(encoding="utf-8"))
        if not any(a.get("source") == str(attach.resolve()) for a in env.get("attachments") or []):
            raise Fail(f"信封副本里没有附件的本机来源路径：{env.get('attachments')}")
        if (copy.stat().st_mode & 0o077) or (env_copy.stat().st_mode & 0o077):
            raise Fail("副本文件权限应只有本人可读写")
        say("发信时在 ~/.xlt/sent/ 留了正文和信封副本（含附件来源路径，仅本人可读）")

        page = f"http://127.0.0.1:{nes.panel}/"
        browser.js(f"location.href = {page + '#sent:' + msg_id!r}; true")
        wait_js(browser, "document.querySelector('#letter-pane.open .letter-body') && document.querySelector('#letter-pane.open').innerText.indexOf('DELIVERED_BODY_CANARY') >= 0", timeout=15)
        text = browser.js("document.querySelector('#letter-pane').innerText")
        if "正文已不在本机" in text:
            raise Fail("已送达的信不应再提示正文不在本机")
        if "report.txt" not in text or str(attach.resolve()) not in text:
            raise Fail("附件文件名或本机路径没有列出：" + text)
        if not any(w in text for w in ("已送达", "已读")):
            raise Fail("状态应是已送达/已读：" + text)
        if SHOTS.exists():
            browser.js("document.querySelector('#inbox') && document.querySelector('#sent').scrollIntoView({block:'start'}); true")
            browser.screenshot(SHOTS / "v6-sent-delivered.png")
        say("通过 已送达的信：待发箱已清空，在「已发出」里点开仍能看到正文和附件")

        # 命令行：xlt sent <编号> 也能看到正文
        shown = xlt(nes.home, "sent", msg_id)
        if shown.returncode != 0 or "DELIVERED_BODY_CANARY" not in shown.stdout:
            raise Fail("xlt sent <编号> 没有打印正文：" + shown.stdout + shown.stderr)
        say("通过 xlt sent <编号> 打印发出的信的正文")
    finally:
        for node in (nes, hao):
            if node.proc:
                node.proc.stop()


def main() -> int:
    fresh(WORK)
    node = Node(WORK, "nes-mac", "nes", "Nes", "macos", 47970, 47971, 47972)
    node.init()
    att_path = plant(node.home)
    node.start()
    browser = Browser(9270, 1440, 900)
    try:
        page = f"http://127.0.0.1:{node.panel}/"

        # 接口要有状态页自己的头，不能把正文放进列表快照
        code, data = api_letter(node.panel, "inbox", INBOX_ID, header=False)
        if code != 403:
            raise Fail(f"没有面板头却能取正文：{code} {data}")
        html = node.page()
        if "LETTER_BODY_CANARY_INBOX" in html or "PENDING_BODY_CANARY" in html:
            raise Fail("列表页里出现了信纸正文")
        if "点一封信可查看正文" not in html:
            raise Fail("页头没有阅读说明")

        # 点开未读的收信
        env_before = json.loads((node.home / "inbox" / "default" / f"{INBOX_ID}.env.json").read_text(encoding="utf-8"))
        if (env_before.get("state") or {}).get("read_at"):
            raise Fail("种下的信不应该已经是已读")

        browser.open(page + "#inbox")
        wait_js(browser, "document.querySelector('tr[data-open=\"inbox:" + INBOX_ID + "\"]') !== null")
        unread = browser.js("document.body.innerText")
        if "还没读" not in unread:
            raise Fail("打开前应显示还没读")

        browser.js(f'document.querySelector(\'tr[data-open="inbox:{INBOX_ID}"]\').click(); true')
        wait_js(browser, "document.querySelector('#letter-pane.open') && document.querySelector('.letter-body') && document.querySelector('.letter-body').innerText.indexOf('LETTER_BODY_CANARY_INBOX') >= 0")
        hash1 = browser.js("location.hash")
        if hash1 != f"#inbox:{INBOX_ID}":
            raise Fail(f"点开后地址不对：{hash1}")

        info = browser.js("""({
          title: document.querySelector('.letter-title') && document.querySelector('.letter-title').textContent,
          text: document.querySelector('.letter-body') && document.querySelector('.letter-body').innerText,
          html: document.querySelector('.letter-body') && document.querySelector('.letter-body').innerHTML,
          scripts: document.querySelectorAll('.letter-body script, .letter-body img').length,
          strong: !!(document.querySelector('.letter-body strong')),
          atts: [...document.querySelectorAll('.letter-att')].map(el => el.innerText),
          status: [...document.querySelectorAll('.letter-meta dd')].map(el => el.textContent),
        })""")
        if info["title"] != "帮看一下这段报错":
            raise Fail(f"标题不对：{info['title']!r}")
        if "LETTER_BODY_CANARY_INBOX" not in info["text"]:
            raise Fail("正文没有出来")
        if "请看" not in info["text"] or "这段" not in info["text"]:
            raise Fail("正文不全")
        if info["scripts"] != 0:
            raise Fail("信纸里的 HTML 不该被执行或插进节点")
        if "<script>" not in info["text"] and "script" not in info["text"]:
            raise Fail("信纸里的标签应被当成文字显示")
        if not info["strong"]:
            raise Fail("简单 markdown 的加粗没有渲染")
        if not any("error.log" in t and "12" in t and "字节" in t and "attachments" in t for t in info["atts"]):
            raise Fail(f"附件列表不对：{info['atts']}")
        if not any(str(att_path) in t or att_path.name in t for t in info["atts"]):
            raise Fail(f"附件路径没列出：{info['atts']} 期望 {att_path}")
        if not any("已读" in t for t in info["status"]):
            raise Fail(f"点开后状态应是已读：{info['status']}")

        env_after = json.loads((node.home / "inbox" / "default" / f"{INBOX_ID}.env.json").read_text(encoding="utf-8"))
        if not (env_after.get("state") or {}).get("read_at"):
            raise Fail("打开未读收信后，应和 xlt read 一样记为已读")
        if (env_after.get("state") or {}).get("read_via") != "panel":
            raise Fail(f"已读来源不对：{env_after.get('state')}")
        say("通过 点开收到的信：看到正文、附件、已读，HTML 被转义")

        # 等列表刷新后徽章变成已读，阅读面板还在
        wait_js(browser, f"""(function(){{
          var row = document.querySelector('tr[data-open="inbox:{INBOX_ID}"]');
          var pane = document.querySelector('#letter-pane.open .letter-body');
          return !!(row && row.innerText.indexOf('已读') >= 0 && pane && pane.innerText.indexOf('LETTER_BODY_CANARY_INBOX') >= 0);
        }})()""", timeout=8)
        say("通过 列表刷新后变成已读，阅读面板还开着")

        # 深链接：直接打开带 hash 的地址
        browser.js("window.__xltProbe = 'keep'; true")
        browser.js(f"location.hash = '#sent:{SENT_GONE_ID}'; true")
        wait_js(browser, "document.querySelector('#letter-pane.open .letter-missing') && document.querySelector('#letter-pane.open').innerText.indexOf('更新前发出') >= 0 && document.querySelector('#letter-pane.open').innerText.indexOf('正文已不在本机') >= 0")
        say("通过 更新前发出、没有副本的信：提示「这封信是在更新前发出的，当时没留正文副本」")

        browser.js(f"location.hash = '#pending:{PENDING_ID}'; true")
        wait_js(browser, "document.querySelector('#letter-pane.open .letter-body') && document.querySelector('#letter-pane.open').innerText.indexOf('PENDING_BODY_CANARY') >= 0")
        say("通过 待送达的信能看到还在本机的正文")

        browser.js(f"location.hash = '#sent:{SENT_KEPT_ID}'; true")
        wait_js(browser, "document.querySelector('#letter-pane.open .letter-body') && document.querySelector('#letter-pane.open').innerText.indexOf('PENDING_BODY_CANARY') >= 0")
        say("通过 已发出且待发箱里还留着信纸时能看到正文")

        # 缺少正文的收信
        browser.js(f"location.hash = '#inbox:{MISSING_ID}'; true")
        wait_js(browser, "document.querySelector('#letter-pane.open .letter-missing') && /正文已不在本机/.test(document.querySelector('#letter-pane.open').innerText)")
        say("通过 收信只有信封时也提示正文已不在本机")

        # 再打开那封有正文的收信，滚动阅读区，改另一封信的标题让 5 秒刷新换列表，面板和滚动位置要留下
        browser.js(f"location.hash = '#inbox:{INBOX_ID}'; true")
        wait_js(browser, "document.querySelector('#letter-pane.open .letter-body') && document.querySelector('.letter-body').innerText.indexOf('LETTER_BODY_CANARY_INBOX') >= 0")
        browser.js("""(function(){
          var sc = document.querySelector('#letter-pane .letter-scroll');
          var pad = document.createElement('div');
          pad.style.height = '2000px';
          pad.setAttribute('data-pad', '1');
          sc.appendChild(pad);
          sc.scrollTop = 800;
          window.__letterScroll = sc.scrollTop;
          return sc.scrollTop;
        })()""")
        # 改另一封信的标题，逼一次原地刷新
        missing_path = node.home / "inbox" / "default" / f"{MISSING_ID}.env.json"
        missing_env = json.loads(missing_path.read_text(encoding="utf-8"))
        missing_env["subject"] = "只有信封没有信纸-已改"
        missing_path.write_text(json.dumps(missing_env, ensure_ascii=False), encoding="utf-8")
        wait_js(browser, "document.body.innerText.indexOf('只有信封没有信纸-已改') >= 0", timeout=10)
        kept = browser.js("""({
          marker: window.__xltLiveMarker,
          probe: window.__xltProbe,
          hash: location.hash,
          open: !!(document.querySelector('#letter-pane.open')),
          body: document.querySelector('#letter-pane .letter-body') && document.querySelector('#letter-pane .letter-body').innerText,
          scroll: document.querySelector('#letter-pane .letter-scroll') && document.querySelector('#letter-pane .letter-scroll').scrollTop,
          expect: window.__letterScroll
        })""")
        if not kept["open"] or "LETTER_BODY_CANARY_INBOX" not in (kept["body"] or ""):
            raise Fail(f"刷新后阅读面板被关掉或正文没了：{kept}")
        if kept["hash"] != f"#inbox:{INBOX_ID}":
            raise Fail(f"刷新后 hash 丢了：{kept['hash']}")
        if abs((kept["scroll"] or 0) - (kept["expect"] or 0)) > 5:
            raise Fail(f"刷新后阅读区滚动位置丢了：{kept['scroll']} vs {kept['expect']}")
        say("通过 5 秒原地刷新后，打开的信还在，滚动位置也还在")

        # 键盘：关掉，再在焦点行上按 Enter
        browser.js("""(function(){
          var ev = new KeyboardEvent('keydown', {key:'Escape', bubbles:true});
          document.dispatchEvent(ev);
          return true;
        })()""")
        wait_js(browser, "!(document.querySelector('#letter-pane.open')) && location.hash === '#inbox'")
        browser.js(f"""(function(){{
          var row = document.querySelector('tr[data-open="inbox:{INBOX_ID}"]');
          row.focus();
          row.dispatchEvent(new KeyboardEvent('keydown', {{key:'Enter', bubbles:true}}));
          return true;
        }})()""")
        wait_js(browser, f"document.querySelector('#letter-pane.open') && location.hash === '#inbox:{INBOX_ID}'")
        say("通过 Esc 关闭，Enter 再打开")

        browser.js(f'document.querySelector(\'tr[data-open="inbox:{INBOX_ID}"]\').click(); true')
        wait_js(browser, "!(document.querySelector('#letter-pane.open')) && location.hash === '#inbox'")
        say("通过 再点同一行关闭")

        # 深链接：整页带着 hash 重新加载，仍要打开这封信
        browser.js(f"location.href = {page + '#inbox:' + INBOX_ID!r}; true")
        wait_js(browser, "document.querySelector('#letter-pane.open .letter-body') && document.querySelector('.letter-body').innerText.indexOf('LETTER_BODY_CANARY_INBOX') >= 0", timeout=12)
        if browser.js("location.hash") != f"#inbox:{INBOX_ID}":
            raise Fail("深链接没有打开这封信")
        say("通过 地址栏 hash 深链接能直接打开信")

        # 关掉按钮
        browser.js("document.querySelector('[data-letter-close]').click(); true")
        wait_js(browser, "!(document.querySelector('#letter-pane.open'))")
        say("通过 点 × 关闭")

        delivered_stage(browser)
        print("PANEL LETTER E2E 全部通过")
        return 0
    finally:
        try:
            browser.close()
        except Exception:
            pass
        if node.proc:
            node.proc.stop()


if __name__ == "__main__":
    raise SystemExit(main())
