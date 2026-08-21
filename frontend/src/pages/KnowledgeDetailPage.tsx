// 知识库详情（KB_MANAGEMENT_SPEC §4.3）：文档管理 / 分块管理 / 测试检索 + 溯源深链
import { useCallback, useEffect, useMemo, useState } from "react";
import { Button, Tag, message } from "antd";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { getData } from "@/api/http";
import type { KnowledgeBaseDetail, KnowledgeDocItem } from "@/types/api";
import DocumentTaskRows from "@/components/knowledge/DocumentTaskRows";
import ChunkRecordsTable from "@/components/knowledge/ChunkRecordsTable";
import TestRetrievePanel from "@/components/knowledge/TestRetrievePanel";
import KnowledgeEvalPanel from "@/components/knowledge/KnowledgeEvalPanel";
import { formatTime } from "@/utils/format";
import { InspectorField, ResponsiveInspector, StatusDot } from "@/components/common/Workspace";

type TabKey = "info" | "documents" | "chunks" | "test" | "eval";

const TABS: { key: TabKey; label: string; icon: string }[] = [
  { key: "info", label: "知识库信息", icon: "info" },
  { key: "documents", label: "文档管理", icon: "folder_open" },
  { key: "chunks", label: "分块管理", icon: "grid_view" },
  { key: "test", label: "测试检索", icon: "travel_explore" },
  { key: "eval", label: "检索评测", icon: "fact_check" },
];

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
  const [loading, setLoading] = useState(false);
  const [tab, setTab] = useState<TabKey>(() => {
    if (tabParam && TABS.some((t) => t.key === tabParam)) return tabParam;
    return "documents";
  });
  const [showLocateTip, setShowLocateTip] = useState(false);
  const [selectedDocId, setSelectedDocId] = useState<number | null>(null);
  const [inspectorOpen, setInspectorOpen] = useState(false);

  const load = useCallback(async () => {
    if (!kbId) return;
    setLoading(true);
    try {
      const data = await getData<KnowledgeBaseDetail>(`/knowledge-bases/${kbId}`);
      setDetail(data);
    } catch (e) {
      message.error(e instanceof Error ? e.message : "加载知识库失败");
    } finally {
      setLoading(false);
    }
  }, [kbId]);

  useEffect(() => {
    void load();
  }, [load]);

  // 溯源深链：tab=chunks 自动切到分块管理
  useEffect(() => {
    if (tabParam === "chunks") setTab("chunks");
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
  const documents = detail?.documents ?? [];
  const selectedDocument = documents.find((doc) => doc.doc_id === selectedDocId) ?? documents[0] ?? null;
  const parsingCount = documents.filter(
    (doc) => doc.parse_status === "parsing" || doc.parse_status === "reparsing"
  ).length;

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
        </div>
        {parsingCount > 0 ? (
          <Tag color="processing" icon={<span className="material-symbols-rounded spin" style={{ fontSize: 12 }}>progress_activity</span>}>
            {parsingCount} 个文档解析中
          </Tag>
        ) : null}
        <Button onClick={() => navigate("/knowledge")}>返回</Button>
      </header>

      {loading && !detail ? (
        <div className="kb-detail-loading">加载中…</div>
      ) : !detail ? (
        <div className="kb-detail-empty">知识库不存在或无权访问</div>
      ) : (
        <div className="kb-detail-body">
          <nav className="kb-side-nav" role="tablist" aria-label="知识库详情功能区">
            {TABS.map((item) => (
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

            {tab === "info" && kb ? (
              <div className="kb-info-panel">
                <div className="kb-info-row">
                  <span className="kb-info-label">知识库 ID</span>
                  <code>{kb.kb_id}</code>
                </div>
                <div className="kb-info-row">
                  <span className="kb-info-label">状态</span>
                  <Tag color={kb.status === "active" ? "success" : "default"}>
                    {kb.status === "active" ? "已启用" : "已停用"}
                  </Tag>
                </div>
                <div className="kb-info-row">
                  <span className="kb-info-label">文档 / 分块</span>
                  <span>{documents.length} 文档 · {kb.chunk_count} 分块</span>
                </div>
                <div className="kb-info-row">
                  <span className="kb-info-label">创建时间</span>
                  <span>{formatTime(kb.created_at)}</span>
                </div>
                <div className="kb-info-hint">
                  <span className="material-symbols-rounded">lightbulb</span>
                  知识库启用后，对话中 Agent 会通过 knowledge_search 工具按需检索其中的分块，
                  回答将标注可点击的来源引用。
                </div>
              </div>
            ) : null}

            {tab === "documents" ? (
              <div className="knowledge-detail-master">
                <DocumentTaskRows kbId={kbId} documents={documents} loading={loading} onChanged={load} onPreviewDoc={handlePreviewDoc} selectedDocId={selectedDocument?.doc_id ?? null} onSelectDoc={(doc) => { setSelectedDocId(doc.doc_id); setInspectorOpen(true); }} />
                <ResponsiveInspector open={inspectorOpen && Boolean(selectedDocument)} onClose={() => setInspectorOpen(false)} title={selectedDocument?.source_file ?? "文档详情"} subtitle={selectedDocument ? <StatusDot label={selectedDocument.parse_status === "parsed" ? "已就绪" : selectedDocument.parse_status} tone={selectedDocument.parse_status === "parsed" ? "success" : selectedDocument.parse_status === "failed" ? "danger" : "warning"} /> : undefined}>
                  {!selectedDocument ? <div className="kb-doc-empty"><span className="material-symbols-rounded">description</span><p>选择一个文档查看详情</p></div> : <><section className="inspector-section"><h3>文件信息</h3><InspectorField label="文件名">{selectedDocument.source_file}</InspectorField><InspectorField label="文件大小">{selectedDocument.file_size.toLocaleString()} B</InspectorField><InspectorField label="解析来源">{selectedDocument.parse_source || "—"}</InspectorField><InspectorField label="分块数">{selectedDocument.chunk_count}</InspectorField><InspectorField label="更新时间">{formatTime(selectedDocument.updated_at ?? selectedDocument.created_at)}</InspectorField><InspectorField label="文档 ID" mono>{selectedDocument.doc_id}</InspectorField></section>{selectedDocument.error ? <section className="inspector-section"><h3>解析错误</h3><p className="inspector-copy obs-err-text">{selectedDocument.error}</p></section> : null}<section className="inspector-section inspector-button-stack"><Button block onClick={() => handlePreviewDoc(selectedDocument)} disabled={selectedDocument.parse_status !== "parsed"}>查看分块</Button></section></>}
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

            {tab === "test" ? <TestRetrievePanel kbId={kbId} /> : null}

            {tab === "eval" ? <KnowledgeEvalPanel kbId={kbId} /> : null}
          </section>
        </div>
      )}
    </div>
  );
}
