import type { KnowledgeDocItem } from "@/types/api";
import IndexHealthBadge from "./IndexHealthBadge";

const PARSE_LABEL: Record<string, string> = {
  pending: "等待",
  parsing: "解析中",
  reparsing: "重新解析",
  parsed: "已完成",
  failed: "失败",
};

export default function PipelineStatus({ doc, showGraph }: { doc: KnowledgeDocItem; showGraph: boolean }) {
  return (
    <div className="kb-pipeline-status" aria-label="索引流水线状态">
      <span><b>解析</b>{PARSE_LABEL[doc.parse_status] ?? doc.parse_status}</span>
      <span><b>文本</b><IndexHealthBadge status={doc.vector_status} /></span>
      {showGraph ? <span><b>图谱</b><IndexHealthBadge status={doc.graph_status} /></span> : null}
    </div>
  );
}
