# Dockerfile Discovery & Preflight 系统级整体 Review

## 背景与问题
Dockerfile 自动发现是 AntiHub 构建链路的第一步，历史上出现过以下误报或可用性问题，主要体现在 `DOCKERFILE_AMBIGUOUS` 触发过早、缺乏可诊断信息：
- **备份文件干扰**：`Dockerfile.original`/`.bak`/`.old` 等被当作同级候选，导致“一个主 Dockerfile + 一个备份”的仓库被判为 ambiguous。
- **多候选但可安全默认**：存在 root `Dockerfile` + 子目录/变种 `Dockerfile.dev` 的仓库，虽然 root 是最合理默认，但仍被误判为 ambiguous。
- **显式路径与上下文覆盖不清晰**：调用方提供 `dockerfile_path/context_path` 时，历史上自动发现逻辑与覆盖规则不够一致，增加了误判。
- **可观测性不足**：preflight 日志不包含结构化的候选列表与选择理由，难以排查“为何被选中/为何 ambiguous”。

本次改造目标是**减少误报、保留安全边界、增强诊断可见性**，在不破坏生产依赖的前提下补齐测试与 CI 回归。

## 变更范围（What changed）
按模块归档（文件路径为仓库实际路径）：

- `dockerfile_discovery.py`
  - 过滤备份候选：`.orig/.original/.bak/.backup/.old/.save/.disabled`（大小写不敏感）。
  - 显式 `dockerfile_path/context_path` 覆盖自动发现。
  - **root Dockerfile 优先**：`context_path/Dockerfile` 优先于其他 primary。
  - 仅在“不安全自动选择”时抛 `DOCKERFILE_AMBIGUOUS`。
  - 元信息新增 `non_unique_primary`（多个 primary 时标记）。

- `worker.py`
  - preflight 结构化日志补齐字段：`scanned_candidates/primary/backup/ignored/selected/selected_backup/selection_reason/non_unique_primary` 等。
  - 当选中备份 Dockerfile 时记录 warning：`DOCKERFILE_BACKUP_SELECTED`。

- `tests/`
  - `tests/test_dockerfile_discovery.py` 新增 6 个 pytest 用例，覆盖 root-priority 与 backup 过滤。
  - `pytest.ini` 限制只收集 `tests/`，避免 `test-repos` 触发外部依赖。

- 工程化/文档
  - `requirements-dev.txt` 引入 `pytest>=7.0`（不污染生产依赖）。
  - `scripts/test.sh` 一键创建 venv → 安装依赖 → 跑 pytest。
  - `.github/workflows/tests.yml`：push/PR 自动回归（py_compile + pytest）。
  - `README.md` 补充最短测试命令 + Python 版本说明。

## 行为变化（Before/After）
以下为典型场景（至少 4 个），对比行为变化：

1) **metagpt-min：root Dockerfile + Dockerfile.original**
- Before：常误判 `DOCKERFILE_AMBIGUOUS`。
- After：选择 root `Dockerfile`，`ignored_backups` 包含 `Dockerfile.original`，`selection_reason=root_dockerfile`；不再抛 ambiguous。

2) **root Dockerfile + Dockerfile.dev（同级 primary）**
- Before：误判 ambiguous。
- After：优先 root，`non_unique_primary=true`，`selection_reason=root_dockerfile`；日志包含候选列表，避免静默误选。

3) **仅备份 Dockerfile.old（无 primary）**
- Before：常报 not found 或 ambiguous（不可用）。
- After：允许 backup-only 兜底，`selected_backup=true`、`selection_reason=single_candidate`，并在 preflight warnings 里标记 `DOCKERFILE_BACKUP_SELECTED`。

4) **多 primary 且无 root（如 serviceA/Dockerfile + serviceB/Dockerfile）**
- Before：ambiguous。
- After：仍 ambiguous（安全边界保留），`selection_reason=ambiguous`，并附带 `ambiguous_candidates` 供排查。

5) **显式 dockerfile_path/context_path**
- Before：自动发现可能覆盖用户选择。
- After：显式路径优先，`selection_reason=explicit_path`；上下文与路径必须在 repo 内。

## 选择策略说明
- **优先级顺序**
  1) 显式 `dockerfile_path`（存在即选中）。
  2) 自动发现：先过滤备份 → 只在 primary 为空时使用备份。
  3) 若 `context_path/Dockerfile` 存在，**root 优先**。
  4) 若候选唯一，直接选中。
  5) 否则标记 ambiguous 并抛错。

- **何时仍 ambiguous**
  - primary 多于一个且没有 root Dockerfile；
  - 或仅备份候选但数量大于 1 且无 root。

- **系统稳定性理由**
  - root Dockerfile 通常是仓库最通用入口，能最大化“一键跑通”；
  - ambiguous 只在真正“不安全自动选择”场景触发；
  - 与 `non_unique_primary` 结合，保留可诊断能力与显式覆盖路径。

## 可诊断性
preflight 结构化日志（`worker.py::_log_dockerfile_discovery`）提供如下字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| scanned_candidates | list[str] | 扫描到的 Dockerfile 候选（去重、排序） |
| primary_candidates | list[str] | 非备份候选 |
| backup_candidates | list[str] | 备份候选 |
| ignored_backups | list[str] | 被忽略的备份候选 |
| non_unique_primary | bool | primary 候选 > 1 时为 true |
| selected_dockerfile | str\|null | 选中 Dockerfile 相对路径 |
| selected_backup | bool | 是否选中备份 Dockerfile |
| selection_reason | str | explicit_path/root_dockerfile/single_candidate/ambiguous/not_found |
| ambiguous_candidates | list[str] | ambiguous 时的候选列表（可选） |
| how_to_fix | str | 修复指引（可选） |

示例（root 优先）：
```json
{
  "scanned_candidates": ["Dockerfile", "docker/Dockerfile", "Dockerfile.original"],
  "primary_candidates": ["Dockerfile", "docker/Dockerfile"],
  "backup_candidates": ["Dockerfile.original"],
  "ignored_backups": ["Dockerfile.original"],
  "non_unique_primary": true,
  "selected_dockerfile": "Dockerfile",
  "selected_backup": false,
  "selection_reason": "root_dockerfile"
}
```

备注：日志中候选列表会做截断（最多 20 项），避免过长输出。

## 可配置性
- **覆盖规则**
  - `dockerfile_path` 指定时直接选中（必须在 repo 内）。
  - `context_path` 限定搜索根与 root Dockerfile 优先范围。
  - `dockerfile_path` 与 `context_path` 组合时，构建上下文以 `context_path` 为准。

- **API/UI 建议**
  - 在创建/重跑接口中暴露 `dockerfile_path/context_path`；
  - 当 `non_unique_primary=true` 时提示用户确认；
  - 显示候选列表并支持“一键重选 + 重试”。

## 可回归性
- **pytest 用例摘要（`tests/test_dockerfile_discovery.py`）**
  1) `Dockerfile + Dockerfile.original` → 选择 root，备份被忽略。
  2) `Dockerfile.old` → 允许备份兜底，`selected_backup=true`。
  3) root `Dockerfile` + `docker/Dockerfile` → root 优先。
  4) root `Dockerfile` + `Dockerfile.dev` → root 优先。
  5) `Dockerfile + Dockerfile.BAK` → 备份大小写不敏感。
  6) `Dockerfile + Dockerfile.disabled + Dockerfile.prod` → disabled 作为备份忽略，root 优先。

- **现有回归覆盖**
  - `tests/test_dockerfile_preflight.py` 仍验证多阶段解析与基础镜像拉取逻辑。
  - `pytest.ini` 限制收集路径，避免 `test-repos` 触发外部依赖（如 aiohttp/tenacity）。

- **CI 覆盖范围**
  - `.github/workflows/tests.yml` 在 push/PR 执行：
    - `python -m py_compile dockerfile_discovery.py worker.py`
    - `python -m pytest -q`
  - Python 版本矩阵：3.10/3.11（CI 为准）。

## 风险与权衡
- **root 优先可能“选错但能跑”**：在多服务/多 Dockerfile 仓库中，root Dockerfile 可能不是目标服务。
  - 缓解：`non_unique_primary=true` + 候选列表日志，提示显式覆盖；UI 侧引导选择。
- **backup-only 兜底可能掩盖问题**：如果唯一 Dockerfile 是旧备份，可能导致构建异常。
  - 缓解：`selected_backup=true` + `DOCKERFILE_BACKUP_SELECTED` warning；仍可显式覆盖。

## 回滚策略
如需切回“更保守默认 ambiguous”，建议采用以下策略之一：
1) **回滚提交**：回退 `dockerfile_discovery.py` 到引入 root-priority 之前的版本，并同步调整 `tests/test_dockerfile_discovery.py` 期望。
2) **引入开关（建议）**：添加配置开关（例如 `ANTIHUB_DOCKERFILE_ROOT_PRIORITY=0`），当关闭时恢复“多 primary 即 ambiguous”的旧策略；日志仍保留 `non_unique_primary`。
3) **阶段性灰度**：仅在 auto mode 启用 root 优先，手动模式继续严格 ambiguous（需额外实现）。

## 验收清单（可直接用于验收）
- **本地回归**
  - `pip install -r requirements.txt -r requirements-dev.txt && python -m pytest -q` → 全绿。
- **语法检查**
  - `python -m py_compile dockerfile_discovery.py worker.py` → 无错误。
- **CI 回归**
  - push/PR 自动触发 `.github/workflows/tests.yml` 且通过（Python 3.10/3.11）。
- **metagpt-min 行为**
  - 仓库含 `Dockerfile` + `Dockerfile.original` → 不再报 `DOCKERFILE_AMBIGUOUS`，选择 root。
- **日志字段完整**
  - preflight 日志包含 `non_unique_primary` 与 `selection_reason`；候选列表可见。

## 下一步建议（Roadmap，按收益排序）
1) **前端/接口“候选选择 + 重跑”**：在 case 详情页展示候选列表并一键重试。
2) **落库 discovery_meta**：将 `dockerfile_discovery` 元信息写入 `preflight_meta`，便于追踪和告警。
3) **集成测试覆盖真实仓库**：在 `test-repos` 维度做自动化端到端，验证 ambiguous 与 override 行为。
4) **可配置 search_depth 与 root 优先**：引入 config/ENV 开关，满足企业仓库差异化需求。
5) **CLI 辅助命令**：提供 `antihub inspect-dockerfile` 输出候选与建议。

