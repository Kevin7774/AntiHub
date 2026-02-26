# SaaS Entitlements 使用说明

## 1. 命名约定
建议使用 `域.能力` 或 `域:能力` 风格，全小写：
- `feature.deep_search`
- `deploy.one_click`
- `api.rpm`

建议规则：
- 前缀体现业务域（`feature/deploy/api/ai`）
- key 稳定，不要把价格、套餐名编码进 key
- 具体数值放到 `value` / `limit`

## 2. 默认策略（向后兼容）
- `FEATURE_SAAS_ENTITLEMENTS=false`（默认）
  - `/billing/entitlements/me` 与 entitlement 检查接口关闭
  - 线上现有路径保持原行为
- `FEATURE_SAAS_ADMIN_API=false`（默认）
  - `/admin/saas/*` 全部关闭
  - 现有 `/admin/billing/*` 不受影响

## 3. 如何新增一个 Entitlement

### 3.1 打开功能开关
```bash
export FEATURE_SAAS_ENTITLEMENTS=true
export FEATURE_SAAS_ADMIN_API=true
```

### 3.2 创建/更新套餐（SaaS Admin API）
```bash
curl -s -X POST http://127.0.0.1:8010/admin/saas/plans \
  -H "Authorization: Bearer <ADMIN_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "code":"pro_saas",
    "name":"Pro SaaS",
    "currency":"usd",
    "price_cents":19900,
    "monthly_points":3000,
    "billing_cycle":"monthly",
    "trial_days":7,
    "metadata":{"segment":"self-serve"},
    "active":true
  }'
```

### 3.3 给套餐挂权益
```bash
curl -s -X POST http://127.0.0.1:8010/admin/saas/plans/<PLAN_ID>/entitlements \
  -H "Authorization: Bearer <ADMIN_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "key":"feature.deep_search",
    "enabled":true,
    "value":{"mode":"deep"},
    "limit":100,
    "metadata":{"unit":"req/day"}
  }'
```

## 4. 如何给用户绑定套餐
```bash
curl -s -X POST http://127.0.0.1:8010/admin/saas/users/alice/plan \
  -H "Authorization: Bearer <ADMIN_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "plan_code":"pro_saas",
    "duration_days":30,
    "auto_renew":false
  }'
```

## 5. 用户侧验证

### 5.1 查看当前权益
```bash
curl -s http://127.0.0.1:8010/billing/entitlements/me \
  -H "Authorization: Bearer <USER_TOKEN>"
```

### 5.2 验证 require_entitlement 保护路径
```bash
curl -s -i http://127.0.0.1:8010/billing/entitlements/check/deep-search \
  -H "Authorization: Bearer <USER_TOKEN>"
```
- 有 `feature.deep_search` 且 `enabled=true`：返回 200
- 无该权益：返回 403

## 6. 迁移/初始化
- Alembic：`python -m alembic upgrade head`
- 生产初始化脚本：`python scripts/init_prod_db.py`
- 若 Alembic 异常，脚本会 fallback `create_all`，并做最小 schema reconcile，避免启动中断。
