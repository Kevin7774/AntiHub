#!/usr/bin/env bash
set -euo pipefail

# AntiHub production deploy + mock payment acceptance
#
# Usage:
#   export API_BASE_URL="https://your-domain.example.com/api"
#   export ADMIN_USERNAME="admin"
#   export ADMIN_PASSWORD="***"
#   export PAYMENT_WEBHOOK_SECRET="***"
#   export FEATURE_SAAS_ADMIN_API="true"
#   export FEATURE_SAAS_ENTITLEMENTS="true"
#   ./scripts/deploy_acceptance_mock.sh
#
# Minimal env example (.env.prod):
#   AUTH_ENABLED=true
#   AUTH_TOKEN_SECRET=replace_with_strong_secret
#   PAYMENT_PROVIDER=mock
#   PAYMENT_WEBHOOK_SECRET=replace_with_webhook_secret
#   FEATURE_SAAS_ADMIN_API=true
#   FEATURE_SAAS_ENTITLEMENTS=true
#   OPENAI_API_MODEL=deepseek-chat
#   DATABASE_URL=postgresql+psycopg2://antihub:***@db:5432/antihub

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
API_BASE_URL="${API_BASE_URL:-http://127.0.0.1:8000}"
ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"
PLAN_CODE="${PLAN_CODE:-monthly_198}"
PAYMENT_WEBHOOK_SECRET="${PAYMENT_WEBHOOK_SECRET:-}"
TEST_USER_PREFIX="${TEST_USER_PREFIX:-acceptance}"

if [[ -z "$ADMIN_PASSWORD" ]]; then
  echo "[acceptance] ADMIN_PASSWORD is required"
  exit 1
fi
if [[ -z "$PAYMENT_WEBHOOK_SECRET" ]]; then
  echo "[acceptance] PAYMENT_WEBHOOK_SECRET is required"
  exit 1
fi

need_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "[acceptance] missing required command: $1"
    exit 1
  fi
}

need_cmd docker
need_cmd curl
need_cmd python3
need_cmd openssl

timestamp="$(date +%s)"
TEST_USERNAME="${TEST_USERNAME:-${TEST_USER_PREFIX}_${timestamp}}"
TEST_PASSWORD="${TEST_PASSWORD:-Passw0rd_${timestamp}!}"
TEST_TENANT_CODE="${TEST_TENANT_CODE:-${TEST_USER_PREFIX}-${timestamp}}"

echo "[1/8] Deploy services"
docker compose -f "$COMPOSE_FILE" up -d --build

echo "[2/8] Run migration and init seed"
docker compose -f "$COMPOSE_FILE" exec -T api alembic upgrade head
docker compose -f "$COMPOSE_FILE" exec -T api python scripts/init_prod_db.py

echo "[3/8] Wait for API health"
for i in $(seq 1 60); do
  if curl -fsS "${API_BASE_URL}/health" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
curl -fsS "${API_BASE_URL}/health" >/dev/null

echo "[4/8] Validate billing health"
billing_health="$(curl -fsS "${API_BASE_URL}/health/billing")"
echo "$billing_health" | python3 - <<'PY'
import json, sys
data = json.load(sys.stdin)
print(json.dumps(data, ensure_ascii=False, indent=2))
status = str(data.get("status") or "").lower()
if status not in {"ok", "degraded"}:
    raise SystemExit(f"billing health not ready: status={status}")
PY

echo "[5/8] Admin login and SaaS admin check"
admin_login_payload="$(printf '{"username":"%s","password":"%s"}' "$ADMIN_USERNAME" "$ADMIN_PASSWORD")"
admin_login_json="$(curl -fsS -X POST "${API_BASE_URL}/auth/login" \
  -H "Content-Type: application/json" \
  -d "$admin_login_payload")"
ADMIN_TOKEN="$(echo "$admin_login_json" | python3 - <<'PY'
import json, sys
print(json.load(sys.stdin)["access_token"])
PY
)"
curl -fsS "${API_BASE_URL}/admin/saas/plans" \
  -H "Authorization: Bearer ${ADMIN_TOKEN}" \
  >/dev/null

echo "[6/8] Register acceptance user"
register_payload="$(printf '{"username":"%s","password":"%s","tenant_name":"%s workspace","tenant_code":"%s"}' \
  "$TEST_USERNAME" "$TEST_PASSWORD" "$TEST_USERNAME" "$TEST_TENANT_CODE")"
register_json="$(curl -fsS -X POST "${API_BASE_URL}/auth/register" \
  -H "Content-Type: application/json" \
  -d "$register_payload")"
USER_TOKEN="$(echo "$register_json" | python3 - <<'PY'
import json, sys
print(json.load(sys.stdin)["access_token"])
PY
)"

echo "[7/8] Create checkout order"
checkout_payload="$(printf '{"plan_code":"%s","idempotency_key":"acceptance-%s"}' "$PLAN_CODE" "$timestamp")"
checkout_json="$(curl -fsS -X POST "${API_BASE_URL}/billing/checkout" \
  -H "Authorization: Bearer ${USER_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "$checkout_payload")"
EXTERNAL_ORDER_ID="$(echo "$checkout_json" | python3 - <<'PY'
import json, sys
print(json.load(sys.stdin)["external_order_id"])
PY
)"
AMOUNT_CENTS="$(echo "$checkout_json" | python3 - <<'PY'
import json, sys
print(int(json.load(sys.stdin).get("checkout_payload", {}).get("amount_cents", 0) or 0))
PY
)"
if [[ "$AMOUNT_CENTS" -le 0 ]]; then
  # Fallback when provider payload doesn't include amount.
  AMOUNT_CENTS="$(curl -fsS "${API_BASE_URL}/billing/orders/me/${EXTERNAL_ORDER_ID}/status" \
    -H "Authorization: Bearer ${USER_TOKEN}" | python3 - <<'PY'
import json, sys
print(int(json.load(sys.stdin)["amount_cents"]))
PY
)"
fi
CURRENCY="$(curl -fsS "${API_BASE_URL}/billing/orders/me/${EXTERNAL_ORDER_ID}/status" \
  -H "Authorization: Bearer ${USER_TOKEN}" | python3 - <<'PY'
import json, sys
print(str(json.load(sys.stdin)["currency"]).lower())
PY
)"

echo "[8/8] Simulate payment webhook and verify subscription/points"
EVENT_ID="evt_acceptance_${timestamp}"
PAID_AT="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
WEBHOOK_PAYLOAD="$(python3 - <<PY
import json
print(json.dumps({
  "event_type": "payment.succeeded",
  "event_id": "${EVENT_ID}",
  "provider": "mock",
  "data": {
    "external_order_id": "${EXTERNAL_ORDER_ID}",
    "amount_cents": int("${AMOUNT_CENTS}"),
    "currency": "${CURRENCY}",
    "paid_at": "${PAID_AT}"
  }
}, ensure_ascii=False))
PY
)"
SIGNATURE="$(printf '%s' "$WEBHOOK_PAYLOAD" | openssl dgst -sha256 -hmac "$PAYMENT_WEBHOOK_SECRET" | awk '{print $2}')"

curl -fsS -X POST "${API_BASE_URL}/billing/webhooks/payment" \
  -H "Content-Type: application/json" \
  -H "X-Signature: ${SIGNATURE}" \
  -d "$WEBHOOK_PAYLOAD" \
  >/dev/null

order_status="$(curl -fsS "${API_BASE_URL}/billing/orders/me/${EXTERNAL_ORDER_ID}/status" \
  -H "Authorization: Bearer ${USER_TOKEN}")"
subscription_status="$(curl -fsS "${API_BASE_URL}/billing/subscription/me" \
  -H "Authorization: Bearer ${USER_TOKEN}")"
points_status="$(curl -fsS "${API_BASE_URL}/billing/points/me" \
  -H "Authorization: Bearer ${USER_TOKEN}")"

echo "$order_status" | python3 - <<'PY'
import json, sys
data = json.load(sys.stdin)
print("[order]", json.dumps(data, ensure_ascii=False))
if str(data.get("status", "")).lower() != "paid":
    raise SystemExit("order status is not paid")
PY

echo "$subscription_status" | python3 - <<'PY'
import json, sys
data = json.load(sys.stdin)
print("[subscription]", json.dumps(data, ensure_ascii=False))
if str(data.get("status", "")).lower() != "active":
    raise SystemExit("subscription is not active")
PY

echo "$points_status" | python3 - <<'PY'
import json, sys
data = json.load(sys.stdin)
print("[points]", json.dumps(data, ensure_ascii=False))
if int(data.get("balance", 0)) <= 0:
    raise SystemExit("points balance is not positive")
PY

echo
echo "[acceptance] PASS"
echo "user=${TEST_USERNAME}"
echo "order=${EXTERNAL_ORDER_ID}"
