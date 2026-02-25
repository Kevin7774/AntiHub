from __future__ import annotations

import time
from collections import defaultdict
from threading import Lock
from typing import Any


class RuntimeMetrics:
    def __init__(self) -> None:
        self._lock = Lock()
        self._started_at = int(time.time())
        self._total_requests = 0
        self._total_errors = 0
        self._duration_sum_ms = 0
        self._duration_max_ms = 0
        self._path_counts: dict[str, int] = defaultdict(int)
        self._status_counts: dict[str, int] = defaultdict(int)
        self._custom_counters: dict[str, int] = defaultdict(int)
        self._custom_timings: dict[str, dict[str, float]] = defaultdict(
            lambda: {"count": 0, "sum_ms": 0.0, "max_ms": 0.0}
        )

    def record_request(self, *, path: str, status_code: int, duration_ms: int) -> None:
        normalized_path = str(path or "/").strip() or "/"
        bucket = f"{int(status_code)}"
        with self._lock:
            self._total_requests += 1
            self._duration_sum_ms += int(duration_ms)
            self._duration_max_ms = max(self._duration_max_ms, int(duration_ms))
            self._path_counts[normalized_path] += 1
            self._status_counts[bucket] += 1
            if int(status_code) >= 500:
                self._total_errors += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            total = int(self._total_requests)
            average_ms = round(self._duration_sum_ms / total, 2) if total > 0 else 0.0
            custom_timers: dict[str, Any] = {}
            for key, values in self._custom_timings.items():
                count = int(values.get("count") or 0)
                sum_ms = float(values.get("sum_ms") or 0.0)
                avg_ms = round(sum_ms / count, 2) if count > 0 else 0.0
                custom_timers[key] = {
                    "count": count,
                    "avg_ms": avg_ms,
                    "max_ms": round(float(values.get("max_ms") or 0.0), 2),
                    "sum_ms": round(sum_ms, 2),
                }
            return {
                "started_at": self._started_at,
                "uptime_seconds": max(0, int(time.time()) - self._started_at),
                "requests_total": total,
                "errors_5xx_total": int(self._total_errors),
                "latency_avg_ms": average_ms,
                "latency_max_ms": int(self._duration_max_ms),
                "status_counts": dict(sorted(self._status_counts.items())),
                "top_paths": sorted(self._path_counts.items(), key=lambda item: item[1], reverse=True)[:20],
                "custom_counters": dict(sorted(self._custom_counters.items())),
                "custom_timers": dict(sorted(custom_timers.items())),
            }

    def record_counter(self, *, name: str, value: int = 1) -> None:
        metric = str(name or "").strip().lower()
        if not metric:
            return
        with self._lock:
            self._custom_counters[metric] += int(value)

    def record_timing(self, *, name: str, duration_ms: int | float) -> None:
        metric = str(name or "").strip().lower()
        if not metric:
            return
        duration = max(0.0, float(duration_ms))
        with self._lock:
            row = self._custom_timings[metric]
            row["count"] = float(row.get("count") or 0.0) + 1.0
            row["sum_ms"] = float(row.get("sum_ms") or 0.0) + duration
            row["max_ms"] = max(float(row.get("max_ms") or 0.0), duration)


_RUNTIME_METRICS = RuntimeMetrics()


def record_request_metric(*, path: str, status_code: int, duration_ms: int) -> None:
    _RUNTIME_METRICS.record_request(path=path, status_code=status_code, duration_ms=duration_ms)


def get_runtime_metrics_snapshot() -> dict[str, Any]:
    return _RUNTIME_METRICS.snapshot()


def record_counter_metric(*, name: str, value: int = 1) -> None:
    _RUNTIME_METRICS.record_counter(name=name, value=value)


def record_timing_metric(*, name: str, duration_ms: int | float) -> None:
    _RUNTIME_METRICS.record_timing(name=name, duration_ms=duration_ms)
