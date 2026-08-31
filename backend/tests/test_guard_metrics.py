"""CONCURRENCY_GUARD_SPEC §12：防护指标与默认告警。"""

from __future__ import annotations

import time

import pytest

from app.observability.alerts import _DEFAULT_RULES
from app.observability.metrics import MetricStore, metrics


def test_guard_metrics_are_visible_in_summary_and_collector_health():
    store = MetricStore()
    store.set_guard_llm_inflight("online", 3)
    store.set_guard_llm_inflight("background", 1)
    store.set_guard_llm_effective_max("online", 80)
    store.set_guard_llm_effective_max("background", 10)
    store.record_guard_llm_admitted("online", count=3)
    store.record_guard_llm_admitted("background")
    store.record_guard_first_outcome(120)
    store.record_guard_first_outcome(240)
    store.set_guard_stream_inflight(4)
    store.set_guard_queue_length(2)
    store.record_guard_queue_wait(100)
    store.record_guard_queue_wait(300)
    store.record_guard_rejected("queue_full")
    store.record_upstream_429()
    store.set_cb_state("https://llm|model", "open")
    store.set_degrade_level(1)
    store.record_stream_cancelled()
    store.record_llm_orphan_call()
    store.record_semantic_cache_lookup("hit")

    snapshot = store.guard_snapshot()
    assert snapshot["guard_llm_inflight"] == {"online": 3, "background": 1}
    assert snapshot["guard_llm_effective_max"] == {"online": 80, "background": 10}
    assert snapshot["guard_llm_admitted_total"] == {"online": 3, "background": 1}
    assert snapshot["guard_first_outcome_samples"] == 2
    assert snapshot["guard_first_outcome_max_ms"] == 240
    assert snapshot["guard_first_outcome_p95_ms"] == pytest.approx(234)
    assert snapshot["guard_first_outcome_window_complete"] is True
    assert snapshot["guard_stream_inflight"] == 4
    assert snapshot["guard_queue_length"] == 2
    assert snapshot["guard_queue_wait_samples"] == 2
    assert snapshot["guard_queue_wait_p95_ms"] == pytest.approx(290)
    assert snapshot["guard_queue_wait_window_complete"] is True
    assert snapshot["guard_rejected_total"] == {"queue_full": 1}
    assert snapshot["guard_upstream_429_total"] == 1
    assert snapshot["cb_state"] == {"https://llm|model": 2}
    assert snapshot["degrade_level"] == 1
    assert snapshot["degrade_transition_samples"] == 1
    assert snapshot["degrade_transition_window_complete"] is True
    assert snapshot["stream_cancelled_total"] == 1
    assert snapshot["llm_orphan_calls_total"] == 1
    assert snapshot["semantic_cache_lookups"] == {"hit": 1}
    assert store.summary()["concurrency_guard"]["guard_queue_length"] == 2
    assert store.summary()["concurrency_guard"]["guard_queue_wait_samples"] == 2
    assert store.collector_health()["concurrency_guard"]["degrade_level"] == 1
    assert store.collector_health()["concurrency_guard"]["guard_queue_wait_samples"] == 2


def test_guard_llm_admitted_total_is_explicit_and_independent_from_gauge_changes():
    store = MetricStore()

    store.set_guard_llm_inflight("online", 1)
    store.set_guard_llm_inflight("online", 2)
    store.set_guard_llm_inflight("online", 1)
    store.set_guard_llm_inflight("online", 3)
    store.set_guard_llm_inflight("online", 3)
    store.record_guard_llm_admitted("online")
    store.record_guard_llm_admitted("online", count=3)

    snapshot = store.guard_snapshot()
    assert snapshot["guard_llm_inflight"] == {"online": 3}
    assert snapshot["guard_llm_admitted_total"] == {"online": 4}


def test_guard_first_outcome_window_uses_sequence_baseline():
    store = MetricStore()
    store.record_guard_first_outcome(100)
    baseline = store.guard_snapshot()["guard_first_outcome_samples"]
    store.record_guard_first_outcome(250)
    store.record_guard_first_outcome(150)

    window = store.guard_snapshot(first_outcome_after=baseline)
    assert window["guard_first_outcome_samples"] == 2
    assert window["guard_first_outcome_max_ms"] == 250
    assert window["guard_first_outcome_p95_ms"] == pytest.approx(245)
    assert window["guard_first_outcome_window_complete"] is True

    invalid = store.guard_snapshot(first_outcome_after=baseline + 99)
    assert invalid["guard_first_outcome_samples"] == 0
    assert invalid["guard_first_outcome_window_complete"] is False


def test_guard_queue_wait_window_uses_sequence_baseline():
    store = MetricStore()
    store.record_guard_queue_wait(100)
    baseline = store.guard_snapshot()["guard_queue_wait_samples"]
    store.record_guard_queue_wait(70_000)
    store.record_guard_queue_wait(90_000)

    window = store.guard_snapshot(queue_wait_after=baseline)
    assert window["guard_queue_wait_samples"] == 2
    assert window["guard_queue_wait_p95_ms"] == pytest.approx(89_000)
    assert window["guard_queue_wait_window_complete"] is True

    invalid = store.guard_snapshot(queue_wait_after=baseline + 99)
    assert invalid["guard_queue_wait_samples"] == 0
    assert invalid["guard_queue_wait_window_complete"] is False


def test_degrade_transition_window_preserves_timing_and_sequence():
    store = MetricStore()
    baseline = store.guard_snapshot()["degrade_transition_samples"]
    store.set_degrade_level(1)
    store.set_degrade_level(1)
    store.set_degrade_level(0)

    window = store.guard_snapshot(degrade_after=baseline)
    assert window["degrade_transition_samples"] == 2
    assert [event["level"] for event in window["degrade_transitions"]] == [1, 0]
    assert window["degrade_transitions"][1]["at_ms"] >= window["degrade_transitions"][0]["at_ms"]
    assert window["degrade_transition_window_complete"] is True

    invalid = store.guard_snapshot(degrade_after=baseline + 99)
    assert invalid["degrade_transition_samples"] == 0
    assert invalid["degrade_transition_window_complete"] is False


@pytest.mark.asyncio
async def test_metrics_api_exposes_first_outcome_window(app_client):
    baseline = metrics.guard_snapshot()["guard_first_outcome_samples"]
    metrics.record_guard_first_outcome(321)

    response = await app_client.get(
        "/api/v1/metrics",
        params={"guard_first_outcome_after": baseline},
        headers={"X-API-Key": "test-key-1"},
    )

    assert response.status_code == 200
    snapshot = response.json()["data"]["concurrency_guard"]
    assert snapshot["guard_first_outcome_samples"] == 1
    assert snapshot["guard_first_outcome_max_ms"] == 321
    assert snapshot["guard_first_outcome_p95_ms"] == 321
    assert snapshot["guard_first_outcome_window_complete"] is True


@pytest.mark.asyncio
async def test_metrics_api_exposes_queue_wait_window(app_client):
    baseline = metrics.guard_snapshot()["guard_queue_wait_samples"]
    metrics.record_guard_queue_wait(456)

    response = await app_client.get(
        "/api/v1/metrics",
        params={"guard_queue_wait_after": baseline},
        headers={"X-API-Key": "test-key-1"},
    )

    assert response.status_code == 200
    snapshot = response.json()["data"]["concurrency_guard"]
    assert snapshot["guard_queue_wait_samples"] == 1
    assert snapshot["guard_queue_wait_p95_ms"] == 456
    assert snapshot["guard_queue_wait_window_complete"] is True


@pytest.mark.asyncio
async def test_metrics_api_exposes_degrade_transition_window(app_client):
    metrics.set_degrade_level(0)
    baseline = metrics.guard_snapshot()["degrade_transition_samples"]
    metrics.set_degrade_level(1)
    metrics.set_degrade_level(0)

    response = await app_client.get(
        "/api/v1/metrics",
        params={"guard_degrade_after": baseline},
        headers={"X-API-Key": "test-key-1"},
    )

    assert response.status_code == 200
    snapshot = response.json()["data"]["concurrency_guard"]
    assert [event["level"] for event in snapshot["degrade_transitions"]] == [1, 0]
    assert snapshot["degrade_transition_window_complete"] is True


def test_recent_guard_rates_and_sustained_state_durations():
    store = MetricStore()
    old = time.time() - 20 * 60
    store.guard_queue_wait_events = [(old, 99_000)]
    store.record_guard_queue_wait(31_000)
    store.record_guard_rejected("queue_full", count=11)
    store.record_upstream_429(count=6)
    store.set_cb_state("endpoint", "open")
    store.cb_unavailable_since["endpoint"] = time.time() - 301
    store.set_degrade_level(1)
    store.degrade_nonzero_since = time.time() - 1801
    store.set_degrade_level(2)

    assert store.guard_queue_wait_p95_recent(15) == 31_000
    assert store.guard_rejected_rate_recent("queue_full", 1) == 11
    assert store.guard_upstream_429_rate_recent(2) == 3
    assert store.guard_cb_open_seconds() >= 300
    store.set_cb_state("endpoint", "half_open")
    assert store.guard_cb_open_seconds() >= 300
    store.set_cb_state("endpoint", "open")
    assert store.guard_cb_open_seconds() >= 300
    # D1 -> D2 不得重置“持续处于降级态”的计时。
    assert store.guard_degrade_seconds() >= 1800
    store.set_degrade_level(0)
    assert store.guard_degrade_seconds() == 0


def test_remote_worker_guard_snapshot_is_aggregated_without_feedback():
    store = MetricStore()
    store.set_guard_llm_inflight("online", 2)
    store.set_guard_llm_effective_max("online", 80)
    store.record_guard_llm_admitted("online", count=2)
    store.record_upstream_429(count=1)
    store.set_remote_guard_snapshots(
        {
            "session-worker": {
                "guard_llm_inflight": {"background": 4},
                "guard_llm_effective_max": {"background": 10},
                "guard_llm_admitted_total": {"background": 7},
                "guard_first_outcome_samples": 0,
                "guard_first_outcome_max_ms": 0,
                "guard_stream_inflight": 0,
                "guard_queue_length": 0,
                "guard_queue_wait_samples": 4,
                "guard_queue_wait_p95_ms": 0,
                "guard_rejected_total": {},
                "guard_upstream_429_total": 3,
                "cb_state": {"worker-endpoint": 2},
                "cb_open_seconds": 301,
                "degrade_level": 0,
                "degrade_seconds": 0,
                "stream_cancelled_total": 0,
                "llm_orphan_calls_total": 0,
                "semantic_cache_lookups": {},
            }
        }
    )

    local = store.local_guard_snapshot()
    aggregate = store.guard_snapshot()
    assert local["guard_llm_inflight"] == {"online": 2}
    assert aggregate["guard_llm_inflight"] == {"online": 2, "background": 4}
    assert aggregate["guard_llm_effective_max"] == {"online": 80, "background": 10}
    assert aggregate["guard_llm_admitted_total"] == {"online": 2, "background": 7}
    assert aggregate["guard_queue_wait_samples"] == 4
    assert aggregate["guard_upstream_429_total"] == 4
    assert aggregate["cb_state"]["worker-endpoint"] == 2
    assert store.guard_cb_open_seconds() >= 301

    event_count = len(store.guard_upstream_429_events)
    store.set_remote_guard_snapshots({})
    store.set_remote_guard_snapshots(
        {
            "session-worker": {
                **store.remote_guard_snapshots.get("session-worker", {}),
                "guard_upstream_429_total": 3,
            }
        }
    )
    assert len(store.guard_upstream_429_events) == event_count


def test_remote_guard_429_first_snapshot_is_baseline_only():
    store = MetricStore()

    store.set_remote_guard_snapshots(
        {"session-worker": {"guard_upstream_429_total": 100}}
    )
    assert store.guard_upstream_429_rate_recent(1) == 0

    store.set_remote_guard_snapshots(
        {"session-worker": {"guard_upstream_429_total": 100}}
    )
    assert store.guard_upstream_429_rate_recent(1) == 0

    store.set_remote_guard_snapshots(
        {"session-worker": {"guard_upstream_429_total": 101}}
    )
    assert store.guard_upstream_429_rate_recent(1) == 1


def test_default_guard_alert_rules_cover_spec_thresholds():
    rules = {rule["name"]: rule for rule in _DEFAULT_RULES}
    assert rules["guard-queue-wait-p95"]["threshold"] == 30_000
    assert rules["guard-queue-wait-p95"]["window_minutes"] == 15
    assert rules["guard-queue-wait-p95"]["severity"] == "warning"
    assert rules["guard-queue-full-rate"]["threshold"] == 10
    assert rules["guard-queue-full-rate"]["window_minutes"] == 5
    assert rules["guard-queue-full-rate"]["severity"] == "critical"
    assert rules["guard-circuit-open"]["threshold"] == 300
    assert rules["guard-circuit-open"]["severity"] == "critical"
    assert rules["guard-degrade-sustained"]["threshold"] == 1800
    assert "guard-upstream-429-rate" in rules
