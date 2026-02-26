# Round 5 Tenant Membership Management API（最小版）

## 1) 目标与范围
- 本轮仅新增最小租户成员管理 API（兼容优先）。
- 不改 auth token/session 模型。
- 不改 billing/payment 业务逻辑。

## 2) Feature Flag
- 开关：`FEATURE_MULTI_TENANT_FOUNDATION`
- 默认：`false`
- 关闭时：以下 API 返回 `404 feature disabled`（受控隐藏，不影响既有流程）。

## 3) AuthZ 矩阵（本轮最窄策略）
- `root`：允许管理任意 tenant 的 membership / setting。
- `admin`：本轮不开放（返回 403），避免引入新的 RBAC 语义漂移。
- `user`：不允许（403）。

## 4) API 列表

### 4.1 Tenant Membership
- `GET /admin/tenants/{tenant_id}/members`
- `PUT /admin/tenants/{tenant_id}/members/{username}`
- `DELETE /admin/tenants/{tenant_id}/members/{username}`

说明：
- `PUT` 为 upsert（可创建或更新成员记录）。
- `DELETE` 为软删除语义（`active=false`），不物理删行。

### 4.2 Tenant Settings（最小子集）
- `GET /admin/tenants/{tenant_id}/settings/{key}`
- `PUT /admin/tenants/{tenant_id}/settings/{key}`

说明：
- key 规范：`^[a-z][a-z0-9_.:-]{0,63}$`

## 5) 示例
```bash
# root token
ROOT_TOKEN="<root-token>"
TENANT_ID="<tenant-id>"

# upsert member
curl -s -X PUT "http://127.0.0.1:8010/admin/tenants/${TENANT_ID}/members/alice" \
  -H "Authorization: Bearer ${ROOT_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"role":"member","active":true,"is_default":false}'

# list members
curl -s "http://127.0.0.1:8010/admin/tenants/${TENANT_ID}/members?include_inactive=true" \
  -H "Authorization: Bearer ${ROOT_TOKEN}"

# deactivate member
curl -s -X DELETE "http://127.0.0.1:8010/admin/tenants/${TENANT_ID}/members/alice" \
  -H "Authorization: Bearer ${ROOT_TOKEN}"

# upsert tenant setting
curl -s -X PUT "http://127.0.0.1:8010/admin/tenants/${TENANT_ID}/settings/feature.deep_search" \
  -H "Authorization: Bearer ${ROOT_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"value":{"enabled":true,"rpm":120},"metadata":{"source":"ops"}}'
```

## 6) 回滚
- 本轮无新增 migration。
- 代码回滚：回退本轮提交即可。
- 如果必须临时停用：设置 `FEATURE_MULTI_TENANT_FOUNDATION=false` 并重启 API。

## 7) 下一轮建议（不在本轮实现）
- tenant-admin 在同租户内管理 membership 的受限授权模型（明确可管理角色与边界）
- membership 审计日志完善（谁改了什么）
