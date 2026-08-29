import type { ChatOrchestration } from "@/types/chat";

export default function ClarificationCard({
  orchestration,
  onSelect,
  onOther,
}: {
  orchestration: ChatOrchestration;
  onSelect?: (option: string) => void;
  onOther?: () => void;
}) {
  const clarification = orchestration.clarification;
  if (!clarification) return null;
  const disabled = Boolean(clarification.selected);
  return (
    <section className="orch-clarification" aria-labelledby="orch-clarification-title">
      <header>
        <span className="material-symbols-rounded" aria-hidden="true">help</span>
        <b id="orch-clarification-title">需要确认</b>
      </header>
      <p>{clarification.question}</p>
      <div className="orch-clarification-options">
        {clarification.options.map((option) => (
          <button
            type="button"
            key={option}
            disabled={disabled}
            className={clarification.selected === option ? "selected" : ""}
            onClick={() => onSelect?.(option)}
          >
            {option}
          </button>
        ))}
        <button type="button" disabled={disabled} onClick={onOther}>其他</button>
      </div>
    </section>
  );
}
