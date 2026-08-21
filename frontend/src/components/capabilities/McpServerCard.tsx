import { Button, Dropdown, Switch } from "antd";
import type { MenuProps } from "antd";
import type { McpServerItem } from "@/types/api";

const HEALTH_LABEL: Record<McpServerItem["health_status"], string> = {
  unknown: "未检查", checking: "检查中", ready: "正常", degraded: "部分可用",
  error: "连接异常", disabled: "已停用",
};

export default function McpServerCard({ server, busy, onInspect, onTest, onToggle, onDelete }: {
  server: McpServerItem;
  busy: boolean;
  onInspect: () => void;
  onTest: () => void;
  onToggle: () => void;
  onDelete: () => void;
}) {
  const menu: MenuProps = { items: [
    { key: "test", label: "测试连接", onClick: onTest },
    { key: "detail", label: "查看工具", onClick: onInspect },
    { type: "divider" },
    { key: "delete", danger: true, label: "删除 Server", onClick: onDelete },
  ] };
  return <article className={`capability-card mcp-card health-${server.health_status}`}>
    <header className="capability-card-head">
      <span className="capability-card-icon material-symbols-rounded">hub</span>
      <button type="button" className="capability-card-title" onClick={onInspect}><b>{server.name}</b><small>Streamable HTTP</small></button>
      <Dropdown menu={menu} trigger={["click"]}><Button type="text" aria-label={`${server.name} 更多操作`} icon={<span className="material-symbols-rounded">more_horiz</span>} /></Dropdown>
    </header>
    <p className="capability-card-description">{server.description || server.endpoint_url}</p>
    <div className="capability-card-status"><span className={`status-dot status-${server.health_status === "ready" ? "success" : server.health_status === "error" ? "danger" : server.health_status === "degraded" ? "warning" : "neutral"}`}><i />{HEALTH_LABEL[server.health_status]}</span>{server.pending_tool_count ? <span className="capability-review-chip">{server.pending_tool_count} 待审查</span> : null}</div>
    <div className="capability-card-metrics"><span><b>{server.tool_count}</b>已发现</span><span><b>{server.enabled_tool_count}</b>已启用</span><span><b>{server.protocol_version || "—"}</b>协议</span></div>
    {server.last_error_message ? <div className="capability-inline-error" title={server.last_error_message}>{server.last_error_message}</div> : null}
    <footer className="capability-card-footer"><small>{server.last_checked_at ? `检查于 ${new Date(server.last_checked_at).toLocaleString()}` : "尚未测试连接"}</small><Button size="small" onClick={onTest} loading={busy}>测试</Button><Switch checked={server.enabled} loading={busy} onChange={onToggle} aria-label={`${server.name} 启用状态`} /></footer>
  </article>;
}
