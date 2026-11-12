"""自动更新：定时看 GitHub main 有没有新提交，有就更新、重启、自检，不行就退回去。

只动代码目录（git 副本用 fast-forward，非 git 副本整目录换新并留一份旧的）。
绝不碰 ~/.xlt 里的数据、证书、密钥、配置；这里只写 update-state.json 和一把 update.lock。
"""

from __future__ import annotations

import io
import json
import os
import random
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from xlt import __version__
from xlt.common import now_iso, read_json, write_json
from xlt.doctor import pid_alive  # 提前导入：更新途中代码目录会被换掉，不能在换完之后才第一次导入
from xlt.store import Home, load_config, runtime_path

REPO = "Nestor3699/xiaolingtong"
BRANCH = "main"
CODE_ROOT = Path(__file__).resolve().parent.parent
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
LAUNCHD_LABEL = "com.xiaolingtong.gateway"
WIN_TASK = "Xiaolingtong Gateway"

# 非 git 副本整目录换新时，这些顶层名字一定当作「代码」管理（旧版本里有、新版本里删了的也要清掉）。
MANAGED_DEFAULT = {
    "xlt", "e2e", "docs", "install", "skills", "scripts", "examples",
    "pyproject.toml", "README.md", "DEPLOY-AGENT.md", "AGENTS.md", "CLAUDE.md",
    "THIRD_PARTY_NOTICES.md", ".gitignore", ".xlt-commit",
}
# 这些永远不动：虚拟环境、备份、git 数据、本地测试夹具、安装产生的 egg-info。
PRESERVE = {".venv", "venv", ".update", ".git", "grok-test", ".demo", "__pycache__"}


def _preserved(name: str) -> bool:
    return name in PRESERVE or name.endswith(".egg-info")


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


# ---------------------------------------------------------------- 来源（测试里可以换成本地的）

def source() -> dict:
    return {
        "git_url": os.environ.get("XLT_UPDATE_GIT_URL") or f"https://github.com/{REPO}.git",
        "api_url": os.environ.get("XLT_UPDATE_API_URL") or f"https://api.github.com/repos/{REPO}/commits/{BRANCH}",
        "archive_url": os.environ.get("XLT_UPDATE_ARCHIVE_URL") or f"https://codeload.github.com/{REPO}/tar.gz/{{sha}}",
        "branch": BRANCH,
    }


# ---------------------------------------------------------------- 小工具

def _flags() -> dict:
    if os.name == "nt":
        return {"creationflags": 0x08000000}  # CREATE_NO_WINDOW
    return {}


def run(cmd: list[str], timeout: float = 60, cwd: Path | str | None = None, env: dict | None = None) -> tuple[int, str]:
    merged = os.environ.copy()
    merged["GIT_TERMINAL_PROMPT"] = "0"
    merged["LC_ALL"] = "C"
    if env:
        merged.update(env)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=str(cwd) if cwd else None,
            env=merged, errors="replace", **_flags(),
        )
    except subprocess.TimeoutExpired:
        return 124, "超时"
    except OSError as exc:
        return 127, f"{exc.__class__.__name__}: {exc}"
    return proc.returncode, ((proc.stdout or "") + (proc.stderr or "")).strip()


def git(root: Path, *args: str, timeout: float = 60) -> tuple[int, str]:
    return run(["git", "-C", str(root), *args], timeout=timeout)


def short(sha: str | None) -> str:
    return (sha or "")[:7] or "未知"


def _rmtree(path: Path) -> None:
    def onerror(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass

    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path, onerror=onerror)


# ---------------------------------------------------------------- 装的是哪一种

def install_info(root: Path | None = None) -> dict:
    root = Path(root or CODE_ROOT)
    info = {"root": str(root), "shape": "none", "commit": "", "can_update": False, "reason": ""}
    if not (root / "pyproject.toml").exists() or not (root / "xlt" / "__init__.py").exists():
        info["reason"] = "不是从源码目录安装的（没有 pyproject.toml），不能自动更新。"
        return info
    if (root / ".git").exists():
        info["shape"] = "git"
        if not shutil.which("git"):
            info["reason"] = "这份代码是 git 副本，但这台电脑上找不到 git 命令，不能自动更新。"
            return info
        rc, out = git(root, "rev-parse", "HEAD")
        info["commit"] = out.strip() if rc == 0 and SHA_RE.match(out.strip()) else ""
        info["can_update"] = True
        return info
    info["shape"] = "archive"
    marker = root / ".xlt-commit"
    if marker.exists():
        text = marker.read_text(encoding="utf-8", errors="replace").strip()
        info["commit"] = text if SHA_RE.match(text) else ""
    info["can_update"] = True
    return info


_RUNNING: dict = {}


def running_commit() -> str:
    """通讯员进程启动时代码是哪个提交。启动后更新了代码也不变，直到重启。"""
    if "commit" not in _RUNNING:
        _RUNNING["commit"] = install_info().get("commit") or ""
    return _RUNNING["commit"]


def restart_command(home: Home, argv: list[str]) -> list[str]:
    rest: list[str] = []
    if "serve" in argv:
        rest = list(argv[argv.index("serve") + 1:])
    return [sys.executable, "-m", "xlt", "--home", str(home.root), "serve", *rest]


# ---------------------------------------------------------------- 状态文件

def state_path(home: Home) -> Path:
    return home.root / "update-state.json"


def load_state(home: Home) -> dict:
    data = read_json(state_path(home), {})
    return data if isinstance(data, dict) else {}


def mutate_state(home: Home, fn) -> dict:
    home.root.mkdir(parents=True, exist_ok=True)
    with home.lock():
        st = load_state(home)
        fn(st)
        write_json(state_path(home), st)
    return st


def _history(st: dict, text: str) -> None:
    rows = st.setdefault("history", [])
    rows.append({"ts": now_iso(), "text": text})
    del rows[:-20]


def dismiss_notice(home: Home, notice_id: str) -> int:
    count = [0]

    def fn(st):
        notice = st.get("notice")
        if notice and not notice.get("dismissed") and notice_id in (notice.get("id"), "all"):
            notice["dismissed"] = True
            count[0] = 1

    mutate_state(home, fn)
    return count[0]


def panel_info(home: Home) -> dict:
    """状态页要显示的更新信息。"""
    st = load_state(home)
    cfg = load_config(home)
    notice = st.get("notice") if isinstance(st.get("notice"), dict) else None
    if notice and notice.get("dismissed"):
        notice = None
    return {
        "auto": bool(cfg.get("auto_update", True)),
        "commit": running_commit(),
        "version": __version__,
        "last_check": st.get("last_check") or "",
        "result": st.get("last_result") or "",
        "message": st.get("message") or "",
        "notice": notice,
        "latest": st.get("latest_seen") or "",
    }


# ---------------------------------------------------------------- 锁

LOCK_STALE_S = 1800


def _lock_alive(home: Home) -> bool:
    path = home.root / "update.lock"
    data = read_json(path, {}) if path.exists() else {}
    if not data:
        return False
    if not pid_alive(data.get("pid")):
        return False
    return (time.time() - float(data.get("ts") or 0)) < LOCK_STALE_S


def _acquire(home: Home) -> bool:
    path = home.root / "update.lock"
    home.root.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if _lock_alive(home):
            return False
        path.unlink(missing_ok=True)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"pid": os.getpid(), "ts": time.time()}, fh)
    return True


def _release(home: Home) -> None:
    path = home.root / "update.lock"
    data = read_json(path, {}) if path.exists() else {}
    if data.get("pid") == os.getpid():
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------- 远端最新提交

def remote_head(info: dict, src: dict) -> str:
    if info["shape"] == "git":
        rc, out = git(Path(info["root"]), "ls-remote", src["git_url"], f"refs/heads/{src['branch']}", timeout=45)
        if rc != 0:
            raise RuntimeError(f"git ls-remote 失败：{(out.splitlines() or [''])[0][:160]}")
        sha = out.split()[0] if out.split() else ""
    else:
        req = urllib.request.Request(
            src["api_url"],
            headers={"Accept": "application/vnd.github.sha", "User-Agent": "xiaolingtong-updater"},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                sha = resp.read(4096).decode("utf-8", errors="replace").strip()
        except (urllib.error.URLError, OSError) as exc:
            raise RuntimeError(f"连不上 GitHub：{exc}") from exc
        if sha.startswith("{"):
            try:
                sha = str(json.loads(sha).get("sha") or "")
            except json.JSONDecodeError:
                sha = ""
    if not SHA_RE.match(sha):
        raise RuntimeError("GitHub 返回的内容不像提交号")
    return sha


# ---------------------------------------------------------------- 准备新版本

class Skip(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def git_prepare(info: dict, src: dict, head: str) -> str:
    """git 副本：检查干净、在 main 上，取回远端，确认能 fast-forward。返回要更新到的提交。"""
    root = Path(info["root"])
    rc, branch = git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if rc != 0 or branch.strip() != src["branch"]:
        raise Skip("skipped_branch", f"这份代码现在不在 {src['branch']} 分支上（{branch.strip() or '游离状态'}），没有自动更新。")
    rc, dirty = git(root, "status", "--porcelain", "--untracked-files=no")
    if dirty.strip():
        names = "、".join(line[3:] for line in dirty.splitlines()[:5])
        raise Skip("skipped_dirty", f"这份代码里有你自己改过的文件，没有自动更新（不会动你的修改）：{names}。")
    rc, out = git(root, "fetch", "--quiet", src["git_url"], src["branch"], timeout=120)
    if rc != 0:
        raise RuntimeError(f"git fetch 失败：{out[-200:]}")
    rc, fetched = git(root, "rev-parse", "FETCH_HEAD")
    fetched = fetched.strip()
    if rc != 0 or not SHA_RE.match(fetched):
        raise RuntimeError("git fetch 之后读不到远端提交")
    if fetched == info["commit"]:
        raise Skip("up_to_date", f"已经是最新版（{short(fetched)}）。")
    rc, _ = git(root, "merge-base", "--is-ancestor", fetched, "HEAD")
    if rc == 0:
        raise Skip("up_to_date", f"本地比线上还新（{short(info['commit'])} 已经包含 {short(fetched)}），没有动。")
    rc, _ = git(root, "merge-base", "--is-ancestor", "HEAD", fetched)
    if rc != 0:
        raise Skip("skipped_non_ff", f"本地的提交和线上不在一条线上（本地 {short(info['commit'])}，线上 {short(fetched)}），没有自动更新，也没有动你的提交。")
    return fetched


def _safe_extract(tar: tarfile.TarFile, dest: Path) -> None:
    base = dest.resolve()
    for member in tar.getmembers():
        target = (dest / member.name).resolve()
        if base != target and base not in target.parents:
            raise RuntimeError("压缩包里有越界路径，已拒绝")
        if member.issym() or member.islnk():
            raise RuntimeError("压缩包里有链接，已拒绝")
    try:
        tar.extractall(dest, filter="data")
    except TypeError:
        tar.extractall(dest)


def archive_prepare(info: dict, src: dict, head: str) -> Path:
    """非 git 副本：下载 tar.gz，解到 .update/staging，返回解开后的代码顶层目录。"""
    root = Path(info["root"])
    staging = root / ".update" / "staging"
    _rmtree(staging)
    staging.mkdir(parents=True)
    url = src["archive_url"].format(sha=head)
    req = urllib.request.Request(url, headers={"User-Agent": "xiaolingtong-updater"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = resp.read(200 * 1024 * 1024 + 1)
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(f"下载新版本失败：{exc}") from exc
    if len(data) > 200 * 1024 * 1024:
        raise RuntimeError("新版本压缩包超过 200MB，已拒绝")
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            _safe_extract(tar, staging)
    except (tarfile.TarError, OSError) as exc:
        raise RuntimeError(f"新版本压缩包打不开：{exc}") from exc
    tops = [p for p in staging.iterdir()]
    top = tops[0] if len(tops) == 1 and tops[0].is_dir() else staging
    if not (top / "xlt" / "__init__.py").exists() or not (top / "pyproject.toml").exists():
        raise RuntimeError("新版本压缩包里没有 xlt/ 和 pyproject.toml，已拒绝")
    return top


def swap_in(root: Path, top: Path, sha: str, old_commit: str) -> list[str]:
    """把代码目录里「由发布管理」的顶层项换成新版，旧的整体挪到 .update/prev。逐步记账，中途失败就原样退回。"""
    upd = root / ".update"
    prev = upd / "prev"
    _rmtree(prev)
    (prev / "tree").mkdir(parents=True)
    old_manifest = read_json(upd / "manifest.json", {}).get("names", [])
    new_names = sorted(p.name for p in top.iterdir() if not _preserved(p.name))
    managed = {n for n in (set(new_names) | set(old_manifest) | MANAGED_DEFAULT) if not _preserved(n)}
    journal: list[tuple[str, str]] = []
    try:
        for name in sorted(managed):
            src = root / name
            if src.exists() or src.is_symlink():
                os.rename(src, prev / "tree" / name)
                journal.append(("moved", name))
        for name in new_names:
            os.rename(top / name, root / name)
            journal.append(("placed", name))
        (root / ".xlt-commit").write_text(sha + "\n", encoding="utf-8")
    except Exception:
        for kind, name in reversed(journal):
            try:
                if kind == "placed":
                    _rmtree(root / name)
                else:
                    os.rename(prev / "tree" / name, root / name)
            except OSError:
                pass
        raise
    write_json(upd / "manifest.json", {"names": new_names, "commit": sha})
    write_json(prev / "meta.json", {"commit": old_commit, "names": [n for k, n in journal if k == "moved"],
                                    "new_names": new_names, "old_manifest": old_manifest, "at": now_iso()})
    return new_names


def swap_back(root: Path) -> bool:
    upd = root / ".update"
    prev = upd / "prev"
    meta = read_json(prev / "meta.json", {})
    if not meta:
        return False
    for name in meta.get("new_names") or []:
        _rmtree(root / name)
    _rmtree(root / ".xlt-commit")
    for name in meta.get("names") or []:
        src = prev / "tree" / name
        if src.exists() or src.is_symlink():
            os.rename(src, root / name)
    write_json(upd / "manifest.json", {"names": meta.get("old_manifest") or [], "commit": meta.get("commit") or ""})
    return True


# ---------------------------------------------------------------- pip

def pip_install(root: Path, required: bool) -> tuple[bool, str]:
    if os.environ.get("XLT_UPDATE_PIP", "") == "skip":
        return True, "测试里跳过 pip"
    cmd = [sys.executable, "-m", "pip", "install", "-e", str(root), "--quiet", "--disable-pip-version-check"]
    rc, out = run(cmd, timeout=900, cwd=root)
    if rc == 0:
        return True, "pip install -e . 成功"
    text = f"pip install -e . 失败：{out[-300:]}"
    return (not required), text


# ---------------------------------------------------------------- 通讯员：停、启、问

def _panel_port(home: Home) -> int | None:
    runtime = read_json(runtime_path(home), {})
    port = runtime.get("panel_port")
    return int(port) if port else None


def _local(home: Home, path: str, method: str = "GET", timeout: float = 4) -> tuple[int, str]:
    port = _panel_port(home)
    if not port:
        return 0, ""
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", method=method,
        data=b"{}" if method == "POST" else None,
        headers={"X-Xlt-Panel": "1", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(2_000_000).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except (urllib.error.URLError, OSError):
        return 0, ""


def gateway_pid(home: Home):
    pid = read_json(runtime_path(home), {}).get("pid")
    return pid if pid and pid_alive(pid) else None


def gateway_commit(home: Home) -> str:
    code, body = _local(home, "/local/busy")
    if code != 200:
        return ""
    try:
        return str(json.loads(body).get("commit") or "")
    except json.JSONDecodeError:
        return ""


def managed_mode() -> str:
    forced = os.environ.get("XLT_UPDATE_RESTART", "")
    if forced:
        return forced
    if sys.platform == "darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
        if plist.exists():
            rc, _ = run(["launchctl", "print", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"], timeout=10)
            if rc == 0:
                return "launchd"
    if os.name == "nt":
        rc, _ = run(["schtasks", "/Query", "/TN", WIN_TASK], timeout=15)
        if rc == 0:
            return "schtasks"
    return "direct"


def stop_gateway(home: Home, timeout: float = 25) -> object:
    pid = gateway_pid(home)
    if not pid:
        return None
    _local(home, "/local/shutdown", "POST")
    end = time.time() + timeout
    while time.time() < end and pid_alive(pid):
        time.sleep(0.4)
    if pid_alive(pid):
        try:
            if os.name == "nt":
                run(["taskkill", "/PID", str(pid), "/T", "/F"], timeout=15)
            else:
                os.kill(int(pid), signal.SIGTERM)
        except OSError:
            pass
        end = time.time() + 10
        while time.time() < end and pid_alive(pid):
            time.sleep(0.4)
    return pid


def _spawn_gateway(home: Home, cmd: list[str]) -> None:
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "cwd": str(home.root), "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, **kwargs)


def start_gateway(home: Home, old_pid, cmd: list[str], mode: str, wait: float = 30) -> None:
    def fresh() -> bool:
        pid = read_json(runtime_path(home), {}).get("pid")
        return bool(pid) and pid != old_pid and pid_alive(pid)

    if mode == "launchd":
        end = time.time() + 25  # 开机自启设了 KeepAlive，退出后系统会自己拉起来
        while time.time() < end and not fresh():
            time.sleep(0.5)
        if not fresh():
            run(["launchctl", "kickstart", f"gui/{os.getuid()}/{LAUNCHD_LABEL}"], timeout=20)
    elif mode == "schtasks":
        for _ in range(6):
            rc, _out = run(["schtasks", "/Run", "/TN", WIN_TASK], timeout=20)
            if rc == 0:
                break
            time.sleep(2)
        else:
            _spawn_gateway(home, cmd)
    else:
        _spawn_gateway(home, cmd)
    end = time.time() + wait
    while time.time() < end and not fresh():
        time.sleep(0.5)


# ---------------------------------------------------------------- 自检

def health(home: Home, target: str, expect_gateway: bool, timeout: float | None = None) -> tuple[bool, str]:
    timeout = timeout if timeout is not None else env_float("XLT_UPDATE_HEALTH_S", 60)
    problem = "通讯员没有起来"
    if expect_gateway:
        end = time.time() + timeout
        while time.time() < end:
            if gateway_pid(home):
                code, body = _local(home, "/")
                running = gateway_commit(home)
                if code == 200 and "小灵通" in body:
                    if not target or running == target:
                        problem = ""
                        break
                    problem = f"通讯员在跑，但跑的是 {short(running)}，不是 {short(target)}"
                else:
                    problem = "通讯员进程在，但状态页没有响应"
            else:
                problem = "通讯员进程没有起来（启动就退出了）"
            time.sleep(1)
        if problem:
            return False, problem
    rc, out = run([sys.executable, "-m", "xlt", "--home", str(home.root), "doctor", "--json"], timeout=90, cwd=CODE_ROOT)
    try:
        rows = json.loads(out[out.index("["):])
    except (ValueError, json.JSONDecodeError):
        return False, f"xlt doctor 没有给出可读的结果：{out[-160:]}"
    gateway = next((r for r in rows if r.get("key") == "gateway"), None)
    if expect_gateway and (gateway is None or gateway.get("status") != "ok"):
        return False, "doctor 认为通讯员没有在运行"
    bad = [r.get("title") for r in rows if r.get("status") == "fail"]
    note = "（doctor 另有需要注意的项：" + "、".join(str(b) for b in bad) + "，和更新无关）" if bad else ""
    return True, "通讯员在运行，状态页正常，doctor 能跑" + note


# ---------------------------------------------------------------- 主流程

def _finish(home: Home, result: str, message: str, **extra) -> dict:
    def fn(st):
        st["last_result"] = result
        st["message"] = message
        for key, value in extra.items():
            if key == "notice":
                st["notice"] = value
            else:
                st[key] = value
        _history(st, message)

    mutate_state(home, fn)
    return {"result": result, "message": message}


def _notice(kind: str, text: str) -> dict:
    return {"id": f"{kind}-{int(time.time())}", "kind": kind, "text": text, "at": now_iso(), "dismissed": False}


def _wait_idle(home: Home, limit: float, say) -> bool:
    if not gateway_pid(home):
        return True
    end = time.time() + limit
    told = False
    while True:
        code, body = _local(home, "/local/busy")
        busy = False
        if code == 200:
            try:
                busy = bool(json.loads(body).get("busy"))
            except json.JSONDecodeError:
                busy = False
        if not busy:
            return True
        if not told:
            say("通讯员正在收发文件，先等它忙完再重启……")
            told = True
        if time.time() >= end:
            return False
        time.sleep(1.5)


def check_only(home: Home) -> dict:
    info = install_info()
    st = load_state(home)
    out = {"installed": info["commit"], "shape": info["shape"], "can_update": info["can_update"], "reason": info["reason"]}
    if not info["can_update"]:
        return dict(out, result="unsupported", message=info["reason"])
    src = source()
    try:
        head = remote_head(info, src)
    except RuntimeError as exc:
        mutate_state(home, lambda s: s.update(last_check=now_iso(), last_check_ts=time.time()))
        return dict(out, result="error", message=f"检查更新失败：{exc}")
    mutate_state(home, lambda s: s.update(last_check=now_iso(), last_check_ts=time.time(), latest_seen=head))
    out["latest"] = head
    if head == info["commit"]:
        return dict(out, result="up_to_date", message=f"已经是最新版（{short(head)}）。")
    if head in (st.get("failed_commits") or {}):
        return dict(out, result="skipped_bad", message=f"线上最新版 {short(head)} 以前更新失败过，会被跳过。")
    return dict(out, result="available", message=f"有新版本：{short(info['commit'])} → {short(head)}。运行 xlt update 更新。")


def update(home: Home, auto: bool = False, force: bool = False, say=print) -> dict:
    """检查并更新。manual 和 auto 走同一条路。返回 {"result","message"}。"""
    if not _acquire(home):
        return {"result": "busy", "message": "另一个更新正在进行，这次先不做。"}
    try:
        return _update_locked(home, auto, force, say)
    finally:
        _release(home)


def _update_locked(home: Home, auto: bool, force: bool, say) -> dict:
    info = install_info()
    if not info["can_update"]:
        return _finish(home, "unsupported", info["reason"], last_check=now_iso(), last_check_ts=time.time())
    src = source()
    st = load_state(home)
    try:
        head = remote_head(info, src)
    except RuntimeError as exc:
        return _finish(home, "error", f"检查更新失败：{exc}（可能是没网或访问不了 GitHub，下次会再试）", last_check=now_iso(), last_check_ts=time.time())
    mutate_state(home, lambda s: s.update(last_check=now_iso(), last_check_ts=time.time(), latest_seen=head))
    if head == info["commit"]:
        return _finish(home, "up_to_date", f"已经是最新版（{short(head)}）。", deferrals=0)
    if head in (st.get("failed_commits") or {}):
        return _finish(home, "skipped_bad", f"线上最新版 {short(head)} 以前更新失败过，已跳过，等有更新的版本再试。")

    prev_commit = info["commit"]
    root = Path(info["root"])
    say(f"发现新版本 {short(head)}（现在 {short(prev_commit)}），正在准备……")
    top = None
    try:
        if info["shape"] == "git":
            target = git_prepare(info, src, head)
        else:
            target = head
            top = archive_prepare(info, src, head)
    except Skip as skip:
        return _finish(home, skip.code, skip.message, deferrals=0 if skip.code == "up_to_date" else st.get("deferrals", 0))
    except RuntimeError as exc:
        return _finish(home, "error", f"准备新版本失败：{exc}")

    if target in (st.get("failed_commits") or {}):
        return _finish(home, "skipped_bad", f"线上最新版 {short(target)} 以前更新失败过，已跳过。")

    wait_limit = env_float("XLT_UPDATE_WAIT_S", 600)
    if not force and not _wait_idle(home, wait_limit, say):
        deferrals = int(st.get("deferrals") or 0) + 1
        if deferrals < int(env_float("XLT_UPDATE_DEFER_LIMIT", 6)):
            retry = time.time() + env_float("XLT_UPDATE_RETRY_S", 300)
            return _finish(home, "deferred", f"通讯员还在收发文件，这次先不重启（第 {deferrals} 次推迟），稍后再试。", deferrals=deferrals, retry_at=retry)
        say("推迟了很多次，现在还是更新；附件会从断点续传。")

    return _apply(home, info, root, prev_commit, target, top, say)


def _apply(home: Home, info: dict, root: Path, prev_commit: str, target: str, top: Path | None, say) -> dict:
    shape = info["shape"]
    expect_gateway = bool(gateway_pid(home))
    runtime = read_json(runtime_path(home), {})
    cmd = runtime.get("cmd") or [sys.executable, "-m", "xlt", "--home", str(home.root), "serve"]
    mode = managed_mode()
    pyproject_before = (root / "pyproject.toml").read_bytes() if (root / "pyproject.toml").exists() else b""

    # 先把「正在更新」记下来：就算更新进程中途被杀，下次通讯员启动会接着验证。
    mutate_state(home, lambda s: s.update(pending={
        "from": prev_commit, "to": target, "shape": shape, "root": str(root),
        "expect_gateway": expect_gateway, "started": now_iso(), "cmd": cmd, "mode": mode,
    }))
    try:
        if shape == "git":
            rc, out = git(root, "merge", "--ff-only", target)
            if rc != 0:
                mutate_state(home, lambda s: s.pop("pending", None))
                return _finish(home, "skipped_dirty", f"git 没能快进到新版本（不会动你的文件）：{out[-200:]}")
        else:
            swap_in(root, top, target, prev_commit)
            _rmtree(root / ".update" / "staging")
    except Exception as exc:
        mutate_state(home, lambda s: s.pop("pending", None))
        return _finish(home, "error", f"换上新版本时出错，已原样保留旧版：{exc.__class__.__name__}: {exc}")

    pyproject_after = (root / "pyproject.toml").read_bytes() if (root / "pyproject.toml").exists() else b""
    ok_pip, pip_text = pip_install(root, required=pyproject_before != pyproject_after)
    problem = "" if ok_pip else pip_text
    old_pid = None
    if not problem:
        say("代码已换新，正在重启通讯员……")
        if expect_gateway:
            old_pid = stop_gateway(home)
            start_gateway(home, old_pid, cmd, mode)
        ok, why = health(home, target, expect_gateway)
        if not ok:
            problem = why
    if not problem:
        text = f"已更新 {short(prev_commit)} → {short(target)}，通讯员已重启，自检通过。"
        return _finish(home, "updated", text, installed_at=now_iso(), deferrals=0, retry_at=0, notice=_notice("updated", text), pending=None)
    return _rollback(home, info, root, prev_commit, target, problem, expect_gateway, cmd, mode, say)


def _rollback(home: Home, info: dict, root: Path, prev_commit: str, target: str, problem: str,
              expect_gateway: bool, cmd: list, mode: str, say) -> dict:
    say(f"自检没通过（{problem}），正在退回 {short(prev_commit)}……")
    restored = True
    detail = ""
    if info["shape"] == "git":
        rc, head = git(root, "rev-parse", "HEAD")
        if head.strip() == target:
            rc, out = git(root, "reset", "--keep", prev_commit)
            restored = rc == 0
            detail = "" if restored else f"git reset --keep 失败：{out[-160:]}"
        else:
            restored = False
            detail = "代码目录的提交已经不是刚更新的那个，没有自动退回，请手动处理"
    else:
        try:
            restored = swap_back(root)
        except OSError as exc:
            restored = False
            detail = f"换回旧版时出错：{exc}"
    if restored:
        pip_install(root, required=False)
        if expect_gateway:
            old = stop_gateway(home)
            start_gateway(home, old, cmd, mode)
    ok_after, why_after = False, detail
    if restored:
        ok_after, why_after = health(home, prev_commit, expect_gateway)
    def fn(st):
        bad = st.setdefault("failed_commits", {})
        bad[target] = {"at": now_iso(), "reason": problem}
    mutate_state(home, fn)
    if restored:
        text = f"更新到 {short(target)} 后自检没通过（{problem}），已退回 {short(prev_commit)} 并重启" + ("。" if ok_after else f"；退回后自检也有问题：{why_after}。")
    else:
        text = f"更新到 {short(target)} 后自检没通过（{problem}），而且没能自动退回：{detail}。"
    return _finish(home, "rolled_back", text, notice=_notice("rolled_back", text), pending=None, retry_at=0)


def resume_pending(home: Home, say=print) -> dict:
    """上次更新做到一半进程被杀了：通讯员重启后，看新版本是不是真的跑起来了，不行就退回。"""
    if not _acquire(home):
        return {"result": "busy", "message": "另一个更新正在进行。"}
    try:
        st = load_state(home)
        pending = st.get("pending")
        if not pending:
            return {"result": "none", "message": "没有未完成的更新。"}
        info = install_info()
        root = Path(info["root"])
        target = pending.get("to") or ""
        expect = bool(pending.get("expect_gateway"))
        ok, why = health(home, target, expect, timeout=env_float("XLT_UPDATE_HEALTH_S", 60))
        if ok:
            text = f"已更新 {short(pending.get('from'))} → {short(target)}，通讯员已重启，自检通过。"
            return _finish(home, "updated", text, installed_at=now_iso(), deferrals=0, notice=_notice("updated", text), pending=None)
        return _rollback(home, info, root, pending.get("from") or "", target, why, expect,
                         pending.get("cmd") or [sys.executable, "-m", "xlt", "--home", str(home.root), "serve"],
                         pending.get("mode") or "direct", say)
    finally:
        _release(home)


# ---------------------------------------------------------------- 通讯员里的定时器

def spawn_detached(home: Home, resume: bool = False) -> None:
    cmd = [sys.executable, "-m", "xlt", "--home", str(home.root), "update", "--resume" if resume else "--auto"]
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "cwd": str(CODE_ROOT), "close_fds": True}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000
    else:
        kwargs["start_new_session"] = True  # 独立会话：通讯员被重启时，更新进程不会跟着被杀
    subprocess.Popen(cmd, **kwargs)


class Scheduler:
    def __init__(self, home: Home, log):
        self.home = home
        self.log = log
        now = time.time()
        self.next_check = now + env_float("XLT_UPDATE_FIRST_CHECK_S", random.uniform(40, 80))
        self.resume_at = now + env_float("XLT_UPDATE_RESUME_S", 15)
        self.resumed = False
        self.last_poll = 0.0

    def _interval(self) -> float:
        env = os.environ.get("XLT_UPDATE_INTERVAL_S")
        if env:
            base = env_float("XLT_UPDATE_INTERVAL_S", 1800)
        else:
            try:
                base = max(5, int(load_config(self.home).get("auto_update_interval_min") or 30)) * 60
            except (TypeError, ValueError):
                base = 1800
        return base * random.uniform(0.8, 1.2)

    def tick(self, now: float) -> None:
        if now - self.last_poll < 2:
            return
        self.last_poll = now
        if not self.resumed and now >= self.resume_at:
            self.resumed = True
            if load_state(self.home).get("pending") and not _lock_alive(self.home):
                self.log("上次更新没做完，接着验证新版本")
                spawn_detached(self.home, resume=True)
                return
        if os.environ.get("XLT_UPDATE_DISABLE") == "1":
            return  # 端到端测试用：测试里的通讯员不许去碰真实的 GitHub 和真实的代码目录
        cfg = load_config(self.home)
        if not cfg.get("auto_update", True):
            self.next_check = max(self.next_check, now + 60)
            return
        st = load_state(self.home)
        retry_at = float(st.get("retry_at") or 0)
        due = now >= self.next_check or (retry_at and now >= retry_at)
        if not due:
            return
        self.next_check = now + self._interval()
        if now - float(st.get("last_check_ts") or 0) < env_float("XLT_UPDATE_MIN_GAP_S", 300) and not (retry_at and now >= retry_at):
            return
        if _lock_alive(self.home) or st.get("pending"):
            return
        if retry_at:
            mutate_state(self.home, lambda s: s.update(retry_at=0))
        self.log("定时检查更新")
        spawn_detached(self.home)
