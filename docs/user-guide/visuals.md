# Visual Pack 一键生成视频

## 前置条件
- Node.js 18+
- Remotion 依赖（项目内 `visualize/remotion`）
- OpenClaw 控制平面（用于 `github.fetch`）

安装 Remotion 依赖（首次）：
```bash
cd visualize/remotion
npm install
```

启动 OpenClaw（本地 mock，可选）：
```bash
python3 -m openclaw.server
```

环境变量示例：
```bash
export OPENCLAW_BASE_URL=http://127.0.0.1:8787
```

## 一键生成 Visual Pack（含视频）
```bash
curl -s -X POST http://localhost:8010/cases/{case_id}/visualize \
  -H "Content-Type: application/json" \
  -d '{"force": false}'
```

## 单独触发视频渲染
```bash
curl -s -X POST http://localhost:8010/cases/{case_id}/visualize/video \
  -H "Content-Type: application/json" \
  -d '{"force": false}'
```

## 查看结果
```bash
curl -s http://localhost:8010/cases/{case_id}/visuals
```

返回资产包含：
- `visual_pack.mp4`（默认）
- 可选 `visual_pack.webm`（设置 `VISUAL_RENDER_WEBM=true`）
- `repo_index.json` / `spotlights.json` / `storyboard.json`

## Demo 脚本（推荐）
```bash
scripts/demo_visualize.sh https://github.com/owner/repo
```

输出：
- `visual_output/repo_index.json`
- `visual_output/spotlights.json`
- `visual_output/storyboard.json`
- `visual_output/output.mp4`

## 常见问题
- 若提示 `VISUALIZE_RENDER_FAILED`，请检查 Node.js 与 Remotion CLI。
- 大仓库建议调小 `VISUAL_TREE_DEPTH` / `VISUAL_TREE_MAX_ENTRIES` 以缩短耗时。
- 若提示 `GITHUB_RATE_LIMIT`，请配置 `GITHUB_TOKEN`。
