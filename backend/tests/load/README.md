# Concurrency guard load acceptance

This directory provides a deterministic OpenAI-compatible mock and one k6 suite
for all seven acceptance scenarios in `CONCURRENCY_GUARD_SPEC` section 14. The
suite checks outcomes from SSE frames, guard metrics, observability TTFT spans,
and the mock's recorded request parameters. It is a mock-upstream acceptance
test, not evidence of production hardware capacity.

## Start the isolated services

Generate enough distinct, valid API keys for the 300-VU execution pool used by
the staged upstream-429 scenario, then start the mock and a dedicated backend.
Capacity scenarios intentionally relax L1 request-rate limits so they reach
L2-L5; L1 behavior remains covered by `tests/test_rate_limit.py`.

```bash
cd backend
export LOAD_API_KEYS="$(python3 -c 'print(",".join(f"load-key-{i}" for i in range(1, 351)))')"

.venv/bin/uvicorn tests.load.mock_llm:app --host 127.0.0.1 --port 18080

APP_ENV=test \
API_KEYS="$LOAD_API_KEYS" \
LLM_MODEL=mock-model \
LLM_BASE_URL=http://127.0.0.1:18080/v1 \
LLM_API_KEY=mock-key \
LLM_MAX_TOKENS=4096 \
LLM_ALLOWED_BASE_HOSTS=127.0.0.1 \
GUARD_ENABLED=true \
LLM_MAX_CONCURRENCY=90 \
LLM_BG_MAX_CONCURRENCY=8 \
STREAM_MAX_CONCURRENCY=120 \
QUEUE_CAPACITY=100 \
QUEUE_TIMEOUT_SECONDS=60 \
RATE_LIMIT_STREAM_PER_KEY=100000/minute \
RATE_LIMIT_REST_PER_KEY=100000/minute \
RATE_LIMIT_PER_IP=100000/minute \
MULTI_AGENT_ENABLED=false \
SEMANTIC_CACHE_ENABLED=false \
MEMORY_ENABLED=false \
MQ_ENABLED=false MCP_ENABLED=false \
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

Run the mock and backend commands in separate terminals. PostgreSQL must be
available because the chat and observability APIs persist sessions and spans.
Before resetting the mock ledger, every scenario verifies that mock inflight is
zero. Wait for the previous run to drain, or restart the dedicated services, if
setup reports a nonzero baseline.

## Run a scenario

Pass the same key pool to k6 and explicitly acknowledge the isolated L1
overrides. Replace `p0_steady` with `spike`, `upstream_failure`, `upstream_429`,
`single_user`, `degrade`, or `disconnect`.

```bash
RATE_LIMITS_RELAXED=1 \
BACKGROUND_LLM_DISABLED=1 \
API_KEYS="$LOAD_API_KEYS" \
SCENARIO=p0_steady \
k6 run tests/load/concurrency_guard.js
```

For P0, `BACKGROUND_LLM_DISABLED=1` confirms that the isolated backend was
started with `MEMORY_ENABLED=false`. This keeps mock call counts limited to the
foreground chat chain. The default durations match the specification: P0 runs
for 10 minutes; degrade keeps utilization high past the 60-second entry hold
and then low past the 120-second exit hold. Shortening those durations turns
the run into a smoke test and cannot be reported as section 14 acceptance
evidence.

Formal acceptance mode rejects `DURATION`, `WARMUP`, `RAMP`, `HOLD`,
`HIGH_HOLD`, `LOW_HOLD`, `BURST_DURATION`, `RAMP_DOWN_DURATION`, and
`CONVERGENCE_DURATION` overrides for the scenario that uses them. Set
`SMOKE=1` explicitly to enable duration overrides. Formal mode also rejects
`VUS`, `RATE`, `SUSTAIN_RATE`, `PACING_SECONDS`, `EXPECTED_MAX_TOKENS`,
`MOCK_DELAY_MS`, and `UPSTREAM_MAX_CONCURRENCY`. This keeps scenario intensity,
request pacing, token expectations, mock latency, and the upstream concurrency
limit fixed at their acceptance values. Smoke mode is the only mode that reads
those overrides. Every k6 sample is tagged with `guard_test_mode=acceptance` or
`guard_test_mode=smoke`.

For example, run a short P0 smoke check with two keys:

```bash
RATE_LIMITS_RELAXED=1 \
BACKGROUND_LLM_DISABLED=1 \
SMOKE=1 DURATION=30s VUS=2 \
API_KEYS="load-key-1,load-key-2" \
SCENARIO=p0_steady \
k6 run tests/load/concurrency_guard.js
```

Smoke results are diagnostic only and are not section 14 acceptance evidence.

## Result interpretation

The k6 HTTP API buffers the response body, so its per-request timing cannot
recover when an individual SSE frame arrived. The backend therefore records
the elapsed time from stream request entry until it yields the first
`message_delta` or `error`. Teardown queries only samples after the setup
baseline. The mock records every iteration before session creation, every
completed response, and whether that response contained a first outcome.
Teardown requires the started, completed, client-first-outcome, and
server-first-outcome counts to match exactly. Every formal scenario also
requires zero `dropped_iterations`; non-disconnect scenarios require zero
transport-level HTTP failures. A killed or hung iteration therefore cannot
disappear from the denominator. The disconnect scenario intentionally aborts
clients before an outcome, so it uses a separate exact-count ledger.

- P0 requires no guard error, waits for the collector to drain, and reconciles
  successful SSE requests, mock LLM calls, persisted successful LLM spans, and
  spans containing TTFT. It also requires a zero `spans_dropped_total` delta,
  zero queue frames, a zero queue-wait sample delta, and TTFT P95 under three
  seconds. Call pagination continues to exhaustion; `MAX_CALL_PAGES` defaults
  to 1000 and exceeding it fails explicitly.
- Spike requires at least 30 distinct queued requests in formal mode, zero
  `42900` queue-full outcomes, integer/range-valid `position`, `queue_size`, and
  `estimated_wait_ms` fields, and non-increasing positions. It also requires a
  positive scenario-local queue-wait window with P95 under 60 seconds for
  section 14.1 and under 30 seconds for the P1 commitment in section 14.2. The
  rejection rate must stay below 1%, using every started iteration as the
  denominator. The server queue-wait sample count must equal the mock's distinct
  queued-request count. The sequence window must be complete, so one wait
  sample, one queued response, or waits from an earlier scenario cannot make
  the run pass.
- Upstream-failure setup requires every breaker to be closed. The run requires
  the mock to receive at least one request and return 500 for every received
  request, the breaker to open within 30 seconds, and every open-breaker request
  to fail within one second with `50300` and a positive integer
  `retry_after_ms`. The fast-fail timing sample excludes the final second of the
  cooldown window, so a legitimate half-open probe is never misclassified as
  an open-breaker fast failure.
- Upstream-429 setup requires `guard_llm_effective_max.online=90`. Its formal
  arrival plan sends 40 requests per second for five seconds, ramps to 20 per
  second over one second, and holds 20 per second for 44 seconds. The first
  phase exceeds the mock's 25-request upstream limit; the final phase stays
  below AIMD's 22-request floor for more than the required 30-second
  convergence window. The run must start and complete all 1,110 planned
  iterations with no dropped iterations. Teardown also requires the final
  effective limit to be below its setup baseline, and the mock's late 429 count
  and inflight peak to converge within the upstream limit.
- Single-user abuse requires exactly one completion and four `42901` responses.
- Degrade requires every frame to use level 1 or 2, a non-empty reason, and an
  effects array containing `thinking_disabled` and `max_tokens_reduced`. It
  requires a fixed 4096-token request before the measured D1 transition and an
  exactly halved 2048-token request after that transition. In formal mode, the
  mock inflight history must prove utilization stayed above 80% for the full
  60-second entry window and below 70% for the full 120-second exit window. The
  P2 rejection rate must stay below 5%, using every started iteration as the
  denominator.
- Disconnect setup requires both application and mock inflight to be zero. All
  30 formal iterations must be recorded and reach the mock, none may complete
  normally, mock peak inflight must reach 30, and the online admission counter
  and cancellation counter must each increase by exactly 30. Within five
  seconds of the last client-side abort timestamp, application guard inflight
  must return to exactly zero. The mock inflight value remains an observation:
  a synchronous provider request already running in an executor cannot be
  interrupted and may finish naturally as an orphan after the application slot
  is released. The next scenario still refuses to reset the mock until those
  calls drain. Smoke mode uses its configured `VUS` as the exact target.
