# 系统级整体 Review（模板库 + 多策略引擎 + Dockerfile Discovery + 文档交付）

> 面向乙方/受众的一页式工程复盘：可诊断、可配置、可回归、可演示。

## 1) 背景与问题
- 过去演示常见问题：
  - 真实仓库没有 Dockerfile 或在子目录，触发 `DOCKERFILE_NOT_FOUND`
  - Dockerfile 备份文件（`.original/.bak`）误入候选导致 `DOCKERFILE_AMBIGUOUS`
  - 模板指向本地路径，换环境后无法复现
- 目标：模板库稳定可拉取、Dockerfile 发现可解释、**自动兜底可演示**、日志可定位、回归可自动化。

## 2) 变更范围（What changed）
- 模板库
  - 模板统一使用公开 GitHub repo_url（不依赖本地 fixtures）
  - 通过 `context_path` 支持子目录 Dockerfile
  - 新增字段：`dimensions` / `expected` / `what_to_verify`
- 多策略引擎
  - 默认 `run_mode=auto`：可跑就跑，不可跑自动生成或降级 showcase
  - Repo Inspector：识别 `repo_type` + `evidence`
  - Strategy Registry：Dockerfile / Compose / Generated / Showcase
- Dockerfile discovery
  - backup 后缀过滤策略贯穿运行链路
  - preflight 结构化日志补齐可诊断字段
- 文档交付
  - 新增系统级 review（本文件）
  - 新增 Docker Desktop 代理配置说明
  - README 增加 10 分钟跑通步骤 + FAQ

## 3) 行为变化（Before / After）
- **Dockerfile + Dockerfile.original**
  - Before：可能误报 `DOCKERFILE_AMBIGUOUS`
  - After：过滤 backup，自动选择 `Dockerfile`，日志记录 `ignored_backups`
- **无 Dockerfile 仓库**
  - Before：直接失败，无法演示
  - After：`run_mode=auto` 自动生成 Dockerfile（node/python/static）或降级 showcase
- **Dockerfile 在子目录**
  - Before：需要手动猜测路径
  - After：模板自带 `context_path`，自动可用
- **多候选 Dockerfile**
  - Before：错误不透明
  - After：返回 `DOCKERFILE_AMBIGUOUS`，日志含候选清单与修复建议

## 4) 选择策略说明（多策略 + Dockerfile discovery）
- **策略选择**（auto）：Dockerfile → Compose → Generated → Showcase
- **容器强制**（container）：只认 Dockerfile/Compose；缺失直接报错
- **展示模式**（showcase）：只生成说明书/展厅
- 分类：primary 与 backup
- 选择优先级：
  1) 显式 `dockerfile_path/context_path`
  2) context root `Dockerfile`
  3) 仅剩单候选
  4) 多候选 → `DOCKERFILE_AMBIGUOUS`
- 设计理由：避免误选备份；在可确定场景自动选中；不确定则提示用户显式指定。

## 5) 可诊断性（结构化日志）
字段（节选）：
- `dockerfile_candidates` / `primary_candidates` / `backup_candidates_filtered`
- `selected_dockerfile_path` / `selected_context_path`
- `selection_reason` / `non_unique_primary`
- `repo_type` / `evidence` / `strategy_selected` / `fallback_reason`

示例：
```json
{
  "dockerfile_candidates": ["Dockerfile", "Dockerfile.original"],
  "primary_candidates": ["Dockerfile"],
  "backup_candidates_filtered": ["Dockerfile.original"],
  "non_unique_primary": false,
  "selected_dockerfile_path": "Dockerfile",
  "selected_context_path": ".",
  "selection_reason": "root_dockerfile"
}
```

## 6) 可配置性（模板与覆盖）
- 模板字段：`repo_url` + `context_path` + `expected` + `what_to_verify`
- 显式覆盖：用户可以在创建 case 时指定 `dockerfile_path/context_path`
- 模板来源：公开 GitHub 仓库，默认无需额外配置

## 7) 可回归性（测试 + CI）
- pytest 覆盖 Dockerfile 发现与预检逻辑
- 新增 worker 路径的发现日志格式测试
- CI 持续回归（py_compile + pytest）

## 8) 风险与权衡
- root 优先策略可能导致“能跑但不是用户预期”的风险
  - 缓解：`non_unique_primary` + `selection_reason` + 明确 override
- 真实 GitHub 仓库可能因网络/代理失败
  - 缓解：代理配置文档 + 选用轻量样例仓库

## 9) 回滚策略
- Dockerfile 选择策略：回退到仅在单候选时自动选择（保留 `DOCKERFILE_AMBIGUOUS`）
- 模板策略：固定到已知稳定 GitHub repo，或在模板层切换到 showcase

## 10) 验收清单
- `./dev.sh up`
- UI 选择模板创建 case → 可看到预期/维度/验证说明
- Logs 中包含 dockerfile 结构化字段
- `python -m pytest -q` 全绿
- 代理/拉取失败按 `docs/operations/docker-desktop-proxy.md` 排查

## 11) Roadmap（收益排序）
1) 模板仓库健康检查与定期可达性验证
2) 模板预期与真实状态的自动对比报告
3) 模板 YAML 校验与 schema 校验
4) 更多真实仓库样例（轻量）
5) UI 侧一键 override Dockerfile/context
