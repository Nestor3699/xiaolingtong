#!/usr/bin/env python3
"""一对新家目录：只做 init + 双向 trust（不手改政策），应能互发 notify。"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "work-trust-grant"
PY = sys.executable


class Fail(Exception):
    pass


def xlt(home: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([PY, "-m", "xlt", "--home", str(home), *args], capture_output=True, text=True)


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


class Proc:
    def __init__(self, name: str, args: list[str]):
        WORK.mkdir(parents=True, exist_ok=True)
        self.log_path = WORK / f"{name}.out"
        self.log = open(self.log_path, "w", encoding="utf-8")
        self.p = subprocess.Popen(args, stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True)

    def stop(self) -> None:
        if self.p.poll() is not None:
            return
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
        self.log.close()


def main() -> int:
    if WORK.exists():
        import shutil
        shutil.rmtree(WORK)
    WORK.mkdir()
    nes = WORK / "nes"
    hao = WORK / "hao"
    procs: list[Proc] = []
    try:
        for home, me, person, owner in (
            (nes, "nes-mac", "nes", "Nes"),
            (hao, "haonian-win", "haonian", "浩年"),
        ):
            r = xlt(home, "init", "--me", me, "--person", person, "--owner", owner)
            if r.returncode != 0:
                raise Fail("init 失败: " + r.stderr)
            if "可以发给同事" not in r.stdout or "团队群" not in r.stdout:
                raise Fail("init 文案未改为交给同事/团队群: " + r.stdout)
            if "交给 Nes 登记" in r.stdout:
                raise Fail("init 仍在让人把证书交给 Nes: " + r.stdout)
        t1 = xlt(
            nes, "trust", "--name", "haonian-win", "--person", "haonian", "--owner", "浩年",
            "--os", "windows", "--port", "47620", "--crt", str(hao / "tls" / "haonian-win.crt"), "--primary",
        )
        t2 = xlt(
            hao, "trust", "--name", "nes-mac", "--person", "nes", "--owner", "Nes",
            "--os", "macos", "--port", "47610", "--crt", str(nes / "tls" / "nes-mac.crt"), "--primary",
        )
        if t1.returncode != 0 or t2.returncode != 0:
            raise Fail("trust 失败: " + (t1.stderr or "") + (t2.stderr or ""))
        if "已授予 haonian 普通写信权限（ask / notify / reply）" not in t1.stdout:
            raise Fail("trust 未打印授予内容: " + t1.stdout)
        if "已授予 nes 普通写信权限（ask / notify / reply）" not in t2.stdout:
            raise Fail("trust 未打印授予内容: " + t2.stdout)
        for home, who in ((nes, "haonian"), (hao, "nes")):
            policy = (home / "policy.yaml").read_text(encoding="utf-8")
            if f"g-trust-{who}" not in policy or "operations: [ask, notify, reply]" not in policy:
                raise Fail(f"{home.name} 政策里没有自动授权: " + policy)
            if "只有本机主人能改" not in policy or "rate_per_minute: 30" not in policy:
                raise Fail(f"{home.name} 手写模板注释或其它字段被改丢了")
        again = xlt(
            hao, "trust", "--name", "nes-mac", "--person", "nes", "--owner", "Nes",
            "--os", "macos", "--port", "47610", "--crt", str(nes / "tls" / "nes-mac.crt"), "--primary",
        )
        if "已有普通写信权限" not in again.stdout:
            raise Fail("再次 trust 应是幂等: " + again.stdout)
        if (hao / "policy.yaml").read_text(encoding="utf-8").count("g-trust-nes") != 1:
            raise Fail("再次 trust 重复写了授权")

        # --no-grant 不应写入授权；grant-trusted 再补上
        skipped = xlt(
            nes, "trust", "--name", "haonian-win", "--person", "haonian", "--owner", "浩年",
            "--os", "windows", "--port", "47620", "--crt", str(hao / "tls" / "haonian-win.crt"),
            "--primary", "--no-grant",
        )
        if skipped.returncode != 0 or "跳过授权" not in skipped.stdout:
            raise Fail("--no-grant 输出不对: " + skipped.stdout + skipped.stderr)
        # 此时 haonian 的授权已在，--no-grant 只是这次不改。另建一个第三者来测跳过。
        leo = WORK / "leo"
        r = xlt(leo, "init", "--me", "leo-mac", "--person", "leo", "--owner", "沈总")
        if r.returncode != 0:
            raise Fail("leo init 失败: " + r.stderr)
        skip = xlt(
            hao, "trust", "--name", "leo-mac", "--person", "leo", "--owner", "沈总",
            "--os", "macos", "--crt", str(leo / "tls" / "leo-mac.crt"), "--primary", "--no-grant",
        )
        if "跳过授权" not in skip.stdout:
            raise Fail("对 leo --no-grant 失败: " + skip.stdout)
        if "g-trust-leo" in (hao / "policy.yaml").read_text(encoding="utf-8"):
            raise Fail("--no-grant 仍然写了 leo 的授权")
        filled = xlt(hao, "policy", "grant-trusted")
        if filled.returncode != 0 or "leo" not in filled.stdout:
            raise Fail("grant-trusted 未补上 leo: " + filled.stdout + filled.stderr)
        if "g-trust-leo" not in (hao / "policy.yaml").read_text(encoding="utf-8"):
            raise Fail("grant-trusted 没有写入 g-trust-leo")
        again2 = xlt(hao, "policy", "grant-trusted")
        if "都已有普通写信权限" not in again2.stdout:
            raise Fail("grant-trusted 应幂等: " + again2.stdout)

        def serve(name: str, home: Path, tcp: int, udp: int, panel: int, peer_udp: int) -> Proc:
            return Proc(
                name,
                [
                    PY, "-m", "xlt", "--home", str(home), "serve",
                    "--bind", "127.0.0.1", "--ip", "127.0.0.1",
                    "--tcp-port", str(tcp), "--udp-port", str(udp), "--panel-port", str(panel),
                    "--heartbeat", "3", "--no-ip-auto",
                    "--announce", f"127.0.0.1:{peer_udp}",
                ],
            )

        procs.append(serve("nes", nes, 47610, 47611, 47612, 47621))
        procs.append(serve("hao", hao, 47620, 47621, 47622, 47611))
        deadline = time.time() + 10
        while time.time() < deadline:
            cache = nes / "peers.cache.json"
            if cache.exists() and "online" in cache.read_text(encoding="utf-8"):
                break
            time.sleep(0.2)
        else:
            raise Fail("两台通讯员没有互相发现")

        sent = xlt(nes, "send", "--to", "haonian", "--type", "notify", "--subject", "自检", "--text", "只做了 init 和 trust")
        body = json.loads(sent.stdout or "{}")
        if body.get("status") != "delivered":
            raise Fail("notify 没有送达: " + sent.stdout + sent.stderr)
        if body.get("id") not in inbox_ids(hao):
            raise Fail("浩年信箱里没有这封 notify")
        print("ok: 一对新家目录在只做 init + 双向 trust 后互发 notify 成功")
        print("ok: --no-grant / 再次 trust / policy grant-trusted 行为符合预期")
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
