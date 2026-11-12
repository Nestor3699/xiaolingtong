#!/usr/bin/env python3
"""已送达 / 已读回执，以及和已经发给同事的旧版本（9d45baf）互通。"""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import Fail, fresh, old_release, pair, say, wait_for  # noqa: E402

WORK = Path(__file__).resolve().parent / "work-receipts"


def send(node, text: str) -> str:
    r = node.cli("send", "--to", "haonian" if node.person == "nes" else "nes", "--type", "notify", "--subject", text, "--text", text)
    out = json.loads(r.stdout)
    if out.get("status") != "delivered":
        raise Fail(f"{text}：应该 delivered，实际 {r.stdout}")
    return out["id"]


def status_of(node, msg_id: str) -> str:
    for row in node.api().get("sent") or []:
        if row["id"] == msg_id:
            return row["status"]
    return ""


def receipts_file(node) -> dict:
    p = node.home / "receipts.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"items": {}}


def main() -> int:
    fresh(WORK)
    nes, hao = pair(WORK, 47600)
    try:
        nes.start()
        hao.start()
        wait_for(lambda: all((hao.home / "peers.cache.json").exists() and (nes.home / "peers.cache.json").exists() for _ in [0]), 15, "双方没有互相发现")

        l1 = send(nes, "L1 用 xlt read 读")
        l2 = send(nes, "L2 频道推进了会话")
        l3 = send(nes, "L3 pi 扩展取走")
        l4 = send(nes, "L4 没人读")
        wait_for(lambda: all(status_of(nes, i) == "receipt_stored" for i in (l1, l2, l3, l4)), 10, "四封都应该先是「已送达」")
        say("四封信送到后，发信方都显示「已送达」")

        # 读法一：agent 跑 xlt read
        r = hao.cli("read", l1)
        if r.returncode != 0 or "L1" not in r.stdout:
            raise Fail("xlt read 没打印信纸：" + r.stdout + r.stderr)
        # 读法二：xlt-channel 把信推进会话后把状态改成 delivered（直接改信箱文件，和 channel.mjs 做的一样）
        env_path = next((hao.home / "inbox").rglob(f"{l2}.env.json"))
        env = json.loads(env_path.read_text(encoding="utf-8"))
        env["state"]["status"] = "delivered"
        env_path.write_text(json.dumps(env, ensure_ascii=False), encoding="utf-8")
        # 读法三：pi 扩展把信从 bridges/pi/inbox 移进 inbox-taken
        taken = hao.home / "bridges" / "pi" / "inbox-taken"
        taken.mkdir(parents=True, exist_ok=True)
        (taken / f"{l3}.letter.md").write_text("x", encoding="utf-8")

        wait_for(lambda: all(status_of(nes, i) == "receipt_read" for i in (l1, l2, l3)), 25, "三种读法都应该在 25 秒内让发信方看到「已读」")
        if status_of(nes, l4) != "receipt_stored":
            raise Fail("没人读的 L4 不该变成已读")
        say("xlt read / 频道交给 AI / pi 取走 三种读法 → 发信方「已读」；没人读的仍是「已送达」")

        out = nes.cli("sent").stdout
        if "已读" not in out or "已送达" not in out:
            raise Fail("xlt sent 没有同时显示已读和已送达：" + out)
        page = nes.page()
        if "已读" not in page or "已送达" not in page:
            raise Fail("状态页「已发出」没有已读/已送达")
        inbox_out = hao.cli("inbox").stdout
        if "已读" not in inbox_out or "未读" not in inbox_out:
            raise Fail("收信方的 xlt inbox 应显示已读/未读：" + inbox_out)
        say("命令行 xlt sent / xlt inbox 和状态页都显示回执状态")

        # 发信方被杀后重启，回执不丢
        nes.crash()
        nes.start()
        if status_of(nes, l1) != "receipt_read" or status_of(nes, l4) != "receipt_stored":
            raise Fail("重启后回执状态丢了")
        say("发信方被直接杀掉重启：回执状态还在")

        # 回执只给原发信人：haonian 自己的证书去问 haonian 自己的服务「L1 的回执」，
        # L1 是 nes 发的，调用者不是发信人 → 404 NO_SUCH_MESSAGE
        from xlt.mtls import request
        from xlt.store import Home, load_directory

        crt, key = hao.home / "tls" / "haonian-win.crt", hao.home / "tls" / "haonian-win.key"
        me_fp = [m for m in load_directory(Home(hao.home)) if m["name"] == "haonian-win"][0]["cert_sha256"]
        trust_me = crt.read_text(encoding="utf-8")
        code, body = request("127.0.0.1", hao.tcp, "GET", f"/xlt/v1/receipt?id={l1}", b"", crt, key, me_fp, trust_me, 5)
        if code != 404 or json.loads(body)["error"]["code"] != "NO_SUCH_MESSAGE":
            raise Fail(f"不是原发信人却能问到回执：{code} {body!r}")
        say("不是原发信人的证书去问回执：404，什么都不透露")

        # ---- 兼容：新发信方 → 旧收信方 ----
        old = old_release(WORK)
        hao.crash()
        hao.use_old(old)
        hao.start()
        wait_for(lambda: (nes.home / "peers.cache.json").exists(), 5, "")
        l5 = send(nes, "L5 发给旧版本")
        time.sleep(1)
        wait_for(lambda: receipts_file(nes)["items"].get(l5, {}).get("no_receipts"), 20, "对旧版本收信方，发信方应记下「不支持回执」并停止询问")
        polls = receipts_file(nes)["items"][l5]["polls"]
        time.sleep(7)
        if receipts_file(nes)["items"][l5]["polls"] != polls:
            raise Fail("已经知道对方是旧版本，却还在继续询问")
        if status_of(nes, l5) != "receipt_stored":
            raise Fail("旧版本收信方应停在「已送达」，实际 " + status_of(nes, l5))
        if "Traceback" in nes.log_text() or "Traceback" in (WORK / "nes.out").read_text(encoding="utf-8"):
            raise Fail("发信方日志里有异常")
        say("新发信方 → 旧版本收信方：信正常送达，停在「已送达」，不再询问，没有报错")

        # 旧版本回信 = 对方读过了：新发信方升到「已读」
        r = hao.cli("reply", l5, "--text", "旧版本回信")
        if r.returncode != 0:
            raise Fail("旧版本回信失败：" + r.stdout + r.stderr)
        wait_for(lambda: status_of(nes, l5) == "receipt_read", 20, "旧版本回信后，发信方应显示已读")
        say("旧版本收信方回了信 → 发信方升为「已读」（回信本身就是读过的证据）")

        # ---- 兼容：旧发信方 → 新收信方 ----
        nes.crash()
        nes.use_old(old)
        nes.start()
        hao.crash()
        hao.use_new()
        hao.start()
        time.sleep(3)
        r = nes.cli("send", "--to", "haonian", "--type", "notify", "--subject", "L6", "--text", "旧版本发的")
        if json.loads(r.stdout).get("status") != "delivered":
            raise Fail("旧发信方 → 新收信方没送达：" + r.stdout + r.stderr)
        if not any(e.get("subject") == "L6" for e in hao.inbox_envs()):
            raise Fail("新收信方没收到旧版本的信")
        say("旧版本发信方 → 新收信方：照常送达")
        print("RECEIPTS E2E 全部通过")
        return 0
    except Fail as exc:
        print("失败", exc)
        for node in (nes, hao):
            print(f"--- {node.me} 日志尾部 ---")
            print(node.log_text()[-2500:])
        return 1
    finally:
        for node in (nes, hao):
            node.crash()


if __name__ == "__main__":
    raise SystemExit(main())
