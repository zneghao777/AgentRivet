// 观测台（OBSERVABILITY_SPEC §3）：统一入口，顶部水平 tab 切换各功能区
// 总览 + 运行记录 + 调用分析 + 日志 + 成本 + 质量 + 告警
import { lazy, Suspense, useCallback, useEffect, useMemo, useState, type KeyboardEvent } from "react";
import { Button, Tag, message } from "antd";
import { useSearchParams } from "react-router-dom";
import { getOverview } from "@/api/observability";
import type { OverviewData } from "@/types/observability";
import { formatTime } from "@/utils/format";
import { MetricStrip, WorkspaceHeader } from "@/components/common/Workspace";
import { GlassEmptyState } from "@/components/common/Glass";

const OverviewCharts = lazy(() => import("@/components/observability/OverviewCharts"));
const TraceListPage = lazy(() => import("./TraceListPage"));
const CallsPage = lazy(() => import("./CallsPage"));
const LogsPage = lazy(() => import("./LogsPage"));
const CostsPage = lazy(() => import("./CostsPage"));
const QualityPage = lazy(() => import("./QualityPage"));
const AlertsPage = lazy(() => import("./AlertsPage"));

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

  const successRate = Math.round(d.today_success_rate * 100);

  return (
    <>
      <MetricStrip items={[
        { label: "今日请求", value: d.today_requests, meta: `活跃会话 ${d.active_sessions} · 错误 ${d.today_errors}` },
        { label: "今日成功率", value: `${successRate}%`, meta: successRate >= 95 ? "健康" : successRate >= 80 ? "需关注" : "异常", tone: successRate >= 95 ? "success" : successRate >= 80 ? "warning" : "danger" },
        { label: "今日 Token", value: d.today_tokens.total.toLocaleString(), meta: `输入 ${d.today_tokens.prompt.toLocaleString()} · 输出 ${d.today_tokens.completion.toLocaleString()}`, tone: "teal" },
        { label: "P95 延迟", value: `${d.today_latency_p95_ms}ms`, meta: `P50 ${d.today_latency_p50_ms}ms`, tone: "warning" },
      ]} />

      <Suspense fallback={<div className="obs-chart-loading"><GlassEmptyState icon="progress_activity" title="正在加载图表" description="观测指标已就绪，正在准备交互图表…" /></div>}>
        <OverviewCharts data={d} />
      </Suspense>

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

  const onTabKeyDown = (event: KeyboardEvent<HTMLButtonElement>, current: TabKey) => {
    const index = TABS.findIndex((item) => item.key === current);
    let nextIndex = index;
    if (event.key === "ArrowRight") nextIndex = (index + 1) % TABS.length;
    else if (event.key === "ArrowLeft") nextIndex = (index - 1 + TABS.length) % TABS.length;
    else if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = TABS.length - 1;
    else return;
    event.preventDefault();
    const next = TABS[nextIndex].key;
    switchTab(next);
    window.requestAnimationFrame(() => document.getElementById(`obs-tab-${next}`)?.focus());
  };

  return (
    <div className="page-scroll obs-console">
      <WorkspaceHeader icon="monitoring" title="观测台" description="Agent 执行全链路：请求、成功率、Token、延迟与工具调用（本地采集）" actions={<Button icon={<span className="material-symbols-rounded">refresh</span>} onClick={() => window.location.reload()}>刷新</Button>} />

      <nav className="obs-tab-strip" aria-label="观测台功能区" role="tablist">
        {TABS.map((item) => (
          <button
            key={item.key}
            type="button"
            id={`obs-tab-${item.key}`}
            role="tab"
            aria-selected={tab === item.key}
            aria-controls="obs-tab-panel"
            tabIndex={tab === item.key ? 0 : -1}
            className={`obs-tab-btn${tab === item.key ? " active" : ""}`}
            onClick={() => switchTab(item.key)}
            onKeyDown={(event) => onTabKeyDown(event, item.key)}
          >
            <span className="material-symbols-rounded">{item.icon}</span>
            <span className="obs-tab-label">{item.label}</span>
          </button>
        ))}
      </nav>

      <div className="obs-console-body" id="obs-tab-panel" role="tabpanel" aria-labelledby={`obs-tab-${tab}`}>
        <Suspense fallback={<div className="obs-tab-loading"><GlassEmptyState icon="progress_activity" title="正在加载功能区" /></div>}>
          {tab === "overview" ? <OverviewTab /> : null}
          {tab === "traces" ? <TraceListPage hideHeader /> : null}
          {tab === "calls" ? <CallsPage hideHeader /> : null}
          {tab === "logs" ? <LogsPage hideHeader /> : null}
          {tab === "costs" ? <CostsPage hideHeader /> : null}
          {tab === "quality" ? <QualityPage hideHeader /> : null}
          {tab === "alerts" ? <AlertsPage hideHeader /> : null}
        </Suspense>
      </div>
    </div>
  );
}
