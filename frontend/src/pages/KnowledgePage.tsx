// 知识库页: 创建 / 上传文档 / 删除知识库，构建企业知识供 Agent 检索
import { useEffect, useState } from "react";
import {
  Button,
  Empty,
  Form,
  Input,
  Modal,
  Popconfirm,
  Switch,
  Tag,
  Upload,
  message,
} from "antd";
import { deleteData, getData, http } from "@/api/http";
import type { KnowledgeBaseItem, KnowledgeUploadResponse } from "@/types/api";

const MAX_FILE_SIZE_MB = 20;
const ACCEPT_EXTS = ".md,.txt,.pdf,.docx";

export default function KnowledgePage() {
  const [kbs, setKbs] = useState<KnowledgeBaseItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [uploadingKb, setUploadingKb] = useState<string | null>(null);
  const [togglingKb, setTogglingKb] = useState<string | null>(null);
  const [form] = Form.useForm();

  const load = async () => {
    setLoading(true);
    try {
      const data = await getData<KnowledgeBaseItem[]>("/knowledge-bases");
      setKbs(data);
    } catch (e) {
      message.error(e instanceof Error ? e.message : "加载知识库失败");
    } finally {
      setLoading(false);
    }
  };

  const toggleKb = async (kb: KnowledgeBaseItem, enabled: boolean) => {
    setTogglingKb(kb.kb_id);
    try {
      const updated = await http.patch<{ data: KnowledgeBaseItem }>(
        `/knowledge-bases/${kb.kb_id}/status`,
        { enabled }
      );
      const item = updated.data?.data;
      if (item) {
        setKbs((current) => current.map((entry) => entry.kb_id === item.kb_id ? item : entry));
      }
      message.success(enabled ? "知识库已启用" : "知识库已停用");
    } catch (e) {
      message.error(e instanceof Error ? e.message : "更新状态失败");
    } finally {
      setTogglingKb(null);
    }
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const createKb = async () => {
    const values = await form.validateFields();
    setCreating(true);
    try {
      await http.post("/knowledge-bases", values);
      message.success("知识库已创建");
      setCreateOpen(false);
      form.resetFields();
      await load();
    } catch (e) {
      message.error(e instanceof Error ? e.message : "创建失败");
    } finally {
      setCreating(false);
    }
  };

  const removeKb = async (kb: KnowledgeBaseItem) => {
    try {
      await deleteData(`/knowledge-bases/${kb.kb_id}`);
      message.success("已删除");
      await load();
    } catch (e) {
      message.error(e instanceof Error ? e.message : "删除失败");
    }
  };

  const uploadFile = async (kb: KnowledgeBaseItem, file: File) => {
    if (file.size > MAX_FILE_SIZE_MB * 1024 * 1024) {
      message.error(`文件超过 ${MAX_FILE_SIZE_MB}MB 上限`);
      return;
    }
    setUploadingKb(kb.kb_id);
    try {
      const formData = new FormData();
      formData.append("file", file);
      formData.append("replace", "true");
      const resp = await http.post<{ data: KnowledgeUploadResponse }>(
        `/knowledge-bases/${kb.kb_id}/documents`,
        formData
      );
      message.success(`已上传 ${resp.data?.data?.source_file}（${resp.data?.data?.chunk_count} 分块）`);
      await load();
    } catch (e) {
      message.error(e instanceof Error ? e.message : "上传失败");
    } finally {
      setUploadingKb(null);
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
      <header
        style={{
          display: "flex",
          alignItems: "center",
          gap: 14,
          padding: "14px 22px",
          marginBottom: 24,
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
            database
          </span>
        </div>
        <div style={{ flex: 1 }}>
          <div
            style={{
              fontFamily: "var(--font-display)",
              fontSize: 20,
              fontWeight: 700,
              color: "var(--gray-900)",
            }}
          >
            知识库
          </div>
          <div
            style={{
              fontSize: 12,
              color: "var(--gray-500)",
              marginTop: 4,
            }}
          >
            在这里统一管理知识库，启用后 Agent 会按需检索相关内容
          </div>
        </div>
        <Button
          type="primary"
          icon={<span className="material-symbols-rounded" style={{ fontSize: 16 }}>add</span>}
          onClick={() => setCreateOpen(true)}
        >
          新建知识库
        </Button>
        <Button onClick={load}>刷新</Button>
      </header>

      {!loading && kbs.length === 0 && <Empty description="暂无知识库" />}

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))",
          gap: 12,
        }}
      >
        {kbs.map((kb) => (
          <article
            key={kb.kb_id}
            style={{
              background: "var(--bg-card)",
              borderRadius: "var(--r-xl)",
              border: "var(--border-soft)",
              padding: 18,
              boxShadow: "var(--shadow-sm)",
              transition: "all var(--dur-base) var(--ease-out)",
              display: "flex",
              flexDirection: "column",
              gap: 10,
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <div
                style={{
                  width: 34,
                  height: 34,
                  borderRadius: 10,
                  background:
                    "linear-gradient(135deg, var(--brand-500), var(--brand-400))",
                  color: "#fff",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  flexShrink: 0,
                }}
              >
                <span className="material-symbols-rounded" style={{ fontSize: 18 }}>
                  database
                </span>
              </div>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div
                  style={{
                    fontSize: 14,
                    fontWeight: 600,
                    color: "var(--gray-900)",
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                  }}
                >
                  {kb.name}
                </div>
                <Tag
                  color={kb.status === "active" ? "success" : "default"}
                  style={{ marginTop: 4, borderRadius: "var(--r-full)" }}
                >
                  {kb.status === "active" ? "已启用" : "已停用"}
                </Tag>
              </div>
              <Switch
                size="small"
                checked={kb.status === "active"}
                loading={togglingKb === kb.kb_id}
                onChange={(checked) => void toggleKb(kb, checked)}
                aria-label={`${kb.status === "active" ? "停用" : "启用"}知识库 ${kb.name}`}
              />
              <Upload
                accept={ACCEPT_EXTS}
                showUploadList={false}
                beforeUpload={(file) => {
                  void uploadFile(kb, file);
                  return false;
                }}
              >
                <Button size="small" loading={uploadingKb === kb.kb_id} title="上传文档">
                  <span className="material-symbols-rounded" style={{ fontSize: 15 }}>upload</span>
                </Button>
              </Upload>
              <Popconfirm
                title="删除知识库"
                description="将删除该知识库全部文档与分块，确定？"
                onConfirm={() => removeKb(kb)}
              >
                <Button size="small" danger>
                  <span className="material-symbols-rounded" style={{ fontSize: 15 }}>delete</span>
                </Button>
              </Popconfirm>
            </div>
            <div
              style={{
                fontSize: 12,
                color: "var(--gray-600)",
                lineHeight: 1.6,
                minHeight: 36,
              }}
            >
              {kb.description || "（无描述）"}
            </div>
            <div className="kb-stat">
              <span>
                <b>{kb.doc_count}</b> 文档
              </span>
              <span>
                <b>{kb.chunk_count}</b> 分块
              </span>
              <span className="kb-stat-hint">上传 {ACCEPT_EXTS}</span>
            </div>
          </article>
        ))}
      </div>

      <Modal
        title="新建知识库"
        open={createOpen}
        onOk={() => void createKb()}
        confirmLoading={creating}
        onCancel={() => setCreateOpen(false)}
        destroyOnHidden
      >
        <Form form={form} layout="vertical" initialValues={{ name: "", description: "" }}>
          <Form.Item
            name="name"
            label="名称"
            rules={[{ required: true, message: "请输入知识库名称" }]}
          >
            <Input placeholder="如：员工手册" maxLength={128} />
          </Form.Item>
          <Form.Item name="description" label="描述">
            <Input.TextArea placeholder="这个知识库包含什么内容？（可选）" rows={3} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
