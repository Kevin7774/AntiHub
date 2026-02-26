# AntiHub Commercial Readiness Audit (Monetization Readiness)

**Date:** 2026-02-26
**Round:** Commercial Readiness Audit
**Branch:** `claude/audit-codebase-qFhts`
**Focus:** Can AntiHub charge real users and operate as a SaaS business?

---

## 1. Offer Clarity — What Is Being Sold?

### Current Product

AntiHub ("智能讲代码") sells **AI-powered code repository analysis** as a SaaS:
- User pastes a GitHub repo URL, system analyzes and explains it in 30-60 seconds
- Also includes a "技术选型决策引擎" (Technology Decision Engine) for comparing tech stacks
- Users pay for a **subscription** (monthly/quarterly/yearly) which grants **points**
- Points are consumed per analysis/deep-search action

### Target User
Developer teams, tech leads, and CTOs who need to quickly understand codebases for decision-making.

### What's Actually Sold (Plan/Credit Model)

| Plan | Price | Duration | Points/Month | Total Points |
|------|-------|----------|-------------|-------------|
| monthly_198 | ¥198 | 30 days | 10,000 | 10,000 |
| quarterly_398 | ¥398 | 90 days | 30,000 | 30,000 |
| yearly_1980 | ¥1,980 | 365 days | 150,000 | 150,000 |

### Clarity Issues

| # | Issue | Impact |
|---|-------|--------|
| C-1 | **Points-per-action cost is invisible** — user sees "10,000 积分/月" but has no idea how many analyses that buys | User cannot make rational purchase decision |
| C-2 | **Entitlements are not surfaced** — plans have real entitlements (deep_search, api.rpm, workspace tier) but the pricing page only shows points | User doesn't know what they're paying for beyond points |
| C-3 | **No free tier or trial** — `trial_days=0` on all plans. User must pay ¥198 before trying the product | Extremely high friction; kills conversion |
| C-4 | **"积分" (points) vs "配额" confusion** — header says "套餐与积分" but entitlements.py has separate RPM limits. User may think points = everything | Leads to "why am I rate limited, I have points" complaints |
| C-5 | **Quarterly plan 3x the monthly points for 2x the price** — this suggests the monthly plan is a bad deal, but it's not labeled as such | Price/value comparison is unclear without a comparison table |

---

## 2. Pricing/Package Readiness

### What Works
- 3-tier structure (monthly/quarterly/yearly) is standard and easy to understand
- Currency is CNY, appropriate for Chinese market
- Price points (¥198/¥398/¥1980) are in a reasonable SaaS range
- Frontend renders tier cards with features and CTA buttons

### Issues

| # | Issue | Severity |
|---|-------|----------|
| PR-1 | **No comparison table** — features per tier are hidden. Only point count varies visually | P0 |
| PR-2 | **No "most popular" or "best value" highlight** beyond a small "推荐" badge on yearly | P1 |
| PR-3 | **Savings math is not shown** — quarterly saves vs 3×monthly, yearly saves vs 12×monthly. Users must calculate themselves | P1 |
| PR-4 | **"monthly_points: 30000" for quarterly is confusing** — is it 30K/month (=90K total) or 30K total for the quarter? Backend: `billing/seed.py:83` grants 30K total. Frontend says "30,000 积分/月" which is misleading | P0 — **user will think they get 30K/month for 3 months** |
| PR-5 | **No annual price breakdown** — "¥1980/年" doesn't show "仅 ¥165/月" which is the key conversion message | P1 |
| PR-6 | **Plan descriptions are generic taglines**, not benefit statements ("适合高频日常使用" doesn't tell me what I get) | P1 |

### Critical: Points Labeling Bug (PR-4)

In `billing/seed.py`:
- `quarterly_398` has `monthly_points=30000` — this is the **total** point grant for the 90-day subscription
- But the frontend renders it as "30,000 积分/月" (per month)
- **User expects 30K × 3 = 90K points, but actually gets 30K total**
- This is a billing dispute waiting to happen

Evidence: `billing/seed.py:83`, `App.tsx:1408` (`formatNumber(plan.monthly_points) 积分`)

---

## 3. Checkout/Payment Readiness

### What Works
- Checkout creates an idempotent order, returns QR code for WeChat scan-to-pay
- Payment modal polls order status every 2 seconds
- 10-minute timeout with manual "verify payment" button
- Dev simulator blocked in production (`main.py:3695`)

### Failure Points Between Checkout and Entitlement Grant

| # | Failure Point | Impact | Evidence |
|---|--------------|--------|----------|
| CK-1 | **WeChat QR expires (~2h) but no user warning** | User sees stale QR, payment fails silently | No expiry countdown in `PaymentModal` |
| CK-2 | **No retry mechanism if checkout API fails** | User sees error toast but must restart flow manually | `App.tsx:1589-1593` — just pushToast on error |
| CK-3 | **Payment modal closes → polling stops** | User pays after closing modal, subscription never activates until next page load | `App.tsx:1882-1892` |
| CK-4 | **No order history page** | User can't see their past orders or check a specific order's status | No `/billing/orders/me` list endpoint exposed in UI |
| CK-5 | **"支付成功" toast is only 3.2 seconds** | If user is distracted, they miss the confirmation | `App.tsx:3423` |
| CK-6 | **No amount shown in payment modal** | User sees QR but not how much they're paying | `PaymentModal` shows plan name but not price |
| CK-7 | **No receipt or confirmation after payment** | No email, no PDF, no persistent "payment successful" page | Nothing in codebase |

### Missing User-Facing Messaging

- No "你将支付 ¥198" confirmation before QR generation
- No "订单已创建，等待支付" status indicator
- No "如支付遇到问题，请联系 XXX" fallback in payment modal
- No "积分将在支付成功后 X 秒内到账" expectation setting

---

## 4. Account Lifecycle Readiness

| Lifecycle State | Implemented? | User Visibility | Gap |
|----------------|-------------|-----------------|-----|
| **Free (no subscription)** | Yes | Shows "None" badge | No free trial, no demo credits |
| **Checkout in progress** | Yes | QR modal with polling | Missing: amount display, timeout warning |
| **Payment succeeded** | Yes | Toast + subscription activates | Missing: receipt, email, persistent confirmation |
| **Active subscription** | Yes | "Active" pill, plan name, expiry date | Missing: days remaining, renewal reminder |
| **Approaching expiry** | No | Nothing | P0: User has no warning before downgrade |
| **Expired** | Yes (backend) | Status changes to "expired" | Missing: "your plan expired" notification |
| **Renewal / Re-subscribe** | Partial | "继续续费" button visible | Missing: auto-renewal flow |
| **Upgrade** | No | Can buy higher plan, but no upgrade path | Old subscription just exists in parallel |
| **Downgrade** | No | Not implemented | No way to switch to cheaper plan |
| **Cancellation** | No | Not implemented | No cancel button, no prorated refund |
| **Refund** | Backend only | `flow_type: refund` exists in point model | No user-facing refund request flow |
| **Account deletion** | No | Not implemented | Required by Chinese data privacy regulations |

---

## 5. User Trust Readiness

### Missing Trust Elements (Critical for Chinese SaaS)

| # | Element | Status | Impact |
|---|---------|--------|--------|
| T-1 | **Terms of Service (服务协议)** | Missing entirely | Cannot legally collect payments |
| T-2 | **Privacy Policy (隐私政策)** | Missing entirely | Required by Chinese Cybersecurity Law |
| T-3 | **Refund Policy (退款政策)** | Missing entirely | WeChat Pay merchants must display this |
| T-4 | **Company identity / ICP备案号** | Not displayed | Chinese users expect to see this in footer |
| T-5 | **Business license / 营业执照** | Not displayed | Builds trust for enterprise buyers |
| T-6 | **Contact information** | Missing entirely | No email, WeChat, phone, or ticket system |
| T-7 | **Payment security badge** | Missing | Chinese users look for "安全支付" indicators |
| T-8 | **User testimonials / case studies** | Missing | "Who else uses this?" is a key trust signal |
| T-9 | **FAQ section** | Missing | Common questions about billing go unanswered |
| T-10 | **"官方" markers in UI** | Missing | WeChat ecosystem users expect official branding |

---

## 6. Conversion Readiness

### Landing Page (控制台)
- **CTA is functional** ("一键看懂仓库") but assumes user already knows the product
- **No value proposition above the fold** — just a URL input box
- **No "see it in action" demo or video** — cold start problem
- **No pricing teaser** — user must navigate to "会员" tab to discover pricing
- **No social proof** — no user count, no testimonials

### Pricing Page (会员)
- Plan cards exist but feature differentiation is weak
- **No FAQ below pricing** (common questions about billing)
- **No annual price breakdown** (¥165/月 equivalent)
- **No money-back guarantee** messaging

### Onboarding Friction
- Register requires "租户名称" — confusing for individual users
- No "skip" for optional fields — tenant code is optional but looks required
- No email field — cannot recover account or send receipts
- No password strength indicator

---

## 7. Customer Support and Escalation Readiness

| Capability | Status |
|-----------|--------|
| User contacts support | **No mechanism** — no email, chat, WeChat, or ticket form |
| Operator views user billing status | Yes — `/admin/billing/users/status` API |
| Operator views orders | Yes — `/admin/billing/orders` API |
| Operator views audit log | Yes — `/admin/billing/audit` API |
| Operator manually adjusts points | No API for manual point adjustment |
| Operator issues refund | No refund endpoint (order status FSM supports it, but no trigger) |
| Operator manually activates subscription | Yes — `/admin/saas/users/{username}/plan` |
| Operator deactivates user | Yes — `/admin/users/{username}` PATCH active=false |
| User self-service billing history | Partial — point history visible, order history not |

### Manual Operations Still Required

1. **Refunds**: Must be done manually in WeChat Pay merchant dashboard + manual DB update
2. **Billing disputes**: Must cross-reference `billing_audit_logs` with WeChat Pay records
3. **Plan changes mid-cycle**: No automated prorating — operator must manually create subscription
4. **Point adjustments**: No admin endpoint — requires direct DB access
5. **User communication**: No in-app messaging or email — must use external channel

---

## 8. Internal Operator Readiness

### What the Admin UI Provides (管理 tab)
- Plan CRUD (create/edit/activate/deactivate)
- Subscription query by user
- Order list with status
- Audit log viewer

### What's Missing for Operator Day-to-Day

| # | Capability | Status |
|---|-----------|--------|
| OP-1 | **Dashboard / summary view** | Missing — no total revenue, active subscribers count, MRR |
| OP-2 | **Manual point grant/deduction** | Missing — no admin endpoint |
| OP-3 | **Refund processing** | Missing — no refund API or UI |
| OP-4 | **User search by email/phone** | Missing — no email/phone fields |
| OP-5 | **Export billing data** | Missing — no CSV/Excel export |
| OP-6 | **Alerting on failed payments** | Missing — only visible in audit logs |
| OP-7 | **User impersonation for debugging** | Missing |
| OP-8 | **Bulk operations** | Missing — no batch activate/deactivate |

---

## Monetization Gap Matrix

### P0 — Must have before first payment

| # | Gap | Category | Evidence |
|---|-----|----------|----------|
| M-P0-1 | **Points labeling is misleading** ("积分/月" vs actual total grant) | product | `seed.py:83`, `App.tsx:1408` |
| M-P0-2 | **No Terms of Service / Privacy Policy / Refund Policy** | legal | No files or routes found |
| M-P0-3 | **No contact/support channel** | operations | No support mechanism in app |
| M-P0-4 | **No free trial or demo credits** — ¥198 minimum to try | conversion | `seed.py:47` — trial_days=0 |
| M-P0-5 | **Payment amount not shown in modal** | UX/trust | `PaymentModal` component |
| M-P0-6 | **No plan comparison table** showing feature differences | conversion | Pricing page only shows point counts |

### P1 — Should have before 10th customer

| # | Gap | Category |
|---|-----|----------|
| M-P1-1 | No order history page for users | UX |
| M-P1-2 | No subscription expiry warning | lifecycle |
| M-P1-3 | No receipt/confirmation after payment | trust |
| M-P1-4 | No savings calculation on pricing page | conversion |
| M-P1-5 | No FAQ section on pricing page | conversion |
| M-P1-6 | No admin manual point adjustment endpoint | operations |
| M-P1-7 | No refund processing flow | operations |
| M-P1-8 | Register asks for "租户名称" — confusing for individuals | onboarding |

### P2 — Should have before 100th customer

| # | Gap | Category |
|---|-----|----------|
| M-P2-1 | No upgrade/downgrade path | lifecycle |
| M-P2-2 | No auto-renewal | lifecycle |
| M-P2-3 | No cancellation flow | lifecycle |
| M-P2-4 | No account deletion (PIPL compliance) | legal |
| M-P2-5 | No email field in registration | lifecycle |
| M-P2-6 | No admin revenue dashboard | operations |
| M-P2-7 | No billing data export | operations |
| M-P2-8 | No in-app announcements/changelog | engagement |

---

## "Can Charge First 10 Users" Checklist

- [ ] Fix points labeling — show correct total grant, not misleading "per month"
- [ ] Add Terms of Service page (even minimal 1-page version)
- [ ] Add Privacy Policy page
- [ ] Add Refund Policy (even "联系客服处理退款")
- [ ] Add payment amount (¥198/¥398/¥1980) to the checkout modal
- [ ] Add at least one contact method (WeChat group QR code or email)
- [ ] Add a plan comparison showing what each tier unlocks (deep search, RPM, etc.)
- [ ] Add "points cost per action" reference (e.g. "一次仓库分析约消耗 X 积分")
- [ ] Wire up real WeChat Pay (or Stripe) credentials and test end-to-end
- [ ] Seed plans in production database via `init_prod_db.py`
- [ ] Test the full journey: register → pay → subscription active → use feature → verify points deducted

## "Can Charge First 100 Users" Checklist

Everything from the 10-user checklist, plus:

- [ ] Add free trial credits (e.g. 500 points for new users) to reduce signup friction
- [ ] Add order history page
- [ ] Add subscription expiry warning (7 days before, 1 day before)
- [ ] Add email receipts (or at minimum, WeChat template message)
- [ ] Add FAQ section to pricing page
- [ ] Add admin manual point adjustment endpoint
- [ ] Add admin refund processing flow
- [ ] Add savings breakdown on pricing page ("相当于 ¥165/月，省 ¥396")
- [ ] Add ICP备案号 in footer (if applicable)
- [ ] Set up basic uptime monitoring
- [ ] Make register simpler for individual users (tenant optional, not prominent)
- [ ] Add password recovery flow

---

## What I Still Need From You (Business/Legal/Operations)

| # | Item | Why |
|---|------|-----|
| 1 | **Points-per-action cost table** — how many points does each feature consume? | Need this to explain value to users |
| 2 | **Is 10K/30K/150K correct as TOTAL per subscription?** Or intended as monthly grants? | The field is called `monthly_points` but behavior is one-time grant |
| 3 | **Legal entity information** — company name, ICP filing, business license | Footer and Terms of Service |
| 4 | **Refund policy text** — conditions for refund (within 7 days? Pro-rated?) | Policy page |
| 5 | **Terms of Service text** — even a basic Chinese SaaS template | Policy page |
| 6 | **Privacy Policy text** — data collected, stored, processed | Policy page |
| 7 | **Support channel decision** — WeChat group? Email? Ticket system? | Contact information |
| 8 | **Free trial decision** — free credits for new users? If so, how many? | Onboarding friction reduction |
| 9 | **Pricing finality** — are ¥198/¥398/¥1980 final? Any launch discount? | Seed data and frontend |
| 10 | **Payment provider** — WeChat Pay only? Alipay? Stripe for international? | Provider wiring |

---

## Recommended Implementation Sequence

### Round A: Trust & Legal Foundation (before any payment)
1. Add Terms of Service page
2. Add Privacy Policy page
3. Add Refund Policy page
4. Add footer with company identity / ICP
5. Add contact/support section

### Round B: Pricing Clarity Fix (before any payment)
1. Fix points labeling (total vs monthly)
2. Add plan comparison table with entitlement differences
3. Add payment amount to checkout modal
4. Add "points cost per action" reference
5. Add savings breakdown on pricing page

### Round C: Checkout UX Hardening
1. Add QR expiry countdown
2. Add order history page
3. Add persistent payment confirmation
4. Add fallback/retry on checkout failure
5. Add free trial credits for new users

### Round D: Operator Tooling
1. Add admin manual point adjustment
2. Add admin refund endpoint
3. Add admin revenue summary dashboard
4. Add subscription expiry warning job
5. Add billing data export

---

*End of Commercial Readiness Audit*
