from __future__ import annotations

import base64
import json

from billing.wechatpay import (
    build_v3_authorization_header,
    decrypt_notification,
    sign_rsa_sha256_base64,
    verify_wechatpay_notify_signature,
)


def _extract_token_field(token: str, field: str) -> str:
    marker = f'{field}="'
    start = token.find(marker)
    assert start >= 0, token
    start += len(marker)
    end = token.find('"', start)
    assert end > start, token
    return token[start:end]


def _make_self_signed_cert_pem() -> tuple[str, str]:
    """
    Returns (private_key_pem, cert_pem).
    """

    from datetime import datetime, timedelta, timezone

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "CN"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "AntiHub Test"),
            x509.NameAttribute(NameOID.COMMON_NAME, "wechatpay-platform"),
        ]
    )
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(private_key, hashes.SHA256())
    )
    private_key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
    return private_key_pem, cert_pem


def test_decrypt_notification_roundtrip() -> None:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    api_v3_key = "0123456789abcdef0123456789abcdef"  # 32 bytes
    plaintext = {"out_trade_no": "ord_001", "trade_state": "SUCCESS", "amount": {"total": 9900, "currency": "CNY"}}
    aad = "associated-data"
    nonce = "0123456789ab"  # 12 bytes recommended by AESGCM
    aesgcm = AESGCM(api_v3_key.encode("utf-8"))
    ciphertext = aesgcm.encrypt(nonce.encode("utf-8"), json.dumps(plaintext).encode("utf-8"), aad.encode("utf-8"))
    resource = {
        "algorithm": "AEAD_AES_256_GCM",
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        "nonce": nonce,
        "associated_data": aad,
    }

    decrypted = decrypt_notification(api_v3_key=api_v3_key, resource=resource)
    assert decrypted == plaintext


def test_verify_wechatpay_notify_signature() -> None:
    private_key_pem, cert_pem = _make_self_signed_cert_pem()
    body = json.dumps({"id": "evt_1"}, separators=(",", ":")).encode("utf-8")
    timestamp = "1700000000"
    nonce = "nonce"
    msg = f"{timestamp}\n{nonce}\n{body.decode('utf-8')}\n".encode("utf-8")
    signature = sign_rsa_sha256_base64(private_key_pem=private_key_pem, message=msg)

    platform_certs = {"serial_1": cert_pem}
    assert (
        verify_wechatpay_notify_signature(
            timestamp=timestamp,
            nonce=nonce,
            signature_b64=signature,
            serial="serial_1",
            body=body,
            platform_certs=platform_certs,
        )
        is True
    )
    assert (
        verify_wechatpay_notify_signature(
            timestamp=timestamp,
            nonce=nonce,
            signature_b64="invalid",
            serial="serial_1",
            body=body,
            platform_certs=platform_certs,
        )
        is False
    )


def test_build_v3_authorization_header_signature_matches() -> None:
    private_key_pem, _ = _make_self_signed_cert_pem()
    body = json.dumps({"a": 1}, separators=(",", ":"))
    token = build_v3_authorization_header(
        mchid="1900000001",
        serial_no="serial_merchant",
        private_key_pem=private_key_pem,
        method="POST",
        path_with_query="/v3/pay/transactions/jsapi",
        body=body,
        timestamp="1700000000",
        nonce_str="nonce",
    )
    signature = _extract_token_field(token, "signature")
    msg = f"POST\n/v3/pay/transactions/jsapi\n1700000000\nnonce\n{body}\n".encode("utf-8")
    expected = sign_rsa_sha256_base64(private_key_pem=private_key_pem, message=msg)
    assert signature == expected

