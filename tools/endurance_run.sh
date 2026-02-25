#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Optional: load local .env (gitignored). This repo already uses "source .env" elsewhere.
if [[ -f ".env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source ".env"
  set +a
fi

LOG_DIR="${LOG_DIR:-$ROOT/.antihub}"
mkdir -p "$LOG_DIR"

LOG_FILE="${LOG_FILE:-$LOG_DIR/endurance_billing.log}"
HEALTH_URL="${BILLING_HEALTH_URL:-http://127.0.0.1:8010/health/billing}"
WEBHOOK_URL="${PAYMENT_WEBHOOK_URL:-http://127.0.0.1:8010/billing/webhooks/payment}"

# Keep LLM runs short and frequent by default; override via env.
COUNT_LLM="${COUNT_LLM:-40}"
CONC_LLM="${CONC_LLM:-40}"
COUNT_DET="${COUNT_DET:-2000}"
CONC_DET="${CONC_DET:-200}"

SUITE_ATTACK_COUNT="${SUITE_ATTACK_COUNT:-500}"
SUITE_REPLAY_CONCURRENCY="${SUITE_REPLAY_CONCURRENCY:-50}"

SLEEP_SECONDS="${SLEEP_SECONDS:-2}"

echo "[endurance] start ts=$(date -Is) root=$ROOT" | tee -a "$LOG_FILE"
echo "[endurance] health_url=$HEALTH_URL webhook_url=$WEBHOOK_URL" | tee -a "$LOG_FILE"

iter=0
while true; do
  iter=$((iter + 1))
  echo "" | tee -a "$LOG_FILE"
  echo "[endurance] iter=$iter ts=$(date -Is)" | tee -a "$LOG_FILE"

  # Health check (fast fail if backend is down).
  if ! .venv/bin/python - <<PY >/dev/null 2>&1
import httpx
r=httpx.get("$HEALTH_URL",timeout=5.0,trust_env=False)
raise SystemExit(0 if r.status_code==200 else 1)
PY
  then
    echo "[endurance] health_check_failed url=$HEALTH_URL" | tee -a "$LOG_FILE"
    exit 2
  fi

  # 1) LLM-driven chaos (burn quota).
  llm_out="$LOG_DIR/last_llm_chaos.txt"
  set +e
  {
    echo "[endurance] llm_chaos count=$COUNT_LLM concurrency=$CONC_LLM"
    # Default to a single batch to avoid multi-batch stalls when upstream is slow/down.
    CHAOS_LLM_BATCH="${CHAOS_LLM_BATCH:-$COUNT_LLM}" \
      CHAOS_LLM_MAX_TOKENS="${CHAOS_LLM_MAX_TOKENS:-3000}" \
      PYTHONUNBUFFERED=1 \
      .venv/bin/python tools/chaos_payment_test.py \
        --webhook-url "$WEBHOOK_URL" \
        --count "$COUNT_LLM" \
        --concurrency "$CONC_LLM" \
        --timeout 20 \
        --llm-timeout 60
  } >"$llm_out" 2>&1
  llm_status=$?
  set -e
  cat "$llm_out" | tee -a "$LOG_FILE"
  if [[ $llm_status -ne 0 ]]; then
    echo "[endurance] llm_chaos_exit=$llm_status: see $llm_out" | tee -a "$LOG_FILE"
    exit 3
  fi
  if grep -Eq "5xx server errors:\\s*[1-9]" "$llm_out" || grep -Eq "Request errors:\\s*[1-9]" "$llm_out"; then
    echo "[endurance] llm_chaos_failed: see $llm_out" | tee -a "$LOG_FILE"
    exit 3
  fi

  # 2) Deterministic flood (resource exhaustion without LLM).
  det_out="$LOG_DIR/last_det_chaos.txt"
  set +e
  {
    echo "[endurance] det_chaos count=$COUNT_DET concurrency=$CONC_DET (MINIMAX_API_KEY disabled)"
    MINIMAX_API_KEY= \
      PYTHONUNBUFFERED=1 \
      .venv/bin/python tools/chaos_payment_test.py \
        --webhook-url "$WEBHOOK_URL" \
        --count "$COUNT_DET" \
        --concurrency "$CONC_DET" \
        --timeout 30
  } >"$det_out" 2>&1
  det_status=$?
  set -e
  cat "$det_out" | tee -a "$LOG_FILE"
  if [[ $det_status -ne 0 ]]; then
    echo "[endurance] det_chaos_exit=$det_status: see $det_out" | tee -a "$LOG_FILE"
    exit 4
  fi
  if grep -Eq "5xx server errors:\\s*[1-9]" "$det_out" || grep -Eq "Request errors:\\s*[1-9]" "$det_out"; then
    echo "[endurance] det_chaos_failed: see $det_out" | tee -a "$LOG_FILE"
    exit 4
  fi

  # 3) Resilience suite (replay + integrity).
  suite_out="$LOG_DIR/last_suite.txt"
  set +e
  {
    echo "[endurance] suite attack_count=$SUITE_ATTACK_COUNT replay_concurrency=$SUITE_REPLAY_CONCURRENCY"
    PYTHONUNBUFFERED=1 \
      .venv/bin/python tools/chaos_suite.py \
        --webhook-url "$WEBHOOK_URL" \
        --health-url "$HEALTH_URL" \
        --attack-count "$SUITE_ATTACK_COUNT" \
        --replay-concurrency "$SUITE_REPLAY_CONCURRENCY" \
        --http-timeout 30
  } >"$suite_out" 2>&1
  suite_status=$?
  set -e
  cat "$suite_out" | tee -a "$LOG_FILE"
  if [[ $suite_status -ne 0 ]]; then
    echo "[endurance] suite_exit=$suite_status: see $suite_out" | tee -a "$LOG_FILE"
    exit 5
  fi
  if grep -Eq "5xx=[1-9]" "$suite_out" || grep -Eq "err=[1-9]" "$suite_out"; then
    echo "[endurance] suite_failed: see $suite_out" | tee -a "$LOG_FILE"
    exit 5
  fi

  sleep "$SLEEP_SECONDS"
done
