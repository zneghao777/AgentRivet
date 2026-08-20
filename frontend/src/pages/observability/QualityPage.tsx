// 质量（OBSERVABILITY_SPEC §11 / §12.3）：反馈、评分、评测与版本回归
import { useCallback, useEffect, useState } from "react";
import { Empty, Progress, Table, Tag, message } from "antd";
import { useNavigate } from "react-router-dom";
import { getQuality } from "@/api/observability";
import { formatTime } from "@/utils/format";

const SOURCE_LABEL: Record<string, string> = {
  user: "用户反馈",
  human: "人工标注",
  rule: "规则",
  llm_eval: "LLM 评测",
  rag_eval: "RAG 评测",
};

export default function QualityPage({ hideHeader = false }: { hideHeader?: boolean } = {}) {
  const navigate = useNavigate();
  const [data, setData] = useState<{
    score_count: number;
    avg_by_source: Record<string, number>;
    recent_scores: { id: number; trace_id: string; score_name: string; value: number; source: string; reason: string | null; created_at: string | null }[];
  } | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const d = await getQuality();
      setData(d);
    } catch (e) {
      message.error(e instanceof Error ? e.message : "加载质量数据失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const avgEntries = Object.entries(data?.avg_by_source ?? {}).map(([source, avg]) => ({
    key: source,
    source,
    avg: Math.round(avg * 100),
  }));

  const scoreRows = (data?.recent_scores ?? []).map((s) => ({
    key: s.id,
    trace_id: s.trace_id,
    score_name: s.score_name,
    value: s.value,
    source: s.source,
    reason: s.reason,
    created_at: s.created_at,
  }));

  return (
    <div className="page-scroll obs-tab-pane">
      {!hideHeader ? (
        <header className="page-header">
          <div className="page-header-icon">
            <span className="material-symbols-rounded">tune</span>
          </div>
          <div className="page-header-copy">
            <h1>质量</h1>
            <p>"运行成功"与"回答有用"分开评估：反馈、评分、评测与版本回归</p>
          </div>
        </header>
      ) : null}

      <div className="obs-stat-grid">
        <article className="obs-stat-card">
          <div className="obs-stat-head">
            <div className="obs-stat-icon"><span className="material-symbols-rounded">score</span></div>
            <div className="obs-stat-label">评分总数</div>
          </div>
          <div className="obs-stat-value">{data?.score_count ?? 0}</div>
        </article>
        {avgEntries.map((e) => (
          <article className="obs-stat-card" key={e.key}>
            <div className="obs-stat-head">
              <div className="obs-stat-icon"><span className="material-symbols-rounded">grade</span></div>
              <div className="obs-stat-label">{SOURCE_LABEL[e.source] || e.source} 平均分</div>
            </div>
            <div className="obs-stat-value">{e.avg}%</div>
          </article>
        ))}
      </div>

      <div className="obs-panel">
        <div className="kb-panel-toolbar">
          <div className="kb-panel-title">
            <span className="material-symbols-rounded">feedback</span>
            <b>最近评分</b>
          </div>
        </div>
        {scoreRows.length === 0 && !loading ? (
          <Empty description="暂无评分。可在 Trace 详情中为用户反馈打分（POST /observability/traces/:id/scores）。" style={{ padding: 40 }} />
        ) : (
          <Table
            size="small"
            loading={loading}
            rowKey="key"
            dataSource={scoreRows}
            pagination={false}
            columns={[
              {
                title: "分数",
                dataIndex: "value",
                key: "value",
                width: 160,
                render: (v: number) => (
                  <Progress
                    percent={Math.round(v * 100)}
                    size="small"
                    status={v >= 0.8 ? "success" : v >= 0.5 ? "normal" : "exception"}
                  />
                ),
              },
              { title: "指标", dataIndex: "score_name", key: "score_name", width: 140 },
              { title: "来源", dataIndex: "source", key: "source", width: 110, render: (v: string) => <Tag bordered={false}>{SOURCE_LABEL[v] || v}</Tag> },
              {
                title: "Trace",
                dataIndex: "trace_id",
                key: "trace_id",
                render: (v: string) => (
                  <a onClick={() => navigate(`/observability/traces/${v}`)} style={{ fontFamily: "monospace", fontSize: 12 }}>
                    {v.slice(0, 16)}…
                  </a>
                ),
              },
              { title: "原因", dataIndex: "reason", key: "reason", ellipsis: true, render: (v: string | null) => v || "—" },
              { title: "时间", dataIndex: "created_at", key: "created_at", width: 160, render: (v: string | null) => (v ? formatTime(v) : "—") },
            ]}
          />
        )}
      </div>
    </div>
  );
}
