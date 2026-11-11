"""双向 TLS。自签证书放进信任库，连上之后再核对成员名单里的指纹。"""

from __future__ import annotations

import socket
import ssl
from pathlib import Path

from xlt.crypto import cert_from_der, fingerprint_cert, load_cert
from xlt.store import Home, load_directory


class FingerprintMismatch(Exception):
    def __init__(self, got: str):
        super().__init__("证书指纹不符")
        self.got = got


def trust_pems(home: Home) -> str:
    chunks = []
    for agent in load_directory(home):
        path = home.peers_dir / f"{agent.get('name')}.crt"
        if not path.exists():
            continue
        try:
            cert = load_cert(path)
        except Exception:
            continue
        if fingerprint_cert(cert).upper() != str(agent.get("cert_sha256") or "").upper():
            continue
        chunks.append(path.read_text(encoding="utf-8"))
    return "\n".join(chunks)


def context(cert: Path, key: Path, server: bool, trust: str) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER if server else ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.load_cert_chain(str(cert), str(key))
    if trust.strip():
        ctx.load_verify_locations(cadata=trust)
    # 服务端用 OPTIONAL：新同事的证明连接在对方还没登记我时也能完成握手；
    # 是否放行仍由应用层按成员名单的指纹判断，没有证书或名单里没有就是 401。
    ctx.verify_mode = ssl.CERT_OPTIONAL if server else ssl.CERT_REQUIRED
    return ctx


def peer_fingerprint(ssock: ssl.SSLSocket) -> str:
    der = ssock.getpeercert(True)
    if not der:
        raise FingerprintMismatch("")
    return fingerprint_cert(cert_from_der(der))


def request(
    ip: str,
    port: int,
    method: str,
    path: str,
    body: bytes,
    cert: Path,
    key: Path,
    expect_fp: str,
    trust_pem: str,
    timeout: float,
    content_type: str = "application/json",
    body_path: Path | None = None,
    body_offset: int = 0,
    body_length: int | None = None,
    extra_headers: dict | None = None,
) -> tuple[int, bytes]:
    raw = socket.create_connection((ip, port), timeout=timeout)
    ssock = context(cert, key, server=False, trust=trust_pem).wrap_socket(raw, server_hostname="xlt.local")
    try:
        got = peer_fingerprint(ssock)
        if got.upper() != (expect_fp or "").upper():
            raise FingerprintMismatch(got)
        if body_path is not None:
            length = int(body_length if body_length is not None else Path(body_path).stat().st_size - body_offset)
        else:
            payload = body or b""
            length = len(payload)
        extra = ""
        for key_h, value in (extra_headers or {}).items():
            extra += f"{key_h}: {value}\r\n"
        head = (
            f"{method} {path} HTTP/1.1\r\n"
            f"Host: {ip}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {length}\r\n"
            f"{extra}"
            "Connection: close\r\n"
            "\r\n"
        ).encode("ascii")
        ssock.sendall(head)
        if body_path is not None:
            with Path(body_path).open("rb") as fh:
                fh.seek(int(body_offset or 0))
                left = length
                while left > 0:
                    piece = fh.read(min(65536, left))
                    if not piece:
                        break
                    ssock.sendall(piece)
                    left -= len(piece)
        else:
            if payload:
                ssock.sendall(payload)
        chunks = []
        while True:
            piece = ssock.recv(65536)
            if not piece:
                break
            chunks.append(piece)
            if sum(len(item) for item in chunks) > 2_000_000:
                break
    finally:
        ssock.close()
    data = b"".join(chunks)
    header, _, resp = data.partition(b"\r\n\r\n")
    parts = header.split()
    if len(parts) < 2:
        raise OSError("对方没有返回 HTTP 状态")
    return int(parts[1]), resp


def fetch_server_pem(ip: str, port: int, expect_fp: str, timeout: float = 2.0) -> str | None:
    """在还没有对方 PEM 时，从宣布的 TCP 端口取出服务器证书并核对指纹。不出示客户端证书，以免对方尚未登记我时握手失败。"""
    from xlt.crypto import cert_to_pem

    raw = socket.create_connection((ip, int(port)), timeout=timeout)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ssock = ctx.wrap_socket(raw, server_hostname="xlt.local")
    try:
        der = ssock.getpeercert(True)
        if not der:
            return None
        got = cert_from_der(der)
        if fingerprint_cert(got).upper() != (expect_fp or "").upper():
            return None
        return cert_to_pem(got)
    finally:
        ssock.close()


def prove_peer(ip: str, port: int, expect_fp: str, trust_pem: str, timeout: float = 2.0) -> str | None:
    """用声称的证书作信任锚，确认该地址出示的就是这张证书。不出示客户端证书。

    成功返回 None；失败返回原因（指纹不符或连不上）。
    """
    raw = socket.create_connection((ip, int(port)), timeout=timeout)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.load_verify_locations(cadata=trust_pem)
    ctx.verify_mode = ssl.CERT_REQUIRED
    ssock = ctx.wrap_socket(raw, server_hostname="xlt.local")
    try:
        got = peer_fingerprint(ssock)
        if got.upper() != (expect_fp or "").upper():
            return f"对方出示的指纹是 {got}"
        return None
    finally:
        ssock.close()
