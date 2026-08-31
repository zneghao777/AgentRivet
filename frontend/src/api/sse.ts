// SSE 流式客户端：fetch + ReadableStream，手动解析 event:/data: 帧
// 因为使用 POST，不能用 EventSource
import type {
  SessionStart,
  TraceStart,
  MessageDelta,
  RunStepStart,
  RunStepUpdate,
  RunStepEnd,
  ToolStart,
  ToolEnd,
  InterruptRequested,
  MessageEnd,
  RunEnd,
  SseError,
  QueueStatus,
  DegradeStatus,
  OrchestrationRouterDecision,
  OrchestrationClarification,
  OrchestrationPlan,
  OrchestrationWorkerStarted,
  OrchestrationWorkerFinished,
  OrchestrationSynthesis,
} from "@/types/sse";
import { API_KEY_STORAGE_KEY, DEFAULT_API_KEY } from "@/constants";

export interface StreamHandlers {
  onSessionStart?(d: SessionStart): void;
  onTraceStart?(d: TraceStart): void;
  onMessageDelta?(d: MessageDelta): void;
  onRunStepStart?(d: RunStepStart): void;
  onRunStepUpdate?(d: RunStepUpdate): void;
  onRunStepEnd?(d: RunStepEnd): void;
  onToolStart?(d: ToolStart): void;
  onToolEnd?(d: ToolEnd): void;
  onInterrupt?(d: InterruptRequested): void;
  onMessageEnd?(d: MessageEnd): void;
  onRunEnd?(d: RunEnd): void;
  onError?(d: SseError): void;
  onQueue?(d: QueueStatus): void;
  onDegrade?(d: DegradeStatus): void;
  onOrchestrationRouterDecision?(d: OrchestrationRouterDecision): void;
  onOrchestrationClarification?(d: OrchestrationClarification): void;
  onOrchestrationPlan?(d: OrchestrationPlan): void;
  onOrchestrationWorkerStarted?(d: OrchestrationWorkerStarted): void;
  onOrchestrationWorkerFinished?(d: OrchestrationWorkerFinished): void;
  onOrchestrationSynthesis?(d: OrchestrationSynthesis): void;
  onClose?(): void;
}

export interface ChatRuntimeOptions {
  skills?: string[];
  knowledge_bases?: string[];
  capabilities?: Array<{ kind: "skill" | "mcp_tool"; id: string }>;
  model?: string;
  provider_config?: {
    base_url: string;
    api_key: string;
  };
  attachment_ids?: string[];
  agent_id?: string;
}

export interface ChatStreamRequest extends ChatRuntimeOptions {
  message: string;
  approval_mode?: "manual" | "auto";
}

export interface ChatResumeRequest extends ChatRuntimeOptions {
  input: string;
  decision: "approve" | "reject";
}

export async function streamChat(
  sessionId: string,
  body: ChatStreamRequest,
  handlers: StreamHandlers,
  signal?: AbortSignal
): Promise<void> {
  return postStream(`/sessions/${sessionId}/messages/stream`, body, handlers, signal);
}

export async function resumeChat(
  sessionId: string,
  body: ChatResumeRequest,
  handlers: StreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  return postStream(`/sessions/${sessionId}/resume`, body, handlers, signal);
}

async function postStream(
  path: string,
  body: ChatStreamRequest | ChatResumeRequest,
  handlers: StreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const apiKey = localStorage.getItem(API_KEY_STORAGE_KEY) || DEFAULT_API_KEY;
  const base = import.meta.env.VITE_API_BASE ?? "/api/v1";

  const resp = await fetch(`${base}${path}`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(apiKey ? { "X-API-Key": apiKey } : {}),
    },
    body: JSON.stringify(body),
    signal,
  });

  if (!resp.ok || !resp.body) {
    let payload: {
      code?: number;
      message?: string;
      retry_after_ms?: number;
      detail?: string | {
        code?: number;
        message?: string;
        retry_after_ms?: number;
      };
    } = {};
    try {
      payload = await resp.json();
    } catch {
      payload = {};
    }
    const detail = typeof payload.detail === "string" ? payload.detail : payload.detail?.message ?? "";
    const msg =
      resp.status === 401
        ? "鉴权失败(401)：请确认 API Key 已配置且与后端一致"
        : resp.status === 503
          ? payload.message || detail || "服务未就绪(503)：请检查后端是否启动"
          : payload.message || detail || `请求失败: HTTP ${resp.status}`;
    const retryAfterHeader = Number(resp.headers.get("Retry-After"));
    const retryAfterMs = payload.retry_after_ms
      ?? (payload.detail && typeof payload.detail === "object" ? payload.detail.retry_after_ms : undefined)
      ?? (Number.isFinite(retryAfterHeader) && retryAfterHeader > 0
        ? retryAfterHeader * 1000
        : undefined);
    handlers.onError?.({
      code: payload.code
        ?? (payload.detail && typeof payload.detail === "object" ? payload.detail.code : undefined)
        ?? resp.status,
      message: msg,
      retry_after_ms: retryAfterMs,
    });
    handlers.onClose?.();
    return;
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let eventName = "";
  let dataLines: string[] = [];

  const dispatch = (event: string, data: string) => {
    if (!data) return;
    try {
      const d = JSON.parse(data);
      switch (event) {
        case "session_start":
          handlers.onSessionStart?.(d as SessionStart);
          break;
        case "trace_start":
          handlers.onTraceStart?.(d as TraceStart);
          break;
        case "message_delta":
          handlers.onMessageDelta?.(d as MessageDelta);
          break;
        case "run_step_start":
          handlers.onRunStepStart?.(d as RunStepStart);
          break;
        case "run_step_update":
          handlers.onRunStepUpdate?.(d as RunStepUpdate);
          break;
        case "run_step_end":
          handlers.onRunStepEnd?.(d as RunStepEnd);
          break;
        case "tool_start":
          handlers.onToolStart?.(d as ToolStart);
          break;
        case "tool_end":
          handlers.onToolEnd?.(d as ToolEnd);
          break;
        case "interrupt_requested":
          handlers.onInterrupt?.(d as InterruptRequested);
          break;
        case "message_end":
          handlers.onMessageEnd?.(d as MessageEnd);
          break;
        case "run_end":
          handlers.onRunEnd?.(d as RunEnd);
          break;
        case "error":
          handlers.onError?.(d as SseError);
          break;
        case "queue":
          handlers.onQueue?.(d as QueueStatus);
          break;
        case "degrade":
          handlers.onDegrade?.(d as DegradeStatus);
          break;
        case "orchestration.router_decision":
          handlers.onOrchestrationRouterDecision?.(d as OrchestrationRouterDecision);
          break;
        case "orchestration.clarification":
          handlers.onOrchestrationClarification?.(d as OrchestrationClarification);
          break;
        case "orchestration.plan":
          handlers.onOrchestrationPlan?.(d as OrchestrationPlan);
          break;
        case "orchestration.worker_started":
          handlers.onOrchestrationWorkerStarted?.(d as OrchestrationWorkerStarted);
          break;
        case "orchestration.worker_finished":
          handlers.onOrchestrationWorkerFinished?.(d as OrchestrationWorkerFinished);
          break;
        case "orchestration.synthesis":
          handlers.onOrchestrationSynthesis?.(d as OrchestrationSynthesis);
          break;
        default:
          break;
      }
    } catch {
      // 解析失败跳过
    }
  };

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // 按空行拆帧
    let idx: number;
    while ((idx = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, idx);
      buffer = buffer.slice(idx + 2);
      // 解析帧内行（兼容 \r\n）
      for (const rawLine of frame.split("\n")) {
        const line = rawLine.replace(/\r$/, "");
        if (!line || line.startsWith(":")) continue; // 跳过空行/心跳注释
        if (line.startsWith("event:")) {
          eventName = line.slice(6).trim();
        } else if (line.startsWith("data:")) {
          dataLines.push(line.slice(5).trim());
        }
      }
      if (eventName && dataLines.length) {
        dispatch(eventName, dataLines.join("\n"));
      }
      eventName = "";
      dataLines = [];
    }
  }

  handlers.onClose?.();
}
