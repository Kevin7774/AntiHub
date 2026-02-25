# Docker Desktop 代理配置（WSL2 + HTTP 7897）

目标：让 `docker pull`、`docker build`（Dockerfile 内部网络请求）和 `docker run` 稳定走代理。

## 1. Docker Desktop 侧（影响 docker pull）
在 Windows Docker Desktop：
- Settings → Proxies
- HTTP Proxy / HTTPS Proxy：`http://127.0.0.1:7897`

验证：
```bash
docker info | grep -i proxy
```

## 2. AntiHub 侧（影响 docker build/run）
在 `config.yaml` 中设置（容器内访问宿主代理需使用 `host.docker.internal`）：
```yaml
network:
  http_proxy: "http://host.docker.internal:7897"
  https_proxy: "http://host.docker.internal:7897"
  no_proxy: "localhost,127.0.0.1,::1,host.docker.internal"
  inject_runtime_proxy: true
  check_docker_proxy: true
```

也可通过环境变量覆盖：`HTTP_PROXY/HTTPS_PROXY/NO_PROXY`（大小写均支持）。

验证：
```bash
docker run --rm curlimages/curl:latest curl -I http://host.docker.internal:7897
```

## 3. 常见问题定位
- **docker pull 失败**：说明 daemon 代理未生效（检查 Desktop 配置 + `docker info`）
- **Dockerfile 内 apt/pip/npm 失败**：说明 build/run 代理未注入（检查 AntiHub `network.*` 配置）

> 提示：preflight 日志会输出 `proxy_injection` 与 `docker_daemon_proxy` 字段，便于判断注入是否生效。
