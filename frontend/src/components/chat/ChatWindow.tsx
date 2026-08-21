import { useEffect, useRef, useState } from "react";
import MessageList from "./MessageList";
import { useSessionStore } from "@/stores/sessionStore";
import { useSettingsStore } from "@/stores/settingsStore";
import { streamChat } from "@/api/sse";
import type { SkillItem } from "@/types/api";
import { summarize } from "@/utils/format";

export default function ChatWindow({
  skills,
}: {
  skills: SkillItem[];
}) {
  const { currentSessionId, messages, streaming, lastError, setStreaming, setError,
    pushMessage, updateLastAssistant, removeEmptyAssistant, beginRun, startRunStep,
    updateRunStep, endRunStep, setRunTokenUsage, finishRun, failRun, cancelRun,
    setSessionTitle } = useSessionStore();
  const apiKey = useSettingsStore((state) => state.apiKey);
  const [text, setText] = useState("");
  const abortRef = useRef<AbortController | null>(null);
  const deltaFrameRef = useRef<number | null>(null);
  const pendingDeltaRef = useRef("");
  const inputRef = useRef<HTMLTextAreaElement>(null);

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
    pushMessage({
      role: "assistant",
      content: "",
      run: { status: "running", startedAt: Date.now(), steps: [] },
    });
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
      }, {
        onTraceStart: (event) => { if (isCurrent()) beginRun(event); },
        onRunStepStart: (event) => { if (isCurrent()) startRunStep(event); },
        onRunStepUpdate: (event) => { if (isCurrent()) updateRunStep(event); },
        onRunStepEnd: (event) => { if (isCurrent()) endRunStep(event); },
        onMessageDelta: (delta) => {
          if (!isCurrent()) return;
          pendingDeltaRef.current += delta.content;
          if (deltaFrameRef.current === null) {
            deltaFrameRef.current = window.requestAnimationFrame(flushPendingDelta);
          }
        },
        onMessageEnd: (event) => { if (isCurrent()) setRunTokenUsage(event); },
        onRunEnd: (event) => { if (isCurrent()) finishRun(event); },
        onError: (error) => {
          if (!isCurrent()) return;
          setError(error.message);
          failRun();
        },
      }, controller.signal);
    } catch (error) {
      if (isCurrent()) {
        if (controller.signal.aborted) cancelRun();
        else {
          setError(error instanceof Error ? error.message : "连接中断");
          failRun();
        }
      }
    } finally {
      if (isCurrent()) {
        if (deltaFrameRef.current !== null) window.cancelAnimationFrame(deltaFrameRef.current);
        flushPendingDelta();
        const currentMessages = useSessionStore.getState().messages;
        const last = currentMessages[currentMessages.length - 1];
        if (last?.role === "assistant" && !last.content && !last.run) removeEmptyAssistant();
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
        <div className={`status-pill${currentSessionId ? " active" : ""}`} aria-live="polite"><i />{streaming ? "正在生成" : currentSessionId ? "当前状态: 活跃" : "等待会话"}</div>
      </header>

      {lastError ? <div className="chat-error" role="alert"><span className="material-symbols-rounded" aria-hidden="true">error</span>{lastError}<button type="button" aria-label="关闭错误提示" onClick={() => setError(null)}>×</button></div> : null}
      <MessageList messages={messages} streaming={streaming} />

      <footer className="composer-area">
        {!currentSessionId ? <div className="composer-hint">请先新建或选择一个会话</div> : null}
        <div className="composer">
          <button type="button" title="附加" aria-label="附加内容"><span className="material-symbols-rounded" aria-hidden="true">add_circle</span></button>
          <textarea ref={inputRef} value={text} rows={1} disabled={streaming || !currentSessionId}
            aria-label="发送给 Harness Agent 的消息"
            placeholder="输入消息，Enter 发送，Shift+Enter 换行"
            onChange={(event) => { setText(event.target.value); if (lastError) setError(null); }}
            onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void send(); } }} />
          {streaming ? (
            <button type="button" className="send-button stop" title="停止生成" aria-label="停止生成" onClick={() => abortRef.current?.abort()}><span className="material-symbols-rounded" aria-hidden="true">stop</span></button>
          ) : (
            <button type="button" className="send-button" title="发送" aria-label="发送消息" disabled={!currentSessionId || !text.trim()} onClick={() => void send()}><span className="material-symbols-rounded" aria-hidden="true">arrow_upward</span></button>
          )}
        </div>
        <small className="ai-notice">内容由 AI 生成，请注意甄别。</small>
      </footer>
    </div>
  );
}
