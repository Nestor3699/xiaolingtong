"""新增 E2E 共用的小工具：起两台本机通讯员、读状态页、等条件成立。

不是单元测试。每个 *_e2e.py 自己讲完一整段故事，这里只放起进程、发命令、读文件这些重复动作。
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
PY = sys.executable
os.environ["PYTHONPATH"] = str(REPO)
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


class Fail(Exception):
    pass


def fresh(work: Path) -> Path:
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    return work


def env_with(extra: dict | None = None) -> dict:
    merged = os.environ.copy()
    merged["PYTHONPATH"] = str(REPO)
    if extra:
        merged.update(extra)
    return merged


def xlt(home: Path, *args: str, env: dict | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PY, "-m", "xlt", "--home", str(home), *args],
        capture_output=True,
        text=True,
        env=env_with(env),
        timeout=timeout,
    )


class Proc:
    def __init__(self, work: Path, name: str, args: list[str], env: dict | None = None):
        self.name = name
        self.log_path = work / f"{name}.out"
        self.log = open(self.log_path, "a", encoding="utf-8")
        self.p = subprocess.Popen(
            args, stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True, env=env_with(env)
        )

    def kill(self) -> None:
        """像断电一样直接杀，不给它收尾的机会。"""
        if self.p.poll() is None:
            try:
                os.killpg(self.p.pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                self.p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        try:
            self.log.close()
        except Exception:
            pass

    stop = kill


class Node:
    """一台本机通讯员：名字、端口、数据目录。"""

    def __init__(self, work: Path, me: str, person: str, owner: str, system: str, tcp: int, udp: int, panel: int):
        self.work = work
        self.me, self.person, self.owner, self.system = me, person, owner, system
        self.tcp, self.udp, self.panel = tcp, udp, panel
        self.home = work / person
        self.proc: Proc | None = None
        self.peer_udp = 0
        self.env: dict = {}

    def init(self) -> None:
        r = xlt(self.home, "init", "--me", self.me, "--person", self.person, "--owner", self.owner)
        if r.returncode != 0:
            raise Fail(f"init {self.me} 失败：{r.stderr}")

    def trust(self, other: "Node") -> None:
        r = xlt(
            self.home, "trust", "--name", other.me, "--person", other.person, "--owner", other.owner,
            "--os", other.system, "--port", str(other.tcp),
            "--crt", str(other.home / "tls" / f"{other.me}.crt"), "--primary",
        )
        if r.returncode != 0:
            raise Fail(f"trust {other.me} 失败：{r.stderr}")

    def serve_args(self) -> list[str]:
        args = [
            PY, "-m", "xlt", "--home", str(self.home), "serve",
            "--bind", "127.0.0.1", "--ip", "127.0.0.1",
            "--tcp-port", str(self.tcp), "--udp-port", str(self.udp), "--panel-port", str(self.panel),
            "--heartbeat", "3", "--no-ip-auto",
        ]
        if self.peer_udp:
            args += ["--announce", f"127.0.0.1:{self.peer_udp}"]
        return args

    def start(self) -> None:
        self.proc = Proc(self.work, self.person, self.serve_args(), self.env)
        wait_for(lambda: self.panel_ok(), 15, f"{self.me} 的状态页没起来")

    def crash(self) -> None:
        if self.proc:
            self.proc.kill()
            self.proc = None
        time.sleep(0.5)

    def panel_ok(self) -> bool:
        try:
            self.api()
            return True
        except Exception:
            return False

    def api(self) -> dict:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.panel}/api/panel", timeout=5) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def page(self) -> str:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.panel}/", timeout=5) as resp:
            return resp.read().decode("utf-8")

    def post(self, path: str, payload: dict, headers: dict | None = None) -> tuple[int, dict]:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.panel}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read().decode("utf-8"))
            except Exception:
                body = {}
            return exc.code, body

    def pending_files(self) -> list[Path]:
        root = self.home / "pending"
        out = []
        if root.exists():
            for p in root.rglob("*.env.json"):
                if "expired" in p.parts or "failed" in p.parts:
                    continue
                out.append(p)
        return out

    def inbox_envs(self) -> list[dict]:
        out = []
        root = self.home / "inbox"
        if root.exists():
            for p in sorted(root.rglob("*.env.json")):
                try:
                    out.append(json.loads(p.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError):
                    continue
        return out

    def log_text(self) -> str:
        p = self.home / "messenger.log"
        return p.read_text(encoding="utf-8") if p.exists() else ""


def wait_for(fn, seconds: float, why: str, every: float = 0.25):
    end = time.time() + seconds
    last = None
    while time.time() < end:
        try:
            last = fn()
        except Exception as exc:  # 条件里读文件可能碰上半写，下一轮再看
            last = None
            _ = exc
        if last:
            return last
        time.sleep(every)
    raise Fail(why)


def pair(work: Path, base: int) -> tuple[Node, Node]:
    """nes-mac 和 haonian-win，互相登记，互相把对方的 UDP 端口当单播目标。"""
    nes = Node(work, "nes-mac", "nes", "Nes", "macos", base, base + 1, base + 2)
    hao = Node(work, "haonian-win", "haonian", "浩年", "windows", base + 10, base + 11, base + 12)
    nes.init()
    hao.init()
    nes.trust(hao)
    hao.trust(nes)
    nes.peer_udp = hao.udp
    hao.peer_udp = nes.udp
    return nes, hao


def say(text: str) -> None:
    print("通过", text, flush=True)
