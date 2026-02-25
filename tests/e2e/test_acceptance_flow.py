from __future__ import annotations

import os

import httpx
import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

RUN_E2E = os.getenv("RUN_E2E", "").strip() == "1"
FRONTEND_URL = os.getenv("E2E_FRONTEND_URL", "http://127.0.0.1:5173").rstrip("/")
API_URL = os.getenv("E2E_API_URL", "http://127.0.0.1:8010").rstrip("/")
LOGIN_USER = os.getenv("E2E_USER", "Zed")
LOGIN_PASSWORD = os.getenv("E2E_PASSWORD", "AFKzzd123")

DEFAULT_PLANS = [
    {"code": "plan_monthly", "name": "月付", "price_cents": 19800, "monthly_points": 1980, "currency": "cny"},
    {"code": "plan_quarterly", "name": "季付", "price_cents": 39800, "monthly_points": 3980, "currency": "cny"},
    {"code": "plan_yearly", "name": "年付", "price_cents": 198000, "monthly_points": 19800, "currency": "cny"},
]


def _api_login(client: httpx.Client, api_url: str, username: str, password: str) -> str:
    response = client.post(
        f"{api_url}/auth/login",
        json={"username": username, "password": password},
    )
    response.raise_for_status()
    data = response.json()
    token = str(data.get("access_token") or "")
    if not token:
        raise RuntimeError("missing access_token from /auth/login")
    return token


def _ensure_pricing_plans(client: httpx.Client, api_url: str, token: str) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    response = client.get(f"{api_url}/billing/plans", headers=headers)
    response.raise_for_status()
    plans = response.json()
    existing_codes = {str(item.get("code") or "").strip() for item in plans if isinstance(item, dict)}

    for plan in DEFAULT_PLANS:
        if plan["code"] in existing_codes:
            continue
        create_resp = client.post(
            f"{api_url}/admin/billing/plans",
            headers=headers,
            json=plan,
        )
        create_resp.raise_for_status()


def _simulate_payment(client: httpx.Client, api_url: str, token: str, external_order_id: str) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    response = client.post(
        f"{api_url}/billing/dev/simulate-payment",
        headers=headers,
        json={"external_order_id": external_order_id},
    )
    response.raise_for_status()


@pytest.mark.e2e
@pytest.mark.skipif(not RUN_E2E, reason="set RUN_E2E=1 to run playwright e2e")
def test_pricing_payment_and_recommendations_flow() -> None:
    with httpx.Client(timeout=15.0, trust_env=False) as api_client:
        token = _api_login(api_client, API_URL, LOGIN_USER, LOGIN_PASSWORD)
        _ensure_pricing_plans(api_client, API_URL, token)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()

            page.goto(FRONTEND_URL, wait_until="domcontentloaded", timeout=30_000)

            page.get_by_role("textbox", name="用户名").fill(LOGIN_USER)
            page.get_by_label("密码").fill(LOGIN_PASSWORD)
            page.get_by_role("button", name="登录").click()
            page.locator("[data-testid='page-create']").wait_for(timeout=20_000)

            page.get_by_role("button", name="会员 套餐与积分").click()
            page.locator("[data-testid='page-billing']").wait_for(timeout=10_000)

            subscribe_button = page.locator("[data-testid='page-billing'] .plan-actions button").first
            subscribe_button.click()
            page.get_by_text("微信扫码支付").wait_for(timeout=10_000)

            order_id = page.locator(".payment-qr-meta .mono").first.inner_text().strip()
            _simulate_payment(api_client, API_URL, token, order_id)

            try:
                page.get_by_text("支付成功", exact=True).wait_for(timeout=25_000)
            except PlaywrightTimeoutError:
                # fallback: rely on state refresh card if modal animation already ended
                page.locator("[data-testid='page-billing']").wait_for(timeout=5_000)
            page.get_by_text("Active").wait_for(timeout=25_000)

            page.get_by_role("button", name="控制台 仓库讲解").click()
            page.locator("[data-testid='page-create']").wait_for(timeout=10_000)

            page.get_by_role("textbox", name="需求摘要").fill("CRM")
            page.get_by_role("button", name="智能推荐方案").click()
            page.get_by_text("匹配结果").wait_for(timeout=30_000)

            # Hybrid ranking should contain both commercial and open-source options.
            page.get_by_text("Salesforce", exact=False).first.wait_for(timeout=30_000)
            page.get_by_text("Odoo", exact=False).first.wait_for(timeout=30_000)

            context.close()
            browser.close()
