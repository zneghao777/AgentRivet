const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const script = fs.readFileSync(path.join(__dirname, "concurrency_guard.js"), "utf8");
const mockServer = fs.readFileSync(path.join(__dirname, "mock_llm.py"), "utf8");
const executable = script
  .replace(/^import .*;$/gm, "")
  .replace("export const options", "const options")
  .replace("export default function (data)", "function defaultFunction(data)")
  .replace("export function setup()", "function setup()")
  .replace("export function teardown(data)", "function teardown(data)");

class Metric {
  add() {}
}

function loadContract(env = {}) {
  return new Function(
    "__ENV",
    "http",
    "check",
    "sleep",
    "exec",
    "Counter",
    "Gauge",
    "Rate",
    "Trend",
    `${executable}; return {
      options,
      setup,
      scenarioNumber,
      scenarioDuration,
      durationSeconds,
      plannedIterationsFor,
      requiredKeyCount,
      mockControlFor,
      parseFrames,
      errorCodes,
      validQueuePayload,
      validDegradePayload,
      hasPositiveRetryAfter,
      isFastFailCircuitSnapshot,
      onlineEffectiveMax,
      validateScenarioBaseline,
      rejectionRate,
      utilizationWindowProven,
    };`,
  )(
    env,
    {},
    () => true,
    () => {},
    { vu: {}, scenario: {} },
    Metric,
    Metric,
    Metric,
    Metric,
  );
}

test("guard error parsing preserves queue and response timeout codes", () => {
  const contract = loadContract();
  const body =
    'event: error\ndata: {"code":50400,"message":"queue timeout"}\n\n' +
    'event: error\ndata: {"code":50401,"message":"first outcome timeout"}\n\n' +
    'event: error\ndata: {"code":50402,"message":"model response timeout"}\n\n';

  assert.deepEqual(contract.errorCodes(body), [50400, 50401, 50402]);
});

test("acceptance ignores and rejects load or expectation overrides", () => {
  const env = {
    SCENARIO: "p0_steady",
    VUS: "2",
    RATE: "1",
    SUSTAIN_RATE: "1",
    PACING_SECONDS: "0",
    EXPECTED_MAX_TOKENS: "32",
    MOCK_DELAY_MS: "0",
    UPSTREAM_MAX_CONCURRENCY: "999",
  };
  const contract = loadContract(env);

  assert.equal(contract.options.scenarios.p0_steady.vus, 30);
  assert.equal(contract.scenarioNumber("PACING_SECONDS", 1), 1);
  assert.equal(contract.scenarioNumber("EXPECTED_MAX_TOKENS", 4096), 4096);
  assert.throws(
    () => contract.setup(),
    /Load or expectation overrides require SMOKE=1/,
  );

  const upstream = loadContract({
    SCENARIO: "upstream_429",
    RATE: "1",
    SUSTAIN_RATE: "1",
    MOCK_DELAY_MS: "0",
    UPSTREAM_MAX_CONCURRENCY: "999",
  });
  assert.equal(upstream.options.scenarios.upstream_429.startRate, 40);
  assert.deepEqual(upstream.options.scenarios.upstream_429.stages, [
    { duration: "5s", target: 40 },
    { duration: "1s", target: 20 },
    { duration: "44s", target: 20 },
  ]);
  assert.equal(upstream.options.scenarios.upstream_429.preAllocatedVUs, 300);
  assert.equal(upstream.options.scenarios.upstream_429.maxVUs, 300);
  assert.equal(upstream.requiredKeyCount("upstream_429"), 300);
  assert.deepEqual(upstream.mockControlFor("upstream_429"), {
    reset_stats: true,
    upstream_max_concurrency: 25,
    mode: "rate_limit",
    delay_ms: 1000,
  });

  const disconnect = loadContract({ SCENARIO: "disconnect", VUS: "2" });
  assert.equal(disconnect.options.scenarios.disconnect.vus, 30);
  assert.deepEqual(disconnect.options.thresholds.guard_disconnect_mock_calls, [
    "value==30",
  ]);
  assert.deepEqual(disconnect.options.thresholds.guard_disconnect_peak_inflight, [
    "value>=30",
  ]);
  assert.deepEqual(disconnect.options.thresholds.guard_disconnect_llm_admitted_delta, [
    "value==30",
  ]);
  assert.deepEqual(disconnect.options.thresholds.guard_stream_cancelled_delta, [
    "value==30",
  ]);

  const spike = loadContract({ SCENARIO: "spike", VUS: "2" });
  assert.equal(spike.options.scenarios.spike.stages[1].target, 150);
  assert.equal(spike.options.scenarios.spike.stages[2].target, 150);
  assert.deepEqual(
    spike.options.thresholds.guard_queue_wait_window_complete,
    ["value==1"],
  );
  const degrade = loadContract({ SCENARIO: "degrade", VUS: "2" });
  assert.equal(degrade.options.scenarios.degrade.stages[0].target, 150);
  assert.equal(degrade.options.scenarios.degrade.stages[1].target, 150);
});

test("SMOKE=1 applies load and expectation overrides", () => {
  const p0 = loadContract({
    SCENARIO: "p0_steady",
    SMOKE: "1",
    VUS: "2",
    DURATION: "5s",
    PACING_SECONDS: "0",
    EXPECTED_MAX_TOKENS: "32",
  });
  assert.equal(p0.options.scenarios.p0_steady.vus, 2);
  assert.equal(p0.options.scenarios.p0_steady.duration, "5s");
  assert.equal(p0.scenarioNumber("PACING_SECONDS", 1), 0);
  assert.equal(p0.scenarioNumber("EXPECTED_MAX_TOKENS", 4096), 32);
  assert.equal(p0.requiredKeyCount("p0_steady"), 2);

  const upstream = loadContract({
    SCENARIO: "upstream_429",
    SMOKE: "1",
    RATE: "3",
    SUSTAIN_RATE: "2",
    BURST_DURATION: "2s",
    RAMP_DOWN_DURATION: "1s",
    CONVERGENCE_DURATION: "3s",
    MOCK_DELAY_MS: "10",
    UPSTREAM_MAX_CONCURRENCY: "7",
  });
  assert.equal(upstream.options.scenarios.upstream_429.startRate, 3);
  assert.deepEqual(upstream.options.scenarios.upstream_429.stages, [
    { duration: "2s", target: 3 },
    { duration: "1s", target: 2 },
    { duration: "3s", target: 2 },
  ]);
  assert.equal(upstream.mockControlFor("upstream_429").delay_ms, 10);
  assert.equal(upstream.mockControlFor("upstream_429").upstream_max_concurrency, 7);

  const disconnect = loadContract({ SCENARIO: "disconnect", SMOKE: "1", VUS: "2" });
  assert.equal(disconnect.options.scenarios.disconnect.vus, 2);
  assert.deepEqual(disconnect.options.thresholds.guard_disconnect_mock_calls, [
    "value==2",
  ]);
  assert.deepEqual(disconnect.options.thresholds.guard_disconnect_llm_admitted_delta, [
    "value==2",
  ]);
  assert.deepEqual(disconnect.options.thresholds.guard_stream_cancelled_delta, [
    "value==2",
  ]);
});

test("setup rejects unknown scenarios and formal duration overrides", () => {
  assert.throws(
    () => loadContract({ SCENARIO: "unknown" }).setup(),
    /Unknown SCENARIO=unknown/,
  );
  assert.throws(
    () => loadContract({ SCENARIO: "p0_steady", DURATION: "1s" }).setup(),
    /Duration overrides require SMOKE=1/,
  );
  assert.throws(
    () =>
      loadContract({
        SCENARIO: "upstream_429",
        BURST_DURATION: "1s",
      }).setup(),
    /Duration overrides require SMOKE=1/,
  );
  assert.match(script, /SCENARIO=\$\{SCENARIO\} requires an idle mock before reset/);
  assert.match(mockServer, /async def control[\s\S]*async with _lock:/);
});

test("arrival-rate scenarios prove planned delivery without dropped iterations", () => {
  const failure = loadContract({ SCENARIO: "upstream_failure" });
  assert.equal(failure.plannedIterationsFor("upstream_failure"), 450);
  assert.deepEqual(failure.options.thresholds.dropped_iterations, ["count==0"]);
  assert.deepEqual(failure.options.thresholds.guard_planned_iterations_delivered, [
    "value==1",
  ]);

  const rateLimit = loadContract({ SCENARIO: "upstream_429" });
  assert.equal(rateLimit.plannedIterationsFor("upstream_429"), 1110);
  assert.deepEqual(rateLimit.options.thresholds.dropped_iterations, ["count==0"]);
  assert.deepEqual(rateLimit.options.thresholds.guard_planned_iterations_delivered, [
    "value==1",
  ]);

  const smoke = loadContract({
    SCENARIO: "upstream_429",
    SMOKE: "1",
    RATE: "4",
    SUSTAIN_RATE: "2",
    BURST_DURATION: "2s",
    RAMP_DOWN_DURATION: "1s",
    CONVERGENCE_DURATION: "3s",
  });
  assert.equal(smoke.plannedIterationsFor("upstream_429"), 17);
  assert.equal(smoke.durationSeconds("250ms"), 0.25);
});

test("scenario baselines reject stale breaker, AIMD, and disconnect state", () => {
  const failure = loadContract({ SCENARIO: "upstream_failure" });
  assert.doesNotThrow(() =>
    failure.validateScenarioBaseline("upstream_failure", {
      cb_state: { endpoint: 0 },
    }),
  );
  assert.throws(
    () =>
      failure.validateScenarioBaseline("upstream_failure", {
        cb_state: { endpoint: 2 },
      }),
    /requires all circuit breakers closed/,
  );

  const rateLimit = loadContract({ SCENARIO: "upstream_429" });
  assert.equal(
    rateLimit.onlineEffectiveMax({ guard_llm_effective_max: { online: 90 } }, true),
    90,
  );
  assert.doesNotThrow(() =>
    rateLimit.validateScenarioBaseline("upstream_429", {
      guard_llm_effective_max: { online: 90 },
    }),
  );
  assert.throws(
    () =>
      rateLimit.validateScenarioBaseline("upstream_429", {
        guard_llm_effective_max: { online: 72 },
      }),
    /requires guard_llm_effective_max\.online=90/,
  );

  const disconnect = loadContract({ SCENARIO: "disconnect" });
  assert.doesNotThrow(() =>
    disconnect.validateScenarioBaseline("disconnect", {
      guard_llm_inflight: { online: 0 },
    }),
  );
  assert.throws(
    () =>
      disconnect.validateScenarioBaseline("disconnect", {
        guard_llm_inflight: { online: 1 },
      }),
    /requires zero online inflight/,
  );
});

test("queue, degrade, and breaker retry frames enforce their contracts", () => {
  const contract = loadContract();
  assert.equal(
    contract.validQueuePayload({
      position: 1,
      queue_size: 2,
      estimated_wait_ms: 1000,
    }),
    true,
  );
  assert.equal(
    contract.validDegradePayload({
      level: 1,
      reason: "high_utilization",
      effects: ["thinking_disabled", "max_tokens_reduced"],
    }),
    true,
  );
  assert.equal(
    contract.validDegradePayload({
      level: 0,
      reason: "high_utilization",
      effects: ["thinking_disabled", "max_tokens_reduced"],
    }),
    false,
  );
  assert.equal(
    contract.validDegradePayload({
      level: 2,
      reason: " ",
      effects: ["thinking_disabled", "max_tokens_reduced"],
    }),
    false,
  );
  assert.equal(
    contract.validDegradePayload({
      level: 2,
      reason: "critical_utilization",
      effects: ["thinking_disabled"],
    }),
    false,
  );

  const parsed = contract.parseFrames(
    'event: degrade\ndata: {"level":1,"reason":"high","effects":["thinking_disabled","max_tokens_reduced"]}\n\n' +
      'event: degrade\ndata: {"level":3,"reason":"bad","effects":[]}\n\n',
  );
  assert.equal(parsed.degradeCount, 2);
  assert.equal(parsed.degradeProtocolViolationCount, 1);

  assert.equal(
    contract.hasPositiveRetryAfter(
      'event: error\ndata: {"code":50300,"message":"open","retry_after_ms":1000}\n\n',
      50300,
    ),
    true,
  );
  assert.equal(
    contract.hasPositiveRetryAfter(
      'event: error\ndata: {"code":50300,"message":"open","retry_after_ms":0}\n\n',
      50300,
    ),
    false,
  );

  const degradeThresholds = loadContract({ SCENARIO: "degrade" }).options.thresholds;
  assert.deepEqual(degradeThresholds.guard_degrade_protocol_violations, ["count==0"]);
  assert.deepEqual(degradeThresholds.guard_degrade_full_tokens_observed, ["value==1"]);
  assert.deepEqual(degradeThresholds.guard_degrade_enter_timing_valid, ["value==1"]);
  assert.deepEqual(degradeThresholds.guard_degrade_exit_timing_valid, ["value==1"]);
  assert.deepEqual(degradeThresholds.guard_degrade_transition_window_complete, [
    "value==1",
  ]);
  assert.deepEqual(degradeThresholds.guard_degrade_high_window_proven, ["value==1"]);
  assert.deepEqual(degradeThresholds.guard_degrade_low_window_proven, ["value==1"]);
  const breakerThresholds = loadContract({
    SCENARIO: "upstream_failure",
  }).options.thresholds;
  assert.deepEqual(breakerThresholds.guard_breaker_fast_fail_samples, ["count>0"]);
  assert.deepEqual(breakerThresholds.guard_breaker_retry_after_violations, ["count==0"]);
  assert.deepEqual(breakerThresholds.guard_injected_500_verified, ["value==1"]);
  assert.equal(
    contract.isFastFailCircuitSnapshot({
      cb_state: { endpoint: 2 },
      cb_open_seconds: 10,
    }),
    true,
  );
  assert.equal(
    contract.isFastFailCircuitSnapshot({
      cb_state: { endpoint: 2 },
      cb_open_seconds: 29,
    }),
    false,
  );
  assert.equal(
    contract.isFastFailCircuitSnapshot({
      cb_state: { endpoint: 1 },
      cb_open_seconds: 10,
    }),
    false,
  );
  assert.match(script, /if \(breakerShouldFastFail\)/);
});

test("degrade evidence covers complete utilization windows and transition-bound tokens", () => {
  const contract = loadContract({ SCENARIO: "degrade" });
  const continuouslyHigh = [
    [0, 73],
    [30_000, 90],
    [60_000, 73],
  ];
  assert.equal(
    contract.utilizationWindowProven(
      continuouslyHigh,
      0,
      60_000,
      (value) => value / 90 > 0.8,
    ),
    true,
  );
  assert.equal(
    contract.utilizationWindowProven(
      [...continuouslyHigh, [45_000, 72]],
      0,
      60_000,
      (value) => value / 90 > 0.8,
    ),
    false,
  );
  assert.equal(
    contract.utilizationWindowProven(
      [
        [0, 62],
        [120_000, 0],
      ],
      0,
      120_000,
      (value) => value / 90 < 0.7,
    ),
    true,
  );
  assert.match(script, /Number\(item\.at_ms\) < entered\.atMs/);
  assert.match(script, /Number\(item\.at_ms\) >= entered\.atMs/);
  assert.match(script, /entered\.atMs - 60000/);
  assert.match(script, /exited\.atMs - 120000/);
});

test("formal SSE scenarios enforce server-observed first-outcome coverage and deadline", () => {
  for (const scenario of [
    "p0_steady",
    "spike",
    "upstream_failure",
    "upstream_429",
    "single_user",
    "degrade",
  ]) {
    const thresholds = loadContract({ SCENARIO: scenario }).options.thresholds;
    assert.deepEqual(thresholds.guard_first_outcome_samples, ["value>0"]);
    assert.deepEqual(thresholds.guard_first_outcome_max_ms, ["value<=60000"]);
    assert.deepEqual(thresholds.guard_first_outcome_coverage, ["value==1"]);
    assert.deepEqual(thresholds.guard_first_outcome_window_complete, ["value==1"]);
    assert.deepEqual(thresholds.guard_client_ledger_reconciled, ["value==1"]);
    assert.deepEqual(thresholds.dropped_iterations, ["count==0"]);
    assert.deepEqual(thresholds.http_req_failed, ["rate==0"]);
  }

  const disconnect = loadContract({ SCENARIO: "disconnect" }).options.thresholds;
  assert.equal(disconnect.guard_first_outcome_max_ms, undefined);
  const p0 = loadContract({ SCENARIO: "p0_steady" }).options.thresholds;
  assert.deepEqual(p0.guard_first_outcome_p95_ms, ["value<3000"]);
  assert.match(script, /guard_first_outcome_after=/);
  assert.match(script, /guard_queue_wait_after=/);
  assert.match(script, /guard_degrade_after=/);
  assert.match(script, /Number\(item\.max_tokens\) === half/);
  assert.match(script, /first_outcome: firstOutcome/);
  assert.match(script, /clientCompleted === clientStarted/);
  assert.match(script, /serverSamples === clientFirstOutcomes/);
  assert.match(mockServer, /@app\.post\("\/client-start"\)/);
  assert.match(mockServer, /@app\.post\("\/client-disconnect"\)/);
  assert.match(mockServer, /client_completed/);
  assert.match(mockServer, /rejected_requests/);
});

test("spike and disconnect thresholds reject false-green overload and leak results", () => {
  const spike = loadContract({ SCENARIO: "spike" }).options.thresholds;
  assert.deepEqual(spike.guard_queued_requests, ["value>=30"]);
  assert.deepEqual(spike.guard_queue_full_requests, ["value==0"]);
  assert.deepEqual(spike.guard_observed_queue_p95_ms, ["value<60000"]);
  assert.deepEqual(spike.guard_p1_queue_p95_ms, ["value<30000"]);
  assert.deepEqual(spike.guard_queue_wait_reconciled, ["value==1"]);
  assert.deepEqual(spike.guard_p1_rejection_rate, ["value<0.01"]);
  assert.deepEqual(spike.guard_unexpected_server_error, ["rate==0"]);

  const disconnect = loadContract({ SCENARIO: "disconnect" }).options.thresholds;
  assert.deepEqual(disconnect.dropped_iterations, ["count==0"]);
  assert.deepEqual(disconnect.guard_stream_cancelled_delta, ["value==30"]);
  assert.deepEqual(disconnect.guard_disconnect_client_started, ["value==30"]);
  assert.deepEqual(disconnect.guard_disconnect_client_completed, ["value==0"]);
  assert.deepEqual(disconnect.guard_disconnect_events, ["value==30"]);
  assert.deepEqual(disconnect.guard_disconnect_final_app_inflight, ["value==0"]);
  assert.equal(disconnect.guard_disconnect_final_mock_inflight, undefined);
  assert.equal(disconnect.http_req_failed, undefined);
  assert.match(script, /if \(currentAppInflight === 0\)/);
  assert.match(script, /recoveryDeadlineMs = lastDisconnectMs \+ 5000/);
});

test("P1 and P2 rejection commitments use every started iteration", () => {
  const contract = loadContract();
  assert.equal(contract.rejectionRate({ client_started: 100, rejected_requests: 0 }), 0);
  assert.equal(
    contract.rejectionRate({ client_started: 100, rejected_requests: 1 }),
    0.01,
  );
  assert.throws(
    () => contract.rejectionRate({ client_started: 1, rejected_requests: 2 }),
    /exceeds client start count/,
  );
  const degrade = loadContract({ SCENARIO: "degrade" }).options.thresholds;
  assert.deepEqual(degrade.guard_p2_rejection_rate, ["value<0.05"]);
  assert.match(script, /p1RejectionRate\.add\(rejectionRate\(finalMockStats\)\)/);
  assert.match(script, /p2RejectionRate\.add\(rejectionRate\(finalMockStats\)\)/);
});

test("429 acceptance requires measured AIMD capacity reduction", () => {
  const thresholds = loadContract({ SCENARIO: "upstream_429" }).options.thresholds;
  assert.deepEqual(thresholds.guard_aimd_effective_max_reduced, ["value==1"]);
  assert.match(script, /finalEffectiveMax < baselineEffectiveMax/);
  assert.match(script, /guard_llm_effective_max/);
});

test("tuning variables are only read through the smoke-aware helper", () => {
  for (const name of [
    "VUS",
    "RATE",
    "SUSTAIN_RATE",
    "PACING_SECONDS",
    "EXPECTED_MAX_TOKENS",
    "MOCK_DELAY_MS",
    "UPSTREAM_MAX_CONCURRENCY",
  ]) {
    assert.doesNotMatch(script, new RegExp(`__ENV\\.${name}(?![A-Za-z0-9_])`));
  }
});
