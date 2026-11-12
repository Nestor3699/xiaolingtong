#!/usr/bin/env python3
"""待送达（存了再转）：对方不在 → 信进待发箱；发信端断电重启信还在；对方一上线自动补发；同一封信只落一次。"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import Fail, fresh, pair, say, wait_for, xlt  # noqa: E402

from xlt.crypto import load_key  # noqa: E402
from xlt.mtls import request  # noqa: E402
from xlt.store import Home, load_directory  # noqa: E402

WORK = Path(__file__).resolve().parent / "work-outbox"


def main() -> int:
    fresh(WORK)
    nes, hao = pair(WORK, 47500)
    # 退避压短，好在几秒内看到「超过最后一档以后仍然每档重试」
    nes.env = {"XLT_BACKOFF": "1,2"}
    try:
        # 1. 对方没开：信进待发箱，面板写「待送达」
        nes.start()
        r = xlt(nes.home, "send", "--to", "haonian", "--type", "notify", "--subject", "离线测试", "--text", "第一封：对方还没开")
        out = json.loads(r.stdout)
        if out.get("status") != "queued":
            raise Fail("对方离线时应该是 queued：" + r.stdout)
        files = nes.pending_files()
        if len(files) != 1:
            raise Fail(f"待发箱里应有 1 封，实际 {len(files)}")
        msg_id = json.loads(files[0].read_text(encoding="utf-8"))["id"]
        page = nes.page()
        if "待送达" not in page or msg_id not in page:
            raise Fail("状态页没有显示「待送达」和这封信的编号")
        say("对方不在时：信进待发箱，状态页写「待送达」")

        # 2. 重试节奏：退避走完最后一档以后仍然有下次重试时间，没有变成每 2 秒一次
        def attempts():
            env = json.loads(files[0].read_text(encoding="utf-8"))
            return env if int(env.get("attempts") or 0) >= 4 else None

        env = wait_for(attempts, 30, "30 秒内应该已经重试 4 次以上")
        if not env.get("next_try_at"):
            raise Fail("退避走完以后 next_try_at 变成空，会每 2 秒重试一次")
        gap = float(env["next_try_at"]) - time.time()
        if gap > 2.5:
            raise Fail(f"最后一档应是 2 秒，实际离下次还有 {gap:.1f} 秒")
        row = [x for x in nes.api()["pending"] if x["id"] == msg_id][0]
        if not row.get("next_try_at"):
            raise Fail("状态页数据里没有下次重试时间")
        say("退避走完后继续按最后一档重试（不会停，也不会每 2 秒狂试）")

        # 3. 发信端「断电」：直接杀，不收尾。信必须还在，重启后仍在待发箱
        nes.crash()
        if not files[0].exists():
            raise Fail("杀掉网关以后待发信不见了")
        nes.env = {}
        nes.start()
        if msg_id not in nes.page():
            raise Fail("重启后状态页没有这封待送达的信")
        say("发信端被直接杀掉再重启：信还在待发箱")

        # 4. 对方上线：自动补发，不用重发；收件箱里恰好 1 封
        dup_env = json.loads(files[0].read_text(encoding="utf-8"))
        dup_letter = (files[0].with_name(f"{msg_id}.letter.md")).read_text(encoding="utf-8")
        hao.start()
        wait_for(lambda: len([e for e in hao.inbox_envs() if e["id"] == msg_id]) == 1, 40, "对方上线后 40 秒内没有收到补发的信")
        wait_for(lambda: not nes.pending_files(), 10, "补发成功后待发箱应该清空")
        say("对方上线后自动补发，收件箱恰好 1 封，待发箱清空")

        # 5. 幂等：把同一封信再投一次（模拟「收到了但回执丢了，发信端又发一遍」）
        key = nes.home / "tls" / "nes-mac.key"
        crt = nes.home / "tls" / "nes-mac.crt"
        trust = (nes.home / "peers" / "haonian-win.crt").read_text(encoding="utf-8")
        member_fp = [m for m in load_directory(Home(nes.home)) if m["name"] == "haonian-win"][0]["cert_sha256"]
        from xlt.engine import _public

        payload = json.dumps({"envelope": _public(dup_env), "letter": dup_letter}, ensure_ascii=False).encode("utf-8")
        code, body = request("127.0.0.1", hao.tcp, "POST", "/xlt/v1/messages", payload, crt, key, member_fp, trust, 8)
        if code != 200 or json.loads(body).get("status") != "duplicate":
            raise Fail(f"重复投递应返回 200 duplicate，实际 {code} {body!r}")
        if len([e for e in hao.inbox_envs() if e["id"] == msg_id]) != 1:
            raise Fail("重复投递后收件箱里多了一封")
        say("同一个消息编号再投一次：对方去重，仍然只有 1 封")

        # 6. 网关没开时用命令行发：进 spool，网关一起来就发出
        nes.crash()
        r = xlt(nes.home, "send", "--to", "haonian", "--type", "notify", "--text", "第二封：我网关没开")
        out = json.loads(r.stdout)
        if out.get("reason") != "DAEMON_DOWN":
            raise Fail("网关没开时应进本机暂存：" + r.stdout)
        nes.start()
        wait_for(lambda: len(hao.inbox_envs()) == 2, 30, "网关启动后，暂存的信没有发出去")
        wait_for(lambda: not nes.pending_files(), 10, "第二封发出后待发箱应清空")
        say("网关没开时发的信先暂存，网关启动后自动发出（收件箱累计 2 封，没有重复）")
        ids = [e["id"] for e in hao.inbox_envs()]
        if len(set(ids)) != len(ids):
            raise Fail("收件箱里有重复编号")
        print("OUTBOX E2E 全部通过")
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
