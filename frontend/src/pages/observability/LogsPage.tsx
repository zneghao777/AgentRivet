// 结构化日志（OBSERVABILITY_SPEC §8.1 / §12.3）：搜索并关联 Trace
import { useCallback, useEffect, useState } from "react";
import { Empty, Input, Select, Table, Tag, message } from "antd";
import { useNavigate } from "react-router-dom";
import { getLogs } from "@/api/observability";
import type { LogItem } from "@/api/observability";
import { formatTime } from "@/utils/format";

const SEVERITY_COLOR: Record<string, string> = {
  DEBUG: "default",
  INFO: "blue",
  WARN: "gold",
  ERROR: "red",
  FATAL: "magenta",
};

export default function LogsPage({ hideHeader = false }: { hideHeader?: boolean } = {}) {
  const navigate = useNavigate();
  const [severity, setSeverity] = useState<string>("");
  const [keyword, setKeyword] = useState("");
  const [data, setData] = useState<{ items: LogItem[]; next_cursor: number | null }>({ items: [], next_cursor: null });
  const [loading, setLoading] = useState(false);
  const [limit, setLimit] = useState(50);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const d = await getLogs({ severity: severity || undefined, keyword: keyword || undefined, limit });
      setData(d);
    } catch (e) {
      message.error(e instanceof Error ? e.message : "加载日志失败");
    } finally {
      setLoading(false);
    }
  }, [severity, keyword, limit]);

  useEffect(() => {
    void load();
  }, [load]);

  const columns = [
    {
      title: "级别",
      dataIndex: "severity_text",
      key: "severity_text",
      width: 80,
      render: (v: string) => <Tag color={SEVERITY_COLOR[v] || "default"} bordered={false}>{v}</Tag>,
    },
    {
      title: "时间",
      dataIndex: "timestamp",
      key: "timestamp",
      width: 170,
      render: (v: string | null) => (v ? formatTime(v) : "—"),
    },
    {
      title: "来源",
      key: "source",
      width: 200,
      render: (_: unknown, r: LogItem) => (
        <span className="log-source">
          {r.logger_name || "root"}
          {r.module ? <small> · {r.module}</small> : null}
        </span>
      ),
    },
    {
      title: "消息",
      dataIndex: "body",
      key: "body",
      ellipsis: true,
      render: (v: string | null) => v || "—",
    },
    {
      title: "异常",
      key: "exception",
      width: 180,
      render: (_: unknown, r: LogItem) =>
        r.exception_type ? (
          <Tag color="red" bordered={false}>
            {r.exception_type}
            {r.exception_message ? `: ${String(r.exception_message).slice(0, 40)}` : ""}
          </Tag>
        ) : (
          "—"
        ),
    },
    {
      title: "Trace",
      dataIndex: "trace_id",
      key: "trace_id",
      width: 130,
      render: (v: string | null) =>
        v ? (
          <a onClick={() => navigate(`/observability/traces/${v}`)} style={{ fontFamily: "monospace", fontSize: 12 }}>
            {v.slice(0, 12)}…
          </a>
        ) : (
          "—"
        ),
    },
  ];

  return (
    <div className="page-scroll obs-tab-pane">
      {!hideHeader ? (
        <header className="page-header">
          <div className="page-header-icon">
            <span className="material-symbols-rounded">terminal</span>
          </div>
          <div className="page-header-copy">
            <h1>结构化日志</h1>
            <p>应用日志自动关联 Trace / Span / Request，可从错误日志下钻到调用链</p>
          </div>
        </header>
      ) : null}

      <div className="obs-panel">
        <div className="kb-panel-toolbar">
          <div className="kb-panel-title">
            <span className="material-symbols-rounded">notes</span>
            <b>日志流</b>
          </div>
          <div className="obs-filters">
            <Select size="small" style={{ minWidth: 110 }} value={severity} onChange={setSeverity} options={[
              { value: "", label: "全部级别" },
              { value: "DEBUG", label: "DEBUG" },
              { value: "INFO", label: "INFO" },
              { value: "WARN", label: "WARN" },
              { value: "ERROR", label: "ERROR" },
              { value: "FATAL", label: "FATAL" },
            ]} />
            <Select size="small" style={{ minWidth: 110 }} value={limit} onChange={(v) => setLimit(v)} options={[
              { value: 50, label: "50 条" },
              { value: 100, label: "100 条" },
              { value: 200, label: "200 条" },
            ]} />
            <Input size="small" allowClear prefix={<span className="material-symbols-rounded" style={{ fontSize: 14 }}>search</span>} placeholder="关键字 / 模块…" style={{ width: 220 }} value={keyword} onChange={(e) => setKeyword(e.target.value)} />
          </div>
        </div>
        {data.items.length === 0 && !loading ? (
          <Empty description="暂无日志。WARN/ERROR 及以上级别会自动采集并关联 Trace。" style={{ padding: 40 }} />
        ) : (
          <Table rowKey="id" size="small" loading={loading} columns={columns} dataSource={data.items} pagination={false} />
        )}
      </div>
    </div>
  );
}
