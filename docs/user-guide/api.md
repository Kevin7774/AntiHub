# API 使用示例

以下示例以本地 `http://localhost:8010` 为例。

## 创建 Case
```bash
curl -s -X POST http://localhost:8010/cases \
  -H "Content-Type: application/json" \
  -d '{
    "repo_url": "/path/to/your/repo",
    "ref": "main",
    "env": {"OPENAI_API_KEY": "***"}
  }'
```

可选字段（常用）：
- `run_mode`：`auto` / `container` / `showcase` / `compose`（默认 auto）
- `dockerfile_path`：显式 Dockerfile 路径
- `compose_file`：显式 docker compose 文件路径（如 `pwd.yml`）
- `context_path`：显式构建上下文
- `build`：构建参数（network/no_cache/build_args）

示例（强制容器）：
```bash
curl -s -X POST http://localhost:8010/cases \
  -H "Content-Type: application/json" \
  -d '{
    "repo_url": "https://github.com/org/repo",
    "run_mode": "container"
  }'
```

示例（自动）：
```bash
curl -s -X POST http://localhost:8010/cases \
  -H "Content-Type: application/json" \
  -d '{
    "repo_url": "https://github.com/org/repo",
    "run_mode": "auto"
  }'
```

## 查询状态
```bash
curl -s http://localhost:8010/cases/{case_id}
```
返回字段新增：
- `analyze_status`：`PENDING|RUNNING|FINISHED|FAILED`
- `report_ready`：是否已生成 Explain 报告
- `visual_status`：`NOT_STARTED|PENDING|RUNNING|SUCCESS|PARTIAL|FAILED`
- `visual_ready`：是否已生成 Visuals

## 实时日志（WebSocket）
```bash
wscat -c ws://localhost:8010/ws/logs/{case_id}
```
日志为 JSON 行：`{ts, stream, level, line}`。

## 触发说明书
```bash
curl -s -X POST http://localhost:8010/cases/{case_id}/manual
```

## 查看说明书
```bash
curl -s http://localhost:8010/cases/{case_id}/manual
```

## 触发 Explain（自动说明书）
```bash
curl -s -X POST http://localhost:8010/cases/{case_id}/analyze \
  -H "Content-Type: application/json" \
  -d '{"force": false, "mode": "light"}'
```

## 查看 Explain 报告
```bash
curl -s http://localhost:8010/cases/{case_id}/report
```

## 触发 Visualize（可视化资产包）
```bash
curl -s -X POST http://localhost:8010/cases/{case_id}/visualize \
  -H "Content-Type: application/json" \
  -d '{"force": false}'
```

## 触发 Visualize 视频渲染
```bash
curl -s -X POST http://localhost:8010/cases/{case_id}/visualize/video \
  -H "Content-Type: application/json" \
  -d '{"force": false}'
```

## 查看 Visuals 资产
```bash
curl -s http://localhost:8010/cases/{case_id}/visuals
```

## 下载 Visuals 文件
```bash
curl -s http://localhost:8010/cases/{case_id}/visuals/{filename}
```

## Visualize 说明
- 资产缓存以 `repo_url + commit_sha + template_version` 为维度，命中缓存会直接返回。
- 资产默认存储在 `.antihub/reports/{repo_slug}/{commit_sha}-{template_version}/` 目录。
- 需要 `mmdc` 渲染 Mermaid（未安装会导致渲染失败）。
- MiniMax 配置读取环境变量：`VISUAL_API_KEY` / `VISUAL_BASE_URL` / `VISUAL_IMAGE_MODEL`。
- Remotion 视频渲染依赖 Node.js 与 Remotion CLI；可通过 `VISUAL_RENDER_WEBM=true` 生成 webm。
- OpenClaw `github.fetch` 为必需：配置 `OPENCLAW_BASE_URL` / `OPENCLAW_API_KEY`。\n+  - skill 契约文档：`docs/openclaw/github_fetch.md`。

## Explain 说明
- 报告生成会复用缓存：同 `repo_url + commit_sha` 将直接命中缓存。
- Mermaid 校验优先使用 `mmdc`（mermaid-cli）；若不可用将降级为语法校验。
- 报告存储默认路径：`.antihub/reports/{repo_slug}/{commit_sha}.json`。
- LLM 配置读取环境变量：`OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_API_MODEL`。

### Mermaid 校验依赖
```bash
npm i -g @mermaid-js/mermaid-cli
mmdc --version
```

### Report 清理策略
- 默认存储在 `.antihub/reports/`，可定期删除旧的 `repo_slug` 目录或 JSON 文件。
- 也可以通过环境变量 `REPORT_ROOT` 指定独立的存储路径并自行清理。

## 管理动作（Stop/Restart/Retry/Archive）
```bash
curl -s -X POST http://localhost:8010/cases/{case_id}/stop
curl -s -X POST http://localhost:8010/cases/{case_id}/restart
curl -s -X POST http://localhost:8010/cases/{case_id}/retry
curl -s -X POST http://localhost:8010/cases/{case_id}/archive
```
