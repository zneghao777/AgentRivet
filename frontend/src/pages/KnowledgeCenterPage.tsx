// 知识库体系总览（KB_MANAGEMENT_SPEC §4.2）：context-cards 质感卡片网格
import { useCallback, useEffect, useState } from "react";
import {
  Button,
  Empty,
  Form,
  Input,
  Modal,
  Popconfirm,
  Skeleton,
  Switch,
  Tag,
  Upload,
  message,
} from "antd";
import { useNavigate } from "react-router-dom";
import { deleteData, getData, patchData, postData } from "@/api/http";
import type {
  KnowledgeBaseDetail,
  KnowledgeBaseItem,
  KnowledgeUploadResponse,
} from "@/types/api";
import { formatTime } from "@/utils/format";

const MAX_FILE_SIZE_MB = 50;
const ACCEPT_EXTS = ".md,.txt,.pdf,.docx";

export default function KnowledgeCenterPage() {
  const navigate = useNavigate();
  const [kbs, setKbs] = useState<KnowledgeBaseItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [uploadingKb, setUploadingKb] = useState<string | null>(null);
  const [togglingKb, setTogglingKb] = useState<string | null>(null);
  const [stats, setStats] = useState<Record<string, { docs: number; chunks: number; parsing: number }>>({});
  const [form] = Form.useForm();

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await getData<KnowledgeBaseItem[]>("/knowledge-bases");
      setKbs(data);
      // 并行拉取各库详情统计
      const results = await Promise.allSettled(
        data.map((kb) =>
          getData<KnowledgeBaseDetail>(`/knowledge-bases/${kb.kb_id}`)
        )
      );
      const next: Record<string, { docs: number; chunks: number; parsing: number }> = {};
      results.forEach((result, index) => {
        if (result.status === "fulfilled") {
          const kb = data[index];
          const docs = result.value.documents;
          next[kb.kb_id] = {
            docs: docs.length,
            chunks: kb.chunk_count,
            parsing: docs.filter((d) => d.parse_status === "parsing" || d.parse_status === "reparsing").length,
          };
        }
      });
      setStats(next);
    } catch (e) {
      message.error(e instanceof Error ? e.message : "加载知识库失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    // 解析中任务自动刷新
    const timer = window.setInterval(() => {
      void load();
    }, 5000);
    return () => window.clearInterval(timer);
  }, [load]);

  const toggleKb = async (kb: KnowledgeBaseItem, enabled: boolean) => {
    setTogglingKb(kb.kb_id);
    try {
      const updated = await patchData<KnowledgeBaseItem>(
        `/knowledge-bases/${kb.kb_id}/status`,
        { enabled }
      );
      setKbs((current) => current.map((entry) => (entry.kb_id === updated.kb_id ? updated : entry)));
      message.success(enabled ? "知识库已启用" : "知识库已停用");
    } catch (e) {
      message.error(e instanceof Error ? e.message : "更新状态失败");
    } finally {
      setTogglingKb(null);
    }
  };

  const createKb = async () => {
    const values = await form.validateFields();
    setCreating(true);
    try {
      await postData("/knowledge-bases", values);
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
      const data = await postData<KnowledgeUploadResponse>(
        `/knowledge-bases/${kb.kb_id}/documents/upload`,
        formData
      );
      message.success(`已上传 ${data.source_file}，后台解析中`);
      await load();
    } catch (e) {
      message.error(e instanceof Error ? e.message : "上传失败");
    } finally {
      setUploadingKb(null);
    }
  };

  return (
    <div className="page-scroll">
      <header className="page-header">
        <div className="page-header-icon">
          <span className="material-symbols-rounded">database</span>
        </div>
        <div className="page-header-copy">
          <h1>知识库</h1>
          <p>上传文档构建企业知识，让 Agent 基于知识回答并引用来源</p>
        </div>
        <Button
          type="primary"
          icon={<span className="material-symbols-rounded" style={{ fontSize: 16 }}>add</span>}
          onClick={() => setCreateOpen(true)}
        >
          新建知识库
        </Button>
        <Button icon={<span className="material-symbols-rounded" style={{ fontSize: 16 }}>refresh</span>} onClick={() => void load()}>
          刷新
        </Button>
      </header>

      {loading && kbs.length === 0 ? (
        <div className="kb-grid">
          {Array.from({ length: 6 }).map((_, index) => (
            <div className="kb-card-skeleton" key={index}>
              <Skeleton active avatar paragraph={{ rows: 3 }} />
            </div>
          ))}
        </div>
      ) : kbs.length === 0 ? (
        <Empty description="暂无知识库，点击右上角「新建知识库」开始" style={{ marginTop: 64 }} />
      ) : (
        <div className="kb-grid">
          {kbs.map((kb) => {
            const stat = stats[kb.kb_id] ?? { docs: kb.doc_count, chunks: kb.chunk_count, parsing: 0 };
            return (
              <article
                className="kb-card"
                key={kb.kb_id}
                onClick={() => navigate(`/knowledge/${kb.kb_id}`)}
              >
                <div className="kb-card-head">
                  <div className="kb-card-icon">
                    <span className="material-symbols-rounded">database</span>
                  </div>
                  <div className="kb-card-title">
                    <div className="kb-card-name">{kb.name}</div>
                    <Tag
                      color={kb.status === "active" ? "success" : "default"}
                      className="kb-card-status"
                    >
                      {kb.status === "active" ? "已启用" : "已停用"}
                    </Tag>
                  </div>
                  <Switch
                    size="small"
                    checked={kb.status === "active"}
                    loading={togglingKb === kb.kb_id}
                    onChange={(checked) => void toggleKb(kb, checked)}
                    onClick={(_checked, event) => event.stopPropagation()}
                    aria-label={`${kb.status === "active" ? "停用" : "启用"}知识库 ${kb.name}`}
                  />
                </div>
                <p className="kb-card-desc">{kb.description || "（无描述）"}</p>
                <div className="kb-card-stats">
                  <span><b>{stat.docs}</b> 文档</span>
                  <span><b>{stat.chunks}</b> 分块</span>
                  {stat.parsing > 0 ? (
                    <span className="kb-card-parsing">
                      <i className="spin" /> {stat.parsing} 解析中
                    </span>
                  ) : null}
                  <span className="kb-card-time">{formatTime(kb.updated_at ?? kb.created_at)}</span>
                </div>
                <div className="kb-card-actions" onClick={(event) => event.stopPropagation()}>
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
                      上传
                    </Button>
                  </Upload>
                  <Button size="small" type="primary" ghost onClick={() => navigate(`/knowledge/${kb.kb_id}`)}>
                    详情
                  </Button>
                  <Popconfirm
                    title="删除知识库"
                    description="将删除该知识库全部文档与分块，确定？"
                    okText="删除"
                    cancelText="取消"
                    okButtonProps={{ danger: true }}
                    onConfirm={() => void removeKb(kb)}
                  >
                    <Button size="small" danger>
                      <span className="material-symbols-rounded" style={{ fontSize: 15 }}>delete</span>
                    </Button>
                  </Popconfirm>
                </div>
              </article>
            );
          })}
        </div>
      )}

      <Modal
        title="新建知识库"
        open={createOpen}
        onOk={() => void createKb()}
        confirmLoading={creating}
        onCancel={() => setCreateOpen(false)}
        destroyOnHidden
      >
        <Form form={form} layout="vertical" initialValues={{ name: "", description: "" }}>
          <Form.Item name="name" label="名称" rules={[{ required: true, message: "请输入知识库名称" }]}>
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
