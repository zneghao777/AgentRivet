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
import time
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
    agent_requests: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    agent_errors: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    agent_tokens: dict[str, dict[str, int]] = field(
        default_factory=lambda: defaultdict(lambda: {"prompt": 0, "completion": 0, "total": 0})
    )
    agent_latencies: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    active_sessions: set[str] = field(default_factory=set)
    # 检索 / Rerank 指标（AGENTIC_RAG_UPGRADE_SPEC §3.10）
    retrieval_calls: dict[str, int] = field(default_factory=lambda: defaultdict(int))  # strategy -> count
    rerank_calls: dict[str, int] = field(default_factory=lambda: defaultdict(int))  # status(ok/error/skipped) -> count
    rerank_latencies: list[float] = field(default_factory=list)
    # 采集器健康（§10.1 采集器 / §5.5）
    spans_dropped_total: int = 0
    spans_dropped_by_reason: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    write_failures: int = 0
    queue_depth: int = 0
    collector_started_at: float = 0.0
    mq_publish: dict[str, dict[str, int]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(int))
    )
    mq_consume: dict[str, dict[str, int]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(int))
    )
    mq_deadletter: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    mq_queue_depth: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    mq_publish_events: list[tuple[float, str]] = field(default_factory=list)
    mq_deadletter_events: list[tuple[float, str]] = field(default_factory=list)
    orch_router_decisions: dict[str, list[dict]] = field(
        default_factory=lambda: defaultdict(list)
    )
    orch_worker_durations: dict[str, list[dict]] = field(
        default_factory=lambda: defaultdict(list)
    )
    orch_plan_task_counts: dict[str, list[int]] = field(
        default_factory=lambda: defaultdict(list)
    )
    orch_clarifications: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def record_spans_dropped(self, count: int = 1, *, reason: str = "unknown") -> None:
        """采集器丢 span 计数。"""
        with self.lock:
            self.spans_dropped_total += count
            self.spans_dropped_by_reason[reason] += count

    def record_write_failure(self) -> None:
        """写入失败计数。"""
        with self.lock:
            self.write_failures += 1

    def set_queue_depth(self, depth: int) -> None:
        with self.lock:
            self.queue_depth = depth

    def record_mq_publish(self, message_type: str, result: str) -> None:
        with self.lock:
            self.mq_publish[message_type][result] += 1
            self.mq_publish_events.append((time.time(), result))

    def record_mq_consume(self, queue: str, result: str) -> None:
        with self.lock:
            self.mq_consume[queue][result] += 1

    def record_mq_deadletter(self, queue: str, count: int = 1) -> None:
        with self.lock:
            self.mq_deadletter[queue] += count
            self.mq_deadletter_events.extend((time.time(), queue) for _ in range(count))

    def set_mq_queue_depth(self, queue: str, depth: int) -> None:
        with self.lock:
            self.mq_queue_depth[queue] = max(0, depth)

    def mq_publish_fallback_ratio(self) -> float:
        with self.lock:
            total = sum(sum(results.values()) for results in self.mq_publish.values())
            fallback = sum(results.get("fallback", 0) for results in self.mq_publish.values())
            return fallback / total if total else 0.0

    def mq_publish_fallback_ratio_recent(self, window_minutes: int) -> float:
        cutoff = time.time() - window_minutes * 60
        with self.lock:
            self.mq_publish_events = [e for e in self.mq_publish_events if e[0] >= cutoff]
            total = len(self.mq_publish_events)
            fallback = sum(1 for _, result in self.mq_publish_events if result == "fallback")
            return fallback / total if total else 0.0

    def mq_deadletter_recent(self, window_minutes: int) -> int:
        cutoff = time.time() - window_minutes * 60
        with self.lock:
            self.mq_deadletter_events = [e for e in self.mq_deadletter_events if e[0] >= cutoff]
            return len(self.mq_deadletter_events)

    def mq_queue_depth_value(self, queue: str) -> int:
        with self.lock:
            return self.mq_queue_depth.get(queue, 0)

    def collector_health(self) -> dict:
        with self.lock:
            publish_total = sum(sum(results.values()) for results in self.mq_publish.values())
            publish_fallback = sum(
                results.get("fallback", 0) for results in self.mq_publish.values()
            )
            return {
                "spans_dropped_total": self.spans_dropped_total,
                "spans_dropped_by_reason": dict(self.spans_dropped_by_reason),
                "write_failures": self.write_failures,
                "queue_depth": self.queue_depth,
                "active_sessions": len(self.active_sessions),
                "mq_publish": {k: dict(v) for k, v in self.mq_publish.items()},
                "mq_consume": {k: dict(v) for k, v in self.mq_consume.items()},
                "mq_deadletter": dict(self.mq_deadletter),
                "mq_queue_depth": dict(self.mq_queue_depth),
                "mq_publish_fallback_ratio": (
                    publish_fallback / publish_total if publish_total else 0.0
                ),
                "uptime_seconds": (time.time() - self.collector_started_at)
                if self.collector_started_at
                else 0,
            }

    def queue_usage_ratio(self) -> float:
        """采集队列使用率（§10.2 queue_usage 指标）。无队列信息时返回 0。"""
        try:
            from . import tracing

            return tracing.queue_usage_ratio()
        except Exception:  # noqa: BLE001
            return 0.0

    def record_request(
        self,
        tenant: str,
        *,
        ok: bool,
        duration_ms: float,
        token_usage: dict | None = None,
        agent_id: str | None = None,
    ) -> None:
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
            if agent_id:
                self.agent_requests[agent_id] += 1
                if not ok:
                    self.agent_errors[agent_id] += 1
                self.agent_latencies[agent_id].append(duration_ms)
                if token_usage:
                    agent_token = self.agent_tokens[agent_id]
                    agent_token["prompt"] += token_usage.get("prompt", 0)
                    agent_token["completion"] += token_usage.get("completion", 0)
                    agent_token["total"] += token_usage.get("total", 0)

    def record_tool(self, name: str) -> None:
        with self.lock:
            self.tool_calls[name] += 1

    def record_orch_router_decision(self, tenant: str, decision: dict) -> None:
        """记录 Router 分布与置信度；保留有界窗口供管理台读取。"""
        with self.lock:
            self.orch_router_decisions[tenant].append(
                {
                    "mode": decision.get("mode", "direct"),
                    "target_agent": decision.get("target_agent", "main"),
                    "confidence": float(decision.get("confidence", 0) or 0),
                }
            )
            self.orch_router_decisions[tenant] = self.orch_router_decisions[tenant][-5000:]
            if decision.get("needs_clarification"):
                self.orch_clarifications[tenant] += 1

    def record_orch_worker(
        self, tenant: str, *, agent_name: str, status: str, duration_ms: int
    ) -> None:
        with self.lock:
            self.orch_worker_durations[tenant].append(
                {
                    "agent_name": agent_name,
                    "status": status,
                    "duration_ms": duration_ms,
                }
            )
            self.orch_worker_durations[tenant] = self.orch_worker_durations[tenant][-5000:]

    def record_orch_plan(self, tenant: str, task_count: int) -> None:
        with self.lock:
            self.orch_plan_task_counts[tenant].append(task_count)
            self.orch_plan_task_counts[tenant] = self.orch_plan_task_counts[tenant][-5000:]

    def router_stats(self, tenant: str) -> dict:
        with self.lock:
            decisions = list(self.orch_router_decisions.get(tenant, []))
            by_agent: dict[str, dict] = {}
            for item in decisions:
                name = item["target_agent"]
                bucket = by_agent.setdefault(name, {"count": 0, "confidence_total": 0.0})
                bucket["count"] += 1
                bucket["confidence_total"] += item["confidence"]
            return {
                "total": len(decisions),
                "by_agent": {
                    name: {
                        "count": item["count"],
                        "avg_confidence": round(
                            item["confidence_total"] / item["count"], 4
                        ),
                    }
                    for name, item in sorted(by_agent.items())
                },
                "clarifications": self.orch_clarifications.get(tenant, 0),
            }

    def record_retrieval(self, strategy: str) -> None:
        """检索调用计数（按 strategy：vector/keyword/hybrid）。"""
        with self.lock:
            self.retrieval_calls[strategy] += 1

    def record_rerank(self, status: str, latency_ms: float | None = None) -> None:
        """Rerank 调用计数（status: ok/error/skipped）与耗时。"""
        with self.lock:
            self.rerank_calls[status] += 1
            if latency_ms is not None:
                self.rerank_latencies.append(latency_ms)

    def session_open(self, session_id: str) -> None:
        with self.lock:
            self.active_sessions.add(session_id)

    def session_close(self, session_id: str) -> None:
        with self.lock:
            self.active_sessions.discard(session_id)

    def active_session_count(self) -> int:
        """活跃会话数（内存态，跨租户全局）。"""
        with self.lock:
            return len(self.active_sessions)

    def summary(self, *, group_by: str | None = None) -> dict:
        with self.lock:
            if group_by == "tenant":
                out: dict = {}
                tenants = set(self.requests) | set(self.errors) | set(self.tokens)
                for t in tenants:
                    out[t] = self._tenant_summary(t)
                return out
            if group_by == "agent":
                return {
                    agent_id: {
                        "requests": requests,
                        "errors": self.agent_errors[agent_id],
                        "success_rate": round(
                            (requests - self.agent_errors[agent_id]) / requests, 4
                        ) if requests else 0.0,
                        "tokens": dict(self.agent_tokens[agent_id]),
                        "latency_p50_ms": round(
                            statistics.median(self.agent_latencies[agent_id]), 2
                        ) if self.agent_latencies[agent_id] else 0,
                        "latency_p95_ms": round(
                            _percentile(self.agent_latencies[agent_id], 95), 2
                        ) if self.agent_latencies[agent_id] else 0,
                    }
                    for agent_id, requests in self.agent_requests.items()
                }
            # 聚合全量
            total_req = sum(self.requests.values())
            total_err = sum(self.errors.values())
            all_lat = [v for vals in self.latencies.values() for v in vals]
            total_tokens = {k: sum(self.tokens[t][k] for t in self.tokens) for k in ("prompt", "completion", "total")}
            publish_total = sum(sum(results.values()) for results in self.mq_publish.values())
            publish_fallback = sum(
                results.get("fallback", 0) for results in self.mq_publish.values()
            )
            return {
                "total_requests": total_req,
                "success_rate": round((total_req - total_err) / total_req, 4) if total_req else 0.0,
                "error_rate": round(total_err / total_req, 4) if total_req else 0.0,
                "tokens": total_tokens,
                "tool_calls": dict(self.tool_calls),
                "latency_p50_ms": round(statistics.median(all_lat), 2) if all_lat else 0,
                "latency_p95_ms": round(_percentile(all_lat, 95), 2) if all_lat else 0,
                "active_sessions": len(self.active_sessions),
                "mq_publish": {k: dict(v) for k, v in self.mq_publish.items()},
                "mq_consume": {k: dict(v) for k, v in self.mq_consume.items()},
                "mq_deadletter": dict(self.mq_deadletter),
                "mq_queue_depth": dict(self.mq_queue_depth),
                "mq_publish_fallback_ratio": (
                    publish_fallback / publish_total if publish_total else 0.0
                ),
                # 检索 / Rerank 指标（AGENTIC_RAG_UPGRADE_SPEC §3.10）
                "retrieval_calls": dict(self.retrieval_calls),
                "rerank_calls": dict(self.rerank_calls),
                "rerank_latency_p95_ms": round(_percentile(self.rerank_latencies, 95), 2)
                if self.rerank_latencies
                else 0,
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
