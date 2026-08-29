import { useCallback, useDeferredValue, useEffect, useMemo, useState, type CSSProperties } from "react";
import {
  Alert,
  Button,
  App as AntdApp,
  Checkbox,
  Drawer,
  Empty,
  Form,
  Input,
  Popconfirm,
  Progress,
  Select,
  Skeleton,
  Switch,
  Tag,
} from "antd";
import { deleteData, getData, patchData, postData, putData } from "@/api/http";
import { MetricStrip, StatusDot, WorkspaceHeader } from "@/components/common/Workspace";
import type { AgentItem, AgentListData, AgentOptions, AgentScope, RouteDryRunResult } from "@/types/api";
import { formatTime } from "@/utils/format";
import { agentColor } from "@/utils/agentColor";

interface AgentFormValues {
  name: string;
  description: string;
  system_prompt?: string;
  model?: string;
  model_base_url?: string;
  memory_enabled: boolean;
  scope: AgentScope;
  skill_ids: number[];
  tool_names: string[];
  capability_ids: string[];
  kb_ids: string[];
}

const EMPTY_OPTIONS: AgentOptions = {
  skills: [],
  tools: [],
  mcp_servers: [],
  knowledge_bases: [],
};

function bindingTotal(agent: AgentItem): number {
  const counts = agent.binding_counts;
  return counts.skills + counts.tools + counts.mcp + counts.kbs;
}

function AgentCard({
  agent,
  busy,
  onEdit,
  onToggle,
  onDelete,
}: {
  agent: AgentItem;
  busy: boolean;
  onEdit: () => void;
  onToggle: () => void;
  onDelete: () => void;
}) {
  const invalid = [
    ...agent.bindings.skills,
    ...agent.bindings.mcp_capabilities,
    ...agent.bindings.knowledge_bases,
  ].filter((item) => item.invalid).length;
  return (
    <article className={`agent-card${agent.status === "disabled" ? " is-disabled" : ""}`}>
      <header className="agent-card-head">
        <div className="agent-card-icon" aria-hidden="true">
          <span className="material-symbols-rounded">smart_toy</span>
        </div>
        <button type="button" className="agent-card-title" onClick={onEdit}>
          <b>{agent.name}</b>
          <small>{agent.scope === "internal" ? "内部 Agent" : "用户可选 Agent"}</small>
        </button>
        <StatusDot
          label={agent.status === "active" ? "运行中" : "已停用"}
          tone={agent.status === "active" ? "success" : "neutral"}
        />
      </header>
      <p className="agent-card-description">{agent.description}</p>
      <div className="agent-card-model">
        <span className="material-symbols-rounded">memory</span>
        <b>{agent.model || "跟随全局默认模型"}</b>
        {agent.memory_enabled ? <Tag bordered={false} color="cyan">记忆开启</Tag> : <Tag bordered={false}>记忆关闭</Tag>}
      </div>
      <div className="agent-card-metrics">
        <span><b>{bindingTotal(agent)}</b>能力绑定</span>
        <span><b>{agent.session_count}</b>关联会话</span>
        <span><b>{agent.binding_counts.mcp}</b>MCP Tools</span>
      </div>
      <footer className="agent-card-footer">
        <small>{agent.last_used_at ? `最近使用 ${formatTime(agent.last_used_at)}` : "尚未用于会话"}</small>
        {invalid ? <span className="agent-invalid-count"><span className="material-symbols-rounded">warning</span>{invalid} 项失效</span> : null}
        <Button size="small" type="text" onClick={onEdit}>配置</Button>
        <Switch size="small" checked={agent.status === "active"} loading={busy} onChange={onToggle} />
        <Popconfirm
          title={`归档 ${agent.name}？`}
          description="归档后不会出现在列表和会话选择器中，历史消息仍会保留。"
          okText="归档"
          cancelText="取消"
          okButtonProps={{ danger: true }}
          onConfirm={onDelete}
        >
          <Button size="small" type="text" danger aria-label={`归档 ${agent.name}`} icon={<span className="material-symbols-rounded">archive</span>} />
        </Popconfirm>
      </footer>
    </article>
  );
}

export default function AgentHubPage() {
  const { message: toast } = AntdApp.useApp();
  const [form] = Form.useForm<AgentFormValues>();
  const [agents, setAgents] = useState<AgentItem[]>([]);
  const [options, setOptions] = useState<AgentOptions>(EMPTY_OPTIONS);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [keyword, setKeyword] = useState("");
  const [status, setStatus] = useState("all");
  const [editorOpen, setEditorOpen] = useState(false);
  const [editing, setEditing] = useState<AgentItem | null>(null);
  const [simulatorAgentId, setSimulatorAgentId] = useState("");
  const [simulatorMessage, setSimulatorMessage] = useState("");
  const [simulatorLoading, setSimulatorLoading] = useState(false);
  const [simulatorError, setSimulatorError] = useState<string | null>(null);
  const [simulatorResult, setSimulatorResult] = useState<RouteDryRunResult | null>(null);
  const query = useDeferredValue(keyword.trim().toLowerCase());

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [list, catalog] = await Promise.all([
        getData<AgentListData>("/agents?page=1&page_size=100"),
        getData<AgentOptions>("/agents/options"),
      ]);
      setAgents(list.items);
      setOptions(catalog);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载 Agent 管理数据失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    if (!simulatorAgentId && agents.length) {
      setSimulatorAgentId(agents.find((agent) => agent.status === "active")?.id ?? agents[0].id);
    }
  }, [agents, simulatorAgentId]);

  const visibleAgents = useMemo(() => agents.filter((agent) => {
    const matchesQuery = !query || `${agent.name} ${agent.description} ${agent.model || ""}`.toLowerCase().includes(query);
    return matchesQuery && (status === "all" || agent.status === status);
  }), [agents, query, status]);

  const openCreate = () => {
    setEditing(null);
    form.resetFields();
    form.setFieldsValue({
      memory_enabled: true,
      scope: "user",
      skill_ids: [],
      tool_names: [],
      capability_ids: [],
      kb_ids: [],
    });
    setEditorOpen(true);
  };

  const openEdit = async (agent: AgentItem) => {
    setBusy(agent.id);
    try {
      const detail = await getData<AgentItem>(`/agents/${agent.id}`);
      setEditing(detail);
      form.setFieldsValue({
        name: detail.name,
        description: detail.description,
        system_prompt: detail.system_prompt || "",
        model: detail.model || "",
        model_base_url: detail.model_base_url || "",
        memory_enabled: detail.memory_enabled,
        scope: detail.scope,
        skill_ids: detail.bindings.skills.map((item) => item.id),
        tool_names: detail.bindings.tools,
        capability_ids: detail.bindings.mcp_capabilities.map((item) => item.id),
        kb_ids: detail.bindings.knowledge_bases.map((item) => item.id),
      });
      setEditorOpen(true);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载 Agent 详情失败");
    } finally {
      setBusy(null);
    }
  };

  const save = async () => {
    let values: AgentFormValues;
    try {
      values = await form.validateFields();
    } catch {
      return;
    }
    setSaving(true);
    try {
      const payload = {
        name: values.name.trim(),
        description: values.description.trim(),
        system_prompt: values.system_prompt?.trim() || null,
        model: values.model?.trim() || null,
        model_base_url: values.model_base_url?.trim() || null,
        memory_enabled: values.memory_enabled,
        scope: values.scope,
      };
      const saved = editing
        ? await patchData<AgentItem>(`/agents/${editing.id}`, payload)
        : await postData<AgentItem>("/agents", payload);
      await Promise.all([
        putData(`/agents/${saved.id}/bindings/skills`, { skill_ids: values.skill_ids || [] }),
        putData(`/agents/${saved.id}/bindings/tools`, { tool_names: values.tool_names || [] }),
        putData(`/agents/${saved.id}/bindings/mcp`, { capability_ids: values.capability_ids || [] }),
        putData(`/agents/${saved.id}/bindings/kbs`, { kb_ids: values.kb_ids || [] }),
      ]);
      toast.success(editing ? "Agent 配置已更新" : "Agent 已创建");
      setEditorOpen(false);
      await load();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "保存 Agent 失败");
    } finally {
      setSaving(false);
    }
  };

  const toggle = async (agent: AgentItem) => {
    setBusy(agent.id);
    try {
      await postData(`/agents/${agent.id}/${agent.status === "active" ? "disable" : "enable"}`, {});
      await load();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "更新 Agent 状态失败");
    } finally {
      setBusy(null);
    }
  };

  const archive = async (agent: AgentItem) => {
    setBusy(agent.id);
    try {
      await deleteData(`/agents/${agent.id}`);
      toast.success("Agent 已归档");
      await load();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "归档 Agent 失败");
    } finally {
      setBusy(null);
    }
  };

  const allCapabilityIds = options.mcp_servers.flatMap((server) => server.capabilities.map((item) => item.id));
  const selectedMcp = Form.useWatch("capability_ids", form) || [];
  const descriptionValue = Form.useWatch("description", form) || "";
  const descriptionNeedsGuidance = Boolean(descriptionValue)
    && !(descriptionValue.includes("负责") && descriptionValue.includes("不能"));

  const runRouteSimulation = async () => {
    if (!simulatorAgentId || !simulatorMessage.trim()) return;
    setSimulatorLoading(true);
    setSimulatorError(null);
    try {
      const result = await postData<RouteDryRunResult>(
        `/admin/agents/${simulatorAgentId}/route-dry-run`,
        { message: simulatorMessage.trim() },
      );
      setSimulatorResult(result);
    } catch (error) {
      setSimulatorResult(null);
      setSimulatorError(error instanceof Error ? error.message : "路由模拟失败");
    } finally {
      setSimulatorLoading(false);
    }
  };

  return (
    <div className="page-scroll workspace-page agent-hub-page">
      <WorkspaceHeader
        icon="smart_toy"
        title="Agent 管理"
        description="集中定义 Agent 的职责、模型与能力边界，并验证多 Agent 路由决策。"
        eyebrow={<span>AgentHub · Agent 编排运行时</span>}
        actions={<Button type="primary" icon={<span className="material-symbols-rounded">add</span>} onClick={openCreate}>新建 Agent</Button>}
      />
      <MetricStrip items={[
        { label: "Agent 总数", value: agents.length, meta: `${agents.filter((item) => item.status === "active").length} 个运行中` },
        { label: "能力绑定", value: agents.reduce((sum, item) => sum + bindingTotal(item), 0), meta: "Skill、工具、MCP 与知识库", tone: "teal" },
        { label: "关联会话", value: agents.reduce((sum, item) => sum + item.session_count, 0), meta: "保留消息级 Agent 归属", tone: "success" },
        { label: "需要处理", value: agents.filter((item) => item.status === "disabled").length, meta: "当前已停用", tone: "warning", onClick: () => setStatus("disabled") },
      ]} />
      <section className="agent-route-simulator" aria-labelledby="agent-route-simulator-title">
        <header>
          <span className="material-symbols-rounded" aria-hidden="true">route</span>
          <div><h2 id="agent-route-simulator-title">路由模拟器</h2><p>只执行 Router 和 Plan 预览，不会调用 Worker 或写入会话。</p></div>
        </header>
        <div className="agent-route-simulator-grid">
          <div className="agent-route-simulator-form">
            <label htmlFor="route-simulator-agent">调试入口 Agent</label>
            <Select
              id="route-simulator-agent"
              value={simulatorAgentId || undefined}
              onChange={setSimulatorAgentId}
              options={agents.map((agent) => ({ value: agent.id, label: `${agent.name} · ${agent.scope === "internal" ? "内部" : "用户可选"}` }))}
              placeholder="选择 Agent"
            />
            <label htmlFor="route-simulator-message">模拟用户消息</label>
            <Input.TextArea id="route-simulator-message" rows={3} value={simulatorMessage} onChange={(event) => setSimulatorMessage(event.target.value)} placeholder="例如：分析这份销售数据并生成一份管理层报告" />
            <Button type="primary" loading={simulatorLoading} disabled={!simulatorAgentId || !simulatorMessage.trim()} onClick={() => void runRouteSimulation()}>模拟路由</Button>
          </div>
          <div className="agent-route-result" aria-live="polite">
            {simulatorError ? <Alert type="error" showIcon message={simulatorError} /> : null}
            {!simulatorError && simulatorLoading ? <Skeleton active paragraph={{ rows: 3 }} /> : null}
            {!simulatorError && !simulatorLoading && !simulatorResult ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="输入消息后查看路由结果" /> : null}
            {simulatorResult ? (
              <div className="agent-route-result-card" style={{ "--agent-color": agentColor(simulatorResult.target_agent) } as CSSProperties}>
                <header><span className="material-symbols-rounded">smart_toy</span><div><b>{simulatorResult.target_agent}</b><small>{simulatorResult.mode === "orchestrate" ? "多 Agent 编排" : "直接执行"}</small></div></header>
                <Progress percent={Math.round(simulatorResult.confidence * 100)} strokeColor={simulatorResult.confidence < .5 ? "var(--warning)" : "var(--brand-500)"} size="small" />
                <p>{simulatorResult.reason}</p>
                {simulatorResult.needs_clarification ? <Alert type="warning" showIcon message={simulatorResult.clarification_question || "需要补充信息"} /> : null}
                {simulatorResult.plan ? <div className="agent-route-plan"><b>{simulatorResult.plan.summary}</b>{simulatorResult.plan.tasks.map((task) => <span key={task.task_id}>{task.target_agent} · {task.goal}</span>)}</div> : null}
              </div>
            ) : null}
          </div>
        </div>
      </section>
      <section className="agent-toolbar" aria-label="Agent 筛选">
        <Input allowClear prefix={<span className="material-symbols-rounded">search</span>} placeholder="搜索名称、职责或模型" value={keyword} onChange={(event) => setKeyword(event.target.value)} />
        <Select value={status} onChange={setStatus} options={[{ value: "all", label: "全部状态" }, { value: "active", label: "运行中" }, { value: "disabled", label: "已停用" }]} />
        <Button icon={<span className="material-symbols-rounded">refresh</span>} loading={loading} onClick={() => void load()}>刷新</Button>
      </section>
      {loading && !agents.length ? <section className="agent-grid">{Array.from({ length: 6 }, (_, index) => <article className="agent-card" key={index}><Skeleton active paragraph={{ rows: 4 }} /></article>)}</section> : null}
      {!loading && !visibleAgents.length ? <Empty description={agents.length ? "没有匹配的 Agent" : "还没有 Agent"}><Button type="primary" onClick={openCreate}>新建 Agent</Button></Empty> : null}
      <section className="agent-grid">
        {visibleAgents.map((agent) => <AgentCard key={agent.id} agent={agent} busy={busy === agent.id} onEdit={() => void openEdit(agent)} onToggle={() => void toggle(agent)} onDelete={() => void archive(agent)} />)}
      </section>

      <Drawer
        className="agent-editor-drawer"
        width="min(760px, 96vw)"
        open={editorOpen}
        onClose={() => !saving && setEditorOpen(false)}
        title={<div className="agent-editor-title"><span className="material-symbols-rounded">tune</span><div><b>{editing ? `配置 ${editing.name}` : "新建 Agent"}</b><small>身份提示词用于定义职责，实际能力以绑定项为准。</small></div></div>}
        extra={<Button type="primary" loading={saving} onClick={() => void save()}>保存配置</Button>}
        destroyOnHidden={false}
      >
        <Form form={form} layout="vertical" requiredMark="optional" initialValues={{ memory_enabled: true, scope: "user", skill_ids: [], tool_names: [], capability_ids: [], kb_ids: [] }}>
          <section className="agent-form-section">
            <div className="agent-form-section-head"><span className="material-symbols-rounded">badge</span><div><h3>基础定义</h3><p>名称与职责会出现在会话选择器中。</p></div></div>
            <div className="agent-form-grid two-columns">
              <Form.Item name="name" label="Agent 名称" rules={[{ required: true, message: "请输入 Agent 名称" }, { max: 128 }]}><Input placeholder="例如：知识库分析助手" /></Form.Item>
              <Form.Item name="scope" label="可见范围"><Select options={[{ value: "user", label: "用户可选" }, { value: "internal", label: "内部编排 · 不出现在会话选择器" }]} /></Form.Item>
            </div>
            <Form.Item name="description" label="职责描述" rules={[{ required: true, message: "请输入职责描述" }, { max: 2000 }]} extra={descriptionNeedsGuidance ? <span className="agent-description-warning">建议同时写明“负责”和“不能”，减少误路由。</span> : "描述负责领域、能做什么、不能做什么，以及输入与输出。"}><Input.TextArea rows={3} showCount maxLength={2000} placeholder="负责【XX 类任务】。能：…。不能：…。输入：…；输出：…。" /></Form.Item>
            <Form.Item name="system_prompt" label="身份与任务边界" extra="该内容会叠加在 Harness 默认安全边界之前，不会替换 Skill 指令。"><Input.TextArea rows={6} showCount maxLength={20000} placeholder="描述角色、目标、约束和输出偏好。" /></Form.Item>
            <div className="agent-form-grid two-columns">
              <Form.Item name="model" label="默认模型"><Input allowClear placeholder="留空则跟随全局默认模型" /></Form.Item>
              <Form.Item name="model_base_url" label="模型 Base URL"><Input allowClear placeholder="仅自定义兼容端点需要填写" /></Form.Item>
            </div>
            <Form.Item name="memory_enabled" label="长期记忆" valuePropName="checked"><Switch checkedChildren="开启" unCheckedChildren="关闭" /></Form.Item>
          </section>

          <section className="agent-form-section">
            <div className="agent-form-section-head"><span className="material-symbols-rounded">bolt</span><div><h3>Skill</h3><p>空绑定表示该 Agent 不注入任何 Skill 指令与脚本工具。</p></div></div>
            <Form.Item name="skill_ids" className="agent-check-group"><Checkbox.Group>{options.skills.map((skill) => <Checkbox key={skill.id} value={skill.id} disabled={!skill.enabled}><span><b>{skill.name}</b><small>{skill.description}</small></span></Checkbox>)}</Checkbox.Group></Form.Item>
          </section>

          <section className="agent-form-section">
            <div className="agent-form-section-head"><span className="material-symbols-rounded">build</span><div><h3>内置工具</h3><p>不选择表示不限制内置工具；选择后只开放勾选项。</p></div></div>
            <Form.Item name="tool_names" className="agent-check-group compact"><Checkbox.Group>{options.tools.map((tool) => <Checkbox key={tool.name} value={tool.name}><span><b>{tool.name}</b><small>{tool.description}</small></span></Checkbox>)}</Checkbox.Group></Form.Item>
          </section>

          <section className="agent-form-section">
            <div className="agent-form-section-head"><span className="material-symbols-rounded">hub</span><div><h3>MCP 工具</h3><p>按 Server 分组勾选到工具级，运行时会跳过异常或未批准能力。</p></div><Button size="small" type="link" onClick={() => form.setFieldValue("capability_ids", selectedMcp.length === allCapabilityIds.length ? [] : allCapabilityIds)}>{selectedMcp.length === allCapabilityIds.length ? "全部取消" : "全部选择"}</Button></div>
            <Form.Item name="capability_ids" className="agent-check-group mcp-groups"><Checkbox.Group>{options.mcp_servers.map((server) => <div className="agent-mcp-group" key={server.server_id}><header><b>{server.server_name}</b><StatusDot label={server.health_status} tone={server.health_status === "ready" ? "success" : server.health_status === "error" ? "danger" : "warning"} /></header>{server.capabilities.map((capability) => <Checkbox key={capability.id} value={capability.id} disabled={!capability.enabled}><span><b>{capability.name}</b><small>{capability.description || "暂无描述"}</small></span></Checkbox>)}</div>)}</Checkbox.Group></Form.Item>
          </section>

          <section className="agent-form-section">
            <div className="agent-form-section-head"><span className="material-symbols-rounded">database</span><div><h3>知识库</h3><p>不绑定时继续跟随会话请求；绑定后 Agent 默认使用这些知识库。</p></div></div>
            <Form.Item name="kb_ids" className="agent-check-group compact"><Checkbox.Group>{options.knowledge_bases.map((kb) => <Checkbox key={kb.id} value={kb.id}><span><b>{kb.name}</b><small>{kb.description || "暂无描述"}</small></span></Checkbox>)}</Checkbox.Group></Form.Item>
          </section>
        </Form>
      </Drawer>
    </div>
  );
}
