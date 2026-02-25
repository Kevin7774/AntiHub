# E2E 重新测试与 Manual 差异化修复报告

## 1) 时间与环境
- 时间：2026-01-23
- 后端：`./dev.sh up`（FastAPI + Celery + Redis）
- 前端：`cd frontend && npm run dev`

## 2) E2E 重新测试（修复前基线）
> 目的：验证现状流程可复现，为说明书差异化修复提供对照。

### Repo1：wechat-miniprogram-card（远程）
- 创建请求：`{"repo_url":"https://github.com/Kevin7774/wechat-miniprogram-card","ref":"auto"}`
- case_id：`c_2acf73`
- 状态：`FAILED` / `DOCKERFILE_MISSING`
- 关键日志：见 `.devlogs/rerun_pre_repo1_logs.json`
- 说明书：已生成（但内容偏模板化）

### Repo2：hello-world（本地）
- 创建请求：`{"repo_url":"/home/user2643/code/AntiHub/test-repos/hello-world"}`
- case_id：`c_f4ab8b`
- 状态：`RUNNING` / access_url=`http://localhost:30000`
- 访问验证：`curl http://localhost:30000` -> 200
- 关键日志：见 `.devlogs/rerun_pre_repo2_logs.json`

## 3) Manual 差异化修复摘要
- 读取信号：README 标题/摘要/列表、目录结构、配置文件、Dockerfile、package.json scripts、Python 入口。
- 生成内容：1-liner/Features/Quickstart/Config/Tree/Mermaid 均由信号驱动。
- Meta 增强：`repo_fingerprint`、`similarity_score`、`warnings`。
- 日志增强：manual generation started/finished + time_cost_ms + warnings。

## 4) E2E 重新测试（修复后）
> 目的：验证 clone/build/run/日志/状态/说明书生成与前端渲染。

### Repo1：wechat-miniprogram-card（远程）
- 创建请求：`{"repo_url":"https://github.com/Kevin7774/wechat-miniprogram-card","ref":"auto"}`
- case_id：`c_5f131e`
- 状态：`FAILED` / `DOCKERFILE_MISSING`（仓库无 Dockerfile，属于预期外但可解释失败）
- WS 关键日志（摘录）：
  - `Cloning repo ... (ref auto)`
  - `Auto detected default branch 'main'.`
  - `Clone completed`
  - `Manual generation started`
  - `Manual generation finished time_cost_ms=2 warnings=none`
  - 证据：`.devlogs/rerun_post2_repo1_ws.json` / `.devlogs/rerun_post2_repo1_logs.json`
- 说明书 Meta：`repo_fingerprint=b47f2c3ce7`，`similarity_score=0.087`，`warnings=[]`
- Manual 渲染（前端复现）：
  1) 打开前端控制台 → 进入 case 详情
  2) 切换到 Manual Tab
  3) 可见 Markdown + Mermaid 渲染正常（依赖 `app.json/pages` 等信号）

### Repo2：hello-world（本地）
- 创建请求：`{"repo_url":"/home/user2643/code/AntiHub/test-repos/hello-world"}`
- case_id：`c_8f8143`
- 状态：`RUNNING` / access_url=`http://localhost:30000`
- 访问验证：`curl http://localhost:30000` -> 200
- WS 关键日志（摘录）：
  - `Cloning repo ... (ref auto)`
  - `Clone completed`
  - `Manual generation started`
  - `Manual generation finished time_cost_ms=0 warnings=none`
  - 证据：`.devlogs/rerun_post2_repo2c_ws.json` / `.devlogs/rerun_post2_repo2c_logs.json`
- 说明书 Meta：`repo_fingerprint=5e4f7e31d8`，`similarity_score=0.119`，`warnings=[]`
- Manual 渲染（前端复现）：
  1) 打开前端控制台 → 进入 case 详情
  2) Manual Tab 渲染 Dockerfile EXPOSE/CMD 与 Mermaid

## 5) Manual 差异化证据（Repo1 vs Repo2）

### 5.1 一句话简介（1-liner）
- Repo1：`张载德电子名片 - 微信小程序...支持中英双语切换`（来源 README 标题+摘要）
- Repo2：`hello-world：基于 Docker...支持容器化运行`（来源 Dockerfile/目录结构）

### 5.2 Features（含 evidence 注释）
- Repo1 示例（README + 小程序信号）：
  - `✅ 完整的个人信息展示... (evidence: README)`
  - `小程序全局配置与页面路由 (evidence: app.json/pages)`
- Repo2 示例（Dockerfile + 目录信号）：
  - `容器暴露端口 8000 (evidence: Dockerfile EXPOSE)`
  - `包含关键路径 index.html... (evidence: index.html)`

### 5.3 Quickstart
- Repo1：提供“微信开发者工具导入目录 / project.config.json appid”步骤
- Repo2：提供 `docker build` / `docker run -p 8080:8000`（EXPOSE 推断）

### 5.4 Config
- Repo1：未检测到显式 env 示例 → 提示补充 `.env.example`
- Repo2：端口来自 Dockerfile EXPOSE=8000，env_keys 为空（提示补充）

### 5.5 Tree & Key Paths
- Repo1：解释 `app.json` / `project.config.json` / `pages/`
- Repo2：解释 `Dockerfile` / `index.html` / `README`

### 5.6 Mermaid（非固定模板）
- Repo1：`用户 -> 小程序(app.json) -> pages/`（含项目真实路径）
- Repo2：`用户 -> index.html -> 端口 8000`（含 Docker EXPOSE）

### 5.7 同质化检测（B7）
- Manual meta similarity_score：Repo1=0.087 / Repo2=0.119（对模板基线）
- 两仓库互相相似度（Jaccard）：0.428（< 0.75）
- 结论：内容已显著区分且信号关联强

## 6) 结论
- 重新测试完成：Repo1 clone/日志/说明书 OK（Dockerfile 缺失导致 build/run 失败，属仓库问题）；Repo2 成功 RUNNING 且可访问。
- Manual 已从“模板化”改为“证据驱动”，1-liner/features/quickstart/mermaid 均与仓库信号强相关。

## 7) 备注
- 端口冲突曾发生（30000 被既有容器占用），通过停止带 `antihub.managed=true` 标签容器释放端口后恢复。
