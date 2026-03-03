# AntiHub 搜索系统审查报告

> 审查日期：2026-03-03
> 基于：搜索系统盘点报告（Search Inventory Report）
> 目的：验证已识别问题的修复状态 + 各模块多维度评分 + TODO 清单

---

## 一、已识别问题修复状态

### 问题 1：GitCode Provider 死代码 Bug

| 项目 | 状态 |
|------|------|
| **状态** | **未修复** |
| **位置** | `recommend/gitcode.py:62-63` |
| **严重程度** | HIGH |

**现状：** `_request_json()` 函数中第 52-63 行，`try` 块在第 53 行 `return json.loads(raw)` 直接返回，`except` 块在第 57-61 行全部 `raise`。第 62-63 行的 `record_timing_metric` 和 `return parsed` **永远不会执行**，且 `parsed` 变量未定义。

```python
# gitcode.py:52-63 — 死代码示例
try:
    return json.loads(raw)          # ← 成功直接 return
except Exception as exc:
    ...
    raise GitCodeAPIError(...)      # ← 失败直接 raise
record_timing_metric(...)           # ← 不可达
return parsed                       # ← 不可达 + parsed 未定义
```

**影响：**
1. GitCode 搜索延迟指标（`recommend.provider.gitcode.latency_ms`）永远不会被记录
2. 监控面板中 GitCode 延迟数据为空

---

### 问题 2：无 HTTP 重试机制

| 项目 | 状态 |
|------|------|
| **状态** | **未修复** |
| **位置** | `recommend/github.py`, `recommend/gitee.py`, `recommend/gitcode.py` |
| **严重程度** | MEDIUM |

**现状：** 三个 Provider 均使用 `urllib.request` 发起 HTTP 请求，无任何重试/退避逻辑。任何瞬时网络抖动、DNS 超时、服务端 5xx 错误都会直接导致该 Provider 搜索失败。

- `github.py:85-90` — HTTPError 直接 raise
- `gitee.py:36-40` — HTTPError 直接 raise
- `gitcode.py:45-51` — HTTPError 直接 raise

**影响：** 生产环境中网络不稳定时搜索结果不完整。虽然单 Provider 失败不会导致整体失败（降级为 warning），但用户体验受损。

---

### 问题 3：同步阻塞（urllib 而非异步 HTTP）

| 项目 | 状态 |
|------|------|
| **状态** | **未修复（设计选型问题）** |
| **位置** | 所有 Provider + `recommend/service.py` |
| **严重程度** | MEDIUM |

**现状：** 所有 Provider 使用同步 `urllib.request`，通过 `ThreadPoolExecutor(max_workers=12)` 实现并发。虽然功能正常，但存在以下问题：
- 每个并发搜索任务占用一个系统线程
- 高并发时线程资源消耗大
- 无法利用 asyncio 事件循环的轻量级并发

**注意：** 这是一个架构级别的改进项，短期内不影响功能，但长期影响扩展性。

---

### 问题 4：跨平台去重缺失

| 项目 | 状态 |
|------|------|
| **状态** | **未修复** |
| **位置** | `recommend/service.py:555`, `recommend/service.py:1008-1014` |
| **严重程度** | MEDIUM |

**现状：** 去重 ID 格式为 `"{source}:{full_name}"`（如 `github:torvalds/linux`），导致同一仓库在不同平台上不会被去重。

**影响：** 用户搜索 "linux kernel" 可能看到同一仓库的 GitHub、Gitee、GitCode 三个版本，浪费推荐位。

---

### 问题 5：环境变量文档缺失

| 项目 | 状态 |
|------|------|
| **状态** | **部分修复** |
| **位置** | `.env.local.example`, `.env.prod.example` |
| **严重程度** | LOW |

**覆盖情况：**

| 变量 | `.env.local.example` | `.env.prod.example` | 状态 |
|------|---------------------|---------------------|------|
| `GITHUB_TOKEN` | 缺失 | 缺失 | 未修复 |
| `GITEE_TOKEN` | 缺失 | 缺失 | 未修复 |
| `GITCODE_TOKEN` | 缺失 | 缺失 | 未修复 |
| `OPENCLAW_BASE_URL` | 缺失 | 缺失 | 未修复 |
| `RECOMMEND_PROVIDER_TIMEOUT_SECONDS` | 缺失 | 缺失 | 未修复 |
| `RECOMMEND_TOP_K` | 缺失 | 缺失 | 未修复 |
| `DEEP_SEARCH_POINTS_COST` | 缺失 | 缺失 | 未修复 |
| `LLM_PROVIDER` | 缺失 | 已有 | 部分修复 |
| `DEEPSEEK_API_KEY` | 缺失 | 已有 | 部分修复 |
| `RECOMMEND_LLM_PROVIDER` | 缺失 | 已有（注释） | 部分修复 |

---

### 问题 6：LLM 强依赖

| 项目 | 状态 |
|------|------|
| **状态** | **部分修复（降级路径存在但有盲区）** |
| **位置** | `recommend/service.py`, `recommend/llm.py` |
| **严重程度** | MEDIUM |

**降级路径覆盖情况：**

| LLM 调用 | 降级是否存在 | 降级质量 |
|----------|-------------|---------|
| 需求画像 `build_requirement_profile()` | 有 → 返回 None，用原始 query | 可用 |
| 语义排序 `rank_candidates()` | 有 → `_fallback_rank()` 关键词排序 | 可用 |
| 深度总结 `summarize_findings()` | 有 → 规则生成摘要 | 可用 |
| 查询改写 `extract_search_queries()` | **部分** → deep 模式下 LLM 不可用直接报错返回空结果 | **有问题** |

**关键盲区：** `recommend/llm.py:302-305` — deep 模式下如果 LLM 不可用，`extract_search_queries()` 直接抛出 `RecommendLLMError`，导致 `recommend/service.py:967-969` 返回**空推荐结果**而非降级为关键词搜索。

---

### 问题 7：OpenClaw 超时过长

| 项目 | 状态 |
|------|------|
| **状态** | **部分修复** |
| **位置** | `config.py:275`, `config.py:397` |
| **严重程度** | LOW |

**现状：**
- `OPENCLAW_TIMEOUT_SECONDS` 默认仍为 300 秒
- 但在搜索文档抓取场景中，`RECOMMEND_DEEP_DOC_TIMEOUT_SECONDS`（默认 10 秒）被传入并覆盖
- `recommend/deep_fetch.py:134` 使用 `max(4, timeout)` 确保最低 4 秒

**评估：** 搜索场景中实际生效超时为 10 秒，不会导致用户长时间等待。300 秒默认值仅影响非搜索场景下的 OpenClaw 直接调用。

---

### 修复状态总结

| # | 问题 | 状态 | 优先级 |
|---|------|------|--------|
| 1 | GitCode 死代码 Bug | **未修复** | P1 |
| 2 | 无 HTTP 重试 | **未修复** | P2 |
| 3 | 同步阻塞 | **未修复（架构级）** | P3 |
| 4 | 跨平台去重缺失 | **未修复** | P2 |
| 5 | 环境变量文档缺失 | **部分修复** | P3 |
| 6 | LLM 强依赖 | **部分修复** | P2 |
| 7 | OpenClaw 超时过长 | **部分修复** | P3（可接受） |

**结论：7 个问题中 0 个完全修复，3 个部分修复，4 个未修复。**

---

## 二、各模块多维度评分

### 评分维度说明

| 维度 | 说明 | 满分 |
|------|------|------|
| 代码质量 | 可读性、一致性、命名规范、无代码异味 | 10 |
| 架构设计 | 职责分离、模块化、扩展性 | 10 |
| 错误处理 | 异常捕获完整性、降级策略、用户反馈 | 10 |
| 测试覆盖 | 单元/集成测试完整度（基于测试文件分析） | 10 |
| 可维护性 | 文档、配置外化、变更成本 | 10 |
| 安全性 | 认证、输入校验、注入防护、密钥管理 | 10 |
| **综合** | **加权平均** | **10** |

---

### 模块评分卡

#### 1. 前端 — `frontend/src/App.tsx`（7457 行）

| 维度 | 分数 | 说明 |
|------|------|------|
| 代码质量 | 4 | 7457 行单体组件，严重违反 SRP；TypeScript 类型定义较完整 |
| 架构设计 | 3 | 几乎所有页面逻辑在一个文件，无路由分离，无状态管理库 |
| 错误处理 | 6 | `apiFetch` 统一封装、401 自动登出、loading/error 状态管理 |
| 测试覆盖 | 2 | 未发现前端测试文件（无 `*.test.tsx`、无 `__tests__/`） |
| 可维护性 | 3 | 单文件难以多人协作，任何改动需理解全局上下文 |
| 安全性 | 7 | JWT Bearer 认证、XSS 输入检测（`_require_safe_input`） |
| **综合** | **4.2** | **急需拆分组件和引入路由** |

#### 2. API 层 — `main.py`（5921 行）

| 维度 | 分数 | 说明 |
|------|------|------|
| 代码质量 | 4 | 5921 行单文件，包含所有路由+中间件+工具函数 |
| 架构设计 | 4 | FastAPI 路由扁平化，无 Blueprint/Router 分组 |
| 错误处理 | 7 | 全局异常处理器、速率限制中间件、积分扣费检查 |
| 测试覆盖 | 6 | 有 `test_recommend_stream_api.py`(66行) 等多个测试文件 |
| 可维护性 | 3 | 单文件难以导航，改动风险高 |
| 安全性 | 8 | JWT 认证、租户隔离、速率限制、输入校验、CORS |
| **综合** | **5.3** | **需要按领域拆分路由模块** |

#### 3. 决策编排 — `decision/service.py`（1181 行）

| 维度 | 分数 | 说明 |
|------|------|------|
| 代码质量 | 6 | 函数划分合理，命名清晰（`_infer_query_intents`, `_score_case`） |
| 架构设计 | 7 | 清晰的编排模式：目录匹配 → 护栏过滤 → 外部搜索回退 |
| 错误处理 | 6 | 护栏清空时有回退路径，但日志不够结构化 |
| 测试覆盖 | 7 | `test_decision_engine.py`(268行) 覆盖核心流程 |
| 可维护性 | 6 | 职责明确，但与 recommend/service.py 耦合较深 |
| 安全性 | 7 | 语义护栏防止不相关推荐 |
| **综合** | **6.5** | **质量较好，需关注耦合** |

#### 4. 搜索核心 — `recommend/service.py`（1316 行）

| 维度 | 分数 | 说明 |
|------|------|------|
| 代码质量 | 6 | 阶段划分清晰（7 个阶段），但函数较长 |
| 架构设计 | 7 | 多源并发、去重归一化、混合打分架构合理 |
| 错误处理 | 7 | 每个阶段有独立降级路径，warnings 累积传递 |
| 测试覆盖 | 7 | `test_recommend_service.py`(531行) 最大的测试文件 |
| 可维护性 | 6 | 打分权重硬编码，调参需改代码 |
| 安全性 | 6 | 查询长度截断、文件大小限制 |
| **综合** | **6.5** | **核心逻辑质量较好** |

#### 5. GitHub Provider — `recommend/github.py`（132 行）

| 维度 | 分数 | 说明 |
|------|------|------|
| 代码质量 | 7 | 简洁明了，职责单一 |
| 架构设计 | 6 | 接口清晰，但无重试/断路器 |
| 错误处理 | 6 | 区分限流(403)和一般错误，但无重试 |
| 测试覆盖 | 5 | `test_recommend_github.py`(39行) 覆盖较薄 |
| 可维护性 | 7 | 代码量小，改动风险低 |
| 安全性 | 7 | Token 通过环境变量加载，不在代码中硬编码 |
| **综合** | **6.3** | **基础功能完善，缺重试** |

#### 6. Gitee Provider — `recommend/gitee.py`（86 行）

| 维度 | 分数 | 说明 |
|------|------|------|
| 代码质量 | 6 | 简洁，但响应格式兼容处理略复杂 |
| 架构设计 | 6 | 与 GitHub Provider 结构一致 |
| 错误处理 | 5 | 基础错误捕获，无重试 |
| 测试覆盖 | 3 | 无独立测试文件 |
| 可维护性 | 7 | 代码量小 |
| 安全性 | 6 | Token 作为 query param 传递（非 header），安全性略低 |
| **综合** | **5.5** | **需补充测试** |

#### 7. GitCode Provider — `recommend/gitcode.py`（104 行）

| 维度 | 分数 | 说明 |
|------|------|------|
| 代码质量 | 4 | 存在死代码 Bug（L62-63），`parsed` 变量未定义 |
| 架构设计 | 6 | 与其他 Provider 结构一致 |
| 错误处理 | 5 | HTML 检测是亮点，但延迟指标因死代码无法记录 |
| 测试覆盖 | 3 | 无独立测试文件 |
| 可维护性 | 5 | Bug 的存在说明缺少代码审查 |
| 安全性 | 6 | Token 通过环境变量加载 |
| **综合** | **4.8** | **死代码 Bug 必须修复** |

#### 8. LLM 集成 — `recommend/llm.py`（478 行）

| 维度 | 分数 | 说明 |
|------|------|------|
| 代码质量 | 6 | 函数划分清晰（build_profile, rank, summarize） |
| 架构设计 | 7 | 通过 `llm_registry` 抽象多提供商，可扩展 |
| 错误处理 | 6 | 有 `RecommendLLMError` 但 `extract_search_queries` 降级不完整 |
| 测试覆盖 | 5 | `test_recommend_llm.py`(60行) 覆盖基础场景 |
| 可维护性 | 7 | prompt 模板与逻辑分离较好 |
| 安全性 | 6 | API Key 通过环境变量管理 |
| **综合** | **6.2** | **需修补查询改写降级路径** |

#### 9. LLM 注册表 — `llm_registry.py`（400 行）

| 维度 | 分数 | 说明 |
|------|------|------|
| 代码质量 | 7 | 清晰的注册表模式，支持 7 种 LLM 提供商 |
| 架构设计 | 8 | 模块级 provider 覆盖、自动检测、统一接口 |
| 错误处理 | 6 | 提供商不可用时有明确错误信息 |
| 测试覆盖 | 4 | 无独立测试文件 |
| 可维护性 | 8 | 新增 LLM 提供商只需添加配置 |
| 安全性 | 7 | API Key 安全管理 |
| **综合** | **6.7** | **设计良好的抽象层** |

#### 10. 深度抓取 — `recommend/deep_fetch.py`（281 行）

| 维度 | 分数 | 说明 |
|------|------|------|
| 代码质量 | 6 | 三级回退策略清晰（OpenClaw → GitHub API → URL 抓取） |
| 架构设计 | 7 | 多级降级设计合理 |
| 错误处理 | 7 | 每级回退有独立 try-except，失败信息传递完整 |
| 测试覆盖 | 5 | `test_recommend_deep_fetch.py`(51行) 基础覆盖 |
| 可维护性 | 6 | 超时传递链略复杂 |
| 安全性 | 6 | URL 校验、代理绕过 |
| **综合** | **6.2** | **降级设计是亮点** |

#### 11. 数据模型 — `recommend/models.py`（109 行）

| 维度 | 分数 | 说明 |
|------|------|------|
| 代码质量 | 8 | Pydantic v2 模型定义规范，字段文档清晰 |
| 架构设计 | 7 | 响应/请求/健康卡分层合理 |
| 错误处理 | 7 | Pydantic 自动校验 |
| 测试覆盖 | 5 | 通过 service 测试间接覆盖 |
| 可维护性 | 8 | 结构清晰，易于扩展 |
| 安全性 | 7 | 输入校验内建 |
| **综合** | **7.0** | **质量最高的模块之一** |

#### 12. 文本提取 — `recommend/text_extract.py`（77 行）

| 维度 | 分数 | 说明 |
|------|------|------|
| 代码质量 | 6 | 简洁清晰，支持 PDF/DOCX/TXT |
| 架构设计 | 6 | 单一职责，接口简单 |
| 错误处理 | 5 | PDF 解析失败返回空 + warning |
| 测试覆盖 | 3 | 无独立测试文件 |
| 可维护性 | 7 | 代码量小 |
| 安全性 | 5 | 缺少文件类型验证（MIME type check） |
| **综合** | **5.3** | **需加强安全校验和测试** |

#### 13. 全局配置 — `config.py`（420 行）

| 维度 | 分数 | 说明 |
|------|------|------|
| 代码质量 | 6 | 统一 `_get()` 函数加载环境变量 |
| 架构设计 | 6 | 集中管理但未分模块 |
| 错误处理 | 5 | 部分变量有 `max()`/`min()` 校验，部分没有 |
| 测试覆盖 | 3 | 无独立测试文件 |
| 可维护性 | 5 | 420 行单文件，变量查找不便 |
| 安全性 | 5 | 敏感变量（密钥）与普通配置混合 |
| **综合** | **5.0** | **需分模块整理** |

#### 14. Worker — `worker.py`（1930 行）

| 维度 | 分数 | 说明 |
|------|------|------|
| 代码质量 | 5 | 1930 行单文件，包含 git clone/docker build/docker run |
| 架构设计 | 5 | Celery 任务定义与业务逻辑混合 |
| 错误处理 | 7 | 任务重试机制、超时保护、WebSocket 日志推送 |
| 测试覆盖 | 6 | `test_worker_reliability.py` + `test_worker_dockerfile_discovery.py` |
| 可维护性 | 4 | 单文件过大，改动影响面大 |
| 安全性 | 6 | Docker socket 挂载有安全风险但是必要的 |
| **综合** | **5.5** | **需拆分任务模块** |

#### 15. Docker/部署 — `docker-compose.prod.yml` + `Dockerfile`

| 维度 | 分数 | 说明 |
|------|------|------|
| 代码质量 | 7 | 结构清晰，服务定义规范 |
| 架构设计 | 7 | 7 服务编排合理，依赖声明完整 |
| 错误处理 | 6 | healthcheck 定义，depends_on 条件启动 |
| 测试覆盖 | 4 | 无 CI/CD 级别的 compose 验证 |
| 可维护性 | 7 | 环境变量外化，多阶段构建 |
| 安全性 | 6 | Docker socket 挂载、密码使用环境变量 |
| **综合** | **6.2** | **生产部署配置基本完善** |

#### 16. OpenClaw 客户端 — `ingest/openclaw.py`（78 行）

| 维度 | 分数 | 说明 |
|------|------|------|
| 代码质量 | 7 | 简洁清晰的客户端封装 |
| 架构设计 | 6 | 单一职责 |
| 错误处理 | 5 | 基础异常捕获 |
| 测试覆盖 | 3 | 无独立测试文件 |
| 可维护性 | 7 | 代码量小 |
| 安全性 | 6 | API Key 通过环境变量，本地绕过代理 |
| **综合** | **5.7** | **功能简单但缺测试** |

---

### 模块评分汇总

| 排名 | 模块 | 综合分 | 行数 | 关键问题 |
|------|------|--------|------|---------|
| 1 | 数据模型 (`models.py`) | **7.0** | 109 | - |
| 2 | LLM 注册表 (`llm_registry.py`) | **6.7** | 400 | 缺测试 |
| 3 | 决策编排 (`decision/service.py`) | **6.5** | 1181 | 耦合度 |
| 4 | 搜索核心 (`recommend/service.py`) | **6.5** | 1316 | 权重硬编码 |
| 5 | GitHub Provider (`github.py`) | **6.3** | 132 | 无重试 |
| 6 | LLM 集成 (`recommend/llm.py`) | **6.2** | 478 | 降级盲区 |
| 7 | 深度抓取 (`deep_fetch.py`) | **6.2** | 281 | - |
| 8 | Docker/部署 | **6.2** | — | — |
| 9 | OpenClaw 客户端 (`openclaw.py`) | **5.7** | 78 | 缺测试 |
| 10 | Gitee Provider (`gitee.py`) | **5.5** | 86 | 缺测试、无重试 |
| 11 | Worker (`worker.py`) | **5.5** | 1930 | 单文件过大 |
| 12 | API 层 (`main.py`) | **5.3** | 5921 | 单文件过大 |
| 13 | 文本提取 (`text_extract.py`) | **5.3** | 77 | 缺安全校验 |
| 14 | 全局配置 (`config.py`) | **5.0** | 420 | 未分模块 |
| 15 | GitCode Provider (`gitcode.py`) | **4.8** | 104 | 死代码 Bug |
| 16 | 前端 (`App.tsx`) | **4.2** | 7457 | 单体组件 |

**项目平均分：5.9 / 10**

---

### 模块评分雷达图（文本版）

```
                    代码质量
                       10
                        │
                   8    │
                        │
            安全性 ──── 6 ──── 架构设计
                  ╱     │     ╲
                4       │       4
               ╱        │        ╲
         可维护性 ────── 2 ────── 错误处理
                        │
                    测试覆盖

  整体弱项：测试覆盖(4.3)、可维护性(5.5)
  整体强项：安全性(6.3)、错误处理(6.1)
```

---

## 三、TODO 清单

### P0 — 紧急修复（影响正确性）

- [ ] **修复 GitCode 死代码 Bug** — `recommend/gitcode.py:52-63`
  - 将 `return json.loads(raw)` 改为 `parsed = json.loads(raw)`
  - 将 `record_timing_metric` 和 `return parsed` 移到 `try/except` 之后的正常流程中
  - 预计工作量：30 分钟

### P1 — 高优先级（影响可靠性）

- [ ] **为 Provider 添加 HTTP 重试** — `recommend/github.py`, `gitee.py`, `gitcode.py`
  - 实现简单的指数退避重试（最多 2 次重试，初始间隔 1 秒）
  - 只对 5xx 和连接超时重试，不对 4xx 重试
  - 预计工作量：2 小时

- [ ] **修复 deep 模式查询改写降级盲区** — `recommend/llm.py:302-305`
  - 当 LLM 不可用时，`extract_search_queries()` 应降级为关键词组合而非抛出异常
  - 修改 `recommend/service.py:967-969` 中的空结果返回为降级关键词搜索
  - 预计工作量：1 小时

- [ ] **实现跨平台去重** — `recommend/service.py:1008-1014`
  - 添加基于 `full_name`（不含 source 前缀）的二次去重逻辑
  - 相同仓库优先保留 GitHub 版本（stars 数据更可靠）
  - 预计工作量：1 小时

### P2 — 中优先级（影响可维护性）

- [ ] **补充 `.env.example` 文件** — `.env.local.example`, `.env.prod.example`
  - 添加 `GITHUB_TOKEN`, `GITEE_TOKEN`, `GITCODE_TOKEN`（注释说明用途）
  - 添加 `OPENCLAW_BASE_URL`, `RECOMMEND_PROVIDER_TIMEOUT_SECONDS`
  - 添加 `RECOMMEND_TOP_K`, `DEEP_SEARCH_POINTS_COST`
  - 预计工作量：30 分钟

- [ ] **前端 App.tsx 拆分** — `frontend/src/App.tsx`（7457 行）
  - 按页面/路由拆分为独立组件
  - 引入 React Router 管理路由
  - 提取搜索相关状态到 custom hook
  - 预计工作量：3-5 天

- [ ] **后端 main.py 拆分** — `main.py`（5921 行）
  - 按领域拆分为 FastAPI Router（auth、billing、recommend、case、admin）
  - 提取中间件到独立文件
  - 预计工作量：2-3 天

- [ ] **Worker 拆分** — `worker.py`（1930 行）
  - 将 git clone、docker build、docker run 拆分为独立模块
  - Celery 任务定义与业务逻辑分离
  - 预计工作量：2 天

### P3 — 低优先级（影响扩展性/开发体验）

- [ ] **补充缺失模块测试**
  - `recommend/gitee.py` — 添加独立测试文件
  - `recommend/gitcode.py` — 添加独立测试文件
  - `recommend/text_extract.py` — 添加独立测试文件
  - `llm_registry.py` — 添加独立测试文件
  - `ingest/openclaw.py` — 添加独立测试文件
  - `config.py` — 添加配置校验测试
  - 预计工作量：3-4 天

- [ ] **打分权重外化为配置** — `recommend/service.py`
  - 将 `0.52/0.18/0.10/0.20` 等权重提取到 `config.py`
  - 支持通过环境变量调整
  - 预计工作量：1 小时

- [ ] **Provider HTTP 客户端升级**
  - 将 `urllib.request` 替换为 `httpx`（已是项目依赖）
  - 考虑未来引入异步 HTTP 调用
  - 预计工作量：2-3 天

- [ ] **Config 分模块** — `config.py`（420 行）
  - 按领域拆分为 `config/auth.py`, `config/billing.py`, `config/recommend.py` 等
  - 预计工作量：1 天

- [ ] **文本提取安全加固** — `recommend/text_extract.py`
  - 添加 MIME type 校验（不仅依赖文件扩展名）
  - 添加文件内容安全检查
  - 预计工作量：2 小时

---

### TODO 时间线建议

```
Week 1:  P0（GitCode Bug） + P1（重试 / LLM 降级 / 跨平台去重）
Week 2:  P2（env 文档 + 前端拆分启动）
Week 3-4: P2（main.py 拆分 + worker 拆分）
Week 5+: P3（测试补充 + 架构升级）
```

---

### 搜索系统整体健康度

```
┌─────────────────────────────────────────────┐
│         搜索系统健康度评估                     │
│                                              │
│  功能完整性  ████████░░  80%  核心功能完备     │
│  可靠性      ██████░░░░  60%  缺重试/降级     │
│  可维护性    █████░░░░░  50%  单文件过大       │
│  测试覆盖    ████░░░░░░  43%  多模块缺测试     │
│  安全性      ██████░░░░  63%  基础防护到位     │
│  性能/扩展   █████░░░░░  55%  同步阻塞/线程    │
│  文档/配置   █████░░░░░  50%  env 文档缺失     │
│                                              │
│  综合健康度  █████░░░░░  57%                  │
│                                              │
│  评级：C+（可用但需持续改进）                   │
└─────────────────────────────────────────────┘
```

---

*审查报告结束。建议优先处理 P0/P1 项目以提升系统可靠性。*
