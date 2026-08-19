"""运行指标：内存维护（SHOULD 后续接 Prometheus）。

- 请求总数 / 成功率 / 错误率（按租户）
- Token 消耗（prompt / completion / total）
- 工具调用次数（按工具名）
- P50 / P95 延迟
- 活跃会话数
"""

from __future__ import annotations

import statistics
import threading
from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class MetricStore:
    """线程安全的指标存储。"""

    lock: threading.Lock = field(default_factory=threading.Lock)

    requests: dict[str, int] = field(default_factory=lambda: defaultdict(int))  # tenant -> total
    errors: dict[str, int] = field(default_factory=lambda: defaultdict(int))  # tenant -> errors
    tokens: dict[str, dict[str, int]] = field(
        default_factory=lambda: defaultdict(lambda: {"prompt": 0, "completion": 0, "total": 0})
    )
    tool_calls: dict[str, int] = field(default_factory=lambda: defaultdict(int))  # tool -> count
    latencies: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    active_sessions: set[str] = field(default_factory=set)

    def record_request(self, tenant: str, *, ok: bool, duration_ms: float, token_usage: dict | None = None) -> None:
        with self.lock:
            self.requests[tenant] += 1
            if not ok:
                self.errors[tenant] += 1
            self.latencies[tenant].append(duration_ms)
            if token_usage:
                t = self.tokens[tenant]
                t["prompt"] += token_usage.get("prompt", 0)
                t["completion"] += token_usage.get("completion", 0)
                t["total"] += token_usage.get("total", 0)

    def record_tool(self, name: str) -> None:
        with self.lock:
            self.tool_calls[name] += 1

    def session_open(self, session_id: str) -> None:
        with self.lock:
            self.active_sessions.add(session_id)

    def session_close(self, session_id: str) -> None:
        with self.lock:
            self.active_sessions.discard(session_id)

    def summary(self, *, group_by: str | None = None) -> dict:
        with self.lock:
            if group_by == "tenant":
                out: dict = {}
                tenants = set(self.requests) | set(self.errors) | set(self.tokens)
                for t in tenants:
                    out[t] = self._tenant_summary(t)
                return out
            # 聚合全量
            total_req = sum(self.requests.values())
            total_err = sum(self.errors.values())
            all_lat = [v for vals in self.latencies.values() for v in vals]
            total_tokens = {k: sum(self.tokens[t][k] for t in self.tokens) for k in ("prompt", "completion", "total")}
            return {
                "total_requests": total_req,
                "success_rate": round((total_req - total_err) / total_req, 4) if total_req else 0.0,
                "error_rate": round(total_err / total_req, 4) if total_req else 0.0,
                "tokens": total_tokens,
                "tool_calls": dict(self.tool_calls),
                "latency_p50_ms": round(statistics.median(all_lat), 2) if all_lat else 0,
                "latency_p95_ms": round(_percentile(all_lat, 95), 2) if all_lat else 0,
                "active_sessions": len(self.active_sessions),
            }

    def _tenant_summary(self, tenant: str) -> dict:
        req = self.requests[tenant]
        err = self.errors[tenant]
        lat = self.latencies[tenant]
        return {
            "requests": req,
            "errors": err,
            "success_rate": round((req - err) / req, 4) if req else 0.0,
            "tokens": dict(self.tokens[tenant]),
            "latency_p50_ms": round(statistics.median(lat), 2) if lat else 0,
            "latency_p95_ms": round(_percentile(lat, 95), 2) if lat else 0,
        }


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    k = (len(sorted_vals) - 1) * p / 100
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


metrics = MetricStore()
