import { useEffect, useRef, useState } from "react";
import type { InterruptRequested } from "@/types/sse";

export default function ToolApprovalCard({
  approval,
  submitting,
  onDecision,
}: {
  approval: InterruptRequested;
  submitting: boolean;
  onDecision: (decision: "approve" | "reject", input: string) => void;
}) {
  const cardRef = useRef<HTMLElement>(null);
  const [note, setNote] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);
  const calls = approval.state.tool_calls ?? [];

  useEffect(() => {
    cardRef.current?.focus();
  }, [approval.id]);

  return (
    <section
      ref={cardRef}
      className="tool-approval-card"
      role="dialog"
      aria-modal="false"
      aria-labelledby={`approval-title-${approval.id}`}
      tabIndex={-1}
    >
      <div className="tool-approval-head">
        <span className="tool-approval-icon material-symbols-rounded" aria-hidden="true">shield_person</span>
        <span>
          <small>需要你的确认</small>
          <strong id={`approval-title-${approval.id}`}>{approval.reason}</strong>
        </span>
        <em>{calls.length} 项</em>
      </div>

      <div className="tool-approval-list" aria-label="待审批工具">
        {calls.map((call) => {
          const isExpanded = expanded === call.id;
          return (
            <article key={call.id} className={isExpanded ? "expanded" : ""}>
              <button
                type="button"
                aria-expanded={isExpanded}
                onClick={() => setExpanded((current) => current === call.id ? null : call.id)}
              >
                <span className="material-symbols-rounded" aria-hidden="true">
                  {call.name.includes("search") ? "travel_explore" : "build"}
                </span>
                <span>
                  <b>{toolLabel(call.name)}</b>
                  <small>{call.description || call.name}</small>
                </span>
                <span className="material-symbols-rounded approval-chevron" aria-hidden="true">expand_more</span>
              </button>
              {isExpanded ? <pre>{formatArguments(call.arguments)}</pre> : null}
            </article>
          );
        })}
      </div>

      <label className="tool-approval-note">
        <span>审批说明（可选）</span>
        <input
          value={note}
          maxLength={500}
          disabled={submitting}
          placeholder="例如：仅允许读取，不要修改数据"
          onChange={(event) => setNote(event.target.value)}
        />
      </label>

      <div className="tool-approval-actions">
        <span><i />会话已安全暂停，工具尚未执行</span>
        <button type="button" disabled={submitting} onClick={() => onDecision("reject", note)}>
          拒绝
        </button>
        <button className="primary" type="button" disabled={submitting} onClick={() => onDecision("approve", note)}>
          {submitting ? <span className="material-symbols-rounded approval-spin">progress_activity</span> : <span className="material-symbols-rounded">check</span>}
          批准并继续
        </button>
      </div>
    </section>
  );
}

function toolLabel(name: string): string {
  if (name === "builtin_get_time") return "获取当前时间";
  if (name === "builtin_calculator") return "安全计算";
  if (name === "builtin_echo") return "回显测试";
  if (name === "builtin_read_skill_file") return "读取 Skill 资源";
  if (name === "knowledge_search") return "联网 / 知识检索";
  if (name === "knowledge_graph_search") return "知识图谱检索";
  if (name === "memory_search") return "长期记忆检索";
  if (name.startsWith("delegate_to_")) return `委派给 ${name.replace("delegate_to_", "")}`;
  return name.replace(/^builtin_/, "").replace(/_/g, " ");
}

function formatArguments(value: Record<string, unknown>): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return "参数无法展示";
  }
}
