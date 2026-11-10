#!/usr/bin/env python3
"""只测 pi 的 xlt_reply：正式编号（带连字符）要能过，并且回信走 xlt reply。"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "xlt" / "share" / "xlt-pi-extension.ts"
WORK = ROOT / "e2e" / "work-pi"
PY = sys.executable
小灵通 = str(Path(PY).with_name("xlt"))
REAL_ID = "msg-20261110-120000-nes-mac-0001"


def fail(msg: str) -> None:
    print("失败", msg)
    raise SystemExit(1)


def main() -> int:
    src = EXT.read_text(encoding="utf-8")
    if "/^msg-[0-9A-Za-z-]+$/" not in src:
        fail("扩展里没有接受连字符的编号规则")
    if "writeFileSync" in src and ".reply.md" in src:
        fail("xlt_reply 还在自己写回信文件")
    if '["--home", home, "reply", params.msg_id, "--text", params.text]' not in src:
        fail("xlt_reply 没有调用 xlt reply")
    check = subprocess.run(
        ["node", "-e", "const re=/^msg-[0-9A-Za-z-]+$/; const id=process.argv[1]; if(!re.test(id)||re.test('msg-')||re.test('hello')) process.exit(1)", REAL_ID],
        capture_output=True,
        text=True,
    )
    if check.returncode != 0:
        fail("编号规则没有放过正式 id")
    print("通过  正式编号能过，坏编号不能过")

    if WORK.exists():
        subprocess.run(["rm", "-rf", str(WORK)], check=True)
    WORK.mkdir()
    nes, hao = WORK / "nes", WORK / "hao"
    procs = []

    def xlt(home: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([小灵通, "--home", str(home), *args], capture_output=True, text=True)

    def start(name: str, home: Path, tcp: int, udp: int, panel: int, peer_udp: int) -> None:
        log = open(WORK / f"{name}.out", "w", encoding="utf-8")
        procs.append(subprocess.Popen(
            [小灵通, "--home", str(home), "serve", "--bind", "127.0.0.1", "--ip", "127.0.0.1",
             "--tcp-port", str(tcp), "--udp-port", str(udp), "--panel-port", str(panel),
             "--heartbeat", "30", "--no-ip-auto", "--announce", f"127.0.0.1:{peer_udp}"],
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        ))

    try:
        for home, me, person in ((nes, "nes-mac", "nes"), (hao, "haonian-win", "haonian")):
            result = xlt(home, "init", "--me", me, "--person", person, "--owner", person)
            if result.returncode != 0:
                fail("init " + result.stderr)
        xlt(nes, "trust", "--name", "haonian-win", "--person", "haonian", "--os", "windows", "--port", "47420", "--crt", str(hao / "tls" / "haonian-win.crt"), "--primary")
        xlt(hao, "trust", "--name", "nes-mac", "--person", "nes", "--os", "macos", "--port", "47410", "--crt", str(nes / "tls" / "nes-mac.crt"), "--primary")
        (hao / "policy.yaml").write_text(
            "version: 1\nowner: haonian\ndefault: deny\nmode: normal\ngrants:\n  - id: g1\n    grantees: [nes]\n    operations: [ask, notify]\n",
            encoding="utf-8",
        )
        xlt(hao, "register", "--agent", "pi", "--window", "main", "--delivery", "extension")
        xlt(nes, "register", "--agent", "pi", "--window", "main", "--delivery", "extension")
        (hao / "routes.yaml").write_text("version: 1\ndefault: pi/main\non_window_lost: fallback_default\nbusy_timeout_s: 600\nrules: []\n", encoding="utf-8")
        (nes / "routes.yaml").write_text("version: 1\ndefault: pi/main\non_window_lost: fallback_default\nbusy_timeout_s: 600\nrules: []\n", encoding="utf-8")
        start("nes", nes, 47410, 47411, 47412, 47421)
        start("hao", hao, 47420, 47421, 47422, 47411)
        deadline = time.time() + 8
        while time.time() < deadline:
            cache = nes / "peers.cache.json"
            if cache.exists() and "online" in cache.read_text(encoding="utf-8"):
                break
            time.sleep(0.2)
        else:
            fail("两台没有互相发现")
        sent = xlt(nes, "send", "--to", "haonian", "--type", "ask", "--subject", "pi回信", "--text", "请回我一句", "--origin-window", "main")
        body = json.loads(sent.stdout or "{}")
        if body.get("status") != "delivered":
            fail("信没送到：" + sent.stdout + sent.stderr)
        msg_id = body["id"]
        if "-" not in msg_id.split("msg-", 1)[-1]:
            fail("通讯员没有发出带连字符的正式编号")
        env = {
            "XLT_HOME": str(hao),
            "XLT_BIN": 小灵通,
            "XLT_ROOT": str(hao / "bridges" / "pi"),
        }
        node = r"""
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");
const msgId = process.argv[1];
const text = process.argv[2];
if (!/^msg-[0-9A-Za-z-]+$/.test(msgId)) process.exit(2);
const ROOT = process.env.XLT_ROOT;
const home = process.env.XLT_HOME || (fs.existsSync(path.join(ROOT, "config.json")) ? ROOT : path.resolve(ROOT, "..", ".."));
const bin = process.env.XLT_BIN || "xlt";
const run = spawnSync(bin, ["--home", home, "reply", msgId, "--text", text], { encoding: "utf-8" });
process.stdout.write(run.stdout || "");
process.exit(run.status ?? 1);
"""
        replied = subprocess.run(["node", "-e", node, msg_id, "收到，已处理"], capture_output=True, text=True, env={**os.environ, **env})
        parsed = json.loads(replied.stdout or "{}")
        if replied.returncode != 0 or parsed.get("status") not in ("delivered", "queued"):
            fail("xlt reply 没有把回信送出：" + replied.stdout + replied.stderr)
        deadline = time.time() + 6
        while time.time() < deadline:
            inbox = list((nes / "inbox").rglob("*.env.json"))
            if any(json.loads(p.read_text(encoding="utf-8")).get("in_reply_to") == msg_id for p in inbox):
                break
            time.sleep(0.2)
        else:
            fail("Nes 没有收到这条回信")
        print("通过  带连字符的编号经 xlt reply 送到了发信人")
        blocked = subprocess.run(["node", "-e", node, msg_id, "-----BEGIN PRIVATE KEY-----\nMIIE\n-----END PRIVATE KEY-----"], capture_output=True, text=True, env={**os.environ, **env})
        blocked_body = json.loads(blocked.stdout or "{}")
        if blocked.returncode == 0 or blocked_body.get("reason") != "EGRESS":
            fail("私钥回信没有被通讯员拦住：" + blocked.stdout)
        print("通过  走 xlt reply 时，私钥回信被出口检查拦住")
        return 0
    finally:
        for proc in procs:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                except OSError:
                    pass
        for proc in procs:
            try:
                proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)


if __name__ == "__main__":
    raise SystemExit(main())
