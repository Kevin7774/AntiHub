# AntiHub Improvement Plan (2026-01-27)

## 1) Problem Classification and Root Causes
- Multi-stage Dockerfile stage names mis-pulled: preflight used incomplete FROM parsing and could treat stage names as external images.
- BuildKit-required Dockerfiles: legacy builder paths and missing BuildKit detection led to late failures.
- Dockerfile discovery gaps: discovery relied on a single path and could not detect ambiguous Dockerfile.* cases or subdir layouts reliably.
- Remote build slow failures: network/registry/DNS errors surfaced late and were not clearly categorized.
- LFS/submodule incomplete clones: clone succeeded but content was missing without clear warnings.
- Showcase/manual not first-class: deploy failures could prevent manual generation; showcase fallback needed explicit control.

## 2) Implemented Changes (Key Systems)
- Module refactor (maintainable/testable):
  - `git_ops.py`: ref auto-detection, main→master fallback, LFS/submodule helpers.
  - `dockerfile_parser.py`: robust FROM/ARG parsing, stage extraction, BuildKit detection.
  - `dockerfile_discovery.py`: candidate discovery + ambiguity handling.
- Deploy vs. Showcase workflow:
  - `mode=deploy|showcase` + `auto_mode` fallback if Dockerfile is missing.
  - `auto_manual` queues manual generation after clone; deploy failures still produce manual if clone succeeded.
- Preflight accuracy + diagnostics:
  - Stage names excluded from pull list, ARG defaults respected for base image resolution.
  - `preflight_meta` structured with `stages`, `external_images_to_pull`, `warnings`, `dockerfile_path`, `context_path`, `candidates`.
- BuildKit: default enabled + fail-fast
  - Detect `RUN --mount`, `FROM --platform`, `#syntax` and raise `BUILDKIT_REQUIRED` when disabled.
  - BuildKit default on, `DOCKER_BUILDKIT=1` enforced for docker build.
- Dockerfile discovery:
  - Depth-limited search for `Dockerfile`, `Dockerfile.*`, `*/Dockerfile`, `*/Dockerfile.*`.
  - Multiple candidates → `DOCKERFILE_AMBIGUOUS` + candidate list returned in `preflight_meta`.
- Clone robustness + warnings:
  - `git ls-remote --symref` for default branch; fallback main→master.
  - LFS/submodule detection emits warnings and logs `[clone-post]`.
- Error mapping + timeouts:
  - DNS/registry/build network error classification preserved.
  - `TIMEOUT_CLONE/BUILD/RUN/MANUAL` remain configurable via env/config.
- Manual metadata surfaced:
  - `manual_meta` stored in case and returned by GET `/cases/{id}`.
  - `runtime.url` added for contract alignment.
- Frontend-ready system endpoints:
  - `/templates` + `/templates/{template_id}` with JSON-backed templates.
  - `/plans` + `/usage` for subscription/usage MVP.
  - `/healthz` includes version/git_sha/root_path/api_host/api_port for diagnostics.
  - `ROOT_PATH` supported for reverse proxy `/docs` reachability.

## 3) API Contract Updates (OpenAPI reflected)
### POST /cases (examples)
Deploy (default):
```json
{
  "repo_url": "https://github.com/org/repo",
  "mode": "deploy",
  "auto_mode": true,
  "ref": "auto",
  "dockerfile_path": "Dockerfile",
  "context_path": ".",
  "build": { "network": "bridge", "no_cache": false, "build_args": {} },
  "git": { "enable_submodule": false, "enable_lfs": false },
  "auto_manual": true
}
```

Showcase:
```json
{
  "repo_url": "https://github.com/org/repo",
  "mode": "showcase",
  "ref": "auto",
  "auto_manual": true
}
```

Notes:
- If `dockerfile_path/context_path` are omitted, discovery auto-selects a single candidate or raises `DOCKERFILE_AMBIGUOUS` when multiple exist.
- Missing Dockerfile with `auto_mode=true` auto-downgrades to `SHOWCASE_READY`.
- `template_id` (optional) will prefill `repo_url/ref/mode` and suggested env keys.

### GET /cases/{id} (key fields)
- `resolved_ref`, `resolved_dockerfile_path`, `resolved_context_path`
- `preflight_meta`: `{ stages, external_images_to_pull, warnings, dockerfile_path, context_path, candidates }`
- `manual_status`: `PENDING|RUNNING|SUCCESS|FAILED`
- `manual_meta`: detailed manual signals
- `runtime`: `{ container_id, host_port, url }`

### Templates / Plans / Usage
- GET `/templates`, GET `/templates/{template_id}`
- GET `/plans`, GET `/usage`
- GET `/cases` supports `search/limit/offset` aliases for frontend filters
- Docs/host config: `API_HOST`, `API_PORT`, `ROOT_PATH`, `CORS_ORIGINS`

## 4) Regression Matrix and Results (Executed)
Results file: `integration_test_results.json` (mirror: `.devlogs/integration_test_results.json`)

Summary: **PASSED (12/12)** on **2026-01-27**
- multi_stage_stage_ref: PASS (no stage-name pull) — case_id `c_c07c24`
- buildkit_required_pass: PASS — case_id `c_eff3ff`
- buildkit_required_fail_fast: PASS (`BUILDKIT_REQUIRED`) — case_id `c_4c58a5`
- dockerfile_subdir: PASS — case_id `c_e06c0e`
- dockerfile_ambiguous: PASS (`DOCKERFILE_AMBIGUOUS`) — case_id `c_56fdf8`
- no_dockerfile_deploy: PASS (`DOCKERFILE_NOT_FOUND`) — case_id `c_efabe9`
- no_dockerfile_showcase: PASS (manual SUCCESS) — case_id `c_d52b1a`
- wechat_miniprogram_showcase: PASS (ref=main + manual SUCCESS) — case_id `c_ce6de7`
- docs_reachable: PASS (HTTP 200 on `/docs`)
- templates_list: PASS (built-ins present)
- template_case_create: PASS (template_id creates showcase case)
- plans_usage: PASS (plans + usage shape stable)

## 5) Next Steps
- Add docker-compose / multi-service detection for richer deploy + showcase graphs.
- Provide registry mirror + image caching policy to reduce network variance.
- Add resource quotas (CPU/mem/time) and concurrency limits per tenant.
- Add cleanup job for finished containers and stale port locks.

## Appendix A) Error Codes (Patterns / Stage / Suggestions)
- DNS_RESOLUTION_FAILED (clone/build): logs include “no such host”, “temporary failure in name resolution”. 建议：检查 DNS/代理/镜像源配置。
- REGISTRY_UNREACHABLE (pull/build): logs include “registry-1.docker.io”, “connection refused”, “tls handshake timeout”. 建议：检查 registry mirror 与网络策略。
- DOCKER_BUILD_NETWORK_FAILED (build): logs include “network is unreachable”, “i/o timeout”, “dial tcp”. 建议：检查 build network 与代理。
- BUILDKIT_REQUIRED (preflight/build): logs include `RUN --mount`, `FROM --platform`, `#syntax`. 建议：开启 BuildKit（`DOCKER_BUILDKIT=1` 或 daemon features.buildkit=true）。
- DOCKERFILE_NOT_FOUND (preflight): logs include “Dockerfile not found”. 建议：确认 `dockerfile_path/context_path` 或切换 showcase。
- DOCKERFILE_AMBIGUOUS (preflight): logs include “Multiple Dockerfile candidates”. 建议：显式指定 `dockerfile_path/context_path`。
- GIT_REF_NOT_FOUND (clone/manual): logs include “Remote branch … not found”. 建议：使用 `ref=auto` 或确认分支名。
- GIT_CLONE_FAILED (clone/manual): logs include git clone 非零退出。 建议：检查仓库地址/权限/网络。
- TIMEOUT_CLONE / TIMEOUT_BUILD / TIMEOUT_RUN / TIMEOUT_MANUAL：超时提示。 建议：提升对应 TIMEOUT_* 或优化仓库/构建。

## Appendix B) TIMEOUT Defaults & Precedence
- Defaults (seconds): CLONE=120, BUILD=1800, RUN=120, MANUAL=300
- Config keys: `TIMEOUT_CLONE`, `TIMEOUT_BUILD`, `TIMEOUT_RUN`, `TIMEOUT_MANUAL`
- Precedence: **env** > `config.yaml` > code defaults
