// 成本分析（OBSERVABILITY_SPEC §12.3 / §6.4）：总成本、按模型分组、价格版本
import { useCallback, useEffect, useState } from "react";
import { Empty, Select, Table, Tag, message } from "antd";
import ReactApexChart from "react-apexcharts";
import type { ApexAxisChartSeries, ApexOptions } from "apexcharts";
import { getCosts, getModelPrices } from "@/api/observability";
import { formatTime } from "@/utils/format";

type GroupBy = "none" | "model" | "user" | "environment";

export default function CostsPage({ hideHeader = false }: { hideHeader?: boolean } = {}) {
  const [groupBy, setGroupBy] = useState<GroupBy>("model");
  const [costData, setCostData] = useState<{
    total_cost: number;
    total_tokens: number;
    unconfigured_price_calls: number;
    groups: Record<string, { cost: number; tokens: number; input_tokens: number; output_tokens: number }>;
  } | null>(null);
  const [prices, setPrices] = useState<{ items: { id: number; model_provider: string | null; model_name: string; input_price_per_million: number; output_price_per_million: number; currency: string; effective_from: string | null; version: string | null }[] }>({ items: [] });
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [c, p] = await Promise.all([getCosts({ group_by: groupBy === "none" ? undefined : groupBy }), getModelPrices()]);
      setCostData(c);
      setPrices(p);
    } catch (e) {
      message.error(e instanceof Error ? e.message : "加载成本数据失败");
    } finally {
      setLoading(false);
    }
  }, [groupBy]);

  useEffect(() => {
    void load();
  }, [load]);

  const groups = costData ? Object.entries(costData.groups).map(([key, g]) => ({ key, ...g })) : [];
  const priceRows = prices.items.map((p) => ({
    key: p.id,
    model_name: p.model_name,
    model_provider: p.model_provider,
    input_price: p.input_price_per_million,
    output_price: p.output_price_per_million,
    currency: p.currency,
    effective_from: p.effective_from,
    version: p.version,
  }));
  const costSeries: ApexAxisChartSeries = [{ name: "成本", data: groups.map((group) => Number(group.cost.toFixed(6))) }];
  const costOptions: ApexOptions = {
    chart: { type: "bar", background: "transparent", fontFamily: 'Inter, "Noto Sans SC", "PingFang SC", sans-serif', toolbar: { show: false }, parentHeightOffset: 0 },
    colors: ["#2f7fe6"],
    dataLabels: { enabled: false },
    grid: { borderColor: "rgba(126, 166, 202, 0.22)", strokeDashArray: 4 },
    legend: { show: false },
    plotOptions: { bar: { horizontal: true, borderRadius: 6, borderRadiusApplication: "end", barHeight: "46%" } },
    xaxis: { categories: groups.map((group) => group.key), labels: { formatter: (value) => `$${Number(value).toFixed(4)}` } },
    yaxis: { labels: { maxWidth: 150, style: { fontSize: "11px", fontWeight: 600 } } },
    tooltip: { shared: false, intersect: true, y: { formatter: (value) => `$${value.toFixed(6)}` } },
  };

  return (
    <div className="page-scroll obs-tab-pane">
      {!hideHeader ? (
        <header className="page-header">
          <div className="page-header-icon">
            <span className="material-symbols-rounded">payments</span>
          </div>
          <div className="page-header-copy">
            <h1>成本分析</h1>
            <p>按价格版本计算的模型用量成本（不会把当前价格回填到历史调用）</p>
          </div>
        </header>
      ) : null}

      <div className="obs-stat-grid">
        <article className="obs-stat-card">
          <div className="obs-stat-head">
            <div className="obs-stat-icon"><span className="material-symbols-rounded">savings</span></div>
            <div className="obs-stat-label">近 7 天总成本</div>
          </div>
          <div className="obs-stat-value">${(costData?.total_cost ?? 0).toFixed(4)}</div>
        </article>
        <article className="obs-stat-card">
          <div className="obs-stat-head">
            <div className="obs-stat-icon"><span className="material-symbols-rounded">data_object</span></div>
            <div className="obs-stat-label">总 Token</div>
          </div>
          <div className="obs-stat-value">{(costData?.total_tokens ?? 0).toLocaleString()}</div>
        </article>
        <article className="obs-stat-card">
          <div className="obs-stat-head">
            <div className="obs-stat-icon"><span className="material-symbols-rounded">warning</span></div>
            <div className="obs-stat-label">未配置价格调用</div>
          </div>
          <div className="obs-stat-value">{costData?.unconfigured_price_calls ?? 0}</div>
        </article>
      </div>

      <div className="obs-panel">
        <div className="kb-panel-toolbar">
          <div className="kb-panel-title">
            <span className="material-symbols-rounded">pie_chart</span>
            <b>成本拆分</b>
          </div>
          <Select size="small" style={{ minWidth: 130 }} value={groupBy} onChange={setGroupBy} options={[
            { value: "model", label: "按模型" },
            { value: "user", label: "按用户" },
            { value: "none", label: "汇总" },
          ]} />
        </div>
        {loading && !costData ? (
          <div className="kb-skeleton">{Array.from({ length: 4 }).map((_, i) => <div className="kb-skeleton-row" style={{ height: 48 }} key={i} />)}</div>
        ) : groups.length === 0 ? (
          <Empty description="暂无成本数据" style={{ padding: 32 }} />
        ) : (
          <div className="cost-chart-layout">
            <div className="cost-chart"><ReactApexChart type="bar" height="100%" options={costOptions} series={costSeries} /></div>
            <div className="cost-group-list" aria-label="成本拆分文字摘要">
              {groups.map((g) => (
                <div className="cost-group-row" key={g.key}>
                  <div className="cost-group-name">{g.key}</div>
                  <div className="cost-group-val"><b>${g.cost.toFixed(4)}</b><small>{g.tokens.toLocaleString()} tokens · 输入 {g.input_tokens.toLocaleString()} / 输出 {g.output_tokens.toLocaleString()}</small></div>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>

      <div className="obs-panel">
        <div className="kb-panel-title">
          <span className="material-symbols-rounded">price_change</span>
          <b>模型价格版本</b>
        </div>
        {priceRows.length === 0 ? (
          <Empty description="暂无价格配置。可通过 API POST /observability/model-prices 添加。" style={{ padding: 32 }} />
        ) : (
          <Table
            size="small"
            rowKey="key"
            dataSource={priceRows}
            pagination={false}
            columns={[
              { title: "模型", dataIndex: "model_name", key: "model_name" },
              { title: "Provider", dataIndex: "model_provider", key: "model_provider", render: (v: string | null) => v || "—" },
              { title: "输入价($/M)", dataIndex: "input_price", key: "input_price" },
              { title: "输出价($/M)", dataIndex: "output_price", key: "output_price" },
              { title: "币种", dataIndex: "currency", key: "currency", width: 80 },
              { title: "生效时间", dataIndex: "effective_from", key: "effective_from", render: (v: string | null) => (v ? formatTime(v) : "—") },
              { title: "版本", dataIndex: "version", key: "version", render: (v: string | null) => v ? <Tag bordered={false}>{v}</Tag> : "—" },
            ]}
          />
        )}
      </div>
    </div>
  );
}
