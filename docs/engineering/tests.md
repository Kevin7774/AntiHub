# 测试体系

## 单元测试（pytest）
- 测试入口：`python -m pytest -q`
- 依赖：`requirements-dev.txt`（`pytest>=7.0`）
- 只收集 `tests/`：通过 `pytest.ini` 避免 `test-repos` 依赖干扰

### Dockerfile discovery 用例（6 个）
> 结构 → 期望行为
1) `Dockerfile` + `Dockerfile.original` → 选择 root，忽略备份
2) 仅 `Dockerfile.old` → backup-only 兜底，`selected_backup=true`
3) root `Dockerfile` + `docker/Dockerfile` → root 优先
4) root `Dockerfile` + `Dockerfile.dev` → root 优先
5) `Dockerfile` + `Dockerfile.BAK` → 备份大小写不敏感
6) `Dockerfile` + `Dockerfile.disabled` + `Dockerfile.prod` → disabled 视为备份，root 优先

## 其他测试
- `tests/test_dockerfile_preflight.py`：多阶段解析与基础镜像拉取逻辑
- 集成测试脚本：`./integration_tests.md`

## CI 回归
- Workflow：`.github/workflows/tests.yml`
- Python 版本：3.10 / 3.11
- 步骤：`py_compile` + `pytest -q`
