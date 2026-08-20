// 告警与规则（OBSERVABILITY_SPEC §10.2 / §12.3）：默认 SLO + 告警事件 + 采集健康
import { useCallback, useEffect, useState } from "react";
import { Empty, Table, Tag, message } from "antd";
import { getAlerts, getCollectorHealth, getIncidents } from "@/api/observability";
import type { AlertRuleItem, IncidentItem } from "@/api/observability";
import { formatTime } from "@/utils/format";

const INCIDENT_COLOR: Record<string, string> = {
  pending: "default",
  firing: "red",
  acknowledged: "gold",
  resolved: "success",
};

export default function AlertsPage({ hideHeader = false }: { hideHeader?: boolean } = {}) {
  const [rules, setRules] = useState<AlertRuleItem[]>([]);
  const [incidents, setIncidents] = useState<IncidentItem[]>([]);
  const [health, setHealth] = useState<{ spans_dropped_total: number; queue_depth: number; active_sessions: number; latest_aggregation: string | null; data_lag_seconds: number | null } | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [r, i, h] = await Promise.all([getAlerts(), getIncidents(), getCollectorHealth()]);
      setRules(r.items);
      setIncidents(i.items);
      setHealth(h);
    } catch (e) {
      message.error(e instanceof Error ? e.message : "加载告警数据失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const ruleRows = rules.map((r) => ({
    key: r.id,
    name: r.name,
    metric: r.metric,
    threshold: r.threshold,
    comparison: r.comparison,
    window: r.window_minutes,
    min_samples: r.min_samples,
    enabled: r.enabled,
  }));

  const incidentRows = incidents.map((i) => ({
    key: i.id,
    status: i.status,
    metric: i.metric,
    current_value: i.current_value,
    threshold: i.threshold,
    message: i.message,
    created_at: i.created_at,
    resolved_at: i.resolved_at,
  }));

  return (
    <div className="page-scroll obs-tab-pane">
      {!hideHeader ? (
        <header className="page-header">
          <div className="page-header-icon">
            <span className="material-symbols-rounded">notification_important</span>
          </div>
          <div className="page-header-copy">
            <h1>告警与采集健康</h1>
            <p>SLO 规则自动评估触发/确认/恢复；采集器队列、丢弃数与数据新鲜度监控</p>
          </div>
        </header>
      ) : null}

      <div className="obs-stat-grid">
        <article className="obs-stat-card">
          <div className="obs-stat-head">
            <div className="obs-stat-icon"><span className="material-symbols-rounded">notifications_active</span></div>
            <div className="obs-stat-label">活动告警</div>
          </div>
          <div className="obs-stat-value">{incidents.filter((i) => i.status !== "resolved").length}</div>
          <div className="obs-stat-sub">已恢复 {incidents.filter((i) => i.status === "resolved").length}</div>
        </article>
        <article className="obs-stat-card">
          <div className="obs-stat-head">
            <div className="obs-stat-icon"><span className="material-symbols-rounded">rule</span></div>
            <div className="obs-stat-label">规则</div>
          </div>
          <div className="obs-stat-value">{rules.length}</div>
          <div className="obs-stat-sub">启用 {rules.filter((r) => r.enabled).length}</div>
        </article>
        <article className="obs-stat-card">
          <div className="obs-stat-head">
            <div className="obs-stat-icon"><span className="material-symbols-rounded">storage</span></div>
            <div className="obs-stat-label">采集队列</div>
          </div>
          <div className="obs-stat-value">{health?.queue_depth ?? 0}</div>
          <div className="obs-stat-sub">丢弃 {health?.spans_dropped_total ?? 0} · 活跃会话 {health?.active_sessions ?? 0}</div>
        </article>
        <article className="obs-stat-card">
          <div className="obs-stat-head">
            <div className="obs-stat-icon"><span className="material-symbols-rounded">update</span></div>
            <div className="obs-stat-label">数据新鲜度</div>
          </div>
          <div className="obs-stat-value">
            {health?.data_lag_seconds != null ? `${Math.round(health.data_lag_seconds)}s` : "—"}
          </div>
          <div className="obs-stat-sub">最近聚合 {health?.latest_aggregation ? formatTime(health.latest_aggregation) : "—"}</div>
        </article>
      </div>

      <div className="obs-panel">
        <div className="kb-panel-toolbar">
          <div className="kb-panel-title">
            <span className="material-symbols-rounded">rule</span>
            <b>SLO 规则</b>
          </div>
        </div>
        {ruleRows.length === 0 && !loading ? (
          <Empty description="暂无规则。启动后会自动注入默认 SLO（错误率/P95/TTFT/工具错误率/队列使用率）。" style={{ padding: 32 }} />
        ) : (
          <Table
            size="small"
            loading={loading}
            rowKey="key"
            dataSource={ruleRows}
            pagination={false}
            columns={[
              { title: "规则", dataIndex: "name", key: "name" },
              { title: "指标", dataIndex: "metric", key: "metric" },
              { title: "阈值", key: "threshold", render: (_: unknown, r: { threshold: number; comparison: string }) => `${r.comparison} ${r.threshold}` },
              { title: "窗口", dataIndex: "window", key: "window", render: (v: number) => `${v} 分钟` },
              { title: "最小样本", dataIndex: "min_samples", key: "min_samples" },
              { title: "状态", dataIndex: "enabled", key: "enabled", render: (v: boolean) => <Tag color={v ? "success" : "default"}>{v ? "启用" : "停用"}</Tag> },
            ]}
          />
        )}
      </div>

      <div className="obs-panel">
        <div className="kb-panel-toolbar">
          <div className="kb-panel-title">
            <span className="material-symbols-rounded">notification_important</span>
            <b>告警事件</b>
          </div>
        </div>
        {incidentRows.length === 0 && !loading ? (
          <Empty description="暂无告警事件。SLO 被突破时自动触发。" style={{ padding: 32 }} />
        ) : (
          <Table
            size="small"
            loading={loading}
            rowKey="key"
            dataSource={incidentRows}
            pagination={false}
            columns={[
              { title: "状态", dataIndex: "status", key: "status", width: 110, render: (v: string) => <Tag color={INCIDENT_COLOR[v] || "default"}>{v}</Tag> },
              { title: "指标", dataIndex: "metric", key: "metric", width: 130 },
              { title: "当前值 / 阈值", key: "value", width: 150, render: (_: unknown, r: { current_value: number; threshold: number }) => `${r.current_value.toFixed(3)} / ${r.threshold}` },
              { title: "消息", dataIndex: "message", key: "message", ellipsis: true },
              { title: "触发时间", dataIndex: "created_at", key: "created_at", width: 160, render: (v: string | null) => (v ? formatTime(v) : "—") },
            ]}
          />
        )}
      </div>
    </div>
  );
}
