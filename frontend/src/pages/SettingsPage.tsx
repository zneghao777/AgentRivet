// 设置页: 与新主题一致的设置管理
import { useState } from "react";
import {
  Alert,
  Button,
  Form,
  Input,
  Select,
  Space,
  Tag,
  Typography,
  message,
} from "antd";
import { useSettingsStore } from "@/stores/settingsStore";
import { postData } from "@/api/http";

const { Text } = Typography;

const Section = ({
  icon,
  title,
  desc,
  children,
}: {
  icon: string;
  title: string;
  desc: string;
  children: React.ReactNode;
}) => (
  <section
    style={{
      background: "var(--bg-card)",
      borderRadius: "var(--r-xl)",
      border: "var(--border-soft)",
      boxShadow: "var(--shadow-sm)",
      padding: 24,
      marginBottom: 16,
    }}
  >
    <div
      style={{
        display: "flex",
        alignItems: "flex-start",
        gap: 12,
        marginBottom: 18,
      }}
    >
      <div
        style={{
          width: 38,
          height: 38,
          borderRadius: 10,
          background:
            "linear-gradient(135deg, var(--brand-50), rgba(0,184,169,0.10))",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          flexShrink: 0,
        }}
      >
        <span
          className="material-symbols-rounded"
          style={{
            color: "var(--brand-500)",
            fontSize: 20,
          }}
        >
          {icon}
        </span>
      </div>
      <div style={{ flex: 1 }}>
        <div
          style={{
            fontSize: 15,
            fontWeight: 600,
            color: "var(--gray-900)",
          }}
        >
          {title}
        </div>
        <div style={{ fontSize: 12, color: "var(--gray-500)", marginTop: 2 }}>
          {desc}
        </div>
      </div>
    </div>
    {children}
  </section>
);

export default function SettingsPage() {
  const { apiKey, setApiKey, model, setModel, baseUrl, setBaseUrl } =
    useSettingsStore();
  const [testing, setTesting] = useState(false);

  const save = () => {
    message.success("设置已保存");
  };

  const testConnection = async () => {
    if (!apiKey) {
      message.warning("请先填写 API Key");
      return;
    }
    setTesting(true);
    try {
      await postData("/admin/test-connection", {
        api_key: apiKey,
        model,
      });
      message.success("连接测试成功");
    } catch (e) {
      message.error(e instanceof Error ? e.message : "连接失败");
    } finally {
      setTesting(false);
    }
  };

  return (
    <div
      style={{
        flex: 1,
        minWidth: 0,
        height: "100%",
        overflowY: "auto",
        padding: "28px 32px 60px",
      }}
    >
      <div style={{ marginBottom: 24 }}>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 14,
            padding: "14px 22px",
            background:
              "linear-gradient(135deg, rgba(0,80,203,0.06), rgba(0,184,169,0.04))",
            borderRadius: "var(--r-2xl)",
            border: "1px solid var(--brand-100)",
          }}
        >
          <div
            style={{
              width: 44,
              height: 44,
              borderRadius: 12,
              background:
                "linear-gradient(135deg, var(--brand-500), var(--brand-400))",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              boxShadow: "var(--shadow-brand)",
            }}
          >
            <span
              className="material-symbols-rounded icon-filled"
              style={{ color: "#fff", fontSize: 22 }}
            >
              settings
            </span>
          </div>
          <div>
            <div
              style={{
                fontFamily: "var(--font-display)",
                fontSize: 20,
                fontWeight: 700,
                color: "var(--gray-900)",
              }}
            >
              系统设置
            </div>
            <div
              style={{
                fontSize: 12,
                color: "var(--gray-500)",
                marginTop: 4,
              }}
            >
              管理你的模型、密钥、技能开关
            </div>
          </div>
        </div>
      </div>

      <Section icon="cloud" title="模型连接" desc="选择要使用的语言模型与接入参数">
        <Alert
          type="info"
          showIcon
          message="请确保 API Key 与模型匹配，本地代理仅代理 deepseek 系列。"
          style={{ marginBottom: 16, borderRadius: "var(--r-md)" }}
        />
        <Form layout="vertical" size="large">
          <Form.Item label="模型">
            <Select
              value={model}
              onChange={setModel}
              options={[
                { value: "deepseek-chat", label: "deepseek-chat" },
                { value: "deepseek-reasoner", label: "deepseek-reasoner" },
              ]}
            />
          </Form.Item>
          <Form.Item label="Base URL">
            <Input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} />
          </Form.Item>
          <Form.Item label="API Key">
            <Input.Password
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="sk-..."
            />
          </Form.Item>
          <Space>
            <Button type="primary" onClick={save}>
              保存设置
            </Button>
            <Button onClick={testConnection} loading={testing}>
              测试连接
            </Button>
          </Space>
        </Form>
      </Section>

      <Section icon="info" title="关于" desc="版本与构建信息">
        <Space direction="vertical" size="small" style={{ width: "100%" }}>
          <div>
            <Text type="secondary">产品名称</Text>
            <div
              style={{
                fontSize: 14,
                color: "var(--gray-800)",
                fontWeight: 500,
                marginTop: 2,
              }}
            >
              Harness · 企业级 Agent 工作台
            </div>
          </div>
          <div>
            <Text type="secondary">运行模式</Text>
            <div style={{ marginTop: 2 }}>
              <Tag color="processing">研发环境</Tag>
            </div>
          </div>
        </Space>
      </Section>
    </div>
  );
}
