import { memo } from "react";
import type { ChatMessage } from "@/stores/sessionStore";
import { formatTime } from "@/utils/format";
import MarkdownRenderer from "./MarkdownRenderer";
import RunProcessPanel from "./RunProcessPanel";
import AnswerSources from "./AnswerSources";

function MessageBubble({ message, streaming }: { message: ChatMessage; streaming: boolean }) {
  if (message.role === "system") return null;
  if (message.role === "user") {
    return (
      <div className="message-row user-message">
        {message.createdAt ? <time>{formatTime(message.createdAt)}</time> : null}
        <div className="user-bubble">{message.content}</div>
      </div>
    );
  }
  return (
    <div className="message-row assistant-message">
      <div className="assistant-avatar"><span className="material-symbols-rounded">smart_toy</span></div>
      <div className="assistant-stack">
        {message.createdAt ? <time>{formatTime(message.createdAt)}</time> : null}
        {message.run ? <RunProcessPanel run={message.run} /> : null}
        {message.content || !streaming ? (
          <div className="assistant-bubble">
            <MarkdownRenderer
              content={message.content || "（运行结束，无文本回复）"}
              streaming={streaming}
            />
            {streaming ? <i className="stream-caret" /> : null}
          </div>
        ) : null}
        {message.run && !streaming ? <AnswerSources run={message.run} /> : null}
      </div>
    </div>
  );
}

export default memo(
  MessageBubble,
  (previous, next) => previous.message === next.message && previous.streaming === next.streaming
);
