// 文档 / 解析任务列表（KB_MANAGEMENT_SPEC §4.3-A）：task-rows 动效、状态徽标、轮询
import { useEffect, useRef, useState } from "react";
import { Button, Popconfirm, Tag, Tooltip, message } from "antd";
import { deleteData, postData } from "@/api/http";
import type {
  KnowledgeDocItem,
  KnowledgeBaseItem,
} from "@/types/api";
import { formatTime } from "@/utils/format";
import PipelineStatus from "./PipelineStatus";
import KnowledgeUploadModal from "./KnowledgeUploadModal";

function fileSizeLabel(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${bytes} B`;
}

export default function DocumentTaskRows({
  kbId,
  kb,
  documents,
  loading,
  onChanged,
  onPreviewDoc,
  selectedDocId,
  onSelectDoc,
}: {
  kbId: string;
  kb: KnowledgeBaseItem;
  documents: KnowledgeDocItem[];
  loading: boolean;
  onChanged: () => void;
  onPreviewDoc: (doc: KnowledgeDocItem) => void;
  selectedDocId?: number | null;
  onSelectDoc?: (doc: KnowledgeDocItem) => void;
}) {
  const [uploadOpen, setUploadOpen] = useState(false);
  const [busyDoc, setBusyDoc] = useState<number | null>(null);
  const timerRef = useRef<number | null>(null);

  const hasRunning = documents.some((doc) =>
    ["parsing", "reparsing"].includes(doc.parse_status) ||
    ["pending", "building"].includes(doc.vector_status) ||
    ["building"].includes(doc.graph_status)
  );

  // 解析状态轮询：有解析中任务时 3s 刷新
  useEffect(() => {
    if (hasRunning && timerRef.current === null) {
      timerRef.current = window.setInterval(() => {
        if (document.visibilityState === "visible") onChanged();
      }, 3000);
    }
    if (!hasRunning && timerRef.current !== null) {
      window.clearInterval(timerRef.current);
      timerRef.current = null;
    }
    return () => {
      if (timerRef.current !== null) {
        window.clearInterval(timerRef.current);
        timerRef.current = null;
      }
    };
  }, [hasRunning, onChanged]);

  const retry = async (doc: KnowledgeDocItem) => {
    setBusyDoc(doc.doc_id);
    try {
      await postData(`/knowledge-bases/${kbId}/documents/${doc.doc_id}/retry`);
      message.success("已重试失败阶段");
      onChanged();
    } catch (e) {
      message.error(e instanceof Error ? e.message : "重试失败");
    } finally {
      setBusyDoc(null);
    }
  };

  const reparse = async (doc: KnowledgeDocItem) => {
    setBusyDoc(doc.doc_id);
    try {
      await postData(`/knowledge-bases/${kbId}/documents/${doc.doc_id}/reparse`);
      message.success("旧证据已清理，正在重新解析");
      onChanged();
    } catch (error) {
      message.error(error instanceof Error ? error.message : "重新解析失败");
    } finally {
      setBusyDoc(null);
    }
  };

  const removeDoc = async (doc: KnowledgeDocItem) => {
    try {
      await deleteData(`/knowledge-bases/${kbId}/documents/${doc.doc_id}`);
      message.success(`已删除 ${doc.source_file}`);
      onChanged();
    } catch (e) {
      message.error(e instanceof Error ? e.message : "删除失败");
    }
  };

  return (
    <div className="kb-doc-tasks">
      <div className="kb-panel-toolbar">
        <div className="kb-panel-title">
          <span className="material-symbols-rounded">folder_open</span>
          <b>文档管理</b>
          <Tag bordered={false}>{documents.length} 个文档</Tag>
        </div>
        <Button type="primary" size="small" onClick={() => setUploadOpen(true)} icon={<span className="material-symbols-rounded" style={{ fontSize: 15 }}>upload_file</span>}>上传文档</Button>
      </div>

      {documents.length === 0 ? (
        <div className="kb-doc-empty">
          <span className="material-symbols-rounded">upload_file</span>
          <p>还没有文档。上传 .md / .txt / .pdf / .docx，系统将自动解析并向量化入库。</p>
        </div>
      ) : (
        <div className="kb-doc-list">
          {documents.map((doc) => {
            const running = ["parsing", "reparsing"].includes(doc.parse_status) || doc.vector_status === "building" || doc.graph_status === "building";
            const failed = doc.parse_status === "failed" || doc.vector_status === "failed" || doc.graph_status === "failed";
            return (
              <div className={`kb-doc-row${running ? " running" : ""}${selectedDocId === doc.doc_id ? " selected" : ""}`} key={doc.doc_id} role={onSelectDoc ? "option" : undefined} aria-selected={onSelectDoc ? selectedDocId === doc.doc_id : undefined} onClick={() => onSelectDoc?.(doc)} tabIndex={onSelectDoc ? (selectedDocId === doc.doc_id ? 0 : -1) : undefined} onKeyDown={(event) => { if (onSelectDoc && (event.key === "Enter" || event.key === " ")) { event.preventDefault(); onSelectDoc(doc); } }}>
                <div className="kb-doc-icon">
                  <span className="material-symbols-rounded">{doc.source_file.endsWith(".pdf") ? "picture_as_pdf" : "description"}</span>
                </div>
                <div className="kb-doc-main">
                  <div className="kb-doc-name" title={doc.source_file}>
                    {doc.source_file}
                    <small>{fileSizeLabel(doc.file_size)}</small>
                  </div>
                  <div className="kb-doc-sub">
                    <PipelineStatus doc={doc} showGraph={kb.index_mode === "graph_enhanced"} />
                    {doc.parse_source === "glm" ? <Tag color="purple" bordered={false}>GLM 解析</Tag> : null}
                    {doc.parse_source === "local" ? <Tag bordered={false}>本地解析</Tag> : null}
                    {doc.parse_status === "parsed" ? <span className="kb-doc-chunks">{doc.chunk_count} 分块</span> : null}
                    <span className="kb-doc-time">{formatTime(doc.updated_at ?? doc.created_at)}</span>
                  </div>
                </div>
                <div className="kb-doc-actions" onClick={(event) => event.stopPropagation()}>
                  {failed ? (
                    <Tooltip title={doc.graph_error || doc.vector_error || doc.error || "索引失败"}>
                      <Button size="small" type="text" danger onClick={() => void retry(doc)} loading={busyDoc === doc.doc_id}>
                        <span className="material-symbols-rounded" style={{ fontSize: 15 }}>refresh</span>
                        重试
                      </Button>
                    </Tooltip>
                  ) : (
                    <Button
                      size="small"
                      type="text"
                      loading={busyDoc === doc.doc_id}
                      onClick={() => void reparse(doc)}
                      title="重新解析并更新全部证据"
                    >
                      <span className="material-symbols-rounded" style={{ fontSize: 15 }}>refresh</span>
                    </Button>
                  )}
                  {doc.parse_status === "parsed" ? (
                    <Button size="small" type="text" onClick={() => onPreviewDoc(doc)} title="分块预览">
                      <span className="material-symbols-rounded" style={{ fontSize: 15 }}>view_list</span>
                    </Button>
                  ) : null}
                  <Popconfirm
                    title="删除该文档？"
                    description={`将删除 ${doc.source_file}、分块、向量、实体、关系、事件、社区归属和历史引用目标。`}
                    okText="删除"
                    cancelText="取消"
                    okButtonProps={{ danger: true }}
                    onConfirm={() => void removeDoc(doc)}
                  >
                    <Button size="small" type="text" danger title="删除文档">
                      <span className="material-symbols-rounded" style={{ fontSize: 15 }}>delete</span>
                    </Button>
                  </Popconfirm>
                </div>
              </div>
            );
          })}
        </div>
      )}
      {loading ? <div className="kb-loading-hint">加载中…</div> : null}
      <KnowledgeUploadModal kb={kb} open={uploadOpen} onClose={() => setUploadOpen(false)} onUploaded={onChanged} />
    </div>
  );
}
