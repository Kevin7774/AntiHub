from __future__ import annotations

import abc
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Literal, Optional

import httpx

from config import (
    PAYMENT_PROVIDER,
    WECHATPAY_API_BASE_URL,
    WECHATPAY_APPID,
    WECHATPAY_CERT_SERIAL,
    WECHATPAY_MCHID,
    WECHATPAY_NOTIFY_URL,
    WECHATPAY_PRIVATE_KEY_PATH,
    WECHATPAY_PRIVATE_KEY_PEM,
)

from .wechatpay import build_v3_authorization_header, load_pem

ProviderName = Literal["mock", "stripe", "wechatpay"]


@dataclass(frozen=True)
class CheckoutSession:
    """
    Normalized provider checkout session payload.

    We keep this intentionally small so providers can evolve without forcing a
    schema migration or front-end changes.
    """

    provider: ProviderName
    checkout_url: str
    external_reference: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


class BasePaymentProvider(abc.ABC):
    @property
    @abc.abstractmethod
    def name(self) -> ProviderName:
        raise NotImplementedError

    @abc.abstractmethod
    def create_checkout_session(
        self,
        *,
        user_id: str,
        plan_code: str,
        amount_cents: int,
        currency: str,
        external_order_id: str,
        success_url: str,
        cancel_url: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> CheckoutSession:
        """
        Create a provider-side checkout session (or equivalent).

        NOTE: The current system uses an internal webhook signature secret
        (PAYMENT_WEBHOOK_SECRET). A real Stripe integration should verify the
        Stripe webhook signature separately in a dedicated endpoint.
        """


class MockProvider(BasePaymentProvider):
    @property
    def name(self) -> ProviderName:
        return "mock"

    def create_checkout_session(
        self,
        *,
        user_id: str,
        plan_code: str,
        amount_cents: int,
        currency: str,
        external_order_id: str,
        success_url: str,
        cancel_url: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> CheckoutSession:
        _ = (user_id, plan_code, amount_cents, currency, success_url, cancel_url, metadata)
        # Development-only "WeChat-like" URL used to render a QR code in the UI.
        # This is intentionally non-functional and should never be treated as a real payment URL.
        url = f"weixin://mock/pay?id={external_order_id}"
        return CheckoutSession(provider="mock", checkout_url=url, external_reference=external_order_id, raw={})


class StripeProvider(BasePaymentProvider):
    @property
    def name(self) -> ProviderName:
        return "stripe"

    def create_checkout_session(
        self,
        *,
        user_id: str,
        plan_code: str,
        amount_cents: int,
        currency: str,
        external_order_id: str,
        success_url: str,
        cancel_url: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> CheckoutSession:
        _ = (user_id, plan_code, amount_cents, currency, external_order_id, success_url, cancel_url, metadata)
        raise NotImplementedError("StripeProvider is a scaffold only; wire it to stripe-python when ready.")


class WechatPayProvider(BasePaymentProvider):
    @property
    def name(self) -> ProviderName:
        return "wechatpay"

    def create_checkout_session(
        self,
        *,
        user_id: str,
        plan_code: str,
        amount_cents: int,
        currency: str,
        external_order_id: str,
        success_url: str,
        cancel_url: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> CheckoutSession:
        _ = (success_url, cancel_url)

        private_key_pem = load_pem(pem_value=WECHATPAY_PRIVATE_KEY_PEM, pem_path=WECHATPAY_PRIVATE_KEY_PATH)
        if not private_key_pem:
            raise ValueError("WECHATPAY_PRIVATE_KEY_PEM/WECHATPAY_PRIVATE_KEY_PATH is missing")
        if not WECHATPAY_NOTIFY_URL:
            raise ValueError("WECHATPAY_NOTIFY_URL is missing")
        if not WECHATPAY_API_BASE_URL:
            raise ValueError("WECHATPAY_API_BASE_URL is missing")

        # Native scan pay: returns a code_url that is rendered as a QR code on PC.
        path = "/v3/pay/transactions/native"
        payload = {
            "appid": WECHATPAY_APPID,
            "mchid": WECHATPAY_MCHID,
            "description": f"AntiHub {plan_code}",
            "out_trade_no": external_order_id,
            "notify_url": WECHATPAY_NOTIFY_URL,
            "amount": {"total": int(amount_cents), "currency": str(currency or "CNY").upper()},
            # Keep metadata for debugging/dispute resolution; do not trust it in webhook processing.
            "attach": json.dumps({"user_id": user_id, "plan_code": plan_code}, ensure_ascii=False),
        }
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        authorization = build_v3_authorization_header(
            mchid=WECHATPAY_MCHID,
            serial_no=WECHATPAY_CERT_SERIAL,
            private_key_pem=private_key_pem,
            method="POST",
            path_with_query=path,
            body=body,
        )
        headers = {
            "Authorization": authorization,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        resp = httpx.post(
            f"{WECHATPAY_API_BASE_URL}{path}",
            content=body.encode("utf-8"),
            headers=headers,
            timeout=20.0,
            trust_env=False,
        )
        resp.raise_for_status()
        data = resp.json() if resp.content else {}
        code_url = str(data.get("code_url") or "").strip()
        if not code_url:
            raise RuntimeError(f"wechatpay code_url missing: status={resp.status_code} body={(resp.text or '')[:200]}")

        return CheckoutSession(
            provider="wechatpay",
            checkout_url=code_url,
            external_reference=external_order_id,
            raw={"code_url": code_url},
        )


def get_payment_provider(name: Optional[str] = None) -> BasePaymentProvider:
    """
    Provider factory.

    If `name` is not provided, reads from config.PAYMENT_PROVIDER.
    """

    selected = (name or PAYMENT_PROVIDER or "mock").strip().lower()
    if selected == "stripe":
        return StripeProvider()
    if selected == "wechatpay":
        return WechatPayProvider()
    return MockProvider()
