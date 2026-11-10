"""自签证书、指纹、ECDSA 签名。算法按方案 §5.3 / §6.6.8。"""

from __future__ import annotations

import base64
import datetime
import os
import stat
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from xlt.common import canon


def fingerprint_bytes(data: bytes) -> str:
    digest = hashes.Hash(hashes.SHA256())
    digest.update(data)
    return ":".join(f"{b:02X}" for b in digest.finalize())


def fingerprint_cert(cert: x509.Certificate) -> str:
    return ":".join(f"{b:02X}" for b in cert.fingerprint(hashes.SHA256()))


def load_cert(path: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate(Path(path).read_bytes())


def load_key(path: Path):
    return serialization.load_pem_private_key(Path(path).read_bytes(), password=None)


def cert_from_der(der: bytes) -> x509.Certificate:
    return x509.load_der_x509_certificate(der)


def generate_cert(name: str, out_dir: Path) -> tuple[Path, Path, str]:
    """EC P-256，825 天，serverAuth + clientAuth。私钥只留在本机。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    moment = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(moment - datetime.timedelta(minutes=5))
        .not_valid_after(moment + datetime.timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(f"{name}.xlt.local")]), critical=False)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    key_path = out_dir / f"{name}.key"
    crt_path = out_dir / f"{name}.crt"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    crt_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    if os.name != "nt":
        os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR)
        os.chmod(crt_path, stat.S_IRUSR | stat.S_IWUSR)
    return key_path, crt_path, fingerprint_cert(cert)


def sign_obj(key, obj: dict) -> str:
    payload = canon({k: v for k, v in obj.items() if k != "sig"})
    return base64.b64encode(key.sign(payload, ec.ECDSA(hashes.SHA256()))).decode("ascii")


def verify_obj(cert: x509.Certificate, obj: dict) -> bool:
    sig = obj.get("sig")
    if not sig:
        return False
    payload = canon({k: v for k, v in obj.items() if k != "sig"})
    try:
        cert.public_key().verify(base64.b64decode(sig), payload, ec.ECDSA(hashes.SHA256()))
        return True
    except Exception:
        return False


def sign_envelope(key, env: dict) -> dict:
    from xlt.common import wire_view

    signed = wire_view(env)
    signed["sig"] = sign_obj(key, signed)
    return signed


def verify_envelope(cert: x509.Certificate, env: dict) -> bool:
    from xlt.common import wire_view

    signed = wire_view(env)
    signed["sig"] = env.get("sig")
    return verify_obj(cert, signed)
