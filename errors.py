from typing import Dict

ERROR_CODE_MAP: Dict[str, Dict[str, str]] = {
    "PORT_POOL_EXHAUSTED": {
        "message": "端口池已满",
        "hint": "停止旧容器或扩大端口范围。",
    },
    "PORT_IN_USE": {
        "message": "端口已被占用",
        "hint": "请等待端口释放或改用其他端口。",
    },
    "GIT_CLONE_FAILED": {
        "message": "Git 克隆失败",
        "hint": "检查仓库地址、网络与权限配置。",
    },
    "GIT_REF_NOT_FOUND": {
        "message": "Git 分支不存在",
        "hint": "确认 ref/branch 是否存在，或留空自动识别默认分支。",
    },
    "GIT_CLONE_TIMEOUT": {
        "message": "Git 克隆超时",
        "hint": "缩小仓库或提升克隆超时。",
    },
    "DOCKER_BUILD_FAILED": {
        "message": "Docker 构建失败",
        "hint": "检查 Dockerfile 与构建日志。",
    },
    "DOCKER_API_ERROR": {
        "message": "Docker 守护进程异常",
        "hint": "确认 Docker 服务可用、磁盘空间充足。",
    },
    "DOCKERFILE_MISSING": {
        "message": "Dockerfile 缺失",
        "hint": "请在仓库根目录补齐 Dockerfile。",
    },
    "DOCKERFILE_NOT_FOUND": {
        "message": "Dockerfile 未找到",
        "hint": "检查 dockerfile_path/context_path 或切换为 showcase 模式。",
    },
    "DOCKERFILE_AMBIGUOUS": {
        "message": "Dockerfile 存在多个候选",
        "hint": "请显式指定 dockerfile_path/context_path。",
    },
    "DOCKER_BASE_IMAGE_PULL_FAILED": {
        "message": "基础镜像拉取失败",
        "hint": "检查 registry mirror、网络或 DNS 配置。",
    },
    "CONTAINER_EXITED": {
        "message": "容器启动阶段退出",
        "hint": "确认进程持续运行且监听端口正确。",
    },
    "DOCKER_BUILD_NETWORK_FAILED": {
        "message": "构建网络失败",
        "hint": "检查 DNS/代理/构建网络配置（docker_build_network）。",
    },
    "DNS_RESOLUTION_FAILED": {
        "message": "DNS 解析失败",
        "hint": "检查 DNS 服务器、镜像源或代理配置。",
    },
    "REGISTRY_UNREACHABLE": {
        "message": "镜像仓库不可达",
        "hint": "检查 registry 地址、镜像源或网络策略。",
    },
    "BUILDKIT_REQUIRED": {
        "message": "Docker BuildKit 未启用",
        "hint": "请设置 DOCKER_BUILDKIT=1 或在 daemon.json 中开启 features.buildkit=true。",
    },
    "CONTAINER_EXIT_NONZERO": {
        "message": "容器非零退出",
        "hint": "检查应用启动日志与依赖配置。",
    },
    "STARTUP_TIMEOUT": {
        "message": "容器启动超时",
        "hint": "确认应用监听 0.0.0.0 且端口匹配 EXPOSE。",
    },
    "TIMEOUT_CLONE": {
        "message": "克隆超时",
        "hint": "优化仓库体积或提升超时配置。",
    },
    "TIMEOUT_BUILD": {
        "message": "构建超时",
        "hint": "减少构建步骤或提升超时配置。",
    },
    "TIMEOUT_RUN": {
        "message": "启动超时",
        "hint": "检查应用启动耗时与健康检查。",
    },
    "TIMEOUT_ANALYZE": {
        "message": "分析超时",
        "hint": "简化分析步骤或提升超时配置。",
    },
    "TIMEOUT_VISUALIZE": {
        "message": "可视化超时",
        "hint": "简化可视化步骤或提升 VISUALIZE_TIMEOUT 配置。",
    },
    "ANALYZE_REPO_NOT_FOUND": {
        "message": "分析仓库不可用",
        "hint": "确认仓库地址可访问或已成功克隆。",
    },
    "ANALYZE_LLM_FAILED": {
        "message": "说明书生成失败",
        "hint": "检查 LLM 配置（API Key/Base URL/Model）或稍后重试。",
    },
    "ANALYZE_MERMAID_VALIDATE_FAILED": {
        "message": "Mermaid 校验失败",
        "hint": "图表未通过渲染校验，将返回原始代码以便手动修复。",
    },
    "REPORT_NOT_READY": {
        "message": "说明书尚未生成",
        "hint": "请先触发分析并等待完成。",
    },
    "VISUAL_NOT_READY": {
        "message": "可视化尚未生成",
        "hint": "请先触发 Visualize 并等待完成。",
    },
    "VISUALIZE_REPORT_NOT_READY": {
        "message": "Explain 报告缺失",
        "hint": "请先生成 Explain 报告再执行可视化。",
    },
    "VISUALIZE_MERMAID_RENDER_FAILED": {
        "message": "Mermaid 渲染失败",
        "hint": "检查 mmdc 安装或 Mermaid 语法。",
    },
    "VISUALIZE_IMAGE_API_FAILED": {
        "message": "图像生成失败",
        "hint": "检查 MiniMax 配置（API Key/Base URL/Model）或稍后重试。",
    },
    "VISUALIZE_INVALID_RESPONSE": {
        "message": "图像生成响应异常",
        "hint": "检查接口返回或更新模型配置。",
    },
    "VISUALIZE_INDEX_FAILED": {
        "message": "仓库索引生成失败",
        "hint": "检查仓库结构或缩小分析范围。",
    },
    "VISUALIZE_GRAPH_FAILED": {
        "message": "仓库图谱生成失败",
        "hint": "请检查 repo_index 数据是否完整。",
    },
    "VISUALIZE_SPOTLIGHT_FAILED": {
        "message": "代码精选失败",
        "hint": "检查仓库文件读取权限或缩小范围。",
    },
    "VISUALIZE_STORYBOARD_FAILED": {
        "message": "视频分镜生成失败",
        "hint": "确认索引/图谱/代码数据齐全。",
    },
    "VISUALIZE_PRODUCT_STORY_FAILED": {
        "message": "产品故事生成失败",
        "hint": "检查 LLM 配置或稍后重试。",
    },
    "VISUALIZE_RENDER_FAILED": {
        "message": "视频渲染失败",
        "hint": "检查 Remotion/Node 环境或查看渲染日志。",
    },
    "INGEST_REPO_NOT_FOUND": {
        "message": "仓库抓取失败",
        "hint": "确认仓库地址、分支和权限配置。",
    },
    "INGEST_FAILED": {
        "message": "仓库抓取异常",
        "hint": "检查 OpenClaw/网络配置或稍后重试。",
    },
    "GITHUB_RATE_LIMIT": {
        "message": "GitHub 访问受限",
        "hint": "触发了 GitHub API 限流，请稍后重试或配置 Token。",
    },
    "LFS_FAILED": {
        "message": "Git LFS 拉取失败",
        "hint": "检查 LFS 安装或关闭 LFS 选项后重试。",
    },
    "SUBMODULE_FAILED": {
        "message": "子模块拉取失败",
        "hint": "检查子模块路径或关闭 submodule 选项后重试。",
    },
    "TIMEOUT_MANUAL": {
        "message": "说明书生成超时",
        "hint": "缩小仓库或提升 TIMEOUT_MANUAL 配置。",
    },
    "COMPOSE_UP_FAILED": {
        "message": "Compose 启动失败",
        "hint": "检查 docker compose 输出与 compose 配置。",
    },
    "COMPOSE_TIMEOUT": {
        "message": "Compose 启动超时",
        "hint": "确认服务健康检查与端口映射配置。",
    },
    "COMPOSE_NOT_AVAILABLE": {
        "message": "Compose 不可用",
        "hint": "安装 Docker Compose v2 或检查 docker compose 命令。",
    },
    "COMPOSE_CONTAINER_EXITED": {
        "message": "Compose 容器异常退出",
        "hint": "查看容器日志与依赖服务状态。",
    },
    "STOPPED_BY_USER": {
        "message": "已手动停止",
        "hint": "可使用 Restart 重新启动。",
    },
    "ARCHIVED": {
        "message": "已归档",
        "hint": "可在列表中启用 include_archived 查看。",
    },
    "MANUAL_GENERATION_FAILED": {
        "message": "说明书生成失败",
        "hint": "检查仓库结构与 README，稍后重试。",
    },
    "MANUAL_CLONE_FAILED": {
        "message": "说明书克隆失败",
        "hint": "确认仓库地址可访问。",
    },
    "MANUAL_PARSE_FAILED": {
        "message": "说明书解析失败",
        "hint": "检查仓库文件编码与权限。",
    },
    "FEATURE_DISABLED": {
        "message": "功能未开启",
        "hint": "请联系管理员开启对应 Feature Flag。",
    },
    "NOT_ADMIN": {
        "message": "管理员权限不足",
        "hint": "仅管理员可访问该接口。",
    },
    "ENTITLEMENT_REQUIRED": {
        "message": "权益不足",
        "hint": "当前套餐未包含该功能，请升级套餐后重试。",
    },
    "UNEXPECTED_ERROR": {
        "message": "未知错误",
        "hint": "查看日志详情或联系管理员。",
    },
}


def explain_error(code: str | None) -> Dict[str, str] | None:
    if not code:
        return None
    return ERROR_CODE_MAP.get(code)
