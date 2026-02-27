# WechatPay Launch Runbook

AntiHub 微信支付上线运维手册。

> **安全提醒**: 本文档仅包含环境变量名和文件路径，不包含任何真实密钥或证书内容。

---

## 1. 环境变量配置核对

将以下变量填入 `.env.prod`（从 `.env.prod.example` 复制后修改）。

### 必填项（缺失任何一项将导致启动失败或支付不可用）

| 变量名 | 说明 | 示例值 |
|--------|------|--------|
| `PAYMENT_PROVIDER` | 支付提供商 | `wechatpay` |
| `PAYMENT_WEBHOOK_SECRET` | 内部 webhook 签名密钥（32 位随机 hex） | `openssl rand -hex 32` |
| `WECHATPAY_NOTIFY_URL` | 微信支付回调地址 | `https://zenplat.top/api/billing/webhooks/wechatpay` |
| `WECHATPAY_MCHID` | 商户号 | 微信商户平台获取 |
| `WECHATPAY_APPID` | 应用 ID | 微信商户平台获取 |
| `WECHATPAY_CERT_SERIAL` | 商户 API 证书序列号 | 微信商户平台获取 |
| `WECHATPAY_PRIVATE_KEY_PATH` | 商户私钥文件路径（容器内） | `/app/certs/apiclient_key.pem` |
| `WECHATPAY_APIV3_KEY` | APIv3 密钥（32 字节） | 微信商户平台设置 |
| `WECHATPAY_PLATFORM_CERT_SERIAL` | 微信平台证书序列号 | 微信商户平台获取 |
| `WECHATPAY_PLATFORM_CERT_PATH` | 微信平台证书文件路径（容器内） | `/app/certs/wechatpay_platform.pem` |

### 可选项（仅当不使用文件挂载时）

| 变量名 | 说明 |
|--------|------|
| `WECHATPAY_PRIVATE_KEY_PEM` | 商户私钥内联 PEM（替代 `_PATH`） |
| `WECHATPAY_PLATFORM_CERT_PEM` | 平台证书内联 PEM（替代 `_PATH`） |
| `WECHATPAY_PLATFORM_CERTS_JSON` | 平台证书 JSON map（替代单证书） |

### 已有默认值（通常无需修改）

| 变量名 | 默认值 |
|--------|--------|
| `WECHATPAY_API_BASE_URL` | `https://api.mch.weixin.qq.com` |

---

## 2. 证书文件部署

在服务器（宿主机）项目根目录创建 `certs/` 目录：

```bash
mkdir -p certs
chmod 700 certs
```

将以下文件放入 `certs/`：

| 文件 | 来源 | 容器内路径 |
|------|------|-----------|
| `apiclient_key.pem` | 微信商户平台下载 | `/app/certs/apiclient_key.pem` |
| `wechatpay_platform.pem` | 微信商户平台获取或通过 API 下载 | `/app/certs/wechatpay_platform.pem` |

```bash
chmod 600 certs/*
```

`docker-compose.prod.yml` 中 api 服务已配置 `./certs:/app/certs:ro` 只读挂载。

> **注意**: `certs/` 目录已在 `.gitignore` 中，不会被提交到 git。

---

## 3. Cloudflared Tunnel 路由确认

上线路由链路：

```
微信支付服务器
  → POST https://zenplat.top/api/billing/webhooks/wechatpay
  → cloudflared tunnel (zenplat.top → frontend:80)
  → nginx location /api/ → proxy_pass http://api:8000/
  → FastAPI POST /billing/webhooks/wechatpay
```

### 确认清单

- [ ] Cloudflare Dashboard 中 tunnel 的 `zenplat.top` service 指向 `http://frontend:80`
- [ ] nginx `/api/` 代理已配置（`frontend/nginx.prod.conf` 默认已包含）
- [ ] Cloudflare WAF 规则未拦截 POST 请求到 `/api/billing/webhooks/wechatpay`
- [ ] Cloudflare 未对 webhook 请求体启用验证/转换（微信签名基于原始 body）

---

## 4. Health Check 验证

部署后执行：

```bash
# 从容器内部
docker compose -f docker-compose.prod.yml exec api curl -s http://127.0.0.1:8000/health/billing | python3 -m json.tool

# 预期输出
{
    "status": "ok",
    "config": "ok",
    "details": {}
}
```

如果返回 `"status": "error"`，检查 `details` 字段了解缺失的配置项。

---

## 5. Webhook 可达性验证

从外部测试 webhook 端点是否可达：

```bash
# 无签名请求，预期 403（签名验证失败）或 503（配置缺失）
curl -X POST https://zenplat.top/api/billing/webhooks/wechatpay \
  -H "Content-Type: application/json" \
  -d '{"test": true}' \
  -w "\nHTTP_STATUS: %{http_code}\n"

# 预期: HTTP 403 或 503（不应返回 404 或 502）
```

如果返回 `404`：检查 nginx 路由配置和 tunnel 转发规则。
如果返回 `502`：检查 api 容器是否启动成功。

---

## 6. 首次小额支付测试（E2E）

### 6.1 创建测试套餐

管理员在管理页（`/admin/billing`）创建一个 1 分钱测试套餐：
- `price_cents`: `1`
- `currency`: `CNY`
- `monthly_points`: `1`
- `code`: `test_1fen`（可自定义）

### 6.2 发起支付

1. 使用普通用户账号访问 `/billing`
2. 找到测试套餐，点击购买
3. 确认弹出微信扫码支付弹窗
4. 确认二维码可正常扫描（微信识别为收款码）

### 6.3 完成支付

1. 微信扫码支付 0.01 元
2. 观察弹窗状态：
   - 自动检测应在 2 秒内开始轮询
   - 支付成功后弹窗显示 "支付成功" 并自动关闭

### 6.4 验证到账

- [ ] 订单状态从 `PENDING` 变为 `PAID`（管理页 → 订单 tab）
- [ ] 积分已到账（工作台 → 积分余额）
- [ ] 审计日志记录正常（管理页 → 审计 tab）
  - 应有 `wechatpay.notify` 事件
  - `signature_valid` = `true`
  - `outcome` = `processed` 或 `ok`

### 6.5 验证后清理

管理员可在管理页将测试套餐设为 inactive 或将 `price_cents` 改回正常价格。

---

## 7. 异常路径验证

| 场景 | 操作 | 预期结果 |
|------|------|----------|
| 二维码过期 | 不扫码等待 10 分钟 | 前端提示"检测超时"，可手动校验 |
| 重复 checkout | 同一套餐连续点击购买 | 返回缓存的 `code_url`，不重复创建订单 |
| Dev-simulate 端点 | `POST /billing/dev/simulate-payment` | 返回 `404 not found`（生产环境已禁用） |
| Stripe 误配 | 设置 `PAYMENT_PROVIDER=stripe` 启动 | 进程启动时立即崩溃并报错 |
| 缺少证书启动 | 删除 `WECHATPAY_PRIVATE_KEY_PATH` 启动 | Health check 返回 `status=error` |

---

## 8. 回滚步骤

如果上线后支付功能出现严重问题：

```bash
# 1. 切回模拟模式（立即停止真实支付）
# 编辑 .env.prod，将 PAYMENT_PROVIDER 改为 mock
sed -i 's/PAYMENT_PROVIDER=wechatpay/PAYMENT_PROVIDER=mock/' .env.prod

# 2. 重启 api 服务
docker compose -f docker-compose.prod.yml restart api

# 3. 验证
docker compose -f docker-compose.prod.yml exec api curl -s http://127.0.0.1:8000/health/billing
```

切回 `mock` 模式后：
- 已完成的支付和积分不受影响
- 新的 checkout 将返回模拟 QR 码（不可真实支付）
- `PENDING` 状态的订单不会自动取消（需管理员手动处理或等待过期）

---

## 9. 常见问题排查

| 问题 | 可能原因 | 排查步骤 |
|------|---------|---------|
| Checkout 返回 502 | 私钥/证书序列号不匹配 | 检查 `WECHATPAY_CERT_SERIAL` 是否与 `apiclient_key.pem` 对应 |
| Webhook 返回 403 | 平台证书不匹配或过期 | 检查 `WECHATPAY_PLATFORM_CERT_SERIAL` 和证书文件 |
| Webhook 返回 400 (decrypt failed) | `WECHATPAY_APIV3_KEY` 不正确 | 在微信商户平台重新设置 APIv3 密钥 |
| 支付成功但积分未到账 | Webhook 未到达后端 | 检查审计日志是否有 `wechatpay.notify` 事件 |
| 二维码无法扫描 | `code_url` 格式错误 | 检查 `WECHATPAY_APPID` 是否正确 |
