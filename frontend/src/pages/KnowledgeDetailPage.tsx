// 知识库详情（KB_MANAGEMENT_SPEC §4.3）：文档管理 / 分块管理 / 测试检索 + 溯源深链
import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { Button, Tag, message } from "antd";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { getData } from "@/api/http";
import type { KnowledgeBaseDetail, KnowledgeDocItem, KnowledgeIndexJob } from "@/types/api";
import DocumentTaskRows from "@/components/knowledge/DocumentTaskRows";
import ChunkRecordsTable from "@/components/knowledge/ChunkRecordsTable";
import TestRetrievePanel from "@/components/knowledge/TestRetrievePanel";
import KnowledgeEvalPanel from "@/components/knowledge/KnowledgeEvalPanel";
import KnowledgeModeBadge from "@/components/knowledge/KnowledgeModeBadge";
import IndexHealthBadge from "@/components/knowledge/IndexHealthBadge";
import KnowledgeUploadModal from "@/components/knowledge/KnowledgeUploadModal";
import KnowledgeSettingsPanel from "@/components/knowledge/KnowledgeSettingsPanel";
import KnowledgeOverviewPanel from "@/components/knowledge/KnowledgeOverviewPanel";
import { formatTime } from "@/utils/format";
import { InspectorField, ResponsiveInspector, StatusDot } from "@/components/common/Workspace";

type TabKey = "overview" | "documents" | "chunks" | "test" | "eval" | "graph" | "settings";

const TABS: { key: TabKey; label: string; icon: string }[] = [
  { key: "overview", label: "概览", icon: "dashboard" },
  { key: "documents", label: "文档", icon: "folder_open" },
  { key: "chunks", label: "分块", icon: "grid_view" },
  { key: "test", label: "测试检索", icon: "travel_explore" },
  { key: "graph", label: "知识图谱", icon: "account_tree" },
  { key: "eval", label: "检索评测", icon: "fact_check" },
  { key: "settings", label: "设置", icon: "settings" },
];

const GraphPanel = lazy(() => import("@/components/knowledge/GraphPanel"));

export default function KnowledgeDetailPage() {
  const { kbId = "" } = useParams<{ kbId: string }>();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const tabParam = searchParams.get("tab") as TabKey | null;
  const focusChunkId = useMemo(() => {
    const raw = searchParams.get("chunk");
    return raw && Number.isInteger(Number(raw)) ? Number(raw) : null;
  }, [searchParams]);
  const focusDocId = useMemo(() => {
    const raw = searchParams.get("doc");
    return raw && Number.isInteger(Number(raw)) ? Number(raw) : null;
  }, [searchParams]);

  const [detail, setDetail] = useState<KnowledgeBaseDetail | null>(null);
  const [jobs, setJobs] = useState<KnowledgeIndexJob[]>([]);
  const [loading, setLoading] = useState(false);
  const [tab, setTab] = useState<TabKey>(() => {
    if (tabParam && TABS.some((t) => t.key === tabParam)) return tabParam;
    return "documents";
  });
  const [showLocateTip, setShowLocateTip] = useState(false);
  const [selectedDocId, setSelectedDocId] = useState<number | null>(null);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [uploadOpen, setUploadOpen] = useState(false);

  const load = useCallback(async () => {
    if (!kbId) return;
    setLoading(true);
    try {
      const [data, jobData] = await Promise.all([
        getData<KnowledgeBaseDetail>(`/knowledge-bases/${kbId}`),
        getData<KnowledgeIndexJob[]>(`/knowledge-bases/${kbId}/jobs`),
      ]);
      setDetail(data);
      setJobs(jobData);
    } catch (e) {
      message.error(e instanceof Error ? e.message : "加载知识库失败");
    } finally {
      setLoading(false);
    }
  }, [kbId]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!jobs.some((job) => job.status === "queued" || job.status === "running")) return undefined;
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void load();
    }, 3000);
    return () => window.clearInterval(timer);
  }, [jobs, load]);

  // 深链优先打开对应页签。
  useEffect(() => {
    if (tabParam && TABS.some((item) => item.key === tabParam)) setTab(tabParam);
  }, [tabParam]);

  // 溯源提示条
  useEffect(() => {
    if (focusChunkId) {
      setShowLocateTip(true);
      const timer = window.setTimeout(() => setShowLocateTip(false), 4000);
      return () => window.clearTimeout(timer);
    }
    return undefined;
  }, [focusChunkId]);

  const switchTab = (key: TabKey) => {
    setTab(key);
    if (key === "chunks") {
      // 保留 chunk 深链参数；切到其他 tab 清理
      setSearchParams(
        key === "chunks" && focusChunkId
          ? { tab: "chunks", chunk: String(focusChunkId) }
          : { tab: key },
        { replace: true }
      );
    } else {
      setSearchParams({ tab: key }, { replace: true });
    }
  };

  const handlePreviewDoc = (doc: KnowledgeDocItem) => {
    // 进入分块 tab 并过滤该文档
    const params = new URLSearchParams({ tab: "chunks", doc: String(doc.doc_id) });
    setSearchParams(params, { replace: true });
    setTab("chunks");
  };

  const handleFocusHandled = useCallback(() => {
    // 定位已处理：清理 chunk 参数，避免刷新重复高亮
    const params = new URLSearchParams(searchParams);
    params.delete("chunk");
    params.delete("doc");
    params.set("tab", "chunks");
    setSearchParams(params, { replace: true });
  }, [searchParams, setSearchParams]);

  const kb = detail?.kb ?? null;
  const availableTabs = TABS.filter((item) => item.key !== "graph" || kb?.index_mode === "graph_enhanced");
  const documents = detail?.documents ?? [];
  const selectedDocument = documents.find((doc) => doc.doc_id === selectedDocId) ?? documents[0] ?? null;
  const parsingCount = documents.filter(
    (doc) => doc.parse_status === "parsing" || doc.parse_status === "reparsing"
  ).length;
  const activeJobs = jobs.filter((job) => job.status === "queued" || job.status === "running");

  useEffect(() => {
    if (kb?.index_mode === "standard" && tab === "graph") {
      setTab("overview");
      setSearchParams({ tab: "overview" }, { replace: true });
    }
  }, [kb?.index_mode, setSearchParams, tab]);

  return (
    <div className="page-scroll kb-detail-page">
      <header className="page-header">
        <div className="page-header-icon">
          <span className="material-symbols-rounded">database</span>
        </div>
        <div className="page-header-copy">
          <div className="kb-detail-breadcrumb">
            <button onClick={() => navigate("/knowledge")}>知识库</button>
            <span className="material-symbols-rounded" style={{ fontSize: 14 }}>chevron_right</span>
            <span>{kb?.name ?? "加载中…"}</span>
          </div>
          <p>
            {kb?.description || "管理文档解析、分块与向量，测试检索效果"}
            {kb ? <span className="kb-detail-time">更新于 {formatTime(kb.updated_at ?? kb.created_at)}</span> : null}
          </p>
          {kb ? <div className="kb-detail-badges"><KnowledgeModeBadge mode={kb.index_mode} /><IndexHealthBadge status={kb.index_status} prefix="文本" />{kb.index_mode === "graph_enhanced" ? <IndexHealthBadge status={kb.graph_status} prefix="图谱" /> : null}</div> : null}
        </div>
        <div className="kb-detail-header-actions">
          {activeJobs.length > 0 ? (
            <Tag color="processing" icon={<span className="material-symbols-rounded spin" style={{ fontSize: 12 }}>progress_activity</span>}>
              正在处理 {activeJobs.length} 个任务 · {activeJobs[0]?.stage}
            </Tag>
          ) : null}
          {kb ? <Button type="primary" onClick={() => setUploadOpen(true)} icon={<span className="material-symbols-rounded">upload_file</span>}>上传文档</Button> : null}
          <Button onClick={() => navigate("/knowledge")}>返回</Button>
        </div>
      </header>

      {loading && !detail ? (
        <div className="kb-detail-loading">加载中…</div>
      ) : !detail ? (
        <div className="kb-detail-empty">知识库不存在或无权访问</div>
      ) : (
        <div className="kb-detail-body">
          <nav className="kb-side-nav" role="tablist" aria-label="知识库详情功能区">
            {availableTabs.map((item) => (
              <button
                key={item.key}
                role="tab"
                aria-selected={tab === item.key}
                aria-controls="knowledge-tab-panel"
                className={`kb-side-nav-item${tab === item.key ? " active" : ""}`}
                onClick={() => switchTab(item.key)}
              >
                <span className="material-symbols-rounded">{item.icon}</span>
                {item.label}
                {item.key === "documents" && parsingCount > 0 ? <i className="kb-side-dot" /> : null}
              </button>
            ))}
          </nav>
          <section className="kb-detail-content" id="knowledge-tab-panel" role="tabpanel">
            {showLocateTip && focusChunkId ? (
              <div className="kb-locate-tip">
                <span className="material-symbols-rounded">near_me</span>
                已定位到来源分块 #{focusChunkId}
              </div>
            ) : null}

            {tab === "overview" && kb ? <KnowledgeOverviewPanel kb={kb} documents={documents} jobs={jobs} onOpenDocuments={() => switchTab("documents")} onOpenGraph={() => switchTab("graph")} /> : null}

            {tab === "documents" ? (
              <div className="knowledge-detail-master">
                <DocumentTaskRows kbId={kbId} kb={kb!} documents={documents} loading={loading} onChanged={load} onPreviewDoc={handlePreviewDoc} selectedDocId={selectedDocument?.doc_id ?? null} onSelectDoc={(doc) => { setSelectedDocId(doc.doc_id); setInspectorOpen(true); }} />
                <ResponsiveInspector open={inspectorOpen && Boolean(selectedDocument)} onClose={() => setInspectorOpen(false)} title={selectedDocument?.source_file ?? "文档详情"} subtitle={selectedDocument ? <StatusDot label={selectedDocument.parse_status === "parsed" ? "已就绪" : selectedDocument.parse_status} tone={selectedDocument.parse_status === "parsed" ? "success" : selectedDocument.parse_status === "failed" ? "danger" : "warning"} /> : undefined}>
                  {!selectedDocument ? <div className="kb-doc-empty"><span className="material-symbols-rounded">description</span><p>选择一个文档查看详情</p></div> : <><section className="inspector-section"><h3>文件与索引</h3><InspectorField label="文件名">{selectedDocument.source_file}</InspectorField><InspectorField label="文件大小">{selectedDocument.file_size.toLocaleString()} B</InspectorField><InspectorField label="解析 / 文本">{selectedDocument.parse_status} / {selectedDocument.vector_status}</InspectorField>{kb?.index_mode === "graph_enhanced" ? <InspectorField label="图谱 / 参与方式">{selectedDocument.graph_status} / {selectedDocument.graph_override}</InspectorField> : null}<InspectorField label="内容哈希" mono>{selectedDocument.content_hash?.slice(0, 16) || "—"}</InspectorField><InspectorField label="分块数">{selectedDocument.chunk_count}</InspectorField><InspectorField label="更新时间">{formatTime(selectedDocument.updated_at ?? selectedDocument.created_at)}</InspectorField><InspectorField label="文档 ID" mono>{selectedDocument.doc_id}</InspectorField></section>{selectedDocument.error || selectedDocument.vector_error || selectedDocument.graph_error ? <section className="inspector-section"><h3>最近错误</h3><p className="inspector-copy obs-err-text">{selectedDocument.graph_error || selectedDocument.vector_error || selectedDocument.error}</p><small>文本状态为“可检索”时，图谱失败不会影响标准问答。</small></section> : null}<section className="inspector-section inspector-button-stack"><Button block onClick={() => handlePreviewDoc(selectedDocument)} disabled={selectedDocument.vector_status !== "ready"}>查看分块</Button></section></>}
                </ResponsiveInspector>
              </div>
            ) : null}

            {tab === "chunks" ? (
              <ChunkRecordsTable
                kbId={kbId}
                documents={documents}
                focusChunkId={focusChunkId}
                focusDocId={focusDocId}
                onFocusHandled={handleFocusHandled}
              />
            ) : null}

            {tab === "test" && kb ? <TestRetrievePanel kbId={kbId} indexMode={kb.index_mode} /> : null}

            {tab === "graph" ? <Suspense fallback={<div className="kb-detail-loading">正在加载图谱工作区…</div>}><GraphPanel kbId={kbId} /></Suspense> : null}

            {tab === "eval" ? <KnowledgeEvalPanel kbId={kbId} /> : null}

            {tab === "settings" && kb ? <KnowledgeSettingsPanel kb={kb} onChanged={load} /> : null}
          </section>
        </div>
      )}
      <KnowledgeUploadModal kb={kb} open={uploadOpen} onClose={() => setUploadOpen(false)} onUploaded={load} />
    </div>
  );
}
