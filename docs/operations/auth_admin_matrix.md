# Auth/Admin 配置矩阵（Local vs Production）

## 1) AuthN vs AuthZ 边界
- `AuthN（认证）`：确认“你是谁”，由 `auth_middleware` + JWT (`AUTH_TOKEN_SECRET`) 完成。
- `AuthZ（授权）`：确认“你能做什么”，由依赖守卫完成：
  - `require_admin`：仅 `admin/root` 通过
  - `require_root`：仅 `root` 通过
  - `require_saas_admin`：`admin/root` 且 `FEATURE_SAAS_ADMIN_API=true`

说明：
- 本轮将 root-only 路由（租户创建/更新）统一为 `require_root`。
- `/admin/saas/*` 统一使用 `require_saas_admin`，避免散落式 ad-hoc 检查。

## 2) 环境变量矩阵

| 变量 | Local 推荐 | Production 推荐 | 影响 |
|---|---|---|---|
| `AUTH_ENABLED` | `true` | `true` | 认证总开关；关闭后大多数接口不需要登录（仅用于极少数本地调试） |
| `AUTH_TOKEN_SECRET` | 强随机值（本地专用） | 强随机值（必须） | JWT 签发/校验密钥；缺失时登录不可用 |
| `AUTH_TOKEN_TTL_SECONDS` | `3600`~`43200` | 按安全策略设置 | Token 过期时间 |
| `AUTH_USERS_JSON` | 可选（仅本地快速登录） | 建议留空 | 兼容旧登录源；生产建议走 DB 用户 |
| `ROOT_ADMIN_USERNAME` | `root` | 固定并受控 | 启动 bootstrap root 用户名 |
| `ROOT_ADMIN_PASSWORD` / `ROOT_ADMIN_PASSWORD_HASH` | 本地临时值 | 仅密文/密钥管理系统注入 | root 初始化凭据 |
| `STARTUP_BOOTSTRAP_ENABLED` | `true`（本地） | 视部署策略；常见为 `false` + init 脚本 | 是否在应用启动时执行 bootstrap |
| `FEATURE_SAAS_ADMIN_API` | 演示时 `true` | 按运营策略开启 | 控制 `/admin/saas/*` |
| `FEATURE_SAAS_ENTITLEMENTS` | 演示时 `true` | 按运营策略开启 | 控制 entitlement 用户侧接口 |

## 3) 最小行为预期
- 普通用户访问 `/admin/*`：`403`
- `admin` 访问 `/admin/*`：允许（受租户边界限制）
- 非 `root` 的 `admin` 访问 root-only 路由（如租户创建/更新）：`403`
- `root` 访问 root-only 路由：`200`

## 4) 线上核对清单
1. 确认 `AUTH_ENABLED=true`
2. 确认 `AUTH_TOKEN_SECRET` 非空且已轮换
3. 确认 root/admin 账号状态和角色正确（`/auth/me` + `/auth/permissions/me`）
4. 若启用 SaaS 管理端，确认 `FEATURE_SAAS_ADMIN_API=true`
5. 用三种角色做一次 smoke：
   - user -> `/admin/billing/orders` 预期 `403`
   - admin -> `/admin/billing/orders` 预期 `200`
   - root -> `/admin/tenants` `POST` 预期 `200`
