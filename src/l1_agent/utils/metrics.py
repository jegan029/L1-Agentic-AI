"""In-process metrics collector with Prometheus text-format export."""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from threading import Lock
from typing import Dict, List


@dataclass
class MetricPoint:
    name: str
    value: float
    timestamp: float
    labels: Dict[str, str] = field(default_factory=dict)


class MetricsCollector:
    """Thread-safe metrics collector for counters and histograms."""

    def __init__(self) -> None:
        self._counters: Dict[str, float] = defaultdict(float)
        self._histograms: Dict[str, List[float]] = defaultdict(list)
        self._gauges: Dict[str, float] = defaultdict(float)
        self._counter_labels: Dict[str, Dict[str, str]] = {}
        self._lock = Lock()
        self._start_time = time.time()

    def increment(self, name: str, value: float = 1.0, labels: Dict[str, str] | None = None) -> None:
        key = self._key(name, labels)
        with self._lock:
            self._counters[key] += value
            if labels and key not in self._counter_labels:
                self._counter_labels[key] = labels

    def observe(self, name: str, value: float, labels: Dict[str, str] | None = None) -> None:
        key = self._key(name, labels)
        with self._lock:
            self._histograms[key].append(value)

    def set_gauge(self, name: str, value: float, labels: Dict[str, str] | None = None) -> None:
        key = self._key(name, labels)
        with self._lock:
            self._gauges[key] = value

    def get_counter(self, name: str, labels: Dict[str, str] | None = None) -> float:
        key = self._key(name, labels)
        with self._lock:
            return self._counters.get(key, 0.0)

    def get_histogram_stats(self, name: str, labels: Dict[str, str] | None = None) -> Dict[str, float]:
        key = self._key(name, labels)
        with self._lock:
            values = self._histograms.get(key, [])
        if not values:
            return {"count": 0, "min": 0, "max": 0, "avg": 0}
        return {
            "count": len(values),
            "min": min(values),
            "max": max(values),
            "avg": sum(values) / len(values),
        }

    def snapshot(self) -> Dict[str, object]:
        with self._lock:
            return {
                "counters": dict(self._counters),
                "histograms": {
                    k: {"count": len(v), "avg": sum(v) / len(v) if v else 0}
                    for k, v in self._histograms.items()
                },
                "gauges": dict(self._gauges),
                "uptime_seconds": time.time() - self._start_time,
            }

    def prometheus_text(self) -> str:
        """Render all metrics in Prometheus text exposition format.

        Compatible with Prometheus scrape_configs and Grafana datasources.
        """
        lines: List[str] = []
        now_ms = int(time.time() * 1000)

        with self._lock:
            counters = dict(self._counters)
            histograms = dict(self._histograms)
            gauges = dict(self._gauges)

        # ── Counters ──────────────────────────────────────────────────
        for key, value in sorted(counters.items()):
            safe_name = _prometheus_name(key)
            lines.append(f"# TYPE {safe_name} counter")
            lines.append(f"{safe_name} {value:.6g} {now_ms}")

        # ── Histograms ────────────────────────────────────────────────
        for key, values in sorted(histograms.items()):
            if not values:
                continue
            safe_name = _prometheus_name(key)
            count = len(values)
            total = sum(values)
            sorted_vals = sorted(values)
            lines.append(f"# TYPE {safe_name} histogram")
            for quantile, pct in [(0.5, 0.5), (0.9, 0.9), (0.99, 0.99)]:
                idx = max(0, int(pct * count) - 1)
                lines.append(f'{safe_name}{{quantile="{quantile}"}} {sorted_vals[idx]:.6g} {now_ms}')
            lines.append(f"{safe_name}_sum {total:.6g} {now_ms}")
            lines.append(f"{safe_name}_count {count} {now_ms}")

        # ── Gauges ────────────────────────────────────────────────────
        for key, value in sorted(gauges.items()):
            safe_name = _prometheus_name(key)
            lines.append(f"# TYPE {safe_name} gauge")
            lines.append(f"{safe_name} {value:.6g} {now_ms}")

        # ── Process uptime ────────────────────────────────────────────
        lines.append("# TYPE l1_agent_uptime_seconds gauge")
        lines.append(f"l1_agent_uptime_seconds {time.time() - self._start_time:.3f} {now_ms}")

        return "\n".join(lines) + "\n"

    @staticmethod
    def _key(name: str, labels: Dict[str, str] | None) -> str:
        if not labels:
            return name
        sorted_labels = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
        return f"{name}{{{sorted_labels}}}"


def _prometheus_name(key: str) -> str:
    """Convert a metric key (possibly with label suffix) to a valid Prometheus name."""
    # Strip label block for the metric name portion
    base = key.split("{")[0]
    # Replace dots and hyphens with underscores
    return "l1_agent_" + base.replace(".", "_").replace("-", "_")


# Global singleton
metrics = MetricsCollector()
