from __future__ import annotations

import base64
import json
import secrets
import time
from typing import Any, Mapping


class WechatpayCryptoError(RuntimeError):
    pass


def _normalize_pem(value: str) -> str:
    pem = str(value or "").strip()
    if not pem:
        return ""
    # Allow storing PEM in env vars with literal "\n" separators.
    if "\\n" in pem and "BEGIN" in pem:
        pem = pem.replace("\\n", "\n")
    return pem


def _read_text(path: str) -> str:
    if not path:
        return ""
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def load_pem(*, pem_value: str = "", pem_path: str = "") -> str:
    pem = _normalize_pem(pem_value)
    if pem:
        return pem
    if pem_path:
        return _normalize_pem(_read_text(pem_path))
    return ""


def parse_platform_certs(
    *,
    certs_json: str = "",
    cert_serial: str = "",
    cert_pem: str = "",
    cert_path: str = "",
) -> dict[str, str]:
    """
    Build a serial_no -> cert_pem mapping.

    Supports:
    - WECHATPAY_PLATFORM_CERTS_JSON: {"SERIAL":"-----BEGIN CERTIFICATE-----..."}
    - WECHATPAY_PLATFORM_CERT_SERIAL + (PEM or PATH)
    """

    out: dict[str, str] = {}
    raw_json = str(certs_json or "").strip()
    if raw_json:
        try:
            parsed = json.loads(raw_json)
        except Exception as exc:  # noqa: BLE001
            raise WechatpayCryptoError("WECHATPAY_PLATFORM_CERTS_JSON must be valid JSON") from exc
        if not isinstance(parsed, dict):
            raise WechatpayCryptoError("WECHATPAY_PLATFORM_CERTS_JSON must be a JSON object")
        for key, value in parsed.items():
            if not isinstance(key, str) or not isinstance(value, str):
                continue
            serial = key.strip()
            pem = _normalize_pem(value)
            if serial and pem:
                out[serial] = pem

    serial = str(cert_serial or "").strip()
    pem = load_pem(pem_value=cert_pem, pem_path=cert_path)
    if serial and pem:
        out[serial] = pem

    return out


def _require_cryptography() -> None:
    try:
        import cryptography  # noqa: F401
    except ModuleNotFoundError as exc:
        raise WechatpayCryptoError("cryptography is required for WeChat Pay support") from exc


def sign_rsa_sha256_base64(*, private_key_pem: str, message: bytes) -> str:
    _require_cryptography()
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    pem = _normalize_pem(private_key_pem)
    if not pem:
        raise WechatpayCryptoError("missing merchant private key PEM")
    key = serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise WechatpayCryptoError("merchant private key must be RSA")
    signature = key.sign(message, padding.PKCS1v15(), hashes.SHA256())
    return base64.b64encode(signature).decode("ascii")


def verify_rsa_sha256_base64_with_cert(*, cert_pem: str, message: bytes, signature_b64: str) -> bool:
    _require_cryptography()
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    pem = _normalize_pem(cert_pem)
    if not pem:
        return False
    try:
        cert = x509.load_pem_x509_certificate(pem.encode("utf-8"))
        public_key = cert.public_key()
        if not isinstance(public_key, rsa.RSAPublicKey):
            return False
        signature = base64.b64decode(signature_b64.encode("ascii"))
        public_key.verify(signature, message, padding.PKCS1v15(), hashes.SHA256())
    except Exception:
        return False
    return True


def build_v3_authorization_header(
    *,
    mchid: str,
    serial_no: str,
    private_key_pem: str,
    method: str,
    path_with_query: str,
    body: str,
    timestamp: str | None = None,
    nonce_str: str | None = None,
) -> str:
    """
    Build WECHATPAY2-SHA256-RSA2048 Authorization header for WeChat Pay v3.

    Message format:
      HTTPMethod\n
      URLPath\n
      Timestamp\n
      NonceStr\n
      Body\n
    """

    mchid_value = str(mchid or "").strip()
    serial_value = str(serial_no or "").strip()
    if not mchid_value:
        raise WechatpayCryptoError("WECHATPAY_MCHID is missing")
    if not serial_value:
        raise WechatpayCryptoError("WECHATPAY_CERT_SERIAL is missing")

    ts = str(timestamp or str(int(time.time()))).strip()
    nonce = str(nonce_str or secrets.token_urlsafe(16)).strip()
    msg = f"{method.upper()}\n{path_with_query}\n{ts}\n{nonce}\n{body}\n".encode("utf-8")
    signature = sign_rsa_sha256_base64(private_key_pem=private_key_pem, message=msg)
    token = (
        "WECHATPAY2-SHA256-RSA2048 "
        f'mchid="{mchid_value}",nonce_str="{nonce}",timestamp="{ts}",serial_no="{serial_value}",signature="{signature}"'
    )
    return token


def build_jsapi_pay_params(
    *,
    appid: str,
    prepay_id: str,
    private_key_pem: str,
    timestamp: str | None = None,
    nonce_str: str | None = None,
) -> dict[str, str]:
    """
    Build JSAPI pay params for front-end wx.requestPayment.

    paySign message:
      appId\ntimestamp\nnonceStr\npackage\n
    """

    appid_value = str(appid or "").strip()
    prepay_value = str(prepay_id or "").strip()
    if not appid_value:
        raise WechatpayCryptoError("WECHATPAY_APPID is missing")
    if not prepay_value:
        raise WechatpayCryptoError("missing prepay_id")

    ts = str(timestamp or str(int(time.time()))).strip()
    nonce = str(nonce_str or secrets.token_urlsafe(16)).strip()
    package = f"prepay_id={prepay_value}"
    msg = f"{appid_value}\n{ts}\n{nonce}\n{package}\n".encode("utf-8")
    pay_sign = sign_rsa_sha256_base64(private_key_pem=private_key_pem, message=msg)
    return {
        "appId": appid_value,
        "timeStamp": ts,
        "nonceStr": nonce,
        "package": package,
        "signType": "RSA",
        "paySign": pay_sign,
    }


def verify_wechatpay_notify_signature(
    *,
    timestamp: str,
    nonce: str,
    signature_b64: str,
    serial: str,
    body: bytes,
    platform_certs: Mapping[str, str],
) -> bool:
    """
    Verify WeChat Pay notification signature.

    Signature message:
      timestamp\nnonce\nbody\n

    Notes:
    - Signature algorithm is RSA-SHA256 with PKCS1v1.5 padding.
    - The public key is obtained from the WeChat platform certificate identified by `Wechatpay-Serial`.
    - We verify against the *raw* HTTP body bytes, decoded as UTF-8 for message construction.
    """

    ts = str(timestamp or "").strip()
    nonce_value = str(nonce or "").strip()
    sig = str(signature_b64 or "").strip()
    serial_value = str(serial or "").strip()
    if not ts or not nonce_value or not sig or not serial_value:
        return False

    cert_pem = str(platform_certs.get(serial_value) or "").strip()
    if not cert_pem:
        return False

    try:
        body_text = body.decode("utf-8")
    except Exception:
        return False
    msg = f"{ts}\n{nonce_value}\n{body_text}\n".encode("utf-8")
    return verify_rsa_sha256_base64_with_cert(cert_pem=cert_pem, message=msg, signature_b64=sig)


def decrypt_resource(*, api_v3_key: str, nonce: str, ciphertext_b64: str, associated_data: str | None) -> bytes:
    """
    Decrypt a WeChat Pay v3 "resource" payload.

    WeChat uses AES-256-GCM with:
    - key: `WECHATPAY_APIV3_KEY` (32 bytes)
    - nonce: `resource.nonce` (UTF-8 bytes)
    - aad: `resource.associated_data` (optional, UTF-8 bytes)
    - ciphertext: `resource.ciphertext` (base64, includes GCM tag)
    """

    _require_cryptography()
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = str(api_v3_key or "").encode("utf-8")
    if len(key) != 32:
        raise WechatpayCryptoError("WECHATPAY_APIV3_KEY must be 32 bytes")
    aesgcm = AESGCM(key)

    nonce_bytes = str(nonce or "").encode("utf-8")
    if not nonce_bytes:
        raise WechatpayCryptoError("missing resource.nonce")

    if not ciphertext_b64:
        raise WechatpayCryptoError("missing resource.ciphertext")
    ciphertext = base64.b64decode(ciphertext_b64.encode("ascii"))

    aad_bytes = associated_data.encode("utf-8") if associated_data is not None else None
    return aesgcm.decrypt(nonce_bytes, ciphertext, aad_bytes)


def decrypt_notification(*, api_v3_key: str, resource: Mapping[str, Any]) -> dict[str, Any]:
    """
    Decrypt and parse a WeChat Pay v3 webhook notification resource.

    Returns the decrypted transaction object as a JSON dict.
    Raises WechatpayCryptoError on:
    - unsupported algorithm
    - AES-GCM decryption failure
    - invalid JSON
    """

    algo = str(resource.get("algorithm") or "").strip()
    if algo and algo != "AEAD_AES_256_GCM":
        raise WechatpayCryptoError(f"unsupported algorithm: {algo}")

    plaintext = decrypt_resource(
        api_v3_key=api_v3_key,
        nonce=str(resource.get("nonce") or ""),
        ciphertext_b64=str(resource.get("ciphertext") or ""),
        associated_data=str(resource.get("associated_data") or "") if resource.get("associated_data") is not None else None,
    )
    try:
        parsed = json.loads(plaintext.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise WechatpayCryptoError("decrypted resource is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise WechatpayCryptoError("decrypted resource must be a JSON object")
    return parsed
