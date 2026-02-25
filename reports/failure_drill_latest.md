# Failure Drill Report

- Timestamp (UTC): `2026-02-16T12:41:48.622143+00:00`
- Result: `PASS`

## Steps
1. `/home/zed/code/AntiHub/.venv/bin/python tools/chaos_suite.py --attack-count 20 --replay-concurrency 10 --webhook-url http://127.0.0.1:8010/billing/webhooks/payment --health-url http://127.0.0.1:8010/health/billing`
   - status: `0`
```text
[suite] health_check url=http://127.0.0.1:8010/health/billing
[suite] snapshot_before {'paid_count': 7013, 'paid_sum': 11005075070622550, 'grant_count': 7144, 'grant_sum': 7144980}
[suite] attack_simulation cases=20 url=http://127.0.0.1:8010/billing/webhooks/payment
[suite] attack_summary 2xx=3 400=11 403=6 5xx=0 err=0
[suite] replay_attack same_payload concurrency=10
[suite] replay_attack same_order_new_event_ids concurrency=10
[suite] snapshot_after {'paid_count': 7014, 'paid_sum': 11005075070632450, 'grant_count': 7145, 'grant_sum': 7145980}
[suite] ALL CHECKS PASSED
```
2. `/home/zed/code/AntiHub/.venv/bin/python -m pytest tests/test_billing_rate_limit.py::test_rate_limiter_requires_redis_in_production tests/test_global_exception_handler.py::test_unhandled_exceptions_are_normalized -q`
   - status: `0`
```text
..                                                                       [100%]
2 passed in 1.07s
```
