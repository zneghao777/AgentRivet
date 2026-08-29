import { memo, useCallback, useEffect, useMemo, useState } from "react";
import { Button, Empty, Skeleton, Tooltip, message } from "antd";
import { useNavigate, useParams } from "react-router-dom";
import { getTraceEvents, getTraceLogs } from "@/api/observability";
import type { TraceEventItem, TraceEventsData, TraceLaneBlock } from "@/types/observability";
import { formatTime } from "@/utils/format";

type ExplorerMode = "duration" | "turns" | "calls";
type DetailTab = "summary" | "preview" | "raw";
type ExplorerSection = "trace" | "logs";

interface TraceLogItem {
  id: number;
  timestamp: string | null;
  severity_text: string;
  body: string | null;
  module: string | null;
  span_id: string | null;
}

const STATUS_LABEL: Record<string, string> = {
  ok: "成功",
  error: "错误",
  timeout: "超时",
  canceled: "已取消",
  interrupted: "已中断",
  running: "运行中",
  queued: "排队中",
};

const EVENT_META: Record<TraceEventItem["kind"], { label: string; icon: string }> = {
  system: { label: "SYSTEM", icon: "settings" },
  user: { label: "USER", icon: "person" },
  context: { label: "CONTEXT", icon: "dataset" },
  assistant: { label: "ASSISTANT", icon: "smart_toy" },
  tool: { label: "TOOL", icon: "build" },
  node: { label: "NODE", icon: "account_tree" },
};

const MODE_OPTIONS: { key: ExplorerMode; label: string; icon: string }[] = [
  { key: "duration", label: "Duration", icon: "schedule" },
  { key: "turns", label: "Turns", icon: "view_agenda" },
  { key: "calls", label: "Calls", icon: "grid_view" },
];

function tokenValue(usage: Record<string, number> | null | undefined, canonical: string, legacy: string): number {
  return Number(usage?.[canonical] ?? usage?.[legacy] ?? 0);
}

function formatDuration(ms: number | null | undefined): string {
  if (ms == null) return "—";
  if (ms < 1000) return `${ms.toLocaleString()}ms`;
  return `${(ms / 1000).toFixed(ms >= 10_000 ? 1 : 2)}s`;
}

function formatExactTime(iso: string | null): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(date);
}

function LaneOverview({ blocks, totalMs, selectedId, onSelect }: {
  blocks: TraceLaneBlock[];
  totalMs: number;
  selectedId: string | null;
  onSelect: (eventId: string) => void;
}) {
  const lanes: { key: TraceLaneBlock["lane"]; label: string }[] = [
    { key: "input", label: "Input" },
    { key: "model", label: "Model" },
    { key: "tools", label: "Tools" },
  ];

  return (
    <section className="trace-lanes" aria-label="Trace 相对时间泳道">
      {lanes.map((lane) => (
        <div className="trace-lane-row" key={lane.key}>
          <span className="trace-lane-label">{lane.label}</span>
          <div className="trace-lane-track">
            {blocks.filter((block) => block.lane === lane.key).map((block) => {
              const left = Math.min(99.4, Math.max(0, (block.start_ms / totalMs) * 100));
              const width = Math.max(0.65, Math.min(100 - left, (block.duration_ms / totalMs) * 100));
              return (
                <Tooltip key={block.block_id} title={`${lane.label} · ${formatDuration(block.duration_ms)}`}>
                  <button
                    type="button"
                    className={`trace-lane-block ${lane.key}${block.status === "error" ? " error" : ""}${selectedId === block.event_id ? " selected" : ""}`}
                    style={{ left: `${left}%`, width: `${width}%` }}
                    onClick={() => onSelect(block.event_id)}
                    aria-label={`查看 ${lane.label} 事件`}
                  />
                </Tooltip>
              );
            })}
          </div>
        </div>
      ))}
    </section>
  );
}

const EventRow = memo(function EventRow({ event, selected, onSelect, depth = 0 }: {
  event: TraceEventItem;
  selected: boolean;
  onSelect: (eventId: string) => void;
  depth?: number;
}) {
  const meta = EVENT_META[event.kind];
  return (
    <div className="trace-event-with-turn">
      {event.kind === "user" ? <div className="trace-turn-marker">Turn {event.turn_index}</div> : null}
      <button
        type="button"
        className={`trace-event-row ${event.kind}${selected ? " selected" : ""}`}
        style={{ marginLeft: `${Math.min(4, depth) * 14}px` }}
        onClick={() => onSelect(event.event_id)}
        aria-pressed={selected}
      >
        <span className={`trace-event-kind ${event.kind}`}>
          <span className="material-symbols-rounded" aria-hidden="true">{meta.icon}</span>
          {meta.label}
        </span>
        <span className="trace-event-content">
          <span className="trace-event-title">{event.name}</span>
          <span className={`trace-event-summary${event.summary ? "" : " muted"}`}>
            {event.summary || "仅采集运行元数据"}
          </span>
          <span className="trace-event-meta">
            {event.duration_ms > 0 ? <span>{formatDuration(event.duration_ms)}</span> : null}
            {event.model ? <span>{event.model}</span> : null}
            {event.kind === "tool" ? <span>1 tool call</span> : null}
            {event.token_usage ? <span>{tokenValue(event.token_usage, "total", "total").toLocaleString()} tokens</span> : null}
            {event.task_id ? <span className="trace-task-badge" title={event.task_id}>task {event.task_id.slice(0, 8)}</span> : null}
          </span>
        </span>
        <span className="trace-event-step">Step {event.step_index}</span>
        <span className={`trace-event-status ${event.status}`}>{STATUS_LABEL[event.status] ?? event.status}</span>
      </button>
    </div>
  );
});

function Inspector({ event }: { event: TraceEventItem | null }) {
  const [tab, setTab] = useState<DetailTab>("summary");

  useEffect(() => setTab("summary"), [event?.event_id]);

  if (!event) {
    return <aside className="trace-inspector-panel"><Empty description="选择一条事件查看详情" /></aside>;
  }

  const meta = EVENT_META[event.kind];
  const inputTokens = tokenValue(event.token_usage, "input", "prompt");
  const outputTokens = tokenValue(event.token_usage, "output", "completion");
  const reasoningTokens = tokenValue(event.token_usage, "reasoning", "reasoning");
  const totalTokens = tokenValue(event.token_usage, "total", "total");

  return (
    <aside className="trace-inspector-panel" aria-label="事件检视">
      <header className="trace-inspector-header">
        <span className={`trace-event-kind ${event.kind}`}>{meta.label}</span>
        <span>Turn {event.turn_index}</span>
        <span>Step {event.step_index}</span>
        <span className={`trace-inspector-state ${event.status}`}><i />{STATUS_LABEL[event.status] ?? event.status}</span>
      </header>

      <nav className="trace-inspector-tabs" aria-label="检视内容">
        {(["summary", "preview", "raw"] as DetailTab[]).map((item) => (
          <button type="button" key={item} className={tab === item ? "active" : ""} onClick={() => setTab(item)}>
            {item === "summary" ? "Summary" : item === "preview" ? "Preview" : "Raw"}
          </button>
        ))}
      </nav>

      <div className="trace-inspector-scroll">
        {tab === "summary" ? (
          <div className="trace-inspector-summary">
            <section className="trace-inspector-section">
              <h3>调用摘要</h3>
              <dl className="trace-inspector-kv">
                <div><dt>Source</dt><dd>{event.name}</dd></div>
                <div><dt>Status</dt><dd>{STATUS_LABEL[event.status] ?? event.status}</dd></div>
                {event.model ? <div><dt>Model</dt><dd>{event.model}</dd></div> : null}
                {event.finish_reason ? <div><dt>Finish</dt><dd>{event.finish_reason}</dd></div> : null}
                <div><dt>Capture</dt><dd>{event.capture_mode}</dd></div>
              </dl>
            </section>

            {event.token_usage ? (
              <section className="trace-inspector-section">
                <h3>Tokens</h3>
                <dl className="trace-inspector-kv tokens">
                  <div><dt>Total</dt><dd>{totalTokens.toLocaleString()} tok</dd></div>
                  <div><dt>Input</dt><dd>{inputTokens.toLocaleString()} tok</dd></div>
                  <div><dt>Output</dt><dd>{outputTokens.toLocaleString()} tok</dd></div>
                  {reasoningTokens > 0 ? <div><dt>Reasoning</dt><dd>{reasoningTokens.toLocaleString()} tok</dd></div> : null}
                </dl>
              </section>
            ) : null}

            <section className="trace-inspector-section">
              <h3>Request Timing</h3>
              <dl className="trace-inspector-kv">
                <div><dt>Started</dt><dd>{formatExactTime(event.started_at)}</dd></div>
                <div><dt>Total duration</dt><dd>{formatDuration(event.duration_ms)}</dd></div>
                {event.ttft_ms != null ? <div><dt>TTFT</dt><dd>{formatDuration(event.ttft_ms)}</dd></div> : null}
                {event.generation_ms != null ? <div><dt>Generation</dt><dd>{formatDuration(event.generation_ms)}</dd></div> : null}
                {event.throughput != null ? <div><dt>Throughput</dt><dd>{event.throughput} tok/s</dd></div> : null}
              </dl>
            </section>

            {event.span_id ? (
              <section className="trace-inspector-section"><h3>Span</h3><p className="trace-mono-value">{event.span_id}</p></section>
            ) : null}
          </div>
        ) : null}

        {tab === "preview" ? (
          <div className="trace-preview-stack">
            <section><h3>Input</h3><p>{event.input_summary || "未采集输入内容"}</p></section>
            <section><h3>Output</h3><p>{event.output_summary || event.summary || "未采集输出内容"}</p></section>
            {event.error ? <section className="error"><h3>Error</h3><p>{event.error}</p></section> : null}
          </div>
        ) : null}

        {tab === "raw" ? <pre className="trace-raw-view">{JSON.stringify(event.raw, null, 2)}</pre> : null}
      </div>
    </aside>
  );
}

function TraceLogs({ items, loading }: { items: TraceLogItem[]; loading: boolean }) {
  if (loading) return <Skeleton active paragraph={{ rows: 8 }} />;
  if (!items.length) return <Empty description="该 Trace 暂无关联日志" />;
  return (
    <div className="trace-log-list">
      {items.map((log) => (
        <div className={`trace-log-row ${log.severity_text.toLowerCase()}`} key={log.id}>
          <time>{formatExactTime(log.timestamp)}</time>
          <span className="trace-log-level">{log.severity_text}</span>
          <span>{log.module || "—"}</span>
          <p>{log.body || "—"}</p>
        </div>
      ))}
    </div>
  );
}

export default function TraceDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [data, setData] = useState<TraceEventsData | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [mode, setMode] = useState<ExplorerMode>("duration");
  const [section, setSection] = useState<ExplorerSection>("trace");
  const [keyword, setKeyword] = useState("");
  const [loading, setLoading] = useState(true);
  const [copied, setCopied] = useState(false);
  const [logs, setLogs] = useState<TraceLogItem[]>([]);
  const [logsLoading, setLogsLoading] = useState(false);

  const load = useCallback(async () => {
    if (!id) return;
    setLoading(true);
    try {
      const result = await getTraceEvents(id);
      setData(result);
      const initial = result.events.find((event) => event.kind === "user") ?? result.events[0];
      setSelectedId(initial?.event_id ?? null);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "加载 Trace 详情失败");
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => { void load(); }, [load]);

  const loadLogs = useCallback(async () => {
    if (!id || logsLoading || logs.length) return;
    setLogsLoading(true);
    try {
      const result = await getTraceLogs(id);
      setLogs(result.items);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "加载关联日志失败");
    } finally {
      setLogsLoading(false);
    }
  }, [id, logs.length, logsLoading]);

  const filteredEvents = useMemo(() => {
    if (!data) return [];
    const query = keyword.trim().toLowerCase();
    return data.events.filter((event) => {
      const modeMatch = mode === "duration"
        || (mode === "turns" && ["user", "assistant", "context", "system"].includes(event.kind))
        || (mode === "calls" && Boolean(event.span_id) && ["assistant", "tool"].includes(event.kind));
      if (!modeMatch) return false;
      if (!query) return true;
      return [event.name, event.summary, event.model, event.span_id, event.status]
        .some((value) => value?.toLowerCase().includes(query));
    });
  }, [data, keyword, mode]);

  const eventDepths = useMemo(() => {
    const bySpan = new Map(data?.events.filter((event) => event.span_id).map((event) => [event.span_id as string, event]) ?? []);
    const depths = new Map<string, number>();
    const depthOf = (event: TraceEventItem, seen = new Set<string>()): number => {
      if (!event.span_id || !event.parent_span_id || seen.has(event.span_id)) return 0;
      const cached = depths.get(event.span_id);
      if (cached != null) return cached;
      const parent = bySpan.get(event.parent_span_id);
      if (!parent) return 0;
      seen.add(event.span_id);
      const depth = Math.min(4, depthOf(parent, seen) + 1);
      depths.set(event.span_id, depth);
      return depth;
    };
    data?.events.forEach((event) => depths.set(event.event_id, depthOf(event)));
    return depths;
  }, [data]);

  useEffect(() => {
    if (filteredEvents.length && !filteredEvents.some((event) => event.event_id === selectedId)) {
      setSelectedId(filteredEvents[0].event_id);
    }
  }, [filteredEvents, selectedId]);

  const selected = useMemo(
    () => data?.events.find((event) => event.event_id === selectedId) ?? null,
    [data, selectedId],
  );

  const copyTraceId = async () => {
    if (!id) return;
    try {
      await navigator.clipboard.writeText(id);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      message.info(id);
    }
  };

  if (loading && !data) {
    return <div className="page-scroll trace-explorer-page"><Skeleton active paragraph={{ rows: 12 }} /></div>;
  }
  if (!data) {
    return <div className="page-scroll trace-explorer-page"><Empty description="Trace 不存在或已被清理" /></div>;
  }

  const trace = data.trace;
  const totalTokens = tokenValue(trace.token_usage, "total", "total");

  return (
    <div className="page-scroll trace-explorer-page">
      <header className="page-header trace-explorer-header">
        <div className="page-header-icon"><span className="material-symbols-rounded">account_tree</span></div>
        <div className="page-header-copy"><h1>Trace 详情</h1><p className="trace-header-id">{trace.trace_id}</p></div>
        <Button icon={<span className="material-symbols-rounded">content_copy</span>} onClick={() => void copyTraceId()}>{copied ? "已复制" : "复制 trace_id"}</Button>
        <Button onClick={() => navigate("/observability/traces")}>返回</Button>
      </header>

      <section className="trace-explorer-summary" aria-label="Trace 运行摘要">
        <div className={`trace-explorer-status ${trace.status}`}>
          <span className="material-symbols-rounded">{trace.status === "ok" ? "check_circle" : "error"}</span>
          <span><small>运行状态</small><strong>{STATUS_LABEL[trace.status] ?? trace.status}</strong></span>
        </div>
        <div className="trace-explorer-metric"><small>总耗时</small><strong>{formatDuration(trace.duration_ms)}</strong></div>
        <div className="trace-explorer-metric"><small>Token</small><strong>{totalTokens.toLocaleString()}</strong></div>
        <div className="trace-explorer-metric"><small>Span</small><strong>{trace.span_count}</strong></div>
        <div className="trace-explorer-metric"><small>工具调用</small><strong>{trace.tool_calls}</strong></div>
        <div className="trace-explorer-metric wide"><small>开始时间</small><strong>{formatTime(trace.started_at)}</strong></div>
        <div className="trace-capture-badge"><span className="material-symbols-rounded">shield_lock</span>{trace.content_capture_mode}</div>
      </section>

      <section className="trace-explorer-surface">
        <nav className="trace-section-tabs" aria-label="Trace 详情视图">
          <button type="button" className={section === "trace" ? "active" : ""} onClick={() => setSection("trace")}>运行轨迹</button>
          <button type="button" className={section === "logs" ? "active" : ""} onClick={() => { setSection("logs"); void loadLogs(); }}>日志{logs.length ? ` ${logs.length}` : ""}</button>
        </nav>

        {section === "trace" ? (
          <>
            <div className="trace-explorer-toolbar">
              <div className="trace-mode-switch" role="group" aria-label="事件显示模式">
                {MODE_OPTIONS.map((item) => (
                  <button type="button" key={item.key} className={mode === item.key ? "active" : ""} onClick={() => setMode(item.key)}>
                    <span className="material-symbols-rounded">{item.icon}</span>{item.label}
                  </button>
                ))}
              </div>
              <label className="trace-search-box">
                <span className="material-symbols-rounded">search</span>
                <input value={keyword} onChange={(event) => setKeyword(event.target.value)} placeholder="搜索事件、模型、Span..." />
                {keyword ? <button type="button" onClick={() => setKeyword("")} aria-label="清空搜索"><span className="material-symbols-rounded">close</span></button> : null}
              </label>
            </div>

            <LaneOverview blocks={data.lanes} totalMs={Math.max(1, trace.duration_ms)} selectedId={selectedId} onSelect={setSelectedId} />

            <div className="trace-workbench-grid">
              <section className="trace-events-panel" aria-label="Trace 事件流">
                <header><div><strong>执行事件</strong><span>{filteredEvents.length} 条</span></div><small>按真实发生时间排序</small></header>
                <div className="trace-event-list">
                  {filteredEvents.length ? filteredEvents.map((event) => (
                    <EventRow key={event.event_id} event={event} selected={event.event_id === selectedId} onSelect={setSelectedId} depth={eventDepths.get(event.event_id) ?? 0} />
                  )) : <Empty description="没有匹配的事件" />}
                </div>
              </section>
              <Inspector event={selected} />
            </div>
          </>
        ) : <div className="trace-logs-panel"><TraceLogs items={logs} loading={logsLoading} /></div>}
      </section>
    </div>
  );
}
