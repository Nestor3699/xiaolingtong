#!/usr/bin/env python3
"""两台本机实例：发小文本、二进制、约 1GB 稀疏文件；中途断开后续传，并核对哈希。"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
WORK = ROOT / "work-attach"
PY = sys.executable
os.environ.setdefault("PYTHONPATH", str(REPO))
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


class Fail(Exception):
    pass


def xlt(home: Path, *args: str, env: dict | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    merged["PYTHONPATH"] = str(REPO)
    return subprocess.run(
        [PY, "-m", "xlt", "--home", str(home), *args],
        capture_output=True,
        text=True,
        env=merged,
        timeout=timeout,
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            piece = fh.read(1024 * 1024)
            if not piece:
                break
            digest.update(piece)
    return digest.hexdigest().upper()


def colon_sha(path: Path) -> str:
    raw = bytes.fromhex(sha256_file(path))
    return ":".join(f"{b:02X}" for b in raw)


def inbox_letters(home: Path) -> list[Path]:
    inbox = home / "inbox"
    if not inbox.exists():
        return []
    return sorted(inbox.rglob("*.letter.md"))


def find_env(home: Path, msg_id: str) -> dict | None:
    for path in (home / "inbox").rglob(f"{msg_id}.env.json"):
        return json.loads(path.read_text(encoding="utf-8"))
    for path in (home / "pending").rglob(f"{msg_id}.env.json"):
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def latest_msg_id(home: Path) -> str:
    ids = []
    root = home / "inbox"
    if not root.exists():
        return ""
    for path in root.rglob("*.env.json"):
        try:
            ids.append(json.loads(path.read_text(encoding="utf-8")).get("id") or "")
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(ids)[-1] if ids else ""


def attach_dir(home: Path, msg_id: str) -> Path:
    return home / "inbox" / msg_id / "attachments"


def part_files(home: Path, msg_id: str) -> list[Path]:
    folder = attach_dir(home, msg_id)
    if not folder.exists():
        return []
    return list(folder.glob("*.part"))


class Proc:
    def __init__(self, name: str, args: list[str], env: dict | None = None):
        WORK.mkdir(parents=True, exist_ok=True)
        self.name = name
        self.log_path = WORK / f"{name}.out"
        self.log = open(self.log_path, "w", encoding="utf-8")
        merged = os.environ.copy()
        if env:
            merged.update(env)
        merged["PYTHONPATH"] = str(REPO)
        self.p = subprocess.Popen(args, stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True, env=merged)

    def stop(self) -> None:
        if self.p.poll() is not None:
            try:
                self.log.close()
            except Exception:
                pass
            return
        try:
            os.killpg(self.p.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            self.p.wait(timeout=4)
        except subprocess.TimeoutExpired:
            pass
        try:
            self.log.close()
        except Exception:
            pass


def make_sparse(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        fh.write(b"START-SPARSE-HEAD:" + os.urandom(4096) + b"\n")
        mid = size // 2
        fh.seek(mid)
        fh.write(b"MID-SPARSE-MARK:" + os.urandom(4096) + b"\n")
        fh.seek(size - 8192)
        fh.write(b"END-SPARSE-TAIL:" + os.urandom(4096) + b"\n")
        fh.seek(size - 1)
        fh.write(b"X")


def serve_cmd(home: Path, tcp: int, udp: int, panel: int, peer_udp: int) -> list[str]:
    return [
        PY, "-m", "xlt", "--home", str(home), "serve",
        "--bind", "127.0.0.1", "--ip", "127.0.0.1",
        "--tcp-port", str(tcp), "--udp-port", str(udp), "--panel-port", str(panel),
        "--heartbeat", "3", "--no-ip-auto",
        "--announce", f"127.0.0.1:{peer_udp}",
    ]


def main() -> int:
    if WORK.exists():
        import shutil
        shutil.rmtree(WORK)
    WORK.mkdir()
    nes = WORK / "nes"
    hao = WORK / "hao"
    files = WORK / "files"
    files.mkdir()
    env_slow = {
        "XLT_ATTACH_CHUNK": str(1024 * 1024),
        "XLT_ATTACH_SLEEP": "0.05",
        "PYTHONPATH": str(REPO),
    }
    procs: list[Proc] = []
    send_proc = None
    try:
        for home, me, person, owner in (
            (nes, "nes-mac", "nes", "Nes"),
            (hao, "haonian-win", "haonian", "浩年"),
        ):
            r = xlt(home, "init", "--me", me, "--person", person, "--owner", owner)
            if r.returncode != 0:
                raise Fail("init 失败: " + r.stderr)
        t1 = xlt(
            nes, "trust", "--name", "haonian-win", "--person", "haonian", "--owner", "浩年",
            "--os", "windows", "--port", "47720", "--crt", str(hao / "tls" / "haonian-win.crt"), "--primary",
        )
        t2 = xlt(
            hao, "trust", "--name", "nes-mac", "--person", "nes", "--owner", "Nes",
            "--os", "macos", "--port", "47710", "--crt", str(nes / "tls" / "nes-mac.crt"), "--primary",
        )
        if t1.returncode != 0 or t2.returncode != 0:
            raise Fail("trust 失败: " + (t1.stderr or "") + (t2.stderr or ""))

        # 出口检查：敏感文件必须被拒
        secret = files / "dummy.key"
        secret.write_text("-----BEGIN PRIVATE KEY-----\nMII\n-----END PRIVATE KEY-----\n", encoding="utf-8")
        blocked = xlt(nes, "send", "--to", "haonian", "--type", "notify", "--text", "不该发出去", "--attach", str(secret))
        body = json.loads(blocked.stdout or "{}")
        if body.get("status") != "rejected":
            # daemon 还没起来时会先在 CLI 侧拦截
            raise Fail("敏感 .key 没有被拒绝: " + blocked.stdout + blocked.stderr)
        if "黑名单" not in (body.get("message") or "") and "敏感" not in (body.get("message") or "") and "key" not in (body.get("message") or "").lower():
            raise Fail("敏感文件拒绝原因不清楚: " + blocked.stdout)
        envf = files / ".env"
        envf.write_text("SECRET_KEY=super-secret\n", encoding="utf-8")
        blocked2 = xlt(nes, "send", "--to", "haonian", "--type", "notify", "--text", "不该发出去", "--attach", str(envf))
        body2 = json.loads(blocked2.stdout or "{}")
        if body2.get("status") != "rejected":
            raise Fail(".env 没有被拒绝: " + blocked2.stdout)

        text = files / "note.txt"
        text.write_text("hello from nes, 这是文本附件\n", encoding="utf-8")
        binary = files / "blob.bin"
        binary.write_bytes(bytes(range(256)) * 64 + os.urandom(4096))
        big = files / "big.bin"
        size = 1024 * 1024 * 1024
        make_sparse(big, size)
        if big.stat().st_size != size:
            raise Fail(f"大文件大小不对: {big.stat().st_size}")
        want = {p.name: colon_sha(p) for p in (text, binary, big)}

        procs.append(Proc("nes", serve_cmd(nes, 47710, 47711, 47712, 47721), env_slow))
        procs.append(Proc("hao", serve_cmd(hao, 47720, 47721, 47722, 47711), env_slow))
        deadline = time.time() + 12
        while time.time() < deadline:
            cache = nes / "peers.cache.json"
            if cache.exists() and "online" in cache.read_text(encoding="utf-8"):
                break
            time.sleep(0.2)
        else:
            raise Fail("两台通讯员没有互相发现\n" + (WORK / "nes.out").read_text(encoding="utf-8")[-2000:])

        # 小文本 + 二进制，一次送达
        sent = xlt(
            nes, "send", "--to", "haonian", "--type", "notify",
            "--subject", "小附件", "--text", "请看两个附件",
            "--attach", str(text), "--attach", str(binary),
            env=env_slow, timeout=60,
        )
        body = json.loads(sent.stdout or "{}")
        if body.get("status") != "delivered":
            raise Fail("小附件没有送达: " + sent.stdout + sent.stderr)
        msg_small = body.get("id") or latest_msg_id(hao)
        dest = attach_dir(hao, msg_small)
        for name in ("note.txt", "blob.bin"):
            got = dest / name
            if not got.is_file():
                raise Fail(f"找不到收件 {name}，目录是 {list(dest.glob('*')) if dest.exists() else dest}")
            if colon_sha(got) != want[name]:
                raise Fail(f"{name} 哈希不一致")
        letter = None
        for path in inbox_letters(hao):
            if msg_small in path.name:
                letter = path.read_text(encoding="utf-8")
                break
        if not letter or "附件" not in letter or "note.txt" not in letter or str(dest / "note.txt") not in letter:
            raise Fail("信纸没有列出附件名和本机路径: " + (letter or ""))

        # 1GB + 中途断开
        before = set()
        if (hao / "inbox").exists():
            before = {p.stem for p in (hao / "inbox").rglob("*.env.json")}
        send_proc = Proc(
            "send-big",
            [
                PY, "-m", "xlt", "--home", str(nes), "send",
                "--to", "haonian", "--type", "notify",
                "--subject", "大文件", "--text", "约 1GB",
                "--attach", str(big),
            ],
            env_slow,
        )
        # 等出发并出现 part
        part = None
        msg_big = ""
        deadline = time.time() + 30
        while time.time() < deadline:
            if (hao / "inbox").exists():
                for env_path in (hao / "inbox").rglob("*.env.json"):
                    data = json.loads(env_path.read_text(encoding="utf-8"))
                    mid = data.get("id") or ""
                    if mid and mid not in before and data.get("attachments"):
                        msg_big = mid
                        parts = part_files(hao, mid)
                        if parts:
                            part = parts[0]
                            if part.stat().st_size >= 2 * 1024 * 1024:
                                break
            if part and part.stat().st_size >= 2 * 1024 * 1024:
                break
            time.sleep(0.05)
        else:
            logs = (WORK / "hao.out").read_text(encoding="utf-8")[-2000:]
            raise Fail(f"等不到大文件的部分写入 msg={msg_big} part={part}\n{logs}")

        # 杀掉接收方，强制中断
        recv = procs.pop()  # hao is last
        if recv.name != "hao":
            # find hao
            for i, proc in enumerate(procs):
                if proc.name == "hao":
                    recv = procs.pop(i)
                    break
        grew = part.stat().st_size
        recv.stop()
        # 等发送方结束
        deadline = time.time() + 30
        while time.time() < deadline and send_proc.p.poll() is None:
            time.sleep(0.2)
        if send_proc.p.poll() is None:
            send_proc.stop()
            raise Fail("发送进程在接收方被杀掉后没有退出")
        send_out = (WORK / "send-big.out").read_text(encoding="utf-8")
        try:
            send_body = json.loads(send_out.strip().splitlines()[-1])
        except Exception:
            send_body = {}
        if send_body.get("status") == "delivered":
            raise Fail(f"中途断开后不应该已经 delivered（part 才 {grew} 字节）: {send_out}")
        if not msg_big:
            msg_big = send_body.get("id") or ""
        if part.exists() and part.stat().st_size >= size:
            raise Fail("杀掉接收方之前文件已经收完，无法测续传")
        # 待发箱应还在
        pending = list((nes / "pending").rglob(f"{msg_big}.env.json")) if msg_big else list((nes / "pending").rglob("*.env.json"))
        if not pending:
            raise Fail("中断后待发箱里没有这封信: " + send_out)

        # 重启接收方，等待续传完成
        procs.append(Proc("hao", serve_cmd(hao, 47720, 47721, 47722, 47711), env_slow))
        deadline = time.time() + 90
        dest_big = attach_dir(hao, msg_big) / "big.bin"
        while time.time() < deadline:
            if dest_big.is_file() and dest_big.stat().st_size == size:
                env = find_env(hao, msg_big) or {}
                if (env.get("state") or {}).get("status") not in ("receiving",):
                    break
            time.sleep(0.4)
        else:
            logs = (WORK / "hao.out").read_text(encoding="utf-8")[-3000:]
            nes_logs = (WORK / "nes.out").read_text(encoding="utf-8")[-2000:]
            raise Fail(f"续传没有完成 dest={dest_big} exists={dest_big.exists()} size={dest_big.stat().st_size if dest_big.exists() else 0}\n{logs}\n--- nes ---\n{nes_logs}")

        if colon_sha(dest_big) != want["big.bin"]:
            raise Fail("大文件哈希不一致")
        if dest_big.stat().st_size != size:
            raise Fail("大文件大小不一致")
        letter_big = ""
        for path in inbox_letters(hao):
            if msg_big in path.name:
                letter_big = path.read_text(encoding="utf-8")
                break
        if "big.bin" not in letter_big or str(dest_big) not in letter_big:
            raise Fail("大文件信纸没有列出附件路径: " + letter_big[-500:])
        # 发送方待发箱应已清空这封
        if list((nes / "pending").rglob(f"{msg_big}.env.json")):
            # 可能还在，再等一会儿 flush 删除
            time.sleep(2)
        if list((nes / "pending").rglob(f"{msg_big}.env.json")):
            raise Fail("续传完成后待发箱还留着这封信")

        # 回信带附件
        reply_file = files / "reply.txt"
        reply_file.write_text("回信附件内容\n", encoding="utf-8")
        replied = xlt(
            hao, "reply", msg_small, "--text", "已收到小附件", "--attach", str(reply_file),
            env=env_slow, timeout=30,
        )
        rbody = json.loads(replied.stdout or "{}")
        if rbody.get("status") != "delivered":
            raise Fail("回信附件没有送达: " + replied.stdout + replied.stderr)
        reply_id = rbody.get("id") or latest_msg_id(nes)
        reply_dest = attach_dir(nes, reply_id) / "reply.txt"
        if not reply_dest.is_file() or colon_sha(reply_dest) != colon_sha(reply_file):
            raise Fail(f"回信附件不对: {reply_dest}")

        print("ok: 小文本和二进制附件送达，哈希一致，信纸列出本机路径")
        print("ok: 敏感 .key / .env 被出口检查拒绝")
        print("ok: 约 1GB 文件中途断开后续传成功，哈希一致")
        print("ok: 回信 --attach 送达")
        return 0
    finally:
        if send_proc is not None:
            send_proc.stop()
        for proc in procs:
            proc.stop()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Fail as exc:
        print("失败：", exc, file=sys.stderr)
        sys.exit(1)
    except subprocess.TimeoutExpired as exc:
        print("失败：命令超时", exc, file=sys.stderr)
        sys.exit(1)
