import { useEffect, useRef, useState } from "react";
import { App as AntdApp } from "antd";
import MessageList from "./MessageList";
import { deleteData, http } from "@/api/http";
import { streamChat } from "@/api/sse";
import { CHAT_MODELS, VISION_CHAT_MODEL } from "@/constants";
import { useSessionStore } from "@/stores/sessionStore";
import { useSettingsStore } from "@/stores/settingsStore";
import type { ApiResponse, SkillItem } from "@/types/api";
import type { ChatAttachment } from "@/types/chat";
import { summarize } from "@/utils/format";

const ACCEPTED_ATTACHMENTS = [
  "image/png", "image/jpeg", "image/gif", "image/webp", ".txt", ".md", ".csv",
  ".json", ".pdf", ".docx", ".py", ".js", ".jsx", ".ts", ".tsx",
].join(",");

export default function ChatWindow({ skills }: { skills: SkillItem[] }) {
  const { message: toast } = AntdApp.useApp();
  const {
    currentSessionId, messages, streaming, lastError, setStreaming, setError,
    pushMessage, updateLastAssistant, removeEmptyAssistant, beginRun, startRunStep,
    updateRunStep, endRunStep, setRunTokenUsage, finishRun, failRun, cancelRun,
    setSessionTitle, fetchSessions,
  } = useSessionStore();
  const apiKey = useSettingsStore((state) => state.apiKey);
  const model = useSettingsStore((state) => state.model);
  const setModel = useSettingsStore((state) => state.setModel);
  const [text, setText] = useState("");
  const [attachments, setAttachments] = useState<ChatAttachment[]>([]);
  const [uploading, setUploading] = useState(0);
  const [plusOpen, setPlusOpen] = useState(false);
  const [modelOpen, setModelOpen] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const deltaFrameRef = useRef<number | null>(null);
  const pendingDeltaRef = useRef("");
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const composerRef = useRef<HTMLDivElement>(null);
  const previewUrlsRef = useRef(new Set<string>());

  useEffect(() => {
    if (currentSessionId) inputRef.current?.focus();
    setAttachments([]);
    setPlusOpen(false);
    setModelOpen(false);
    return () => {
      abortRef.current?.abort();
      if (deltaFrameRef.current !== null) window.cancelAnimationFrame(deltaFrameRef.current);
      deltaFrameRef.current = null;
      pendingDeltaRef.current = "";
    };
  }, [currentSessionId]);

  useEffect(() => {
    const closeMenus = (event: PointerEvent) => {
      if (!composerRef.current?.contains(event.target as Node)) {
        setPlusOpen(false);
        setModelOpen(false);
      }
    };
    window.addEventListener("pointerdown", closeMenus);
    return () => window.removeEventListener("pointerdown", closeMenus);
  }, []);

  useEffect(() => () => {
    previewUrlsRef.current.forEach((url) => URL.revokeObjectURL(url));
    previewUrlsRef.current.clear();
  }, []);

  const uploadFiles = async (files: FileList | null) => {
    if (!currentSessionId || !files?.length) return;
    const available = Math.max(0, 6 - attachments.length);
    const selected = Array.from(files).slice(0, available);
    if (selected.length < files.length) toast.warning("每条消息最多添加 6 个附件");
    if (!selected.length) return;
    setPlusOpen(false);
    setUploading((count) => count + selected.length);
    const sessionId = currentSessionId;
    const results = await Promise.all(selected.map(async (file) => {
      const form = new FormData();
      form.append("file", file);
      try {
        const response = await http.post<ApiResponse<ChatAttachment>>(
          `/sessions/${sessionId}/attachments`, form,
          { headers: { "Content-Type": "multipart/form-data" } },
        );
        const uploaded = response.data.data as ChatAttachment;
        if (uploaded.kind === "image") {
          const previewUrl = URL.createObjectURL(file);
          previewUrlsRef.current.add(previewUrl);
          return { ...uploaded, previewUrl };
        }
        return uploaded;
      } catch (error) {
        toast.error(`${file.name}：${error instanceof Error ? error.message : "上传失败"}`);
        return null;
      } finally {
        setUploading((count) => Math.max(0, count - 1));
      }
    }));
    const uploaded = results.filter((item): item is ChatAttachment => item !== null);
    if (uploaded.length) {
      setAttachments((current) => [...current, ...uploaded]);
      if (uploaded.some((item) => item.kind === "image") && model !== VISION_CHAT_MODEL) {
        setModel(VISION_CHAT_MODEL);
        toast.info("检测到图片，已切换至视觉模型");
      }
    }
    if (fileInputRef.current) fileInputRef.current.value = "";
  };

  const removeAttachment = async (attachment: ChatAttachment) => {
    setAttachments((current) => current.filter((item) => item.id !== attachment.id));
    if (attachment.previewUrl) {
      URL.revokeObjectURL(attachment.previewUrl);
      previewUrlsRef.current.delete(attachment.previewUrl);
    }
    if (currentSessionId) {
      try {
        await deleteData(`/sessions/${currentSessionId}/attachments/${attachment.id}`);
      } catch (error) {
        toast.error(error instanceof Error ? error.message : "删除附件失败");
      }
    }
  };

  const send = async () => {
    if ((!text.trim() && !attachments.length) || !currentSessionId || streaming || uploading) return;
    if (!apiKey) { setError("未配置 API Key，请到「设置」页填写"); return; }
    const message = text.trim() || "请分析这些附件";
    const pendingAttachments = attachments;
    const sessionId = currentSessionId;
    const activeSession = useSessionStore.getState().sessions.find(
      (session) => session.session_id === sessionId,
    );
    if (!activeSession?.title || ["新对话", "新会话"].includes(activeSession.title)) {
      setSessionTitle(sessionId, summarize(message, 20));
    }
    setText("");
    setAttachments([]);
    setPlusOpen(false);
    setModelOpen(false);
    pushMessage({ role: "user", content: message, attachments: pendingAttachments });
    pushMessage({ role: "assistant", content: "", run: { status: "running", startedAt: Date.now(), steps: [] } });
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
        model,
        attachment_ids: pendingAttachments.map((item) => item.id),
        skills: skills.filter((skill) => skill.enabled).map((skill) => skill.name),
      }, {
        onTraceStart: (event) => { if (isCurrent()) beginRun(event); },
        onRunStepStart: (event) => { if (isCurrent()) startRunStep(event); },
        onRunStepUpdate: (event) => { if (isCurrent()) updateRunStep(event); },
        onRunStepEnd: (event) => { if (isCurrent()) endRunStep(event); },
        onMessageDelta: (delta) => {
          if (!isCurrent()) return;
          pendingDeltaRef.current += delta.content;
          if (deltaFrameRef.current === null) deltaFrameRef.current = window.requestAnimationFrame(flushPendingDelta);
        },
        onMessageEnd: (event) => { if (isCurrent()) setRunTokenUsage(event); },
        onRunEnd: (event) => { if (isCurrent()) finishRun(event); },
        onError: (error) => { if (isCurrent()) { setError(error.message); failRun(); } },
      }, controller.signal);
    } catch (error) {
      if (isCurrent()) {
        if (controller.signal.aborted) cancelRun();
        else { setError(error instanceof Error ? error.message : "连接中断"); failRun(); }
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
      void fetchSessions();
    }
  };

  const canSend = Boolean(currentSessionId && !streaming && !uploading && (text.trim() || attachments.length));

  return (
    <div className="chat-window">
      <header className="chat-header">
        <div className="chat-title"><div className="header-icon"><span className="material-symbols-rounded">forum</span></div><h1>与 Harness Agent 对话</h1></div>
        <div className={`status-pill${currentSessionId ? " active" : ""}`} aria-live="polite"><i />{streaming ? "正在生成" : currentSessionId ? "当前状态: 活跃" : "等待会话"}</div>
      </header>

      {lastError ? <div className="chat-error" role="alert"><span className="material-symbols-rounded" aria-hidden="true">error</span>{lastError}<button type="button" aria-label="关闭错误提示" onClick={() => setError(null)}>×</button></div> : null}
      <MessageList messages={messages} streaming={streaming} />

      <footer className="composer-area">
        {!currentSessionId ? <div className="composer-hint">请先新建或选择一个会话</div> : null}
        <div className="composer-shell" ref={composerRef}>
          {attachments.length || uploading ? (
            <div className="composer-attachments" aria-label="待发送附件">
              {attachments.map((attachment) => (
                <div className="composer-attachment" key={attachment.id}>
                  {attachment.previewUrl ? <img src={attachment.previewUrl} alt="" /> : <span className="material-symbols-rounded">description</span>}
                  <span><b title={attachment.name}>{attachment.name}</b><small>{formatBytes(attachment.size)}</small></span>
                  <button type="button" aria-label={`移除 ${attachment.name}`} onClick={() => void removeAttachment(attachment)}><span className="material-symbols-rounded">close</span></button>
                </div>
              ))}
              {uploading ? <div className="attachment-uploading"><span className="material-symbols-rounded">progress_activity</span>正在上传 {uploading} 个附件</div> : null}
            </div>
          ) : null}
          <div className="composer">
            <div className="composer-menu-anchor">
              <button type="button" className={`composer-plus${plusOpen ? " active" : ""}`} title="附加" aria-label="附加文件或图片" aria-expanded={plusOpen} onClick={() => { setPlusOpen((open) => !open); setModelOpen(false); }}><span className="material-symbols-rounded" aria-hidden="true">add</span></button>
              {plusOpen ? <div className="composer-popover attachment-menu" role="menu"><div className="popover-eyebrow">添加</div><button type="button" role="menuitem" onClick={() => fileInputRef.current?.click()}><span className="material-symbols-rounded">attach_file</span><span><b>文件和图片</b><small>最多 6 个，单个不超过 20MB</small></span></button></div> : null}
            </div>
            <textarea ref={inputRef} value={text} rows={1} disabled={streaming || !currentSessionId} aria-label="发送给 Harness Agent 的消息" placeholder="输入消息，Enter 发送，Shift+Enter 换行" onChange={(event) => { setText(event.target.value); if (lastError) setError(null); }} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void send(); } }} />
            <div className="composer-model-anchor">
              <button type="button" className="composer-model-trigger" aria-label={`当前模型 ${model}`} aria-expanded={modelOpen} title={model} onClick={() => { setModelOpen((open) => !open); setPlusOpen(false); }}><span className="material-symbols-rounded">memory</span><b>{model}</b><span className="material-symbols-rounded">expand_more</span></button>
              {modelOpen ? <div className="composer-popover model-menu" role="menu"><div className="popover-eyebrow">选择模型</div>{CHAT_MODELS.map((item) => <button type="button" role="menuitemradio" aria-checked={model === item.value} className={model === item.value ? "selected" : ""} key={item.value} onClick={() => { setModel(item.value); setModelOpen(false); inputRef.current?.focus(); }}><span className="model-menu-icon material-symbols-rounded">{item.value === VISION_CHAT_MODEL ? "image" : "bolt"}</span><span><b>{item.label}</b><small>{item.description}</small></span>{model === item.value ? <span className="material-symbols-rounded model-check">check</span> : null}</button>)}</div> : null}
            </div>
            {streaming ? <button type="button" className="send-button stop" title="停止生成" aria-label="停止生成" onClick={() => abortRef.current?.abort()}><span className="material-symbols-rounded" aria-hidden="true">stop</span></button> : <button type="button" className="send-button" title="发送" aria-label="发送消息" disabled={!canSend} onClick={() => void send()}><span className="material-symbols-rounded" aria-hidden="true">arrow_upward</span></button>}
          </div>
        </div>
        <input ref={fileInputRef} className="visually-hidden" type="file" multiple accept={ACCEPTED_ATTACHMENTS} onChange={(event) => void uploadFiles(event.target.files)} />
        <small className="ai-notice">内容由 AI 生成，请注意甄别。</small>
      </footer>
    </div>
  );
}

function formatBytes(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}
