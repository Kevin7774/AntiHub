# Dockerfile Discovery 专项

本页解释 Dockerfile 自动发现的规则、常见问题及排查方式。

## 发现规则（优先级）
1) **显式路径优先**：传入 `dockerfile_path` 时直接使用。
2) **过滤备份**：`.orig/.original/.bak/.backup/.old/.save/.disabled`（大小写不敏感）视为备份。
3) **root 优先**：若 `context_path/Dockerfile` 存在，优先选中。
4) **唯一候选**：仅有一个候选则直接选中。
5) **仍不安全**：才抛出 `DOCKERFILE_AMBIGUOUS`。

## 何时仍会 ambiguous
- primary 候选多于一个且无 root Dockerfile
- 或仅备份候选但数量大于 1

## 显式指定示例
```json
{
  "repo_url": "/path/to/your/repo",
  "dockerfile_path": "docker/Dockerfile",
  "context_path": "docker"
}
```

## 结构化日志字段（关键诊断）
```json
{
  "dockerfile_candidates": ["Dockerfile", "docker/Dockerfile", "Dockerfile.original"],
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

字段解释：
- `non_unique_primary=true`：存在多个 primary，但已按 root 优先选中
- `selection_reason`：选择原因（explicit_path/root_dockerfile/single_candidate/ambiguous/not_found）
- `selected_backup=true`：仅在 backup-only 兜底时出现，应关注 `warnings`

## metagpt-min 场景
- 目录含 `Dockerfile` + `Dockerfile.original`
- 预期：不再报 `DOCKERFILE_AMBIGUOUS`，选择 root Dockerfile
