// 调用分析（OBSERVABILITY_SPEC §12.3）：LLM / 工具 / 检索 / Embedding / Memory 调用
import { useCallback, useEffect, useState } from "react";
import { Empty, Input, Select, Table, Tag, message } from "antd";
import { useSearchParams } from "react-router-dom";
import { getCalls } from "@/api/observability";
import type { CallItem } from "@/api/observability";
import { formatTime } from "@/utils/format";

const TYPE_OPTIONS = [
  { value: "", label: "全部类型" },
  { value: "llm", label: "LLM" },
  { value: "tool", label: "工具" },
  { value: "retriever", label: "检索" },
  { value: "embedding", label: "Embedding" },
  { value: "memory", label: "记忆" },
  { value: "node", label: "节点" },
];

const STATUS_COLOR: Record<string, string> = {
  ok: "success",
  error: "error",
  timeout: "warning",
  interrupted: "default",
  canceled: "default",
};

export default function CallsPage({ hideHeader = false }: { hideHeader?: boolean } = {}) {
  const [searchParams] = useSearchParams();
  const initialType = searchParams.get("span_type") || searchParams.get("type") || "";
  const [spanType, setSpanType] = useState<string>(initialType);
  const [status, setStatus] = useState<string>("");
  const [keyword, setKeyword] = useState("");
  const [page, setPage] = useState(1);
  const [data, setData] = useState<{ items: CallItem[]; total: number; page: number; page_size: number }>({
    items: [],
    total: 0,
    page: 1,
    page_size: 20,
  });
  const [loading, setLoading] = useState(false);

  const load = useCallback(async (p: number, st: string, k: string, ty: string) => {
    setLoading(true);
    try {
      const d = await getCalls({
        page: p,
        page_size: 20,
        span_type: ty || undefined,
        status: st || undefined,
        model: k || undefined,
        tool_name: k || undefined,
      });
      setData(d);
    } catch (e) {
      message.error(e instanceof Error ? e.message : "加载调用数据失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(page, status, keyword, spanType);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page]);

  const applyFilter = (p = 1) => {
    setPage(p);
    void load(p, status, keyword, spanType);
  };

  const columns = [
    {
      title: "类型",
      dataIndex: "span_type",
      key: "span_type",
      width: 110,
      render: (v: string) => <Tag bordered={false}>{v}</Tag>,
    },
    {
      title: "调用",
      dataIndex: "span_name",
      key: "span_name",
      ellipsis: true,
    },
    {
      title: "模型",
      dataIndex: "model",
      key: "model",
      width: 160,
      render: (v: string | null, r: CallItem) => v || r.model_name_resolved || "—",
    },
    {
      title: "Token",
      dataIndex: "token_usage",
      key: "token_usage",
      width: 120,
      render: (v: Record<string, number> | null) =>
        v ? (v.total ?? 0).toLocaleString() : "—",
    },
    {
      title: "TTFT",
      dataIndex: "ttft_ms",
      key: "ttft_ms",
      width: 80,
      render: (v: number | null) => (v != null ? `${v}ms` : "—"),
    },
    {
      title: "成本",
      dataIndex: "cost_total",
      key: "cost_total",
      width: 100,
      render: (v: number | null) => (v != null ? `$${v.toFixed(6)}` : "—"),
    },
    {
      title: "耗时",
      dataIndex: "duration_ms",
      key: "duration_ms",
      width: 90,
      render: (v: number) => `${v}ms`,
    },
    {
      title: "状态",
      dataIndex: "status",
      key: "status",
      width: 90,
      render: (v: string) => <Tag color={STATUS_COLOR[v] || "default"}>{v}</Tag>,
    },
    {
      title: "时间",
      dataIndex: "started_at",
      key: "started_at",
      width: 160,
      render: (v: string | null) => (v ? formatTime(v) : "—"),
    },
    {
      title: "Trace",
      dataIndex: "trace_id",
      key: "trace_id",
      width: 130,
      ellipsis: true,
      render: (v: string) => <span style={{ fontFamily: "monospace", fontSize: 12 }}>{v.slice(0, 12)}…</span>,
    },
  ];

  return (
    <div className="page-scroll obs-tab-pane">
      {!hideHeader ? (
        <header className="page-header">
          <div className="page-header-icon">
            <span className="material-symbols-rounded">hub</span>
          </div>
          <div className="page-header-copy">
            <h1>调用分析</h1>
            <p>LLM、工具、检索、Embedding 与记忆调用明细：模型、Token、TTFT、成本与状态</p>
          </div>
        </header>
      ) : null}

      <div className="obs-panel">
        <div className="kb-panel-toolbar">
          <div className="kb-panel-title">
            <span className="material-symbols-rounded">deployed_code</span>
            <b>调用列表</b>
            <Tag bordered={false}>{data.total} 条</Tag>
          </div>
          <div className="obs-filters">
            <Select size="small" style={{ minWidth: 120 }} value={spanType} onChange={(v) => { setSpanType(v); applyFilter(1); }} options={TYPE_OPTIONS} />
            <Select size="small" style={{ minWidth: 110 }} value={status} onChange={(v) => { setStatus(v); applyFilter(1); }} options={[
              { value: "", label: "全部状态" },
              { value: "ok", label: "成功" },
              { value: "error", label: "错误" },
              { value: "timeout", label: "超时" },
            ]} />
            <Input size="small" allowClear prefix={<span className="material-symbols-rounded" style={{ fontSize: 14 }}>search</span>} placeholder="模型 / 工具名…" style={{ width: 200 }} value={keyword} onChange={(e) => { setKeyword(e.target.value); applyFilter(1); }} />
          </div>
        </div>
        {data.items.length === 0 && !loading ? (
          <Empty description="暂无调用数据。发起一次对话后，这里会展示所有 LLM/工具调用。" style={{ padding: 40 }} />
        ) : (
          <Table
            rowKey="span_id"
            size="small"
            loading={loading}
            columns={columns}
            dataSource={data.items}
            pagination={{
              current: page,
              total: data.total,
              pageSize: data.page_size,
              onChange: setPage,
              showSizeChanger: false,
            }}
          />
        )}
      </div>
    </div>
  );
}
