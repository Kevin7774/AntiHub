# 部署

## 本地/WSL 部署（推荐）
```bash
./dev.sh up
```
- 默认端口：`8010`
- 日志文件：`.devlogs/api.log` 与 `.devlogs/worker.log`

## 环境变量（常用）
- `REDIS_URL`：Redis 地址
- `API_HOST` / `API_PORT`：API 监听地址/端口
- `PUBLIC_HOST`：对外访问域名（用于 access_url）
- `ROOT_PATH`：反向代理路径前缀
- `DOCKER_BUILD_NETWORK`：构建网络（例如 `host`）
- `DOCKERFILE_SEARCH_DEPTH`：Dockerfile 搜索深度

配置优先级：环境变量 > `config.yaml` > 默认值。

## 代理配置（Docker Desktop / WSL2）
> 目标：让 `docker pull`、`docker build`（Dockerfile 内部网络请求）和 `docker run` 稳定走代理。

1) Docker Desktop → Settings → Proxies  
   填 `http://127.0.0.1:7897`（daemon pull 使用）

2) AntiHub `config.yaml`（容器内出网使用 `host.docker.internal`）
```yaml
network:
  http_proxy: "http://host.docker.internal:7897"
  https_proxy: "http://host.docker.internal:7897"
  no_proxy: "localhost,127.0.0.1,::1,host.docker.internal"
  inject_runtime_proxy: true
  check_docker_proxy: true
```
也可用环境变量覆盖：`HTTP_PROXY/HTTPS_PROXY/NO_PROXY`（大小写都支持）。

3) 验证
```bash
docker info | grep -i proxy
docker run --rm curlimages/curl:latest curl -I http://host.docker.internal:7897
```

说明：如果 AntiHub 已配置代理但 `docker info` 看不到 Proxy 字段，日志会提示 daemon 可能未配置。
详见：`docs/operations/docker-desktop-proxy.md`

## 前端部署
```bash
cd frontend
npm ci
npm run build
npm run preview
```

## 生产部署（Docker Compose）
1) 准备生产环境变量：
```bash
cp .env.prod.example .env.prod
```
2) 首次启动：
```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build
docker compose -f docker-compose.prod.yml --env-file .env.prod ps
```
3) 日常更新：
- Git 模式（服务器有 git 仓库）：
```bash
./scripts/update_prod.sh
```
- SFTP 模式（Termius 直传代码）：
```bash
SOURCE_SYNC_MODE=sftp ./scripts/update_prod.sh
```
`update_prod.sh` 默认 `SOURCE_SYNC_MODE=auto`，会自动识别是否可执行 git pull。
