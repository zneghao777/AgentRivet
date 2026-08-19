import { memo, useState } from "react";
import type { ChatMessage } from "@/stores/sessionStore";
import { formatTime } from "@/utils/format";
import MarkdownRenderer from "./MarkdownRenderer";

const SOURCE_RE = /来源[:：]\s*([^\s\])}]+)/g;

function parseSources(result: string): string[] {
  const sources: string[] = [];
  const re = new RegExp(SOURCE_RE.source, "g");
  let match: RegExpExecArray | null;
  while ((match = re.exec(result)) !== null) {
    const file = match[1].replace(/[)\]},]+$/, "");
    if (file && !sources.includes(file)) sources.push(file);
  }
  return sources;
}

function ToolCall({ name, args, result }: { name: string; args?: Record<string, unknown>; result?: string }) {
  const [open, setOpen] = useState(false);
  const isKnowledgeSearch = name === "knowledge_search";
  const sources = result ? parseSources(result) : [];
  return (
    <div className="tool-call">
      <button onClick={() => setOpen((value) => !value)}>
        <span className="material-symbols-rounded">{open ? "expand_more" : "chevron_right"}</span>
        <span className="material-symbols-rounded">{isKnowledgeSearch ? "database" : "build_circle"}</span>
        {isKnowledgeSearch ? "知识库检索 · " + (String(args?.query ?? "") || name) : "工具调用 · " + name}
        {!result ? <b>执行中</b> : null}
      </button>
      {open && result ? (
        isKnowledgeSearch ? (
          <div className="tool-call-body">
            <div className="kb-sources">
              {sources.length ? (
                sources.map((source, index) => (
                  <span className="kb-source" key={`${source}-${index}`}>
                    <span className="material-symbols-rounded">description</span>
                    {source}
                  </span>
                ))
              ) : (
                <span className="kb-source">（未命中来源）</span>
              )}
            </div>
            <pre>{result}</pre>
          </div>
        ) : (
          <pre>参数: {JSON.stringify(args ?? {}, null, 2)}{result ? `\n结果: ${result}` : ""}</pre>
        )
      ) : null}
      {open && !result ? <pre>参数: {JSON.stringify(args ?? {}, null, 2)}</pre> : null}
    </div>
  );
}

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
        {message.tools?.map((tool, index) => <ToolCall key={`${tool.name}-${index}`} name={tool.name} args={tool.arguments} result={tool.result} />)}
        <div className="assistant-bubble">
          <MarkdownRenderer
            content={message.content || (streaming ? "" : "（仅工具调用，无文本回复）")}
            streaming={streaming}
          />
          {streaming ? <i className="stream-caret" /> : null}
        </div>
      </div>
    </div>
  );
}

export default memo(
  MessageBubble,
  (previous, next) => previous.message === next.message && previous.streaming === next.streaming
);
