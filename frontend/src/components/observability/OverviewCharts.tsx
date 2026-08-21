import ReactApexChart from "react-apexcharts";
import type { ApexAxisChartSeries, ApexOptions } from "apexcharts";
import type { OverviewData, TrendPoint } from "@/types/observability";
import { GlassEmptyState } from "@/components/common/Glass";

const COLORS = {
  blue: "#2f7fe6",
  blueDark: "#1d62c3",
  cyan: "#1597a5",
  amber: "#d98b27",
  red: "#d9505d",
  grid: "rgba(126, 166, 202, 0.22)",
  muted: "#60778e",
};

function xy(data: TrendPoint[], key: keyof TrendPoint) {
  return data.map((point) => ({
    x: new Date(point.ts).getTime(),
    y: Number(point[key]) || 0,
  }));
}

function baseOptions(id: string): ApexOptions {
  const reducedMotion = typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  return {
    chart: {
      id,
      background: "transparent",
      fontFamily: 'Inter, "Noto Sans SC", "PingFang SC", sans-serif',
      foreColor: COLORS.muted,
      animations: { enabled: !reducedMotion, speed: 420, dynamicAnimation: { speed: 320 } },
      toolbar: { show: false },
      zoom: { enabled: false },
      parentHeightOffset: 0,
    },
    colors: [COLORS.blue, COLORS.red, COLORS.cyan, COLORS.amber],
    dataLabels: { enabled: false },
    grid: {
      borderColor: COLORS.grid,
      strokeDashArray: 4,
      padding: { left: 4, right: 12, top: 0, bottom: 0 },
    },
    legend: {
      position: "top",
      horizontalAlign: "left",
      fontSize: "12px",
      markers: { size: 5, shape: "circle" },
      itemMargin: { horizontal: 10, vertical: 4 },
    },
    tooltip: {
      shared: true,
      intersect: false,
      theme: "light",
      x: { format: "MM/dd HH:mm" },
    },
    xaxis: {
      type: "datetime",
      axisBorder: { show: false },
      axisTicks: { show: false },
      labels: { datetimeUTC: false, style: { fontSize: "11px" } },
      tooltip: { enabled: false },
    },
    yaxis: {
      min: 0,
      forceNiceScale: true,
      labels: { formatter: (value) => Math.round(value).toLocaleString(), style: { fontSize: "11px" } },
    },
    stroke: { width: 2.5, curve: "monotoneCubic", lineCap: "round" },
    markers: { size: 0, hover: { sizeOffset: 3 } },
    noData: { text: "暂无趋势数据", align: "center", verticalAlign: "middle" },
    responsive: [
      { breakpoint: 560, options: { legend: { position: "bottom" }, xaxis: { labels: { show: false } } } },
      { breakpoint: 1024, options: { chart: { height: 260 } } },
    ],
  };
}

function ChartPanel({
  icon,
  title,
  description,
  children,
}: {
  icon: string;
  title: string;
  description: string;
  children: React.ReactNode;
}) {
  return (
    <section className="obs-panel obs-chart-panel" aria-label={title}>
      <header className="obs-panel-title">
        <span className="obs-chart-icon material-symbols-rounded" aria-hidden="true">{icon}</span>
        <div className="obs-chart-heading"><b>{title}</b><small>{description}</small></div>
      </header>
      {children}
    </section>
  );
}

export default function OverviewCharts({ data }: { data: OverviewData }) {
  const trend = data.trend;
  const hasRequestTrend = trend.some((point) => point.requests > 0 || point.errors > 0);
  const hasTokenTrend = trend.some((point) => point.tokens_prompt > 0 || point.tokens_completion > 0);
  const hasLatencyTrend = trend.some((point) => point.latency_p50_ms > 0 || point.latency_p95_ms > 0);
  const sparse = trend.length <= 1;

  const requestSeries: ApexAxisChartSeries = [
    { name: "请求", type: "column", data: xy(trend, "requests") },
    { name: "错误", type: "line", data: xy(trend, "errors") },
  ];
  const requestOptions: ApexOptions = {
    ...baseOptions("overview-requests"),
    colors: [COLORS.blue, COLORS.red],
    plotOptions: { bar: { borderRadius: 6, borderRadiusApplication: "end", columnWidth: "44%" } },
    stroke: { width: [0, 2.5], curve: sparse ? "straight" : "monotoneCubic" },
    markers: { size: sparse ? [0, 5] : 0 },
  };

  const tokenSeries: ApexAxisChartSeries = [
    { name: "输入 Token", data: xy(trend, "tokens_prompt") },
    { name: "输出 Token", data: xy(trend, "tokens_completion") },
  ];
  const tokenOptions: ApexOptions = {
    ...baseOptions("overview-tokens"),
    chart: { ...baseOptions("overview-tokens").chart, type: "area", stacked: true },
    colors: [COLORS.blue, COLORS.cyan],
    stroke: { width: 2, curve: sparse ? "straight" : "monotoneCubic" },
    fill: { type: "gradient", gradient: { opacityFrom: 0.42, opacityTo: 0.08, stops: [0, 92, 100] } },
    markers: { size: sparse ? 4 : 0 },
  };

  const latencySeries: ApexAxisChartSeries = [
    { name: "P50 延迟", data: xy(trend, "latency_p50_ms") },
    { name: "P95 延迟", data: xy(trend, "latency_p95_ms") },
  ];
  const latencyOptions: ApexOptions = {
    ...baseOptions("overview-latency"),
    colors: [COLORS.blueDark, COLORS.amber],
    stroke: { width: [2.5, 2.5], curve: sparse ? "straight" : "monotoneCubic", dashArray: [0, 5] },
    markers: { size: sparse ? 4 : 0 },
    yaxis: {
      min: 0,
      forceNiceScale: true,
      labels: { formatter: (value) => `${Math.round(value)}ms`, style: { fontSize: "11px" } },
    },
    tooltip: { ...baseOptions("overview-latency").tooltip, y: { formatter: (value) => `${Math.round(value)}ms` } },
  };

  const tools = data.tool_stats.slice(0, 8);
  const toolSeries: ApexAxisChartSeries = [{ name: "调用次数", data: tools.map((tool) => tool.calls) }];
  const toolOptions: ApexOptions = {
    ...baseOptions("overview-tools"),
    chart: { ...baseOptions("overview-tools").chart, type: "bar" },
    colors: [COLORS.blue],
    xaxis: {
      categories: tools.map((tool) => tool.tool_name),
      labels: { formatter: (value) => Math.round(Number(value)).toString(), style: { fontSize: "11px" } },
    },
    yaxis: { labels: { maxWidth: 132, style: { fontSize: "11px", fontWeight: 600 } } },
    plotOptions: { bar: { horizontal: true, borderRadius: 6, borderRadiusApplication: "end", barHeight: "48%" } },
    grid: { ...baseOptions("overview-tools").grid, padding: { left: 8, right: 16, top: 0, bottom: 0 } },
    legend: { show: false },
    tooltip: { shared: false, intersect: true, y: { formatter: (value) => `${value} 次` } },
  };

  return (
    <div className="obs-dashboard-grid">
      <ChartPanel icon="show_chart" title="近 24 小时请求与错误" description="柱形为请求量，折线为错误量">
        {hasRequestTrend ? <div className="obs-chart-wrap"><ReactApexChart options={requestOptions} series={requestSeries} type="line" height="100%" width="100%" /></div> : <GlassEmptyState icon="query_stats" title="小时聚合尚未生成" description={`今日已有 ${data.today_requests} 次请求、${data.today_errors} 次错误；聚合任务完成后将显示趋势。`} />}
      </ChartPanel>
      <ChartPanel icon="data_usage" title="Token 趋势" description={`今日输入 ${data.today_tokens.prompt.toLocaleString()}，输出 ${data.today_tokens.completion.toLocaleString()}`}>
        {hasTokenTrend ? <div className="obs-chart-wrap"><ReactApexChart options={tokenOptions} series={tokenSeries} type="area" height="100%" width="100%" /></div> : <GlassEmptyState icon="data_usage" title="Token 聚合尚未生成" description={`今日累计 ${data.today_tokens.total.toLocaleString()} Token，当前保留总量指标而不绘制零值趋势。`} />}
      </ChartPanel>
      <ChartPanel icon="speed" title="延迟分位" description={`P50 ${data.today_latency_p50_ms}ms · P95 ${data.today_latency_p95_ms}ms`}>
        {hasLatencyTrend ? <div className="obs-chart-wrap"><ReactApexChart options={latencyOptions} series={latencySeries} type="line" height="100%" width="100%" /></div> : <GlassEmptyState icon="speed" title="延迟聚合尚未生成" description={`当前 P50 ${data.today_latency_p50_ms}ms、P95 ${data.today_latency_p95_ms}ms；样本不足时不绘制误导曲线。`} />}
      </ChartPanel>
      <ChartPanel icon="widgets" title="工具调用分布" description="按调用次数排序，最多显示 8 个工具">
        {tools.length ? (
          <>
            <div className="obs-chart-wrap obs-tool-chart"><ReactApexChart options={toolOptions} series={toolSeries} type="bar" height="100%" width="100%" /></div>
            <div className="chart-accessible-summary" aria-label="工具调用文字摘要">
              {tools.map((tool) => <span key={tool.tool_name}>{tool.tool_name}：{tool.calls} 次，成功率 {tool.calls ? Math.round(tool.success / tool.calls * 100) : 0}%</span>)}
            </div>
          </>
        ) : <GlassEmptyState icon="widgets" title="暂无工具调用" description="使用工具后会在这里显示调用次数、成功率和平均耗时。" />}
      </ChartPanel>
    </div>
  );
}
