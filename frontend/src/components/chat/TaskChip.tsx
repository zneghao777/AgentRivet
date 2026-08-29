import { useState } from "react";
import type { ChatOrchestration } from "@/types/chat";

export default function TaskChip({ orchestration }: { orchestration: ChatOrchestration }) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div className="orch-task-chip-wrap">
      <button
        type="button"
        className="orch-task-chip"
        aria-expanded={expanded}
        onClick={() => setExpanded((value) => !value)}
      >
        <span className="material-symbols-rounded" aria-hidden="true">route</span>
        已路由 → {orchestration.targetAgent === "main" ? "通用助手" : orchestration.targetAgent}
        <span className="orch-confidence">置信度 {Math.round(orchestration.confidence * 100)}%</span>
      </button>
      {expanded ? <p className="orch-task-chip-reason">{orchestration.reason}</p> : null}
    </div>
  );
}
