// SSE 流式客户端：fetch + ReadableStream，手动解析 event:/data: 帧
// 因为使用 POST，不能用 EventSource
import type {
  SessionStart,
  MessageDelta,
  ToolStart,
  ToolEnd,
  InterruptRequested,
  MessageEnd,
  RunEnd,
  SseError,
} from "@/types/sse";
import { API_KEY_STORAGE_KEY, DEFAULT_API_KEY } from "@/constants";

export interface StreamHandlers {
  onSessionStart?(d: SessionStart): void;
  onMessageDelta?(d: MessageDelta): void;
  onToolStart?(d: ToolStart): void;
  onToolEnd?(d: ToolEnd): void;
  onInterrupt?(d: InterruptRequested): void;
  onMessageEnd?(d: MessageEnd): void;
  onRunEnd?(d: RunEnd): void;
  onError?(d: SseError): void;
  onClose?(): void;
}

export async function streamChat(
  sessionId: string,
  body: { message: string; skills?: string[]; knowledge_bases?: string[] },
  handlers: StreamHandlers,
  signal?: AbortSignal
): Promise<void> {
  const apiKey = localStorage.getItem(API_KEY_STORAGE_KEY) || DEFAULT_API_KEY;
  const base = import.meta.env.VITE_API_BASE ?? "/api/v1";

  const resp = await fetch(`${base}/sessions/${sessionId}/messages/stream`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(apiKey ? { "X-API-Key": apiKey } : {}),
    },
    body: JSON.stringify(body),
    signal,
  });

  if (!resp.ok || !resp.body) {
    const msg =
      resp.status === 401
        ? "鉴权失败(401)：请确认 API Key 已配置且与后端一致"
        : resp.status === 503
          ? "服务未就绪(503)：请检查后端是否启动"
          : `请求失败: HTTP ${resp.status}`;
    handlers.onError?.({ code: resp.status, message: msg });
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
        case "message_delta":
          handlers.onMessageDelta?.(d as MessageDelta);
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
