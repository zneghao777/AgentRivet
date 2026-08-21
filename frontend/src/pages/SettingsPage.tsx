import { useState } from "react";
import { Alert, Button, Form, Input, Select, message } from "antd";
import { postData } from "@/api/http";
import { StatusDot, WorkspaceHeader } from "@/components/common/Workspace";
import { useSettingsStore } from "@/stores/settingsStore";

type SettingsSection = "model" | "auth" | "about";

const SECTIONS: Array<{ key: SettingsSection; icon: string; label: string; description: string }> = [
  { key: "model", icon: "cloud", label: "模型连接", description: "模型与 API 基础地址" },
  { key: "auth", icon: "key", label: "API Key / 鉴权", description: "本机密钥与连接测试" },
  { key: "about", icon: "info", label: "关于", description: "版本与运行信息" },
];

export default function SettingsPage() {
  const { apiKey, setApiKey, model, setModel, baseUrl, setBaseUrl } = useSettingsStore();
  const [section, setSection] = useState<SettingsSection>("model");
  const [testing, setTesting] = useState(false);
  const [dirty, setDirty] = useState(false);

  const save = () => {
    setDirty(false);
    message.success("设置已保存到本地浏览器");
  };

  const testConnection = async () => {
    if (!apiKey) {
      message.warning("请先填写 API Key");
      return;
    }
    setTesting(true);
    try {
      await postData("/admin/test-connection", { api_key: apiKey, model });
      message.success("连接测试成功");
    } catch (error) {
      message.error(error instanceof Error ? error.message : "连接失败");
    } finally {
      setTesting(false);
    }
  };

  const markDirty = (update: () => void) => {
    update();
    setDirty(true);
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
            <div className="settings-section-heading"><div><h2>模型连接</h2><p>选择要使用的语言模型并配置接入参数</p></div></div>
            <Alert type="info" showIcon message="请确保 API Key 与模型匹配；本地代理当前仅代理 DeepSeek 系列。" />
            <Form layout="vertical" className="settings-form">
              <Form.Item label="模型" extra="选择提供响应的默认模型">
                <Select value={model} onChange={(value) => markDirty(() => setModel(value))} options={[{ value: "deepseek-chat", label: "deepseek-chat" }, { value: "deepseek-reasoner", label: "deepseek-reasoner" }]} />
              </Form.Item>
              <Form.Item label="Base URL" extra="兼容 OpenAI API 的服务地址">
                <Input value={baseUrl} onChange={(event) => markDirty(() => setBaseUrl(event.target.value))} />
              </Form.Item>
              <div className="connection-status-row"><span>连接状态</span><StatusDot label={apiKey ? "等待测试" : "尚未配置"} tone={apiKey ? "warning" : "neutral"} /><Button onClick={() => void testConnection()} loading={testing}>测试连接</Button></div>
            </Form>
          </> : null}
          {section === "auth" ? <>
            <div className="settings-section-heading"><div><h2>API Key / 鉴权</h2><p>密钥仅保存在当前浏览器中，不会出现在页面明文或日志里</p></div></div>
            <Form layout="vertical" className="settings-form">
              <Form.Item label="API Key" extra="用于请求当前模型服务">
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
    </div>
  );
}
