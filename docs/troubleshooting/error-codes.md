# 错误码手册

本页按错误码给出**原因 → 定位 → 解决 → 复现**的最小闭环。

## DOCKERFILE_AMBIGUOUS
- 原因：存在多个可选 Dockerfile 且无法安全自动选择。
- 定位：查看 preflight 结构化日志的 `primary_candidates` / `selection_reason`。
- 解决：显式指定 `dockerfile_path/context_path`。
- 复现：仓库内放置 `serviceA/Dockerfile` + `serviceB/Dockerfile` 且无 root Dockerfile。

## DOCKERFILE_NOT_FOUND
- 原因：在搜索深度内未找到 Dockerfile。
- 定位：日志显示 `Dockerfile not found`，`selection_reason=not_found`。
- 解决：补充 Dockerfile 或显式指定路径；或切换 showcase。
- 备注：`run_mode=auto` 会自动尝试生成 Dockerfile（node/python/static），失败则降级 showcase，不再致命。
- 复现：仓库中无 Dockerfile。

## DOCKER_BUILD_FAILED
- 原因：Docker build 失败（语法错误/依赖拉取失败）。
- 定位：查看 build 日志与 Dockerfile。
- 解决：修正 Dockerfile、依赖、网络或 BuildKit 设置。
- 复现：在 Dockerfile 中写入不存在的基础镜像。

## DOCKER_BASE_IMAGE_PULL_FAILED
- 原因：基础镜像拉取失败（网络/DNS/registry）。
- 定位：preflight pull 日志出现超时或解析失败。
- 解决：检查网络、镜像源、代理、DNS。
- 复现：拉取一个不存在的镜像标签。

## DOCKER_BUILD_NETWORK_FAILED
- 原因：构建网络不通。
- 定位：日志包含 `dial tcp`/`temporary failure in name resolution`。
- 解决：设置 `docker_build_network=host` 或修复 DNS/代理。
- 复现：禁用网络或配置错误 DNS。

## BUILDKIT_REQUIRED
- 原因：Dockerfile 使用 BuildKit 特性但未启用。
- 定位：日志包含 `RUN --mount` 或 `FROM --platform`。
- 解决：开启 BuildKit（`DOCKER_BUILDKIT=1`）。
- 复现：在 Dockerfile 中加入 `RUN --mount=type=cache`。

## GIT_REF_NOT_FOUND
- 原因：指定分支不存在。
- 定位：日志包含 requested_ref/heads 列表。
- 解决：使用 `ref=auto` 或改为存在的分支名。
- 复现：创建 case 时传入不存在的 ref。

## PORT_IN_USE / PORT_POOL_EXHAUSTED
- 原因：端口被占用或端口池耗尽。
- 定位：错误码与日志提示端口冲突。
- 解决：停止旧容器或扩大端口范围。
- 复现：手动占用端口池范围内端口。

## CONTAINER_EXIT_NONZERO
- 原因：应用启动后非零退出。
- 定位：查看容器日志与退出码。
- 解决：修复应用启动问题或依赖配置。
- 复现：让容器 CMD 立即退出。

## STARTUP_TIMEOUT
- 原因：应用未在超时时间内通过健康检查。
- 定位：日志显示超时，未进入 RUNNING。
- 解决：确认应用监听 0.0.0.0 且端口正确；提升 `TIMEOUT_RUN`。
- 复现：让应用 sleep 超过超时。

## COMPOSE_UP_FAILED / COMPOSE_TIMEOUT / COMPOSE_NOT_AVAILABLE
- 原因：Compose 启动失败、启动超时或 docker compose 不可用。
- 定位：查看 case 日志与 compose 输出。
- 解决：确认 docker compose v2 可用、镜像可拉取、端口映射正确。
- 复现：删除 docker compose 或在 compose 中配置错误服务。

## COMPOSE_CONTAINER_EXITED
- 原因：关键服务（如 create-site）非零退出。
- 定位：查看 compose 日志与对应服务日志。
- 解决：检查数据库/依赖服务配置或重试初始化流程。

## ANALYZE_REPO_NOT_FOUND
- 原因：Explain 分析阶段无法访问仓库或克隆失败。
- 定位：查看 analyze 日志，确认 repo_url/权限/网络。
- 解决：修正仓库地址或访问权限，再触发分析。
- 复现：传入不可访问的 repo_url。

## ANALYZE_LLM_FAILED
- 原因：LLM 调用失败或配置缺失（API Key/Base URL/Model）。
- 定位：检查环境变量与 analyze 日志。
- 解决：补齐 `OPENAI_API_KEY` 等配置，或切换可用的 LLM endpoint。
- 复现：不设置 API Key 或填入错误地址。

## ANALYZE_MERMAID_VALIDATE_FAILED
- 原因：Mermaid 语法无法渲染或校验失败。
- 定位：Explain 报告中的 validation 信息与原始 Mermaid 代码块。
- 解决：修正 Mermaid 代码，或让 LLM 重新生成。
- 复现：提交错误的 Mermaid 语法。

## REPORT_NOT_READY
- 原因：Explain 报告尚未生成或未命中缓存。
- 定位：查看 analyze 状态与日志。
- 解决：触发 `POST /cases/{id}/analyze` 并等待完成。
- 复现：未触发 analyze 直接请求 report。
