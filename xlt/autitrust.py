"""局域网自动登记同事。只授予普通写信权限，不采信广播里任何额外授权声明。"""

from __future__ import annotations

from pathlib import Path

from xlt.crypto import fingerprint_cert, load_cert_pem, verify_obj
from xlt.mtls import fetch_server_pem, prove_peer
from xlt.policy import LETTER_OPS, ensure_letter_grant
from xlt.store import (
    Home,
    load_cache,
    load_config,
    member_by_name,
    person_from_machine,
    save_cache,
    trust_member,
)

ALLOWED_OS = {"macos", "windows", "linux"}


def auto_trust_enabled(home: Home) -> bool:
    return bool(load_config(home).get("auto_trust", True))


def pem_from_message(msg: dict) -> str:
    raw = msg.get("cert") or msg.get("cert_pem") or ""
    return raw if isinstance(raw, str) else ""


def cert_and_fp(pem: str, claimed_fp: str):
    if not pem or not claimed_fp:
        return None, "广播里没有证书或指纹"
    try:
        cert = load_cert_pem(pem)
    except Exception:
        return None, "证书格式不对"
    got = fingerprint_cert(cert)
    if got.upper() != str(claimed_fp).upper():
        return None, "证书和指纹对不上"
    return cert, None


def resolve_cert(home: Home, msg: dict, ip: str, log) -> tuple[object | None, str, str | None]:
    """取出并核对证书。没有随包附带 PEM 时，向宣布的 TCP 端口取。返回 (cert, pem, error)."""
    claimed = str(msg.get("cert_sha256") or "")
    pem = pem_from_message(msg)
    if pem:
        cert, err = cert_and_fp(pem, claimed)
        return cert, pem, err
    if not claimed:
        return None, "", "广播里没有证书指纹"
    cfg = load_config(home)
    port = int(msg.get("port") or cfg.get("tcp_port") or 47800)
    try:
        fetched = fetch_server_pem(ip, port, claimed, timeout=2.0)
    except OSError as exc:
        return None, "", f"向 {ip}:{port} 取证书失败：{exc}"
    if not fetched:
        return None, "", f"向 {ip}:{port} 取到的证书与广播指纹不符"
    cert, err = cert_and_fp(fetched, claimed)
    if err:
        return None, "", err
    log(f"已从 {ip}:{port} 取回 {msg.get('name')} 的证书")
    return cert, fetched, None


def admit_from_broadcast(home: Home, msg: dict, ip: str, log) -> str | None:
    """验证广播身份并写入名单。成功返回 added / replaced / updated；失败返回 None 且已打日志。

    只会调用 ensure_letter_grant（ask / notify / reply，含这些信上的附件）。
    广播里的 ai_tools、permissions、grants 等字段一律不产生额外授权。
    """
    name = str(msg.get("name") or "")
    cfg = load_config(home)
    if not name or name == cfg.get("me"):
        return None
    claimed = str(msg.get("cert_sha256") or "")
    port = int(msg.get("port") or 47800)
    cert, pem, err = resolve_cert(home, msg, ip, log)
    if err or cert is None or not pem:
        log(f"自动登记失败 {name}：{err or '没有证书'}")
        return None
    if not verify_obj(cert, msg):
        log(f"自动登记失败 {name}：广播签名无效（不能用别人的证书冒充）")
        return None
    try:
        why = prove_peer(ip, port, claimed, pem, timeout=2.0)
    except OSError as exc:
        log(f"自动登记失败 {name}：连不上 {ip}:{port} 做证书核对（{exc}）")
        return None
    if why:
        log(f"自动登记失败 {name}：{ip}:{port} 上的证书与广播不符（{why}），未写入名单")
        return None

    existing = member_by_name(home, name)
    old_fp = str((existing or {}).get("cert_sha256") or "")
    if existing and old_fp.upper() == claimed.upper():
        return "updated"

    person = str(msg.get("person") or "").strip() or person_from_machine(name)
    owner = str(msg.get("owner") or "").strip() or person
    system = str(msg.get("os") or "").strip().lower()
    if system not in ALLOWED_OS:
        system = (existing or {}).get("os") or "macos"
    tmp = home.d("tmp")
    tmp.mkdir(parents=True, exist_ok=True)
    crt_path = tmp / f"{name}.auto.crt"
    crt_path.write_text(pem, encoding="utf-8")
    try:
        with home.lock():
            # 再次读名单，避免并发重复写
            existing = member_by_name(home, name)
            old_fp = str((existing or {}).get("cert_sha256") or "")
            if existing and old_fp.upper() == claimed.upper():
                return "updated"
            trust_member(
                home,
                crt_path,
                name,
                person,
                owner,
                system,
                ip,
                port,
                (existing or {}).get("tailscale_ip") or "",
                primary=bool(existing.get("primary")) if existing else True,
            )
            # 只授予普通写信；忽略广播中任何额外权限声明
            ensure_letter_grant(home.root / "policy.yaml", person)
    finally:
        try:
            crt_path.unlink(missing_ok=True)
        except OSError:
            pass

    if existing and old_fp and old_fp.upper() != claimed.upper():
        line = f"警告：证书已替换 {name} 旧指纹 {old_fp} 新指纹 {claimed}"
        log(line)
        _store_warning(home, name, line, ip, port)
        return "replaced"
    log(f"已自动登记 {name}（{person}），授予普通写信权限（{' / '.join(LETTER_OPS)}，含这些信上的附件）")
    return "added"


def _store_warning(home: Home, name: str, line: str, ip: str, port: int) -> None:
    cache = load_cache(home)
    row = dict((cache.get("peers") or {}).get(name) or {})
    row["cert_warning"] = line
    row["ip"] = ip
    row["port"] = port
    cache.setdefault("peers", {})[name] = row
    save_cache(home, cache)
