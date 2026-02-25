# 快速上手

本页提供“从零到可用”的最短路径。

## 前置条件
- Python 3.10/3.11（CI 口径）；本地 3.12 可用但以 CI 为准
- Docker 已启动（`docker info` 正常）
- Redis 可用（默认 `redis://localhost:6379/0`）

## 3 分钟启动
1) 启动服务
```bash
./dev.sh up
```

2) 创建一个 case
```bash
curl -s -X POST http://localhost:8010/cases \
  -H "Content-Type: application/json" \
  -d '{"repo_url":"/path/to/your/repo","ref":"main"}'
```

3) 查看状态与日志
```bash
curl -s http://localhost:8010/cases/{case_id}
# WS 日志：wscat -c ws://localhost:8010/ws/logs/{case_id}
```

## 常用命令
- 停止：`./dev.sh down`
- 运行 pytest：`python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt -r requirements-dev.txt && python -m pytest -q`
