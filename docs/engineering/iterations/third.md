# Phase 3 可展示并且管理验收报告

## 1. 概述 / 范围 / 结论
- 验收范围：Dashboard 列表管理、搜索/筛选/分页、详情管理动作（Stop/Restart/Retry/Archive）、日志导出、OpenAPI 对齐、Demo 脚本与 E2E 证据。
- 结论：PASS（管理 API + 前端控制台闭环完成，可用于演示与基础运维操作）。

## 2. 新增能力清单
- Dashboard（/）：列表管理、搜索、状态筛选、分页、归档可见控制。
- 详情页管理动作：Stop / Restart / Retry / Archive（均带二次确认与结果提示）。
- 日志管理：历史日志查询 + JSONL 下载导出。
- 容器可识别性：所有容器打 label（`antihub.case_id`、`antihub.managed=true`），动作只操作带 label 的容器。

## 3. 管理 API 契约（OpenAPI 可见）

### 3.1 列表查询
- `GET /cases?q=&status=&stage=&page=&size=&include_archived=`
- 返回：`items[] + {total,page,size}`
- 示例（节选）：`.devlogs/phase3_cases_page1.json`
```json
{
  "items": [
    {
      "case_id": "c_95d61d",
      "status": "RUNNING",
      "stage": "run",
      "repo_url": "/home/user2643/code/AntiHub/test-repos/hello-world",
      "ref": "master",
      "runtime": {"host_port":30000,"access_url":"http://localhost:30000"},
      "updated_at": 1769088996.6860673
    }
  ],
  "total": 1,
  "page": 1,
  "size": 5
}
```

### 3.2 管理动作
- `POST /cases/{id}/stop`
- `POST /cases/{id}/restart`
- `POST /cases/{id}/retry`（可带 `env` 覆盖）
- `POST /cases/{id}/archive`
- 示例（Stop）：`.devlogs/phase3_case1_stop.json`
```json
{"case_id":"c_95d61d","action":"stop","status":"STOPPED","message":"Stopped"}
```
- 示例（Restart）：`.devlogs/phase3_case1_restart.json`
```json
{"case_id":"c_95d61d","action":"restart","status":"RUNNING","message":"Restarted"}
```
- 示例（Retry）：`.devlogs/phase3_case3_retry.json`
```json
{"case_id":"c_a88c95","action":"retry","status":"PENDING","message":"Retry started"}
```
- 示例（Archive）：`.devlogs/phase3_case2_archive.json`
```json
{"case_id":"c_d2be0f","action":"archive","status":"ARCHIVED","message":"Archived"}
```

### 3.3 日志查询/导出
- `GET /cases/{id}/logs?limit=1000&offset=0&format=jsonl`
- `GET /cases/{id}/logs/download?limit=1000`（浏览器下载）
- 示例（JSONL 导出）：`.devlogs/phase3_case1_logs.jsonl`
```json
{"ts": 1769088996.689066, "stream": "system", "level": "INFO", "line": "Container started: http://localhost:30000"}
```

## 4. Demo Script（5 分钟）
1) 打开控制台 `http://127.0.0.1:5173`，进入 Dashboard
2) 新建案例：填写 `repo_url` + 可选 `ref` + env（只填 key 或打码 value）
3) 返回列表：看到新 case 出现在顶部，状态变化可见
4) 进入详情：查看状态/阶段、运行信息、env_keys、实时日志
5) 点击 Stop：状态变为 STOPPED（提示成功）
6) 点击 Restart：状态恢复 RUNNING
7) 点击“下载 1000 行”：导出日志 JSONL
8) 点击 Archive：列表默认隐藏；切换“显示归档”可再次看到

## 5. E2E 用例证据

### 用例 1：轻量仓 hello-world（RUNNING + Stop/Restart + 日志导出）
- 创建响应：`.devlogs/phase3_case1_create.json`（`case_id = c_95d61d`）
- 列表可见：`.devlogs/phase3_cases_page1.json`
- 运行状态：`.devlogs/phase3_case1_status.json`
- 访问验证：`curl http://localhost:30000/` -> `HTTP/1.0 200 OK`
- Stop：`.devlogs/phase3_case1_stop.json`，状态：`.devlogs/phase3_case1_stopped_status.json`
- Restart：`.devlogs/phase3_case1_restart.json`，状态：`.devlogs/phase3_case1_restart_status.json`
- 日志导出：`.devlogs/phase3_case1_logs.jsonl`
- 结论：PASS

### 用例 2：MetaGPT 场景（RUNNING）
- 创建响应：`.devlogs/phase3_case4_create.json`（`case_id = c_f02478`）
- 状态响应：`.devlogs/phase3_case4_status.json`（`RUNNING`，`env_keys=["OPENAI_API_KEY"]`）
- 访问验证：`curl http://localhost:30000/` -> `HTTP/1.0 200 OK`
- 结论：PASS

### 用例 3：失败场景 + Retry + Archive
- 失败构建：`.devlogs/phase3_case3_status.json`（`error_code=DOCKER_BUILD_FAILED`）
- Retry：`.devlogs/phase3_case3_retry.json` 与 `.devlogs/phase3_case3_retry_status.json`（attempt=2，env_keys 保留）
- Archive：`.devlogs/phase3_case2_archive.json`；列表默认隐藏 `.devlogs/phase3_cases_no_archived.json`；include_archived 显示 `.devlogs/phase3_cases_with_archived.json`
- 结论：PASS

## 6. 问题清单与修复记录
1) 管理动作需要可识别容器
- 修复：新增容器 label（`antihub.case_id` / `antihub.managed=true`），仅对带 label 容器执行 stop/restart

2) Retry 的 env_keys 展示
- 修复：当 Retry 未提供 env 时，沿用已有 env_keys（不存 env value）

3) 日志导出
- 修复：新增 JSONL 导出与下载接口，并在前端提供按钮

Patch：`phase3.patch`
- 应用：`patch -p0 < phase3.patch`
- 回滚：`patch -p0 -R < phase3.patch`

## 7. 启动说明
- 后端：`./dev.sh up`
- 前端：`cd frontend && npm i && npm run dev`
- 说明：如在 WSL 环境，请使用 WSL 内部 Node/npm（避免 Windows npm 访问 UNC 路径失败）

## 8. Phase 4 建议
1) 列表性能与索引（Redis 索引/分页优化）
2) 多项目视角与角色权限
3) 日志与指标聚合（服务级别状态视图）
4) 长任务治理（重试策略、队列可视化）
5) 容器生命周期与资源配额管理
