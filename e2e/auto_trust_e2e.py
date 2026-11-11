#!/usr/bin/env python3
"""三台本机实例：A、B 只做 init 即互发现并互发 notify；C 冒用 B 的名字换新证书，A 替换并记警告；自动登记的成员读文件会被拒绝。"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
WORK = ROOT / "work-auto-trust"
PY = sys.executable
os.environ.setdefault("PYTHONPATH", str(REPO))
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from xlt.crypto import fingerprint_cert, load_cert
from xlt.mtls import request
from xlt.store import Home, member_by_name
from xlt.yamlutil import loads


class Fail(Exception):
    pass


def xlt(home: Path, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO)
    return subprocess.run(
        [PY, "-m", "xlt", "--home", str(home), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
    )


class Proc:
    def __init__(self, name: str, args: list[str]):
        WORK.mkdir(parents=True, exist_ok=True)
        self.name = name
        self.log_path = WORK / f"{name}.out"
        self.log = open(self.log_path, "w", encoding="utf-8")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(REPO)
        self.p = subprocess.Popen(args, stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True, env=env)

    def stop(self) -> None:
        if self.p.poll() is None:
            try:
                os.killpg(self.p.pid, signal.SIGTERM)
            except OSError:
                pass
            try:
                self.p.wait(timeout=4)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.p.pid, signal.SIGKILL)
                except OSError:
                    pass
        try:
            self.log.close()
        except Exception:
            pass


def serve_cmd(home: Path, tcp: int, udp: int, panel: int, *peer_udps: int) -> list[str]:
    args = [
        PY, "-m", "xlt", "--home", str(home), "serve",
        "--bind", "127.0.0.1", "--ip", "127.0.0.1",
        "--tcp-port", str(tcp), "--udp-port", str(udp), "--panel-port", str(panel),
        "--heartbeat", "3", "--no-ip-auto",
    ]
    for udp in peer_udps:
        args.extend(["--announce", f"127.0.0.1:{udp}"])
    return args


def inbox_ids(home: Path) -> set[str]:
    found = set()
    inbox = home / "inbox"
    if not inbox.exists():
        return found
    for path in inbox.rglob("*.env.json"):
        try:
            found.add(json.loads(path.read_text(encoding="utf-8")).get("id") or "")
        except (OSError, json.JSONDecodeError):
            continue
    return found


def wait_member(home: Path, name: str, timeout: float = 12) -> dict:
    deadline = time.time() + timeout
    box = Home(home)
    while time.time() < deadline:
        found = member_by_name(box, name)
        if found:
            return found
        time.sleep(0.2)
    raise Fail(f"{home.name} 在 {timeout}s 内没有自动登记 {name}")


def main() -> int:
    if WORK.exists():
        import shutil
        shutil.rmtree(WORK)
    WORK.mkdir()
    a = WORK / "A"
    b = WORK / "B"
    c = WORK / "C"
    procs: list[Proc] = []
    try:
        for home, me, person, owner in (
            (a, "nes-mac", "nes", "Nes"),
            (b, "haonian-win", "haonian", "浩年"),
        ):
            r = xlt(home, "init", "--me", me, "--person", person, "--owner", owner)
            if r.returncode != 0:
                raise Fail("init 失败: " + r.stderr)
            if "不必再交换证书" not in r.stdout:
                raise Fail("init 仍在要求交换证书: " + r.stdout)
            cfg = json.loads((home / "config.json").read_text(encoding="utf-8"))
            if cfg.get("auto_trust") is not True:
                raise Fail(f"{home.name} auto_trust 默认不是 true: {cfg}")

        skipped = xlt(
            WORK / "manual", "init",
            "--me", "leo-mac", "--person", "leo", "--owner", "沈总",
            "--no-auto-trust",
        )
        if skipped.returncode != 0 or "已关闭自动登记" not in skipped.stdout:
            raise Fail("--no-auto-trust 文案不对: " + skipped.stdout + skipped.stderr)
        man_cfg = json.loads((WORK / "manual" / "config.json").read_text(encoding="utf-8"))
        if man_cfg.get("auto_trust") is not False:
            raise Fail("--no-auto-trust 没有写入 auto_trust=false")

        # 不要执行 xlt trust。只启动 A、B。
        if (a / "xlt-directory.yaml").read_text(encoding="utf-8").count("haonian-win"):
            raise Fail("A 在启动前就已经有 B，测试无效")

        procs.append(Proc("A", serve_cmd(a, 47830, 47831, 47832, 47841)))
        procs.append(Proc("B", serve_cmd(b, 47840, 47841, 47842, 47831)))
        rec_a = wait_member(a, "haonian-win")
        rec_b = wait_member(b, "nes-mac")
        fp_b = fingerprint_cert(load_cert(b / "tls" / "haonian-win.crt"))
        if rec_a.get("cert_sha256", "").upper() != fp_b.upper():
            raise Fail(f"A 登记的指纹不是 B 的证书: {rec_a.get('cert_sha256')} vs {fp_b}")
        if rec_b.get("cert_sha256", "").upper() != fingerprint_cert(load_cert(a / "tls" / "nes-mac.crt")).upper():
            raise Fail("B 登记的指纹不是 A 的证书")

        # 只授予写信权限
        policy = loads((a / "policy.yaml").read_text(encoding="utf-8"))
        grants = [g for g in (policy.get("grants") or []) if "haonian" in str(g.get("grantees") or g)]
        if not grants:
            # ensure_letter_grant 用 person
            text = (a / "policy.yaml").read_text(encoding="utf-8")
            if "g-trust-haonian" not in text or "operations: [ask, notify, reply]" not in text:
                raise Fail("自动登记后没有普通写信授权: " + text)
        else:
            ops = set()
            for g in grants:
                ops.update(g.get("operations") or [])
            if ops - {"ask", "notify", "reply"}:
                raise Fail(f"自动登记授予了额外权限: {ops}")

        sent = xlt(a, "send", "--to", "haonian", "--type", "notify", "--subject", "自检", "--text", "没有手工 trust")
        body = json.loads(sent.stdout or "{}")
        if body.get("status") != "delivered":
            raise Fail("未 trust 的 notify 没有送达: " + sent.stdout + sent.stderr)
        if body.get("id") not in inbox_ids(b):
            raise Fail("B 的信箱里没有这封 notify")
        back = xlt(b, "send", "--to", "nes", "--type", "notify", "--subject", "回声", "--text", "B 也收到了")
        back_body = json.loads(back.stdout or "{}")
        if back_body.get("status") != "delivered":
            raise Fail("B 回给 A 的 notify 没有送达: " + back.stdout + back.stderr)

        # 自动登记的成员请求非写信权限必须被拒
        sample = WORK / "readable.txt"
        sample.write_text("should not be readable by auto-added peer\n", encoding="utf-8")
        trust_pem = (a / "tls" / "nes-mac.crt").read_text(encoding="utf-8")
        expect_fp = fingerprint_cert(load_cert(a / "tls" / "nes-mac.crt"))
        code, raw = request(
            "127.0.0.1", 47830, "POST", "/xlt/v1/files/read",
            json.dumps({"path": str(sample)}).encode("utf-8"),
            b / "tls" / "haonian-win.crt",
            b / "tls" / "haonian-win.key",
            expect_fp,
            trust_pem,
            5,
        )
        parsed = json.loads(raw.decode("utf-8") or "{}")
        if code != 403 or (parsed.get("error") or {}).get("code") != "NO_GRANT":
            raise Fail(f"自动登记成员读文件应是 NO_GRANT，实际 {code} {parsed}")

        # 先停 B，避免它后续心跳把证书再换回去；然后 C 冒用 B 的名字、新证书
        for i, proc in enumerate(list(procs)):
            if proc.name == "B":
                proc.stop()
                procs.pop(i)
                break

        # C 冒用 B 的名字、新证书
        r = xlt(c, "init", "--me", "haonian-win", "--person", "haonian", "--owner", "浩年")
        if r.returncode != 0:
            raise Fail("C init 失败: " + r.stderr)
        fp_c = fingerprint_cert(load_cert(c / "tls" / "haonian-win.crt"))
        if fp_c.upper() == fp_b.upper():
            raise Fail("C 的证书指纹不应和 B 相同")
        procs.append(Proc("C", serve_cmd(c, 47850, 47851, 47852, 47831)))

        deadline = time.time() + 15
        replaced = None
        while time.time() < deadline:
            rec = member_by_name(Home(a), "haonian-win")
            if rec and str(rec.get("cert_sha256") or "").upper() == fp_c.upper():
                replaced = rec
                break
            time.sleep(0.3)
        if replaced is None:
            log = (WORK / "A.out").read_text(encoding="utf-8")[-3000:]
            raise Fail("A 没有用 C 的新证书替换 B\n" + log)

        log = (a / "messenger.log").read_text(encoding="utf-8") if (a / "messenger.log").exists() else ""
        log += "\n" + (WORK / "A.out").read_text(encoding="utf-8")
        if "证书已替换" not in log or fp_b not in log or fp_c not in log:
            raise Fail("messenger.log / 运行日志没有同时写出「证书已替换」和旧/新指纹:\n" + log[-3000:])

        peers = xlt(a, "peers")
        status = xlt(a, "status")
        shown = (peers.stdout or "") + (status.stdout or "")
        if "证书已替换" not in shown or fp_b not in shown or fp_c not in shown:
            raise Fail("xlt peers/status 没有显示替换警告和旧/新指纹:\n" + shown)

        print("ok: A 与 B 只做 init 即互发现，并交换 notify")
        print("ok: 自动登记只授予 ask/notify/reply，读文件被拒绝（NO_GRANT）")
        print("ok: C 冒用 B 的名字后，A 替换证书并记下「证书已替换」警告")
        return 0
    finally:
        for proc in procs:
            proc.stop()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Fail as exc:
        print("失败：", exc, file=sys.stderr)
        sys.exit(1)
