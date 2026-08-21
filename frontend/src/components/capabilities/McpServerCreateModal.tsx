import { useState } from "react";
import { Alert, Button, Form, Input, Modal, Select, Switch, message } from "antd";
import { patchData, postData } from "@/api/http";
import type { McpCapabilityItem, McpServerItem } from "@/types/api";

type Values = { name: string; description?: string; endpoint_url: string; auth_type: "none" | "bearer"; secret_value?: string };

export default function McpServerCreateModal({ open, onClose, onCreated }: { open: boolean; onClose: () => void; onCreated: (server: McpServerItem) => void }) {
  const [form] = Form.useForm<Values>();
  const [stage, setStage] = useState<"form" | "testing" | "review">("form");
  const [server, setServer] = useState<McpServerItem | null>(null);
  const [tools, setTools] = useState<McpCapabilityItem[]>([]);
  const [busyTool, setBusyTool] = useState<string | null>(null);
  const close = () => { if (stage === "testing") return; form.resetFields(); setStage("form"); setServer(null); setTools([]); onClose(); };
  const submit = async () => {
    const values = await form.validateFields();
    setStage("testing");
    try {
      const created = await postData<McpServerItem>("/mcp/servers", { name: values.name, description: values.description, transport: "streamable_http", endpoint_url: values.endpoint_url, auth: { type: values.auth_type, secret_value: values.secret_value } });
      setServer(created);
      const result = await postData<{ server: McpServerItem; capabilities: McpCapabilityItem[] }>(`/mcp/servers/${created.id}/test`, {});
      setServer(result.server); setTools(result.capabilities); setStage("review");
    } catch (error) { message.error(error instanceof Error ? error.message : "MCP 连接失败"); setStage("form"); }
  };
  const toggleTool = async (tool: McpCapabilityItem, enabled: boolean) => {
    if (!server) return; setBusyTool(tool.id);
    try { const updated = await patchData<McpCapabilityItem>(`/mcp/servers/${server.id}/capabilities/${tool.id}`, { enabled }); setTools((current) => current.map((item) => item.id === updated.id ? updated : item)); }
    catch (error) { message.error(error instanceof Error ? error.message : "工具审查失败"); }
    finally { setBusyTool(null); }
  };
  const finish = async () => {
    if (!server) return;
    try { const enabled = tools.some((tool) => tool.enabled) ? await postData<McpServerItem>(`/mcp/servers/${server.id}/enable`, {}) : server; onCreated(enabled); close(); }
    catch (error) { message.error(error instanceof Error ? error.message : "启用 MCP Server 失败"); }
  };
  return <Modal open={open} onCancel={close} width={760} title="添加 MCP Server" className="mcp-create-modal" footer={stage === "form" ? <><Button onClick={close}>取消</Button><Button type="primary" onClick={() => void submit()}>测试连接</Button></> : stage === "review" ? <><Button onClick={close}>稍后启用</Button><Button type="primary" onClick={() => void finish()}>保存并启用</Button></> : null}>
    {stage === "form" ? <Form form={form} layout="vertical" initialValues={{ auth_type: "none" }}><Alert type="info" showIcon message="当前版本支持远程 Streamable HTTP。新 Server 和新工具默认停用。" /><Form.Item name="name" label="Server 名称" rules={[{ required: true }]}><Input placeholder="例如 internal-github" /></Form.Item><Form.Item name="description" label="说明"><Input placeholder="说明该 Server 的用途和数据范围" /></Form.Item><Form.Item name="endpoint_url" label="MCP Endpoint" rules={[{ required: true }, { type: "url" }]}><Input placeholder="https://mcp.example.com/mcp" /></Form.Item><Form.Item name="auth_type" label="认证方式"><Select options={[{ value: "none", label: "无需认证" }, { value: "bearer", label: "Bearer Token" }]} /></Form.Item><Form.Item noStyle shouldUpdate={(a, b) => a.auth_type !== b.auth_type}>{({ getFieldValue }) => getFieldValue("auth_type") === "bearer" ? <Form.Item name="secret_value" label="Token" rules={[{ required: true }]}><Input.Password autoComplete="new-password" placeholder="仅提交一次，不会回显" /></Form.Item> : null}</Form.Item></Form> : null}
    {stage === "testing" ? <section className="mcp-task-state" aria-live="polite"><span className="material-symbols-rounded mcp-task-spinner">progress_activity</span><div><b>正在测试 MCP 连接</b><p>校验 Endpoint、初始化协议并发现 Tools…</p></div></section> : null}
    {stage === "review" ? <section className="mcp-review"><Alert type="success" showIcon message={`连接成功，发现 ${tools.length} 个工具`} description="审查后选择允许 Agent 使用的工具；未选择的工具保持停用。" /><div className="mcp-tool-review-list">{tools.map((tool) => <article key={tool.id} className="mcp-tool-row"><div><b>{tool.remote_name}</b><p>{tool.description || "暂无工具描述"}</p><code>{tool.effective_name}</code></div><span className={`risk-${tool.risk_level}`}>{tool.risk_level}</span><Switch checked={tool.enabled} loading={busyTool === tool.id} onChange={(checked) => void toggleTool(tool, checked)} /></article>)}</div></section> : null}
  </Modal>;
}
