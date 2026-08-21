// Trace 列表（OBSERVABILITY_SPEC §3.3）：表格 + 状态/时间/关键词筛选
import { useCallback, useEffect, useState } from "react";
import { Empty, Input, Pagination, Select, Tag, message } from "antd";
import { useNavigate } from "react-router-dom";
import { getTraces } from "@/api/observability";
import type { TraceListData } from "@/types/observability";
import { formatTime, summarize } from "@/utils/format";
import { InspectorField, MetricStrip, ResponsiveInspector, StatusDot } from "@/components/common/Workspace";

export default function TraceListPage({ hideHeader = false }: { hideHeader?: boolean } = {}) {
  const navigate = useNavigate();
  const [data, setData] = useState<TraceListData>({ items: [], total: 0, page: 1, page_size: 20 });
  const [loading, setLoading] = useState(false);
  const [status, setStatus] = useState<string>("");
  const [keyword, setKeyword] = useState("");
  const [searchKeyword, setSearchKeyword] = useState("");
  const [page, setPage] = useState(1);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [inspectorOpen, setInspectorOpen] = useState(false);

  const load = useCallback(
    async (p: number, s: string, k: string) => {
      setLoading(true);
      try {
        const d = await getTraces({ page: p, page_size: 20, status: s || undefined, keyword: k || undefined });
        setData(d);
        setSelectedId((current) => current && d.items.some((item) => item.trace_id === current) ? current : d.items[0]?.trace_id ?? null);
      } catch (e) {
        message.error(e instanceof Error ? e.message : "加载 Trace 失败");
      } finally {
        setLoading(false);
      }
    },
    []
  );

  useEffect(() => {
    void load(page, status, searchKeyword);
  }, [load, page, searchKeyword, status]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setPage(1);
      setSearchKeyword(keyword.trim());
    }, 320);
    return () => window.clearTimeout(timer);
  }, [keyword]);
  const selected = data.items.find((trace) => trace.trace_id === selectedId) ?? null;
  const successful = data.items.filter((trace) => trace.status === "ok").length;
  const totalTokens = data.items.reduce((sum, trace) => sum + (trace.token_usage?.total ?? 0), 0);
  const totalTools = data.items.reduce((sum, trace) => sum + trace.tool_count, 0);
  const averageDuration = data.items.length ? Math.round(data.items.reduce((sum, trace) => sum + trace.duration_ms, 0) / data.items.length) : 0;

  return (
    <div className="page-scroll obs-tab-pane">
      {!hideHeader ? (
        <header className="page-header">
          <div className="page-header-icon">
            <span className="material-symbols-rounded">account_tree</span>
          </div>
          <div className="page-header-copy">
            <h1>Agent 轨迹</h1>
            <p>一次完整运行的 span 树：根 agent.run → node → llm / tool</p>
          </div>
        </header>
      ) : null}

      <MetricStrip items={[{ label: "Trace 数", value: data.total, meta: `当前页 ${data.items.length}` }, { label: "成功率", value: `${data.items.length ? Math.round(successful / data.items.length * 100) : 0}%`, meta: `${successful}/${data.items.length}`, tone: "success" }, { label: "平均持续时间", value: `${averageDuration}ms`, meta: "当前页" }, { label: "总 Token", value: totalTokens.toLocaleString(), meta: "当前页", tone: "teal" }, { label: "工具调用", value: totalTools, meta: "当前页", tone: "warning" }]} />
      <div className="trace-master-detail">
        <section className="obs-panel trace-master-panel" aria-label="Trace 列表">
          <div className="kb-panel-toolbar">
            <div className="kb-panel-title">
              <span className="material-symbols-rounded">history</span>
              <b>Trace 列表</b>
              <Tag bordered={false}>{data.total} 条</Tag>
            </div>
            <div className="obs-filters">
              <Select
                size="small"
                style={{ minWidth: 110 }}
                value={status}
                onChange={(v) => { setStatus(v); setPage(1); }}
                options={[
                  { value: "", label: "全部状态" },
                  { value: "ok", label: "成功" },
                  { value: "error", label: "错误" },
                ]}
              />
              <Input
                size="small"
                allowClear
                prefix={<span className="material-symbols-rounded" style={{ fontSize: 14 }}>search</span>}
                placeholder="关键字（会话/工具/摘要）…"
                style={{ width: 220 }}
                value={keyword}
                onChange={(e) => setKeyword(e.target.value)}
              />
            </div>
          </div>

          {loading && data.items.length === 0 ? (
            <div className="kb-skeleton">
              {Array.from({ length: 6 }).map((_, i) => (
                <div className="kb-skeleton-row" style={{ height: 56 }} key={i} />
              ))}
            </div>
          ) : data.items.length === 0 ? (
            <Empty description="暂无 Trace。发起一次对话后，这里会展示完整的 Agent 执行轨迹。" style={{ padding: 40 }} />
          ) : (
            <>
              <div className="kb-chunk-list">
                {data.items.map((t) => (
                  <button
                    type="button"
                    className="obs-trace-row"
                    key={t.trace_id}
                    onClick={() => { setSelectedId(t.trace_id); setInspectorOpen(true); }}
                    onDoubleClick={() => navigate(`/observability/traces/${t.trace_id}`)}
                    aria-selected={selectedId === t.trace_id}
                  >
                    <span className={`obs-node-dot ${t.status}`} />
                    <div className="obs-trace-main">
                      <div className="obs-trace-name">
                        {summarize(t.input_summary || t.output_summary || t.span_name, 52)}
                      </div>
                      <div className="obs-trace-sub">
                        <small className="mono">{t.trace_id.slice(0, 12)}…</small>
                        <small>{formatTime(t.started_at)}</small>
                        <small>{t.duration_ms}ms</small>
                        <small>{t.span_count} spans</small>
                        <small>{t.tool_count} tools</small>
                        {t.token_usage ? <small>{((t.token_usage.total ?? 0)).toLocaleString()} tokens</small> : null}
                        {t.error ? <span className="obs-err-text">{t.error}</span> : null}
                      </div>
                    </div>
                    <Tag color={t.status === "ok" ? "success" : "error"} bordered={false}>
                      {t.status === "ok" ? "成功" : "错误"}
                    </Tag>
                  </button>
                ))}
              </div>
              {data.total > data.page_size ? (
                <div style={{ display: "flex", justifyContent: "center", marginTop: 16 }}>
                  <Pagination current={page} total={data.total} pageSize={data.page_size} onChange={setPage} />
                </div>
              ) : null}
            </>
          )}
        </section>
        <ResponsiveInspector open={inspectorOpen && Boolean(selected)} onClose={() => setInspectorOpen(false)} title="Trace 检视" subtitle={selected ? <StatusDot label={selected.status === "ok" ? "成功" : "错误"} tone={selected.status === "ok" ? "success" : "danger"} /> : undefined}>
          {!selected ? <Empty description="选择一条 Trace 查看详情" /> : <><section className="inspector-section"><h3>运行摘要</h3><InspectorField label="根 Span">{selected.span_name}</InspectorField><InspectorField label="开始时间">{formatTime(selected.started_at)}</InspectorField><InspectorField label="持续时间">{selected.duration_ms}ms</InspectorField><InspectorField label="Span 数">{selected.span_count}</InspectorField><InspectorField label="工具调用">{selected.tool_count}</InspectorField><InspectorField label="Token">{(selected.token_usage?.total ?? 0).toLocaleString()}</InspectorField></section><section className="inspector-section"><h3>Trace ID</h3><p className="inspector-copy mono">{selected.trace_id}</p></section>{selected.error ? <section className="inspector-section"><h3>错误</h3><p className="inspector-copy obs-err-text">{selected.error}</p></section> : null}<section className="inspector-section inspector-button-stack"><button type="button" className="inspector-primary-action" onClick={() => navigate(`/observability/traces/${selected.trace_id}`)}>查看完整 Trace</button></section></>}
        </ResponsiveInspector>
      </div>
    </div>
  );
}
