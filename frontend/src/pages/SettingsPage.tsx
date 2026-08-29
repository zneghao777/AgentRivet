import { useMemo, useState } from "react";
import { Alert, App as AntdApp, Button, Empty, Form, Input, Modal, Popconfirm, Select, Tag } from "antd";
import { postData } from "@/api/http";
import { StatusDot, WorkspaceHeader } from "@/components/common/Workspace";
import { CHAT_MODELS } from "@/constants";
import { useSettingsStore } from "@/stores/settingsStore";
import type { CustomChatModel, CustomChatModelInput } from "@/stores/settingsStore";

type SettingsSection = "model" | "auth" | "about";
type ConnectionState = "idle" | "success" | "error";

interface ConnectionTestResult {
  connected: boolean;
  model: string;
  resolved_model: string;
  latency_ms: number;
}

const SECTIONS: Array<{ key: SettingsSection; icon: string; label: string; description: string }> = [
  { key: "model", icon: "cloud", label: "模型管理", description: "内置与自定义模型" },
  { key: "auth", icon: "key", label: "API Key / 鉴权", description: "本机密钥与连接测试" },
  { key: "about", icon: "info", label: "关于", description: "版本与运行信息" },
];

export default function SettingsPage() {
  const { message } = AntdApp.useApp();
  const {
    apiKey, setApiKey, model, setModel, baseUrl, setBaseUrl, customModels,
    addCustomModel, updateCustomModel, removeCustomModel,
  } = useSettingsStore();
  const [modelForm] = Form.useForm<CustomChatModelInput>();
  const [section, setSection] = useState<SettingsSection>("model");
  const [testing, setTesting] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [connectionState, setConnectionState] = useState<ConnectionState>("idle");
  const [connectionDetail, setConnectionDetail] = useState("");
  const [modelModalOpen, setModelModalOpen] = useState(false);
  const [editingModel, setEditingModel] = useState<CustomChatModel | null>(null);

  const modelOptions = useMemo(() => [
    {
      label: "内置模型",
      options: CHAT_MODELS.map((item) => ({ value: item.value, label: item.label })),
    },
    ...(customModels.length ? [{
      label: "自定义模型",
      options: customModels.map((item) => ({ value: item.modelId, label: item.name })),
    }] : []),
  ], [customModels]);

  const activeModelName = customModels.find((item) => item.modelId === model)?.name
    ?? CHAT_MODELS.find((item) => item.value === model)?.label
    ?? model;
  const activeCustomModel = customModels.find((item) => item.modelId === model);
  const activeBaseUrl = activeCustomModel?.baseUrl ?? baseUrl;

  const save = () => {
    setDirty(false);
    message.success("设置已保存到本地浏览器");
  };

  const testConnection = async () => {
    setTesting(true);
    try {
      const result = await postData<ConnectionTestResult>("/admin/test-connection", {
        model,
        base_url: activeBaseUrl,
        api_key: activeCustomModel?.apiKey,
      });
      setConnectionState("success");
      setConnectionDetail(`${result.resolved_model} · ${result.latency_ms}ms`);
      message.success("模型连接测试成功");
    } catch (error) {
      const detail = error instanceof Error ? error.message : "连接失败";
      setConnectionState("error");
      setConnectionDetail(detail);
      message.error(detail);
    } finally {
      setTesting(false);
    }
  };

  const markDirty = (update: () => void) => {
    update();
    setDirty(true);
    setConnectionState("idle");
    setConnectionDetail("");
  };

  const openAddModel = () => {
    setEditingModel(null);
    modelForm.resetFields();
    setModelModalOpen(true);
  };

  const openEditModel = (item: CustomChatModel) => {
    setEditingModel(item);
    modelForm.setFieldsValue({
      name: item.name,
      modelId: item.modelId,
      description: item.description,
      baseUrl: item.baseUrl,
      apiKey: item.apiKey,
    });
    setModelModalOpen(true);
  };

  const submitCustomModel = async () => {
    const values = await modelForm.validateFields();
    if (editingModel) {
      updateCustomModel(editingModel.id, values);
      message.success("自定义模型已更新");
    } else {
      const added = addCustomModel(values);
      setModel(added.modelId);
      message.success("模型已添加，并设为当前模型");
    }
    setDirty(true);
    setConnectionState("idle");
    setModelModalOpen(false);
  };

  const deleteCustomModel = (item: CustomChatModel) => {
    removeCustomModel(item.id);
    setDirty(true);
    setConnectionState("idle");
    message.success(item.modelId === model ? "模型已删除，已切换至默认模型" : "模型已删除");
  };

  return (
    <div className="page-scroll workspace-page settings-page">
      <WorkspaceHeader icon="settings" title="系统设置" description="管理你的模型、密钥和运行环境" />
      <div className="settings-layout">
        <nav className="settings-nav" aria-label="设置分类">
          {SECTIONS.map((item) => (
            <button type="button" key={item.key} className={section === item.key ? "active" : ""} onClick={() => setSection(item.key)}>
              <span className="material-symbols-rounded">{item.icon}</span>
              <span><b>{item.label}</b><small>{item.description}</small></span>
            </button>
          ))}
        </nav>
        <main className="settings-surface">
          {section === "model" ? <>
            <div className="settings-section-heading settings-heading-with-action"><div><h2>模型管理</h2><p>管理可用于 Harness Agent 对话的模型</p></div><Button type="primary" icon={<span className="material-symbols-rounded">add</span>} onClick={openAddModel}>添加自定义模型</Button></div>
            <Alert type="info" showIcon message="每个自定义模型可使用独立的 OpenAI 兼容 Base URL 与供应商 API Key；添加后会立即出现在对话条中。" />
            <Form layout="vertical" className="settings-form">
              <Form.Item label="当前模型" extra={`对话条当前使用：${activeModelName} · ${model}`}>
                <Select
                  value={model}
                  onChange={(value) => markDirty(() => setModel(value))}
                  options={modelOptions}
                  optionFilterProp="label"
                  showSearch
                />
              </Form.Item>
              <Form.Item label="当前接入地址" extra={activeCustomModel ? "该地址属于当前自定义模型，请通过下方编辑按钮修改" : "内置模型使用的 OpenAI 兼容服务地址"}>
                <Input value={activeBaseUrl} disabled={Boolean(activeCustomModel)} onChange={(event) => markDirty(() => setBaseUrl(event.target.value))} />
              </Form.Item>
              <div className="connection-status-row">
                <span>连接状态</span>
                <StatusDot
                  label={connectionState === "success" ? `已连接 · ${connectionDetail}` : connectionState === "error" ? "连接失败" : apiKey ? "等待测试" : "尚未配置"}
                  tone={connectionState === "success" ? "success" : connectionState === "error" ? "danger" : apiKey ? "warning" : "neutral"}
                />
                <Button onClick={() => void testConnection()} loading={testing} disabled={!model.trim() || !activeBaseUrl.trim()}>测试连接</Button>
              </div>
              {connectionState === "error" ? <Alert type="error" showIcon message={connectionDetail} /> : null}
            </Form>
            <section className="model-management-list" aria-labelledby="custom-model-heading">
              <div className="model-list-heading"><div><h3 id="custom-model-heading">自定义模型</h3><p>模型配置保存在当前浏览器中，共 {customModels.length} 个</p></div></div>
              {customModels.length ? <div className="model-list-rows">
                {customModels.map((item) => <article className={`model-management-row${model === item.modelId ? " active" : ""}`} key={item.id}>
                  <span className="model-management-icon material-symbols-rounded">deployed_code</span>
                  <div className="model-management-main"><div><strong>{item.name}</strong>{model === item.modelId ? <Tag color="blue">当前使用</Tag> : <Tag>自定义</Tag>}</div><code>{item.modelId}</code><p>{item.description || "未填写模型说明"}</p><small title={item.baseUrl}>{item.baseUrl} · Key {maskApiKey(item.apiKey)}</small></div>
                  <div className="model-management-actions">
                    {model !== item.modelId ? <Button size="small" onClick={() => markDirty(() => setModel(item.modelId))}>设为当前</Button> : null}
                    <Button size="small" type="text" aria-label={`编辑 ${item.name}`} onClick={() => openEditModel(item)}><span className="material-symbols-rounded">edit</span></Button>
                    <Popconfirm title="删除这个自定义模型？" description={model === item.modelId ? "删除后将自动切换到默认模型。" : undefined} okText="删除" cancelText="取消" onConfirm={() => deleteCustomModel(item)}>
                      <Button size="small" type="text" danger aria-label={`删除 ${item.name}`}><span className="material-symbols-rounded">delete</span></Button>
                    </Popconfirm>
                  </div>
                </article>)}
              </div> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚未添加自定义模型" />}
            </section>
          </> : null}
          {section === "auth" ? <>
            <div className="settings-section-heading"><div><h2>API Key / 鉴权</h2><p>密钥仅保存在当前浏览器中，不会出现在页面明文或日志里</p></div></div>
            <Form layout="vertical" className="settings-form">
              <Form.Item label="Harness API Key" extra="用于浏览器访问 Harness 后端，与自定义模型的供应商 Key 相互独立">
                <Input.Password autoComplete="off" value={apiKey} onChange={(event) => markDirty(() => setApiKey(event.target.value))} placeholder="sk-••••••••••••••••" />
              </Form.Item>
              <Alert type="warning" showIcon message="配置仅保存在本地浏览器，请勿在共享设备中保存生产密钥。" />
            </Form>
          </> : null}
          {section === "about" ? <>
            <div className="settings-section-heading"><div><h2>关于 Harness</h2><p>当前前端与运行环境信息</p></div></div>
            <div className="about-grid"><div><span>产品名称</span><b>Harness · 企业级 Agent 工作台</b></div><div><span>前端版本</span><b>0.1.0</b></div><div><span>运行模式</span><StatusDot label="研发环境" tone="info" /></div><div><span>数据存储</span><b>本地服务</b></div></div>
          </> : null}
        </main>
      </div>
      <footer className="settings-savebar"><span>所有更改仅保存在本地浏览器中</span><Button onClick={() => setDirty(false)}>重置状态</Button><Button type="primary" disabled={!dirty} onClick={save}>{dirty ? "保存设置" : "设置已保存"}</Button></footer>
      <Modal title={editingModel ? "编辑自定义模型" : "添加自定义模型"} open={modelModalOpen} okText={editingModel ? "保存更改" : "添加并使用"} cancelText="取消" forceRender onCancel={() => setModelModalOpen(false)} onOk={() => void submitCustomModel()}>
        <Form form={modelForm} layout="vertical" className="custom-model-form" preserve={false}>
          <Form.Item name="name" label="显示名称" rules={[{ required: true, whitespace: true, message: "请输入显示名称" }, { max: 40, message: "最多 40 个字符" }]}>
            <Input placeholder="例如：团队推理模型" autoFocus />
          </Form.Item>
          <Form.Item name="modelId" label="模型 ID" extra="将原样传给 OpenAI 兼容接口" rules={[
            { required: true, whitespace: true, message: "请输入模型 ID" },
            { max: 128, message: "最多 128 个字符" },
            { pattern: /^[A-Za-z0-9._:/-]+$/, message: "仅支持字母、数字及 . _ : / -" },
            { validator: (_, value: string) => {
              const normalized = value?.trim();
              const builtInConflict = CHAT_MODELS.some((item) => item.value === normalized);
              const customConflict = customModels.some((item) => item.id !== editingModel?.id && item.modelId === normalized);
              return builtInConflict || customConflict ? Promise.reject(new Error("该模型 ID 已存在")) : Promise.resolve();
            } },
          ]}>
            <Input placeholder="例如：gpt-5-mini" autoComplete="off" />
          </Form.Item>
          <Form.Item name="description" label="说明（可选）" initialValue="" rules={[{ max: 100, message: "最多 100 个字符" }]}>
            <Input.TextArea rows={3} showCount maxLength={100} placeholder="简要说明适用场景，便于在对话条中识别" />
          </Form.Item>
          <Form.Item name="baseUrl" label="Base URL" extra="填写 OpenAI 兼容接口的基础地址，通常以 /v1 结尾" rules={[
            { required: true, whitespace: true, message: "请输入 Base URL" },
            { max: 2048, message: "Base URL 过长" },
            { pattern: /^https?:\/\/[^\s]+$/i, message: "请输入有效的 HTTP(S) 地址" },
          ]}>
            <Input placeholder="例如：https://api.openai.com/v1" autoComplete="url" />
          </Form.Item>
          <Form.Item name="apiKey" label="供应商 API Key（可选）" extra="本地无鉴权服务可留空；填写后仅保存在当前浏览器中" initialValue="" rules={[{ max: 4096, message: "API Key 过长" }]}>
            <Input.Password placeholder="sk-••••••••••••••••" autoComplete="new-password" />
          </Form.Item>
          <Alert type="warning" showIcon message="发送消息时，URL 与 Key 会通过当前 Harness 后端转发给模型服务；生产环境还需将主机加入 LLM_ALLOWED_BASE_HOSTS。" />
        </Form>
      </Modal>
    </div>
  );
}

function maskApiKey(value: string): string {
  if (!value) return "未配置";
  return `••••••••${value.slice(-4)}`;
}
