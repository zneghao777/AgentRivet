// 分块编辑 Modal（KB_MANAGEMENT_SPEC §4.3-B）：编辑文本自动重新向量化
import { useEffect, useState } from "react";
import { Modal, Tag, message } from "antd";
import { putData } from "@/api/http";
import type { KnowledgeChunkItem } from "@/types/api";

export default function ChunkEditModal({
  kbId,
  chunk,
  open,
  onClose,
  onSaved,
}: {
  kbId: string;
  chunk: KnowledgeChunkItem | null;
  open: boolean;
  onClose: () => void;
  onSaved: (updated: KnowledgeChunkItem) => void;
}) {
  const [text, setText] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (open && chunk) {
      setText(chunk.text);
    }
  }, [open, chunk]);

  if (!chunk) return null;

  const save = async () => {
    setSaving(true);
    try {
      const updated = await putData<KnowledgeChunkItem>(
        `/knowledge-bases/${kbId}/chunks/${chunk.chunk_id}`,
        { text }
      );
      message.success("分块已保存，向量已重新生成");
      onSaved(updated);
      onClose();
    } catch (e) {
      message.error(e instanceof Error ? e.message : "保存失败");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      title={
        <div className="kb-modal-title">
          <span className="material-symbols-rounded">edit_note</span>
          编辑分块 #{chunk.chunk_id}
        </div>
      }
      open={open}
      onOk={() => void save()}
      onCancel={onClose}
      okText="保存并重新向量化"
      cancelText="取消"
      confirmLoading={saving}
      width="min(760px, 92vw)"
      destroyOnHidden
    >
      <div className="chunk-edit-meta">
        <Tag color="blue" bordered={false}>{chunk.source_file}</Tag>
        <Tag bordered={false}>序号 {chunk.chunk_index + 1}</Tag>
        <Tag bordered={false}>{chunk.char_count} 字符</Tag>
        {chunk.heading ? <Tag color="purple" bordered={false}>#{chunk.heading}</Tag> : null}
      </div>
      <textarea
        className="chunk-edit-area"
        value={text}
        onChange={(event) => setText(event.target.value)}
        rows={12}
        placeholder="分块内容…"
      />
      <div className="chunk-edit-hint">
        <span className="material-symbols-rounded">info</span>
        保存后将重新生成该分块的向量，Agent 检索将始终指向最新内容。
      </div>
    </Modal>
  );
}
