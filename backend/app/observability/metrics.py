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
    retrieval_calls: dict[str, int] = field(
        default_factory=lambda: defaultdict(int)
    )  # strategy -> count
    rerank_calls: dict[str, int] = field(
        default_factory=lambda: defaultdict(int)
    )  # status(ok/error/skipped) -> count
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
    orch_router_decisions: dict[str, list[dict]] = field(default_factory=lambda: defaultdict(list))
    orch_worker_durations: dict[str, list[dict]] = field(default_factory=lambda: defaultdict(list))
    orch_plan_task_counts: dict[str, list[int]] = field(default_factory=lambda: defaultdict(list))
    orch_clarifications: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    # 并发防护（CONCURRENCY_GUARD_SPEC §12）
    guard_llm_inflight: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    guard_llm_effective_max: dict[str, int] = field(
        default_factory=lambda: defaultdict(int)
    )
    guard_llm_admitted_total: dict[str, int] = field(
        default_factory=lambda: defaultdict(int)
    )
    guard_first_outcome_samples: int = 0
    guard_first_outcome_max_ms: float = 0.0
    guard_first_outcome_events: list[tuple[int, float]] = field(default_factory=list)
    guard_stream_inflight: int = 0
    guard_queue_length: int = 0
    guard_queue_wait_samples: int = 0
    guard_queue_wait_ms: list[float] = field(default_factory=list)
    guard_queue_wait_sample_events: list[tuple[int, float]] = field(default_factory=list)
    guard_queue_wait_events: list[tuple[float, float]] = field(default_factory=list)
    guard_rejected_total: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    guard_rejected_events: list[tuple[float, str]] = field(default_factory=list)
    guard_upstream_429_total: int = 0
    guard_upstream_429_events: list[float] = field(default_factory=list)
    cb_state: dict[str, int] = field(default_factory=dict)
    cb_state_changed_at: dict[str, float] = field(default_factory=dict)
    cb_unavailable_since: dict[str, float] = field(default_factory=dict)
    degrade_level: int = 0
    degrade_nonzero_since: float | None = None
    degrade_transition_samples: int = 0
    degrade_transition_events: list[tuple[int, float, int]] = field(default_factory=list)
    stream_cancelled_total: int = 0
    llm_orphan_calls_total: int = 0
    semantic_cache_lookups: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    # 异步 ChatRun（ASYNC_CONVERSATION_RUN_SPEC §14.2）
    chat_run_active: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    chat_run_queue_wait_ms: list[float] = field(default_factory=list)
    chat_run_duration_ms: list[float] = field(default_factory=list)
    chat_run_subscribers: int = 0
    chat_run_reconnect_total: int = 0
    chat_run_replayed_events_total: int = 0
    chat_run_cancel_total: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    chat_run_recovery_total: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    chat_run_event_lag: int = 0
    chat_run_event_cleanup_total: int = 0
    chat_run_event_cleanup_duration_ms: list[float] = field(default_factory=list)
    chat_run_event_cleanup_failures_total: int = 0
    remote_guard_snapshots: dict[str, dict] = field(default_factory=dict)
    remote_guard_429_seen: dict[str, int] = field(default_factory=dict)

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

    def set_guard_llm_inflight(self, pool: str, value: int) -> None:
        """设置 online/background 池的在途 LLM 调用数。"""
        with self.lock:
            self.guard_llm_inflight[pool] = max(0, int(value))

    def set_guard_llm_effective_max(self, pool: str, value: int) -> None:
        """设置 AIMD 调整后的池有效并发上限。"""
        with self.lock:
            self.guard_llm_effective_max[pool] = max(1, int(value))

    def record_guard_llm_admitted(self, pool: str, count: int = 1) -> None:
        """记录成功取得 LLM 槽位的累计次数。"""
        with self.lock:
            self.guard_llm_admitted_total[pool] += max(0, int(count))

    def record_guard_first_outcome(self, duration_ms: float) -> None:
        """记录 SSE 首个 message_delta/error 的服务端产出耗时。"""
        with self.lock:
            value = max(0.0, float(duration_ms))
            self.guard_first_outcome_samples += 1
            self.guard_first_outcome_max_ms = max(self.guard_first_outcome_max_ms, value)
            self.guard_first_outcome_events.append(
                (self.guard_first_outcome_samples, value)
            )
            self.guard_first_outcome_events = self.guard_first_outcome_events[-50_000:]

    def set_guard_stream_inflight(self, value: int) -> None:
        """设置当前已准入的 SSE 流数。"""
        with self.lock:
            self.guard_stream_inflight = max(0, int(value))

    def set_guard_queue_length(self, value: int) -> None:
        """设置当前 SSE 准入队列长度。"""
        with self.lock:
            self.guard_queue_length = max(0, int(value))

    def record_guard_queue_wait(self, duration_ms: float) -> None:
        """记录一次 SSE 准入排队耗时。"""
        with self.lock:
            value = max(0.0, float(duration_ms))
            self.guard_queue_wait_samples += 1
            self.guard_queue_wait_ms.append(value)
            self.guard_queue_wait_sample_events.append(
                (self.guard_queue_wait_samples, value)
            )
            self.guard_queue_wait_events.append((time.time(), value))
            # 内存指标只保留最近窗口，避免长时间运行后无界增长。
            self.guard_queue_wait_ms = self.guard_queue_wait_ms[-10_000:]
            self.guard_queue_wait_sample_events = self.guard_queue_wait_sample_events[-50_000:]
            self.guard_queue_wait_events = self.guard_queue_wait_events[-10_000:]

    def record_guard_rejected(self, reason: str, count: int = 1) -> None:
        """按原因记录防护栈拒绝数。"""
        value = max(0, int(count))
        with self.lock:
            self.guard_rejected_total[reason] += value
            self.guard_rejected_events.extend((time.time(), reason) for _ in range(value))
            self.guard_rejected_events = self.guard_rejected_events[-10_000:]

    def record_upstream_429(self, count: int = 1) -> None:
        """记录 LLM 上游返回的 429。"""
        value = max(0, int(count))
        with self.lock:
            self.guard_upstream_429_total += value
            self.guard_upstream_429_events.extend(time.time() for _ in range(value))
            self.guard_upstream_429_events = self.guard_upstream_429_events[-10_000:]

    def set_cb_state(self, endpoint: str, state: int | str) -> None:
        """设置端点熔断状态：closed=0、half_open=1、open=2。"""
        states = {"closed": 0, "half_open": 1, "open": 2}
        normalized = states.get(state, state) if isinstance(state, str) else state
        value = int(normalized)
        if value not in {0, 1, 2}:
            raise ValueError("cb state must be closed/half_open/open or 0/1/2")
        with self.lock:
            if self.cb_state.get(endpoint) != value:
                self.cb_state_changed_at[endpoint] = time.time()
            if value in {1, 2}:
                self.cb_unavailable_since.setdefault(endpoint, time.time())
            else:
                self.cb_unavailable_since.pop(endpoint, None)
            self.cb_state[endpoint] = value

    def set_degrade_level(self, level: int) -> None:
        """设置当前降级档位（D0/D1/D2）。"""
        value = int(level)
        if value not in {0, 1, 2}:
            raise ValueError("degrade level must be 0, 1, or 2")
        with self.lock:
            previous = self.degrade_level
            if self.degrade_level == 0 and value >= 1:
                self.degrade_nonzero_since = time.time()
            elif value == 0:
                self.degrade_nonzero_since = None
            self.degrade_level = value
            if value != previous:
                self.degrade_transition_samples += 1
                self.degrade_transition_events.append(
                    (self.degrade_transition_samples, time.time() * 1000, value)
                )
                self.degrade_transition_events = self.degrade_transition_events[-1_000:]

    def record_stream_cancelled(self, count: int = 1) -> None:
        """记录客户端断连后取消的流。"""
        with self.lock:
            self.stream_cancelled_total += max(0, int(count))

    def record_llm_orphan_call(self, count: int = 1) -> None:
        """记录流取消后仍在上游自然结束的 LLM 调用。"""
        with self.lock:
            self.llm_orphan_calls_total += max(0, int(count))

    def record_semantic_cache_lookup(self, result: str) -> None:
        """记录语义缓存结果（hit/miss/bypass/error）。"""
        with self.lock:
            self.semantic_cache_lookups[result] += 1

    def record_chat_run_status(self, status: str, delta: int) -> None:
        with self.lock:
            self.chat_run_active[status] = max(
                0, self.chat_run_active[status] + int(delta)
            )

    def record_chat_run_queue_wait(self, duration_ms: float) -> None:
        with self.lock:
            self.chat_run_queue_wait_ms.append(max(0.0, float(duration_ms)))
            self.chat_run_queue_wait_ms = self.chat_run_queue_wait_ms[-10_000:]

    def record_chat_run_duration(self, duration_ms: float) -> None:
        with self.lock:
            self.chat_run_duration_ms.append(max(0.0, float(duration_ms)))
            self.chat_run_duration_ms = self.chat_run_duration_ms[-10_000:]

    def change_chat_run_subscribers(self, delta: int) -> None:
        with self.lock:
            self.chat_run_subscribers = max(0, self.chat_run_subscribers + int(delta))

    def record_chat_run_reconnect(self, replayed_events: int) -> None:
        with self.lock:
            self.chat_run_reconnect_total += 1
            self.chat_run_replayed_events_total += max(0, int(replayed_events))

    def record_chat_run_cancel(self, result: str) -> None:
        with self.lock:
            self.chat_run_cancel_total[result] += 1

    def record_chat_run_recovery(self, result: str, count: int = 1) -> None:
        with self.lock:
            self.chat_run_recovery_total[result] += max(0, int(count))

    def set_chat_run_event_lag(self, value: int) -> None:
        with self.lock:
            self.chat_run_event_lag = max(0, int(value))

    def record_chat_run_event_cleanup(
        self,
        count: int,
        duration_ms: float = 0,
        *,
        failed: bool = False,
    ) -> None:
        with self.lock:
            self.chat_run_event_cleanup_total += max(0, int(count))
            self.chat_run_event_cleanup_duration_ms.append(
                max(0.0, float(duration_ms))
            )
            self.chat_run_event_cleanup_duration_ms = (
                self.chat_run_event_cleanup_duration_ms[-1000:]
            )
            if failed:
                self.chat_run_event_cleanup_failures_total += 1

    def guard_queue_wait_p95_recent(self, window_minutes: int) -> float | None:
        """返回窗口内准入排队 P95；无样本时返回 None。"""
        cutoff = time.time() - window_minutes * 60
        with self.lock:
            self.guard_queue_wait_events = [
                item for item in self.guard_queue_wait_events if item[0] >= cutoff
            ]
            values = [value for _, value in self.guard_queue_wait_events]
            return _percentile(values, 95) if values else None

    def guard_rejected_rate_recent(self, reason: str, window_minutes: int) -> float:
        """返回窗口内指定拒绝原因的平均每分钟增速。"""
        cutoff = time.time() - window_minutes * 60
        with self.lock:
            self.guard_rejected_events = [
                item for item in self.guard_rejected_events if item[0] >= cutoff
            ]
            count = sum(
                1 for _, event_reason in self.guard_rejected_events if event_reason == reason
            )
            return count / max(1, window_minutes)

    def guard_upstream_429_rate_recent(self, window_minutes: int) -> float:
        """返回窗口内上游 429 的平均每分钟增速。"""
        cutoff = time.time() - window_minutes * 60
        with self.lock:
            self.guard_upstream_429_events = [
                event_at for event_at in self.guard_upstream_429_events if event_at >= cutoff
            ]
            return len(self.guard_upstream_429_events) / max(1, window_minutes)

    def guard_cb_open_seconds(self) -> float:
        """返回当前所有端点中持续 open 最久的秒数。"""
        now = time.time()
        with self.lock:
            durations = [
                now
                - self.cb_unavailable_since.get(
                    endpoint,
                    self.cb_state_changed_at.get(endpoint, now),
                )
                for endpoint, state in self.cb_state.items()
                if state in {1, 2}
            ]
            remote = [
                float(snapshot.get("cb_open_seconds", 0) or 0)
                for snapshot in self.remote_guard_snapshots.values()
            ]
            return max([*durations, *remote], default=0.0)

    def guard_degrade_seconds(self) -> float:
        """返回当前连续处于 D1/D2 的秒数。"""
        with self.lock:
            if self.degrade_level < 1:
                return 0.0
            since = self.degrade_nonzero_since or time.time()
            return max(0.0, time.time() - since)

    def guard_snapshot(
        self,
        *,
        first_outcome_after: int | None = None,
        queue_wait_after: int | None = None,
        degrade_after: int | None = None,
    ) -> dict:
        """返回本进程与 Redis 桥接远端进程的聚合防护指标。"""
        with self.lock:
            return self._guard_snapshot_locked(
                first_outcome_after=first_outcome_after,
                queue_wait_after=queue_wait_after,
                degrade_after=degrade_after,
            )

    def local_guard_snapshot(self) -> dict:
        """Return only this process, for publishing without aggregation loops."""
        with self.lock:
            return self._local_guard_snapshot_locked()

    def set_remote_guard_snapshots(self, snapshots: dict[str, dict]) -> None:
        """Replace live remote process samples and preserve 429 rate deltas."""
        now = time.time()
        with self.lock:
            for instance_id, snapshot in snapshots.items():
                current = int(snapshot.get("guard_upstream_429_total", 0) or 0)
                previous = self.remote_guard_429_seen.get(instance_id)
                # The first observation is a baseline, not a burst of recent 429s.
                delta = 0 if previous is None else max(0, current - previous)
                self.guard_upstream_429_events.extend(now for _ in range(delta))
                self.remote_guard_429_seen[instance_id] = current
            self.guard_upstream_429_events = self.guard_upstream_429_events[-10_000:]
            self.remote_guard_snapshots = {
                str(instance_id): dict(snapshot)
                for instance_id, snapshot in snapshots.items()
            }

    def _guard_snapshot_locked(
        self,
        *,
        first_outcome_after: int | None = None,
        queue_wait_after: int | None = None,
        degrade_after: int | None = None,
    ) -> dict:
        result = self._local_guard_snapshot_locked(
            first_outcome_after=first_outcome_after,
            queue_wait_after=queue_wait_after,
            degrade_after=degrade_after,
        )
        for snapshot in self.remote_guard_snapshots.values():
            for pool, value in (snapshot.get("guard_llm_inflight") or {}).items():
                result["guard_llm_inflight"][pool] = (
                    result["guard_llm_inflight"].get(pool, 0) + int(value or 0)
                )
            for pool, value in (snapshot.get("guard_llm_effective_max") or {}).items():
                result["guard_llm_effective_max"][pool] = (
                    result["guard_llm_effective_max"].get(pool, 0) + int(value or 0)
                )
            for pool, value in (snapshot.get("guard_llm_admitted_total") or {}).items():
                result["guard_llm_admitted_total"][pool] = (
                    result["guard_llm_admitted_total"].get(pool, 0) + int(value or 0)
                )
            remote_outcomes = int(snapshot.get("guard_first_outcome_samples", 0) or 0)
            if first_outcome_after is None:
                result["guard_first_outcome_samples"] += remote_outcomes
                result["guard_first_outcome_max_ms"] = max(
                    result["guard_first_outcome_max_ms"],
                    float(snapshot.get("guard_first_outcome_max_ms", 0) or 0),
                )
                result["guard_first_outcome_p95_ms"] = max(
                    result["guard_first_outcome_p95_ms"],
                    float(snapshot.get("guard_first_outcome_p95_ms", 0) or 0),
                )
            elif remote_outcomes > 0:
                # Sequence IDs are process-local, so a multi-process window
                # cannot be reconstructed from aggregate snapshots.
                result["guard_first_outcome_window_complete"] = False
            result["guard_stream_inflight"] += int(
                snapshot.get("guard_stream_inflight", 0) or 0
            )
            result["guard_queue_length"] += int(
                snapshot.get("guard_queue_length", 0) or 0
            )
            remote_queue_waits = int(snapshot.get("guard_queue_wait_samples", 0) or 0)
            if queue_wait_after is None:
                result["guard_queue_wait_p95_ms"] = max(
                    result["guard_queue_wait_p95_ms"],
                    float(snapshot.get("guard_queue_wait_p95_ms", 0) or 0),
                )
                result["guard_queue_wait_samples"] += remote_queue_waits
            elif remote_queue_waits > 0:
                # Queue sample sequence IDs are process-local. Formal load
                # acceptance runs one API worker; refuse a potentially diluted
                # multi-process window instead of pretending it is complete.
                result["guard_queue_wait_window_complete"] = False
            for reason, value in (snapshot.get("guard_rejected_total") or {}).items():
                result["guard_rejected_total"][reason] = (
                    result["guard_rejected_total"].get(reason, 0) + int(value or 0)
                )
            result["guard_upstream_429_total"] += int(
                snapshot.get("guard_upstream_429_total", 0) or 0
            )
            for endpoint, value in (snapshot.get("cb_state") or {}).items():
                result["cb_state"][endpoint] = max(
                    result["cb_state"].get(endpoint, 0),
                    int(value or 0),
                )
            result["cb_open_seconds"] = max(
                result["cb_open_seconds"],
                float(snapshot.get("cb_open_seconds", 0) or 0),
            )
            result["degrade_level"] = max(
                result["degrade_level"],
                int(snapshot.get("degrade_level", 0) or 0),
            )
            result["degrade_seconds"] = max(
                result["degrade_seconds"],
                float(snapshot.get("degrade_seconds", 0) or 0),
            )
            remote_degrade_transitions = int(
                snapshot.get("degrade_transition_samples", 0) or 0
            )
            if degrade_after is None:
                result["degrade_transition_samples"] += remote_degrade_transitions
            elif remote_degrade_transitions > 0:
                result["degrade_transition_window_complete"] = False
            result["stream_cancelled_total"] += int(
                snapshot.get("stream_cancelled_total", 0) or 0
            )
            result["llm_orphan_calls_total"] += int(
                snapshot.get("llm_orphan_calls_total", 0) or 0
            )
            for outcome, value in (snapshot.get("semantic_cache_lookups") or {}).items():
                result["semantic_cache_lookups"][outcome] = (
                    result["semantic_cache_lookups"].get(outcome, 0) + int(value or 0)
                )
        return result

    def _local_guard_snapshot_locked(
        self,
        *,
        first_outcome_after: int | None = None,
        queue_wait_after: int | None = None,
        degrade_after: int | None = None,
    ) -> dict:
        if first_outcome_after is None:
            first_outcome_samples = self.guard_first_outcome_samples
            first_outcome_max_ms = self.guard_first_outcome_max_ms
            first_outcome_values = [
                value for _, value in self.guard_first_outcome_events
            ]
            first_outcome_window_complete = True
        else:
            after = max(0, int(first_outcome_after))
            retained_from = (
                self.guard_first_outcome_events[0][0]
                if self.guard_first_outcome_events
                else self.guard_first_outcome_samples + 1
            )
            values = [
                value
                for sequence, value in self.guard_first_outcome_events
                if sequence > after
            ]
            first_outcome_samples = len(values)
            first_outcome_max_ms = max(values, default=0.0)
            first_outcome_values = values
            first_outcome_window_complete = (
                after <= self.guard_first_outcome_samples
                and after >= retained_from - 1
            )
        if queue_wait_after is None:
            queue_wait_samples = self.guard_queue_wait_samples
            queue_wait_values = self.guard_queue_wait_ms
            queue_wait_window_complete = True
        else:
            after = max(0, int(queue_wait_after))
            retained_from = (
                self.guard_queue_wait_sample_events[0][0]
                if self.guard_queue_wait_sample_events
                else self.guard_queue_wait_samples + 1
            )
            queue_wait_values = [
                value
                for sequence, value in self.guard_queue_wait_sample_events
                if sequence > after
            ]
            queue_wait_samples = len(queue_wait_values)
            queue_wait_window_complete = (
                after <= self.guard_queue_wait_samples
                and after >= retained_from - 1
            )
        if degrade_after is None:
            degrade_transition_samples = self.degrade_transition_samples
            degrade_transitions: list[dict[str, int | float]] = []
            degrade_transition_window_complete = True
        else:
            after = max(0, int(degrade_after))
            retained_from = (
                self.degrade_transition_events[0][0]
                if self.degrade_transition_events
                else self.degrade_transition_samples + 1
            )
            transitions = [
                (sequence, at_ms, level)
                for sequence, at_ms, level in self.degrade_transition_events
                if sequence > after
            ]
            degrade_transition_samples = len(transitions)
            degrade_transitions = [
                {"sequence": sequence, "at_ms": round(at_ms, 3), "level": level}
                for sequence, at_ms, level in transitions
            ]
            degrade_transition_window_complete = (
                after <= self.degrade_transition_samples
                and after >= retained_from - 1
            )
        return {
            "guard_llm_inflight": dict(self.guard_llm_inflight),
            "guard_llm_effective_max": dict(self.guard_llm_effective_max),
            "guard_llm_admitted_total": dict(self.guard_llm_admitted_total),
            "guard_first_outcome_samples": first_outcome_samples,
            "guard_first_outcome_max_ms": round(first_outcome_max_ms, 2),
            "guard_first_outcome_p95_ms": (
                round(_percentile(first_outcome_values, 95), 2)
                if first_outcome_values
                else 0
            ),
            "guard_first_outcome_window_complete": first_outcome_window_complete,
            "guard_stream_inflight": self.guard_stream_inflight,
            "guard_queue_length": self.guard_queue_length,
            "guard_queue_wait_samples": queue_wait_samples,
            "guard_queue_wait_p95_ms": (
                round(_percentile(queue_wait_values, 95), 2)
                if queue_wait_values
                else 0
            ),
            "guard_queue_wait_window_complete": queue_wait_window_complete,
            "guard_rejected_total": dict(self.guard_rejected_total),
            "guard_upstream_429_total": self.guard_upstream_429_total,
            "cb_state": dict(self.cb_state),
            "cb_open_seconds": max(
                (
                    time.time()
                    - self.cb_unavailable_since.get(
                        endpoint,
                        self.cb_state_changed_at.get(endpoint, time.time()),
                    )
                    for endpoint, state in self.cb_state.items()
                    if state in {1, 2}
                ),
                default=0.0,
            ),
            "degrade_level": self.degrade_level,
            "degrade_seconds": (
                max(0.0, time.time() - self.degrade_nonzero_since)
                if self.degrade_level >= 1 and self.degrade_nonzero_since is not None
                else 0.0
            ),
            "degrade_transition_samples": degrade_transition_samples,
            "degrade_transitions": degrade_transitions,
            "degrade_transition_window_complete": (
                degrade_transition_window_complete
            ),
            "stream_cancelled_total": self.stream_cancelled_total,
            "llm_orphan_calls_total": self.llm_orphan_calls_total,
            "semantic_cache_lookups": dict(self.semantic_cache_lookups),
            "chat_run_active": dict(self.chat_run_active),
            "chat_run_queued": self.chat_run_active.get("queued", 0),
            "chat_run_queue_wait_p95_ms": (
                round(_percentile(self.chat_run_queue_wait_ms, 95), 2)
                if self.chat_run_queue_wait_ms
                else 0
            ),
            "chat_run_duration_p95_ms": (
                round(_percentile(self.chat_run_duration_ms, 95), 2)
                if self.chat_run_duration_ms
                else 0
            ),
            "chat_run_subscribers": self.chat_run_subscribers,
            "chat_run_reconnect_total": self.chat_run_reconnect_total,
            "chat_run_replayed_events_total": self.chat_run_replayed_events_total,
            "chat_run_cancel_total": dict(self.chat_run_cancel_total),
            "chat_run_recovery_total": dict(self.chat_run_recovery_total),
            "chat_run_event_lag": self.chat_run_event_lag,
            "chat_run_event_cleanup_total": self.chat_run_event_cleanup_total,
            "chat_run_event_cleanup_duration_p95_ms": (
                round(_percentile(self.chat_run_event_cleanup_duration_ms, 95), 2)
                if self.chat_run_event_cleanup_duration_ms
                else None
            ),
            "chat_run_event_cleanup_failures_total": (
                self.chat_run_event_cleanup_failures_total
            ),
        }

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
                "concurrency_guard": self._guard_snapshot_locked(),
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
                        "avg_confidence": round(item["confidence_total"] / item["count"], 4),
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
                        )
                        if requests
                        else 0.0,
                        "tokens": dict(self.agent_tokens[agent_id]),
                        "latency_p50_ms": round(
                            statistics.median(self.agent_latencies[agent_id]), 2
                        )
                        if self.agent_latencies[agent_id]
                        else 0,
                        "latency_p95_ms": round(_percentile(self.agent_latencies[agent_id], 95), 2)
                        if self.agent_latencies[agent_id]
                        else 0,
                    }
                    for agent_id, requests in self.agent_requests.items()
                }
            # 聚合全量
            total_req = sum(self.requests.values())
            total_err = sum(self.errors.values())
            all_lat = [v for vals in self.latencies.values() for v in vals]
            total_tokens = {
                k: sum(self.tokens[t][k] for t in self.tokens)
                for k in ("prompt", "completion", "total")
            }
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
                "concurrency_guard": self._guard_snapshot_locked(),
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
