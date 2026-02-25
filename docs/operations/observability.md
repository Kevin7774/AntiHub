# 可观测性

## 日志来源
- WebSocket：`/ws/logs/{case_id}` 实时日志 + 回放
- API：`GET /cases/{id}/logs`（历史日志）
- 本地文件：`.devlogs/api.log` / `.devlogs/worker.log`

日志格式（JSON 行）：
```json
{"ts": 1720000000.1, "stream": "build|run|system", "level": "INFO|ERROR", "line": "..."}
```

## preflight 结构化日志（Dockerfile discovery）
字段（节选）：
- `dockerfile_candidates` / `primary_candidates` / `backup_candidates_filtered`
- `scanned_candidates` / `backup_candidates` / `ignored_backups`（兼容字段）
- `selected_dockerfile_path` / `selected_context_path` / `selected_backup`
- `selection_reason`（explicit_path/root_dockerfile/single_candidate/ambiguous/not_found）
- `non_unique_primary`（多个 primary 时为 true）

示例：
```json
{
  "scanned_candidates": ["Dockerfile", "docker/Dockerfile", "Dockerfile.original"],
  "primary_candidates": ["Dockerfile", "docker/Dockerfile"],
  "backup_candidates": ["Dockerfile.original"],
  "ignored_backups": ["Dockerfile.original"],
  "backup_candidates_filtered": ["Dockerfile.original"],
  "non_unique_primary": true,
  "selected_dockerfile": "Dockerfile",
  "selected_dockerfile_path": "Dockerfile",
  "selected_context_path": ".",
  "selected_backup": false,
  "selection_reason": "root_dockerfile"
}
```

## preflight 结构化日志（策略引擎）
字段（节选）：
- `repo_type` / `evidence`
- `strategy_selected` / `selection_reason` / `fallback_reason`
- `generated_files`（存在时）

示例：
```json
{
  "repo_type": "node",
  "evidence": ["package.json", "package-lock.json"],
  "strategy_selected": "generated",
  "selection_reason": "generated_for_node",
  "fallback_reason": null,
  "generated_files": [".antihub/generated/Dockerfile"]
}
```

## 诊断建议
- `non_unique_primary=true` 时建议显式指定 `dockerfile_path/context_path`
- `selected_backup=true` 时留意 `warnings` 中的 `DOCKERFILE_BACKUP_SELECTED`
