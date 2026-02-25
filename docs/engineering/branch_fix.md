# 分支自动识别修复报告

## 1. 问题概述
- 现象：用户输入 Git URL（如 `https://github.com/Kevin7774/wechat-miniprogram-card`）创建 case 时，默认 ref/branch=master，导致 clone 失败。
- 影响：主流仓库默认分支已迁移为 main，导致大量误报 `GIT_CLONE_FAILED`。

## 2. 根因
- 后端默认 ref/branch 直接使用 `DEFAULT_BRANCH`（旧值 master 的等价行为），未对远端默认分支进行探测。

## 3. 修复点
- 后端：新增默认分支探测（`git ls-remote --symref <url> HEAD`），支持 `ref` 为空/`auto` 自动识别。
- 后端：当用户显式传 `master/main` 且远端不存在时，探测默认分支并自动 fallback 一次，日志注明行为。
- 后端：新增错误码 `GIT_REF_NOT_FOUND`，错误信息包含 requested_ref / default_branch / suggestion / heads（最多 20）。
- 前端：ref 输入框默认留空，placeholder 提示“留空自动识别默认分支（常见 main/master）”。
- OpenAPI：`ref/branch` 字段说明更新为“可选，留空或 auto 自动识别”。

## 4. 关键代码位置
- `docker_ops.py`: `detect_default_branch`, `list_remote_heads`, `clone_repo`（自动识别 + fallback + 友好错误）
- `worker.py`: `build_and_run`, `generate_manual_task`, `classify_error`（新增错误码 + fallback 日志）
- `main.py`: `CaseCreateRequest`, `create_case`, `handle_case_action(retry)`（ref 可选/auto）
- `errors.py`: `GIT_REF_NOT_FOUND`
- `frontend/src/App.tsx`: ref placeholder + `ERROR_EXPLAIN`

## 5. 回归测试（已执行）
> 使用仓库：`https://github.com/Kevin7774/wechat-miniprogram-card`

### 场景 A：不传 ref（自动识别）
- 创建请求：`{"repo_url":"https://github.com/Kevin7774/wechat-miniprogram-card"}`
- case_id：`c_d642d3`
- 关键日志：
  - `Cloning repo ... (ref auto)`
  - `Auto detected default branch 'main'.`
  - `Clone completed`
- 证据：`.devlogs/branch_fix2_a_logs.json`、`.devlogs/branch_fix2_a_status.json`
- 结果：clone 成功；后续因仓库无 Dockerfile 进入 `DOCKERFILE_MISSING`（预期外但与本修复无关）。

### 场景 B：传 ref=master（fallback 到 main）
- 创建请求：`{"repo_url":"https://github.com/Kevin7774/wechat-miniprogram-card","ref":"master"}`
- case_id：`c_c2455f`
- 关键日志：
  - `Cloning repo ... (ref master)`
  - `Requested ref 'master' not found; fallback to 'main'.`
  - `Clone completed`
- 证据：`.devlogs/branch_fix2_b_logs.json`、`.devlogs/branch_fix2_b_status.json`
- 结果：clone 成功；后续因仓库无 Dockerfile 进入 `DOCKERFILE_MISSING`。

### 场景 C：传 ref=nonexistent-branch（应失败）
- 创建请求：`{"repo_url":"https://github.com/Kevin7774/wechat-miniprogram-card","ref":"nonexistent-branch"}`
- case_id：`c_19264b`
- 关键响应：`error_code=GIT_REF_NOT_FOUND`
- error_message 摘要：包含 `requested_ref=nonexistent-branch`, `default_branch=main`, `suggestion=use 'main' or leave ref empty/auto`, `heads=main`
- 证据：`.devlogs/branch_fix2_c_status.json`、`.devlogs/branch_fix2_c_logs.json`
- 结果：PASS（错误码与提示符合要求）

## 6. 结论
- 分支自动识别与 fallback 行为完成，错误码与提示明确可解释。
- 前端默认不再写死 master，提升新仓库兼容性。

## 7. 应用与回滚（无 .git）
- 应用：`patch -p0 < branch_fix.patch`
- 回滚：`patch -R -p0 < branch_fix.patch`
