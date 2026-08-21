// 文档 / 解析任务列表（KB_MANAGEMENT_SPEC §4.3-A）：task-rows 动效、状态徽标、轮询
import { useCallback, useEffect, useRef, useState } from "react";
import { Button, Popconfirm, Tag, Tooltip, Upload, message } from "antd";
import { deleteData, postData } from "@/api/http";
import type {
  KnowledgeDocItem,
  KnowledgeUploadResponse,
} from "@/types/api";
import { formatTime } from "@/utils/format";

const ACCEPT_EXTS = ".md,.txt,.pdf,.docx";
const MAX_FILE_SIZE_MB = 50;

const PARSE_STATUS_META: Record<
  KnowledgeDocItem["parse_status"],
  { color: string; label: string; icon: string }
> = {
  pending: { color: "default", label: "等待解析", icon: "schedule" },
  parsing: { color: "processing", label: "解析中…", icon: "progress_activity" },
  reparsing: { color: "processing", label: "重新解析…", icon: "progress_activity" },
  parsed: { color: "success", label: "已就绪", icon: "check_circle" },
  failed: { color: "error", label: "解析失败", icon: "error" },
};

function fileSizeLabel(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${bytes} B`;
}

export default function DocumentTaskRows({
  kbId,
  documents,
  loading,
  onChanged,
  onPreviewDoc,
  selectedDocId,
  onSelectDoc,
}: {
  kbId: string;
  documents: KnowledgeDocItem[];
  loading: boolean;
  onChanged: () => void;
  onPreviewDoc: (doc: KnowledgeDocItem) => void;
  selectedDocId?: number | null;
  onSelectDoc?: (doc: KnowledgeDocItem) => void;
}) {
  const [uploading, setUploading] = useState(false);
  const [busyDoc, setBusyDoc] = useState<number | null>(null);
  const timerRef = useRef<number | null>(null);

  const hasRunning = documents.some(
    (doc) => doc.parse_status === "parsing" || doc.parse_status === "reparsing"
  );

  // 解析状态轮询：有解析中任务时 3s 刷新
  useEffect(() => {
    if (hasRunning && timerRef.current === null) {
      timerRef.current = window.setInterval(() => {
        onChanged();
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

  const uploadFile = useCallback(
    async (file: File) => {
      if (file.size > MAX_FILE_SIZE_MB * 1024 * 1024) {
        message.error(`文件超过 ${MAX_FILE_SIZE_MB}MB 上限`);
        return;
      }
      setUploading(true);
      try {
        const formData = new FormData();
        formData.append("file", file);
        formData.append("replace", "true");
        const data = await postData<KnowledgeUploadResponse>(
          `/knowledge-bases/${kbId}/documents/upload`,
          formData
        );
        message.success(`已上传 ${data.source_file}，后台解析中`);
        onChanged();
      } catch (e) {
        message.error(e instanceof Error ? e.message : "上传失败");
      } finally {
        setUploading(false);
      }
    },
    [kbId, onChanged]
  );

  const reparse = async (doc: KnowledgeDocItem) => {
    setBusyDoc(doc.doc_id);
    try {
      await postData(`/knowledge-bases/${kbId}/documents/${doc.doc_id}/reparse`);
      message.success("已开始重新解析");
      onChanged();
    } catch (e) {
      message.error(e instanceof Error ? e.message : "重新解析失败");
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
        <Upload
          accept={ACCEPT_EXTS}
          showUploadList={false}
          beforeUpload={(file) => {
            void uploadFile(file);
            return false;
          }}
        >
          <Button
            type="primary"
            size="small"
            loading={uploading}
            icon={<span className="material-symbols-rounded" style={{ fontSize: 15 }}>upload_file</span>}
          >
            上传文档
          </Button>
        </Upload>
      </div>

      {documents.length === 0 ? (
        <div className="kb-doc-empty">
          <span className="material-symbols-rounded">upload_file</span>
          <p>还没有文档。上传 .md / .txt / .pdf / .docx，系统将自动解析并向量化入库。</p>
        </div>
      ) : (
        <div className="kb-doc-list">
          {documents.map((doc) => {
            const meta = PARSE_STATUS_META[doc.parse_status] ?? PARSE_STATUS_META.pending;
            const running = doc.parse_status === "parsing" || doc.parse_status === "reparsing";
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
                    <Tag
                      color={meta.color}
                      icon={running ? <span className="material-symbols-rounded spin" style={{ fontSize: 12 }}>progress_activity</span> : undefined}
                      className="kb-parse-tag"
                    >
                      {meta.label}
                    </Tag>
                    {doc.parse_source === "glm" ? <Tag color="purple" bordered={false}>GLM 解析</Tag> : null}
                    {doc.parse_source === "local" ? <Tag bordered={false}>本地解析</Tag> : null}
                    {doc.parse_status === "parsed" ? <span className="kb-doc-chunks">{doc.chunk_count} 分块</span> : null}
                    <span className="kb-doc-time">{formatTime(doc.updated_at ?? doc.created_at)}</span>
                  </div>
                </div>
                <div className="kb-doc-actions" onClick={(event) => event.stopPropagation()}>
                  {doc.parse_status === "failed" && doc.error ? (
                    <Tooltip title={doc.error}>
                      <Button size="small" type="text" danger onClick={() => void reparse(doc)} loading={busyDoc === doc.doc_id}>
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
                      title="重新解析"
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
                    description={`将删除 ${doc.source_file} 及其全部分块与向量。`}
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
    </div>
  );
}
