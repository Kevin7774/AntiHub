# L7 — SearchSkill v1: Search Relevance & Retrieval Quality (AUDIT ONLY)

## Plan (file-level, scope-locked)

### Critical Findings from Codebase Audit

I traced every line of the search/recommendation pipeline end-to-end:

```
User uploads PRD
  → decision/service.py::recommend_products()        [entry point]
    → _infer_query_intents()                          [intent detection]
    → _infer_hard_intents_from_llm()                  [LLM intent cross-check — 1st LLM call]
    → _score_case() per catalog case                  [catalog scoring]
    → _apply_intent_guardrails()                      [hard filter]
    → _fetch_external_recommendations()               [external lane]
      → recommend/service.py::recommend_repositories()
        → build_requirement_profile()                 [2nd LLM call — DUPLICATE of 1st]
        → extract_search_queries()                    [3rd LLM call — keyword extraction]
        → _search_multi_query_provider_parallel()     [GitHub/Gitee/GitCode]
        → rank_candidates()                           [4th LLM call — reranking]
        → summarize_findings()                        [5th LLM call — deep mode only]
```

**17 concrete issues found.** Grouped by SearchSkill v1 sections:

---

### 1) Requirement Parsing (deterministic) — Capability Map

**Current state:** `decision/service.py` lines 35-49 define `DEFAULT_CAPABILITY_ALIASES` with 13 capabilities. `INTENT_GROUP_ALIASES` (lines 76-85) define 8 intent groups. These work as a flat list — there is NO layered Capability Map.

**What's missing for L1/L2/L3 layers:**

| Layer | What exists today | What's missing |
|-------|-------------------|----------------|
| L1: Product modules | `DEFAULT_CAPABILITY_ALIASES` (13 codes) | No hierarchical grouping; "payment_gateway" and "split_settlement" are siblings when they should nest under "Finance" |
| L2: Technical components | Zero | No entries for: wechatpay-v3, callback-signature-verify, idempotency, ledger, Postgres, Redis, Celery, webhook, QR-native-pay, mini-program, anti-fraud |
| L3: Operational workflows | Zero | No entries for: merchant-onboarding, voucher-issuance, redemption-verification, audit-trail, dispute-handling, reporting, gov-fund-pool-allocation |

**File:** `decision/service.py` lines 35-49, 76-95
**Proposed change:** Add `CAPABILITY_MAP_L2` and `CAPABILITY_MAP_L3` dicts. Wire them into `_infer_requested_capability_codes()` so the scoring function can match technical components and workflows, not just product modules.

**File:** `recommend/llm.py` lines 375-408 (`build_requirement_profile`)
**Proposed change:** Extend the profile prompt to also output `"l2_components"` and `"l3_workflows"` in the JSON response. These feed back into keyword generation.

---

### 2) Keyword Generation (bilingual, structured, anti-noise)

#### ISSUE A — Flat keyword list, no structured buckets

**File:** `recommend/llm.py` lines 306-372 (`extract_search_queries`)
**Current:** Outputs a flat JSON array of 6-8 strings. No differentiation between implementation keywords, repo discovery queries, and scenario keywords.
**Proposed:** Change the LLM prompt to output a JSON object with three buckets:
```json
{
  "implementation": ["微信支付SDK", "wechat-pay-v3", "idempotent-payment-api", ...],
  "repo_discovery": ["loyalty-points-system", "coupon-management", "merchant-saas-platform", ...],
  "scenario": ["merchant-onboarding", "voucher-redemption", "subsidy-audit-trail", ...]
}
```
Each bucket: 4-6 items. Negative keywords extracted separately.

#### ISSUE B — `_normalize_rewritten_queries` caps at 5 — negates the 8-query LLM output

**File:** `recommend/service.py` line 857
**Current:** `return normalized[:5]` — the normalization step caps LLM output at 5, even though `extract_search_queries` now returns up to 8.
**Proposed:** Raise cap to 10 (or remove it — upstream already caps at 8).

#### ISSUE C — CJK synonym expansion is BYPASSED for LLM-rewritten queries

**File:** `recommend/service.py` lines 887-899
**Current:** When `extract_search_queries()` succeeds, the rewritten queries go directly to providers. The `CJK_SYNONYM_MAP` expansion in `_build_search_queries()` (line 791) is never reached. So if the LLM outputs `"微信支付SDK"` but not `"wechat-pay"`, English-primary providers (GitHub) get a Chinese query that won't match well.
**Proposed:** After LLM rewriting succeeds, run each CJK query through `CJK_SYNONYM_MAP` to generate English companion queries. Merge deduplicated.

#### ISSUE D — Noise filter tech_markers incomplete

**File:** `recommend/llm.py` lines 288-300
**Current tech_markers** (30 items) are missing important markers:
- Missing: `"cloud"`, `"docker"`, `"k8s"`, `"database"`, `"db"`, `"cache"`, `"deploy"`, `"hosting"`, `"container"`, `"lambda"`, `"serverless"`, `"devops"`, `"pipeline"`, `"cluster"`, `"registry"`, `"notification"`, `"push"`, `"sms"`, `"oss"`, `"storage"`, `"cdn"`, `"callback"`, `"webhook"`, `"signature"`
- Example false negative: `"enterprise-cloud"` matches noise term `"enterprise"` → checks for tech markers → `"cloud"` is NOT in markers → **incorrectly filtered!**
**Proposed:** Extend tech_markers with the above terms.

#### ISSUE E — No Negative Keywords

**Current:** No mechanism to exclude irrelevant results at query time or post-filter. If a "payment SaaS" query returns hospital management systems (which also have "payment" features), there is no exclusion signal.
**Proposed:** Add negative keyword extraction to the LLM prompt and a `_NEGATIVE_KEYWORDS` filter in the scoring pipeline. These should include terms from `_INDUSTRY_NOISE_TERMS` plus LLM-extracted domain exclusions.

---

### 3) Retrieval Strategy Plan

#### ISSUE F — `RECOMMEND_PROVIDER_TIMEOUT_SECONDS` default (8s) is too low for Gitee

**File:** `config.py` line 283: `max(1, int(_get("RECOMMEND_PROVIDER_TIMEOUT_SECONDS", "8")))`
**File:** `recommend/service.py` line 1040: `timeout=RECOMMEND_PROVIDER_TIMEOUT_SECONDS`
**Current:** All providers get 8s. Gitee's own default is 15s. GitCode's is 12s. But the config value overrides them. From a US server, Gitee takes 10-15s. **This is why Gitee was timing out in production.**
**Proposed:** Either raise default to 15, or pass per-provider timeouts:
```python
TIMEOUT_OVERRIDES = {"github": 10, "gitee": 18, "gitcode": 14}
```

#### ISSUE G — GitHub.py has no retry logic

**File:** `recommend/github.py` lines 71-101
**Current:** GitHub has zero retries (single attempt). Gitee and GitCode both have `_MAX_RETRIES = 2`. GitHub API returns 403 for rate limits and 5xx for transient errors.
**Proposed:** Add identical retry logic (2 retries, 1s/2s backoff) consistent with gitee.py and gitcode.py.

#### ISSUE H — `build_requirement_profile` is called TWICE (wasted LLM budget)

**File:** `decision/service.py` line 261 (`_infer_hard_intents_from_llm`) — calls `build_requirement_profile()`
**File:** `recommend/service.py` line 969 (`_build_profile`) — calls `build_requirement_profile()` AGAIN with same inputs
**Impact:** 2 LLM round-trips (~2-5s each, ~400 tokens each) with identical inputs. Doubles latency and token cost for every recommendation request.
**Proposed:** Cache the profile result in the decision service and pass it downstream instead of re-computing.

#### ISSUE I — Provider failure coverage labeling is unstructured

**File:** `recommend/service.py` lines 1043-1060
**Current:** Failed providers append a warning string but no structured metadata. The `sources` list only includes successful providers.
**Proposed:** Add `failed_sources: list[str]` to the response model and emit it so the UI can show "Gitee unavailable, showing GitHub+GitCode results only".

#### ISSUE J — GitCode endpoint may need alternative search path

**File:** `recommend/gitcode.py` line 88
**Current:** Uses `/api/v4/projects` with `search` parameter. GitCode sometimes returns HTML (WAF/redirect). The GitLab v4 API may not be the right endpoint for GitCode's current infrastructure.
**Proposed:** Add a fallback search path: try `/api/v4/projects` first; if HTML response, try `/api/v4/search?scope=projects` or the newer `/api/v1/search/repositories`. Make the fallback path configurable via `GITCODE_SEARCH_PATH_FALLBACK` env var.

---

### 4) Scoring & Explanation (transparent)

#### ISSUE K — Scoring weights don't match the SearchSkill v1 rubric

**File:** `recommend/service.py` lines 1160-1177 (final score computation)
**Current weights (LLM path):**
| Signal | Weight |
|--------|--------|
| lexical_score | 0.52 |
| precision_score | 0.18 |
| must_coverage | 0.10 |
| model_score | 0.20 |
| **Maturity/activity** | **0.00** |
| **License/operability** | **0.00** |
| **Documentation** | **0.00** |

**SearchSkill v1 target weights:**
| Signal | Weight |
|--------|--------|
| Relevance to modules | 0.30 |
| Technical match | 0.25 |
| Maturity/activity | 0.15 |
| Integration fit | 0.15 |
| License/operability | 0.10 |
| Documentation/examples | 0.05 |

**Impact:** Archived repos, repos with no license, repos not updated in 3+ years all score identically to active repos. Health card data (`_compute_health_card`) exists but is never factored into ranking.
**Proposed:** Incorporate `activity_score`, `community_score`, `maintenance_score` from `_compute_health_card()` into the blended score. Add license penalty (no license → -5), documentation bonus (has README → +3).

#### ISSUE L — "Why this matched" lacks evidence terms

**File:** `recommend/service.py` lines 1186-1199
**Current:** `match_reasons` includes "命中关键词：X, Y, Z" and "语义覆盖：支付系统, 积分管理". But it does NOT include:
- Which bucket (implementation/repo_discovery/scenario) each hit came from
- What the original requirement module was that this hit satisfies
- Confidence level per hit
**Proposed:** Enrich `match_reasons` with `"技术匹配：wechat-pay-v3 → 命中 repo topics"`, `"场景覆盖：merchant-onboarding → 匹配 README 关键词"`.

#### ISSUE M — Semantic group guardrails only hard-gate "wechat" and "crawl"

**File:** `recommend/service.py` lines 1232-1233
**Current:** `hard_groups = [group for group in must_groups if group in {"wechat", "crawl"}]`
**Impact:** For payment/points/coupon queries, NO groups are "hard", so the guardrail effectively does nothing. A candidate matching 0 out of 8 relevant groups can still pass if its `match_score >= 12`.
**Proposed:** Make hard groups configurable or dynamic. If a query hits ≥ 4 semantic groups, require at least 2 group hits (instead of current behavior where hard_groups is empty → no enforcement).

---

### 5) Acceptance Tests

#### Test Case 1: Payment + Points + Coupons + Audit + Subsidy Pool

**Input requirement text:**
```
系统开发：开发扫码支付与积分管理SaaS系统
功能模块：
1. 微信扫码支付（Native支付、JSAPI支付）
2. 积分获取与消耗（消费积分、签到积分、活动积分）
3. 卡券定向发放与核销（优惠券、代金券、折扣券）
4. 商户端管理后台（商户入驻、订单管理、对账报表）
5. 政府补贴资金池对接（资金划拨、使用审计、合规报告）
6. 多方分账与清分（平台-商户-政府三方分账）
技术要求：微信支付V3 API、Redis缓存、PostgreSQL、幂等支付、回调签名验证
```

**Expected Bucket A (Implementation Keywords):**
- `微信支付SDK` / `wechat-pay-v3`
- `idempotent-payment` / `幂等支付接口`
- `callback-signature-verify` / `回调签名验证`
- `loyalty-points-engine` / `积分引擎`
- `coupon-management-api` / `卡券核销接口`
- `ledger-settlement` / `分账清分`
- `subsidy-fund-pool` / `补贴资金池`

**Expected Bucket B (Repo Discovery Queries):**
- `wechat-pay` / `微信支付SDK`
- `loyalty-points-system`
- `coupon-management`
- `merchant-saas-platform`
- `multi-tenant-saas`

**Expected Bucket C (Scenario Keywords):**
- `merchant-onboarding` / `商户入驻`
- `voucher-redemption` / `卡券核销`
- `subsidy-audit-trail` / `补贴审计`
- `reconciliation-report` / `对账报表`
- `three-party-settlement` / `三方分账`

**Why generics are excluded:**
- `政府` → filtered (pure role noun, no tech marker)
- `居民` → filtered (pure role noun)
- `社区` → filtered (pure role noun)
- `企业` → filtered (pure role noun)
- `政府补贴资金池管理系统` → KEPT (contains `管理` + `系统` tech markers)
- `商户端管理后台` → KEPT (contains `管理` + `端` tech markers)

#### Test Case 2: Community Digital Management Platform

**Input:**
```
建设数字化社区综合管理平台，整合物业、餐饮、商超、文旅、农产品等商户，
支持居民积分获取场景（消费、回收、志愿服务），提供积分兑换商品功能，
支持社区公告、活动报名、投诉建议等居民互动功能。
```

**Expected Bucket A:** `community-management-platform`, `property-management-system`, `points-exchange-engine`, `merchant-integration-api`
**Expected Bucket B:** `digital-community-platform`, `社区管理系统`, `积分兑换系统`, `物业管理平台`
**Expected Bucket C:** `resident-points-exchange`, `community-bulletin`, `activity-registration`, `complaint-feedback`

**Why generics are excluded:** `社区` alone → filtered. `居民` alone → filtered. `物业` alone → filtered. But `社区管理系统` → KEPT (has `管理` + `系统`).

#### Test Case 3: B2B Supply Chain + Inventory

**Input:**
```
开发B2B供应链管理系统，实现采购订单管理、供应商评估、仓储WMS、
库存SKU管理、物流跟踪、财务应付账款对接。技术栈：Java Spring Boot + Vue3。
```

**Expected Bucket A:** `supply-chain-management`, `wms-warehouse`, `inventory-sku-management`, `procurement-order-api`, `accounts-payable-integration`
**Expected Bucket B:** `spring-boot-erp`, `供应链管理系统`, `仓储WMS`, `B2B-procurement`
**Expected Bucket C:** `supplier-evaluation`, `purchase-order-approval`, `logistics-tracking`, `inventory-reconciliation`

**Why generics are excluded:** `企业` alone → filtered. `客户` alone → filtered. But `供应商评估系统` → KEPT.

#### Verification Commands

```bash
# 1. Compile check (no secrets needed)
python3 -m py_compile recommend/llm.py
python3 -m py_compile recommend/service.py
python3 -m py_compile decision/service.py
python3 -m py_compile recommend/gitcode.py
python3 -m py_compile recommend/gitee.py
python3 -m py_compile recommend/github.py

# 2. Unit test — _clean_query_term noise filtering (can run locally)
python3 -c "
from recommend.llm import _clean_query_term
# Pure noise terms must be filtered
assert _clean_query_term('社区') == '', f'社区 should be filtered, got: {repr(_clean_query_term(\"社区\"))}'
assert _clean_query_term('政府') == '', f'政府 should be filtered'
assert _clean_query_term('居民') == '', f'居民 should be filtered'
assert _clean_query_term('community') == '', f'community should be filtered'
assert _clean_query_term('government') == '', f'government should be filtered'
# Compound technical phrases must be kept
assert _clean_query_term('社区管理系统') != '', '社区管理系统 should be kept'
assert _clean_query_term('community-management-platform') != '', 'compound should be kept'
assert _clean_query_term('政府补贴资金池管理系统') != '', 'compound should be kept'
assert _clean_query_term('merchant-saas-platform') != '', 'compound should be kept'
assert _clean_query_term('微信支付SDK') != '', 'technical term should be kept'
assert _clean_query_term('wechat-pay-v3') != '', 'technical term should be kept'
assert _clean_query_term('loyalty-points-system') != '', 'technical term should be kept'
# Edge case: tech marker saves noise substring
assert _clean_query_term('enterprise-cloud') == '', f'currently filtered — ISSUE D: cloud not in markers'
print('All _clean_query_term tests passed (except known ISSUE D)')
"

# 3. CJK synonym expansion coverage check
python3 -c "
from recommend.service import CJK_SYNONYM_MAP
required = ['支付', '积分', '卡券', '核销', '审计', '商户', '补贴', '分账', '社区', '物业', '扫码']
missing = [k for k in required if k not in CJK_SYNONYM_MAP]
print(f'CJK_SYNONYM_MAP coverage: {len(required) - len(missing)}/{len(required)}')
if missing:
    print(f'MISSING: {missing}')
else:
    print('All required CJK synonyms present.')
"

# 4. Semantic group coverage check
python3 -c "
from recommend.service import SEMANTIC_GROUPS
required_groups = ['payment', 'points', 'coupon', 'merchant', 'audit', 'subsidy', 'saas']
missing = [g for g in required_groups if g not in SEMANTIC_GROUPS]
print(f'SEMANTIC_GROUPS coverage: {len(required_groups) - len(missing)}/{len(required_groups)}')
if missing:
    print(f'MISSING groups: {missing}')
else:
    print('All required semantic groups present.')
"

# 5. Config value check (no secrets, just env var names)
python3 -c "
from config import (
    RECOMMEND_PROVIDER_TIMEOUT_SECONDS,
    RECOMMEND_ENABLE_GITEE,
    RECOMMEND_ENABLE_GITCODE,
    RECOMMEND_EXTERNAL_SOURCES_ENABLED,
    RECOMMEND_GITHUB_MAX_RESULTS,
)
print(f'RECOMMEND_PROVIDER_TIMEOUT_SECONDS = {RECOMMEND_PROVIDER_TIMEOUT_SECONDS}')
print(f'RECOMMEND_ENABLE_GITEE = {RECOMMEND_ENABLE_GITEE}')
print(f'RECOMMEND_ENABLE_GITCODE = {RECOMMEND_ENABLE_GITCODE}')
print(f'RECOMMEND_EXTERNAL_SOURCES_ENABLED = {RECOMMEND_EXTERNAL_SOURCES_ENABLED}')
print(f'RECOMMEND_GITHUB_MAX_RESULTS = {RECOMMEND_GITHUB_MAX_RESULTS}')
if RECOMMEND_PROVIDER_TIMEOUT_SECONDS < 12:
    print('WARNING: timeout too low for Gitee from US server (needs 15+)')
"

# 6. Server-side integration test (requires Docker running, no secrets exposed)
# Run from project root with Docker compose up:
# curl -s -X POST http://localhost:8000/api/recommend \
#   -H 'Content-Type: application/json' \
#   -d '{"query":"扫码支付与积分管理SaaS","mode":"deep","limit":10}' \
#   | python3 -m json.tool | head -80
```

---

## Patch Summary

**N/A** — audit-only round. No code changes applied.

**Summary of proposed changes across 17 issues (7 files):**

| File | Issues | Lines affected (est.) |
|------|--------|----------------------|
| `recommend/llm.py` | A, D, E | ~80 lines (prompt rewrite, tech_markers, negative keywords) |
| `recommend/service.py` | B, C, K, L, M | ~60 lines (cap fix, synonym expansion, scoring weights, evidence) |
| `decision/service.py` | H, Capability Map | ~40 lines (profile caching, L2/L3 maps) |
| `recommend/github.py` | G | ~20 lines (retry logic) |
| `recommend/gitcode.py` | J | ~15 lines (fallback search path) |
| `config.py` | F | ~5 lines (timeout defaults) |
| `recommend/models.py` | I | ~3 lines (failed_sources field) |

---

## Validation Evidence

| Check | Command | Expected | Actual (current) |
|-------|---------|----------|------------------|
| Compile all | `python3 -m py_compile recommend/llm.py` | exit 0 | exit 0 |
| Noise filter: `社区` | `_clean_query_term('社区')` | `""` | `""` (fixed in previous commit) |
| Noise filter: `政府` | `_clean_query_term('政府')` | `""` | `""` (fixed) |
| Compound kept: `社区管理系统` | `_clean_query_term('社区管理系统')` | `"社区管理系统"` | `"社区管理系统"` (has `管理`+`系统` markers) |
| Compound kept: `wechat-pay-v3` | `_clean_query_term('wechat-pay-v3')` | `"wechat-pay-v3"` | `"wechat-pay-v3"` (no noise substring) |
| ISSUE D false negative: `enterprise-cloud` | `_clean_query_term('enterprise-cloud')` | Should keep | `""` — BLOCKED, `cloud` not in markers |
| Rewrite cap | `_normalize_rewritten_queries` | ≥8 | Max 5 — ISSUE B |
| Provider timeout | `RECOMMEND_PROVIDER_TIMEOUT_SECONDS` | ≥15 for Gitee | 8 — ISSUE F |
| CJK synonym coverage | Check `CJK_SYNONYM_MAP` | 11/11 required keys | 11/11 present |
| Semantic groups | Check `SEMANTIC_GROUPS` | 7/7 payment domain | 7/7 present |

---

## Risks / Blast Radius

| Risk | Severity | Mitigation |
|------|----------|------------|
| Prompt change (Issue A) may produce LLM parse failures if model outputs unexpected JSON structure | Medium | Fallback to flat array parsing if structured parsing fails |
| Raising timeout (Issue F) increases worst-case latency from 8s to 15-18s per request | Low | Per-provider timeouts limit blast radius; parallel execution means wall-clock impact ≤ max(timeouts) |
| Scoring weight changes (Issue K) will reshuffle ALL result rankings | Medium | A/B test with flag; keep old weights as fallback |
| CJK synonym expansion on LLM queries (Issue C) may produce duplicate/overlapping searches | Low | Dedup already in place (`_dedupe_keep_order`) |
| Profile caching (Issue H) — stale cache if requirement_text differs between calls | Low | Cache key must include hash of requirement_text |
| Removing rewrite cap (Issue B) from 5→10 increases provider API call volume | Low | Already capped at 8 upstream; 12 max workers already configured |

**Auth/payment/multi-tenant: NOT TOUCHED.** All changes are confined to the recommend/ and decision/ scoring pipeline.

---

## Rollback Steps

All proposed changes are in 7 files within `recommend/` and `decision/`. Rollback:

```bash
# Revert to current commit (pre-patch)
git log --oneline -3  # note current HEAD
# If patch is applied and needs revert:
git revert HEAD       # single commit revert
# Or hard rollback:
git reset --hard f36bf3a  # current HEAD hash
```

No database migrations. No env var removals needed (only additions/defaults). No Docker image changes.

---

## Merge Readiness

**NOT READY** — this is audit-only. Patch implementation is the next step pending user approval.

**Prerequisites before "apply patch":**
1. Confirm env vars are set (yes/no, no values): `RECOMMEND_PROVIDER_TIMEOUT_SECONDS`, `RECOMMEND_ENABLE_GITEE`, `RECOMMEND_ENABLE_GITCODE`, `GITEE_TOKEN`, `GITCODE_TOKEN`
2. Confirm LLM provider is working: is `LLM_PROVIDER` env var set and reachable?
3. Decide priority order for the 17 issues (recommend: F → B → C → A → D → G → K → H)

---

## Next Recommended Round

**L8 — SearchSkill v2: Implementation Patch**
- Apply the top 8 issues (F, B, C, A, D, G, K, H) as minimal diffs
- Run acceptance tests 1-3 against live server
- Measure: latency before/after, Gitee success rate, keyword quality (manual review of 3 test cases)

**L9 — Scoring Transparency & UI Evidence**
- Wire `match_reasons` evidence terms into the frontend recommendation cards
- Add `failed_sources` display to the UI
- Add "Why this matched" expandable section per result
