// 可观测性前端类型（对应后端 router_observability / schemas）

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
