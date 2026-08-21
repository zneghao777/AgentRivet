import { Tag } from "antd";
import type { IndexMode } from "@/types/api";

export default function KnowledgeModeBadge({ mode }: { mode: IndexMode }) {
  return mode === "graph_enhanced" ? (
    <Tag className="kb-mode-badge" color="blue" icon={<span className="material-symbols-rounded">account_tree</span>}>
      图谱增强
    </Tag>
  ) : (
    <Tag className="kb-mode-badge" icon={<span className="material-symbols-rounded">manage_search</span>}>
      标准
    </Tag>
  );
}
