// 观测台（OBSERVABILITY_SPEC §3）：统一入口，顶部水平 tab 切换各功能区
// 总览 + 运行记录 + 调用分析 + 日志 + 成本 + 质量 + 告警
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Bar,
  CartesianGrid,
  Cell,
  ComposedChart,
  Legend,
  Line,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { Button, Empty, Tag, message } from "antd";
import { useSearchParams } from "react-router-dom";
import { getOverview } from "@/api/observability";
import type { OverviewData } from "@/types/observability";
import { formatTime } from "@/utils/format";
import TraceListPage from "./TraceListPage";
import CallsPage from "./CallsPage";
import LogsPage from "./LogsPage";
import CostsPage from "./CostsPage";
import QualityPage from "./QualityPage";
import AlertsPage from "./AlertsPage";

const SPAN_COLORS = ["#0050cb", "#008a7e", "#e95400", "#7c3aed", "#d97706", "#0e7490", "#be123c"];

const TABS = [
  { key: "overview", label: "总览", icon: "monitoring" },
  { key: "traces", label: "运行记录", icon: "account_tree" },
  { key: "calls", label: "调用分析", icon: "hub" },
  { key: "logs", label: "日志", icon: "terminal" },
  { key: "costs", label: "成本", icon: "payments" },
  { key: "quality", label: "质量", icon: "tune" },
  { key: "alerts", label: "告警", icon: "notification_important" },
] as const;

type TabKey = (typeof TABS)[number]["key"];

function OverviewTab() {
  const [data, setData] = useState<OverviewData | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const d = await getOverview();
      setData(d);
    } catch (e) {
      message.error(e instanceof Error ? e.message : "加载观测数据失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (loading && !data) {
    return (
      <div className="kb-skeleton">
        {Array.from({ length: 4 }).map((_, i) => (
          <div className="kb-skeleton-row" style={{ height: 120 }} key={i} />
        ))}
      </div>
    );
  }

  const d = data ?? {
    today_requests: 0,
    today_success_rate: 1,
    today_errors: 0,
    today_tokens: { prompt: 0, completion: 0, total: 0 },
    today_latency_p50_ms: 0,
    today_latency_p95_ms: 0,
    active_sessions: 0,
    trend: [],
    tool_stats: [],
    recent_errors: [],
  };

  const trend = d.trend.map((t) => ({
    ...t,
    tsLabel: new Date(t.ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
  }));
  const successRate = Math.round(d.today_success_rate * 100);
  const toolStats = d.tool_stats.slice(0, 6);
  const totalCalls = toolStats.reduce((acc, t) => acc + t.calls, 0);

  return (
    <>
      <div className="obs-stat-grid">
        <article className="obs-stat-card">
          <div className="obs-stat-head">
            <div className="obs-stat-icon"><span className="material-symbols-rounded">call_received</span></div>
            <div className="obs-stat-label">今日请求</div>
          </div>
          <div className="obs-stat-value">{d.today_requests}</div>
          <div className="obs-stat-sub">活跃会话 {d.active_sessions} · 错误 {d.today_errors}</div>
        </article>
        <article className="obs-stat-card">
          <div className="obs-stat-head">
            <div className="obs-stat-icon"><span className="material-symbols-rounded">task_alt</span></div>
            <div className="obs-stat-label">今日成功率</div>
          </div>
          <div className="obs-stat-value">{successRate}%</div>
          <div className={`obs-stat-sub ${successRate >= 95 ? "ok" : successRate >= 80 ? "warn" : "danger"}`}>
            {successRate >= 95 ? "健康" : successRate >= 80 ? "需关注" : "异常"}
          </div>
        </article>
        <article className="obs-stat-card">
          <div className="obs-stat-head">
            <div className="obs-stat-icon"><span className="material-symbols-rounded">data_object</span></div>
            <div className="obs-stat-label">今日 Token</div>
          </div>
          <div className="obs-stat-value">{(d.today_tokens.total).toLocaleString()}</div>
          <div className="obs-stat-sub">prompt {(d.today_tokens.prompt).toLocaleString()} · completion {(d.today_tokens.completion).toLocaleString()}</div>
        </article>
        <article className="obs-stat-card">
          <div className="obs-stat-head">
            <div className="obs-stat-icon"><span className="material-symbols-rounded">timer</span></div>
            <div className="obs-stat-label">今日 P95 延迟</div>
          </div>
          <div className="obs-stat-value">{d.today_latency_p95_ms}ms</div>
          <div className="obs-stat-sub">P50 {d.today_latency_p50_ms}ms</div>
        </article>
      </div>

      <div className="obs-panel">
        <div className="obs-panel-title">
          <span className="material-symbols-rounded">show_chart</span>
          <b>近 24h 请求量与错误量</b>
        </div>
        <div className="obs-chart-wrap">
          <ResponsiveContainer width="100%" height="100%">
            <ComposedChart data={trend} margin={{ top: 8, right: 16, bottom: 0, left: -16 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#e6e8eb" />
              <XAxis dataKey="tsLabel" tick={{ fontSize: 11, fill: "#9297a7" }} interval={3} />
              <YAxis tick={{ fontSize: 11, fill: "#9297a7" }} allowDecimals={false} />
              <Tooltip
                contentStyle={{ borderRadius: 14, border: "1px solid #e6e8eb", boxShadow: "var(--shadow-md)" }}
                formatter={(value, name) => [value, name === "requests" ? "请求" : "错误"]}
              />
              <Legend formatter={(v) => (v === "requests" ? "请求" : "错误")} />
              <Bar dataKey="requests" fill="#0050cb" radius={[4, 4, 0, 0]} />
              <Line type="monotone" dataKey="errors" stroke="#ba1a1a" strokeWidth={2} dot={false} />
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      </div>

      <div className="obs-panel">
        <div className="obs-panel-title">
          <span className="material-symbols-rounded">data_usage</span>
          <b>近 24h Token 趋势</b>
        </div>
        <div className="obs-chart-wrap small">
          <ResponsiveContainer width="100%" height="100%">
            <ComposedChart data={trend} margin={{ top: 8, right: 16, bottom: 0, left: -10 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#e6e8eb" />
              <XAxis dataKey="tsLabel" tick={{ fontSize: 11, fill: "#9297a7" }} interval={3} />
              <YAxis tick={{ fontSize: 11, fill: "#9297a7" }} />
              <Tooltip contentStyle={{ borderRadius: 14, border: "1px solid #e6e8eb", boxShadow: "var(--shadow-md)" }} />
              <Legend />
              <Line type="monotone" dataKey="tokens_prompt" name="prompt" stroke="#0050cb" strokeWidth={2} dot={false} />
              <Line type="monotone" dataKey="tokens_completion" name="completion" stroke="#008a7e" strokeWidth={2} dot={false} />
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      </div>

      <div className="obs-panel">
        <div className="obs-panel-title">
          <span className="material-symbols-rounded">speed</span>
          <b>近 24h 延迟（P50 / P95）</b>
        </div>
        <div className="obs-chart-wrap small">
          <ResponsiveContainer width="100%" height="100%">
            <ComposedChart data={trend} margin={{ top: 8, right: 16, bottom: 0, left: -16 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#e6e8eb" />
              <XAxis dataKey="tsLabel" tick={{ fontSize: 11, fill: "#9297a7" }} interval={3} />
              <YAxis tick={{ fontSize: 11, fill: "#9297a7" }} />
              <Tooltip contentStyle={{ borderRadius: 14, border: "1px solid #e6e8eb", boxShadow: "var(--shadow-md)" }} />
              <Legend />
              <Line type="monotone" dataKey="latency_p50_ms" name="P50" stroke="#0050cb" strokeWidth={2} dot={false} />
              <Line type="monotone" dataKey="latency_p95_ms" name="P95" stroke="#e95400" strokeWidth={2} dot={false} />
            </ComposedChart>
          </ResponsiveContainer>
        </div>
      </div>

      <div className="obs-panel">
        <div className="obs-panel-title">
          <span className="material-symbols-rounded">widgets</span>
          <b>工具调用分布</b>
        </div>
        {toolStats.length === 0 ? (
          <Empty description="暂无工具调用数据" style={{ padding: 24 }} />
        ) : (
          <div className="obs-tool-dist">
            <div style={{ height: 220 }}>
              <ResponsiveContainer width="100%" height="100%">
                <PieChart>
                  <Pie
                    data={toolStats}
                    dataKey="calls"
                    nameKey="tool_name"
                    innerRadius={52}
                    outerRadius={84}
                    paddingAngle={2}
                    strokeWidth={2}
                  >
                    {toolStats.map((_, i) => (
                      <Cell key={i} fill={SPAN_COLORS[i % SPAN_COLORS.length]} />
                    ))}
                  </Pie>
                  <Tooltip
                    contentStyle={{ borderRadius: 14, border: "1px solid #e6e8eb", boxShadow: "var(--shadow-md)" }}
                    formatter={(value, name) => [value, name]}
                  />
                </PieChart>
              </ResponsiveContainer>
            </div>
            <div className="obs-tool-legend">
              {toolStats.map((t, i) => (
                <div className="obs-tool-legend-row" key={t.tool_name}>
                  <span className="obs-tool-dot" style={{ background: SPAN_COLORS[i % SPAN_COLORS.length] }} />
                  <b>{t.tool_name}</b>
                  <small>
                    {t.calls} 次 · 成功率 {t.calls ? Math.round((t.success / t.calls) * 100) : 0}% · 均 {t.avg_duration_ms}ms · 占比 {totalCalls ? Math.round((t.calls / totalCalls) * 100) : 0}%
                  </small>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>

      {d.recent_errors.length > 0 ? (
        <div className="obs-panel">
          <div className="obs-panel-title">
            <span className="material-symbols-rounded">error_outline</span>
            <b>最近异常 Trace</b>
            <span style={{ color: "var(--gray-400)", fontSize: 12 }}>点击进入运行记录查看完整链路</span>
          </div>
          <div className="kb-chunk-list">
            {d.recent_errors.slice(0, 5).map((t) => (
              <div className="obs-trace-row" key={t.trace_id}>
                <span className="obs-node-dot error" />
                <div className="obs-trace-main">
                  <div className="obs-trace-name">{t.trace_id.slice(0, 12)}… · {t.span_name}</div>
                  <div className="obs-trace-sub">
                    <small>{t.duration_ms}ms · {t.span_count} spans · {t.tool_count} tools</small>
                    {t.error ? <span className="obs-err-text">{t.error}</span> : null}
                  </div>
                </div>
                <Tag color="error" bordered={false}>{formatTime(t.started_at)}</Tag>
              </div>
            ))}
          </div>
        </div>
      ) : null}
    </>
  );
}

export default function ObservabilityPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const tabParam = searchParams.get("tab");
  const tab: TabKey = useMemo(() => {
    if (TABS.some((t) => t.key === tabParam)) return tabParam as TabKey;
    return "overview";
  }, [tabParam]);

  const switchTab = useCallback(
    (key: TabKey) => {
      const next = new URLSearchParams(searchParams);
      if (key === "overview") next.delete("tab");
      else next.set("tab", key);
      setSearchParams(next, { replace: true });
    },
    [searchParams, setSearchParams]
  );

  return (
    <div className="page-scroll obs-console">
      <header className="page-header">
        <div className="page-header-icon">
          <span className="material-symbols-rounded">monitoring</span>
        </div>
        <div className="page-header-copy">
          <h1>观测台</h1>
          <p>Agent 执行全链路：请求 / 成功率 / Token / 延迟 / 工具调用（本地采集，非 OpenLLMetry）</p>
        </div>
        <Button icon={<span className="material-symbols-rounded" style={{ fontSize: 16 }}>refresh</span>} onClick={() => window.location.reload()}>
          刷新
        </Button>
      </header>

      <nav className="obs-tab-strip" aria-label="观测台功能区">
        {TABS.map((item) => (
          <button
            key={item.key}
            type="button"
            className={`obs-tab-btn${tab === item.key ? " active" : ""}`}
            onClick={() => switchTab(item.key)}
          >
            <span className="material-symbols-rounded">{item.icon}</span>
            <span className="obs-tab-label">{item.label}</span>
          </button>
        ))}
      </nav>

      <div className="obs-console-body">
        {tab === "overview" ? <OverviewTab /> : null}
        {tab === "traces" ? <TraceListPage hideHeader /> : null}
        {tab === "calls" ? <CallsPage hideHeader /> : null}
        {tab === "logs" ? <LogsPage hideHeader /> : null}
        {tab === "costs" ? <CostsPage hideHeader /> : null}
        {tab === "quality" ? <QualityPage hideHeader /> : null}
        {tab === "alerts" ? <AlertsPage hideHeader /> : null}
      </div>
    </div>
  );
}