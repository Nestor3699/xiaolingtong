"""收信、权限、路由、待发箱、把信交给本机适配器。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from xlt import adapters, receipts
from xlt.common import (
    LETTER_MAX,
    atomic_text,
    clean_subject,
    envelope_summary,
    now,
    now_iso,
    parse_time,
    write_json,
)
from xlt.crypto import fingerprint_bytes, load_key, sign_envelope, verify_envelope
from xlt.attach import (
    attachment_dir,
    chunk_size,
    describe_attachments,
    file_egress_blocked,
    hash_and_size,
    prepare_attachments,
    public_attachments,
    unique_path,
    verify_file_hash,
    chmod_private,
)
from xlt.mtls import FingerprintMismatch, request, trust_pems
from xlt.policy import egress_blocked, evaluate
from xlt.store import (
    Home,
    append_jsonl,
    find_window,
    load_config,
    load_directory,
    load_policy,
    load_routes,
    load_sent,
    load_windows,
    member_by_name,
    peer_cert,
    remember_sent,
    resolve_person,
)

BACKOFF = tuple(
    int(item) for item in (os.environ.get("XLT_BACKOFF") or "10,30,120,300").split(",") if item.strip()
) or (10, 30, 120, 300)
NO_RETRY = {400, 401, 403, 404, 410, 413}


class Engine:
    def __init__(self, home: Home, log):
        self.home = home
        self.log = log
        self.cfg = load_config(home)
        self.key_path = home.tls / f"{self.cfg['me']}.key"
        self.key = load_key(self.key_path)
        self.cert = home.tls / f"{self.cfg['me']}.crt"
        self.started = time.time()
        self.rates: dict[str, list[float]] = {}
        self.inflight: set[str] = set()
        self.flight = threading.Lock()
        self.attach_locks: dict[str, threading.Lock] = {}
        self.attach_locks_mu = threading.Lock()
        self.seen: set[str] = set()
        self._poll_guard = threading.Lock()
        self.procs: dict[str, subprocess.Popen] = {}
        self.executor = None
        self._load_seen()

    def _load_seen(self) -> None:
        path = self.home.root / "seen_ids.jsonl"
        if not path.exists():
            return
        kept = []
        cutoff = time.time() - 8 * 86400
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                stamp = parse_time(row["ts"]).timestamp()
            except Exception:
                continue
            if stamp < cutoff:
                continue
            self.seen.add(row["id"])
            kept.append(line)
        if kept and path.read_text(encoding="utf-8").count("\n") != len(kept):
            atomic_text(path, "\n".join(kept) + "\n")

    def mark_seen(self, msg_id: str) -> None:
        if msg_id in self.seen:
            return
        self.seen.add(msg_id)
        append_jsonl(self.home.root / "seen_ids.jsonl", {"id": msg_id, "ts": now_iso()})

    def audit(self, **fields) -> None:
        fields.setdefault("ts", now_iso())
        append_jsonl(self.home.root / "audit.jsonl", fields)

    def reject_log(self, **fields) -> None:
        fields.setdefault("ts", now_iso())
        append_jsonl(self.home.root / "rejects.jsonl", fields)

    def event(self, kind: str, env: dict, **extra) -> None:
        row = {
            "ts": now_iso(),
            "event": kind,
            "id": env.get("id"),
            "to": env.get("to_member") or env.get("to"),
            "type": env.get("type"),
        }
        row.update(extra)
        append_jsonl(self.home.pending / "events.jsonl", row)

    def log_env(self, text: str, env: dict) -> None:
        self.log(
            f"{text} id={env.get('id')} type={env.get('type')} from={env.get('from')} "
            f"to={env.get('to')} bytes={env.get('letter_bytes')}"
        )

    def _rate_ok(self, person: str) -> bool:
        policy = load_policy(self.home)
        limit = int((policy.get("limits") or {}).get("rate_per_minute") or 30)
        stamp = time.time()
        recent = [item for item in self.rates.get(person, []) if stamp - item < 60]
        if len(recent) >= limit:
            self.rates[person] = recent
            return False
        recent.append(stamp)
        self.rates[person] = recent
        return True

    def gate(self, member: dict, action: str, path: str | None = None, implicit: bool = False):
        policy = load_policy(self.home)
        mode = str(policy.get("mode") or "normal")
        if mode == "paused":
            from xlt.policy import Decision

            return Decision(False, "PAUSED", "对方暂停了一切查询", http=403)
        person = member.get("person") or ""
        if not self._rate_ok(person):
            from xlt.policy import Decision

            return Decision(False, "RATE_LIMITED", "这个同事这一分钟的请求太多了", http=429)
        if implicit:
            from xlt.policy import Decision

            return Decision(True, "OK", "这是对我发出去的信的回复", grant="in_reply_to")
        return evaluate(policy, person, member.get("name") or "", action, path)

    def health_body(self) -> dict:
        return {
            "status": "ok",
            "agent": self.cfg["me"],
            "version": "0.1.0",
            "uptime_s": int(time.time() - self.started),
            "time": now_iso(),
        }

    def status_body(self) -> dict:
        policy = load_policy(self.home)
        busy = False
        for path in self.home.inbox.rglob("*.env.json"):
            try:
                env = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if (env.get("state") or {}).get("status") in ("delivered", "delivering", "awaiting_manual"):
                busy = True
                break
        return {
            "agent": self.cfg["me"],
            "version": "0.1.0",
            "mode": policy.get("mode") or "normal",
            "policy_version": policy.get("version"),
            "busy": busy,
            "time": now_iso(),
        }

    def permissions_body(self, member: dict) -> dict:
        policy = load_policy(self.home)
        grants = []
        for grant in policy.get("grants") or []:
            from xlt.policy import grantee_ok

            if not grantee_ok(grant, member.get("person") or "", member.get("name") or ""):
                continue
            grants.append(
                {
                    "id": grant.get("id"),
                    "operations": grant.get("operations"),
                    "allow_paths": grant.get("allow_paths"),
                    "valid_until": grant.get("valid_until"),
                    "max_file_size_kb": grant.get("max_file_size_kb"),
                }
            )
        return {
            "agent": self.cfg["me"],
            "caller": member.get("name"),
            "policy_version": policy.get("version"),
            "mode": policy.get("mode") or "normal",
            "grants": grants,
        }

    def on_message(self, member: dict, env: dict, letter: str, req_id: str) -> tuple[int, dict]:
        with self.home.lock():
            code, payload, job = self._accept(member, env, letter, req_id)
        if job:
            self._submit(job)
        return code, payload

    def _accept(self, member: dict, env: dict, letter: str, req_id: str):
        if not isinstance(env, dict) or not isinstance(letter, str):
            return 400, _err("BAD_ENVELOPE", "信封或信纸格式不对", req_id), None
        for key in ("id", "type", "from", "from_person", "to", "created", "letter_sha256", "sig"):
            if key not in env or env.get(key) in ("", None):
                return 400, _err("BAD_ENVELOPE", f"信封缺少 {key}", req_id), None
        if env.get("type") not in ("ask", "notify", "reply"):
            return 400, _err("BAD_ENVELOPE", "类型只能是 ask、notify、reply", req_id), None
        if env.get("type") == "reply" and not env.get("in_reply_to"):
            return 400, _err("BAD_ENVELOPE", "回信要写 in_reply_to", req_id), None
        if env.get("from") != member.get("name"):
            self._deny(member, env, "IDENTITY_MISMATCH", "信封上的发信人和证书对不上", req_id)
            return 401, _err("IDENTITY_MISMATCH", "信封上的发信人和证书对不上", req_id), None
        cert = peer_cert(self.home, member["name"])
        if cert is None or not verify_envelope(cert, env):
            self._deny(member, env, "BAD_ENVELOPE", "信封签名无效", req_id)
            return 400, _err("BAD_ENVELOPE", "信封签名无效", req_id), None
        raw = letter.encode("utf-8")
        if env.get("letter_bytes") != len(raw) or env.get("letter_sha256") != fingerprint_bytes(raw):
            self._deny(member, env, "BAD_ENVELOPE", "信纸和信封上的大小或哈希不一致", req_id)
            return 400, _err("BAD_ENVELOPE", "信纸和信封上的大小或哈希不一致", req_id), None
        if len(raw) > LETTER_MAX:
            return 413, _err("TOO_LARGE", "信超过 64KB", req_id), None
        att_err = _validate_incoming_attachments(env.get("attachments"))
        if att_err:
            return 400, _err("BAD_ENVELOPE", att_err, req_id), None
        existing = self._find_env(env["id"])
        if existing is not None:
            self.mark_seen(env["id"])
            self.log_env("重复投递，已去重", env)
            return 200, _resume_payload(existing), None
        if _expired(env):
            return 410, _err("MESSAGE_EXPIRED", "这封信已经过了保留期", req_id), None
        sent = self._sent_match(env, member) if env.get("type") == "reply" else None
        if sent:
            # 对方回信了，当然是读过了。老版本对方不发回执，这是它们唯一能升到「已读」的路。
            receipts.mark_read(self.home, env.get("in_reply_to"), to_member=member.get("name") or "", via="reply")
        decision = self.gate(member, env["type"], implicit=bool(sent))
        if not decision.allow:
            self._deny(member, env, decision.code, decision.message, req_id, action=env["type"])
            return decision.http, _err(decision.code, decision.message, req_id), None
        window, why = self._pick_window(env, sent)
        if window and self._queue_len(window["window"]) >= 20:
            return 503, _err("BUSY", "对方这个窗口的队列满了", req_id), None
        folder_name = window["window"] if window else "_hold"
        folder = self.home.inbox / folder_name
        folder.mkdir(parents=True, exist_ok=True)
        state = {
            "received": now_iso(),
            "verified_from": member.get("name"),
            "verified_fp": member.get("cert_sha256"),
            "grant": decision.grant,
            "routed_to": f"{window['agent']}/{window['window']}" if window else "",
            "routed_by": why,
            "status": "queued",
            "updated": now_iso(),
            "history": [["queued", now_iso()]],
            "binding_gen": (window or {}).get("generation"),
        }
        stored = dict(env)
        stored["state"] = state
        waiting = False
        if env.get("attachments"):
            recv = _incoming_attachment_state(self.home, env["id"], env.get("attachments"))
            stored["attachments"] = recv
            if _finalize_empty_attachments(recv):
                stored["attachments_listed"] = False
                # 0 字节附件当场收齐，下面再决定要不要排队
                waiting = not _attachments_done(stored)
            else:
                waiting = True
            if waiting:
                stored["state"]["status"] = "receiving"
        letter_path = folder / f"{env['id']}.letter.md"
        env_path = folder / f"{env['id']}.env.json"
        atomic_text(letter_path, letter)
        write_json(env_path, stored)
        self.mark_seen(env["id"])
        self.audit(
            req_id=req_id,
            caller=member.get("person"),
            caller_name=member.get("name"),
            caller_fp=member.get("cert_sha256"),
            action=env.get("type"),
            grant=decision.grant,
            decision="allow",
            id=env.get("id"),
            bytes=env.get("letter_bytes"),
            subject=env.get("subject"),
        )
        self.log_env(f"已收下 → {state['routed_to'] or '_hold'}（{why}）", env)
        if stored.get("attachments") and not waiting:
            self._finish_attachments(env_path, stored)
        job = (window, env_path) if window and not waiting else None
        payload = {"id": env["id"], "status": "accepted"}
        if stored.get("attachments"):
            payload["attachments"] = [
                {"id": item["id"], "received": int(item.get("received") or 0)}
                for item in stored["attachments"]
            ]
            if waiting:
                payload["status"] = "receiving"
        return 202, payload, job

    def _already(self, msg_id: str) -> bool:
        if msg_id in self.seen:
            return True
        found = self._find_env(msg_id)
        if found:
            self.mark_seen(msg_id)
            return True
        return False

    def _deny(self, member, env, code, message, req_id, action=None) -> None:
        self.audit(
            req_id=req_id,
            caller=member.get("person"),
            caller_name=member.get("name"),
            caller_fp=member.get("cert_sha256"),
            action=action or env.get("type"),
            decision="deny",
            reason=code,
            id=env.get("id"),
            subject=env.get("subject"),
        )
        self.reject_log(
            req_id=req_id,
            caller=member.get("person"),
            caller_name=member.get("name"),
            action=action or env.get("type"),
            reason=code,
            message=message,
            id=env.get("id"),
            type=env.get("type"),
            **{"from": env.get("from")},
            subject=env.get("subject"),
        )
        self.log(f"拒绝 {code} id={env.get('id')} from={member.get('name')} {message}")

    def _sent_match(self, env: dict, member: dict) -> dict | None:
        for row in load_sent(self.home):
            if row.get("id") == env.get("in_reply_to") and row.get("to_member") == member.get("name"):
                return row
        return None

    def _pick_window(self, env: dict, sent: dict | None):
        if sent and sent.get("origin_window"):
            window = find_window(self.home, sent["origin_window"])
            if window and not _channel_lost(self.home, window):
                return window, "origin_window"
        routes = load_routes(self.home)
        for rule in routes.get("rules") or []:
            if not _rule_match(rule, env):
                continue
            window = _window_by_ref(self.home, rule.get("to"))
            if window is None:
                self.log(f"规则 {rule.get('id')} 指向的窗口没登记，跳过")
                continue
            if _channel_lost(self.home, window):
                if routes.get("on_window_lost") == "hold":
                    return window, f"rule {rule.get('id')} hold"
                self.log(f"规则 {rule.get('id')} 的窗口失效，改看下一条")
                continue
            return window, f"rule {rule.get('id')}"
        window = _window_by_ref(self.home, routes.get("default"))
        if window and _channel_lost(self.home, window) and routes.get("on_window_lost") != "hold":
            return None, "no-window"
        if window:
            return window, "default"
        return None, "no-window"

    def _queue_len(self, name: str) -> int:
        folder = self.home.inbox / name
        if not folder.exists():
            return 0
        count = 0
        for path in folder.glob("*.env.json"):
            try:
                env = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if (env.get("state") or {}).get("status") in ("queued", "delivered", "delivering", "awaiting_manual"):
                count += 1
        return count

    def _submit(self, job) -> None:
        window, env_path = job
        if self.executor:
            self.executor.submit(self.dispatch, window, env_path)
        else:
            self.dispatch(window, env_path)

    def dispatch(self, window: dict, env_path: Path) -> None:
        try:
            env = json.loads(env_path.read_text(encoding="utf-8"))
            letter = env_path.with_name(f"{env['id']}.letter.md").read_text(encoding="utf-8")
        except (OSError, json.JSONDecodeError):
            return
        kind = window.get("delivery")
        try:
            if kind == "channel":
                return
            if kind == "extension":
                self._mirror_pi(env, letter)
                return
            if kind == "popup":
                self._popup(window, env, letter, env_path)
                return
            if kind == "webhook":
                self._webhook(window, env, env_path)
                return
            if kind in ("headless", "rpc"):
                self._process(window, env, letter, env_path)
                return
            self._touch(env_path, status="queued", last_error=f"不认识的投递方式 {kind}")
        except Exception as exc:
            self.log(f"投递失败 id={env.get('id')}：{exc.__class__.__name__}")
            self._touch(env_path, last_error=exc.__class__.__name__)

    def _mirror_pi(self, env: dict, letter: str) -> None:
        dest = self.home.d("bridges", "pi", "inbox", f"{env['id']}.letter.md")
        if not dest.exists():
            atomic_text(dest, letter)

    def _popup(self, window: dict, env: dict, letter: str, env_path: Path) -> None:
        self._touch(env_path, status="awaiting_manual")
        popup = window.get("popup") or {}
        limit = int(popup.get("max_url_chars") or adapters.MAX_URL)
        prefill = adapters.build_prefill(env.get("from_person") or "", env.get("from") or "", env["id"], letter)
        workdir = window.get("workdir") or str(self.home.d("workdir", "codex"))
        Path(workdir).mkdir(parents=True, exist_ok=True)
        url = adapters.build_url(prefill, workdir, limit)
        summary = adapters.from_line(env)
        adapters.write_popup_files(self.home.d("popups"), env["id"], summary, prefill, url)
        note = adapters.popup_note(summary, prefill, workdir, url)
        if note == "done":
            self._touch(env_path, status="done", copy_fallback=url is None, popup="done")
        else:
            self._touch(env_path, status="awaiting_manual", copy_fallback=url is None, popup=note)
        self.log(f"待手动处理 id={env['id']} copy_fallback={url is None} popup={note}")

    def _webhook(self, window: dict, env: dict, env_path: Path) -> None:
        secret = window.get("secret_file")
        if not secret:
            self._touch(env_path, status="queued", last_error="没有密钥文件")
            self.log(f"webhook 没发出 msg_id={env['id']}（没配置密钥文件）")
            return
        try:
            _url, _key = adapters.load_secret(Path(secret))
        except Exception:
            self._touch(env_path, status="queued", last_error="密钥文件打不开或格式不对")
            self.log(f"webhook 没发出 msg_id={env['id']}（密钥文件打不开或格式不对）")
            return
        try:
            code = adapters.post_webhook(_url, _key, env["id"])
        except Exception:
            self._touch(env_path, status="queued", last_error="webhook 连接失败")
            self.log(f"webhook 失败 msg_id={env['id']} 未记录网址")
            return
        self.log(f"webhook 结果 msg_id={env['id']} http={code}")
        if code == 200:
            self._touch(env_path, status="delivered", last_error=None)
        else:
            self._touch(env_path, status="queued", last_error=f"http {code}")

    def _process(self, window: dict, env: dict, letter: str, env_path: Path) -> None:
        agent = window.get("agent") or ""
        delivery = window.get("delivery") or "headless"
        draft = self.home.outbox / window["window"] / f"draft-{env['id']}.md"
        draft.parent.mkdir(parents=True, exist_ok=True)
        session_path = self.home.root / "sessions" / f"{agent}-{window['window']}"
        session = session_path.read_text(encoding="utf-8").strip() if session_path.exists() else None
        argv = adapters.canonical_argv(agent, delivery, window["window"], draft, session or None)
        command = window.get("command") or argv
        if not command:
            self._touch(env_path, status="queued", last_error="没有可执行的命令", canonical_argv=argv)
            return
        self._touch(env_path, status="delivering", canonical_argv=argv)
        child_env = os.environ.copy()
        child_env["XLT_MSG_ID"] = env["id"]
        child_env["XLT_WINDOW"] = window["window"]
        child_env["XLT_FROM"] = str(env.get("from") or "")
        child_env["XLT_TYPE"] = str(env.get("type") or "")
        workdir = window.get("workdir") or str(self.home.d("workdir", window["window"]))
        Path(workdir).mkdir(parents=True, exist_ok=True)
        timeout = int(window.get("timeout_s") or 120)
        if delivery == "rpc":
            self._rpc(window, env, letter, command, workdir, child_env, env_path)
            return
        try:
            proc = subprocess.run(
                command,
                input=adapters.stdin_bytes(agent, env, letter),
                capture_output=True,
                timeout=timeout,
                cwd=workdir,
                env=child_env,
            )
        except FileNotFoundError:
            self._touch(env_path, status="queued", last_error="找不到命令")
            self.log(f"headless 找不到命令 id={env['id']} argv0={command[0]}")
            return
        except subprocess.TimeoutExpired:
            self._touch(env_path, status="queued", last_error="超时")
            self.log(f"headless 超时 id={env['id']}")
            return
        text = proc.stdout.decode("utf-8", errors="replace").strip()
        text = _extract_text(text, session_path)
        if proc.returncode != 0 and not text:
            self._touch(env_path, status="queued", last_error=f"退出码 {proc.returncode}")
            self.log(f"headless 退出码 {proc.returncode} id={env['id']}")
            return
        if text:
            self.local_reply(env["id"], text, "replied", origin=window["window"])
        else:
            self._touch(env_path, status="delivered")
        self.log(f"headless 结束 id={env['id']} code={proc.returncode}")

    def _rpc(self, window, env, letter, command, workdir, child_env, env_path) -> None:
        key = window["window"]
        proc = self.procs.get(key)
        if proc is None or proc.poll() is not None:
            try:
                proc = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    cwd=workdir,
                    env=child_env,
                )
            except FileNotFoundError:
                self._touch(env_path, status="queued", last_error="找不到命令")
                return
            self.procs[key] = proc
        try:
            assert proc.stdin is not None
            proc.stdin.write(adapters.rpc_line(window.get("agent") or "", env, letter))
            proc.stdin.flush()
        except Exception:
            self._touch(env_path, status="queued", last_error="写不到常驻进程")
            return
        self._touch(env_path, status="delivered")
        self.log(f"rpc 已写入 id={env['id']}")

    def _touch(self, env_path: Path, **state) -> None:
        try:
            env = json.loads(env_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        current = env.setdefault("state", {})
        current.update(state)
        current["updated"] = now_iso()
        if "status" in state:
            current.setdefault("history", []).append([state["status"], current["updated"]])
            receipts.stamp_read_if_needed(current, state["status"])
        write_json(env_path, env)

    def local_send(
        self,
        target: str,
        kind: str,
        subject: str,
        letter: str,
        tags: list | None = None,
        origin_window: str = "",
        in_reply_to: str | None = None,
        to_member: str | None = None,
        attachments: list | None = None,
    ) -> dict:
        kind = kind or "ask"
        if kind not in ("ask", "notify", "reply"):
            return {"status": "rejected", "reason": "BAD_TYPE", "message": "类型只能是 ask、notify、reply"}
        blocked = egress_blocked(letter or "")
        if blocked:
            return {"status": "rejected", "reason": "EGRESS", "message": blocked}
        prepared = []
        if attachments:
            paths = []
            for item in attachments:
                if isinstance(item, dict) and item.get("source"):
                    paths.append(item["source"])
                else:
                    paths.append(str(item))
            prepared, err = prepare_attachments(paths)
            if err:
                return {"status": "rejected", "reason": "EGRESS", "message": err}
        member = member_by_name(self.home, to_member) if to_member else None
        if member is None:
            member = resolve_person(self.home, target)
        if member is None:
            return {"status": "rejected", "reason": "UNKNOWN_PERSON", "message": f"成员名单里没有 {target}"}
        raw = (letter or "").encode("utf-8")
        if len(raw) > LETTER_MAX:
            return {"status": "rejected", "reason": "TOO_LARGE", "message": "信超过 64KB"}
        with self.home.lock():
            active = _active_pending(self.home)
            mine = [item for item in active if item.parent.name == member["name"]]
            total_bytes = sum(item.stat().st_size for item in active)
            if len(mine) >= 50 or len(active) >= 500 or total_bytes > 20 * 1024 * 1024:
                return {"status": "rejected", "reason": "PENDING_FULL", "message": "待发箱已满"}
            seq = _next_seq(self.home)
            msg_id = f"msg-{now():%Y%m%d-%H%M%S}-{self.cfg['me']}-{seq:04d}"
            env = {
                "envelope_v": 1,
                "id": msg_id,
                "type": kind,
                "from": self.cfg["me"],
                "from_person": self.cfg["person"],
                "to": member.get("person") or target,
                "created": now_iso(),
                "ttl_s": 86400,
                "in_reply_to": in_reply_to,
                "subject": clean_subject(subject),
                "tags": list(tags or []),
                "letter_bytes": len(raw),
                "letter_sha256": fingerprint_bytes(raw),
            }
            if prepared:
                env["attachments"] = public_attachments(prepared)
            signed = sign_envelope(self.key, env)
            stored = dict(signed)
            stored["origin_window"] = origin_window or ""
            stored["to_member"] = member["name"]
            stored["attempts"] = 0
            stored["last_error"] = None
            stored["state"] = {"status": "pending", "updated": now_iso()}
            if prepared:
                stored["attachments"] = prepared
            folder = self.home.pending / member["name"]
            folder.mkdir(parents=True, exist_ok=True)
            atomic_text(folder / f"{msg_id}.letter.md", letter or "")
            env_path = folder / f"{msg_id}.env.json"
            write_json(env_path, stored)
            remember_sent(
                self.home,
                {
                    "id": msg_id,
                    "to": stored["to"],
                    "to_member": member["name"],
                    "origin_window": origin_window or "",
                    "type": kind,
                    "subject": stored["subject"],
                    "created": stored["created"],
                    "ts": now_iso(),
                },
            )
            self.event("queued", stored)
        result = self.try_flush(env_path, force=True)
        result["pending_count"] = len(_active_pending(self.home))
        return result

    def local_reply(self, msg_id: str, text: str, status: str = "replied", origin: str = "", attachments: list | None = None) -> dict:
        found = self._find_env(msg_id)
        if found is None:
            return {"status": "rejected", "reason": "NOT_FOUND", "message": "这封信不在本机信箱里", "id": msg_id}
        env = json.loads(found.read_text(encoding="utf-8"))
        blocked = egress_blocked(text or "")
        if blocked:
            self._touch(found, status="egress_blocked", last_error=blocked)
            self.audit(action="reply", decision="deny", reason="EGRESS", id=msg_id, message=blocked)
            self.log(f"出口检查拦截 id={msg_id} {blocked}")
            return {"status": "rejected", "reason": "EGRESS", "message": blocked, "id": msg_id}
        target_member = env.get("from")
        result = self.local_send(
            target=env.get("from_person") or target_member,
            kind="reply",
            subject=clean_subject("回复 " + (env.get("subject") or "")),
            letter=text or "",
            origin_window=origin,
            in_reply_to=msg_id,
            to_member=target_member,
            attachments=attachments,
        )
        new_status = "declined" if status == "declined" else "replied"
        if result.get("status") != "rejected":
            self._touch(found, status=new_status)
        return result

    def local_done(self, msg_id: str) -> dict:
        found = self._find_env(msg_id)
        if found is None:
            return {"status": "rejected", "reason": "NOT_FOUND", "message": "这封信不在本机信箱里", "id": msg_id}
        self._touch(found, status="done")
        self.log(f"已处理 id={msg_id}")
        return {"status": "done", "id": msg_id, "message": "已标记处理，没有回信"}

    def try_flush(self, env_path: Path, force: bool = False) -> dict:
        try:
            env = json.loads(env_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"status": "rejected", "reason": "NOT_FOUND", "message": "待发信已经不在了"}
        msg_id = env.get("id")
        with self.flight:
            if msg_id in self.inflight:
                return _queued(env, "正在发送")
            self.inflight.add(msg_id)
        try:
            return self._flush(env_path, env, force)
        finally:
            with self.flight:
                self.inflight.discard(msg_id)

    def _flush(self, env_path: Path, env: dict, force: bool) -> dict:
        if _expired(env):
            self._move(env_path, self.home.pending / "expired")
            self.event("expired", env)
            return {"id": env.get("id"), "status": "rejected", "reason": "MESSAGE_EXPIRED", "message": "未送达，已过期", "to": env.get("to_member")}
        if not force and env.get("next_try_at") and time.time() < float(env["next_try_at"]):
            return _queued(env, "还没到重试时间")
        member = member_by_name(self.home, env.get("to_member"))
        if member is None:
            return {"id": env.get("id"), "status": "rejected", "reason": "UNKNOWN_PERSON", "message": "名单里已经没有这个人", "to": env.get("to_member")}
        letter = env_path.with_name(f"{env['id']}.letter.md").read_text(encoding="utf-8")
        tried = []
        for ip, port, source in _candidates(self.home, member):
            tried.append(f"{ip}:{port}")
            try:
                trust = (self.home.peers_dir / f"{member['name']}.crt").read_text(encoding="utf-8")
                code, _body = request(ip, port, "GET", "/xlt/v1/health", b"", self.cert, self.key_path, member.get("cert_sha256"), trust, 2)
            except FingerprintMismatch:
                self.event("blocked_fingerprint", env, ip=ip)
                self.log(f"指纹不符，不发 id={env['id']} ip={ip}")
                continue
            except OSError:
                continue
            if code != 200:
                continue
            payload = json.dumps({"envelope": _public(env), "letter": letter}, ensure_ascii=False).encode("utf-8")
            try:
                code, body = request(ip, port, "POST", "/xlt/v1/messages", payload, self.cert, self.key_path, member.get("cert_sha256"), trust, 8)
            except FingerprintMismatch:
                self.event("blocked_fingerprint", env, ip=ip)
                continue
            except OSError as exc:
                self._bump(env_path, env, f"{source} {exc.__class__.__name__}")
                return _queued(env, "对方离线，已排队，上线后自动补发")
            parsed = _json_body(body)
            if code in (202, 200):
                if env.get("attachments"):
                    _merge_resume(env, parsed.get("attachments") or [])
                    write_json(env_path, env)
                    sent = self._send_attachments(env_path, env, member, ip, port, trust)
                    if sent.get("status") != "delivered":
                        return sent
                    try:
                        env = json.loads(env_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        env = env
                self._delete_pending(env_path, env)
                receipts.record_delivered(self.home, env)
                self.event("delivered", env, http=code, source=source)
                message = "对方已收下" if code == 202 else "对方已收过，已从待发箱移除"
                if env.get("attachments"):
                    message += "，附件已传完"
                self.log_env(message, env)
                return {
                    "id": env["id"],
                    "status": "delivered",
                    "message": message,
                    "to": env.get("to_member"),
                    "expires": _expires(env),
                    "receipt": "stored",
                    "attach_progress": __import__("xlt.attach", fromlist=["progress_text"]).progress_text(env.get("attachments")),
                }
            err = (parsed.get("error") or {}) if isinstance(parsed, dict) else {}
            reason = err.get("code") or f"HTTP_{code}"
            message = err.get("message") or "对方拒绝了"
            if code in NO_RETRY:
                self._move(env_path, self.home.pending / "failed")
                self.event("failed", env, http=code, reason=reason)
                self.log_env(f"失败 {reason}", env)
                return {"id": env["id"], "status": "rejected", "reason": reason, "message": message, "to": env.get("to_member")}
            self._bump(env_path, env, message)
            return _queued(env, message)
        self._bump(env_path, env, "connect failed")
        self._mark_unreachable(member["name"])
        return _queued(env, "对方离线，已排队，上线后自动补发")

    def _bump(self, env_path: Path, env: dict, error: str) -> None:
        attempts = int(env.get("attempts") or 0) + 1
        env["attempts"] = attempts
        env["last_error"] = error
        env["last_attempt"] = now_iso()
        # 退避：10 秒、30 秒、2 分钟、5 分钟，之后一直每 5 分钟试一次，直到信过期。
        # 对方一发上线广播，flush_member 会不管退避时间立刻补发。
        env["next_try_at"] = time.time() + BACKOFF[min(attempts, len(BACKOFF)) - 1]
        env.setdefault("state", {})["status"] = "pending"
        env["state"]["updated"] = now_iso()
        write_json(env_path, env)
        self.event("retry", env, attempts=attempts, error=error)

    def _delete_pending(self, env_path: Path, env: dict) -> None:
        letter = env_path.with_name(f"{env['id']}.letter.md")
        for path in (env_path, letter):
            try:
                path.unlink()
            except OSError:
                pass

    def _move(self, env_path: Path, folder: Path) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        try:
            env = json.loads(env_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        letter = env_path.with_name(f"{env['id']}.letter.md")
        for path in (env_path, letter):
            if path.exists():
                dest = folder / path.name
                try:
                    os.replace(path, dest)
                except OSError:
                    shutil.copyfile(path, dest)
                    path.unlink(missing_ok=True)

    def _mark_unreachable(self, name: str) -> None:
        from xlt.store import load_cache, save_cache

        cache = load_cache(self.home)
        row = cache.get("peers", {}).get(name)
        if row and row.get("state") == "online":
            row["state"] = "unreachable"
            save_cache(self.home, cache)

    def flush_member(self, name: str) -> None:
        folder = self.home.pending / name
        if not folder.exists():
            return
        paths = sorted(folder.glob("*.env.json"), key=lambda item: item.name)
        for path in paths:
            self.try_flush(path, force=True)

    def flush_due(self) -> None:
        for path in _active_pending(self.home):
            self.try_flush(path, force=False)

    def drain_outbox(self) -> None:
        folders = [self.home.outbox / window["window"] for window in load_windows(self.home)]
        folders.append(self.home.d("bridges", "pi", "outbox"))
        for folder in folders:
            if not folder.exists():
                continue
            for path in list(folder.glob("*.json")):
                self._consume_json(path)
            for path in list(folder.glob("*.reply.md")):
                self._consume_reply_md(path)

    def _consume_json(self, path: Path) -> None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        try:
            path.unlink()
        except OSError:
            return
        op = data.get("op")
        msg_id = data.get("msg_id")
        if op == "xlt_reply":
            self.local_reply(msg_id, data.get("text") or "", data.get("status") or "replied", attachments=data.get("attachments") or [])
        elif op == "xlt_done":
            self.local_done(msg_id)

    def _consume_reply_md(self, path: Path) -> None:
        msg_id = path.name[: -len(".reply.md")]
        try:
            text = path.read_text(encoding="utf-8")
            path.unlink()
        except OSError:
            return
        self.local_reply(msg_id, text, "replied")

    def drain_spool(self) -> None:
        folder = self.home.d("spool")
        if not folder.exists():
            return
        for path in list(folder.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            op = data.get("op")
            if op == "reply":
                self.local_reply(
                    data.get("msg_id") or "",
                    data.get("text") or data.get("letter") or "",
                    data.get("status") or "replied",
                    attachments=data.get("attachments") or [],
                )
            else:
                self.local_send(
                    data.get("to") or "",
                    data.get("type") or "ask",
                    data.get("subject") or "",
                    data.get("letter") or "",
                    data.get("tags") or [],
                    data.get("origin_window") or "",
                    attachments=data.get("attachments") or [],
                )
            path.unlink(missing_ok=True)

    def on_receipt(self, member: dict, msg_id: str, req_id: str) -> tuple[int, dict]:
        """只有原发信人的证书能问自己这封信的回执。"""
        found = self._find_env(msg_id)
        if found is None:
            return 404, _err("NO_SUCH_MESSAGE", "这边没有这封信", req_id)
        with self.home.lock():
            env = json.loads(found.read_text(encoding="utf-8"))
            if env.get("from") != member.get("name"):
                return 404, _err("NO_SUCH_MESSAGE", "这边没有这封信", req_id)
            view = receipts.receipt_view(self.home, found, env)
            state = env.setdefault("state", {})
            if view["state"] == "read" and not state.get("read_at") and view.get("read_at"):
                state["read_at"] = view["read_at"]
                write_json(found, env)
        return 200, view

    def _poll_receipts_async(self) -> None:
        if not receipts.due(self.home):
            return
        if not self._poll_guard.acquire(blocking=False):
            return

        def run():
            try:
                self.poll_receipts()
            except Exception as exc:
                self.log(f"回执查询出错：{exc.__class__.__name__}")
            finally:
                self._poll_guard.release()

        threading.Thread(target=run, name="xlt-receipts", daemon=True).start()

    def poll_receipts(self) -> None:
        for msg_id, rec in receipts.due(self.home):
            member = member_by_name(self.home, rec.get("to_member"))
            if member is None:
                receipts.note_poll(self.home, msg_id, no_receipts=True, error="名单里已没有这个人")
                continue
            self._poll_one(msg_id, member)

    def _poll_one(self, msg_id: str, member: dict) -> None:
        trust_path = self.home.peers_dir / f"{member['name']}.crt"
        try:
            trust = trust_path.read_text(encoding="utf-8")
        except OSError:
            receipts.note_poll(self.home, msg_id, error="没有对方证书", retry_in=60)
            return
        for ip, port, _source in _candidates(self.home, member):
            try:
                code, body = request(
                    ip, port, "GET", f"/xlt/v1/receipt?id={msg_id}", b"",
                    self.cert, self.key_path, member.get("cert_sha256"), trust, 3,
                )
            except FingerprintMismatch:
                continue
            except OSError:
                continue
            parsed = _json_body(body)
            err = (parsed.get("error") or {}) if isinstance(parsed, dict) else {}
            if code == 200:
                if parsed.get("state") == "read":
                    receipts.note_poll(self.home, msg_id, state="read", read_at=parsed.get("read_at"))
                    self.log(f"已读回执 id={msg_id} ← {member['name']}")
                else:
                    receipts.note_poll(self.home, msg_id)
                return
            if code == 404 and err.get("code") == "NOT_FOUND":
                # 旧版本通讯员没有这个地址：不再问，这封信停在「已送达」。
                receipts.note_poll(self.home, msg_id, no_receipts=True, error="对方版本不支持回执")
                self.log(f"{member['name']} 的通讯员版本较旧，不支持已读回执，这封信停在「已送达」 id={msg_id}")
                return
            if code == 404 and err.get("code") == "NO_SUCH_MESSAGE":
                receipts.note_poll(self.home, msg_id, no_receipts=True, error="对方那边找不到这封信")
                return
            receipts.note_poll(self.home, msg_id, error=f"HTTP {code}", retry_in=30)
            return
        receipts.note_poll(self.home, msg_id, error="连不上对方", retry_in=30)

    def on_read(self, member: dict, path: str, req_id: str) -> tuple[int, dict]:
        decision = self.gate(member, "read", path=path)
        self.audit(
            req_id=req_id,
            caller=member.get("person"),
            caller_name=member.get("name"),
            caller_fp=member.get("cert_sha256"),
            action="read",
            path=path,
            grant=decision.grant,
            decision="allow" if decision.allow else "deny",
            reason=None if decision.allow else decision.code,
        )
        if not decision.allow:
            self.reject_log(req_id=req_id, caller=member.get("person"), action="read", reason=decision.code, path=path)
            self.log(f"拒绝读文件 {decision.code} caller={member.get('name')}")
            return decision.http, _err(decision.code, decision.message, req_id)
        real = Path(os.path.realpath(path))
        if not real.is_file():
            return 404, _err("NOT_FOUND", "文件不存在", req_id)
        if real.stat().st_size > 512 * 1024:
            return 413, _err("TOO_LARGE", "文件太大", req_id)
        data = real.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return 403, _err("BLOCKED_SENSITIVE", "不是文本文件", req_id)
        blocked = egress_blocked(text)
        if blocked:
            self.log(f"读文件出口拦截 caller={member.get('name')}")
            return 403, _err("BLOCKED_SENSITIVE", blocked, req_id)
        return 200, {"path": str(real), "size": len(data), "truncated": False, "content": text}

    def housekeeping(self) -> None:
        self._expire_pending()
        self._expire_inbox()
        self._downgrade_stale_peers()
        self.flush_due()
        self._poll_receipts_async()

    def _expire_pending(self) -> None:
        for path in list(_active_pending(self.home)):
            try:
                env = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if _expired(env):
                self._move(path, self.home.pending / "expired")
                self.event("expired", env)

    def _expire_inbox(self) -> None:
        for path in list(self.home.inbox.rglob("*.env.json")):
            if "_expired" in path.parts:
                continue
            try:
                env = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            status = (env.get("state") or {}).get("status")
            if status in ("replied", "done", "declined", "expired"):
                continue
            if _expired(env):
                self._move(path, self.home.inbox / "_expired")
                self.log_env("收件过期", env)

    def _downgrade_stale_peers(self) -> None:
        from xlt.store import load_cache, save_cache

        cache = load_cache(self.home)
        changed = False
        for row in cache.get("peers", {}).values():
            seen = row.get("last_seen")
            if row.get("state") == "online" and seen:
                try:
                    age = (now() - parse_time(seen)).total_seconds()
                except Exception:
                    continue
                if age > 600:
                    row["state"] = "offline"
                    changed = True
        if changed:
            save_cache(self.home, cache)

    def _att_lock(self, msg_id: str) -> threading.Lock:
        with self.attach_locks_mu:
            return self.attach_locks.setdefault(msg_id, threading.Lock())

    def _send_attachments(self, env_path: Path, env: dict, member: dict, ip: str, port: int, trust: str) -> dict:
        pause = float(os.environ.get("XLT_ATTACH_SLEEP", "0") or 0)
        for att in list(env.get("attachments") or []):
            source = Path(att.get("source") or "")
            reason = file_egress_blocked(source) if source else "附件源文件不见了"
            if reason:
                self._move(env_path, self.home.pending / "failed")
                self.event("failed", env, reason="EGRESS")
                return {"id": env["id"], "status": "rejected", "reason": "EGRESS", "message": reason, "to": env.get("to_member")}
            try:
                sha, size = hash_and_size(source)
            except OSError as exc:
                self._move(env_path, self.home.pending / "failed")
                return {"id": env["id"], "status": "rejected", "reason": "EGRESS", "message": f"读不了附件 {source.name}：{exc}", "to": env.get("to_member")}
            if sha != att.get("sha256") or size != int(att.get("size") or 0):
                self._move(env_path, self.home.pending / "failed")
                return {
                    "id": env["id"],
                    "status": "rejected",
                    "reason": "FILE_CHANGED",
                    "message": f"附件 {source.name} 在排队期间被改过，已取消发送",
                    "to": env.get("to_member"),
                }
            sent = int(att.get("sent") or 0)
            try:
                code, body = request(
                    ip, port, "GET",
                    f"/xlt/v1/attachments?msg_id={env['id']}&att_id={att['id']}",
                    b"", self.cert, self.key_path, member.get("cert_sha256"), trust, 8,
                )
                if code == 200:
                    got = _json_body(body)
                    sent = max(sent, int(got.get("received") or 0))
                elif code == 404:
                    self._move(env_path, self.home.pending / "failed")
                    return {
                        "id": env["id"],
                        "status": "rejected",
                        "reason": "NO_ATTACH",
                        "message": "对方这一版还不支持附件，请让对方升级小灵通",
                        "to": env.get("to_member"),
                    }
            except FingerprintMismatch:
                self.event("blocked_fingerprint", env, ip=ip)
                return _queued(env, "指纹不符")
            except OSError as exc:
                self._bump(env_path, env, f"附件进度查询失败 {exc.__class__.__name__}")
                return _queued(env, "对方离线，附件会在上线后接着传")
            att["sent"] = sent
            write_json(env_path, env)
            while sent < size:
                length = min(chunk_size(), size - sent)
                try:
                    code, body = request(
                        ip, port, "POST",
                        f"/xlt/v1/attachments?msg_id={env['id']}&att_id={att['id']}&offset={sent}&length={length}",
                        b"",
                        self.cert, self.key_path, member.get("cert_sha256"), trust, 120,
                        content_type="application/octet-stream",
                        body_path=source,
                        body_offset=sent,
                        body_length=length,
                        extra_headers={
                            "X-Xlt-Msg-Id": env["id"],
                            "X-Xlt-Att-Id": att["id"],
                            "X-Xlt-Offset": str(sent),
                            "X-Xlt-Length": str(length),
                        },
                    )
                except FingerprintMismatch:
                    self.event("blocked_fingerprint", env, ip=ip)
                    return _queued(env, "指纹不符")
                except OSError as exc:
                    self._bump(env_path, env, f"附件中断 {exc.__class__.__name__}")
                    return _queued(env, "传输中断，已记下进度，上线后自动续传")
                parsed = _json_body(body)
                if code == 409:
                    sent = int(parsed.get("received") or sent)
                    att["sent"] = sent
                    write_json(env_path, env)
                    continue
                if code == 404:
                    self._move(env_path, self.home.pending / "failed")
                    return {
                        "id": env["id"],
                        "status": "rejected",
                        "reason": "NO_ATTACH",
                        "message": "对方这一版还不支持附件，请让对方升级小灵通",
                        "to": env.get("to_member"),
                    }
                if code not in (200, 202):
                    err = (parsed.get("error") or {}) if isinstance(parsed, dict) else {}
                    reason = err.get("code") or f"HTTP_{code}"
                    message = err.get("message") or "对方拒绝了附件"
                    if code in NO_RETRY:
                        self._move(env_path, self.home.pending / "failed")
                        self.event("failed", env, http=code, reason=reason)
                        return {"id": env["id"], "status": "rejected", "reason": reason, "message": message, "to": env.get("to_member")}
                    self._bump(env_path, env, message)
                    return _queued(env, message)
                sent = int(parsed.get("received") or (sent + length))
                att["sent"] = sent
                write_json(env_path, env)
                if pause > 0:
                    time.sleep(pause)
            att["sent"] = size
            write_json(env_path, env)
        return {"id": env["id"], "status": "delivered"}

    def on_attach_status(self, member: dict, msg_id: str, att_id: str, req_id: str) -> tuple[int, dict]:
        with self.home.lock():
            found = self._find_env(msg_id)
            if found is None:
                return 404, _err("NOT_FOUND", "没有这封信的附件", req_id)
            env = json.loads(found.read_text(encoding="utf-8"))
            if env.get("from") != member.get("name"):
                return 401, _err("IDENTITY_MISMATCH", "这封信不是你发的", req_id)
            att = _find_att(env, att_id)
            if att is None:
                return 404, _err("NOT_FOUND", "没有这个附件", req_id)
            received = int(att.get("received") or 0)
            size = int(att.get("size") or 0)
            return 200, {"id": att_id, "received": received, "size": size, "complete": received >= size and size >= 0}

    def on_attach_chunk(self, member: dict, msg_id: str, att_id: str, offset: int, length: int, reader, req_id: str) -> tuple[int, dict]:
        if length < 0 or length > chunk_size() * 2:
            return 400, _err("BAD_ENVELOPE", "这一块附件太大或长度无效", req_id)
        if offset < 0:
            return 400, _err("BAD_ENVELOPE", "偏移无效", req_id)
        with self._att_lock(msg_id):
            with self.home.lock():
                found = self._find_env(msg_id)
                if found is None:
                    return 404, _err("NOT_FOUND", "没有这封信的附件", req_id)
                env = json.loads(found.read_text(encoding="utf-8"))
                if env.get("from") != member.get("name"):
                    return 401, _err("IDENTITY_MISMATCH", "这封信不是你发的", req_id)
                att = _find_att(env, att_id)
                if att is None:
                    return 404, _err("NOT_FOUND", "没有这个附件", req_id)
                size = int(att.get("size") or 0)
                received = int(att.get("received") or 0)
                dest = Path(att.get("path") or "")
                if not dest:
                    return 400, _err("BAD_ENVELOPE", "附件路径无效", req_id)
                if offset != received:
                    err = _err("OFFSET", "请从已收下的偏移接着传", req_id)
                    err["received"] = received
                    err["id"] = att_id
                    return 409, err
                if received >= size:
                    return 200, {"id": att_id, "received": received, "complete": True}
                if offset + length > size:
                    return 400, _err("TOO_LARGE", "这一块超出了文件大小", req_id)
                part = Path(str(dest) + ".part")
                part.parent.mkdir(parents=True, exist_ok=True)
            wrote = 0
            try:
                with part.open("r+b" if part.exists() else "wb") as fh:
                    if part.exists():
                        fh.seek(0, 2)
                        if fh.tell() != offset:
                            fh.seek(offset)
                    fh.seek(offset)
                    left = length
                    while left > 0:
                        piece = reader.read(min(65536, left))
                        if not piece:
                            break
                        fh.write(piece)
                        left -= len(piece)
                        wrote += len(piece)
            except OSError as exc:
                return 500, _err("BUSY", f"写附件失败：{exc}", req_id)
            if wrote != length:
                return 400, _err("BAD_ENVELOPE", "这一块没传完", req_id)
            chmod_private(part)
            finish_job = None
            with self.home.lock():
                env = json.loads(found.read_text(encoding="utf-8"))
                att = _find_att(env, att_id)
                if att is None:
                    return 404, _err("NOT_FOUND", "没有这个附件", req_id)
                att["received"] = offset + wrote
                complete = att["received"] >= int(att.get("size") or 0)
                if complete:
                    if not verify_file_hash(part, att.get("sha256") or ""):
                        try:
                            part.unlink()
                        except OSError:
                            pass
                        att["received"] = 0
                        write_json(found, env)
                        err = _err("HASH_MISMATCH", f"附件 {att.get('name')} 校验失败，请重传", req_id)
                        err["received"] = 0
                        return 409, err
                    dest = Path(att["path"])
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    part.replace(dest)
                    chmod_private(dest)
                    att["complete"] = True
                    att["path"] = str(dest)
                write_json(found, env)
                if complete and _attachments_done(env):
                    finish_job = self._finish_attachments(found, env)
            if finish_job:
                self._submit(finish_job)
            return 200, {"id": att_id, "received": offset + wrote, "complete": complete}

    def _finish_attachments(self, env_path: Path, env: dict):
        if env.get("attachments_listed"):
            window = find_window(self.home, env_path.parent.name)
            return (window, env_path) if window and env_path.parent.name != "_hold" else None
        dest_dir = attachment_dir(self.home, env["id"])
        letter_path = env_path.with_name(f"{env['id']}.letter.md")
        extra = describe_attachments(env.get("attachments") or [], dest_dir)
        if extra and letter_path.exists():
            with letter_path.open("a", encoding="utf-8") as fh:
                fh.write(extra)
        env["attachments_listed"] = True
        state = env.setdefault("state", {})
        state["status"] = "queued"
        state["updated"] = now_iso()
        hist = state.setdefault("history", [])
        hist.append(["queued", now_iso()])
        write_json(env_path, env)
        self.log_env("附件收齐，交给窗口", env)
        if env_path.parent.name == "_hold":
            return None
        window = find_window(self.home, env_path.parent.name)
        return (window, env_path) if window else None

    def _find_env(self, msg_id: str) -> Path | None:
        if not msg_id:
            return None
        for path in self.home.inbox.rglob(f"{msg_id}.env.json"):
            return path
        return None


def takeover_mail(home: Home, window_name: str, generation: int) -> int:
    """同名再登记：未送达的信重新排队，暂存在 _hold 且指向这个窗口的信搬过来。"""
    moved = 0
    folder = home.inbox / window_name
    folder.mkdir(parents=True, exist_ok=True)
    paths = list(folder.glob("*.env.json"))
    hold = home.inbox / "_hold"
    if hold.exists():
        for path in hold.glob("*.env.json"):
            try:
                env = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            routed = (env.get("state") or {}).get("routed_to") or ""
            if routed.endswith("/" + window_name):
                dest = folder / path.name
                letter = path.with_name(path.name.replace(".env.json", ".letter.md"))
                os.replace(path, dest)
                if letter.exists():
                    os.replace(letter, folder / letter.name)
                paths.append(dest)
    for path in paths:
        try:
            env = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        state = env.setdefault("state", {})
        if state.get("status") not in ("queued", "delivered", "delivering", "awaiting_manual", None):
            continue
        state["status"] = "queued"
        state["taken_over"] = True
        state["binding_gen"] = generation
        state["updated"] = now_iso()
        state.setdefault("history", []).append(["queued", state["updated"]])
        state["routed_to"] = state.get("routed_to") or window_name
        write_json(path, env)
        moved += 1
    return moved


def _channel_lost(home: Home, window: dict) -> bool:
    if window.get("delivery") not in ("channel", "extension"):
        return False
    alive = home.d("channels", f"{window.get('window')}.alive")
    if not alive.exists():
        return False
    return time.time() - alive.stat().st_mtime > 90


def _window_by_ref(home: Home, ref: str | None):
    if not ref or "/" not in str(ref):
        return None
    _agent, _sep, name = str(ref).partition("/")
    return find_window(home, name)


def _rule_match(rule: dict, env: dict) -> bool:
    match = rule.get("match") or {}
    if "type" in match and match.get("type") != env.get("type"):
        return False
    if "from" in match:
        allowed = match.get("from")
        if isinstance(allowed, str):
            allowed = [allowed]
        who = {env.get("from"), env.get("from_person")}
        if not who.intersection(set(allowed or [])):
            return False
    if "tags" in match:
        need = match.get("tags")
        if isinstance(need, str):
            need = [need]
        have = set(env.get("tags") or [])
        if not set(need or []).issubset(have):
            return False
    return True


def _expired(env: dict) -> bool:
    try:
        created = parse_time(env["created"])
        ttl = int(env.get("ttl_s") or 86400)
    except Exception:
        return False
    return (now() - created).total_seconds() > ttl


def _expires(env: dict) -> str:
    try:
        created = parse_time(env["created"])
        from datetime import timedelta

        return (created + timedelta(seconds=int(env.get("ttl_s") or 86400))).isoformat(timespec="seconds")
    except Exception:
        return ""


def _candidates(home: Home, member: dict) -> list[tuple[str, int, str]]:
    from xlt.store import load_cache

    cache = (load_cache(home).get("peers") or {}).get(member["name"]) or {}
    found = []
    if cache.get("ip"):
        found.append((cache["ip"], int(cache.get("port") or member.get("port") or 47800), "cache"))
    if member.get("lan_ip"):
        found.append((member["lan_ip"], int(member.get("port") or 47800), "static"))
    if member.get("tailscale_ip"):
        found.append((member["tailscale_ip"], int(member.get("port") or 47800), "tailscale"))
    unique = []
    seen = set()
    for ip, port, source in found:
        key = (ip, port)
        if key in seen:
            continue
        seen.add(key)
        unique.append((ip, port, source))
    return unique


def _public(env: dict) -> dict:
    from xlt.common import wire_view

    out = wire_view(env)
    out["sig"] = env.get("sig")
    return out


def _validate_incoming_attachments(items) -> str | None:
    if not items:
        return None
    if not isinstance(items, list) or len(items) > 64:
        return "附件列表无效（最多 64 个）"
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            return "附件元数据格式不对"
        for key in ("id", "name", "size", "sha256"):
            if item.get(key) in ("", None):
                return f"附件缺少 {key}"
        try:
            size = int(item.get("size"))
        except (TypeError, ValueError):
            return "附件大小无效"
        if size < 0:
            return "附件大小无效"
        if item["id"] in seen:
            return "附件 id 重复"
        seen.add(item["id"])
    return None


def _incoming_attachment_state(home, msg_id: str, items) -> list[dict]:
    folder = attachment_dir(home, msg_id)
    out = []
    for item in public_attachments(items):
        dest = unique_path(folder, item["name"])
        out.append(
            {
                "id": item["id"],
                "name": dest.name,
                "size": int(item["size"] or 0),
                "sha256": item["sha256"],
                "received": 0,
                "path": str(dest),
            }
        )
    return out


def _resume_payload(path: Path) -> dict:
    try:
        env = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"id": path.stem.replace(".env", ""), "status": "duplicate"}
    payload = {"id": env.get("id"), "status": "duplicate"}
    atts = env.get("attachments") or []
    if atts:
        payload["attachments"] = [
            {"id": item.get("id"), "received": int(item.get("received") or 0)} for item in atts
        ]
        if not _attachments_done(env):
            payload["status"] = "receiving"
    return payload


def _merge_resume(env: dict, offsets: list) -> None:
    by_id = {item.get("id"): item for item in (offsets or []) if isinstance(item, dict)}
    for att in env.get("attachments") or []:
        got = by_id.get(att.get("id"))
        if not got:
            continue
        try:
            received = int(got.get("received") or 0)
        except (TypeError, ValueError):
            continue
        att["sent"] = max(int(att.get("sent") or 0), received)


def _find_att(env: dict, att_id: str):
    for item in env.get("attachments") or []:
        if item.get("id") == att_id:
            return item
    return None


def _finalize_empty_attachments(items: list[dict]) -> bool:
    """把 0 字节附件直接落成空文件并标完。"""
    any_empty = False
    for item in items:
        if int(item.get("size") or 0) != 0:
            continue
        dest = Path(item["path"])
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"")
        from xlt.attach import chmod_private

        chmod_private(dest)
        item["received"] = 0
        item["complete"] = True
        any_empty = True
    return any_empty


def _attachments_done(env: dict) -> bool:
    items = env.get("attachments") or []
    if not items:
        return True
    for item in items:
        size = int(item.get("size") or 0)
        got = int(item.get("received") or 0)
        if got < size or not item.get("complete"):
            return False
    return True


def _active_pending(home: Home) -> list[Path]:
    if not home.pending.exists():
        return []
    found = []
    for path in home.pending.rglob("*.env.json"):
        if "expired" in path.parts or "failed" in path.parts:
            continue
        found.append(path)
    return found


def _next_seq(home: Home) -> int:
    path = home.root / "msg-seq.json"
    from xlt.common import read_json

    data = read_json(path, {"n": 0})
    data["n"] = int(data.get("n") or 0) + 1
    write_json(path, data)
    return data["n"]


def _queued(env: dict, message: str) -> dict:
    return {
        "id": env.get("id"),
        "status": "queued",
        "reason": "PEER_OFFLINE",
        "message": message,
        "to": env.get("to_member") or env.get("to"),
        "expires": _expires(env),
    }


def _err(code: str, message: str, req_id: str) -> dict:
    return {"error": {"code": code, "message": message, "request_id": req_id}}


def _json_body(body: bytes) -> dict:
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        return {}


def _extract_text(text: str, session_path: Path) -> str:
    if not text:
        return ""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(data, dict):
        session = data.get("session_id") or data.get("sessionId")
        if session:
            session_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_text(session_path, str(session))
        result = data.get("result")
        if isinstance(result, str) and result.strip():
            return result.strip()
    return text
