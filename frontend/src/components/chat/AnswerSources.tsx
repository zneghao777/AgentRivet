import { memo, useMemo } from "react";
import type { ChatRun, KnowledgeReference } from "@/types/chat";

function locationText(reference: KnowledgeReference): string {
  const parts: string[] = [];
  if (reference.lineStart != null && reference.lineEnd != null) {
    parts.push(`解析文本第 ${reference.lineStart}–${reference.lineEnd} 行`);
  }
  if (reference.chunkIndex != null) parts.push(`分块 #${reference.chunkIndex}`);
  if (reference.heading) parts.push(reference.heading);
  return parts.join(" · ") || `分块 ID ${reference.chunkId}`;
}

function AnswerSources({ run }: { run: ChatRun }) {
  const references = useMemo(() => {
    const unique = new Map<string, KnowledgeReference>();
    for (const step of run.steps) {
      for (const reference of step.references ?? []) {
        const key = `${reference.kbId}:${reference.chunkId}`;
        if (!unique.has(key)) unique.set(key, reference);
      }
    }
    return Array.from(unique.values()).sort((a, b) => a.rank - b.rank);
  }, [run.steps]);

  if (!references.length) return null;

  return (
    <section className="answer-sources" aria-label="本回答检索来源">
      <div className="answer-sources-label">
        <span className="material-symbols-rounded">format_quote</span>
        参考来源 <small>{references.length}</small>
      </div>
      <div className="answer-source-list">
        {references.map((reference) => {
          const tooltipId = `source-${reference.kbId}-${reference.chunkId}`;
          return (
          <span
            className="answer-source-chip"
            key={`${reference.kbId}:${reference.chunkId}`}
            tabIndex={0}
            aria-describedby={tooltipId}
            aria-label={`来源 ${reference.rank}：${reference.sourceFile}，${locationText(reference)}`}
          >
            <b>{reference.rank}</b>
            <span className="answer-source-popover" role="tooltip" id={tooltipId}>
              <strong>{reference.sourceFile}</strong>
              <small>{locationText(reference)}</small>
              <span>{reference.excerpt || "该来源未提供可预览的引用内容。"}</span>
            </span>
          </span>
          );
        })}
      </div>
    </section>
  );
}

export default memo(AnswerSources);
