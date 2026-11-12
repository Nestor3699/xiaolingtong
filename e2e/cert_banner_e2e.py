#!/usr/bin/env python3
"""证书被替换：自动登记照旧替换，但状态页顶部要挂醒目的横幅；点「知道了」才消失；重启也还在。"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from browser import Browser  # noqa: E402
from harness import Fail, Node, fresh, say, wait_for, xlt  # noqa: E402

from xlt.certevents import short_fp  # noqa: E402
from xlt.crypto import fingerprint_cert, load_cert  # noqa: E402
from xlt.store import Home, member_by_name  # noqa: E402

WORK = Path(__file__).resolve().parent / "work-cert-banner"


def banners(page: str) -> int:
    return page.count('role="alert"')


def main() -> int:
    fresh(WORK)
    a = Node(WORK, "nes-mac", "nes", "Nes", "macos", 47400, 47401, 47402)
    b = Node(WORK, "haonian-win", "haonian", "浩年", "windows", 47410, 47411, 47412)
    c = Node(WORK, "haonian-win", "haonian", "浩年", "windows", 47420, 47421, 47422)
    c.home = WORK / "impostor"
    a.init()
    b.init()
    c.init()
    a.peer_udp, b.peer_udp, c.peer_udp = b.udp, a.udp, a.udp
    browser = None
    try:
        # 只 init，不手工 trust：靠广播自动登记
        a.start()
        b.start()
        wait_for(lambda: member_by_name(Home(a.home), "haonian-win"), 20, "A 没有自动登记 B")
        fp_b = fingerprint_cert(load_cert(b.home / "tls" / "haonian-win.crt"))
        if banners(a.page()):
            raise Fail("还没有替换，页面上不该有横幅")
        say("A 自动登记了 B；此时页面没有横幅")

        # B 关机，C 冒用 B 的名字、换新证书
        b.crash()
        fp_c = fingerprint_cert(load_cert(c.home / "tls" / "haonian-win.crt"))
        c.start()
        wait_for(lambda: (member_by_name(Home(a.home), "haonian-win") or {}).get("cert_sha256", "").upper() == fp_c.upper(), 25, "A 没有用 C 的证书替换 B")
        wait_for(lambda: banners(a.page()) == 1, 10, "替换后页面顶部应有 1 条横幅")
        page = a.page()
        for need in ("证书被替换", "haonian-win", short_fp(fp_b), short_fp(fp_c), "知道了", "可能有人冒充"):
            if need not in page:
                raise Fail(f"横幅里缺少「{need}」")
        if page.index('role="alert"') > page.index('<header class="page-head">'):
            raise Fail("横幅应该在页头之前（页面最顶部）")
        if a.api().get("cert_alerts", [{}])[0].get("name") != "haonian-win":
            raise Fail("状态页数据里没有证书替换事件")
        say("证书被替换后：页面最顶部有横幅，写明是谁、什么时候、旧/新指纹（短）")

        # 重启 A（C 继续心跳，同一个指纹不应再产生第二条）
        a.crash()
        a.start()
        time.sleep(4)
        if banners(a.page()) != 1:
            raise Fail("重启后横幅应该还在，且仍是 1 条：" + str(banners(a.page())))
        say("A 被直接杀掉重启：横幅还在，没有重复")

        # 浏览器里真点一下「知道了」：不整页刷新，横幅就地消失
        browser = Browser(9231)
        browser.open(f"http://127.0.0.1:{a.panel}/")
        if not browser.js("!!document.querySelector('.alert-bar[role=alert]')"):
            raise Fail("浏览器里没看到横幅")
        browser.screenshot(WORK / "cert-banner.png")
        browser.js("window.__probe = 'same-page'; true")
        browser.js("document.querySelector('[data-dismiss]').click(); true")
        wait_for(lambda: browser.js("!document.querySelector('.alert-bar[role=alert]')"), 10, "点了「知道了」以后横幅应就地消失")
        if browser.js("window.__probe") != "same-page":
            raise Fail("点「知道了」导致整页刷新了")
        say("在无头浏览器里点「知道了」：横幅就地消失，页面没有整页刷新")

        # 持久：重启后仍然是消掉的
        a.crash()
        a.start()
        time.sleep(2)
        if banners(a.page()) != 0:
            raise Fail("点掉以后重启，横幅又出来了")
        peers = xlt(a.home, "peers").stdout
        if "警告" in peers:
            raise Fail("点掉以后 xlt peers 还在显示证书警告：" + peers)
        say("点掉以后重启不会再冒出来，xlt peers 的警告也清掉了")

        # 防护：别的网页不能代点
        def dismiss(headers):
            req = urllib.request.Request(f"http://127.0.0.1:{a.panel}/local/cert-dismiss", data=b'{"id":"all"}',
                                         headers={"Content-Type": "application/json", **headers})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status
            except urllib.error.HTTPError as exc:
                return exc.code

        if dismiss({}) != 403:
            raise Fail("没有自定义头的请求应被拒")
        if dismiss({"X-Xlt-Panel": "1", "Host": "evil.example"}) != 403:
            raise Fail("Host 不是本机的请求应被拒（防 DNS 重绑定）")
        if dismiss({"X-Xlt-Panel": "1"}) != 200:
            raise Fail("状态页自己发的请求应该成功")
        say("不带自定义头、或 Host 不是本机的「知道了」请求被拒绝（防别的网页代点）")
        print("CERT BANNER E2E 全部通过")
        return 0
    except Fail as exc:
        print("失败", exc)
        print(a.log_text()[-2000:])
        return 1
    finally:
        if browser:
            browser.close()
        for node in (a, b, c):
            node.crash()


if __name__ == "__main__":
    raise SystemExit(main())
