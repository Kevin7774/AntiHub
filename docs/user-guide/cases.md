# 案例生命周期

## 状态（status）
- `PENDING`：排队等待
- `CLONING`：仓库克隆中
- `BUILDING`：构建镜像
- `STARTING`：启动容器
- `RUNNING`：运行中
- `FAILED`：失败（查看 `error_code`）
- `FINISHED`：运行结束
- `ARCHIVED`：已归档

## 阶段（stage）
- `clone` / `build` / `run` / `analyze` / `system`

## 生命周期路径（典型）
1) `PENDING` → `CLONING` → `BUILDING` → `STARTING` → `RUNNING`
2) 失败时：`FAILED`（包含 `error_code` 与 `error_message`）

## 常见问题处理
- `DOCKERFILE_NOT_FOUND`：检查 `dockerfile_path/context_path` 或切换为 showcase
- `DOCKERFILE_AMBIGUOUS`：显式指定 Dockerfile
- `CONTAINER_EXIT_NONZERO`：查看容器日志定位应用错误

## Manual/Showcase
- 当启用 `auto_mode` 且 Dockerfile 缺失时，会自动切换到 showcase 并尝试生成说明书。
