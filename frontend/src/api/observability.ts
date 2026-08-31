// 可观测性 API 封装（OBSERVABILITY_SPEC §3.6）
import { getData } from "@/api/http";
import type {
  OverviewData,
  TimelineData,
  TraceEventsData,
  ToolLogsData,
  ToolStatItem,
  TraceDetail,
  TraceListData,
  TrendPoint,
  RouterStatsData,
} from "@/types/observability";

export function getOverview(): Promise<OverviewData> {
  return getData<OverviewData>("/observability/overview");
}

export function getRouterStats(): Promise<RouterStatsData> {
  return getData<RouterStatsData>("/observability/router-stats");
}

export function getTraces(params: {
  page?: number;
  page_size?: number;
  status?: string;
  span_type?: string;
  keyword?: string;
  start_from?: string;
  start_to?: string;
}): Promise<TraceListData> {
  const search = new URLSearchParams();
  if (params.page) search.set("page", String(params.page));
  if (params.page_size) search.set("page_size", String(params.page_size));
  if (params.status) search.set("status", params.status);
  if (params.span_type) search.set("span_type", params.span_type);
  if (params.keyword) search.set("keyword", params.keyword);
  if (params.start_from) search.set("start_from", params.start_from);
  if (params.start_to) search.set("start_to", params.start_to);
  return getData<TraceListData>(`/observability/traces?${search.toString()}`);
}

export function getTraceDetail(traceId: string): Promise<TraceDetail> {
  return getData<TraceDetail>(`/observability/traces/${traceId}`);
}

export function getTraceTimeline(traceId: string): Promise<TimelineData> {
  return getData<TimelineData>(`/observability/traces/${traceId}/timeline`);
}

export function getTraceEvents(traceId: string): Promise<TraceEventsData> {
  return getData<TraceEventsData>(`/observability/traces/${traceId}/events`);
}

export function getTraceLogs(traceId: string): Promise<{
  items: { id: number; timestamp: string | null; severity_text: string; body: string | null; module: string | null; span_id: string | null; trace_id: string | null }[];
}> {
  return getData(`/observability/traces/${traceId}/logs`);
}

export function getToolLogs(params: {
  page?: number;
  page_size?: number;
  tool_name?: string;
  status?: string;
  start_from?: string;
  start_to?: string;
}): Promise<ToolLogsData> {
  const search = new URLSearchParams();
  if (params.page) search.set("page", String(params.page));
  if (params.page_size) search.set("page_size", String(params.page_size));
  if (params.tool_name) search.set("tool_name", params.tool_name);
  if (params.status) search.set("status", params.status);
  if (params.start_from) search.set("start_from", params.start_from);
  if (params.start_to) search.set("start_to", params.start_to);
  return getData<ToolLogsData>(`/observability/tool-logs?${search.toString()}`);
}

export function getToolsStats(): Promise<{ items: ToolStatItem[] }> {
  return getData<{ items: ToolStatItem[] }>("/observability/tools/stats");
}

export function getTrend(params: {
  granularity?: "hour" | "day";
  days?: number;
}): Promise<{ items: TrendPoint[] }> {
  const search = new URLSearchParams();
  if (params.granularity) search.set("granularity", params.granularity);
  if (params.days) search.set("days", String(params.days));
  return getData<{ items: TrendPoint[] }>(
    `/observability/metrics/trend?${search.toString()}`
  );
}

// ---------- v2 接口（OBSERVABILITY_SPEC §12） ----------

export interface CallItem {
  span_id: string;
  trace_id: string;
  session_id: string | null;
  span_name: string;
  span_type: string;
  status: string;
  model: string | null;
  model_provider: string | null;
  model_name_resolved: string | null;
  token_usage: Record<string, number> | null;
  ttft_ms: number | null;
  input_summary: string | null;
  output_summary: string | null;
  duration_ms: number;
  started_at: string | null;
  error: string | null;
  finish_reason: string | null;
  retry_count: number;
  cost_total: number | null;
}

export interface LogItem {
  id: number;
  timestamp: string | null;
  severity_text: string;
  body: string | null;
  trace_id: string | null;
  span_id: string | null;
  service_name: string | null;
  module: string | null;
  instance_id: string | null;
  logger_name: string | null;
  exception_type: string | null;
  exception_message: string | null;
  stacktrace: string | null;
}

export interface AlertRuleItem {
  id: number;
  name: string;
  metric: string;
  threshold: number;
  comparison: string;
  window_minutes: number;
  min_samples: number;
  enabled: boolean;
  severity: "warning" | "critical";
  created_at: string | null;
}

export interface IncidentItem {
  id: number;
  rule_id: number | null;
  status: string;
  severity: "warning" | "critical";
  metric: string;
  threshold: number;
  current_value: number;
  window_minutes: number;
  message: string | null;
  related_trace_ids: string[] | null;
  created_at: string | null;
  resolved_at: string | null;
}

export function getCalls(params: {
  span_type?: string;
  model?: string;
  tool_name?: string;
  status?: string;
  page?: number;
  page_size?: number;
}): Promise<{ items: CallItem[]; total: number; page: number; page_size: number }> {
  const search = new URLSearchParams();
  if (params.span_type) search.set("span_type", params.span_type);
  if (params.model) search.set("model", params.model);
  if (params.tool_name) search.set("tool_name", params.tool_name);
  if (params.status) search.set("status", params.status);
  if (params.page) search.set("page", String(params.page));
  if (params.page_size) search.set("page_size", String(params.page_size));
  return getData(`/observability/calls?${search.toString()}`);
}

export function getLogs(params: {
  severity?: string;
  trace_id?: string;
  module?: string;
  keyword?: string;
  limit?: number;
}): Promise<{ items: LogItem[]; next_cursor: number | null }> {
  const search = new URLSearchParams();
  if (params.severity) search.set("severity", params.severity);
  if (params.trace_id) search.set("trace_id", params.trace_id);
  if (params.module) search.set("module", params.module);
  if (params.keyword) search.set("keyword", params.keyword);
  if (params.limit) search.set("limit", String(params.limit));
  return getData(`/observability/logs?${search.toString()}`);
}

export function getCosts(params?: {
  group_by?: string;
}): Promise<{
  total_cost: number;
  total_tokens: number;
  unconfigured_price_calls: number;
  groups: Record<string, { cost: number; tokens: number; input_tokens: number; output_tokens: number }>;
}> {
  const search = new URLSearchParams();
  if (params?.group_by) search.set("group_by", params.group_by);
  const qs = search.toString();
  return getData(`/observability/costs${qs ? `?${qs}` : ""}`);
}

export function getModelPrices(): Promise<{
  items: {
    id: number;
    model_provider: string | null;
    model_name: string;
    input_price_per_million: number;
    cached_input_price_per_million: number;
    output_price_per_million: number;
    currency: string;
    effective_from: string | null;
    version: string | null;
  }[];
}> {
  return getData("/observability/model-prices");
}

export function getQuality(): Promise<{
  score_count: number;
  avg_by_source: Record<string, number>;
  recent_scores: { id: number; trace_id: string; score_name: string; value: number; source: string; reason: string | null; created_at: string | null }[];
}> {
  return getData("/observability/quality");
}

export function getAlerts(): Promise<{ items: AlertRuleItem[] }> {
  return getData("/observability/alerts");
}

export function getIncidents(status?: string): Promise<{ items: IncidentItem[] }> {
  const qs = status ? `?status=${status}` : "";
  return getData(`/observability/incidents${qs}`);
}

export function getCollectorHealth(): Promise<{
  spans_dropped_total: number;
  queue_depth: number;
  active_sessions: number;
  latest_aggregation: string | null;
  data_lag_seconds: number | null;
}> {
  return getData("/observability/health");
}
