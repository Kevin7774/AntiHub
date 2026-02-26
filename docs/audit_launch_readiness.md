# AntiHub Launch Readiness Audit Report

**Date:** 2026-02-26
**Round:** Launch Readiness Audit (Go-Live Audit)
**Auditor:** Claude (Senior Full-Stack Engineer)
**Branch:** `claude/audit-codebase-qFhts`
**Scope:** Can AntiHub go live for paid users now?

---

## 1. Executive Summary

### Verdict: **CONDITIONAL-GO**

The codebase has a solid engineering foundation for a SaaS product — billing models, idempotent webhooks, audit logs, rate limiting, RBAC, multi-tenant scaffolding, and a working deployment pipeline are all present. However, **real payment collection is NOT safe today** due to several P0 gaps.

### Top 5 Blockers (P0)

| # | Blocker | Category |
|---|---------|----------|
| 1 | **Stripe provider is a stub** (`NotImplementedError`); WechatPay needs live credential validation | code/product |
| 2 | **No CI/CD pipeline** — zero `.github/workflows/`, no automated test gate before deploy | deployment/ops |
| 3 | **`.bak` files tracked in git** contain 190KB+ of production source code | security |
| 4 | **No legal/compliance pages** — no Terms of Service, Privacy Policy, Refund Policy in frontend or backend | legal/compliance |
| 5 | **CORS_ORIGINS defaults to localhost** in config; `.env.prod.example` has placeholder domain | deployment/ops |

---

## 2. Launch Gap Matrix

### P0 — Must fix before first paying user

| # | Issue | Category | Impact | Evidence | Fix Round |
|---|-------|----------|--------|----------|-----------|
| P0-1 | **StripeProvider raises `NotImplementedError`** | code/product | Checkout will crash if PAYMENT_PROVIDER=stripe | `billing/provider.py:111` | Round 1 |
| P0-2 | **No CI/CD pipeline** — no GitHub Actions, no test gate | deployment/ops | Regressions can ship to prod undetected | `.github/workflows/` missing entirely | Round 1 |
| P0-3 | **`.bak` files tracked in git** (`main.py.bak_*`) — 190KB each | security | Full source code leaked in repo history | `git ls-files \| grep .bak` → 2 files | Round 1 |
| P0-4 | **No Terms of Service / Privacy Policy / Refund Policy** | legal/compliance | Cannot legally collect payments in China or internationally | `grep -i privacy frontend/src/App.tsx` → 0 results | Owner action |
| P0-5 | **CORS_ORIGINS defaults to `localhost:5173`** | deployment/ops | Production will reject browser requests if not overridden in .env.prod | `config.py:299-308` | Round 1 |
| P0-6 | **WechatPay live credential chain untested** | code/product | Real payment flow may fail at signature/cert stage | `billing/wechatpay.py`, `billing/provider.py:114-186` — only mock tested | Round 2 |
| P0-7 | **`/docs` and `/redoc` (Swagger UI) publicly accessible** | security | Full API schema exposed to internet in production | `main.py:509-511` — in PUBLIC_AUTH_PATHS | Round 1 |
| P0-8 | **No HTTPS enforcement at app level** — relies entirely on Cloudflare tunnel | security | If tunnel misconfigured, traffic is plaintext | `nginx.prod.conf` listens on port 80 only | Round 2 |
| P0-9 | **DB password in docker-compose defaults to `change_me`** | security | If operator forgets to set .env.prod, DB has trivial password | `docker-compose.prod.yml:112` | Round 1 |
| P0-10 | **No automated DB backup before deploy** | deployment/ops | `update_prod.sh` runs migrations without backup step | `scripts/update_prod.sh:79-80` | Round 1 |

### P1 — Should fix before launch week

| # | Issue | Category | Impact | Evidence | Fix Round |
|---|-------|----------|--------|----------|-----------|
| P1-1 | **App.tsx is 266KB single file** | UX/conversion | Extremely fragile; single bug breaks entire app | `frontend/src/App.tsx` — 266,565 bytes | Round 3+ |
| P1-2 | **styles.css is 78KB single file** | UX/conversion | Unmaintainable, performance risk on mobile | `frontend/src/styles.css` — 77,961 bytes | Round 3+ |
| P1-3 | **main.py is 225KB monolith** (~5800+ lines) | code/product | High merge conflict risk, hard to review changes | `main.py` — 225,707 bytes | Round 3+ |
| P1-4 | **No email/notification on payment success** | UX/conversion | User has no receipt, no confirmation outside the app | No email module found in codebase | Round 3 |
| P1-5 | **No subscription expiry notification** | UX/conversion | Users get silently downgraded when subscription expires | `billing/repository.py` — `expire_due_subscriptions()` has no notification | Round 3 |
| P1-6 | **No auto-renewal mechanism** | code/product | `Subscription.auto_renew` field exists but is always `False` | `billing/models.py:197` | Round 3 |
| P1-7 | **Rate limiter raises RuntimeError in production if Redis is down** | code/product | API becomes 500 for all authenticated requests if Redis fails | `billing/middleware.py:157,192,202` | Round 2 |
| P1-8 | **No Prometheus metrics exporter** | deployment/ops | Alert rules in `monitoring/prometheus-alert-rules.yml` have no data source | No `/metrics` Prometheus endpoint found | Round 2 |
| P1-9 | **Pending order cleanup is manual only** (`close_timed_out_orders`) | code/product | Stale orders accumulate; no scheduled job/cron | `billing/service.py:338-368` — no Celery beat task | Round 2 |
| P1-10 | **No contact/support page or help channel** | UX/conversion | Paying users have no way to get help | Grep for "support" / "contact" in frontend → 0 results | Owner action |
| P1-11 | **`/billing/dev/simulate-payment` exists in prod binary** | security | Endpoint returns 404 in prod env but is still in the codebase/routing table | `main.py:3695` — guarded by APP_ENV check | Round 2 |
| P1-12 | **No user password change/reset flow** | UX/conversion | Users cannot change or recover passwords | No `/auth/change-password` or `/auth/reset` endpoint | Round 3 |
| P1-13 | **AUTH_TOKEN_TTL is 12 hours (43200s)** | security | Long-lived JWTs — no refresh token mechanism | `config.py:198` | Round 3 |

### P2 — Nice to have before scale

| # | Issue | Category | Impact | Evidence | Fix Round |
|---|-------|----------|--------|----------|-----------|
| P2-1 | **Duplicate frontend source** — `/src/` mirrors `/frontend/src/` | code/product | Confusing for maintainers | `ls -la src/ frontend/src/` — identical files | Round 4 |
| P2-2 | **No load testing evidence against production config** | deployment/ops | Unknown breaking point for concurrent users | `tools/load_test_k6.js` exists but no results | Round 4 |
| P2-3 | **No log rotation / retention policy** in docker compose | deployment/ops | Logs grow unbounded on disk | No `logging:` section in `docker-compose.prod.yml` | Round 4 |
| P2-4 | **No graceful shutdown for Celery worker** | deployment/ops | In-flight tasks may be killed on deploy | `docker-compose.prod.yml:70` — no `--without-mingle` or drain | Round 4 |
| P2-5 | **Multi-tenant features are behind flags (default OFF)** — good, but untested with real tenant isolation | code/product | When turned ON, cross-tenant data leaks possible | `config.py:172-176` — flags exist | Round 5 |
| P2-6 | **No CSP / security headers** in nginx config | security | XSS risk if user content is rendered | `nginx.prod.conf` / `frontend/nginx.prod.conf` — no `Content-Security-Policy` | Round 4 |
| P2-7 | **No database connection pool sizing** for production | deployment/ops | SQLAlchemy defaults may be too small for Gunicorn workers×4 | `billing/db.py:35-42` — no `pool_size` / `max_overflow` | Round 3 |
| P2-8 | **No admin dashboard for billing ops** (admin APIs exist but no UI) | UX/conversion | Operator must use curl/Postman for billing management | Admin endpoints exist but no admin frontend | Round 5 |

---

## 3. Missing Inputs I Need From You

| # | Item | Why Needed | Urgency |
|---|------|-----------|---------|
| **A** | **Payment provider decision** — WechatPay only? Stripe? Both? | Determines which provider(s) to wire up and test | P0 |
| **B** | **WechatPay merchant credentials** (MCHID, APPID, cert serial, private key, platform cert, APIv3 key) | Cannot test real payment flow without them | P0 |
| **C** | **Domain / DNS / SSL setup details** — which domain? Cloudflare tunnel token? | Needed to set CORS_ORIGINS, VITE_API_BASE_URL, WECHATPAY_NOTIFY_URL | P0 |
| **D** | **Terms of Service, Privacy Policy, Refund Policy** content (Chinese + English?) | Legal requirement for payment collection | P0 |
| **E** | **Pricing decisions** — are the 3 seed plans (¥198/¥398/¥1980) final? | Affects seed data and frontend pricing display | P0 |
| **F** | **Support/contact workflow** — WeChat group? Email? Ticket system? | Users need a way to get help | P1 |
| **G** | **Analytics/tracking preferences** — GA? Baidu? Mixpanel? None? | Affects frontend bundle and privacy policy | P1 |
| **H** | **ICP Filing status** (if hosting in China) | Legal requirement for serving websites in China | P0 (if China-hosted) |
| **I** | **Production server specs** — CPU/RAM/disk for the host machine | Affects Gunicorn workers, Celery concurrency, pool sizing | P1 |
| **J** | **Admin email / notification channel** for alerts | Prometheus rules exist but no notification target | P1 |
| **K** | **Desired APP_ENV value for production** — `prod` or `production`? | Code checks both; need consistency | P1 |

---

## 4. Pre-Launch Checklist (must-have before first paying user)

- [ ] **Remove `.bak` files from git** (`main.py.bak_*`)
- [ ] **Disable `/docs` and `/redoc` in production** (remove from PUBLIC_AUTH_PATHS or gate behind auth)
- [ ] **Set strong defaults or fail-fast** for DB password in docker-compose (remove `change_me` default)
- [ ] **Set CORS_ORIGINS** in `.env.prod` to actual production domain
- [ ] **Wire up at least ONE real payment provider** (WechatPay or Stripe) and test end-to-end
- [ ] **Add Terms of Service, Privacy Policy, Refund Policy** pages to frontend
- [ ] **Add automated DB backup** to `update_prod.sh` before migration step
- [ ] **Create minimal CI pipeline** (pytest + lint on push/PR)
- [ ] **Test full paid user journey** on staging: register → checkout → pay → subscription active → points granted → use feature → subscription expires
- [ ] **Set AUTH_TOKEN_SECRET** to a cryptographically random 32+ byte hex string
- [ ] **Set PAYMENT_WEBHOOK_SECRET** to a cryptographically random 32+ byte hex string
- [ ] **Verify health endpoints** return meaningful data under production compose
- [ ] **Run `scripts/deploy_acceptance_mock.sh`** successfully on staging

---

## 5. Post-Launch Checklist

### First 24 Hours
- [ ] Monitor `/health` and `/health/billing` — set up uptime check (e.g., UptimeRobot, Cloudflare health check)
- [ ] Watch structured logs for `billing.webhook.received` and `billing.webhook.processed` events
- [ ] Check `billing_audit_logs` table for any `outcome != 'processed'` rows
- [ ] Verify first real payment completes full cycle (order → subscription → points)
- [ ] Ensure Redis is stable (rate limiter and entitlements cache depend on it)
- [ ] Run `python scripts/backup_restore.py backup` — verify backup works

### First Week
- [ ] Review `billing_audit_logs` for any signature validation failures
- [ ] Check for stale pending orders — run or automate `close_timed_out_orders`
- [ ] Monitor disk space (Docker images, postgres data, redis AOF, logs)
- [ ] Check Gunicorn worker memory usage (4 workers × N requests)
- [ ] Review error rates in structured logs
- [ ] Collect user feedback on payment UX (QR code scanning flow)
- [ ] Test subscription expiry behavior (advance time or wait for first natural expiry)

---

## 6. Suggested Next 3 Implementation Rounds

### Round 1: Security Hardening & Deploy Safety (1 topic: security)
**Scope:** 5-6 files
1. Remove `.bak` files from git tracking
2. Gate `/docs`, `/redoc`, `/openapi.json` behind admin auth in production
3. Remove `change_me` defaults from docker-compose.prod.yml (fail if not set)
4. Add `pre-deploy backup` step to `update_prod.sh`
5. Create minimal GitHub Actions CI (pytest + basic lint)
6. Add CORS production validation (warn if still localhost)

### Round 2: Payment Provider Wiring (1 topic: billing)
**Scope:** 3-5 files
1. Wire up WechatPay provider with real credentials (or Stripe if that's the choice)
2. Add startup validation — check all required payment env vars on boot
3. Add Celery beat task for `close_timed_out_orders` (scheduled every 10 min)
4. Add connection pool sizing to `billing/db.py` for production
5. Test full webhook flow with real provider sandbox

### Round 3: Paid User Experience (1 topic: UX)
**Scope:** 3-5 files
1. Add legal/policy pages (Terms, Privacy, Refund) — even if minimal
2. Add contact/support section to frontend
3. Add password change endpoint (`/auth/change-password`)
4. Add subscription expiry warning (in /billing/subscription/me response or new endpoint)
5. Consider email notification for payment success (if email infra available)

---

## 7. Special Focus Answers

### If I turn on real payment collection now, what are the top failure risks?

1. **StripeProvider will crash** — it raises `NotImplementedError`. If someone sets `PAYMENT_PROVIDER=stripe`, checkout is broken.
2. **WechatPay credentials are unvalidated** — the code looks correct for WeChat Native Pay (scan-to-pay), but without testing against real sandbox credentials, signature or cert errors will silently fail webhooks.
3. **No receipt/confirmation** — user pays but gets no email/SMS confirmation. If the frontend tab closes before polling detects payment, user may think they lost money.
4. **Pending orders never auto-expire** — if a user initiates checkout but never pays, the order stays in `pending` forever. WeChat Pay's native pay QR code expires in 2 hours, but the AntiHub order doesn't.
5. **Rate limiter hard-fails in production** — if Redis goes down for even a second, `BillingRateLimiter` raises `RuntimeError`, turning the entire API into 500s.

### What would break user trust first?

1. **Taking money without giving access** — if the webhook fails silently (cert error, Redis down, etc.), the user has paid but their subscription is not activated. This is the #1 trust killer.
2. **No refund policy visible** — Chinese consumers expect clear refund terms. Without them, users may report the merchant to WeChat Pay.
3. **No way to contact support** — if anything goes wrong with payment, the user has no recourse.
4. **Exposing full API documentation** — `/docs` is publicly accessible; a security researcher could find this and report it publicly.

### What is missing for "can actually start charging users" vs "just demo works"?

| Demo Works (Current State) | Actually Charging Users (Needed) |
|---|---|
| Mock provider returns fake QR URL | Real WeChat/Stripe provider returns real payment URL |
| Dev-simulate endpoint processes fake payment | Real webhook from payment provider triggers processing |
| No legal pages needed for demo | Terms of Service + Privacy Policy + Refund Policy required |
| Swagger UI open for debugging | Swagger UI hidden in production |
| Default passwords in compose for easy setup | Strong passwords required, no defaults |
| No backup needed for demo data | Pre-deploy backup is mandatory |
| Single developer deploying | CI/CD gate prevents broken deploys |
| CORS allows localhost | CORS locked to production domain only |

---

## 8. Positive Findings (What's Already Good)

Credit where due — the codebase is notably well-structured for a pre-launch product:

- **Billing audit logs** — every webhook attempt (including failures) is recorded with raw payload. Excellent for disputes.
- **Idempotent webhook processing** — points are granted per-order, not per-event. WeChat/Stripe retries won't double-credit.
- **Order-centric security** — webhooks cannot create orders; duration is derived from plan code, not payload. Tamper-resistant.
- **Feature flags default OFF** — `FEATURE_SAAS_ENTITLEMENTS`, `FEATURE_MULTI_TENANT_FOUNDATION` are all default false. Safe.
- **bcrypt password hashing** with backward-compatible SHA-256 migration path.
- **Health checks** on all Docker services (api, celery, frontend, db, redis).
- **Acceptance test script** (`deploy_acceptance_mock.sh`) covers the full paid user journey.
- **Structured JSON logging** via `observability.py`.
- **Rate limiting** with Redis-backed token bucket (Lua script) and in-memory fallback.
- **Tenant context resolution** with explicit flag-gated compatibility path.
- **Production safety gates** in code — Alembic required for prod, SQLite rejected for prod, Redis required for prod.

---

*End of Audit Report*
*Commit SHA: see git log*
*Branch: `claude/audit-codebase-qFhts`*
*Next recommended action: Owner reviews this report and approves Round 1 (Security Hardening).*
