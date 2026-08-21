// 可观测性前端类型（对应后端 router_observability / schemas）
import type { RunStepReference } from "@/types/sse";

export interface TrendPoint {
  ts: string;
  requests: number;
  errors: number;
  tokens_prompt: number;
  tokens_completion: number;
  latency_p50_ms: number;
  latency_p95_ms: number;
}

export interface ToolStatItem {
  tool_name: string;
  calls: number;
  success: number;
  avg_duration_ms: number;
}

export interface RecentErrorTrace {
  trace_id: string;
  span_id: string;
  span_name: string;
  status: string;
  started_at: string | null;
  duration_ms: number;
  error: string | null;
  span_count: number;
  tool_count: number;
}

export interface OverviewData {
  today_requests: number;
  today_success_rate: number;
  today_errors: number;
  today_tokens: { prompt: number; completion: number; total: number };
  today_latency_p50_ms: number;
  today_latency_p95_ms: number;
  active_sessions: number;
  trend: TrendPoint[];
  tool_stats: ToolStatItem[];
  recent_errors: RecentErrorTrace[];
}

export interface TraceItem {
  trace_id: string;
  span_id: string;
  session_id: string | null;
  span_name: string;
  status: string;
  started_at: string | null;
  ended_at: string | null;
  duration_ms: number;
  model: string | null;
  token_usage: Record<string, number> | null;
  input_summary: string | null;
  output_summary: string | null;
  error: string | null;
  span_count: number;
  tool_count: number;
}

export interface TraceListData {
  items: TraceItem[];
  total: number;
  page: number;
  page_size: number;
}

export interface SpanNode {
  span_id: string;
  parent_span_id: string | null;
  span_name: string;
  span_type: string;
  status: string;
  model: string | null;
  token_usage: Record<string, number> | null;
  input_summary: string | null;
  output_summary: string | null;
  duration_ms: number;
  started_at: string | null;
  ended_at: string | null;
  error: string | null;
  children: SpanNode[];
}

export interface TraceDetail {
  trace_id: string;
  root: SpanNode | null;
  token_usage: Record<string, number> | null;
  duration_ms: number;
  status: string;
  started_at: string | null;
  ended_at: string | null;
  error: string | null;
}

export interface TimelineItem {
  span_id: string;
  parent_span_id: string | null;
  span_name: string;
  span_type: string;
  status: string;
  duration_ms: number;
  start_ms: number;
  total_ms: number;
}

export interface TimelineData {
  items: TimelineItem[];
  start: string | null;
  end: string | null;
}

export interface TraceExplorerSummary {
  trace_id: string;
  request_id: string | null;
  session_id: string | null;
  thread_id: string | null;
  status: string;
  started_at: string | null;
  ended_at: string | null;
  duration_ms: number;
  span_count: number;
  tool_calls: number;
  token_usage: Record<string, number> | null;
  ttft_ms: number | null;
  generation_ms: number | null;
  throughput: number | null;
  content_capture_mode: string;
}

export type TraceEventKind =
  | "system"
  | "user"
  | "context"
  | "assistant"
  | "tool"
  | "node";

export interface TraceEventItem {
  event_id: string;
  kind: TraceEventKind;
  role: string;
  name: string;
  summary: string | null;
  status: string;
  span_id: string | null;
  parent_span_id: string | null;
  started_at: string | null;
  ended_at: string | null;
  start_ms: number;
  duration_ms: number;
  token_usage: Record<string, number> | null;
  ttft_ms: number | null;
  generation_ms: number | null;
  throughput: number | null;
  model: string | null;
  model_provider: string | null;
  finish_reason: string | null;
  input_summary: string | null;
  output_summary: string | null;
  error: string | null;
  capture_mode: string;
  references?: RunStepReference[];
  raw: Record<string, unknown>;
  turn_index: number;
  step_index: number;
}

export interface TraceLaneBlock {
  block_id: string;
  lane: "input" | "model" | "tools";
  event_id: string;
  span_id: string | null;
  status: string;
  start_ms: number;
  duration_ms: number;
}

export interface TraceEventsData {
  trace: TraceExplorerSummary;
  events: TraceEventItem[];
  lanes: TraceLaneBlock[];
}

export interface ToolLogItem {
  span_id: string;
  trace_id: string;
  session_id: string | null;
  tool_name: string;
  status: string;
  input_summary: string | null;
  output_summary: string | null;
  duration_ms: number;
  started_at: string | null;
  error: string | null;
}

export interface ToolLogsData {
  items: ToolLogItem[];
  total: number;
  page: number;
  page_size: number;
}
