# Integration Tests

## Prereqs
- Backend auto-starts if needed (`./dev.sh up`).
- Docker available; if port pool is busy, stop containers with label `antihub.managed=true`.

## Run
```bash
python3 scripts/integration_tests.py
```
Optional: `SKIP_NETWORK=1` to skip the real remote repo case when offline.

## Output
- Results file: `integration_test_results.json` (also mirrored in `.devlogs/integration_test_results.json`)
- Each case includes: `case_id`, `status`, `duration_ms`, `error_code`, `pass`.

## Matrix
1) Multi-stage stage reference
- Repo: `test-repos/multi-stage-stage-ref`
- Ensure stage name is not pulled (no `app-base` in preflight images).

2) BuildKit required
- Repo: `test-repos/buildkit-required`
- BuildKit enabled => PASS; disabled => `BUILDKIT_REQUIRED`.

3) Dockerfile subdir + ambiguous
- Repo: `test-repos/docker-subdir` with `dockerfile_path/context_path` => RUNNING
- Repo: `test-repos/docker-ambiguous` without path => `DOCKERFILE_AMBIGUOUS`

4) No Dockerfile
- Repo: `test-repos/no-dockerfile`
- deploy => `DOCKERFILE_NOT_FOUND`
- showcase => manual SUCCESS

5) Real repo
- Repo: `https://github.com/Kevin7774/wechat-miniprogram-card`
- ref=auto + showcase => manual SUCCESS, `resolved_ref=main`

6) Docs reachable
- GET `/docs` returns HTTP 200.

7) Templates
- GET `/templates` includes built-ins (hello-world, wechat-miniprogram-card, metagpt-min).
- POST `/cases` with `template_id=hello-world` creates a showcase case.

8) Plans + Usage
- GET `/plans` returns list of plans.
- GET `/usage` returns usage counters.
