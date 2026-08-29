import { useEffect, useRef } from "react";
import type { ChatMessage } from "@/stores/sessionStore";
import MessageBubble from "./MessageBubble";

export default function MessageList({
  messages,
  streaming,
  onClarificationSelect,
  onClarificationOther,
}: {
  messages: ChatMessage[];
  streaming: boolean;
  onClarificationSelect?: (option: string) => void;
  onClarificationOther?: () => void;
}) {
  const bottomRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      bottomRef.current?.scrollIntoView({ behavior: streaming ? "auto" : "smooth" });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [messages, streaming]);

  if (messages.length === 0) {
    return (
      <div className="empty-chat">
        <div className="empty-chat-icon"><span className="material-symbols-rounded">forum</span></div>
        <h2>开始一段对话吧</h2>
        <p>我是你的企业级 Agent。试试让我调用一项技能，或者直接问我任何问题。</p>
      </div>
    );
  }
  return (
    <div className="message-list">
      <div className="message-column">
        {messages.map((message, index) => (
          <MessageBubble
            key={`${message.createdAt ?? "local"}-${index}`}
            message={message}
            streaming={streaming && index === messages.length - 1}
            onClarificationSelect={index === messages.length - 1 ? onClarificationSelect : undefined}
            onClarificationOther={index === messages.length - 1 ? onClarificationOther : undefined}
          />
        ))}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}
