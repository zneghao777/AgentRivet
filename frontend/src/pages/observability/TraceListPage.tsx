// Trace 列表（OBSERVABILITY_SPEC §3.3）：表格 + 状态/时间/关键词筛选
import { useCallback, useEffect, useState } from "react";
import { Empty, Input, Pagination, Select, Tag, message } from "antd";
import { useNavigate } from "react-router-dom";
import { getTraces } from "@/api/observability";
import type { TraceListData } from "@/types/observability";
import { formatTime } from "@/utils/format";

export default function TraceListPage({ hideHeader = false }: { hideHeader?: boolean } = {}) {
  const navigate = useNavigate();
  const [data, setData] = useState<TraceListData>({ items: [], total: 0, page: 1, page_size: 20 });
  const [loading, setLoading] = useState(false);
  const [status, setStatus] = useState<string>("");
  const [keyword, setKeyword] = useState("");
  const [page, setPage] = useState(1);

  const load = useCallback(
    async (p: number, s: string, k: string) => {
      setLoading(true);
      try {
        const d = await getTraces({ page: p, page_size: 20, status: s || undefined, keyword: k || undefined });
        setData(d);
      } catch (e) {
        message.error(e instanceof Error ? e.message : "加载 Trace 失败");
      } finally {
        setLoading(false);
      }
    },
    []
  );

  useEffect(() => {
    void load(page, status, keyword);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page]);

  const applyFilter = (p = 1) => {
    setPage(p);
    void load(p, status, keyword);
  };

  const onKeywordChange = (value: string) => {
    setKeyword(value);
    applyFilter(1);
  };

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

      <div className="obs-panel">
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
              onChange={(v) => { setStatus(v); applyFilter(1); }}
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
              onChange={(e) => onKeywordChange(e.target.value)}
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
                <div
                  className="obs-trace-row"
                  key={t.trace_id}
                  onClick={() => navigate(`/observability/traces/${t.trace_id}`)}
                >
                  <span className={`obs-node-dot ${t.status}`} />
                  <div className="obs-trace-main">
                    <div className="obs-trace-name">
                      {t.trace_id.slice(0, 16)}… · {t.span_name}
                    </div>
                    <div className="obs-trace-sub">
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
                </div>
              ))}
            </div>
            {data.total > data.page_size ? (
              <div style={{ display: "flex", justifyContent: "center", marginTop: 16 }}>
                <Pagination current={page} total={data.total} pageSize={data.page_size} onChange={setPage} />
              </div>
            ) : null}
          </>
        )}
      </div>
    </div>
  );
}
