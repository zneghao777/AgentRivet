"""Acceptance mock must expose real streaming lifetime and request parameters."""

from __future__ import annotations

import pytest

from tests.load import mock_llm


@pytest.mark.asyncio
async def test_stream_tracking_lives_until_generator_close():
    await mock_llm.control(
        mock_llm.ControlRequest(mode="slow", delay_ms=0, reset_stats=True)
    )
    body = {"model": "mock", "stream": True, "max_tokens": 123}
    await mock_llm._begin_call(body)
    stream = mock_llm._tracked_stream_response(body, 0)

    first = await anext(stream)
    assert first.startswith("data: ")
    assert mock_llm.state.inflight == 1
    assert mock_llm.state.request_parameters[-1]["max_tokens"] == 123

    await stream.aclose()
    assert mock_llm.state.inflight == 0


@pytest.mark.asyncio
async def test_rate_limit_events_are_timestamped_at_capacity_boundary():
    await mock_llm.control(
        mock_llm.ControlRequest(
            mode="rate_limit",
            delay_ms=0,
            upstream_max_concurrency=1,
            reset_stats=True,
        )
    )
    await mock_llm._begin_call({"stream": False, "max_tokens": 10})
    second_inflight = await mock_llm._begin_call({"stream": False, "max_tokens": 5})
    try:
        assert second_inflight == 2
        mock_llm.state.rate_limits += 1
        mock_llm.state.rate_limit_events_ms.append(1)
        snapshot = mock_llm.snapshot()
        assert snapshot["upstream_max_concurrency"] == 1
        assert snapshot["rate_limit_events_ms"] == [1]
    finally:
        await mock_llm._end_call()
        await mock_llm._end_call()


@pytest.mark.asyncio
async def test_client_outcomes_are_reset_and_count_successes_separately():
    await mock_llm.control(
        mock_llm.ControlRequest(mode="ok", delay_ms=0, reset_stats=True)
    )

    await mock_llm.client_outcome(
        mock_llm.ClientOutcomeRequest(success=True, first_outcome=True)
    )
    await mock_llm.client_outcome(
        mock_llm.ClientOutcomeRequest(success=False, first_outcome=False)
    )

    snapshot = mock_llm.snapshot()
    assert snapshot["client_outcomes"] == 2
    assert snapshot["first_outcome_requests"] == 1
    assert snapshot["successful_requests"] == 1

    await mock_llm.control(
        mock_llm.ControlRequest(mode="ok", delay_ms=0, reset_stats=True)
    )
    assert mock_llm.snapshot()["client_outcomes"] == 0
    assert mock_llm.snapshot()["first_outcome_requests"] == 0
    assert mock_llm.snapshot()["successful_requests"] == 0
