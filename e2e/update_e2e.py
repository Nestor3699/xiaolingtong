#!/usr/bin/env python3
"""自动更新：全程用本地的 bare git 仓库和本地 http 服务当「GitHub」，不碰真实仓库和网络。

故事 1  git 副本，通讯员自己发现新提交 → 快进 → 重启 → 自检 → 提示条；之后不会反复更新；数据文件一个字节没变
故事 2  关掉自动更新后不动；有本地改动 / 本地有自己的提交 → 跳过，用户的东西原样；干净了再手动 xlt update 成功
故事 3  非 git 副本（下载压缩包）：新版本一启动就崩 → 自检失败 → 退回旧版并重启；记下坏提交不再重试；
        .venv、grok-test 原样保留；修好的新版本再来就能更新，旧版留在 .update/prev
故事 4  正在传大附件时：更新先等；等不到就推迟（不重启、不打断）；传完立刻更新，附件完整到达
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import PY, REPO, Fail, Node, env_with, fresh, pair, say, wait_for  # noqa: E402

WORK = Path(__file__).resolve().parent / "work-update"
MB = 1024 * 1024
GIT_ENV = {
    "GIT_AUTHOR_NAME": "e2e", "GIT_AUTHOR_EMAIL": "e2e@example.invalid",
    "GIT_COMMITTER_NAME": "e2e", "GIT_COMMITTER_EMAIL": "e2e@example.invalid",
}


def git(cwd: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env=env_with(GIT_ENV))
    if check and r.returncode != 0:
        raise Fail(f"git {' '.join(args)} 失败：{r.stderr}")
    return (r.stdout + r.stderr).strip()


def bump(text: str, version: str) -> str:
    import re

    return re.sub(r'__version__ = "[^"]*"', f'__version__ = "{version}"', text)


def sha_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Upstream:
    """本地的「GitHub」：一个 bare 仓库 + 一个工作副本用来造提交。"""

    def __init__(self, work: Path):
        self.root = work
        self.bare = work / "remote.git"
        self.src = work / "src"
        self.src.mkdir(parents=True)
        listing = subprocess.run(
            ["git", "-C", str(REPO), "ls-files", "-co", "--exclude-standard", "--", "xlt", "pyproject.toml", "README.md"],
            capture_output=True, text=True, check=True,
        ).stdout.split()
        for rel in listing:
            if (REPO / rel).is_file():
                dest = self.src / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(REPO / rel, dest)
        git(self.src, "init", "-q", "-b", "main")
        git(self.src, "add", "-A")
        git(self.src, "commit", "-q", "-m", "A: 起点")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.bare)], check=True)
        git(self.src, "remote", "add", "origin", str(self.bare))
        git(self.src, "push", "-q", "origin", "main")
        self.a = self.head()

    def head(self) -> str:
        return git(self.bare, "rev-parse", "main")

    def commit(self, message: str, edits: dict) -> str:
        for rel, fn in edits.items():
            path = self.src / rel
            path.write_text(fn(path.read_text(encoding="utf-8")), encoding="utf-8")
        git(self.src, "add", "-A")
        git(self.src, "commit", "-q", "-m", message)
        git(self.src, "push", "-q", "origin", "main")
        return self.head()

    def good(self, version: str) -> str:
        return self.commit(f"好版本 {version}", {"xlt/__init__.py": lambda t: bump(t, version)})

    def broken(self) -> str:
        return self.commit("坏版本：启动就崩", {"xlt/daemon.py": lambda t: t.replace(
            "def serve(home: Home, args) -> int:\n", "def serve(home: Home, args) -> int:\n    raise RuntimeError('模拟的坏版本')\n", 1)})

    def fix_broken(self, version: str) -> str:
        return self.commit(f"修好 {version}", {
            "xlt/daemon.py": lambda t: t.replace("    raise RuntimeError('模拟的坏版本')\n", "", 1),
            "xlt/__init__.py": lambda t: bump(t, version),
        })

    # 非 git 副本的下载来源
    def serve_http(self) -> tuple[http.server.ThreadingHTTPServer, dict]:
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                return

            def do_GET(self):
                if self.path == "/head":
                    body = outer.head().encode()
                    ctype = "text/plain"
                elif self.path.startswith("/archive/") and self.path.endswith(".tar.gz"):
                    sha = self.path[len("/archive/"):-len(".tar.gz")]
                    r = subprocess.run(["git", "-C", str(outer.bare), "archive", "--format=tar.gz", f"--prefix=xiaolingtong-{sha}/", sha],
                                       capture_output=True)
                    if r.returncode != 0:
                        self.send_response(404)
                        self.end_headers()
                        return
                    body, ctype = r.stdout, "application/gzip"
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        port = httpd.server_address[1]
        return httpd, {
            "XLT_UPDATE_API_URL": f"http://127.0.0.1:{port}/head",
            "XLT_UPDATE_ARCHIVE_URL": f"http://127.0.0.1:{port}/archive/{{sha}}.tar.gz",
            # 故意把 git 来源指到一个不存在的地方：archive 副本不该碰 git
            "XLT_UPDATE_GIT_URL": str(outer.root / "nowhere.git"),
        }

    def install_git(self, dest: Path) -> None:
        subprocess.run(["git", "clone", "-q", str(self.bare), str(dest)], check=True, env=env_with(GIT_ENV))

    def install_archive(self, dest: Path, sha: str) -> None:
        dest.mkdir(parents=True)
        arch = subprocess.run(["git", "-C", str(self.bare), "archive", sha], capture_output=True, check=True)
        subprocess.run(["tar", "-x", "-C", str(dest)], input=arch.stdout, check=True)
        (dest / ".xlt-commit").write_text(sha + "\n", encoding="utf-8")


def base_env(install: Path, extra: dict | None = None) -> dict:
    env = {
        "PYTHONPATH": str(install),     # 关键：永远指向一次性副本，绝不能指到真实仓库
        "XLT_UPDATE_PIP": "skip",
        "XLT_UPDATE_MIN_GAP_S": "0",
        "XLT_UPDATE_HEALTH_S": "40",
        "XLT_UPDATE_FIRST_CHECK_S": "2",
        "XLT_UPDATE_INTERVAL_S": "4",
        "XLT_UPDATE_RETRY_S": "3",
        "XLT_UPDATE_RESUME_S": "5",
    }
    env.update(extra or {})
    return env


def make_node(work: Path, install: Path, tcp: int, env: dict) -> Node:
    if install.resolve() == REPO.resolve():
        raise Fail("测试副本不能是真实仓库")
    node = Node(work, "nes-mac", "nes", "Nes", "macos", tcp, tcp + 1, tcp + 2)
    node.init()
    node.env = env
    node.cwd = install
    return node


def runtime(node: Node) -> dict:
    p = node.home / "runtime.json"
    try:
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except json.JSONDecodeError:
        return {}


def pid_of(node: Node):
    return runtime(node).get("pid")


def alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def kill_all(node: Node) -> None:
    pid = pid_of(node)
    if node.proc:
        node.proc.kill()
    if pid and alive(pid):
        try:
            os.kill(int(pid), signal.SIGKILL)
        except OSError:
            pass


def update_state(node: Node) -> dict:
    p = node.home / "update-state.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def running_commit(node: Node) -> str:
    return (node.api().get("update") or {}).get("commit") or ""


def install_head(install: Path) -> str:
    if (install / ".git").exists():
        return git(install, "rev-parse", "HEAD")
    return (install / ".xlt-commit").read_text(encoding="utf-8").strip()


def data_fingerprint(home: Path) -> dict:
    """证书、密钥、成员名单、配置：更新不许动它们。"""
    out = {}
    for rel in ("tls", "peers"):
        for p in sorted((home / rel).rglob("*")):
            if p.is_file():
                out[str(p.relative_to(home))] = sha_of(p)
    out["config.json"] = sha_of(home / "config.json")
    return out


# ------------------------------------------------------------------ 故事 1

def story_git_auto(up: Upstream, work: Path) -> None:
    install = work / "install"
    up.install_git(install)
    node = make_node(work, install, 47100, base_env(install, {"XLT_UPDATE_GIT_URL": str(up.bare)}))
    try:
        node.start()
        if running_commit(node) != up.a:
            raise Fail(f"启动时状态页的提交号不对：{running_commit(node)} 应为 {up.a}")
        cfg = json.loads((node.home / "config.json").read_text(encoding="utf-8"))
        if cfg.get("auto_update") is not True:
            raise Fail("auto_update 默认应该是开")
        page = node.page()
        for need in ("更新", "自动更新 开", up.a[:7]):
            if need not in page:
                raise Fail(f"本机块里没有「{need}」")
        say("git 副本：通讯员启动，状态页显示版本和提交号，自动更新默认开")
        time.sleep(7)  # 至少过一轮定时检查：线上没有新提交时什么都不该发生
        old_pid = pid_of(node)
        if update_state(node).get("last_result") != "up_to_date":
            raise Fail("线上没新提交时，检查结果应是 up_to_date：" + str(update_state(node).get("last_result")))
        if pid_of(node) != old_pid:
            raise Fail("没新版本却重启了")
        before = data_fingerprint(node.home)

        b = up.good("0.1.1")
        wait_for(lambda: pid_of(node) != old_pid and alive(pid_of(node)) and running_commit(node) == b, 90,
                 "推了新提交以后，通讯员没有自己更新并重启")
        wait_for(lambda: update_state(node).get("last_result") == "updated" and not update_state(node).get("pending"), 90,
                 "更新进程没有把「自检通过」记下来")
        if install_head(install) != b:
            raise Fail("代码目录没有快进到新提交")
        st = update_state(node)
        if st.get("last_result") != "updated" or st.get("pending"):
            raise Fail("更新状态不对：" + json.dumps(st, ensure_ascii=False)[:300])
        if data_fingerprint(node.home) != before:
            raise Fail("更新碰了证书、成员名单或配置")
        say("推了新提交：不用点任何东西，通讯员自己快进、重启、自检通过（数据文件一个字节没变）")

        page = node.page()
        if "小灵通已自动更新" not in page or "0.1.1" not in page:
            raise Fail("更新后状态页没有提示条或新版本号")
        notice_id = (node.api()["update"]["notice"] or {})["id"]
        code, _ = node.post("/local/update-dismiss", {"id": notice_id})
        if code != 403:
            raise Fail("没有面板请求头的 POST 应该被拒绝，实际 " + str(code))
        code, body = node.post("/local/update-dismiss", {"id": notice_id}, {"X-Xlt-Panel": "1"})
        if code != 200 or "小灵通已自动更新" in node.page():
            raise Fail("提示条点「知道了」后应该消失")
        say("更新后状态页有可关闭的提示条；关闭接口只接受面板自己发的请求")

        pid_after = pid_of(node)
        time.sleep(10)  # 间隔 4 秒，至少再检查两轮：同一个提交不会再更新
        if pid_of(node) != pid_after:
            raise Fail("已经是最新，却又重启了（更新循环）")
        say("更新完后再检查几轮：已是最新，不会重复更新")
    finally:
        kill_all(node)


# ------------------------------------------------------------------ 故事 2

def story_skip(up: Upstream, work: Path) -> None:
    install = work / "install"
    up.install_git(install)
    node = make_node(work, install, 47110, base_env(install, {"XLT_UPDATE_GIT_URL": str(up.bare)}))
    try:
        node.start()
        start_commit = up.head()
        r = node.cli("config", "auto_update", "false")
        if r.returncode != 0 or json.loads((node.home / "config.json").read_text(encoding="utf-8"))["auto_update"] is not False:
            raise Fail("xlt config auto_update false 没生效：" + r.stdout + r.stderr)
        b = up.good("0.1.1")
        pid = pid_of(node)
        time.sleep(10)
        if install_head(install) != start_commit or pid_of(node) != pid:
            raise Fail("auto_update 关着，通讯员却自己更新了")
        if "自动更新 关" not in node.page():
            raise Fail("状态页应该显示「自动更新 关」")
        r = node.cli("update", "--check")
        if "有新版本" not in r.stdout:
            raise Fail("xlt update --check 应该说有新版本：" + r.stdout + r.stderr)
        if install_head(install) != start_commit:
            raise Fail("--check 不该改代码")
        say("自动更新关掉后不动；xlt update --check 只报告有新版本，不改任何东西")

        # a. 本地改了被跟踪的文件
        readme = install / "README.md"
        mine = readme.read_text(encoding="utf-8") + "\n我自己的笔记\n"
        readme.write_text(mine, encoding="utf-8")
        r = node.cli("update")
        if r.returncode != 2 or "自己改过" not in r.stdout:
            raise Fail("有本地改动时应该跳过（退出码 2）：" + r.stdout + r.stderr)
        if readme.read_text(encoding="utf-8") != mine or install_head(install) != start_commit or pid_of(node) != pid:
            raise Fail("跳过时动了用户的改动或重启了")
        if update_state(node).get("last_result") != "skipped_dirty":
            raise Fail("状态里应记下 skipped_dirty")
        say("有本地改动：跳过并说明，文件原样，没重启")

        # b. 本地有自己的提交（和线上分叉）
        git(install, "checkout", "-q", "--", "README.md")
        (install / "my-notes.txt").write_text("本地提交\n", encoding="utf-8")
        git(install, "add", "my-notes.txt")
        git(install, "commit", "-q", "-m", "我自己的提交")
        mine_head = git(install, "rev-parse", "HEAD")
        r = node.cli("update")
        if r.returncode != 2 or "不在一条线上" not in r.stdout:
            raise Fail("分叉时应该跳过：" + r.stdout + r.stderr)
        if git(install, "rev-parse", "HEAD") != mine_head or not (install / "my-notes.txt").exists():
            raise Fail("分叉跳过时动了用户的提交")
        if update_state(node).get("last_result") != "skipped_non_ff":
            raise Fail("状态里应记下 skipped_non_ff")
        say("本地有自己的提交（非快进）：跳过，提交还在，不 reset 不 merge")

        # c. 回到干净状态后手动更新
        git(install, "reset", "-q", "--hard", start_commit)
        r = node.cli("update", timeout=180)
        if r.returncode != 0 or "已更新" not in r.stdout:
            raise Fail("手动 xlt update 应该成功：" + r.stdout + r.stderr)
        wait_for(lambda: running_commit(node) == b and alive(pid_of(node)), 30, "手动更新后通讯员没有跑新版本")
        if pid_of(node) == pid:
            raise Fail("手动更新后通讯员应该已经重启")
        say("手动 xlt update：快进、重启、自检，通讯员跑起新版本")
    finally:
        kill_all(node)


# ------------------------------------------------------------------ 故事 3

def story_rollback(up: Upstream, work: Path) -> None:
    install = work / "install"
    up.install_archive(install, up.a)
    (install / ".venv" / "bin").mkdir(parents=True)
    (install / ".venv" / "bin" / "marker").write_text("venv\n", encoding="utf-8")
    (install / "grok-test").mkdir()
    (install / "grok-test" / "note.txt").write_text("本地夹具\n", encoding="utf-8")
    httpd, src_env = up.serve_http()
    node = make_node(work, install, 47120, base_env(install, src_env))
    try:
        node.start()
        node.cli("config", "auto_update", "false")  # 这一幕用手动，时序更确定
        a_daemon = sha_of(install / "xlt" / "daemon.py")
        pid = pid_of(node)
        c = up.broken()
        r = node.cli("update", timeout=240)
        if r.returncode != 1 or "退回" not in r.stdout:
            raise Fail("坏版本应该被退回，退出码 1：" + r.stdout + r.stderr)
        if sha_of(install / "xlt" / "daemon.py") != a_daemon or install_head(install) != up.a:
            raise Fail("退回后代码目录不是旧版本")
        wait_for(lambda: alive(pid_of(node)) and running_commit(node) == up.a, 30, "退回后通讯员没有重新跑起来旧版本")
        st = update_state(node)
        if c not in (st.get("failed_commits") or {}) or st.get("last_result") != "rolled_back":
            raise Fail("没有记下坏提交：" + json.dumps(st, ensure_ascii=False)[:300])
        page = node.page()
        if "更新失败，已退回旧版" not in page:
            raise Fail("状态页没有显示退回的提示条")
        for kept in (install / ".venv" / "bin" / "marker", install / "grok-test" / "note.txt"):
            if not kept.exists():
                raise Fail(f"{kept} 被动了")
        say("非 git 副本：新版本启动就崩 → 自检失败 → 退回旧版并重启；记下坏提交；.venv 和 grok-test 没动")

        pid2 = pid_of(node)
        r = node.cli("update")
        if r.returncode != 2 or "以前更新失败过" not in r.stdout:
            raise Fail("同一个坏提交不该再试：" + r.stdout + r.stderr)
        if pid_of(node) != pid2:
            raise Fail("跳过坏提交时不该重启")
        say("同一个坏提交不会再试（也不循环）")

        # 自动更新打开：只要线上还是坏提交，也不会反复折腾
        node.cli("config", "auto_update", "true")
        time.sleep(11)
        if pid_of(node) != pid2:
            raise Fail("自动更新开着，遇到已知的坏提交还是重启了")
        node.cli("config", "auto_update", "false")

        d = up.fix_broken("0.1.2")
        r = node.cli("update", timeout=240)
        if r.returncode != 0 or "已更新" not in r.stdout:
            raise Fail("修好的新版本应该能更新：" + r.stdout + r.stderr)
        wait_for(lambda: running_commit(node) == d and alive(pid_of(node)), 30, "更新后通讯员没有跑新版本")
        if install_head(install) != d or '"0.1.2"' not in (install / "xlt" / "__init__.py").read_text(encoding="utf-8"):
            raise Fail("代码目录没有换成新版")
        for kept in (install / ".venv" / "bin" / "marker", install / "grok-test" / "note.txt"):
            if not kept.exists():
                raise Fail(f"更新时 {kept} 被动了")
        prev = install / ".update" / "prev"
        if not (prev / "tree" / "xlt" / "daemon.py").exists() or json.loads((prev / "meta.json").read_text(encoding="utf-8"))["commit"] != up.a:
            raise Fail("旧版本没有留在 .update/prev 里")
        say("非 git 副本换新版本成功；.venv、grok-test 原样；旧版留在 .update/prev 可回滚")
    finally:
        kill_all(node)
        httpd.shutdown()


# ------------------------------------------------------------------ 故事 4

def story_defer(up: Upstream, work: Path) -> None:
    install = work / "install"
    up.install_git(install)
    nes, hao = pair(work, 47130)
    nes.env = base_env(install, {
        "XLT_UPDATE_GIT_URL": str(up.bare), "XLT_ATTACH_CHUNK": str(MB), "XLT_ATTACH_SLEEP": "0.35",
        "XLT_UPDATE_WAIT_S": "3", "XLT_UPDATE_DEFER_LIMIT": "6",
    })
    nes.cwd = install
    big = work / "big.bin"
    big.write_bytes(os.urandom(40 * MB))
    big_sha = sha_of(big)
    sender = None
    try:
        nes.start()
        hao.start()
        wait_for(lambda: (nes.home / "peers.cache.json").exists(), 15, "没有互相发现")
        nes.cli("config", "auto_update", "false")
        b = up.good("0.1.1")
        pid = pid_of(nes)
        sender = subprocess.Popen(
            [PY, "-m", "xlt", "--home", str(nes.home), "send", "--to", "haonian", "--type", "notify",
             "--subject", "大文件", "--text", "更新测试", "--attach", str(big)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env_with(nes.env), text=True, cwd=str(install),
        )
        def busy():
            with __import__("urllib.request").request.urlopen(f"http://127.0.0.1:{nes.panel}/local/busy", timeout=3) as resp:
                return json.loads(resp.read().decode())["busy"]
        wait_for(busy, 20, "附件开始传以后，/local/busy 应该是忙")
        t0 = time.time()
        r = nes.cli("update", timeout=120)
        if r.returncode != 0 or "推迟" not in r.stdout:
            raise Fail("正在传附件时更新应该推迟：" + r.stdout + r.stderr)
        if install_head(install) != up.a or pid_of(nes) != pid:
            raise Fail("推迟时不该改代码或重启")
        if update_state(nes).get("last_result") != "deferred":
            raise Fail("状态里应记下 deferred")
        say("正在传大附件：等了一小会儿还在传 → 推迟，没改代码、没重启、没打断")

        # 第二次：愿意多等。更新要等传完才动手
        waited = subprocess.Popen(
            [PY, "-m", "xlt", "--home", str(nes.home), "update"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=str(install),
            env=env_with(dict(nes.env, XLT_UPDATE_WAIT_S="120")),
        )
        delivered_at = [None]
        early = [False]

        def done():
            if pid_of(nes) != pid and delivered_at[0] is None:
                early[0] = True
            files = [e for e in hao.inbox_envs() if e.get("subject") == "大文件"]
            got = [p for p in (hao.home / "inbox").rglob("attachments/*") if not p.name.endswith(".part")]
            if files and got and delivered_at[0] is None:
                delivered_at[0] = time.time()
            return delivered_at[0]

        wait_for(done, 90, "附件应该完整送到")
        if early[0]:
            raise Fail("附件还没传完，通讯员就被重启了")
        out, err = waited.communicate(timeout=150)
        if waited.returncode != 0 or "已更新" not in out:
            raise Fail("传完以后应该更新成功：" + out + err)
        wait_for(lambda: running_commit(nes) == b and alive(pid_of(nes)), 30, "更新后通讯员没有跑新版本")
        got = [p for p in (hao.home / "inbox").rglob("attachments/*") if not p.name.endswith(".part")]
        if len(got) != 1 or sha_of(got[0]) != big_sha:
            raise Fail("附件没有完整、一致地送到")
        if sender.wait(timeout=60) != 0:
            raise Fail("发送命令应正常结束：" + (sender.stderr.read() if sender.stderr else ""))
        if "等" not in out:
            raise Fail("更新进程应该提示在等通讯员忙完：" + out)
        say("愿意多等时：先等传完（附件完整到达、哈希一致），然后才重启更新")
    finally:
        if sender and sender.poll() is None:
            sender.kill()
        kill_all(nes)
        hao.crash()


def main() -> int:
    real_head = git(REPO, "rev-parse", "HEAD")
    real_status = git(REPO, "status", "--porcelain")
    WORK.mkdir(exist_ok=True)
    stories = [
        ("git", story_git_auto),
        ("skip", story_skip),
        ("rollback", story_rollback),
        ("defer", story_defer),
    ]
    only = set(sys.argv[1:])
    for name, fn in stories:
        if only and name not in only:
            continue
        work = fresh(WORK / name)
        up = Upstream(work / "up")
        fn(up, work)
    if git(REPO, "rev-parse", "HEAD") != real_head or git(REPO, "status", "--porcelain") != real_status:
        raise Fail("真实仓库被动了！")
    say("真实仓库的提交和工作区在整个测试里一个字节没变")
    print("UPDATE E2E 全部通过")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Fail as exc:
        print("失败：", exc)
        raise SystemExit(1)
