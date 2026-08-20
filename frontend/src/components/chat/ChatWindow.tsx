import { useEffect, useRef, useState } from "react";
import { Select } from "antd";
import MessageList from "./MessageList";
import { useSessionStore } from "@/stores/sessionStore";
import { useSettingsStore } from "@/stores/settingsStore";
import { streamChat } from "@/api/sse";
import { getData } from "@/api/http";
import type { KnowledgeBaseItem, SkillItem } from "@/types/api";
import { summarize } from "@/utils/format";

export default function ChatWindow({
  skills,
}: {
  skills: SkillItem[];
}) {
  const { currentSessionId, messages, streaming, lastError, setStreaming, setError,
    pushMessage, updateLastAssistant, addToolCall, completeToolCall, removeEmptyAssistant,
    setSessionTitle } = useSessionStore();
  const apiKey = useSettingsStore((state) => state.apiKey);
  const [text, setText] = useState("");
  const [knowledgeBases, setKnowledgeBases] = useState<KnowledgeBaseItem[]>([]);
  const [selectedKbs, setSelectedKbs] = useState<string[]>([]);
  const abortRef = useRef<AbortController | null>(null);
  const deltaFrameRef = useRef<number | null>(null);
  const pendingDeltaRef = useRef("");
  const inputRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    getData<KnowledgeBaseItem[]>("/knowledge-bases")
      .then((items) => setKnowledgeBases(items.filter((kb) => kb.status === "active")))
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    if (currentSessionId) inputRef.current?.focus();
    return () => {
      abortRef.current?.abort();
      if (deltaFrameRef.current !== null) window.cancelAnimationFrame(deltaFrameRef.current);
      deltaFrameRef.current = null;
      pendingDeltaRef.current = "";
    };
  }, [currentSessionId]);

  const send = async () => {
    if (!text.trim() || !currentSessionId || streaming) return;
    if (!apiKey) { setError("未配置 API Key，请到「设置」页填写"); return; }
    const message = text.trim();
    const sessionId = currentSessionId;
    const activeSession = useSessionStore.getState().sessions.find(
      (session) => session.session_id === sessionId
    );
    if (!activeSession?.title || ["新对话", "新会话"].includes(activeSession.title)) {
      setSessionTitle(sessionId, summarize(message, 20));
    }
    setText("");
    pushMessage({ role: "user", content: message });
    pushMessage({ role: "assistant", content: "" });
    const controller = new AbortController();
    abortRef.current = controller;
    setStreaming(true);
    setError(null);

    const isCurrent = () => useSessionStore.getState().currentSessionId === sessionId;
    const flushPendingDelta = () => {
      deltaFrameRef.current = null;
      const pending = pendingDeltaRef.current;
      pendingDeltaRef.current = "";
      if (pending && isCurrent()) updateLastAssistant(pending);
    };
    try {
      await streamChat(sessionId, {
        message,
        skills: skills.filter((skill) => skill.enabled).map((skill) => skill.name),
        ...(selectedKbs.length > 0 ? { knowledge_bases: selectedKbs } : {}),
      }, {
        onMessageDelta: (delta) => {
          if (!isCurrent()) return;
          pendingDeltaRef.current += delta.content;
          if (deltaFrameRef.current === null) {
            deltaFrameRef.current = window.requestAnimationFrame(flushPendingDelta);
          }
        },
        onToolStart: (tool) => { if (isCurrent()) addToolCall(tool); },
        onToolEnd: (tool) => { if (isCurrent()) completeToolCall(tool); },
        onError: (error) => { if (isCurrent()) setError(error.message); },
      }, controller.signal);
    } catch (error) {
      if (!controller.signal.aborted && isCurrent()) setError(error instanceof Error ? error.message : "连接中断");
    } finally {
      if (isCurrent()) {
        if (deltaFrameRef.current !== null) window.cancelAnimationFrame(deltaFrameRef.current);
        flushPendingDelta();
        const currentMessages = useSessionStore.getState().messages;
        const last = currentMessages[currentMessages.length - 1];
        if (last?.role === "assistant" && !last.content && !last.tools?.length) removeEmptyAssistant();
        setStreaming(false);
      }
      abortRef.current = null;
    }
  };

  return (
    <div className="chat-window">
      <header className="chat-header">
        <div className="chat-title">
          <div className="header-icon"><span className="material-symbols-rounded">forum</span></div>
          <h1>与 Harness Agent 对话</h1>
        </div>
        <div className={`status-pill${currentSessionId ? " active" : ""}`}><i />{streaming ? "正在生成" : currentSessionId ? "当前状态: 活跃" : "等待会话"}</div>
      </header>

      {lastError ? <div className="chat-error"><span className="material-symbols-rounded">error</span>{lastError}<button onClick={() => setError(null)}>×</button></div> : null}
      <MessageList messages={messages} streaming={streaming} />

      <footer className="composer-area">
        {!currentSessionId ? <div className="composer-hint">请先新建或选择一个会话</div> : null}
        <div className="composer-kb-bar">
          <span className="material-symbols-rounded">database</span>
          <Select
            mode="multiple"
            size="small"
            allowClear
            placeholder="绑定知识库（可选，Agent 将基于其检索回答）"
            maxTagCount={3}
            value={selectedKbs}
            onChange={setSelectedKbs}
            style={{ flex: 1, minWidth: 0, maxWidth: 560 }}
            options={knowledgeBases.map((kb) => ({ value: kb.name, label: kb.name }))}
          />
        </div>
        <div className="composer">
          <button title="附加"><span className="material-symbols-rounded">add_circle</span></button>
          <textarea ref={inputRef} value={text} rows={1} disabled={streaming || !currentSessionId}
            placeholder="输入消息，Enter 发送，Shift+Enter 换行"
            onChange={(event) => { setText(event.target.value); if (lastError) setError(null); }}
            onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void send(); } }} />
          {streaming ? (
            <button className="send-button stop" title="停止生成" onClick={() => abortRef.current?.abort()}><span className="material-symbols-rounded">stop</span></button>
          ) : (
            <><button title="语音输入"><span className="material-symbols-rounded">mic</span></button>
            <button className="send-button" title="发送" disabled={!currentSessionId || !text.trim()} onClick={() => void send()}><span className="material-symbols-rounded">arrow_upward</span></button></>
          )}
        </div>
        <small className="ai-notice">内容由 AI 生成，请注意甄别。</small>
      </footer>
    </div>
  );
}
