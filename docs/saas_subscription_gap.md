# SaaS 订阅能力差距盘点（第一轮）

## 1. 当前已具备能力（仓库现状）
- 基础计费模型：`Plan / Order / Subscription / PointFlow / PointAccount / BillingAuditLog`。
- 基础计费流程：
  - `GET /billing/plans`
  - `POST /billing/checkout`
  - `POST /billing/webhooks/payment`
  - `POST /billing/webhooks/wechatpay`
- 会员状态能力：
  - `GET /billing/subscription/me`
  - `GET /billing/points/me`
  - `GET /billing/points/history/me`
- 现有管理接口（非 SaaS Flag 控制）：
  - `POST /admin/billing/plans`
  - `PUT /admin/billing/plans/{plan_id}`
  - 订单/审计查询接口
- 权限体系：已有登录态与 `admin/root` 角色鉴权。

## 2. 缺失能力分级（实施前）

### P0（必须先补基础设施）
- 套餐字段扩展不足：缺少 `billing_cycle / trial_days / metadata`。
- 套餐权益未建模：权益仍无法通过 DB 配置化表达。
- 权益读取与拦截缺失：没有统一 `get_user_entitlements` 与 `require_entitlement`。
- Feature Flag 缺失：无法“默认关闭，不影响线上”。
- 面向 SaaS 的 Admin API 缺失：
  - Plan 的 SaaS 管理视图
  - PlanEntitlement CRUD
  - 用户绑定套餐（最小可用）
- Alembic 基础配置缺失（`alembic.ini`），导致迁移链路不稳定。

### P1（下一阶段）
- 支付网关深度对接治理（多通道对账、失败补偿）。
- 订阅生命周期增强（升级/降级/proration、暂停/恢复）。
- 权益变更审计与运营后台检索能力。
- 配额/限流统一接入 entitlement（当前只提供基础拦截能力）。

### P2（后续商业化完善）
- 优惠券/促销活动。
- 发票/税务（Invoice + Tax）。
- Dunning（失败扣款追缴）与自动催收。
- 完整 webhook 幂等治理与对账报表。

## 3. 本 PR 第一轮范围说明
本轮只实现 **SaaS 订阅基础设施**：
- Plans 扩展字段（`billing_cycle / trial_days / metadata`）
- PlanEntitlements 模型与 CRUD
- Entitlements 服务（读取、缓存、失效、依赖）
- Feature Flags（默认关闭）
- SaaS Admin API（Flag 控制）
- 文档与关键测试

以下能力明确不在本轮实现：
- 支付深度对接、优惠券、dunning、发票、税务、复杂升级/降级账务处理。
