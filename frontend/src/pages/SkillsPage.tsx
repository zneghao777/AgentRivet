import { useCallback, useDeferredValue, useEffect, useMemo, useState, type KeyboardEvent } from "react";
import { Button, Empty, Input, Popconfirm, Select, Skeleton, Switch, message } from "antd";
import { useSearchParams } from "react-router-dom";
import { deleteData, getData, patchData, postData } from "@/api/http";
import McpServerCard from "@/components/capabilities/McpServerCard";
import McpServerCreateModal from "@/components/capabilities/McpServerCreateModal";
import { InspectorField, MetricStrip, ResponsiveInspector, StatusDot, WorkspaceHeader } from "@/components/common/Workspace";
import SkillImportModal from "@/components/skills/SkillImportModal";
import type { CapabilitySummary, McpCapabilityItem, McpServerItem, SkillCategory, SkillItem } from "@/types/api";

type TabKey = "skills" | "mcp";
const CATEGORY_LABEL: Record<SkillCategory, string> = { builtin: "内置", external: "外部" };
const TABS: Array<{ key: TabKey; label: string; icon: string }> = [{ key: "skills", label: "Skills", icon: "bolt" }, { key: "mcp", label: "MCP Servers", icon: "hub" }];

function SkillCard({ skill, busy, onInspect, onToggle, onDelete }: { skill: SkillItem; busy: boolean; onInspect: () => void; onToggle: () => void; onDelete: () => void }) {
  return <article className={`capability-card skill-card${skill.has_scripts ? " has-risk" : ""}`}>
    <header className="capability-card-head"><span className="capability-card-icon material-symbols-rounded">{skill.source === "builtin" ? "bolt" : "deployed_code"}</span><button type="button" className="capability-card-title" onClick={onInspect}><b>{skill.name}</b><small>{CATEGORY_LABEL[skill.source]} · v{skill.version}</small></button>{skill.has_scripts ? <span className="capability-risk-mark material-symbols-rounded" title="包含可执行脚本">security</span> : null}</header>
    <p className="capability-card-description">{skill.description || "暂无描述"}</p>
    <div className="capability-card-status"><StatusDot label={skill.enabled ? "已启用" : "已停用"} tone={skill.enabled ? "success" : "neutral"} />{skill.tags.slice(0, 2).map((tag) => <span className="capability-tag" key={tag}>{tag}</span>)}{skill.tags.length > 2 ? <span className="capability-tag">+{skill.tags.length - 2}</span> : null}</div>
    <div className="capability-card-metrics"><span><b>{skill.file_count}</b>文件</span><span><b>{skill.has_scripts ? "有" : "无"}</b>脚本</span><span><b>{skill.author || "—"}</b>作者</span></div>
    <footer className="capability-card-footer"><small>{skill.source === "builtin" ? "系统内置能力" : "租户托管能力"}</small>{skill.source === "external" ? <Popconfirm title={`删除 ${skill.name}？`} description="将删除托管目录中的全部文件。" okButtonProps={{ danger: true }} onConfirm={onDelete}><Button type="text" size="small" danger>删除</Button></Popconfirm> : null}<Switch checked={skill.enabled} loading={busy} onChange={onToggle} /></footer>
  </article>;
}

export default function SkillsPage() {
  const [params, setParams] = useSearchParams();
  const tab: TabKey = params.get("tab") === "mcp" ? "mcp" : "skills";
  const [skills, setSkills] = useState<SkillItem[]>([]);
  const [servers, setServers] = useState<McpServerItem[]>([]);
  const [summary, setSummary] = useState<CapabilitySummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [keyword, setKeyword] = useState(params.get("q") || "");
  const [status, setStatus] = useState("all");
  const [busy, setBusy] = useState<string | null>(null);
  const [importOpen, setImportOpen] = useState(false);
  const [mcpOpen, setMcpOpen] = useState(false);
  const [selectedSkill, setSelectedSkill] = useState<SkillItem | null>(null);
  const [selectedServer, setSelectedServer] = useState<McpServerItem | null>(null);
  const [serverTools, setServerTools] = useState<McpCapabilityItem[]>([]);
  const query = useDeferredValue(keyword.trim().toLowerCase());

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [skillRows, serverRows, summaryData] = await Promise.all([
        getData<SkillItem[]>("/skills"), getData<McpServerItem[]>("/mcp/servers"), getData<CapabilitySummary>("/capabilities/summary"),
      ]);
      setSkills(skillRows); setServers(serverRows); setSummary(summaryData);
      setSelectedSkill((current) => current ? skillRows.find((item) => item.name === current.name) ?? null : null);
      setSelectedServer((current) => current ? serverRows.find((item) => item.id === current.id) ?? null : null);
    } catch (error) { message.error(error instanceof Error ? error.message : "加载插件失败"); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => { const next = new URLSearchParams(params); keyword ? next.set("q", keyword) : next.delete("q"); setParams(next, { replace: true }); }, [keyword]); // eslint-disable-line react-hooks/exhaustive-deps

  const switchTab = (nextTab: TabKey) => { const next = new URLSearchParams(params); next.set("tab", nextTab); next.delete("q"); setParams(next, { replace: true }); setKeyword(""); setStatus("all"); setSelectedSkill(null); setSelectedServer(null); };
  const onTabKeyDown = (event: KeyboardEvent<HTMLButtonElement>, current: TabKey) => { const index = TABS.findIndex((item) => item.key === current); if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return; event.preventDefault(); const nextIndex = event.key === "Home" ? 0 : event.key === "End" ? TABS.length - 1 : event.key === "ArrowRight" ? (index + 1) % TABS.length : (index - 1 + TABS.length) % TABS.length; switchTab(TABS[nextIndex].key); window.requestAnimationFrame(() => document.getElementById(`cap-tab-${TABS[nextIndex].key}`)?.focus()); };
  const inspectServer = async (server: McpServerItem) => { setSelectedServer(server); try { setServerTools(await getData<McpCapabilityItem[]>(`/mcp/servers/${server.id}/capabilities`)); } catch { setServerTools([]); } };
  const toggleSkill = async (skill: SkillItem) => { setBusy(skill.name); try { await postData(`/skills/${encodeURIComponent(skill.name)}/${skill.enabled ? "disable" : "enable"}`, {}); await load(); } catch (error) { message.error(error instanceof Error ? error.message : "更新 Skill 失败"); } finally { setBusy(null); } };
  const removeSkill = async (skill: SkillItem) => { setBusy(skill.name); try { await deleteData(`/skills/${encodeURIComponent(skill.name)}`); setSelectedSkill(null); await load(); } finally { setBusy(null); } };
  const testServer = async (server: McpServerItem) => { setBusy(server.id); try { await postData(`/mcp/servers/${server.id}/test`, {}); message.success("连接测试和能力发现完成"); await load(); } catch (error) { message.error(error instanceof Error ? error.message : "连接测试失败"); await load(); } finally { setBusy(null); } };
  const toggleServer = async (server: McpServerItem) => { setBusy(server.id); try { await postData(`/mcp/servers/${server.id}/${server.enabled ? "disable" : "enable"}`, {}); await load(); } catch (error) { message.error(error instanceof Error ? error.message : "更新 MCP Server 失败"); } finally { setBusy(null); } };
  const removeServer = async (server: McpServerItem) => { setBusy(server.id); try { await deleteData(`/mcp/servers/${server.id}`); setSelectedServer(null); await load(); } finally { setBusy(null); } };
  const reviewTool = async (tool: McpCapabilityItem, enabled: boolean) => {
    if (!selectedServer) return;
    setBusy(tool.id);
    try {
      const updated = await patchData<McpCapabilityItem>(
        `/mcp/servers/${selectedServer.id}/capabilities/${tool.id}`,
        { enabled },
      );
      setServerTools((current) => current.map((item) => item.id === updated.id ? updated : item));
      await load();
    } catch (error) {
      message.error(error instanceof Error ? error.message : "更新工具失败");
    } finally { setBusy(null); }
  };

  const visibleSkills = useMemo(() => skills.filter((item) => (!query || `${item.name} ${item.description} ${item.tags.join(" ")}`.toLowerCase().includes(query)) && (status === "all" || (status === "enabled") === item.enabled)), [query, skills, status]);
  const visibleServers = useMemo(() => servers.filter((item) => (!query || `${item.name} ${item.description || ""} ${item.endpoint_url}`.toLowerCase().includes(query)) && (status === "all" || status === item.health_status || (status === "enabled" && item.enabled) || (status === "disabled" && !item.enabled))), [query, servers, status]);
  const inspectorOpen = Boolean(selectedSkill || selectedServer);

  return <div className="page-scroll workspace-page capability-hub-page">
    <WorkspaceHeader icon="extension" title="插件" description="统一管理 Skill 与 MCP Server，为 Agent 配置可信、可观察的能力。" actions={<Button type="primary" icon={<span className="material-symbols-rounded">add</span>} onClick={() => tab === "skills" ? setImportOpen(true) : setMcpOpen(true)}>{tab === "skills" ? "导入 Skill" : "添加 MCP Server"}</Button>} />
    <MetricStrip items={[{ label: "Skills", value: summary?.skills.total ?? skills.length, meta: `${summary?.skills.enabled ?? skills.filter((item) => item.enabled).length} 个已启用`, onClick: () => switchTab("skills") }, { label: "MCP Servers", value: summary?.mcp_servers.total ?? servers.length, meta: `${summary?.mcp_servers.ready ?? 0} 个连接正常`, tone: "teal", onClick: () => switchTab("mcp") }, { label: "可用工具", value: servers.reduce((sum, item) => sum + item.enabled_tool_count, 0), meta: "已批准 MCP Tools", tone: "success" }, { label: "需要处理", value: summary?.needs_attention ?? 0, meta: "异常或部分可用", tone: "warning", onClick: () => { switchTab("mcp"); setStatus("error"); } }]} />
    <nav className="capability-tabs" role="tablist" aria-label="能力类型">{TABS.map((item) => <button type="button" role="tab" id={`cap-tab-${item.key}`} aria-selected={tab === item.key} tabIndex={tab === item.key ? 0 : -1} className={tab === item.key ? "active" : ""} key={item.key} onClick={() => switchTab(item.key)} onKeyDown={(event) => onTabKeyDown(event, item.key)}><span className="material-symbols-rounded">{item.icon}</span>{item.label}<small>{item.key === "skills" ? skills.length : servers.length}</small></button>)}</nav>
    <section className="capability-toolbar"><Input allowClear prefix={<span className="material-symbols-rounded">search</span>} placeholder={tab === "skills" ? "搜索名称、描述或标签" : "搜索 Server、Endpoint"} value={keyword} onChange={(event) => setKeyword(event.target.value)} /><Select value={status} onChange={setStatus} options={tab === "skills" ? [{ value: "all", label: "全部状态" }, { value: "enabled", label: "已启用" }, { value: "disabled", label: "已停用" }] : [{ value: "all", label: "全部状态" }, { value: "ready", label: "连接正常" }, { value: "degraded", label: "部分可用" }, { value: "error", label: "连接异常" }, { value: "disabled", label: "已停用" }]} /><Button icon={<span className="material-symbols-rounded">refresh</span>} onClick={() => void load()} loading={loading}>刷新</Button></section>
    <div className={`capability-body${inspectorOpen ? " has-inspector" : ""}`}><main>{loading && !(skills.length || servers.length) ? <section className="capability-card-grid">{Array.from({ length: 6 }, (_, index) => <article className="capability-card" key={index}><Skeleton active paragraph={{ rows: 4 }} /></article>)}</section> : null}{!loading && tab === "skills" && !visibleSkills.length ? <Empty description={skills.length ? "没有匹配的 Skill" : "还没有 Skill"}><Button type="primary" onClick={() => setImportOpen(true)}>导入 Skill</Button></Empty> : null}{!loading && tab === "mcp" && !visibleServers.length ? <Empty description={servers.length ? "没有匹配的 MCP Server" : "还没有 MCP Server"}><Button type="primary" onClick={() => setMcpOpen(true)}>添加 MCP Server</Button></Empty> : null}<section className="capability-card-grid" role="tabpanel" aria-labelledby={`cap-tab-${tab}`}>{tab === "skills" ? visibleSkills.map((skill) => <SkillCard key={skill.name} skill={skill} busy={busy === skill.name} onInspect={() => { setSelectedServer(null); setSelectedSkill(skill); }} onToggle={() => void toggleSkill(skill)} onDelete={() => void removeSkill(skill)} />) : visibleServers.map((server) => <McpServerCard key={server.id} server={server} busy={busy === server.id} onInspect={() => { setSelectedSkill(null); void inspectServer(server); }} onTest={() => void testServer(server)} onToggle={() => void toggleServer(server)} onDelete={() => void removeServer(server)} />)}</section></main>
      {inspectorOpen ? <ResponsiveInspector open onClose={() => { setSelectedSkill(null); setSelectedServer(null); }} title={selectedSkill?.name || selectedServer?.name || "能力详情"} subtitle={selectedSkill ? <StatusDot label={selectedSkill.enabled ? "已启用" : "已停用"} tone={selectedSkill.enabled ? "success" : "neutral"} /> : selectedServer ? <StatusDot label={selectedServer.health_status} tone={selectedServer.health_status === "ready" ? "success" : selectedServer.health_status === "error" ? "danger" : "warning"} /> : undefined}>{selectedSkill ? <><section className="inspector-section"><h3>概览</h3><InspectorField label="来源">{CATEGORY_LABEL[selectedSkill.source]}</InspectorField><InspectorField label="版本">v{selectedSkill.version}</InspectorField><InspectorField label="作者">{selectedSkill.author || "—"}</InspectorField><InspectorField label="文件">{selectedSkill.file_count} 个{selectedSkill.has_scripts ? " · 含脚本" : ""}</InspectorField></section><section className="inspector-section"><h3>描述</h3><p>{selectedSkill.description}</p></section></> : selectedServer ? <><section className="inspector-section"><h3>连接</h3><InspectorField label="Endpoint" mono>{selectedServer.endpoint_url}</InspectorField><InspectorField label="认证">{selectedServer.credential_configured ? `${selectedServer.auth_type} · 已配置` : "无需认证"}</InspectorField><InspectorField label="协议">{selectedServer.protocol_version || "—"}</InspectorField></section><section className="inspector-section"><h3>Tools · {serverTools.length}</h3><div className="mcp-tool-chips">{serverTools.map((tool) => <div className="mcp-tool-chip-row" key={tool.id}><span className="material-symbols-rounded">build</span><b title={tool.description}>{tool.remote_name}</b><small>{tool.enabled ? "已启用" : tool.review_status === "changed" ? "已变化" : "待审查"}</small><button type="button" role="switch" aria-checked={tool.enabled} aria-busy={busy === tool.id} disabled={busy === tool.id} className={`tool-toggle${tool.enabled ? " is-on" : ""}${busy === tool.id ? " is-busy" : ""}`} onClick={() => void reviewTool(tool, !tool.enabled)}><span className="tool-toggle-thumb" /></button></div>)}</div></section>{selectedServer.last_error_message ? <section className="inspector-section capability-error-panel"><h3>最近问题</h3><p>{selectedServer.last_error_message}</p></section> : null}</> : null}</ResponsiveInspector> : null}</div>
    <SkillImportModal open={importOpen} onClose={() => setImportOpen(false)} onImported={() => void load()} />
    <McpServerCreateModal open={mcpOpen} onClose={() => setMcpOpen(false)} onCreated={() => void load()} />
  </div>;
}
