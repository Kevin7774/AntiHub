#!/usr/bin/env bash
set -euo pipefail

ACTION="${1:-up}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$ROOT/.devlogs"
REDIS_NAME="antihub-redis"
REDIS_PORT="${REDIS_PORT:-6379}"
OPENCLAW_HOST="${OPENCLAW_HOST:-127.0.0.1}"
OPENCLAW_PORT="${OPENCLAW_PORT:-8787}"
VENV_DIR="${VENV_DIR:-}"

mkdir -p "$LOG_DIR"

pick_venv() {
  if [ -n "$VENV_DIR" ]; then
    echo "$VENV_DIR"
    return
  fi
  if [ -d "$ROOT/.venv-local" ]; then
    echo "$ROOT/.venv-local"
    return
  fi
  echo "$ROOT/.venv"
}

start_redis() {
  if docker ps --filter "name=^${REDIS_NAME}$" -q | grep -q .; then
    echo "[services] redis already running"
    return
  fi
  echo "[services] starting redis on ${REDIS_PORT}"
  docker run -d --rm --name "$REDIS_NAME" -p "${REDIS_PORT}:6379" redis:7 >/dev/null
}

start_openclaw() {
  local venv
  venv="$(pick_venv)"
  local python_bin="$venv/bin/python"
  if [ ! -x "$python_bin" ]; then
    echo "[services] missing python venv at $venv"
    echo "[services] create one with: python3 -m venv $venv"
    exit 1
  fi

  if [ -f "$LOG_DIR/openclaw.pid" ] && kill -0 "$(cat "$LOG_DIR/openclaw.pid")" >/dev/null 2>&1; then
    echo "[services] openclaw already running"
    return
  fi

  echo "[services] starting openclaw on ${OPENCLAW_HOST}:${OPENCLAW_PORT}"
  OPENCLAW_HOST="$OPENCLAW_HOST" OPENCLAW_PORT="$OPENCLAW_PORT" \
    nohup "$python_bin" -m openclaw.server >"$LOG_DIR/openclaw.log" 2>&1 &
  echo $! >"$LOG_DIR/openclaw.pid"
}

stop_openclaw() {
  if [ -f "$LOG_DIR/openclaw.pid" ]; then
    local pid
    pid="$(cat "$LOG_DIR/openclaw.pid")"
    if kill -0 "$pid" >/dev/null 2>&1; then
      echo "[services] stopping openclaw ($pid)"
      kill "$pid" >/dev/null 2>&1 || true
    fi
    rm -f "$LOG_DIR/openclaw.pid"
  fi
}

stop_redis() {
  if docker ps --filter "name=^${REDIS_NAME}$" -q | grep -q .; then
    echo "[services] stopping redis"
    docker stop "$REDIS_NAME" >/dev/null
  fi
}

case "$ACTION" in
  up)
    start_redis
    start_openclaw
    ;;
  down)
    stop_openclaw
    stop_redis
    ;;
  *)
    echo "Usage: $0 [up|down]"
    exit 1
    ;;
esac
