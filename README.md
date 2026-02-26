# AntiHub

## 开发与测试

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q
```

或使用一键脚本：

```bash
./scripts/test.sh
```

## 本地启动（开发模式）

```bash
# 1. 启动 Redis + Openclaw 后台服务
./scripts/dev_services.sh up

# 2. 启动 API（另一个终端）
source .venv/bin/activate
uvicorn main:app --host 127.0.0.1 --port 8010 --reload
```

停止后台服务：

```bash
./scripts/dev_services.sh down
```

## 生产部署（Docker Compose）

```bash
# 1. 准备环境变量
cp .env.prod.example .env.prod   # 填入真实密钥，切勿提交到 git

# 2. 首次启动
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build

# 3. 查看服务状态
docker compose -f docker-compose.prod.yml --env-file .env.prod ps

# 4. 日常更新
./scripts/update_prod.sh
```

更多文档见 `docs/`。
