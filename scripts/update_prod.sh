#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
ENV_FILE="${ENV_FILE:-.env.prod}"
DEPLOY_BRANCH="${DEPLOY_BRANCH:-}"
MIGRATION_CMD="${MIGRATION_CMD:-python scripts/init_prod_db.py}"
SKIP_GIT_PULL="${SKIP_GIT_PULL:-0}"
SOURCE_SYNC_MODE="${SOURCE_SYNC_MODE:-auto}" # auto|git|sftp
BUILD_SERVICES="${BUILD_SERVICES:-api frontend}"

log() {
  printf '[update_prod] %s\n' "$*"
}

die() {
  printf '[update_prod] ERROR: %s\n' "$*" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "missing required command: $1"
}

require_cmd docker

docker compose version >/dev/null 2>&1 || die "docker compose plugin is required"

cd "$PROJECT_ROOT"

[ -f "$COMPOSE_FILE" ] || die "compose file not found: $COMPOSE_FILE"
[ -f "$ENV_FILE" ] || die "env file not found: $ENV_FILE (copy from .env.prod.example first)"

if [ "$SKIP_GIT_PULL" = "1" ] && [ "$SOURCE_SYNC_MODE" = "auto" ]; then
  SOURCE_SYNC_MODE="sftp"
fi

if [ "$SOURCE_SYNC_MODE" != "auto" ] && [ "$SOURCE_SYNC_MODE" != "git" ] && [ "$SOURCE_SYNC_MODE" != "sftp" ]; then
  die "invalid SOURCE_SYNC_MODE='$SOURCE_SYNC_MODE' (expected: auto|git|sftp)"
fi

sync_mode="$SOURCE_SYNC_MODE"
if [ "$sync_mode" = "auto" ]; then
  if command -v git >/dev/null 2>&1 && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    if git remote get-url origin >/dev/null 2>&1; then
      sync_mode="git"
    else
      sync_mode="sftp"
    fi
  else
    sync_mode="sftp"
  fi
fi

if [ "$sync_mode" = "git" ]; then
  require_cmd git
  git rev-parse --is-inside-work-tree >/dev/null 2>&1 || die "SOURCE_SYNC_MODE=git but current directory is not a git repository"
  git remote get-url origin >/dev/null 2>&1 || die "SOURCE_SYNC_MODE=git but git remote 'origin' is missing"
  log "sync source code from git"
  git fetch --all --prune

  current_branch="$(git rev-parse --abbrev-ref HEAD)"
  target_branch="${DEPLOY_BRANCH:-$current_branch}"
  if [ "$target_branch" != "$current_branch" ]; then
    die "current branch is '$current_branch', but DEPLOY_BRANCH='$target_branch'. switch branch before running update_prod.sh"
  fi
  git pull --ff-only origin "$target_branch"
else
  log "skip git pull (sync mode: sftp/manual upload)"
fi

log "build production images"
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" build --pull $BUILD_SERVICES

log "start postgres and redis"
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d db redis

log "run database migration/init"
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" run --rm api sh -lc "$MIGRATION_CMD"

log "restart application services"
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d api frontend tunnel

log "service status"
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" ps

log "done"
