// SSE 流式事件类型（对应后端 events.py）

export interface SessionStart {
  session_id: string;
  thread_id: string;
}

export interface TraceStart {
  trace_id: string;
  request_id: string | null;
  session_id: string | null;
}

export type RunStepKind = "node" | "llm" | "tool";

export interface RunStepStart {
  id: string;
  kind: RunStepKind;
  name: string;
  parent_id: string | null;
  status: "running";
  summary: string | null;
  started_at: string | null;
}

export interface RunStepUpdate {
  id: string;
  phase: string;
  detail: Record<string, unknown>;
}

export interface RunStepReference {
  rank: number;
  kb_id: string;
  chunk_id: number;
  doc_id: number | null;
  source_file: string;
  chunk_index: number | null;
  heading: string | null;
  line_start: number | null;
  line_end: number | null;
  score: number | null;
  strategy: string | null;
  excerpt: string | null;
}

export interface RunStepEnd {
  id: string;
  status: string;
  duration_ms: number;
  summary: string | null;
  error: string | null;
  token_usage: Record<string, number> | null;
  references: RunStepReference[];
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
