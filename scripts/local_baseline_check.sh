#!/usr/bin/env bash
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PY="$ROOT_DIR/.venv/bin/python"
ENV_LOCAL="$ROOT_DIR/.env.local"
ENV_LOCAL_EXAMPLE="$ROOT_DIR/.env.local.example"
FRONTEND_NODE_MODULES="$ROOT_DIR/frontend/node_modules"

OK_COUNT=0
WARN_COUNT=0
FAIL_COUNT=0

ok() {
  local message="$1"
  echo "[OK] $message"
  OK_COUNT=$((OK_COUNT + 1))
}

warn() {
  local message="$1"
  local fix_cmd="$2"
  echo "[WARN] $message"
  echo "       Fix: $fix_cmd"
  WARN_COUNT=$((WARN_COUNT + 1))
}

fail() {
  local message="$1"
  local fix_cmd="$2"
  echo "[FAIL] $message"
  echo "       Fix: $fix_cmd"
  FAIL_COUNT=$((FAIL_COUNT + 1))
}

check_required_command() {
  local cmd="$1"
  local fix_cmd="$2"
  if command -v "$cmd" >/dev/null 2>&1; then
    ok "required command '$cmd' available"
  else
    fail "required command '$cmd' missing" "$fix_cmd"
  fi
}

default_env_value_for_key() {
  local key="$1"
  case "$key" in
    AUTH_ENABLED) echo "true" ;;
    AUTH_TOKEN_SECRET) echo "CHANGE_ME_LOCAL_ONLY" ;;
    ROOT_ADMIN_USERNAME) echo "root" ;;
    ROOT_ADMIN_PASSWORD) echo "CHANGE_ME_LOCAL_ONLY" ;;
    STARTUP_BOOTSTRAP_ENABLED) echo "true" ;;
    REDIS_DISABLED) echo "true" ;;
    CASE_STORE_BACKEND) echo "database" ;;
    PAYMENT_PROVIDER) echo "mock" ;;
    PAYMENT_WEBHOOK_SECRET) echo "CHANGE_ME_LOCAL_ONLY" ;;
    *) echo "CHANGE_ME" ;;
  esac
}

echo "[baseline] AntiHub local baseline check (WSL/dev)"
echo "[baseline] repo: $ROOT_DIR"

# Required checks (FAIL -> exit 1)
check_required_command "python3" "sudo apt-get update && sudo apt-get install -y python3 python3-venv"

if [[ -x "$VENV_PY" ]]; then
  ok "required virtualenv python available: .venv/bin/python"
else
  fail "required virtualenv python missing: .venv/bin/python" \
    "python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt -r requirements-dev.txt"
fi

if [[ -x "$VENV_PY" ]] && "$VENV_PY" -m pytest --version >/dev/null 2>&1; then
  ok "required test dependency available: pytest in .venv"
else
  fail "required test dependency missing: pytest in .venv" \
    ".venv/bin/python -m pip install -r requirements-dev.txt"
fi

check_required_command "node" "sudo apt-get update && sudo apt-get install -y nodejs npm"
check_required_command "npm" "sudo apt-get update && sudo apt-get install -y nodejs npm"

# Optional checks (WARN only)
if docker compose version >/dev/null 2>&1; then
  ok "optional command available: docker compose"
else
  warn "optional command unavailable: docker compose (likely WSL Docker integration not enabled)" \
    "sed -n '1,220p' docs/operations/deployment.md && docker compose version"
fi

if [[ -d "$FRONTEND_NODE_MODULES" ]]; then
  ok "optional frontend dependencies present: frontend/node_modules"
else
  warn "optional frontend dependencies missing: frontend/node_modules" \
    "npm --prefix frontend ci"
fi

if [[ -f "$ENV_LOCAL" ]]; then
  ok "optional local env file found: .env.local"
else
  warn "optional local env file missing: .env.local" \
    "cp .env.local.example .env.local"
fi

if [[ -f "$ENV_LOCAL_EXAMPLE" ]]; then
  ok "local env template found: .env.local.example"
else
  warn "local env template missing: .env.local.example" \
    "git checkout -- .env.local.example"
fi

if [[ -f "$ENV_LOCAL" ]]; then
  REQUIRED_ENV_KEYS=(
    AUTH_ENABLED
    AUTH_TOKEN_SECRET
    ROOT_ADMIN_USERNAME
    ROOT_ADMIN_PASSWORD
    STARTUP_BOOTSTRAP_ENABLED
    REDIS_DISABLED
    CASE_STORE_BACKEND
    PAYMENT_PROVIDER
    PAYMENT_WEBHOOK_SECRET
  )
  for key in "${REQUIRED_ENV_KEYS[@]}"; do
    if grep -Eq "^[[:space:]]*${key}=" "$ENV_LOCAL"; then
      ok "optional env key present: $key"
    else
      default_value="$(default_env_value_for_key "$key")"
      warn "optional env key missing in .env.local: $key" \
        "printf '%s\n' '${key}=${default_value}' >> .env.local"
    fi
  done
fi

echo
echo "[summary] OK=$OK_COUNT WARN=$WARN_COUNT FAIL=$FAIL_COUNT"
if [[ "$FAIL_COUNT" -gt 0 ]]; then
  echo "[summary] baseline check failed (required items missing)"
  exit 1
fi
echo "[summary] baseline check passed (only OK/WARN)"
exit 0
