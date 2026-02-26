# Round L1: Security Hardening — Execution Plan

**Date:** 2026-02-26
**Branch:** `claude/audit-codebase-qFhts`
**Topic:** Security (1 topic only)
**Change budget:** 5 code files + 2 deletions = within 3-8 file budget

---

## 1. Planned Changes

### Change 1: Gate `/docs`, `/redoc`, `/openapi.json` behind auth in production

**File:** `main.py`
**Lines:** ~501-511 (PUBLIC_AUTH_PATHS), ~544, ~549, ~641

**What:** Remove `/docs`, `/redoc`, `/openapi.json` from `PUBLIC_AUTH_PATHS`. Update `_is_public_path()` to only allow these paths when `APP_ENV` is NOT `prod`/`production`. Update the CSP exemption at line 641 similarly.

**Exact change:**
- Remove lines 509-511 (`"/docs"`, `"/redoc"`, `"/openapi.json"`) from `PUBLIC_AUTH_PATHS`
- In `_is_public_path()` (~line 544): remove `/openapi.json`, `/docs`, `/redoc` from the inline set; add a conditional block that allows them only when `APP_ENV` is not production
- In `_is_public_path()` (~line 549): wrap the `/docs/` and `/redoc/` prefix check behind the same env guard
- Line 641 (CSP exemption): no change needed — CSP exemption for docs pages is harmless since auth blocks access first

**Why:** Blocker #1 — full API schema exposed to the internet in production.

### Change 2: Delete `.bak` files from git

**Files to delete:**
- `main.py.bak_2026-02-25_113658` (190,741 bytes)
- `main.py.bak_2026-02-25_114837` (190,918 bytes)

**What:** `git rm` both files.

**Why:** Blocker #2 — 381KB of full source code tracked in repo.

### Change 3: Add `*.bak*` to `.gitignore`

**File:** `.gitignore`

**What:** Add a line `*.bak*` under the "Logs / temp" section.

**Why:** Prevent recurrence of `.bak` files being accidentally committed.

### Change 4: Remove `change_me` default for POSTGRES_PASSWORD

**File:** `docker-compose.prod.yml` (line 112)

**What:** Change `${POSTGRES_PASSWORD:-change_me}` to `${POSTGRES_PASSWORD:?POSTGRES_PASSWORD must be set in .env.prod}`.

Docker Compose's `${VAR:?message}` syntax causes compose to fail immediately with a clear error if the variable is unset or empty.

**Why:** Blocker #3 — trivial DB password if operator forgets to set `.env.prod`.

### Change 5: Add pre-migration backup step to `update_prod.sh`

**File:** `scripts/update_prod.sh` (between lines 77-79)

**What:** Insert a backup step after starting postgres/redis and before running migration:

```bash
log "pre-migration database backup"
docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" run --rm api \
  python scripts/backup_restore.py backup --output-dir /app/backups \
  || log "WARNING: pre-migration backup failed (continuing anyway)"
```

The backup is best-effort (uses `||` fallback) so a backup failure doesn't block deploys — but the operator sees the warning.

**Why:** Blocker #4 — migration runs without snapshot; bad migration = data loss.

---

## 2. Files Changed Summary

| # | File | Action | Lines Changed |
|---|------|--------|--------------|
| 1 | `main.py` | Edit PUBLIC_AUTH_PATHS + `_is_public_path()` | ~10 lines |
| 2 | `main.py.bak_2026-02-25_113658` | Delete | -190KB |
| 3 | `main.py.bak_2026-02-25_114837` | Delete | -190KB |
| 4 | `.gitignore` | Add 1 line | +1 line |
| 5 | `docker-compose.prod.yml` | Edit 1 line | 1 line |
| 6 | `scripts/update_prod.sh` | Add ~4 lines | +4 lines |

**Total code files edited:** 3 (main.py, docker-compose.prod.yml, update_prod.sh)
**Total files deleted:** 2 (.bak files)
**Total config files edited:** 1 (.gitignore)

---

## 3. Risks / Blast Radius

| Risk | Likelihood | Severity | Mitigation |
|------|-----------|----------|-----------|
| Removing docs from PUBLIC_AUTH_PATHS breaks dev-mode Swagger access | Medium | Low | Guard is `APP_ENV` aware — only blocks in prod/production. Dev mode unaffected. |
| Docker compose fails on startup if POSTGRES_PASSWORD unset | Intended | N/A | This IS the desired behavior — fail-fast. Operator reads error, sets the variable. |
| Backup step adds ~10-30s to deploy time | Low | Negligible | Acceptable tradeoff for data safety. |
| Backup step fails (pg_dump not available in api container) | Medium | None | Step is best-effort with `||` fallback — deploy continues with warning. |

**Blast radius:** Minimal. No business logic, no billing, no DB schema, no frontend changes. All changes are subtractive (removing attack surface) or additive guards (backup, fail-fast).

---

## 4. Validation Evidence Plan

After patching, I will run and report output for each:

| # | Command / Check | Expected Result |
|---|----------------|----------------|
| V-1 | `git ls-files '*.bak*'` | Returns empty (no .bak files tracked) |
| V-2 | `grep 'change_me' docker-compose.prod.yml` | Returns empty (no trivial default) |
| V-3 | `grep '"/docs"' main.py` in PUBLIC_AUTH_PATHS context | Not found in PUBLIC_AUTH_PATHS |
| V-4 | `grep 'backup' scripts/update_prod.sh` | Shows backup step before migration |
| V-5 | `grep '\.bak' .gitignore` | Shows `*.bak*` pattern |
| V-6 | `pytest` (existing tests) | All pass — no regressions |
| V-7 | Manual review: `_is_public_path("/docs")` logic | Returns True in dev, False in prod |

---

## 5. Rollback Steps

All changes are independent. Rollback is a single command:

```bash
git revert <commit-sha>
```

Individual changes can also be reverted by editing specific files back. No database migration, no schema change, no state mutation.

---

## 6. Out-of-Scope Findings (NOT patching in this round)

- CORS_ORIGINS localhost default → needs D-7 (domain) for full fix; will do in L4
- Rate limiter RuntimeError on Redis failure → Round L6
- StripeProvider NotImplementedError → Round L4
- Points labeling → Round L2
- Legal pages → Round L3
- Frontend UX polish → Round L5

---

*Awaiting owner confirmation to proceed with patching.*
