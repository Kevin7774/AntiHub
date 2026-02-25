#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR=$(cd "$(dirname "$0")/.." && pwd)
REPO_NAME="antihub-fixtures"

resolve_owner() {
  if [[ -n "${TEMPLATES_FIXTURES_REPO:-}" ]]; then
    echo "${TEMPLATES_FIXTURES_REPO}" | awk -F/ '{print $1}'
    return
  fi
  if [[ -n "${GITHUB_REPOSITORY:-}" ]]; then
    echo "${GITHUB_REPOSITORY}" | awk -F/ '{print $1}'
    return
  fi
  if git -C "$ROOT_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    origin=$(git -C "$ROOT_DIR" remote get-url origin 2>/dev/null || true)
    if [[ "$origin" == *"github.com"* ]]; then
      owner=$(echo "$origin" | sed -E 's#.*github.com[:/]+([^/]+)/.*#\1#')
      if [[ -n "$owner" ]]; then
        echo "$owner"
        return
      fi
    fi
  fi
}

OWNER=$(resolve_owner || true)
if [[ -z "$OWNER" ]]; then
  echo "Unable to infer GitHub owner. Set TEMPLATES_FIXTURES_REPO=owner/${REPO_NAME} first." >&2
  exit 1
fi

REMOTE_URL="https://github.com/${OWNER}/${REPO_NAME}.git"

if command -v gh >/dev/null 2>&1; then
  if ! gh repo view "${OWNER}/${REPO_NAME}" >/dev/null 2>&1; then
    gh repo create "${OWNER}/${REPO_NAME}" --public --confirm
  fi
else
  echo "gh CLI not found. Ensure ${OWNER}/${REPO_NAME} exists before pushing." >&2
fi

TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

mkdir -p "$TMP_DIR"
cp -R "$ROOT_DIR/test-repos" "$TMP_DIR/test-repos"

cd "$TMP_DIR"
git init
mkdir -p .github
cat <<'README' > README.md
# AntiHub Fixtures

This repository hosts fixtures for AntiHub templates. Contents live under `test-repos/`.
README

git add .
git commit -m "chore: publish fixtures"

git branch -M main
git remote add origin "$REMOTE_URL"
git push -u origin main --force

echo "Fixtures published to ${REMOTE_URL}"
