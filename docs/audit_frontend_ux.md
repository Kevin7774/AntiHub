# AntiHub Frontend UX/UI + Conversion Audit

**Date:** 2026-02-26
**Round:** Frontend UX/UI + Conversion Audit (Polish Before Launch)
**Branch:** `claude/audit-codebase-qFhts`
**Focus:** Trust, clarity, conversion, usability, consistency

---

## Pages Audited (from screenshots + source code)

| Page | Route | Screenshot | Status |
|------|-------|-----------|--------|
| Console (控制台) | `/create` | Screenshot 1 | Reviewed |
| Workspace (工作台) | `/workspace` | Screenshot 2 | Reviewed |
| Billing (会员) | `/billing` | Screenshot 3 | Reviewed |
| Admin (管理) | `/admin/billing` | Screenshot 4 | Reviewed |
| Login/Register | Auth gate | Source code reviewed | Not screenshotted |
| Case detail | `/case/:id` | Not screenshotted | Source code reviewed |
| Payment modal | Overlay | Not screenshotted | Source code reviewed |

---

## 1. Landing / First Impression (控制台 — Screenshot 1)

### What Works
- Clean, minimal design with warm tones (cream/beige background)
- Brand identity present: "AH" mark + "智能讲代码" + "一键看懂仓库"
- Primary CTA ("一键看懂仓库") is prominent with dark gradient button
- Secondary action ("运行环境自检") provides useful diagnostic

### Issues

| # | Issue | Severity | Detail |
|---|-------|----------|--------|
| L-1 | **No value proposition** — page opens directly to an input form | P0 | First-time visitor sees a URL input box with no explanation of what the product does or why they should use it |
| L-2 | **No demo/example result** — user must paste a URL and wait 30-60s to see value | P0 | No "see an example analysis" or sample output. Massive cold start problem |
| L-3 | **No trust signals above the fold** — no user count, testimonials, or recognizable logos | P1 | Chinese SaaS users expect social proof |
| L-4 | **"最近案例" shows "暂无案例"** — cold, unwelcoming empty state | P1 | Could show example/template cases instead of empty state |
| L-5 | **"技术选型决策引擎" section below fold** — second major feature gets no introduction | P1 | Just a text input with a placeholder example |
| L-6 | **Navigation labels are dense** — each nav item has main + sub text, creating visual noise | P2 | "控制台 / 仓库讲解" is two concepts in one button |
| L-7 | **No mobile consideration** — 1280px-centric layout, input fields are full width | P2 | Mobile scan-to-pay is the payment method but the console may not work well on mobile |

### Conversion Impact
- A developer lands on this page and sees a form. Without context, they don't know:
  - What happens when they paste a URL
  - How long it takes
  - What the output looks like
  - Why they should pay for this vs reading the code themselves
- **Recommended:** Add a 3-step "how it works" section and/or an example analysis result

---

## 2. Auth Pages / Onboarding (Login/Register)

### What Works
- Login/Register toggle is clean with active/inactive states
- Error banner exists for validation failures
- Submitting state disables button and shows "登录中…"
- Register auto-creates tenant workspace

### Issues

| # | Issue | Severity | Detail |
|---|-------|----------|--------|
| A-1 | **"API: <same-origin>" shown to users** — developer debug info, not user-friendly | P1 | `App.tsx:1019` — should be hidden in production |
| A-2 | **No password strength indicator** | P1 | No feedback on password quality |
| A-3 | **No password visibility toggle** | P2 | Standard UX pattern for password fields |
| A-4 | **Placeholder says "admin" for username** — confusing | P1 | Suggests the user should type "admin" |
| A-5 | **Register requires "租户名称"** — B2B jargon confusing for individual users | P0 | Developer trying the product doesn't know what a "tenant" is |
| A-6 | **No "forgot password" link** | P1 | No password recovery path exists |
| A-7 | **No email field** | P1 | Cannot send receipts or recover accounts |
| A-8 | **No Terms agreement checkbox** | P0 | Legally required before registration — "注册即表示同意《服务协议》" |
| A-9 | **Register form doesn't validate password minimum length on client** | P2 | Server-side validation exists but client gives no preview |

---

## 3. Pricing / Membership Page (会员 — Screenshot 3)

### What Works
- Clear section title "会员与订阅" with instructional subtitle
- "当前状态" card shows subscription status clearly
- "积分余额" card is visually prominent
- "升级套餐" section with dark cards is visually distinct

### Issues

| # | Issue | Severity | Detail |
|---|-------|----------|--------|
| B-1 | **Error banner "会员数据加载失败"** visible at top of page | P0 | Indicates API failure. Red error on the page where you ask users to pay is a trust killer |
| B-2 | **"暂无套餐，请联系管理员在管理页创建或启用"** shown to regular users | P0 | Plans failed to load from API, so fallback text is shown. User sees "no plans available" and cannot purchase |
| B-3 | **Price text is hardcoded in subtitle** — "月 198￥ / 季 398￥ / 年 1980￥" | P1 | If plans change, this text goes stale. `App.tsx:1686` |
| B-4 | **No plan comparison table** — all 3 tiers look identical except price/points | P0 | User cannot differentiate value across tiers |
| B-5 | **"管理员视图" badge visible** to admin users on pricing page | P2 | Minor: admin metadata leaking into user-facing page |
| B-6 | **Status shows "None" in English** for non-subscribed users | P1 | Mixed Chinese/English UI. Should be "无" or "未订阅" |
| B-7 | **No points-per-action reference** — "积分余额: —" tells user nothing about value | P1 | Need "一次分析约消耗 X 积分" context |
| B-8 | **No "days remaining" display** — just raw ISO date for expiry | P1 | "到期: 2026-03-28T..." is not human-friendly. Show "还剩 30 天" |
| B-9 | **"刷新中…" button in hero section** — looks like loading spinner on first load | P2 | Creates uncertainty: is data loading or refreshing? |

### The Error State Problem (B-1, B-2)
Screenshot 3 clearly shows "会员数据加载失败" error banner. The pricing section below shows the dark card with "暂无套餐" message. This means:
1. The API call to `/billing/plans` failed (likely plans not seeded in this environment)
2. The fallback plans defined in `BILLING_FALLBACK_PLANS` (`App.tsx:65-96`) were supposed to show, but the error toast appeared first
3. **This is the exact page where you want users to spend money, and it's showing an error**

---

## 4. Checkout/Payment UX

### What Works (from source code analysis)
- QR code rendered via `QRCodeCanvas` component
- Auto-polling every 2 seconds for payment status
- Manual "我已完成支付，立即校验" button
- Order ID visible and copyable
- Dev simulate button hidden in production

### Issues

| # | Issue | Severity | Detail |
|---|-------|----------|--------|
| PY-1 | **No payment amount shown in modal** | P0 | User sees QR but doesn't know how much they're paying |
| PY-2 | **No "请使用微信扫一扫" is small muted text** | P1 | Should be prominent — this IS the payment instruction |
| PY-3 | **Raw checkout_url shown** — "weixin://pay..." URL displayed | P1 | Technical info that confuses users. Should be hidden or collapsed |
| PY-4 | **No QR expiry countdown** | P1 | WeChat QR codes expire in ~2 hours. No user warning |
| PY-5 | **Closing modal stops polling** — user pays after closing, subscription stuck | P1 | Need background polling or notification |
| PY-6 | **"检测中 · 当前状态 pending"** is technical language | P1 | Should say "等待支付中…" not "当前状态 pending" |
| PY-7 | **No visual success celebration** — just a toast that disappears in 3.2s | P2 | After paying ¥198+, a brief toast feels anticlimactic |
| PY-8 | **Dev "simulate payment" button visible in non-prod** | P2 | May confuse testers or demo users if they see it |

---

## 5. Admin/Console Usability (管理 — Screenshot 4)

### What Works
- Tab-based navigation (套餐, 订阅查询, 订单, 审计)
- Table layout for plan management
- Audit log viewer exists

### Issues

| # | Issue | Severity | Detail |
|---|-------|----------|--------|
| AD-1 | **Error banner "管理数据加载失败"** visible at top | P0 | Same API failure issue as billing page |
| AD-2 | **"BILLING ADMIN" title is in English** — inconsistent with Chinese UI | P1 | `App.tsx` has mixed English/Chinese section titles |
| AD-3 | **Table headers are in English** ("CODE", "NAME", "PRICE", "POINTS", "ACTIVE", "ACTIONS") | P1 | Inconsistent with the rest of the Chinese UI |
| AD-4 | **"暂无套餐" shown** — plans not loaded | P0 | Admin cannot manage billing if plans don't load |
| AD-5 | **No confirmation for dangerous actions** (deactivating plans, deleting entitlements) | P1 | One mis-click could disable a plan for all users |
| AD-6 | **No revenue summary or subscriber count** | P1 | Admin has no overview of business metrics |
| AD-7 | **Audit log detail requires separate click/expand** | P2 | Key information not visible at a glance |

---

## 6. Visual Consistency

### Overall Aesthetic
The warm beige/cream design language is distinctive and pleasant. However:

| # | Issue | Severity | Detail |
|---|-------|----------|--------|
| V-1 | **Mixed Chinese/English** throughout the UI | P1 | "Active" / "None" / "BILLING ADMIN" / "CODE" etc. alongside Chinese labels. Pick one language or be intentional about which terms stay in English |
| V-2 | **Status pills inconsistent** — some use English ("Active", "None"), some Chinese | P1 | Should be consistently Chinese for Chinese users |
| V-3 | **Font size hierarchy unclear** in workspace page | P2 | "租户身份" and "订阅与积分" cards have same visual weight as less important sections |
| V-4 | **"LP" floating button** in bottom-right corner (all screenshots) | P1 | Unclear what this does — no tooltip. If it's a locale picker, it needs labeling |
| V-5 | **Dark pricing cards vs light page** — strong contrast is good for conversion but the cards currently show "暂无套餐" | P2 | When plans load correctly, this design should work well |
| V-6 | **Error banners are pink/salmon** — not standard red | P2 | Acceptable but slightly soft for critical errors |

---

## 7. System Feedback (Loading, Empty, Error, Success)

| State | Implementation | Quality |
|-------|---------------|---------|
| **Loading** | "刷新中…" button text change | OK but no spinner/skeleton |
| **Empty** | "暂无案例", "暂无套餐", "暂无租户" | Functional but cold — no helpful CTA |
| **Error** | Red banner with "查看详情" expand + copy | Good — error details are copyable |
| **Success** | Toast notification (3.2s auto-dismiss) | Too brief for important actions like payment |
| **Checking** | "正在检查登录状态…" on auth | OK |

### Issues

| # | Issue | Severity |
|---|-------|----------|
| F-1 | **No skeleton loading** — page jumps when data loads | P2 |
| F-2 | **Empty states have no CTA** — "暂无案例" should link to "创建第一个案例" | P1 |
| F-3 | **Payment success toast is 3.2s** — critical financial event should persist | P1 |
| F-4 | **Error banner has "查看详情" but detail is often raw API error** | P2 |

---

## 8. Mobile/Responsive Risks

The product targets WeChat scan-to-pay, which means users need a computer (to show QR) and a phone (to scan). However:

| # | Risk | Severity |
|---|------|----------|
| R-1 | **No responsive breakpoints visible** in styles.css | P1 |
| R-2 | **266KB App.tsx single file** — long initial parse time on mobile | P2 |
| R-3 | **Pricing cards may stack poorly on narrow screens** | P1 |
| R-4 | **QR code is 240px fixed** — may not scale well on tablets | P2 |
| R-5 | **The desktop-to-mobile redirect scenario** — if user visits on phone, they can't scan QR from the same phone | P1 — need to detect mobile and show "请在电脑端完成支付" |

---

## 9. Chinese User Trust Cues

For a paid Chinese SaaS product, users expect:

| # | Trust Cue | Present? | Priority |
|---|-----------|----------|----------|
| CT-1 | **ICP备案号** in footer | No | P0 if hosting in China |
| CT-2 | **公司名称** in footer | No | P0 |
| CT-3 | **"安全支付" badge** near checkout | No | P1 |
| CT-4 | **微信支付 official logo** in payment modal | No | P1 |
| CT-5 | **"已有 X 用户使用"** social proof | No | P1 |
| CT-6 | **服务协议/隐私政策** links | No | P0 |
| CT-7 | **Customer service QR code** (WeChat group) | No | P1 |
| CT-8 | **实名认证 / 企业认证** badge | No | P2 |
| CT-9 | **SSL padlock awareness** (HTTPS indicator) | Via Cloudflare | OK |
| CT-10 | **发票** (invoice/receipt) availability mention | No | P1 for enterprise users |

---

## UI/UX Gap List Summary

### P0 — Must fix before launch

| # | Issue | Page |
|---|-------|------|
| UX-P0-1 | No value proposition / "what is this" on landing page | Console |
| UX-P0-2 | "会员数据加载失败" error on billing page | Billing |
| UX-P0-3 | "暂无套餐" shown when plans fail to load | Billing |
| UX-P0-4 | No payment amount in checkout modal | Payment Modal |
| UX-P0-5 | No Terms/Privacy agreement on registration | Login/Register |
| UX-P0-6 | Register asks for "租户名称" — jargon barrier | Login/Register |
| UX-P0-7 | No plan comparison table on pricing page | Billing |
| UX-P0-8 | No legal pages (Terms, Privacy, Refund) linked anywhere | Global |

### P1 — Important polish before accepting payments

| # | Issue | Page |
|---|-------|------|
| UX-P1-1 | Mixed Chinese/English labels (Active, None, CODE, NAME) | All pages |
| UX-P1-2 | No demo/example result for new visitors | Console |
| UX-P1-3 | "API: <same-origin>" shown on login page | Login |
| UX-P1-4 | Username placeholder says "admin" | Login |
| UX-P1-5 | Raw ISO timestamp for subscription expiry | Billing, Workspace |
| UX-P1-6 | Empty states have no helpful CTA | Console, Workspace |
| UX-P1-7 | No forgot password flow | Login |
| UX-P1-8 | No contact/support link in UI | Global |
| UX-P1-9 | "LP" button in bottom-right has no tooltip | Global |
| UX-P1-10 | Payment status shows "pending" in English | Payment Modal |
| UX-P1-11 | No mobile detection / "please use desktop" for payment | Billing |
| UX-P1-12 | "BILLING ADMIN" and English table headers | Admin |

### P2 — Nice to have polish

| # | Issue | Page |
|---|-------|------|
| UX-P2-1 | No skeleton loading states | All pages |
| UX-P2-2 | No password visibility toggle | Login |
| UX-P2-3 | Payment success toast too brief (3.2s) | Payment Modal |
| UX-P2-4 | No dark mode consideration | Global |
| UX-P2-5 | Error banner detail shows raw API errors | All pages |

---

## Conversion Risk List

Things that may reduce purchase intent, ranked by impact:

1. **Error banner on billing page** — user sees failure before seeing pricing. Trust destroyed immediately.
2. **No product demo** — user must register and wait 60s to see any value. Most will bounce.
3. **¥198 minimum with zero free trial** — extremely high first-purchase friction for an unknown product.
4. **No feature comparison table** — all plans look the same except price. User defaults to cheapest or abandons.
5. **No social proof** — "who else uses this?" is unanswered. No user count, no testimonials.
6. **"租户名称" in registration** — individual developer doesn't know what this is, may abandon registration.
7. **No payment amount in checkout modal** — user is uncertain about what they're paying.
8. **Mixed language** — feels unpolished. Chinese users may question if this is a real product.
9. **No company identity** — who is behind this product? No footer, no about page.
10. **No refund/cancellation visibility** — "what if I don't like it?" is unanswered.

---

## Quick Win Polish List (1-2 days)

These changes are small, safe, and high-impact:

| # | Change | Files Affected | Impact |
|---|--------|---------------|--------|
| QW-1 | Change "None" to "未订阅", "Active" to "已激活" | App.tsx | Trust +: consistent Chinese |
| QW-2 | Hide "API: <same-origin>" in production | App.tsx | Trust +: no debug info on login |
| QW-3 | Change username placeholder from "admin" to "请输入用户名" | App.tsx | Clarity + |
| QW-4 | Show ¥ amount in payment modal header | App.tsx | Trust ++: user knows what they pay |
| QW-5 | Change "当前状态 pending" to "等待支付中…" | App.tsx | Clarity + |
| QW-6 | Format expiry date as "到期: 2026-03-28 (还剩30天)" | App.tsx | Clarity + |
| QW-7 | Translate admin table headers to Chinese | App.tsx | Consistency + |
| QW-8 | Add tooltip to "LP" button | App.tsx or styles.css | Clarity + |
| QW-9 | Make "租户名称" optional and less prominent in register form | App.tsx | Onboarding friction - |
| QW-10 | Add "首次注册赠送 X 积分" message (even if 0 for now, prepare the UI) | App.tsx | Conversion + |

---

## Structural UX Improvements (Later Rounds)

| # | Improvement | Scope | Round |
|---|-------------|-------|-------|
| S-1 | **Add landing/hero section** with value proposition, 3-step "how it works", and example output | New section in Console page | Round 2 |
| S-2 | **Add plan comparison table** showing entitlements per tier | Billing page restructure | Round 1 |
| S-3 | **Split App.tsx** into page components | Architecture refactor | Round 3+ |
| S-4 | **Add Terms/Privacy/Refund pages** as separate routes | New pages | Round 1 |
| S-5 | **Add order history section** to billing page | New section | Round 2 |
| S-6 | **Add admin revenue dashboard** as first admin tab | New section | Round 3 |
| S-7 | **Add responsive breakpoints** for tablet/mobile | styles.css overhaul | Round 3+ |
| S-8 | **Add footer** with company info, ICP, links | Global component | Round 1 |

---

## Pages/States/Screenshots I Need for Stronger Audit

| # | What I Need | Why |
|---|------------|-----|
| 1 | **Login/Register page screenshot** (both modes) | Verify auth UX, error states |
| 2 | **Payment modal with QR code visible** (after clicking checkout) | Verify checkout UX, amount display |
| 3 | **Payment success state** | Verify the toast and post-payment flow |
| 4 | **A completed case detail page** (after analysis) | Verify the core product value delivery |
| 5 | **Admin page with plans loaded** (after seeding) | Verify plan management UX |
| 6 | **Admin audit log view** | Verify operator experience |
| 7 | **Billing page with active subscription** (after successful payment) | Verify subscription display |
| 8 | **Paywall modal** (triggered by 402 response) | Verify upsell UX |
| 9 | **Mobile viewport screenshot** (any page) | Verify responsive behavior |
| 10 | **Empty case list → first analysis → result** flow (3 screenshots) | Verify core user journey end-to-end |

---

## Recommended Next UI Patch Rounds

### UI Round 1: Trust & Legal Layer (before first payment)
**Scope:** App.tsx global elements + new pages
1. Add footer component with company identity, ICP, policy links
2. Add Terms of Service page (route: `/terms`)
3. Add Privacy Policy page (route: `/privacy`)
4. Add Refund Policy page (route: `/refund`)
5. Add "注册即表示同意《服务协议》和《隐私政策》" to register form
6. Add contact/support section (WeChat group QR or email)

### UI Round 2: Pricing Clarity (before first payment)
**Scope:** BillingPage section of App.tsx
1. Add plan comparison table with entitlement differences
2. Show payment amount in checkout modal
3. Fix points labeling (total grant, not "per month")
4. Add savings breakdown ("相当于 ¥165/月")
5. Change "None" / "Active" to Chinese equivalents
6. Format dates as human-readable with "days remaining"

### UI Round 3: Quick Wins (1-2 day polish)
**Scope:** Minor App.tsx changes
1. All items from Quick Win Polish List above
2. Empty state CTAs ("创建第一个案例", "选择一个套餐开始")
3. Hide debug info in production
4. Translate admin table headers

### UI Round 4: Onboarding & Landing (conversion improvement)
**Scope:** Console page + register flow
1. Add hero section with value proposition
2. Add "how it works" 3-step visual
3. Add example/demo analysis result
4. Simplify register form (tenant optional, not prominent)
5. Add password strength indicator

---

*End of Frontend UX/UI + Conversion Audit*
