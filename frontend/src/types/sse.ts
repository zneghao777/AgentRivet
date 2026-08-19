// SSE 流式事件类型（对应后端 events.py）

export interface SessionStart {
  session_id: string;
  thread_id: string;
}

export interface MessageDelta {
  id: string;
  content: string;
}

export interface ToolStart {
  id: string;
  name: string;
  arguments: Record<string, unknown>;
}

export interface ToolEnd {
  id: string;
  name: string;
  result: string;
}

export interface InterruptRequested {
  id: string;
  reason: string;
  state: Record<string, unknown>;
}

export interface MessageEnd {
  id: string;
  token_usage?: Record<string, number>;
}

export interface RunEnd {
  status: string;
  duration_ms: number;
  tool_calls: number;
}

export interface SseError {
  code: number;
  message: string;
}
