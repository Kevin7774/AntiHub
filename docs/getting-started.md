# 快速上手

本页提供"从零到可用"的最短路径。

## WSL 最小检查清单
```bash
python3 --version
.venv/bin/python -V
node -v && npm -v
bash scripts/local_baseline_check.sh
```

## 前置条件（完整模式）
- Python 3.10/3.11（CI 口径）；本地 3.12 可用但以 CI 为准
- Docker 已启动（`docker info` 正常）
- Redis 可用（默认 `redis://localhost:6379/0`）

## 3 分钟启动（完整模式：有 Docker）
1) 启动后台依赖（Redis + Openclaw）
```bash
./scripts/dev_services.sh up
```

2) 启动 API
```bash
source .venv/bin/activate
.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8010 --reload
```

3) 创建一个 case
```bash
curl -s -X POST http://localhost:8010/cases \
  -H "Content-Type: application/json" \
  -d '{"repo_url":"/path/to/your/repo","ref":"main"}'
```

4) 查看状态与日志
```bash
curl -s http://localhost:8010/cases/{case_id}
# WS 日志：wscat -c ws://localhost:8010/ws/logs/{case_id}
```

## 最小本地模式（无 Docker）
适用场景：WSL 暂未启用 Docker Desktop Integration，仅做 API/Auth/Admin/Billing 基线验证。

```bash
source .venv/bin/activate
.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8010 --reload
```

本模式常见现象（预期）：
- `GET /health` 可能为 `status=degraded`（例如 docker/openclaw 不可用）。
- 未配置 `PAYMENT_WEBHOOK_SECRET` 时，`GET /health/billing` 可能出现 `config=error`。

## 常用命令
- 停止后台服务：`./scripts/dev_services.sh down`
- 运行 pytest：`./scripts/test.sh`（或手动：`.venv/bin/python -m pytest -q`）
