# AntiHub Post-Audit Fix Roadmap (Launch P0/P1)

**Date:** 2026-02-26
**Round:** Post-Audit Fix Roadmap Planning
**Branch:** `claude/audit-codebase-qFhts`
**Goal:** Turn audit findings into a scoped, safely executable implementation sequence.

---

## 1. Consolidated Top 10 Blockers (Deduplicated)

These are the 10 unique blockers distilled from all three audit reports, ranked by
"distance to accepting real money safely".

| Rank | Blocker | Source IDs | Category | Blocks Charging? |
|------|---------|-----------|----------|-----------------|
| **1** | **Swagger UI (`/docs`, `/redoc`, `/openapi.json`) publicly accessible in production** — full API surface exposed to the internet | P0-7 | security | Yes — attacker can enumerate every endpoint |
| **2** | **`.bak` files tracked in git** — two 190KB copies of `main.py` with full source in repo | P0-3 | security | Yes — leaked source aids targeted attacks |
| **3** | **DB password defaults to `change_me`** in `docker-compose.prod.yml` | P0-9 | security | Yes — trivial database takeover if operator forgets |
| **4** | **No pre-deploy DB backup** in `update_prod.sh` — migration runs without snapshot | P0-10 | ops | Yes — bad migration = irrecoverable data loss |
| **5** | **CORS_ORIGINS defaults to `localhost:5173`** — production browser requests will be rejected | P0-5 | ops | Yes — frontend literally cannot talk to API |
| **6** | **StripeProvider raises `NotImplementedError`** — checkout crashes if `PAYMENT_PROVIDER=stripe` | P0-1 | code | Yes — silent bomb if misconfigured |
| **7** | **Points labeling is misleading** — frontend says "积分/月" but backend grants a one-time total | M-P0-1, PR-4 | product/trust | Yes — billing disputes from first customer |
| **8** | **No payment amount shown in checkout modal** — user scans QR without knowing price | M-P0-5, PY-1 | UX/trust | Yes — payment trust violation |
| **9** | **No Terms of Service / Privacy Policy / Refund Policy** — anywhere in app | P0-4, M-P0-2, UX-P0-8, T-1/2/3 | legal | Yes — illegal to collect payment without |
| **10** | **No contact/support channel** — paying user has zero recourse if something goes wrong | M-P0-3, P1-10, UX-P1-8, T-6 | operations/trust | Borderline — not illegal, but destroys trust on first incident |

---

## 2. Decision Dependency Map

### Items that REQUIRE your decision/input first

These **cannot be patched** until you provide information:

| ID | Decision Needed | Blocks Which Rounds? | What to Provide |
|----|----------------|---------------------|-----------------|
| **D-1** | **Points semantics**: Is `monthly_points` a one-time total grant per subscription, or monthly? (Quarterly 30K: is it 30K total or 30K×3?) | L2 | Answer: "total" or "per-month-recurring" |
| **D-2** | **Legal page content**: Terms of Service, Privacy Policy, Refund Policy (Chinese text) | L3 | Provide text (even a draft), or confirm you'll write it and I scaffold the pages |
| **D-3** | **Company identity**: Company name, ICP备案号 (if applicable), contact email or WeChat | L3 | Strings for footer/about |
| **D-4** | **Support channel**: WeChat group QR? Email address? External ticket URL? | L3 | One contact method I can link in the UI |
| **D-5** | **Payment provider**: WechatPay only? Stripe? Both? (Affects Blocker #6 fix approach) | L4 | Decision: which provider(s) for launch |
| **D-6** | **WechatPay credentials** (MCHID, APPID, cert serial, private key, APIv3 key, notify URL) | L4 | Credentials (never in repo — env only) |
| **D-7** | **Production domain** for CORS_ORIGINS and WECHATPAY_NOTIFY_URL | L1 (partial), L4 | The actual domain string |
| **D-8** | **Pricing finality**: Are ¥198/¥398/¥1980 final? | L2 | Confirm or revise |
| **D-9** | **Free trial decision**: Give new users demo credits? If so, how many? | L5+ (not blocking launch) | Number or "no" |

### Items that CAN be patched immediately (no business decision needed)

| Blocker # | Item | Why No Decision Needed |
|-----------|------|----------------------|
| 1 | Gate `/docs`/`/redoc`/`/openapi.json` behind auth in prod | Pure security fix; no business logic change |
| 2 | Remove `.bak` files from git | Pure cleanup |
| 3 | Remove `change_me` default; fail-fast if POSTGRES_PASSWORD unset | Pure security fix |
| 4 | Add backup step to `update_prod.sh` | Pure ops safety |
| 5 | Add CORS validation warning on startup (partial; full fix needs D-7) | Defensive code; warns operator |
| 6 | Make StripeProvider return 501 instead of crashing | Pure defensive fix |
| 8 | Show price amount in payment modal | Data already available in plan object |
| 10 | (Partial) — add placeholder "contact" section with configurable env var | Scaffolding that works once D-4 is set |

---

## 3. Patch Round Sequence

### Round L1: Security Hardening (No decision needed — patch immediately)

**Theme:** Remove attack surface and prevent data loss.
**Scope:** 1 topic (security), ~5 files.

| # | Change | File(s) | Why |
|---|--------|---------|-----|
| 1 | Remove `/docs`, `/redoc`, `/openapi.json` from `PUBLIC_AUTH_PATHS`; gate behind admin auth when `APP_ENV` is prod | `main.py` (~line 501-511) | Blocker #1 |
| 2 | Delete `.bak` files from git tracking | `main.py.bak_2026-02-25_113658`, `main.py.bak_2026-02-25_114837` | Blocker #2 |
| 3 | Add `.bak` / `*.bak*` to `.gitignore` | `.gitignore` | Prevent recurrence |
| 4 | Remove `change_me` default for `POSTGRES_PASSWORD`; make compose fail if unset | `docker-compose.prod.yml` (~line 112) | Blocker #3 |
| 5 | Add pre-migration backup call in `update_prod.sh` before `alembic upgrade head` | `scripts/update_prod.sh` (~line 79-80) | Blocker #4 |

**Files touched:** 5 (main.py, 2×.bak, .gitignore, docker-compose.prod.yml, scripts/update_prod.sh)
**Risk:** Low. All changes are subtractive or additive guards. No business logic changed.
**Validation:**
- `git ls-files '*.bak*'` returns empty
- `grep 'change_me' docker-compose.prod.yml` returns empty
- `grep '/docs' main.py` in PUBLIC_AUTH_PATHS returns empty
- `grep 'backup' scripts/update_prod.sh` shows backup step before migration
- Existing tests pass (`pytest`)

**Rollback:** `git revert <commit>` — all changes are isolated and independent.

---

### Round L2: Billing Display Fixes (Needs D-1 and D-8 answered first)

**Theme:** Fix misleading information that would cause billing disputes.
**Scope:** 1 topic (billing display), ~3 files.

| # | Change | File(s) | Why |
|---|--------|---------|-----|
| 1 | Fix points label: if `monthly_points` is total-per-subscription, change frontend display from "X 积分/月" to "共 X 积分" (or vice versa — depends on D-1) | `frontend/src/App.tsx` (~lines 1148, 1408, 1676) | Blocker #7 |
| 2 | Show payment amount (¥) in PaymentModal header — pass `price_cents` and `currency` from plan to modal | `frontend/src/App.tsx` (PaymentModal component, ~line 1770+; BillingPage startCheckout, ~line 1559+) | Blocker #8 |
| 3 | Change "None" → "未订阅", "Active" → "已激活", "pending" → "等待支付中" | `frontend/src/App.tsx` (BillingPage, PaymentModal, WorkspacePage — scattered) | Mixed language trust issue (UX-P1-1, PY-6) |
| 4 | Format expiry date as human-readable "2026-03-28 (还剩30天)" | `frontend/src/App.tsx` (subscription display areas) | UX-P1-5 |

**Files touched:** 1-2 (App.tsx, possibly styles.css for modal price styling)
**Risk:** Low — purely display changes. No backend, no DB, no API contract change.
**Depends on:** D-1 (points semantics), D-8 (pricing finality).
**Validation:**
- Visual: billing page shows correct points label
- Visual: payment modal shows "¥198" in header
- Visual: subscription status shows Chinese labels
- Visual: expiry shows human-friendly date
- `npm run build` succeeds with no TypeScript errors

**Rollback:** `git revert <commit>` — frontend-only change.

---

### Round L3: Trust & Legal Layer (Needs D-2, D-3, D-4 answered first)

**Theme:** Add minimum legal compliance and trust signals required to collect payment.
**Scope:** 1 topic (legal/trust), ~3-4 files.

| # | Change | File(s) | Why |
|---|--------|---------|-----|
| 1 | Add footer component with company name, ICP (if applicable), and policy links | `frontend/src/App.tsx` (new `<Footer>` section at bottom of `<App>`) | Blocker #9, CT-1/CT-2 |
| 2 | Add `/terms`, `/privacy`, `/refund` routes rendering owner-provided text (or placeholder "coming soon" with date) | `frontend/src/App.tsx` (new route handling + 3 simple page components) | Blocker #9 |
| 3 | Add "注册即表示同意《服务协议》和《隐私政策》" link text below register button | `frontend/src/App.tsx` (LoginScreen register mode, ~line 1070) | UX-P0-5, A-8 |
| 4 | Add contact/support section — render `VITE_SUPPORT_CONTACT` env var (email/WeChat) in footer and a "联系我们" link | `frontend/src/App.tsx` (Footer), `frontend/.env.production` | Blocker #10 |

**Files touched:** 2-3 (App.tsx, styles.css, possibly frontend .env template)
**Risk:** Low — additive frontend content. No backend changes.
**Depends on:** D-2 (legal text), D-3 (company identity), D-4 (support channel).
**Validation:**
- Visual: footer renders on every page with company info and links
- Click: `/terms`, `/privacy`, `/refund` routes render content
- Visual: register form shows ToS agreement text
- Visual: contact info visible in footer
- `npm run build` succeeds

**Rollback:** `git revert <commit>` — frontend-only change.

---

### Round L4: Payment Provider Safety (Needs D-5, D-6, D-7 answered first)

**Theme:** Ensure checkout cannot crash; prepare real provider wiring.
**Scope:** 1 topic (payment provider), ~4 files.

| # | Change | File(s) | Why |
|---|--------|---------|-----|
| 1 | StripeProvider: return HTTP 501 "Stripe integration not yet available" instead of raising `NotImplementedError` | `billing/provider.py` (~line 111) | Blocker #6 |
| 2 | Add startup validation: if `PAYMENT_PROVIDER` is set to an unimplemented provider, log a critical warning and fail-fast (or fall back to mock with warning) | `billing/provider.py` (factory function) | Prevent silent misconfiguration |
| 3 | Add CORS_ORIGINS startup warning: if still contains `localhost` when `APP_ENV=prod`, log warning | `main.py` (app startup/lifespan) | Blocker #5 partial |
| 4 | Add `.env.prod.example` entry for `CORS_ORIGINS` with `# REQUIRED: set to your production domain` | `.env.prod.example` | Documentation for operator |
| 5 | (If D-5 = WechatPay) Validate all required WechatPay env vars on startup; fail-fast with clear error if any missing | `billing/wechatpay.py` or `billing/provider.py` | Prevent cryptic runtime errors |

**Files touched:** 3-4 (billing/provider.py, main.py, .env.prod.example, optionally billing/wechatpay.py)
**Risk:** Medium-low. Touches billing provider factory but only adds guards, not logic changes.
**Depends on:** D-5 (provider choice), D-6 (credentials for validation), D-7 (domain for CORS).
**Validation:**
- Set `PAYMENT_PROVIDER=stripe`, hit `/billing/checkout` → get 501 not 500
- Set `PAYMENT_PROVIDER=invalid_xyz` → app refuses to start with clear error
- Set `APP_ENV=prod` with localhost CORS → startup log shows warning
- Existing tests pass (`pytest`)
- `scripts/deploy_acceptance_mock.sh` still passes end-to-end (mock provider path)

**Rollback:** `git revert <commit>` — provider factory is stateless; guards are additive.

---

### Round L5: UX Quick Wins (No decision needed — patch immediately)

**Theme:** Low-risk frontend polish that improves trust and clarity.
**Scope:** 1 topic (UX polish), ~2 files.

| # | Change | File(s) | Why |
|---|--------|---------|-----|
| 1 | Hide "API: <same-origin>" debug text on login page in production | `frontend/src/App.tsx` (~line 1019) | A-1 |
| 2 | Change username placeholder from "admin" to "请输入用户名" | `frontend/src/App.tsx` (~line 1029) | A-4 |
| 3 | Make "租户名称" field less prominent in register (add "（可选）" label, collapse by default) | `frontend/src/App.tsx` (register form, ~line 1047) | A-5, UX-P0-6, M-P1-8 |
| 4 | Translate admin table headers to Chinese ("代号", "名称", "价格", "积分", "状态", "操作") | `frontend/src/App.tsx` (AdminBillingPage) | AD-2, AD-3, UX-P1-12 |
| 5 | Improve payment modal status text: "检测中 · 当前状态 pending" → "等待支付中…" / "支付成功！" | `frontend/src/App.tsx` (PaymentModal, ~line 1953) | PY-6 |
| 6 | Hide raw `checkout_url` by default; show only behind "显示链接" toggle | `frontend/src/App.tsx` (PaymentModal, ~line 1942) | PY-3 |

**Files touched:** 1-2 (App.tsx, possibly styles.css)
**Risk:** Very low — cosmetic text changes. No logic, no backend.
**Validation:**
- Visual spot-check of each change
- `npm run build` succeeds
- No TypeScript errors

**Rollback:** `git revert <commit>`.

---

### Round L6: Ops Resilience (No decision needed — patch immediately)

**Theme:** Prevent Redis failure from taking down the entire API.
**Scope:** 1 topic (resilience), ~2 files.

| # | Change | File(s) | Why |
|---|--------|---------|-----|
| 1 | Rate limiter: catch Redis connection errors at runtime and fall back to in-memory instead of raising `RuntimeError` | `billing/middleware.py` (~line 157, 192, 202) | P1-7 — Redis blip = total API outage |
| 2 | Add Celery beat schedule for `close_timed_out_orders` (every 10 min) | `celery_app.py` or equivalent Celery config + `billing/tasks.py` | P1-9 — stale orders accumulate |

**Files touched:** 2-3 (billing/middleware.py, celery config, billing/tasks.py)
**Risk:** Medium. Touches rate limiter and adds a periodic task. Rate limiter fallback changes behavior (allows requests during Redis outage instead of blocking them — acceptable tradeoff).
**Validation:**
- Stop Redis, make an API request → should succeed (rate limited in-memory) instead of 500
- Start Redis again → transparently switches back
- Check Celery beat schedule shows `close_timed_out_orders` task
- Existing tests pass

**Rollback:** `git revert <commit>`.

---

## 4. Round Execution Order & Dependencies

```
 IMMEDIATELY PATCHABLE          NEEDS YOUR INPUT FIRST
 (no decisions needed)          (blocked until you answer)
 ═══════════════════════        ═══════════════════════════

 ┌──────────────┐
 │  Round L1    │  ← START HERE (security hardening)
 │  Security    │
 └──────┬───────┘              ┌──────────────────────┐
        │                      │  You provide:        │
        │                      │  D-1 (points)        │
        │                      │  D-8 (pricing)       │
        │                      └──────────┬───────────┘
        │                                 │
        ▼                                 ▼
 ┌──────────────┐              ┌──────────────────────┐
 │  Round L5    │              │  Round L2            │
 │  UX Quick    │              │  Billing Display     │
 │  Wins        │              │  Fixes               │
 └──────┬───────┘              └──────────┬───────────┘
        │                                 │
        │                      ┌──────────────────────┐
        │                      │  You provide:        │
        │                      │  D-2 (legal text)    │
        │                      │  D-3 (company info)  │
        │                      │  D-4 (support)       │
        │                      └──────────┬───────────┘
        │                                 │
        │                                 ▼
        │                      ┌──────────────────────┐
        │                      │  Round L3            │
        │                      │  Trust & Legal       │
        │                      └──────────┬───────────┘
        │                                 │
        ▼                      ┌──────────────────────┐
 ┌──────────────┐              │  You provide:        │
 │  Round L6    │              │  D-5 (provider)      │
 │  Ops         │              │  D-6 (credentials)   │
 │  Resilience  │              │  D-7 (domain)        │
 └──────────────┘              └──────────┬───────────┘
                                          │
                                          ▼
                               ┌──────────────────────┐
                               │  Round L4            │
                               │  Payment Provider    │
                               │  Safety              │
                               └──────────────────────┘
                                          │
                                          ▼
                               ┌──────────────────────┐
                               │  READY TO CHARGE     │
                               │  FIRST USER          │
                               └──────────────────────┘
```

**Fastest path to charging:**
1. You answer D-1 + D-8 now → I patch L1 + L2 in parallel
2. You answer D-2/D-3/D-4 → I patch L3
3. You answer D-5/D-6/D-7 → I patch L4
4. L5 and L6 can be interleaved at any point

---

## 5. What You Should Answer Now

To unblock the maximum number of rounds, please answer these:

**Quick answers (one line each):**

1. **D-1**: Is `monthly_points` a one-time total grant per subscription, or monthly recurring?
   _(e.g. quarterly_398 with monthly_points=30000 — is that 30K total or 30K×3?)_

2. **D-8**: Are the current prices final? (¥198 / ¥398 / ¥1980)

3. **D-5**: Which payment provider(s) for launch? (wechatpay / stripe / both)

4. **D-7**: What is the production domain? (e.g. `app.antihub.cn`)

**Longer answers (can provide later, but blocks L3):**

5. **D-2**: Terms of Service / Privacy Policy / Refund Policy text (Chinese).
   _(If you want, I can scaffold placeholder pages now and you fill in text later.)_

6. **D-3**: Company name and ICP备案号 (if applicable).

7. **D-4**: Support contact method (WeChat group QR image URL, email, or external URL).

---

## 6. Summary: What Can Start Now vs What's Blocked

| Round | Can Start Now? | Blocked By | Files | Risk |
|-------|---------------|------------|-------|------|
| **L1** | **YES** | Nothing | 5 | Low |
| **L2** | No | D-1, D-8 | 1-2 | Low |
| **L3** | No | D-2, D-3, D-4 | 2-3 | Low |
| **L4** | No | D-5, D-6, D-7 | 3-4 | Med-Low |
| **L5** | **YES** | Nothing | 1-2 | Very Low |
| **L6** | **YES** | Nothing | 2-3 | Medium |

**Recommended first action:** Approve Round L1 (Security Hardening). It's the highest-impact, lowest-risk round and needs zero decisions from you.

---

*End of Roadmap*
*Branch: `claude/audit-codebase-qFhts`*
*Next action: Owner answers D-1 through D-7, approves Round L1 for immediate execution.*
