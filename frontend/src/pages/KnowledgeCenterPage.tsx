// 知识库体系总览（KB_MANAGEMENT_SPEC §4.2）：context-cards 质感卡片网格
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Button,
  Empty,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Skeleton,
  Switch,
  Upload,
  message,
} from "antd";
import { useNavigate } from "react-router-dom";
import { deleteData, getData, patchData, postData } from "@/api/http";
import type { KnowledgeBaseItem, KnowledgeUploadResponse } from "@/types/api";
import { formatTime } from "@/utils/format";
import { InspectorField, MetricStrip, ResponsiveInspector, StatusDot, WorkspaceHeader } from "@/components/common/Workspace";

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
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [keyword, setKeyword] = useState("");
  const [statusFilter, setStatusFilter] = useState<"all" | "active" | "disabled">("all");
  const [form] = Form.useForm();

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await getData<KnowledgeBaseItem[]>("/knowledge-bases");
      setKbs(data);
      setSelectedId((current) => current && data.some((item) => item.kb_id === current) ? current : data[0]?.kb_id ?? null);
    } catch (e) {
      message.error(e instanceof Error ? e.message : "加载知识库失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
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

  const visibleKbs = useMemo(() => {
    const query = keyword.trim().toLowerCase();
    return kbs.filter((kb) => {
      const matchesStatus = statusFilter === "all" || kb.status === statusFilter;
      const matchesQuery = !query || `${kb.name} ${kb.description ?? ""}`.toLowerCase().includes(query);
      return matchesStatus && matchesQuery;
    });
  }, [kbs, keyword, statusFilter]);
  const selected = kbs.find((kb) => kb.kb_id === selectedId) ?? null;
  const totalDocs = kbs.reduce((sum, kb) => sum + kb.doc_count, 0);
  const totalChunks = kbs.reduce((sum, kb) => sum + kb.chunk_count, 0);

  return (
    <div className="page-scroll workspace-page knowledge-page">
      <WorkspaceHeader icon="database" title="知识库" description="上传文档构建企业知识，让 Agent 基于知识回答并引用来源" actions={<><Button type="primary" icon={<span className="material-symbols-rounded">add</span>} onClick={() => setCreateOpen(true)}>新建知识库</Button><Button icon={<span className="material-symbols-rounded">refresh</span>} onClick={() => void load()}>刷新</Button></>} />
      <MetricStrip items={[{ label: "知识库", value: kbs.length, meta: `已启用 ${kbs.filter((kb) => kb.status === "active").length}` }, { label: "文档", value: totalDocs, meta: "全部知识来源", tone: "teal" }, { label: "分块", value: totalChunks, meta: "可检索内容片段", tone: "warning" }]} />
      <div className="workspace-toolbar"><Input allowClear prefix={<span className="material-symbols-rounded">search</span>} placeholder="搜索知识库名称或描述" value={keyword} onChange={(event) => setKeyword(event.target.value)} /><Select value={statusFilter} onChange={setStatusFilter} options={[{ value: "all", label: "全部状态" }, { value: "active", label: "已启用" }, { value: "disabled", label: "已停用" }]} /></div>
      <div className="workspace-master-detail">
        <section className="data-surface knowledge-master">
          <div className="data-list-header knowledge-grid-row"><span>知识库名称 / 描述</span><span>状态</span><span className="numeric">文档</span><span className="numeric">分块</span><span>最后更新</span></div>
          {loading && kbs.length === 0 ? <Skeleton active paragraph={{ rows: 6 }} /> : null}
          {!loading && visibleKbs.length === 0 ? <Empty description="暂无匹配的知识库" /> : null}
          {visibleKbs.map((kb) => { const active = kb.kb_id === selectedId; return (
            <button type="button" key={kb.kb_id} className={`data-list-row knowledge-grid-row${active ? " selected" : ""}`} onClick={() => { setSelectedId(kb.kb_id); setInspectorOpen(true); }} onDoubleClick={() => navigate(`/knowledge/${kb.kb_id}`)} aria-selected={active}>
              <span className="knowledge-primary-cell"><span className="row-leading-icon material-symbols-rounded">database</span><span><b>{kb.name}</b><small>{kb.description || "暂无描述"}</small></span></span>
              <StatusDot label={kb.status === "active" ? "已启用" : "已停用"} tone={kb.status === "active" ? "success" : "neutral"} />
              <span className="numeric">{kb.doc_count}</span><span className="numeric">{kb.chunk_count}</span><span className="subtle-label">{formatTime(kb.updated_at ?? kb.created_at)}</span>
            </button>
          ); })}
          <footer className="data-list-footer">共 {visibleKbs.length} 个知识库</footer>
        </section>
        <ResponsiveInspector open={inspectorOpen && Boolean(selected)} onClose={() => setInspectorOpen(false)} title={selected?.name ?? "知识库详情"} subtitle={selected ? <StatusDot label={selected.status === "active" ? "已启用" : "已停用"} tone={selected.status === "active" ? "success" : "neutral"} /> : undefined} actions={selected ? <Switch checked={selected.status === "active"} loading={togglingKb === selected.kb_id} onChange={(checked) => void toggleKb(selected, checked)} /> : undefined}>
          {!selected ? <Empty description="选择一个知识库查看详情" /> : <><section className="inspector-section"><h3>基本信息</h3><p className="inspector-copy">{selected.description || "暂无描述"}</p><InspectorField label="知识库 ID" mono>{selected.kb_id}</InspectorField><InspectorField label="创建时间">{formatTime(selected.created_at)}</InspectorField><InspectorField label="最后更新">{formatTime(selected.updated_at ?? selected.created_at)}</InspectorField></section><section className="inspector-section"><h3>统计信息</h3><InspectorField label="文档数">{selected.doc_count}</InspectorField><InspectorField label="分块数">{selected.chunk_count}</InspectorField></section><section className="inspector-section inspector-button-stack"><Upload accept={ACCEPT_EXTS} showUploadList={false} beforeUpload={(file) => { void uploadFile(selected, file); return false; }}><Button block loading={uploadingKb === selected.kb_id} icon={<span className="material-symbols-rounded">upload</span>}>上传文档</Button></Upload><Button block type="primary" onClick={() => navigate(`/knowledge/${selected.kb_id}`)}>进入详情</Button><Popconfirm title="删除知识库" description="将删除全部文档与分块，确定？" okText="删除" cancelText="取消" okButtonProps={{ danger: true }} onConfirm={() => void removeKb(selected)}><Button block danger>删除知识库</Button></Popconfirm></section></>}
        </ResponsiveInspector>
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
