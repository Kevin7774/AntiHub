# Phase 2 前端交互与后端对齐验收报告

## 1. 概述 / 范围 / 结论
- 验收范围：创建 case -> build/run -> WS 实时日志 -> 状态与运行信息 -> ENV 注入安全，新增前端控制台与后端接口契约对齐。
- 结论：PASS（前后端对齐完成，WS 回放+订阅稳定，接口契约与 OpenAPI 同步；端口池冲突属于已知问题，详见第 6 节）。

## 2. 启动与基线确认
- 启动：`./dev.sh up`
- /docs：`http://127.0.0.1:8010/docs` -> 200
- /openapi.json：字段已更新（见第 3 节摘要）
- Phase1 基线：完成创建 case、WS 日志回放、状态查询（示例：`c_536b9e`）

## 3. 接口契约（以 OpenAPI 为准）

### 3.1 CaseCreateRequest
- 字段：`git_url | repo_url`（二选一）、`ref | branch`（二选一）、`env`、`auto_analyze`、`container_port`
- 说明：
  - `git_url` / `repo_url` 兼容输入；内部统一为 `repo_url`
  - `ref` / `branch` 兼容输入；内部统一为 `ref`
  - `auto_analyze` 默认 `false`
  - `env` 仅用于容器注入，不持久化 value（只保留 `env_keys`）

### 3.2 CaseResponse / CaseStatusResponse
- 必备字段：`case_id`, `status`, `stage`, `commit_sha`, `runtime`, `env_keys`, `error_code`, `error_message`, `created_at`, `updated_at`
- 运行信息：`runtime = { container_id?, host_port?, access_url? }`（含 started/exited/exit_code）
- 兼容字段：`repo_url`, `ref`, `branch`, `container_port`, `container_id`, `host_port`, `access_url`, `image_tag`, `last_log_at`

### 3.3 状态/阶段定义
- `status`：`PENDING/CLONING/BUILDING/STARTING/RUNNING/FAILED/FINISHED`（保持现有实现）
- `stage`：`clone/build/run/analyze/system`（已统一为小写）

### 3.4 WS 日志格式
- JSON 行：`{"ts":<epoch_seconds>,"stream":"build|run|analyze|system","level":"INFO|ERROR","line":"..."}`
- 机制：WS 连接后先回放 Redis List，再订阅 Redis Pub/Sub

### 3.5 跨域与依赖
- CORS：允许 `http://127.0.0.1:5173` / `http://localhost:5173`
- 依赖：`websockets` 已加入 `requirements.txt`

## 4. 前端功能清单（frontend/）
- Create Case 页面
  - 表单：`repo_url`、`ref`、`env`（可增删行）、`auto_analyze`
  - 提交后展示 `case_id` 并跳转到 `/cases/:id`
- Case Detail 页面
  - 状态 Badge + stage + 更新时间
  - Runtime 卡片：`access_url`（点击/复制）、`host_port`、`container_id`
  - Env Keys 卡片：仅显示 key
  - 错误卡片：`FAILED` 时显示 `error_code`、`error_message` + 可解释文案
  - 日志面板：WS 回放+实时、自动滚动/暂停、按 stream 过滤、断线提示与一键重连
  - 底部状态刷新按钮 + 轮询（2s，RUNNING/FAILED 后降频 5s）
- 安全：不写入 localStorage，不打印 env value，不在页面显示明文值
- 构建验证：`npm run build` 通过

## 5. 联调用例证据（2 个）

### 用例 1：轻量仓 hello-world（RUNNING）
- 创建请求（env value 已打码）：
```json
{
  "repo_url": "/home/user2643/code/AntiHub/test-repos/hello-world",
  "ref": "master",
  "env": {"FOO": "***"},
  "auto_analyze": false
}
```
- 创建响应：`case_id = c_db5ef1`（秒回）
- WS 日志（摘录）：`.devlogs/phase2_case1_ws.jsonl`
```json
{"ts":1768981005.8472288,"stream":"system","level":"INFO","line":"Cloning repo /home/user2643/code/AntiHub/test-repos/hello-world (ref master)..."}
{"ts":1768981006.0443277,"stream":"build","level":"INFO","line":"Step 1/5 : FROM nikolaik/python-nodejs:python3.9-nodejs20-slim"}
```
- 状态响应（摘录）：`.devlogs/phase2_case1_status.json`
```json
{"case_id":"c_db5ef1","status":"RUNNING","stage":"run","runtime":{"host_port":30000,"access_url":"http://localhost:30000"},"env_keys":["FOO"],"error_code":null}
```
- 访问验证：`curl http://localhost:30000/` -> `HTTP/1.0 200 OK`
- 前端页面行为：
  - 状态卡：RUNNING / stage=run / 更新时间显示
  - Runtime 卡：access_url 可点击与复制
  - Env Keys：展示 `FOO`
  - 日志面板：build/system 实时日志可见、支持过滤与暂停
- 结论：PASS

### 用例 2：MetaGPT 场景仓（RUNNING）
- 创建请求（env value 已打码）：
```json
{
  "repo_url": "/home/user2643/code/AntiHub/test-repos/metagpt-min",
  "ref": "master",
  "env": {"OPENAI_API_KEY": "sk-****"},
  "auto_analyze": false
}
```
- 创建响应：`case_id = c_c6547a`（秒回）
- WS 日志（摘录）：`.devlogs/phase2_case2_ws.jsonl`
```json
{"ts":1768981050.0024369,"stream":"system","level":"INFO","line":"Cloning repo /home/user2643/code/AntiHub/test-repos/metagpt-min (ref master)..."}
{"ts":1768981050.195938,"stream":"build","level":"INFO","line":"Step 1/5 : FROM nikolaik/python-nodejs:python3.9-nodejs20-slim"}
```
- 状态响应（摘录）：`.devlogs/phase2_case2_status.json`
```json
{"case_id":"c_c6547a","status":"RUNNING","stage":"run","runtime":{"host_port":30000,"access_url":"http://localhost:30000"},"env_keys":["OPENAI_API_KEY"],"error_code":null}
```
- 访问验证：`curl http://localhost:30000/` -> `HTTP/1.0 200 OK`
- 前端页面行为：
  - 状态卡：RUNNING / stage=run
  - Env Keys：展示 `OPENAI_API_KEY`
  - 日志面板：build/system 可见，支持过滤与暂停
- 结论：PASS

## 6. 发现问题与修复记录（含 patch）
1) WebSocket 依赖缺失
- 修复：`requirements.txt` 加入 `websockets==12.0`

2) 接口契约对齐
- 修复：新增 `CaseCreateRequest`/`CaseResponse` 字段与默认值，补齐 `runtime`、`env_keys`、`error_code`、`created_at/updated_at`，统一 `stage` 枚举；OpenAPI 同步更新

3) CORS/前端联调
- 修复：后端允许 `http://127.0.0.1:5173` / `http://localhost:5173`

4) 前端控制台
- 修复：新增 `frontend/`（Vite + React + TS），支持创建、详情、WS 日志与可视化状态

5) 端口池冲突（已知问题）
- 现象：若已有容器占用 30000，新的 case 会在启动阶段报错 `port is already allocated`
- 本次处理：停用旧容器释放端口后重试（对业务逻辑无侵入）

Patch：`phase2.patch`
- 应用方式（项目根目录）：`patch -p0 < phase2.patch`

## 7. 启动说明
- 后端：`./dev.sh up`
- 前端：`cd frontend && npm i && npm run dev`
- 并发启动（可选）：开两终端分别执行上述两条命令
## 7.1 前端操作教程（快速上手）
1) 启动前端：`cd frontend && npm i && npm run dev`，浏览器打开 `http://127.0.0.1:5173`  
2) 创建案例：在“创建案例”页填写 `repo_url`、可选 `ref`，按需添加 env（只填 key 或打码 value），点击“创建案例”  
3) 查看详情：提交后自动跳转到 `/cases/:id`，顶部显示状态与阶段  
4) 运行信息：可点击访问 `access_url`，点击“复制”复制地址  
5) 日志面板：支持按 `构建/运行/分析/系统` 过滤，支持“暂停滚动 / 自动滚动”切换  
6) 断线处理：若 WS 断开，面板提示“已断开”，点击“重连”恢复  
7) 状态刷新：点击“刷新状态”立即拉取；系统也会自动轮询

## 8. 下一步（Phase 3）建议
1) Case 列表/检索与生命周期管理（停止/重启/清理）
2) 项目维度视图（按 repo/branch 统计与对比）
3) 环境变量与凭据管理（仅 key 可见，支持模板与复用）
4) 运行观测（资源用量、容器健康、失败聚类）
5) 多租户/权限控制与审计日志