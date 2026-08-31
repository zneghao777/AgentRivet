/*
 * Automated acceptance for CONCURRENCY_GUARD_SPEC section 14.
 *
 * This suite deliberately fails setup unless callers provide enough valid API
 * keys and confirm that L1 limits were relaxed for the capacity test. See the
 * adjacent README for the isolated mock-backend launch command.
 */

import http from "k6/http";
import { check, sleep } from "k6";
import exec from "k6/execution";
import { Counter, Gauge, Rate, Trend } from "k6/metrics";

const BASE_URL = __ENV.BASE_URL || "http://127.0.0.1:8000/api/v1";
const MOCK_URL = __ENV.MOCK_URL || "http://127.0.0.1:18080";
const SCENARIO = __ENV.SCENARIO || "p0_steady";
const SMOKE = __ENV.SMOKE === "1";
const MESSAGE = __ENV.MESSAGE || "请简要说明并发防护的作用";
const ACCEPTANCE_TUNING_OVERRIDES = [
  "VUS",
  "RATE",
  "SUSTAIN_RATE",
  "PACING_SECONDS",
  "EXPECTED_MAX_TOKENS",
  "MOCK_DELAY_MS",
  "UPSTREAM_MAX_CONCURRENCY",
];
const API_KEYS = (__ENV.API_KEYS || __ENV.API_KEY || "sk-dev-key-1")
  .split(",")
  .map((item) => item.trim())
  .filter(Boolean);

const anyGuardError = new Rate("guard_any_error");
const explicitOutcome = new Rate("guard_explicit_outcome");
const responseDuration = new Trend("guard_response_duration_ms", true);
const ttft = new Trend("guard_ttft_ms", true);
const ttftSamples = new Counter("guard_ttft_samples");
const ttftCoverage = new Gauge("guard_ttft_coverage");
const p0SuccessfulRequests = new Counter("guard_p0_successful_requests");
const p0RequestCallReconciled = new Gauge("guard_p0_request_call_reconciled");
const collectorDrained = new Gauge("guard_collector_drained");
const spansDroppedDelta = new Gauge("guard_spans_dropped_delta");
const firstOutcomeSamples = new Gauge("guard_first_outcome_samples");
const firstOutcomeMax = new Gauge("guard_first_outcome_max_ms");
const firstOutcomeP95 = new Gauge("guard_first_outcome_p95_ms");
const firstOutcomeCoverage = new Gauge("guard_first_outcome_coverage");
const firstOutcomeWindowComplete = new Gauge(
  "guard_first_outcome_window_complete",
);
const clientLedgerReconciled = new Gauge("guard_client_ledger_reconciled");
const plannedIterationsDelivered = new Gauge(
  "guard_planned_iterations_delivered",
);

const queueEvents = new Counter("guard_queue_events");
const queuePositionViolations = new Counter("guard_queue_position_violations");
const queueProtocolViolations = new Counter("guard_queue_protocol_violations");
const queueWaitSamples = new Gauge("guard_queue_wait_samples");
const observedQueueP95 = new Gauge("guard_observed_queue_p95_ms");
const p1QueueP95 = new Gauge("guard_p1_queue_p95_ms");
const queueWaitWindowComplete = new Gauge(
  "guard_queue_wait_window_complete",
);
const queueWaitReconciled = new Gauge("guard_queue_wait_reconciled");
const queuedRequests = new Gauge("guard_queued_requests");
const queueFullRequests = new Gauge("guard_queue_full_requests");
const p1RejectionRate = new Gauge("guard_p1_rejection_rate");
const unexpectedServerError = new Rate("guard_unexpected_server_error");

const breaker503 = new Counter("guard_breaker_503_total");
const breakerFastFailSamples = new Counter("guard_breaker_fast_fail_samples");
const breakerFastFailViolations = new Counter("guard_breaker_fast_fail_violations");
const breakerRetryAfterViolations = new Counter("guard_breaker_retry_after_violations");
const breakerOpenAfter = new Trend("guard_breaker_open_after_ms", true);
const injected500Verified = new Gauge("guard_injected_500_verified");

const initial429 = new Counter("guard_initial_429_total");
const aimdConverged = new Gauge("guard_aimd_converged");
const aimdEffectiveMaxReduced = new Gauge("guard_aimd_effective_max_reduced");

const singleUserAccepted = new Counter("guard_single_user_accepted");
const singleUserRejected = new Counter("guard_single_user_rejected");

const degradeEvents = new Counter("guard_degrade_events");
const degradeProtocolViolations = new Counter("guard_degrade_protocol_violations");
const degradeFullTokens = new Gauge("guard_degrade_full_tokens_observed");
const degradeHalfTokens = new Gauge("guard_degrade_half_tokens_observed");
const degradeExited = new Gauge("guard_degrade_exited");
const degradeEnterTimingValid = new Gauge("guard_degrade_enter_timing_valid");
const degradeExitTimingValid = new Gauge("guard_degrade_exit_timing_valid");
const degradeTransitionWindowComplete = new Gauge(
  "guard_degrade_transition_window_complete",
);
const degradeHighWindowProven = new Gauge("guard_degrade_high_window_proven");
const degradeLowWindowProven = new Gauge("guard_degrade_low_window_proven");
const p2RejectionRate = new Gauge("guard_p2_rejection_rate");

const cancelledDelta = new Gauge("guard_stream_cancelled_delta");
const disconnectClientStarted = new Gauge("guard_disconnect_client_started");
const disconnectClientCompleted = new Gauge("guard_disconnect_client_completed");
const disconnectEvents = new Gauge("guard_disconnect_events");
const disconnectMockCalls = new Gauge("guard_disconnect_mock_calls");
const disconnectPeakInflight = new Gauge("guard_disconnect_peak_inflight");
const disconnectAdmittedDelta = new Gauge("guard_disconnect_llm_admitted_delta");
const disconnectFinalAppInflight = new Gauge("guard_disconnect_final_app_inflight");
const disconnectFinalMockInflight = new Gauge("guard_disconnect_final_mock_inflight");
const inflightRecovered = new Gauge("guard_inflight_recovered");
const inflightRecoveryMs = new Gauge("guard_inflight_recovery_ms");

const scenarioOptions = {
  p0_steady: {
    executor: "constant-vus",
    vus: scenarioNumber("VUS", 30),
    duration: scenarioDuration("DURATION", "10m"),
  },
  spike: {
    executor: "ramping-vus",
    startVUs: 30,
    stages: [
      { duration: scenarioDuration("WARMUP", "30s"), target: 30 },
      {
        duration: scenarioDuration("RAMP", "5s"),
        target: scenarioNumber("VUS", 150),
      },
      {
        duration: scenarioDuration("HOLD", "2m"),
        target: scenarioNumber("VUS", 150),
      },
      { duration: "15s", target: 0 },
    ],
  },
  upstream_failure: {
    executor: "constant-arrival-rate",
    rate: scenarioNumber("RATE", 10),
    timeUnit: "1s",
    duration: scenarioDuration("DURATION", "45s"),
    preAllocatedVUs: 20,
    maxVUs: 80,
  },
  upstream_429: {
    executor: "ramping-arrival-rate",
    startRate: scenarioNumber("RATE", 40),
    timeUnit: "1s",
    stages: [
      { duration: scenarioDuration("BURST_DURATION", "5s"), target: scenarioNumber("RATE", 40) },
      {
        duration: scenarioDuration("RAMP_DOWN_DURATION", "1s"),
        target: scenarioNumber("SUSTAIN_RATE", 20),
      },
      {
        duration: scenarioDuration("CONVERGENCE_DURATION", "44s"),
        target: scenarioNumber("SUSTAIN_RATE", 20),
      },
    ],
    preAllocatedVUs: 300,
    maxVUs: 300,
  },
  single_user: {
    executor: "per-vu-iterations",
    vus: 5,
    iterations: 1,
    maxDuration: "30s",
  },
  degrade: {
    executor: "ramping-vus",
    startVUs: 0,
    stages: [
      { duration: "10s", target: scenarioNumber("VUS", 150) },
      {
        duration: scenarioDuration("HIGH_HOLD", "90s"),
        target: scenarioNumber("VUS", 150),
      },
      { duration: "10s", target: 0 },
      { duration: scenarioDuration("LOW_HOLD", "130s"), target: 0 },
    ],
  },
  disconnect: {
    executor: "per-vu-iterations",
    vus: scenarioNumber("VUS", 30),
    iterations: 1,
    maxDuration: "20s",
  },
};

export const options = {
  scenarios: { [SCENARIO]: scenarioOptions[SCENARIO] || scenarioOptions.p0_steady },
  thresholds: thresholdsFor(SCENARIO),
  discardResponseBodies: false,
  tags: { guard_test_mode: SMOKE ? "smoke" : "acceptance" },
};

function scenarioDuration(envName, acceptanceDuration) {
  if (SMOKE && __ENV[envName]) return __ENV[envName];
  return acceptanceDuration;
}

function scenarioNumber(envName, acceptanceValue) {
  if (SMOKE && __ENV[envName] !== undefined && __ENV[envName] !== "") {
    return Number(__ENV[envName]);
  }
  return acceptanceValue;
}

function durationSeconds(value) {
  const match = /^([0-9]+(?:\.[0-9]+)?)(ms|s|m|h)$/.exec(String(value).trim());
  if (!match) throw new Error(`unsupported k6 duration for delivery proof: ${value}`);
  const multipliers = { ms: 0.001, s: 1, m: 60, h: 3600 };
  return Number(match[1]) * multipliers[match[2]];
}

function plannedIterationsFor(name) {
  if (name !== "upstream_failure" && name !== "upstream_429") return null;
  let planned;
  if (name === "upstream_429") {
    const burstRate = scenarioNumber("RATE", 40);
    const sustainRate = scenarioNumber("SUSTAIN_RATE", 20);
    const burstSeconds = durationSeconds(
      scenarioDuration("BURST_DURATION", "5s"),
    );
    const rampSeconds = durationSeconds(
      scenarioDuration("RAMP_DOWN_DURATION", "1s"),
    );
    const convergenceSeconds = durationSeconds(
      scenarioDuration("CONVERGENCE_DURATION", "44s"),
    );
    planned =
      burstRate * burstSeconds +
      ((burstRate + sustainRate) / 2) * rampSeconds +
      sustainRate * convergenceSeconds;
  } else {
    planned =
      scenarioNumber("RATE", 10) *
      durationSeconds(scenarioDuration("DURATION", "45s"));
  }
  if (!Number.isSafeInteger(planned) || planned < 1) {
    throw new Error(`planned iteration count must be a positive integer, received ${planned}`);
  }
  return planned;
}

function durationOverridesFor(name) {
  if (name === "p0_steady" || name === "upstream_failure") {
    return ["DURATION"];
  }
  if (name === "upstream_429") {
    return ["DURATION", "BURST_DURATION", "RAMP_DOWN_DURATION", "CONVERGENCE_DURATION"];
  }
  if (name === "spike") return ["WARMUP", "RAMP", "HOLD"];
  if (name === "degrade") return ["HIGH_HOLD", "LOW_HOLD"];
  return [];
}

function thresholdsFor(name) {
  const common = {
    checks: ["rate==1"],
    dropped_iterations: ["count==0"],
    http_req_failed: ["rate==0"],
    guard_explicit_outcome: ["rate==1"],
    guard_response_duration_ms: ["max<130000"],
    guard_first_outcome_samples: ["value>0"],
    guard_first_outcome_max_ms: ["value<=60000"],
    guard_first_outcome_coverage: ["value==1"],
    guard_first_outcome_window_complete: ["value==1"],
    guard_client_ledger_reconciled: ["value==1"],
  };
  if (name === "p0_steady") {
    return {
      ...common,
      guard_any_error: ["rate==0"],
      guard_first_outcome_p95_ms: ["value<3000"],
      guard_ttft_samples: ["count>0"],
      guard_ttft_ms: ["p(95)<3000"],
      guard_ttft_coverage: ["value==1"],
      guard_p0_successful_requests: ["count>0"],
      guard_p0_request_call_reconciled: ["value==1"],
      guard_collector_drained: ["value==1"],
      guard_spans_dropped_delta: ["value==0"],
      guard_queue_events: ["count==0"],
      guard_queue_wait_samples: ["value==0"],
    };
  }
  if (name === "spike") {
    return {
      ...common,
      guard_queue_events: ["count>0"],
      guard_queue_position_violations: ["count==0"],
      guard_queue_protocol_violations: ["count==0"],
      guard_queue_wait_samples: ["value>0"],
      guard_observed_queue_p95_ms: ["value<60000"],
      guard_p1_queue_p95_ms: ["value<30000"],
      guard_queue_wait_window_complete: ["value==1"],
      guard_queue_wait_reconciled: ["value==1"],
      guard_queued_requests: [SMOKE ? "value>0" : "value>=30"],
      guard_queue_full_requests: ["value==0"],
      guard_p1_rejection_rate: ["value<0.01"],
      guard_unexpected_server_error: ["rate==0"],
    };
  }
  if (name === "upstream_failure") {
    return {
      ...common,
      guard_breaker_503_total: ["count>0"],
      guard_breaker_fast_fail_samples: ["count>0"],
      guard_breaker_fast_fail_violations: ["count==0"],
      guard_breaker_retry_after_violations: ["count==0"],
      guard_breaker_open_after_ms: ["min<30000"],
      guard_injected_500_verified: ["value==1"],
      guard_planned_iterations_delivered: ["value==1"],
    };
  }
  if (name === "upstream_429") {
    return {
      ...common,
      guard_initial_429_total: ["count>0"],
      guard_aimd_converged: ["value==1"],
      guard_aimd_effective_max_reduced: ["value==1"],
      guard_planned_iterations_delivered: ["value==1"],
    };
  }
  if (name === "single_user") {
    return {
      ...common,
      guard_single_user_accepted: ["count==1"],
      guard_single_user_rejected: ["count==4"],
    };
  }
  if (name === "degrade") {
    return {
      ...common,
      guard_degrade_events: ["count>0"],
      guard_degrade_protocol_violations: ["count==0"],
      guard_degrade_full_tokens_observed: ["value==1"],
      guard_degrade_half_tokens_observed: ["value==1"],
      guard_degrade_exited: ["value==1"],
      guard_p2_rejection_rate: ["value<0.05"],
      ...(
        SMOKE
          ? {}
          : {
              guard_degrade_enter_timing_valid: ["value==1"],
              guard_degrade_exit_timing_valid: ["value==1"],
              guard_degrade_transition_window_complete: ["value==1"],
              guard_degrade_high_window_proven: ["value==1"],
              guard_degrade_low_window_proven: ["value==1"],
            }
      ),
    };
  }
  if (name === "disconnect") {
    const target = scenarioNumber("VUS", 30);
    return {
      checks: ["rate==1"],
      dropped_iterations: ["count==0"],
      guard_stream_cancelled_delta: [`value==${target}`],
      guard_disconnect_client_started: [`value==${target}`],
      guard_disconnect_client_completed: ["value==0"],
      guard_disconnect_events: [`value==${target}`],
      guard_disconnect_mock_calls: [`value==${target}`],
      guard_disconnect_peak_inflight: [`value>=${target}`],
      guard_disconnect_llm_admitted_delta: [`value==${target}`],
      guard_disconnect_final_app_inflight: ["value==0"],
      guard_inflight_recovered: ["value==1"],
      guard_inflight_recovery_ms: ["value<=5000"],
    };
  }
  return common;
}

export function setup() {
  if (!Object.prototype.hasOwnProperty.call(scenarioOptions, SCENARIO)) {
    throw new Error(
      `Unknown SCENARIO=${SCENARIO}; expected one of ${Object.keys(scenarioOptions).join(", ")}`,
    );
  }
  const forbiddenDurationOverrides = durationOverridesFor(SCENARIO).filter(
    (name) => !SMOKE && __ENV[name] !== undefined && __ENV[name] !== "",
  );
  if (forbiddenDurationOverrides.length > 0) {
    throw new Error(
      `Duration overrides require SMOKE=1: ${forbiddenDurationOverrides.join(", ")}. ` +
        "Formal acceptance always uses the specification durations.",
    );
  }
  const forbiddenTuningOverrides = ACCEPTANCE_TUNING_OVERRIDES.filter(
    (name) => !SMOKE && __ENV[name] !== undefined && __ENV[name] !== "",
  );
  if (forbiddenTuningOverrides.length > 0) {
    throw new Error(
      `Load or expectation overrides require SMOKE=1: ${forbiddenTuningOverrides.join(", ")}. ` +
        "Formal acceptance always uses the specification intensity and expected parameters.",
    );
  }
  if (__ENV.RATE_LIMITS_RELAXED !== "1") {
    throw new Error(
      "Set RATE_LIMITS_RELAXED=1 after starting the isolated backend with the " +
        "load-test L1 overrides documented in tests/load/README.md",
    );
  }
  if (SCENARIO === "p0_steady" && __ENV.BACKGROUND_LLM_DISABLED !== "1") {
    throw new Error(
      "Set BACKGROUND_LLM_DISABLED=1 after starting the isolated backend with " +
        "MEMORY_ENABLED=false so P0 measures only the foreground chat LLM chain",
    );
  }
  const requiredKeys = SCENARIO === "single_user" ? 1 : requiredKeyCount(SCENARIO);
  if (API_KEYS.length < requiredKeys) {
    throw new Error(
      `SCENARIO=${SCENARIO} requires at least ${requiredKeys} configured API keys; ` +
        `received ${API_KEYS.length}`,
    );
  }

  const preResetMock = mockStats(true);
  if (Number(preResetMock.inflight) !== 0) {
    throw new Error(
      `SCENARIO=${SCENARIO} requires an idle mock before reset; ` +
        `inflight=${preResetMock.inflight}`,
    );
  }

  const response = http.post(
    `${MOCK_URL}/control`,
    JSON.stringify(mockControlFor(SCENARIO)),
    { headers: { "Content-Type": "application/json" }, timeout: "5s" },
  );
  if (response.status !== 200) {
    throw new Error(`mock LLM control failed: HTTP ${response.status} ${response.body}`);
  }

  const baseline = guardSnapshot(API_KEYS[0], true);
  validateScenarioBaseline(SCENARIO, baseline);
  const collectorBaseline =
    SCENARIO === "p0_steady" ? collectorSnapshot(API_KEYS[0], true) : null;
  const startedMs = Date.now();
  return {
    startedMs,
    startedIso: new Date(startedMs).toISOString(),
    baseline,
    collectorBaseline,
    expectedMaxTokens: scenarioNumber("EXPECTED_MAX_TOKENS", 4096),
    plannedIterations: plannedIterationsFor(SCENARIO),
  };
}

export default function (data) {
  const startRecorded = recordClientStart();
  check(startRecorded, {
    "client iteration start recorded by mock": (recorded) => recorded === true,
  });
  if (!startRecorded) {
    explicitOutcome.add(false);
    return;
  }

  const apiKey = keyForIteration();
  const session = createSession(apiKey);
  if (!session) {
    explicitOutcome.add(false);
    return;
  }
  const breakerShouldFastFail =
    SCENARIO === "upstream_failure"
      ? isFastFailCircuitSnapshot(guardSnapshot(apiKey, true))
      : false;

  const timeout = SCENARIO === "disconnect" ? "5s" : __ENV.REQUEST_TIMEOUT || "130s";
  const response = http.post(
    `${BASE_URL}/sessions/${session}/messages/stream`,
    JSON.stringify({
      message: `${MESSAGE} [vu=${exec.vu.idInTest},iter=${exec.scenario.iterationInTest}]`,
    }),
    requestParams(apiKey, timeout),
  );

  if (SCENARIO === "disconnect") {
    const aborted = response.error_code === 1050 || response.status === 0;
    const disconnectRecorded = aborted && recordClientDisconnect();
    check(response, {
      "client aborts after 5s": () => aborted,
      "client abort timestamp recorded by mock": () => disconnectRecorded,
    });
    return;
  }

  const body = String(response.body || "");
  const frames = parseFrames(body);
  const codes = errorCodes(body);
  const outcome = body.includes("event: run_end") || body.includes("event: error");
  const firstOutcome =
    body.includes("event: message_delta") || body.includes("event: error");
  explicitOutcome.add(outcome);
  anyGuardError.add(body.includes("event: error"));
  responseDuration.add(response.timings.duration);
  recordQueue(frames);

  let p0Success = false;
  if (SCENARIO === "p0_steady") {
    p0Success =
      response.status === 200 &&
      runEndStatus(body) === "success" &&
      !body.includes("event: error");
    p0SuccessfulRequests.add(p0Success ? 1 : 0);
  }
  const outcomeRecorded = recordClientOutcome(
    p0Success,
    firstOutcome,
    frames.queueEventCount > 0,
    codes.includes(42900),
    response.status !== 200 || body.includes("event: error"),
  );

  if (SCENARIO === "spike") {
    const serverError = allEventCodes(body).some((code) => code >= 50000);
    unexpectedServerError.add(response.status >= 500 || serverError);
  } else if (SCENARIO === "upstream_failure") {
    const breakerIsOpen = hasOpenCircuit(guardSnapshot(apiKey, true));
    breaker503.add(breakerIsOpen ? 1 : 0);
    if (breakerIsOpen) {
      breakerOpenAfter.add(Date.now() - data.startedMs);
    }
    if (breakerShouldFastFail) {
      const validRetryAfter = hasPositiveRetryAfter(body, 50300);
      breakerFastFailSamples.add(1);
      breakerFastFailViolations.add(response.timings.duration > 1000 ? 1 : 0);
      breakerRetryAfterViolations.add(validRetryAfter ? 0 : 1);
      check(response, {
        "open circuit returns 50300": () => codes.includes(50300),
        "open circuit includes positive retry_after_ms": () => validRetryAfter,
      });
    }
  } else if (SCENARIO === "single_user") {
    singleUserAccepted.add(runEndStatus(body) === "success" ? 1 : 0);
    singleUserRejected.add(codes.includes(42901) ? 1 : 0);
  } else if (SCENARIO === "degrade") {
    degradeEvents.add(frames.degradeCount);
    degradeProtocolViolations.add(frames.degradeProtocolViolationCount);
  }

  const responseChecks = {
    "SSE transport accepted": (result) => result.status === 200,
    "stream has explicit outcome": () => outcome,
    "stream has message_delta or error": () => firstOutcome,
    "stream outcome recorded by mock": () => outcomeRecorded,
  };
  if (SCENARIO === "p0_steady") {
    responseChecks["P0 stream completes successfully"] = () => p0Success;
  }
  check(response, responseChecks);
  sleep(scenarioNumber("PACING_SECONDS", 1));
}

export function teardown(data) {
  if (SCENARIO !== "disconnect") {
    sleep(Number(__ENV.SETTLE_SECONDS || 3));
  }
  let finalGuard = guardSnapshot(API_KEYS[0], true);
  let finalMockStats = null;

  if (SCENARIO !== "disconnect") {
    const baselineSamples = requireNonNegativeInteger(
      (data.baseline || {}).guard_first_outcome_samples,
      "baseline first-outcome samples",
    );
    const outcomeWindow = guardSnapshot(API_KEYS[0], true, baselineSamples);
    finalMockStats = mockStats(true);
    const clientStarted = requireNonNegativeInteger(
      finalMockStats.client_started,
      "mock client start count",
    );
    const clientCompleted = requireNonNegativeInteger(
      finalMockStats.client_completed,
      "mock client completion count",
    );
    const legacyClientOutcomes = requireNonNegativeInteger(
      finalMockStats.client_outcomes,
      "mock client outcome count",
    );
    const clientFirstOutcomes = requireNonNegativeInteger(
      finalMockStats.first_outcome_requests,
      "mock first-outcome request count",
    );
    const serverSamples = requireNonNegativeInteger(
      outcomeWindow.guard_first_outcome_samples,
      "server first-outcome samples",
    );
    const windowComplete = outcomeWindow.guard_first_outcome_window_complete === true;
    firstOutcomeSamples.add(serverSamples);
    firstOutcomeMax.add(Number(outcomeWindow.guard_first_outcome_max_ms || 0));
    firstOutcomeP95.add(Number(outcomeWindow.guard_first_outcome_p95_ms || 0));
    const ledgerReconciled =
      clientStarted > 0 &&
      clientCompleted === clientStarted &&
      legacyClientOutcomes === clientCompleted &&
      clientFirstOutcomes === clientCompleted &&
      serverSamples === clientFirstOutcomes;
    firstOutcomeCoverage.add(ledgerReconciled ? 1 : 0);
    clientLedgerReconciled.add(ledgerReconciled ? 1 : 0);
    firstOutcomeWindowComplete.add(windowComplete ? 1 : 0);
    if (data.plannedIterations !== null) {
      plannedIterationsDelivered.add(
        clientStarted === data.plannedIterations &&
          clientCompleted === data.plannedIterations
          ? 1
          : 0,
      );
    }
  }

  if (SCENARIO === "p0_steady") {
    queueWaitSamples.add(queueWaitSampleDelta(data, finalGuard));
    const stats = finalMockStats;
    const mockCalls = requireNonNegativeInteger(stats.total, "mock total calls");
    const successfulRequests = requireNonNegativeInteger(
      stats.successful_requests,
      "mock successful request count",
    );
    const clientCompleted = requireNonNegativeInteger(
      stats.client_completed,
      "mock client completion count",
    );
    waitForCollectorDrain(data.startedIso, API_KEYS[0], mockCalls);
    collectorDrained.add(1);
    const result = loadTtftValues(data.startedIso, API_KEYS[0]);
    const finalCollector = collectorSnapshot(API_KEYS[0], true);
    const dropped =
      requireNonNegativeInteger(
        finalCollector.spans_dropped_total,
        "final spans_dropped_total",
      ) -
      requireNonNegativeInteger(
        (data.collectorBaseline || {}).spans_dropped_total,
        "baseline spans_dropped_total",
      );
    const reconciled =
      successfulRequests > 0 &&
      clientCompleted === successfulRequests &&
      successfulRequests === mockCalls &&
      result.total === mockCalls;
    ttftSamples.add(result.values.length);
    p0RequestCallReconciled.add(reconciled ? 1 : 0);
    ttftCoverage.add(
      reconciled && result.values.length === result.total ? 1 : 0,
    );
    spansDroppedDelta.add(dropped);
    for (const value of result.values) ttft.add(value);
  } else if (SCENARIO === "spike") {
    const baselineQueueWaits = requireNonNegativeInteger(
      (data.baseline || {}).guard_queue_wait_samples,
      "baseline guard_queue_wait_samples",
    );
    const queueWindow = guardSnapshot(
      API_KEYS[0],
      true,
      null,
      baselineQueueWaits,
    );
    const scenarioQueueWaitSamples = requireNonNegativeInteger(
      queueWindow.guard_queue_wait_samples,
      "scenario queue wait samples",
    );
    queueWaitSamples.add(scenarioQueueWaitSamples);
    const scenarioQueueP95 = Number(queueWindow.guard_queue_wait_p95_ms || 0);
    observedQueueP95.add(scenarioQueueP95);
    p1QueueP95.add(scenarioQueueP95);
    queueWaitWindowComplete.add(
      queueWindow.guard_queue_wait_window_complete === true ? 1 : 0,
    );
    const scenarioQueuedRequests = requireNonNegativeInteger(
      finalMockStats.queued_requests,
      "mock queued request count",
    );
    queuedRequests.add(scenarioQueuedRequests);
    queueWaitReconciled.add(
      scenarioQueueWaitSamples === scenarioQueuedRequests ? 1 : 0,
    );
    queueFullRequests.add(
      requireNonNegativeInteger(
        finalMockStats.queue_full_requests,
        "mock queue-full request count",
      ),
    );
    p1RejectionRate.add(rejectionRate(finalMockStats));
  } else if (SCENARIO === "upstream_failure") {
    const mockCalls = requireNonNegativeInteger(
      finalMockStats.total,
      "upstream-failure mock call count",
    );
    const injectedErrors = requireNonNegativeInteger(
      finalMockStats.errors,
      "injected upstream 500 count",
    );
    injected500Verified.add(
      mockCalls > 0 && injectedErrors === mockCalls ? 1 : 0,
    );
  } else if (SCENARIO === "upstream_429") {
    const stats = finalMockStats;
    const baselineEffectiveMax = onlineEffectiveMax(data.baseline || {}, true);
    const finalEffectiveMax = onlineEffectiveMax(finalGuard, true);
    const events = (stats.rate_limit_events_ms || []).map(Number).sort((a, b) => a - b);
    const first = events[0] || data.startedMs;
    const early = events.filter((value) => value >= first && value < first + 15000).length;
    const late = events.filter((value) => value >= first + 30000 && value < first + 45000).length;
    const earlyInflight = (stats.inflight_samples || [])
      .filter((item) => Number(item[0]) >= first && Number(item[0]) < first + 15000)
      .map((item) => Number(item[1]));
    const lateInflight = (stats.inflight_samples || [])
      .filter((item) => Number(item[0]) >= first + 30000 && Number(item[0]) < first + 45000)
      .map((item) => Number(item[1]));
    const earlyPeak = Math.max(0, ...earlyInflight);
    const latePeak = Math.max(0, ...lateInflight);
    const upstreamLimit = Number(stats.upstream_max_concurrency || 0);
    initial429.add(early);
    aimdEffectiveMaxReduced.add(finalEffectiveMax < baselineEffectiveMax ? 1 : 0);
    aimdConverged.add(
      early > 0 &&
        late < early &&
        earlyPeak > upstreamLimit &&
        latePeak > 0 &&
        latePeak <= upstreamLimit
        ? 1
        : 0,
    );
  } else if (SCENARIO === "degrade") {
    const stats = finalMockStats;
    const half = Math.floor(data.expectedMaxTokens / 2);
    const parameters = stats.request_parameters || [];
    const baselineTransitions = requireNonNegativeInteger(
      (data.baseline || {}).degrade_transition_samples,
      "baseline degrade transition samples",
    );
    const transitionWindow = guardSnapshot(
      API_KEYS[0],
      true,
      null,
      null,
      baselineTransitions,
    );
    const transitions = Array.isArray(transitionWindow.degrade_transitions)
      ? transitionWindow.degrade_transitions
          .map((event) => ({
            atMs: Number(event.at_ms),
            level: Number(event.level),
          }))
          .filter((event) => Number.isFinite(event.atMs) && Number.isInteger(event.level))
          .sort((left, right) => left.atMs - right.atMs)
      : [];
    const entered = transitions.find((event) => event.level === 1);
    const exited = entered
      ? transitions.find((event) => event.level === 0 && event.atMs > entered.atMs)
      : null;
    const enterElapsedMs = entered ? entered.atMs - data.startedMs : -1;
    const exitElapsedMs = exited ? exited.atMs - data.startedMs : -1;
    const fullBeforeEntry = entered
      ? parameters.some(
          (item) =>
            Number(item.at_ms) < entered.atMs &&
            Number(item.max_tokens) === data.expectedMaxTokens,
        )
      : false;
    const reducedAfterEntry = entered
      ? parameters.some(
          (item) =>
            Number(item.at_ms) >= entered.atMs &&
            (!exited || Number(item.at_ms) < exited.atMs) &&
            Number(item.max_tokens) === half,
        )
      : false;
    const effectiveMax = onlineEffectiveMax(data.baseline || {}, false) || 90;
    const highWindowProven = entered
      ? utilizationWindowProven(
          stats.inflight_samples || [],
          entered.atMs - 60000,
          entered.atMs,
          (value) => value / effectiveMax > 0.8,
        )
      : false;
    const lowWindowProven = exited
      ? utilizationWindowProven(
          stats.inflight_samples || [],
          exited.atMs - 120000,
          exited.atMs,
          (value) => value / effectiveMax < 0.7,
        )
      : false;
    degradeFullTokens.add(fullBeforeEntry ? 1 : 0);
    degradeHalfTokens.add(reducedAfterEntry ? 1 : 0);
    degradeExited.add(Number(finalGuard.degrade_level) === 0 ? 1 : 0);
    degradeEnterTimingValid.add(
      SMOKE || (enterElapsedMs >= 60000 && enterElapsedMs <= 110000) ? 1 : 0,
    );
    // Formal load stays above the high watermark through 100s, then ramps to
    // zero for 10s. Requiring exit no earlier than 220s prevents an immediate
    // or shortened low-watermark hold from passing the 120s contract.
    degradeExitTimingValid.add(SMOKE || exitElapsedMs >= 220000 ? 1 : 0);
    degradeTransitionWindowComplete.add(
      transitionWindow.degrade_transition_window_complete === true ? 1 : 0,
    );
    degradeHighWindowProven.add(highWindowProven ? 1 : 0);
    degradeLowWindowProven.add(lowWindowProven ? 1 : 0);
    p2RejectionRate.add(rejectionRate(finalMockStats));
  } else if (SCENARIO === "disconnect") {
    const before = data.baseline || {};
    let stats = mockStats(true);
    const disconnectTimestamps = (stats.disconnect_events_ms || [])
      .map(Number)
      .filter(Number.isFinite)
      .sort((left, right) => left - right);
    const lastDisconnectMs = disconnectTimestamps.length
      ? disconnectTimestamps[disconnectTimestamps.length - 1]
      : Date.now() - 5001;
    const recoveryDeadlineMs = lastDisconnectMs + 5000;
    let recovered = false;
    let recoveredAtMs = null;
    let currentAppInflight = -1;
    let currentMockInflight = -1;
    while (true) {
      finalGuard = guardSnapshot(API_KEYS[0], true);
      stats = mockStats(true);
      currentAppInflight = Number((finalGuard.guard_llm_inflight || {}).online);
      currentMockInflight = Number(stats.inflight);
      if (currentAppInflight === 0) {
        recovered = true;
        recoveredAtMs = Date.now();
        break;
      }
      const remainingMs = recoveryDeadlineMs - Date.now();
      if (remainingMs <= 0) break;
      sleep(Math.min(0.25, remainingMs / 1000));
    }
    const recoveryDuration = Math.max(
      0,
      (recoveredAtMs === null ? Date.now() : recoveredAtMs) - lastDisconnectMs,
    );
    const mockCalls = requireNonNegativeInteger(stats.total, "disconnect mock calls");
    const peakInflight = requireNonNegativeInteger(
      stats.peak_inflight,
      "disconnect mock peak inflight",
    );
    const clientStarted = requireNonNegativeInteger(
      stats.client_started,
      "disconnect client start count",
    );
    const clientCompleted = requireNonNegativeInteger(
      stats.client_completed,
      "disconnect client completion count",
    );
    cancelledDelta.add(
      Number(finalGuard.stream_cancelled_total || 0) - Number(before.stream_cancelled_total || 0),
    );
    const beforeAdmitted = requireNonNegativeInteger(
      (before.guard_llm_admitted_total || {}).online || 0,
      "baseline online guard admissions",
    );
    const finalAdmitted = requireNonNegativeInteger(
      (finalGuard.guard_llm_admitted_total || {}).online || 0,
      "final online guard admissions",
    );
    disconnectClientStarted.add(clientStarted);
    disconnectClientCompleted.add(clientCompleted);
    disconnectEvents.add(disconnectTimestamps.length);
    disconnectMockCalls.add(mockCalls);
    disconnectPeakInflight.add(peakInflight);
    disconnectAdmittedDelta.add(finalAdmitted - beforeAdmitted);
    disconnectFinalAppInflight.add(currentAppInflight);
    disconnectFinalMockInflight.add(currentMockInflight);
    inflightRecovered.add(recovered ? 1 : 0);
    inflightRecoveryMs.add(recoveryDuration);
  }
}

function requiredKeyCount(name) {
  const configuredVus = SMOKE ? scenarioNumber("VUS", 0) : 0;
  if (configuredVus > 0) return configuredVus;
  if (name === "spike" || name === "degrade") return 150;
  if (name === "upstream_429") return 300;
  if (name === "upstream_failure") return 80;
  return 30;
}

function mockControlFor(name) {
  const base = { reset_stats: true, upstream_max_concurrency: 20 };
  if (name === "upstream_failure") return { ...base, mode: "error", delay_ms: 0 };
  if (name === "upstream_429") {
    return {
      ...base,
      mode: "rate_limit",
      delay_ms: scenarioNumber("MOCK_DELAY_MS", 1000),
      upstream_max_concurrency: scenarioNumber("UPSTREAM_MAX_CONCURRENCY", 25),
    };
  }
  if (name === "single_user") return { ...base, mode: "slow", delay_ms: 10000 };
  if (name === "degrade") return { ...base, mode: "slow", delay_ms: 2000 };
  if (name === "disconnect") return { ...base, mode: "slow", delay_ms: 30000 };
  if (name === "spike") return { ...base, mode: "slow", delay_ms: 2000 };
  return { ...base, mode: "ok", delay_ms: 50 };
}

function keyForIteration() {
  if (SCENARIO === "single_user") return API_KEYS[0];
  const index = (exec.vu.idInTest - 1) % API_KEYS.length;
  return API_KEYS[index];
}

function requestParams(apiKey, timeout = "10s") {
  return {
    headers: { "Content-Type": "application/json", "X-API-Key": apiKey },
    timeout,
  };
}

function createSession(apiKey) {
  const response = http.post(
    `${BASE_URL}/sessions`,
    JSON.stringify({}),
    requestParams(apiKey, "10s"),
  );
  const ok = check(response, { "session created": (result) => result.status === 200 });
  if (!ok) return null;
  try {
    return response.json("data.session_id");
  } catch (_) {
    return null;
  }
}

function parseFrames(body) {
  const queueFrames = [];
  let queueEventCount = 0;
  let queueProtocolViolationCount = 0;
  let degradeCount = 0;
  let degradeProtocolViolationCount = 0;
  const pattern = /event:\s*([^\r\n]+)\r?\ndata:\s*([^\r\n]+)/g;
  let match;
  while ((match = pattern.exec(body)) !== null) {
    const eventName = match[1].trim();
    if (eventName === "degrade") {
      degradeCount += 1;
      try {
        if (!validDegradePayload(JSON.parse(match[2]))) {
          degradeProtocolViolationCount += 1;
        }
      } catch (_) {
        degradeProtocolViolationCount += 1;
      }
      continue;
    }
    if (eventName !== "queue") continue;
    queueEventCount += 1;
    try {
      const payload = JSON.parse(match[2]);
      if (!validQueuePayload(payload)) {
        queueProtocolViolationCount += 1;
        continue;
      }
      queueFrames.push(payload);
    } catch (_) {
      queueProtocolViolationCount += 1;
    }
  }
  return {
    queueFrames,
    queueEventCount,
    queueProtocolViolationCount,
    degradeCount,
    degradeProtocolViolationCount,
  };
}

function validQueuePayload(payload) {
  if (payload === null || typeof payload !== "object" || Array.isArray(payload)) {
    return false;
  }
  const position = payload.position;
  const queueSize = payload.queue_size;
  const estimatedWaitMs = payload.estimated_wait_ms;
  return (
    Number.isSafeInteger(position) &&
    position >= 1 &&
    Number.isSafeInteger(queueSize) &&
    queueSize >= position &&
    Number.isSafeInteger(estimatedWaitMs) &&
    estimatedWaitMs >= 1
  );
}

function validDegradePayload(payload) {
  if (payload === null || typeof payload !== "object" || Array.isArray(payload)) {
    return false;
  }
  const requiredEffects = ["thinking_disabled", "max_tokens_reduced"];
  return (
    (payload.level === 1 || payload.level === 2) &&
    typeof payload.reason === "string" &&
    payload.reason.trim().length > 0 &&
    Array.isArray(payload.effects) &&
    requiredEffects.every((effect) => payload.effects.includes(effect))
  );
}

function recordQueue(frames) {
  queueEvents.add(frames.queueEventCount);
  queueProtocolViolations.add(frames.queueProtocolViolationCount);
  const positions = frames.queueFrames.map((frame) => frame.position);
  let violations = 0;
  for (let index = 1; index < positions.length; index += 1) {
    if (positions[index] > positions[index - 1]) violations += 1;
  }
  queuePositionViolations.add(violations);
}

function errorCodes(body) {
  return [...body.matchAll(/"code"\s*:\s*(42900|42901|50300|50400|50401|50402)/g)].map((item) =>
    Number(item[1]),
  );
}

function hasPositiveRetryAfter(body, code) {
  const pattern = /event:\s*error\s*\r?\ndata:\s*([^\r\n]+)/g;
  let match;
  while ((match = pattern.exec(body)) !== null) {
    try {
      const payload = JSON.parse(match[1]);
      if (
        payload !== null &&
        typeof payload === "object" &&
        payload.code === code &&
        Number.isSafeInteger(payload.retry_after_ms) &&
        payload.retry_after_ms > 0
      ) {
        return true;
      }
    } catch (_) {
      // A malformed error frame cannot satisfy the retry contract.
    }
  }
  return false;
}

function allEventCodes(body) {
  return [...body.matchAll(/"code"\s*:\s*(\d{5})/g)].map((item) => Number(item[1]));
}

function runEndStatus(body) {
  const match = /event:\s*run_end\s*[\r\n]+data:\s*([^\r\n]+)/.exec(body);
  if (!match) return null;
  try {
    return String(JSON.parse(match[1]).status || "");
  } catch (_) {
    return null;
  }
}

function guardSnapshot(
  apiKey,
  required = false,
  firstOutcomeAfter = null,
  queueWaitAfter = null,
  degradeAfter = null,
) {
  const query = [];
  if (firstOutcomeAfter !== null) {
    query.push(`guard_first_outcome_after=${encodeURIComponent(firstOutcomeAfter)}`);
  }
  if (queueWaitAfter !== null) {
    query.push(`guard_queue_wait_after=${encodeURIComponent(queueWaitAfter)}`);
  }
  if (degradeAfter !== null) {
    query.push(`guard_degrade_after=${encodeURIComponent(degradeAfter)}`);
  }
  const suffix = query.length ? `?${query.join("&")}` : "";
  const response = http.get(`${BASE_URL}/metrics${suffix}`, requestParams(apiKey, "5s"));
  if (response.status !== 200) {
    if (required) throw new Error(`guard metrics unavailable: HTTP ${response.status}`);
    return {};
  }
  try {
    const snapshot = response.json("data.concurrency_guard") || {};
    if (required && snapshot.guard_llm_inflight === undefined) {
      throw new Error("guard metrics response is missing concurrency_guard fields");
    }
    return snapshot;
  } catch (error) {
    if (required) throw error;
    return {};
  }
}

function hasOpenCircuit(snapshot) {
  return Object.values(snapshot.cb_state || {}).some((value) => Number(value) === 2);
}

function isFastFailCircuitSnapshot(snapshot) {
  const openSeconds = Number(snapshot.cb_open_seconds);
  return (
    hasOpenCircuit(snapshot) &&
    Number.isFinite(openSeconds) &&
    openSeconds >= 0 &&
    openSeconds < 29
  );
}

function onlineEffectiveMax(snapshot, required = false) {
  const value = Number((snapshot.guard_llm_effective_max || {}).online);
  if (!Number.isSafeInteger(value) || value < 1) {
    if (required) {
      throw new Error(
        "guard metrics response is missing positive guard_llm_effective_max.online",
      );
    }
    return null;
  }
  return value;
}

function validateScenarioBaseline(name, snapshot) {
  if (name === "upstream_failure" && hasOpenCircuit(snapshot)) {
    throw new Error(
      "upstream_failure requires all circuit breakers closed at setup; restart " +
        "the dedicated backend or wait for recovery before formal acceptance",
    );
  }
  if (name === "disconnect") {
    const inflight = Number((snapshot.guard_llm_inflight || {}).online);
    if (inflight !== 0) {
      throw new Error(`disconnect requires zero online inflight at setup; received ${inflight}`);
    }
  }
  if (name === "upstream_429") {
    const effectiveMax = onlineEffectiveMax(snapshot, true);
    if (!SMOKE && effectiveMax !== 90) {
      throw new Error(
        `formal upstream_429 requires guard_llm_effective_max.online=90 at setup; ` +
          `received ${effectiveMax}`,
      );
    }
  }
}

function collectorSnapshot(apiKey, required = false) {
  const response = http.get(
    `${BASE_URL}/observability/health`,
    requestParams(apiKey, "10s"),
  );
  if (response.status !== 200) {
    if (required) throw new Error(`collector health unavailable: HTTP ${response.status}`);
    return {};
  }
  try {
    const snapshot = response.json("data") || {};
    if (
      required &&
      (snapshot.queue_depth === undefined || snapshot.spans_dropped_total === undefined)
    ) {
      throw new Error("collector health response is missing queue/drop counters");
    }
    return snapshot;
  } catch (error) {
    if (required) throw error;
    return {};
  }
}

function recordClientStart() {
  const response = http.post(`${MOCK_URL}/client-start`, null, { timeout: "5s" });
  return response.status === 200;
}

function recordClientDisconnect() {
  const response = http.post(`${MOCK_URL}/client-disconnect`, null, { timeout: "5s" });
  return response.status === 200;
}

function recordClientOutcome(success, firstOutcome, queued, queueFull, rejected) {
  const response = http.post(
    `${MOCK_URL}/client-outcome`,
    JSON.stringify({
      success,
      first_outcome: firstOutcome,
      queued,
      queue_full: queueFull,
      rejected,
    }),
    { headers: { "Content-Type": "application/json" }, timeout: "5s" },
  );
  return response.status === 200;
}

function mockStats(required = false) {
  const response = http.get(`${MOCK_URL}/stats`, { timeout: "5s" });
  if (response.status !== 200) {
    if (required) throw new Error(`mock stats unavailable: HTTP ${response.status}`);
    return {};
  }
  try {
    return response.json() || {};
  } catch (error) {
    if (required) throw error;
    return {};
  }
}

function waitForCollectorDrain(startedIso, apiKey, expectedCalls) {
  const timeoutSeconds = requirePositiveInteger(
    __ENV.COLLECTOR_DRAIN_TIMEOUT_SECONDS || 30,
    "COLLECTOR_DRAIN_TIMEOUT_SECONDS",
  );
  const deadline = Date.now() + timeoutSeconds * 1000;
  let stablePolls = 0;
  let lastQueueDepth = -1;
  let lastPersisted = -1;

  while (Date.now() <= deadline) {
    const health = collectorSnapshot(apiKey, true);
    lastQueueDepth = requireNonNegativeInteger(health.queue_depth, "collector queue depth");
    lastPersisted = loadSuccessfulCallCount(startedIso, apiKey);
    if (lastPersisted > expectedCalls) {
      throw new Error(
        `persisted LLM span count ${lastPersisted} exceeds mock call count ${expectedCalls}`,
      );
    }
    if (lastQueueDepth === 0 && lastPersisted === expectedCalls) {
      stablePolls += 1;
      if (stablePolls >= 2) return;
    } else {
      stablePolls = 0;
    }
    sleep(1);
  }
  throw new Error(
    `collector did not drain within ${timeoutSeconds}s: ` +
      `queue_depth=${lastQueueDepth}, persisted=${lastPersisted}, expected=${expectedCalls}`,
  );
}

function callListPage(startedIso, apiKey, page, pageSize) {
  const url =
    `${BASE_URL}/observability/calls?span_type=llm&status=ok&page_size=${pageSize}` +
    `&page=${page}&start_from=${encodeURIComponent(startedIso)}`;
  const response = http.get(url, requestParams(apiKey, "10s"));
  if (response.status !== 200) {
    throw new Error(`LLM call page ${page} unavailable: HTTP ${response.status}`);
  }
  let data;
  try {
    data = response.json("data");
  } catch (error) {
    throw new Error(`LLM call page ${page} returned invalid JSON: ${error}`);
  }
  if (!data || !Array.isArray(data.items)) {
    throw new Error(`LLM call page ${page} is missing data.items`);
  }
  return {
    items: data.items,
    total: requireNonNegativeInteger(data.total, `LLM call page ${page} total`),
  };
}

function loadSuccessfulCallCount(startedIso, apiKey) {
  return callListPage(startedIso, apiKey, 1, 1).total;
}

function loadTtftValues(startedIso, apiKey) {
  const pageSize = 200;
  const maxPages = requirePositiveInteger(
    __ENV.MAX_CALL_PAGES || 1000,
    "MAX_CALL_PAGES",
  );
  const values = [];
  let fetched = 0;
  let expectedTotal = null;

  for (let page = 1; page <= maxPages; page += 1) {
    const result = callListPage(startedIso, apiKey, page, pageSize);
    if (expectedTotal === null) expectedTotal = result.total;
    if (result.total !== expectedTotal) {
      throw new Error(
        `LLM call total changed during pagination: ${expectedTotal} -> ${result.total}`,
      );
    }
    if (result.items.length === 0 && fetched < expectedTotal) {
      throw new Error(
        `LLM call pagination ended early at page ${page}: fetched=${fetched}, total=${expectedTotal}`,
      );
    }
    fetched += result.items.length;
    if (fetched > expectedTotal) {
      throw new Error(`LLM call pagination exceeded total ${expectedTotal}`);
    }
    for (const item of result.items) {
      const value = Number(item.ttft_ms);
      if (
        item.ttft_ms !== null &&
        item.ttft_ms !== undefined &&
        Number.isFinite(value) &&
        value >= 0
      ) {
        values.push(value);
      }
    }
    if (fetched === expectedTotal) {
      return { values, total: expectedTotal };
    }
  }
  throw new Error(
    `LLM call pagination exceeded MAX_CALL_PAGES=${maxPages}; ` +
      `fetched=${fetched}, total=${expectedTotal}`,
  );
}

function requireNonNegativeInteger(value, label) {
  const number = Number(value);
  if (!Number.isSafeInteger(number) || number < 0) {
    throw new Error(`${label} must be a non-negative integer, received ${value}`);
  }
  return number;
}

function requirePositiveInteger(value, label) {
  const number = requireNonNegativeInteger(value, label);
  if (number < 1) throw new Error(`${label} must be at least 1`);
  return number;
}

function rejectionRate(stats) {
  const started = requirePositiveInteger(
    (stats || {}).client_started,
    "mock client start count for rejection rate",
  );
  const rejected = requireNonNegativeInteger(
    (stats || {}).rejected_requests,
    "mock rejected request count",
  );
  if (rejected > started) {
    throw new Error(
      `mock rejected request count ${rejected} exceeds client start count ${started}`,
    );
  }
  return rejected / started;
}

function utilizationWindowProven(samples, startMs, endMs, predicate) {
  if (!Array.isArray(samples) || !Number.isFinite(startMs) || !Number.isFinite(endMs)) {
    return false;
  }
  if (endMs <= startMs || typeof predicate !== "function") return false;
  const ordered = samples
    .map((item) => ({ atMs: Number(item[0]), value: Number(item[1]) }))
    .filter(
      (item) =>
        Number.isFinite(item.atMs) &&
        Number.isFinite(item.value) &&
        item.value >= 0,
    )
    .sort((left, right) => left.atMs - right.atMs);
  let anchor = null;
  for (const sample of ordered) {
    if (sample.atMs <= startMs) anchor = sample;
    else break;
  }
  if (anchor === null || !predicate(anchor.value)) return false;
  for (const sample of ordered) {
    if (sample.atMs <= startMs) continue;
    if (sample.atMs > endMs) break;
    if (!predicate(sample.value)) return false;
  }
  return true;
}

function queueWaitSampleDelta(data, finalGuard) {
  const before = requireNonNegativeInteger(
    (data.baseline || {}).guard_queue_wait_samples,
    "baseline guard_queue_wait_samples",
  );
  const after = requireNonNegativeInteger(
    finalGuard.guard_queue_wait_samples,
    "final guard_queue_wait_samples",
  );
  return after - before;
}
