// 溯源来源徽标（KB_MANAGEMENT_SPEC §4.6-A）：context-cards 质感，点击跳转溯源深链
import { useNavigate } from "react-router-dom";
import { buildTraceUrl } from "@/utils/trace";

interface Props {
  kbId: string;
  chunkId: number;
  docId?: number | null;
  sourceFile: string;
  score?: number;
}

export default function SourceReferenceBadge({
  kbId,
  chunkId,
  docId,
  sourceFile,
  score,
}: Props) {
  const navigate = useNavigate();
  return (
    <button
      type="button"
      className="src-badge"
      title="在知识库分块管理中定位该来源"
      onClick={(event) => {
        event.preventDefault();
        event.stopPropagation();
        navigate(buildTraceUrl(kbId, chunkId, docId));
      }}
    >
      <span className="material-symbols-rounded">description</span>
      <span className="src-badge-name">{sourceFile}</span>
      {typeof score === "number" ? (
        <span className="src-badge-score">{score.toFixed(2)}</span>
      ) : null}
      <span className="material-symbols-rounded src-badge-locate">open_in_new</span>
    </button>
  );
}
