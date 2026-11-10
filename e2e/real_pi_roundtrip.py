#!/usr/bin/env python3
"""本机真 pi 往返：两台通讯员 + 扩展，优先非交互 rpc，不行再 pi -p。只杀自己拉起的进程。"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path.home() / "xiaolingtong"
PY = ROOT / ".venv" / "bin" / "python"
小灵通 = ROOT / ".venv" / "bin" / "xlt"
EXT = ROOT / "xlt" / "share" / "xlt-pi-extension.ts"
WORK = ROOT / "e2e" / "work-real-pi"
LOG = ROOT / "e2e" / "mac-real-pi.log"
NES_TCP, NES_UDP, NES_PANEL = 47510, 47511, 47512
HAO_TCP, HAO_UDP, HAO_PANEL = 47520, 47521, 47522
MODEL = "deepseek/deepseek-flash"
LETTER = "请回复：收到"


class Fail(Exception):
    pass


def log(msg: str) -> None:
    line = msg
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def xlt(home: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(小灵通), "--home", str(home), *args],
        capture_output=True,
        text=True,
    )


def inbox_envs(home: Path) -> list[dict]:
    rows = []
    inbox = home / "inbox"
    if not inbox.exists():
        return rows
    for path in inbox.rglob("*.env.json"):
        try:
            env = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        env["_path"] = str(path)
        rows.append(env)
    return rows


class Proc:
    def __init__(self, name: str, args: list[str], env: dict | None = None, cwd: str | None = None):
        self.name = name
        self.log_path = WORK / f"{name}.out"
        self.log = open(self.log_path, "w", encoding="utf-8")
        self.p = subprocess.Popen(
            args,
            stdout=self.log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )

    def stop(self) -> None:
        if self.p.poll() is not None:
            return
        try:
            os.killpg(self.p.pid, signal.SIGTERM)
        except OSError:
            return
        try:
            self.p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(self.p.pid, signal.SIGKILL)
            except OSError:
                pass
            self.p.wait(timeout=3)


def wait_online(nes: Path, hao: Path, seconds: float = 12) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        def state(home: Path, name: str) -> str:
            path = home / "peers.cache.json"
            if not path.exists():
                return ""
            try:
                row = (json.loads(path.read_text(encoding="utf-8")).get("peers") or {}).get(name) or {}
            except (OSError, json.JSONDecodeError):
                return ""
            return row.get("state") or ""

        if state(nes, "haonian-win") == "online" and state(hao, "nes-mac") == "online":
            return
        time.sleep(0.2)
    raise Fail("两台通讯员没有互相发现")


def wait_file(path: Path, seconds: float = 10) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if path.exists() and path.stat().st_size > 0:
            return
        time.sleep(0.2)
    raise Fail(f"没有等到文件 {path}")


def ext_log_text(hao: Path) -> str:
    path = hao / "bridges" / "pi" / "ext.log"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")


def wait_reply(nes: Path, msg_id: str, seconds: float) -> dict:
    deadline = time.time() + seconds
    while time.time() < deadline:
        for item in inbox_envs(nes):
            if item.get("in_reply_to") == msg_id:
                return item
        time.sleep(0.4)
    raise Fail("Nes 没有收到 pi 的回信")


def start_pi(hao: Path, mode: str) -> Proc:
    workdir = hao / "workdir" / "pi-real"
    sessions = workdir / "sessions"
    workdir.mkdir(parents=True, exist_ok=True)
    sessions.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["XLT_ROOT"] = str(hao / "bridges" / "pi")
    env["XLT_HOME"] = str(hao)
    env["XLT_BIN"] = str(小灵通)
    env["PATH"] = str(ROOT / ".venv" / "bin") + os.pathsep + env.get("PATH", "")
    env["TMPDIR"] = str(workdir / "tmp")
    Path(env["TMPDIR"]).mkdir(parents=True, exist_ok=True)
    args = [
        "pi",
        "-e",
        str(EXT),
        "--model",
        MODEL,
        "--no-extensions",
        "--no-mcp",
        "--no-skills",
        "--no-prompt-templates",
        "--no-context-files",
        "--no-builtin-tools",
        "--session-dir",
        str(sessions),
        "--session-id",
        "xlt-real-mac",
    ]
    if mode == "rpc":
        args[1:1] = ["--mode", "rpc"]
    elif mode == "print":
        args[1:1] = ["-p"]
        args.extend(["请处理收件箱里的同事来信，只用 xlt_reply 工具按信纸要求用最短中文回复，不要做其他事。"])
    else:
        raise Fail("unknown pi mode")
    return Proc("pi-" + mode, args, env=env, cwd=str(workdir))


def main() -> int:
    LOG.write_text(f"===== {time.strftime('%Y-%m-%d %H:%M:%S %Z')} real pi round-trip =====\n", encoding="utf-8")
    if not PY.exists() or not 小灵通.exists() or not EXT.exists():
        log("失败  缺少 venv 或 pi 扩展文件")
        return 1
    if WORK.exists():
        subprocess.run(["rm", "-rf", str(WORK)], check=True)
    WORK.mkdir()
    nes, hao = WORK / "nes", WORK / "hao"
    procs: list[Proc] = []
    used_path = ""
    try:
        for home, me, person, owner, os_name in (
            (nes, "nes-mac", "nes", "Nes", "macos"),
            (hao, "haonian-win", "haonian", "浩年", "windows"),
        ):
            result = xlt(home, "init", "--me", me, "--person", person, "--owner", owner)
            if result.returncode != 0:
                raise Fail("init 失败: " + (result.stderr or result.stdout))
        t1 = xlt(
            nes, "trust", "--name", "haonian-win", "--person", "haonian", "--owner", "浩年",
            "--os", "windows", "--port", str(HAO_TCP),
            "--crt", str(hao / "tls" / "haonian-win.crt"), "--primary",
        )
        t2 = xlt(
            hao, "trust", "--name", "nes-mac", "--person", "nes", "--owner", "Nes",
            "--os", "macos", "--port", str(NES_TCP),
            "--crt", str(nes / "tls" / "nes-mac.crt"), "--primary",
        )
        if t1.returncode != 0 or t2.returncode != 0:
            raise Fail("trust 失败: " + (t1.stderr or "") + (t2.stderr or ""))
        (hao / "policy.yaml").write_text(
            "version: 1\nowner: haonian\ndefault: deny\nmode: normal\n"
            "grants:\n  - id: g1\n    grantees: [nes]\n    operations: [ask, notify]\n",
            encoding="utf-8",
        )
        r1 = xlt(hao, "register", "--agent", "pi", "--window", "main", "--delivery", "extension")
        if r1.returncode != 0:
            raise Fail("register 失败: " + (r1.stderr or r1.stdout))
        (hao / "routes.yaml").write_text(
            "version: 1\ndefault: pi/main\non_window_lost: fallback_default\nbusy_timeout_s: 600\nrules: []\n",
            encoding="utf-8",
        )
        for name, home, tcp, udp, panel, peer in (
            ("nes", nes, NES_TCP, NES_UDP, NES_PANEL, f"127.0.0.1:{HAO_UDP}"),
            ("hao", hao, HAO_TCP, HAO_UDP, HAO_PANEL, f"127.0.0.1:{NES_UDP}"),
        ):
            procs.append(Proc(name, [
                str(小灵通), "--home", str(home), "serve",
                "--bind", "127.0.0.1", "--ip", "127.0.0.1",
                "--tcp-port", str(tcp), "--udp-port", str(udp), "--panel-port", str(panel),
                "--heartbeat", "3", "--no-ip-auto",
                "--announce", peer,
            ]))
        wait_online(nes, hao)
        log("通过  两台本机通讯员已互相发现")

        # Path 1: long-running non-interactive rpc + extension watcher
        pi_proc = start_pi(hao, "rpc")
        procs.append(pi_proc)
        deadline = time.time() + 15
        while time.time() < deadline:
            if "session_start: watching" in ext_log_text(hao):
                break
            if pi_proc.p.poll() is not None:
                tail = pi_proc.log_path.read_text(encoding="utf-8", errors="ignore")[-2000:]
                log("pi --mode rpc 提前退出：\n" + tail)
                break
            time.sleep(0.2)
        rpc_ready = "session_start: watching" in ext_log_text(hao)
        if rpc_ready:
            log("通过  pi --mode rpc 已加载扩展并开始监听收件箱")
            used_path = "extension + pi --mode rpc"
        else:
            log("说明  pi --mode rpc 没有进入扩展监听，改用 pi -p")
            pi_proc.stop()
            if pi_proc in procs:
                procs.remove(pi_proc)

        sent = xlt(nes, "send", "--to", "haonian", "--type", "ask", "--subject", "本机真pi测试", "--text", LETTER)
        body = json.loads(sent.stdout or "{}")
        if body.get("status") != "delivered":
            raise Fail("信没送到：" + (sent.stdout or "") + (sent.stderr or ""))
        msg_id = body["id"]
        log(f"通过  信已送到 haonian，id={msg_id}")

        inbox_letter = hao / "bridges" / "pi" / "inbox" / f"{msg_id}.letter.md"
        taken_letter = hao / "bridges" / "pi" / "inbox-taken" / f"{msg_id}.letter.md"
        # either still in inbox or already claimed
        deadline = time.time() + 8
        while time.time() < deadline:
            if inbox_letter.exists() or taken_letter.exists():
                break
            time.sleep(0.2)
        else:
            raise Fail("通讯员没有把信镜像到 pi 收件箱")
        log("通过  信已镜像到 pi 扩展收件箱")

        if not rpc_ready:
            # Path 2: one-shot non-interactive pi -p, extension scans inbox on session_start
            pi_proc = start_pi(hao, "print")
            procs.append(pi_proc)
            used_path = "extension + pi -p"
            try:
                pi_proc.p.wait(timeout=90)
            except subprocess.TimeoutExpired:
                log("说明  pi -p 90 秒仍未退出")

        try:
            reply = wait_reply(nes, msg_id, 60 if rpc_ready else 20)
        except Fail:
            if not rpc_ready:
                raise
            log("说明  rpc 监听已启动但 60 秒内没有回信，改用 pi -p")
            pi_proc.stop()
            if pi_proc in procs:
                procs.remove(pi_proc)
            pi_proc = start_pi(hao, "print")
            procs.append(pi_proc)
            used_path = "extension + pi -p (rpc 无回信后回退)"
            try:
                pi_proc.p.wait(timeout=90)
            except subprocess.TimeoutExpired:
                log("说明  pi -p 90 秒仍未退出")
            reply = wait_reply(nes, msg_id, 20)
        letter_path = Path(reply["_path"]).with_name(reply["id"] + ".letter.md")
        text = letter_path.read_text(encoding="utf-8") if letter_path.exists() else ""
        ext = ext_log_text(hao)
        via_tool = f"xlt_reply {msg_id}" in ext
        if "收到" not in text:
            raise Fail(f"回信里没有「收到」。path={used_path} text={text!r}")
        if not via_tool:
            raise Fail(f"回信到了，但 ext.log 里没有 xlt_reply 调用。path={used_path} text={text!r}\n{ext}")
        log(f"通过  真 pi 经 xlt_reply / xlt reply 回信成功（{used_path}）")
        log(f"回信摘要：{text.strip()[:80]}")
        return 0
    except Fail as exc:
        log("失败  " + str(exc))
        ext = ext_log_text(hao) if hao.exists() else ""
        if ext:
            log("扩展日志末尾：\n" + "\n".join(ext.splitlines()[-30:]))
        for proc in procs:
            if proc.log_path.exists():
                tail = proc.log_path.read_text(encoding="utf-8", errors="ignore").splitlines()[-40:]
                log(f"## {proc.name} 输出末尾\n" + "\n".join(tail))
        return 1
    finally:
        for proc in procs:
            proc.stop()
        leftover = []
        try:
            out = subprocess.run(
                ["pgrep", "-fl", "xlt-real-mac|work-real-pi|xiaolingtong/.venv/bin/xlt"],
                capture_output=True, text=True,
            ).stdout
            for line in out.splitlines():
                if "work-real-pi" in line or "xlt-real-mac" in line:
                    leftover.append(line)
        except Exception:
            pass
        if leftover:
            log("警告  清理后仍看到本测试相关进程：\n" + "\n".join(leftover))
        else:
            log("已结束并清理本测试拉起的进程")


if __name__ == "__main__":
    raise SystemExit(main())
