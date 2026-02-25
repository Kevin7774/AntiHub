# OpenClaw Skill: github.fetch

## Summary
Fetch a GitHub repository and return a normalized repo snapshot with metadata for visualization. This is required by AntiHub ingest.

## Input
```json
{
  "repo_url": "https://github.com/owner/repo",
  "ref": "main",
  "depth": 1,
  "include_submodules": false,
  "include_lfs": false,
  "max_files": 20000
}
```

## Output (success)
```json
{
  "ok": true,
  "output": {
    "repo_path": "/tmp/openclaw/repo",
    "commit_sha": "abc123...",
    "file_index": [
      {"path": "README.md", "size": 1200, "type": "file"}
    ],
    "readme_rendered": "# Repo\n...",
    "repo_meta": {
      "stars": 123,
      "forks": 45,
      "topics": ["topic-a"],
      "license": "MIT",
      "default_branch": "main"
    },
    "ingest_meta": {"generated_at": 0, "repo_url": "..."},
    "ingest_meta_path": "/tmp/openclaw/repo/.antihub/ingest_meta.json"
  }
}
```

## Output (failure)
```json
{
  "ok": false,
  "error_code": "GIT_CLONE_FAILED",
  "error_message": "git clone failed"
}
```

## Error Codes
- `GITHUB_RATE_LIMIT`
- `GIT_CLONE_FAILED`
- `LFS_FAILED`
- `SUBMODULE_FAILED`

## Notes
- `repo_path` must be accessible to AntiHub (shared filesystem or network mount).
- `readme_rendered` may be raw markdown; AntiHub will sanitize/truncate.
- `file_index` is truncated by `max_files` to control cost.
