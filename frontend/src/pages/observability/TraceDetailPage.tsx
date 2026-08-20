// Trace 详情（OBSERVABILITY_SPEC §3.3）：span 树 + 详情面板 + 时间线视图
import { useCallback, useEffect, useMemo, useState } from "react";
import { Button, Empty, Skeleton, Tag, Tabs, message } from "antd";
import { useNavigate, useParams } from "react-router-dom";
import { getTraceDetail, getTraceTimeline } from "@/api/observability";
import type { SpanNode, TimelineItem, TraceDetail as TraceDetailData } from "@/types/observability";
import { formatTime } from "@/utils/format";

const TYPE_LABEL: Record<string, string> = {
  root: "根",
  node: "节点",
  llm: "LLM",
  tool: "工具",
  workflow: "工作流",
};

function dotClass(span: { span_type: string; status: string }): string {
  if (span.status === "error") return "error";
  if (span.span_type === "llm") return "llm";
  if (span.span_type === "tool") return "tool";
  return "ok";
}

function SpanTree({
  nodes,
  selectedId,
  onSelect,
  depth = 0,
  expanded,
  onToggle,
}: {
  nodes: SpanNode[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  depth?: number;
  expanded: Set<string>;
  onToggle: (id: string) => void;
}) {
  return (
    <div className="obs-tree" style={{ marginLeft: depth > 0 ? 16 : 0 }}>
      {nodes.map((node) => {
        const hasChildren = node.children.length > 0;
        const isExpanded = expanded.has(node.span_id);
        return (
          <div key={node.span_id}>
            <div
              className={`obs-node${selectedId === node.span_id ? " selected" : ""}`}
              onClick={() => onSelect(node.span_id)}
              style={{ paddingLeft: 10 + depth * 14 }}
            >
              <span
                className="obs-node-toggle"
                onClick={(e) => { e.stopPropagation(); if (hasChildren) onToggle(node.span_id); }}
                style={{ visibility: hasChildren ? "visible" : "hidden" }}
              >
                <span className="material-symbols-rounded">{isExpanded ? "expand_more" : "chevron_right"}</span>
              </span>
              <span className={`obs-node-dot ${dotClass(node)}`} />
              <span className="obs-node-name">{node.span_name}</span>
              <span className="obs-node-meta">{node.duration_ms}ms</span>
            </div>
            {hasChildren && isExpanded ? (
              <div className="obs-node-children">
                <SpanTree
                  nodes={node.children}
                  selectedId={selectedId}
                  onSelect={onSelect}
                  depth={depth + 1}
                  expanded={expanded}
                  onToggle={onToggle}
                />
              </div>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

function flattenSpans(root: SpanNode): SpanNode[] {
  const out: SpanNode[] = [];
  const walk = (n: SpanNode) => {
    out.push(n);
    n.children.forEach(walk);
  };
  if (root) walk(root);
  return out;
}

export default function TraceDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [detail, setDetail] = useState<TraceDetailData | null>(null);
  const [timeline, setTimeline] = useState<TimelineItem[] | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [view, setView] = useState<"tree" | "timeline" | "logs">("tree");
  const [logs, setLogs] = useState<{ id: number; timestamp: string | null; severity_text: string; body: string | null; module: string | null; span_id: string | null }[]>([]);
  const [logsLoading, setLogsLoading] = useState(false);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (detail?.root) {
      const ids = flattenSpans(detail.root).map((n) => n.span_id);
      setExpanded(new Set(ids));
      setSelectedId(ids[0] ?? null);
    }
  }, [detail]);

  const load = useCallback(async () => {
    if (!id) return;
    setLoading(true);
    try {
      const [d, tl] = await Promise.all([getTraceDetail(id), getTraceTimeline(id)]);
      setDetail(d);
      setTimeline(tl.items);
    } catch (e) {
      message.error(e instanceof Error ? e.message : "加载 Trace 详情失败");
    } finally {
      setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  useEffect(() => {
    void load();
  }, [load]);

  const spans = useMemo(() => (detail?.root ? flattenSpans(detail.root) : []), [detail]);
  const selected = spans.find((s) => s.span_id === selectedId) ?? null;

  const toggle = (id: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const copyTraceId = async () => {
    if (!id) return;
    try {
      await navigator.clipboard.writeText(id);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      message.info(id);
    }
  };

  const loadLogs = useCallback(async () => {
    if (!id) return;
    setLogsLoading(true);
    try {
      const { getTraceLogs } = await import("@/api/observability");
      const d = await getTraceLogs(id);
      setLogs(d.items);
    } catch (e) {
      message.error(e instanceof Error ? e.message : "加载关联日志失败");
    } finally {
      setLogsLoading(false);
    }
  }, [id]);

  if (loading && !detail) {
    return (
      <div className="page-scroll">
        <Skeleton active paragraph={{ rows: 8 }} />
      </div>
    );
  }

  if (!detail) {
    return (
      <div className="page-scroll">
        <Empty description="Trace 不存在或已被清理" style={{ padding: 60 }} />
      </div>
    );
  }

  return (
    <div className="page-scroll">
      <header className="page-header">
        <div className="page-header-icon">
          <span className="material-symbols-rounded">account_tree</span>
        </div>
        <div className="page-header-copy">
          <h1>Trace 详情</h1>
          <p style={{ fontFamily: "monospace" }}>{detail.trace_id}</p>
        </div>
        <Button icon={<span className="material-symbols-rounded" style={{ fontSize: 16 }}>content_copy</span>} onClick={() => void copyTraceId()}>
          {copied ? "已复制" : "复制 trace_id"}
        </Button>
        <Button onClick={() => navigate("/observability/traces")}>返回</Button>
      </header>

      <div className="obs-panel">
        <div className="kb-panel-toolbar">
          <div className="kb-panel-title">
            <span className="material-symbols-rounded">info</span>
            <b>{detail.status === "ok" ? "成功" : "错误"}</b>
            <Tag bordered={false}>{detail.duration_ms}ms</Tag>
            {detail.token_usage ? (
              <Tag bordered={false}>{(detail.token_usage.total ?? 0).toLocaleString()} tokens</Tag>
            ) : null}
            <span className="obs-trace-sub" style={{ marginTop: 0 }}>
              {spans.length} spans · {formatTime(detail.started_at)}
            </span>
          </div>
          <Tabs
            size="small"
            activeKey={view}
            onChange={(k) => { setView(k as "tree" | "timeline" | "logs"); if (k === "logs") void loadLogs(); }}
            items={[
              { key: "tree", label: "Span 树" },
              { key: "timeline", label: "时间线" },
              { key: "logs", label: `日志${logs.length ? ` (${logs.length})` : ""}` },
            ]}
          />
        </div>

        {detail.error ? (
          <div className="chat-error"><span className="obs-err-text">{detail.error}</span></div>
        ) : null}
      </div>

      {view === "tree" ? (
        <div className="obs-detail-split">
          <div className="obs-panel obs-detail-tree">
            <div className="obs-panel-title">
              <span className="material-symbols-rounded">hierarchy</span>
              <b>Span 层级</b>
            </div>
            {detail.root ? (
              <SpanTree
                nodes={[detail.root]}
                selectedId={selectedId}
                onSelect={setSelectedId}
                expanded={expanded}
                onToggle={toggle}
              />
            ) : (
              <Empty description="无 span" style={{ padding: 20 }} />
            )}
          </div>

          <div className="obs-panel obs-detail-panel">
            <div className="obs-panel-title">
              <span className="material-symbols-rounded">description</span>
              <b>{selected ? selected.span_name : "详情"}</b>
              {selected ? (
                <Tag bordered={false} color={selected.status === "ok" ? "success" : "error"}>
                  {TYPE_LABEL[selected.span_type] ?? selected.span_type} · {selected.status}
                </Tag>
              ) : null}
            </div>
            {!selected ? (
              <Empty description="点击左侧 span 查看详情" style={{ padding: 24 }} />
            ) : (
              <div>
                <div className="obs-detail-field">
                  <div className="obs-detail-label">span_id</div>
                  <div className="obs-detail-value mono">{selected.span_id}</div>
                </div>
                {selected.model ? (
                  <div className="obs-detail-field">
                    <div className="obs-detail-label">模型</div>
                    <div className="obs-detail-value">{selected.model}</div>
                  </div>
                ) : null}
                <div className="obs-detail-field">
                  <div className="obs-detail-label">耗时</div>
                  <div className="obs-detail-value">{selected.duration_ms}ms · {formatTime(selected.started_at)}</div>
                </div>
                {selected.token_usage ? (
                  <div className="obs-detail-field">
                    <div className="obs-detail-label">Token</div>
                    <div className="obs-detail-value">
                      prompt {selected.token_usage.prompt ?? 0} · completion {selected.token_usage.completion ?? 0} · total {selected.token_usage.total ?? 0}
                    </div>
                  </div>
                ) : null}
                {selected.input_summary ? (
                  <div className="obs-detail-field">
                    <div className="obs-detail-label">入参摘要</div>
                    <div className="obs-detail-value">{selected.input_summary}</div>
                  </div>
                ) : selected.span_type === "llm" || selected.span_type === "tool" ? (
                  <div className="obs-detail-field">
                    <div className="obs-detail-label">入参</div>
                    <div className="obs-detail-value capture-note">
                      <span className="material-symbols-rounded" style={{ fontSize: 13 }}>lock</span>
                      内容未采集（metadata_only）或已脱敏
                    </div>
                  </div>
                ) : null}
                {selected.output_summary ? (
                  <div className="obs-detail-field">
                    <div className="obs-detail-label">出参摘要</div>
                    <div className="obs-detail-value">{selected.output_summary}</div>
                  </div>
                ) : selected.span_type === "llm" || selected.span_type === "tool" ? (
                  <div className="obs-detail-field">
                    <div className="obs-detail-label">出参</div>
                    <div className="obs-detail-value capture-note">
                      <span className="material-symbols-rounded" style={{ fontSize: 13 }}>lock</span>
                      内容未采集（metadata_only）或已脱敏
                    </div>
                  </div>
                ) : null}
                {selected.error ? (
                  <div className="obs-detail-field">
                    <div className="obs-detail-label">错误</div>
                    <div className="obs-detail-value" style={{ color: "var(--danger)" }}>{selected.error}</div>
                  </div>
                ) : null}
              </div>
            )}
          </div>
        </div>
      ) : (
        <div className="obs-panel">
          <div className="obs-panel-title">
            <span className="material-symbols-rounded">view_timeline</span>
            <b>执行时间线（Gantt）</b>
          </div>
          {!timeline || timeline.length === 0 ? (
            <Empty description="暂无时间线数据" style={{ padding: 24 }} />
          ) : (
            <div className="obs-timeline">
              {timeline.map((t) => (
                <div className="obs-tl-row" key={t.span_id}>
                  <span className="obs-tl-name">{t.span_name}</span>
                  <div className="obs-tl-track">
                    <div
                      className="obs-tl-bar"
                      style={{
                        left: `${(t.start_ms / t.total_ms) * 100}%`,
                        width: `${Math.max((t.duration_ms / t.total_ms) * 100, 0.6)}%`,
                        background: t.status === "error" ? "var(--danger)" : t.span_type === "llm" ? "var(--brand-400)" : t.span_type === "tool" ? "#e95400" : "var(--success)",
                      }}
                    />
                  </div>
                  <span style={{ width: 70, textAlign: "right" }}>{t.duration_ms}ms</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {view === "logs" ? (
        <div className="obs-panel">
          <div className="obs-panel-title">
            <span className="material-symbols-rounded">terminal</span>
            <b>关联日志</b>
            <span className="obs-trace-sub" style={{ marginTop: 0 }}>按 trace_id 关联（§8.1），可跳转到日志页搜索</span>
          </div>
          {logsLoading ? (
            <Skeleton active paragraph={{ rows: 4 }} />
          ) : logs.length === 0 ? (
            <Empty description="该 Trace 暂无 WARN/ERROR 级关联日志" style={{ padding: 32 }} />
          ) : (
            <div className="obs-log-list">
              {logs.map((l) => (
                <div className="obs-log-row" key={l.id}>
                  <span className={`obs-log-sev sev-${l.severity_text.toLowerCase()}`}>{l.severity_text}</span>
                  <span className="obs-log-time">{l.timestamp ? formatTime(l.timestamp) : "—"}</span>
                  <span className="obs-log-body">{l.body}</span>
                  {l.span_id ? <span className="obs-log-span" style={{ fontFamily: "monospace", fontSize: 11 }}>{l.span_id.slice(0, 10)}…</span> : null}
                </div>
              ))}
            </div>
          )}
        </div>
      ) : null}
    </div>
  );
}
