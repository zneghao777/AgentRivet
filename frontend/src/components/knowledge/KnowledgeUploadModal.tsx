import { useEffect, useState } from "react";
import { Button, Collapse, Modal, Progress, Radio, Upload, message } from "antd";
import type { UploadFile } from "antd";
import { http } from "@/api/http";
import type { ApiResponse, KnowledgeBaseItem, KnowledgeUploadResponse } from "@/types/api";

const MAX_FILE_SIZE_MB = 50;

export default function KnowledgeUploadModal({ kb, open, onClose, onUploaded }: { kb: KnowledgeBaseItem | null; open: boolean; onClose: () => void; onUploaded: () => void }) {
  const [files, setFiles] = useState<UploadFile[]>([]);
  const [parseMethod, setParseMethod] = useState<"text" | "ocr" | null>(null);
  const [graphOverride, setGraphOverride] = useState<"inherit" | "include" | "exclude">("inherit");
  const [uploading, setUploading] = useState(false);
  const [progress, setProgress] = useState<Record<string, number>>({});

  useEffect(() => {
    if (!open) return;
    setFiles([]);
    setProgress({});
    setParseMethod(null);
    setGraphOverride("inherit");
  }, [open]);

  const upload = async () => {
    if (!kb || files.length === 0 || !parseMethod) return;
    setUploading(true);
    try {
      for (const item of files) {
        const raw = item.originFileObj ?? (item as unknown as File);
        const form = new FormData();
        form.append("file", raw);
        form.append("replace", "true");
        form.append("parse_method", parseMethod);
        form.append("graph_override", graphOverride);
        await http.post<ApiResponse<KnowledgeUploadResponse>>(`/knowledge-bases/${kb.kb_id}/documents/upload`, form, {
          onUploadProgress: (event) => {
            const total = event.total;
            if (!total) return;
            setProgress((current) => ({ ...current, [item.uid]: Math.round((event.loaded / total) * 100) }));
          },
        });
      }
      message.success("文件上传完成，后台正在建立索引");
      onUploaded();
      onClose();
    } catch (error) {
      message.error(error instanceof Error ? error.message : "上传失败");
    } finally {
      setUploading(false);
    }
  };

  const pipeline = kb?.index_mode === "graph_enhanced" ? "解析文档 → 生成分块 → 建立文本索引 → 构建知识图谱" : "解析文档 → 生成分块 → 建立文本索引";
  return (
    <Modal className="kb-upload-modal" width={680} title={`上传到 ${kb?.name ?? "知识库"}`} open={open} onCancel={onClose} closable={!uploading} maskClosable={!uploading} footer={[<Button key="cancel" disabled={uploading} onClick={onClose}>取消</Button>, <Button key="upload" type="primary" loading={uploading} disabled={!files.length || !parseMethod} onClick={() => void upload()}>上传 {files.length || ""} 个文件</Button>]}> 
      <section className="kb-parse-method">
        <p className="kb-field-label">解析方式 <span aria-hidden="true">*</span></p>
        <Radio.Group value={parseMethod} onChange={(event) => setParseMethod(event.target.value)} className="kb-parse-options">
          <Radio value="text"><b>文本解析</b><span>适合 MD、TXT、可复制文字的 PDF / DOCX；文件仅在本地提取文本。</span></Radio>
          <Radio value="ocr"><b>OCR 解析</b><span>适合扫描版 PDF 或图片内容较多的文档；会调用远程 OCR 服务。</span></Radio>
        </Radio.Group>
      </section>
      <Upload.Dragger accept=".md,.txt,.pdf,.docx" multiple fileList={files} beforeUpload={(file) => {
        if (file.size > MAX_FILE_SIZE_MB * 1024 * 1024) {
          message.error(`${file.name} 超过 ${MAX_FILE_SIZE_MB}MB 上限`);
          return Upload.LIST_IGNORE;
        }
        setFiles((current) => [...current, file]);
        return false;
      }} onRemove={(file) => { setFiles((current) => current.filter((item) => item.uid !== file.uid)); return true; }}>
        <span className="material-symbols-rounded kb-upload-icon">upload_file</span>
        <p className="ant-upload-text">拖放文件到这里，或选择文件</p>
        <p className="ant-upload-hint">支持 MD、TXT、PDF、DOCX；单文件最大 {MAX_FILE_SIZE_MB} MB</p>
      </Upload.Dragger>
      {files.map((file) => typeof progress[file.uid] === "number" ? <div className="kb-upload-progress" key={file.uid}><span>{file.name}</span><Progress percent={progress[file.uid]} size="small" /></div> : null)}
      <section className="kb-pipeline-preview"><b>即将执行的流水线</b><span>{pipeline}</span>{kb?.index_mode === "graph_enhanced" && kb.graph_build_policy === "manual" ? <p>上传完成后文本检索立即可用，图谱将保持待构建，直到你手动触发。</p> : null}</section>
      {kb?.index_mode === "graph_enhanced" ? <Collapse ghost items={[{ key: "advanced", label: "高级设置", children: <div><p className="kb-field-label">图谱参与方式</p><Radio.Group value={graphOverride} onChange={(event) => setGraphOverride(event.target.value)} className="kb-policy-options"><Radio value="inherit">跟随知识库设置</Radio><Radio value="include">参与图谱构建</Radio><Radio value="exclude">不参与图谱构建</Radio></Radio.Group></div> }]} /> : null}
      <div className="sr-only" aria-live="polite">{uploading ? "正在上传文件" : ""}</div>
    </Modal>
  );
}
