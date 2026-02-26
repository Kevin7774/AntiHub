# Round 4 多租户基础脚手架（兼容优先）

## 1. 范围与开关
- 本轮仅提供多租户基础模型与请求上下文解析能力。
- 功能开关：`FEATURE_MULTI_TENANT_FOUNDATION`。
- 默认值：`false`（关闭）。
- 开关关闭时，系统保持现有单租户行为，不改变现有 billing/payment 路径。

## 2. 数据模型（最小集合）
新增表：
- `auth_tenant_members`
  - 用于 `user <-> tenant` 多对多成员关系
  - 关键字段：`tenant_id`, `username`, `role`, `active`, `is_default`
  - 关键约束：`(tenant_id, username)` 唯一
- `auth_tenant_settings`
  - 用于 tenant 级别最小配置（键值）
  - 关键字段：`tenant_id`, `key`, `value`
  - 关键约束：`(tenant_id, key)` 唯一

## 3. 租户上下文解析策略
- 采用请求头：`X-Tenant-ID`。
- 解析规则：
  - 开关关闭：忽略 header，沿用 `auth_users.tenant_id`（兼容路径）。
  - 开关开启 + 不传 header：沿用兼容路径。
  - 开关开启 + 传 header：
    - root：可切换到任意 active tenant。
    - 非 root：必须在 `auth_tenant_members` 中有 active 关系。

## 4. 兼容策略
- 现有单租户用户无需迁移即可运行。
- 现有接口仍可按原 tenant 逻辑处理。
- 本轮不改 JWT/session 模型，不改 billing/payment 业务语义。

## 5. 迁移与回滚
- 升级：
  ```bash
  .venv/bin/python -m alembic upgrade head
  ```
- 回滚到上一版：
  ```bash
  .venv/bin/python -m alembic downgrade 20260225_0004
  ```

## 6. 本地验证建议
- 开关关闭兼容性 + tenant context 用例：
  ```bash
  .venv/bin/python -m pytest -q tests/test_multi_tenant_foundation.py tests/test_iam_rbac_abac.py
  ```

## 7. 明确保留到后续轮次（本轮不做）
- 企业级 RBAC/ABAC 细化策略
- billing/payment 多租户隔离语义重构
- 前端租户切换 UI 与管理台全面改造
- 生产部署流程改造（本轮仅文档说明）
