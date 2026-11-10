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
    ctx.verify_mode = ssl.CERT_REQUIRED
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
) -> tuple[int, bytes]:
    raw = socket.create_connection((ip, port), timeout=timeout)
    ssock = context(cert, key, server=False, trust=trust_pem).wrap_socket(raw, server_hostname="xlt.local")
    try:
        got = peer_fingerprint(ssock)
        if got.upper() != (expect_fp or "").upper():
            raise FingerprintMismatch(got)
        payload = body or b""
        head = (
            f"{method} {path} HTTP/1.1\r\n"
            f"Host: {ip}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(payload)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("ascii")
        ssock.sendall(head + payload)
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
