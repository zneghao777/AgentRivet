import { useEffect, useMemo, useRef, useState } from "react";
import { App as AntdApp, Select } from "antd";
import { useNavigate } from "react-router-dom";
import MessageList from "./MessageList";
import ToolApprovalCard from "./ToolApprovalCard";
import { deleteData, http, patchData } from "@/api/http";
import type { ChatRuntimeOptions } from "@/api/sse";
import { CHAT_MODELS, VISION_CHAT_MODEL } from "@/constants";
import { chatRunManager } from "@/services/chatRunManager";
import { EMPTY_MESSAGES, useSessionStore } from "@/stores/sessionStore";
import { useSettingsStore } from "@/stores/settingsStore";
import type { AgentItem, ApiResponse, SessionItem, SkillItem } from "@/types/api";
import type { ChatAttachment } from "@/types/chat";
import { summarize } from "@/utils/format";

const ACCEPTED_ATTACHMENTS = [
  "image/png", "image/jpeg", "image/gif", "image/webp", ".txt", ".md", ".csv",
  ".json", ".pdf", ".docx", ".py", ".js", ".jsx", ".ts", ".tsx",
].join(",");

export default function ChatWindow({ skills, agents }: { skills: SkillItem[]; agents: AgentItem[] }) {
  const { message: toast } = AntdApp.useApp();
  const navigate = useNavigate();
  const currentSessionId = useSessionStore((state) => state.currentSessionId);
  const sessions = useSessionStore((state) => state.sessions);
  const messages = useSessionStore((state) => currentSessionId
    ? state.messagesBySession[currentSessionId] ?? EMPTY_MESSAGES : EMPTY_MESSAGES);
  const currentRun = useSessionStore((state) => currentSessionId ? state.runsBySession[currentSessionId] : undefined);
  const lastError = useSessionStore((state) => currentSessionId ? state.errorsBySession[currentSessionId] ?? null : null);
  const pushMessage = useSessionStore((state) => state.pushMessage);
  const removeEmptyAssistant = useSessionStore((state) => state.removeEmptyAssistant);
  const setError = useSessionStore((state) => state.setError);
  const setSessionTitle = useSessionStore((state) => state.setSessionTitle);
  const setSessionAgent = useSessionStore((state) => state.setSessionAgent);
  const fetchSessions = useSessionStore((state) => state.fetchSessions);
  const selectClarification = useSessionStore((state) => state.selectClarification);
  const apiKey = useSettingsStore((state) => state.apiKey);
  const model = useSettingsStore((state) => state.model);
  const setModel = useSettingsStore((state) => state.setModel);
  const approvalMode = useSettingsStore((state) => state.approvalMode);
  const setApprovalMode = useSettingsStore((state) => state.setApprovalMode);
  const customModels = useSettingsStore((state) => state.customModels);
  const [text, setText] = useState("");
  const [attachments, setAttachments] = useState<ChatAttachment[]>([]);
  const [uploading, setUploading] = useState(0);
  const [plusOpen, setPlusOpen] = useState(false);
  const [modelOpen, setModelOpen] = useState(false);
  const [approvalSubmitting, setApprovalSubmitting] = useState(false);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const composerRef = useRef<HTMLDivElement>(null);
  const previewUrlsRef = useRef(new Set<string>());
  const modelOptions = useMemo(() => [
    ...CHAT_MODELS.map((item) => ({ ...item, custom: false })),
    ...customModels.map((item) => ({
      value: item.modelId, label: item.name,
      description: item.description || item.modelId, custom: true,
    })),
  ], [customModels]);
  const activeModelLabel = modelOptions.find((item) => item.value === model)?.label ?? model;
  const activeCustomModel = customModels.find((item) => item.modelId === model);
  const activeSession = useMemo(
    () => sessions.find((session) => session.session_id === currentSessionId),
    [currentSessionId, sessions],
  );
  const activeAgent = agents.find((agent) => agent.id === activeSession?.agent_id);
  const currentApproval = currentRun?.pendingApproval ?? null;
  const streaming = currentRun?.status === "queued" || currentRun?.status === "running";
  const active = streaming || currentRun?.status === "pending_approval";

  const runtimeOptions = (): ChatRuntimeOptions => ({
    model,
    skills: skills.filter((skill) => skill.enabled).map((skill) => skill.name),
    agent_id: activeSession?.agent_id || undefined,
  });

  const switchAgent = async (agentId: string) => {
    if (!currentSessionId || active) return;
    try {
      const updated = await patchData<SessionItem>(`/sessions/${currentSessionId}`, { agent_id: agentId || null });
      setSessionAgent(currentSessionId, updated.agent_id || null);
      toast.success(agentId ? "Agent 已切换，将从下一条消息生效" : "已切换为默认助手");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "切换 Agent 失败");
    }
  };

  useEffect(() => {
    if (currentSessionId) {
      inputRef.current?.focus();
      void chatRunManager.restoreSession(currentSessionId);
    }
    setAttachments([]);
    setPlusOpen(false);
    setModelOpen(false);
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
        if (uploaded.kind !== "image") return uploaded;
        const previewUrl = URL.createObjectURL(file);
        previewUrlsRef.current.add(previewUrl);
        return { ...uploaded, previewUrl };
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
    if (!currentSessionId) return;
    try {
      await deleteData(`/sessions/${currentSessionId}/attachments/${attachment.id}`);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "删除附件失败");
    }
  };

  const send = async (messageOverride?: string) => {
    if (!currentSessionId || active || uploading) return;
    const message = messageOverride?.trim() || text.trim() || (attachments.length ? "请分析这些附件" : "");
    if (!message) return;
    if (!apiKey) {
      setError(currentSessionId, "未配置 API Key，请到「设置」页填写");
      return;
    }
    if (activeCustomModel?.apiKey) {
      setError(currentSessionId, "后台运行不会持久化明文 Provider 密钥；请把自定义模型绑定到受控 Agent 配置后再发送");
      return;
    }
    const sessionId = currentSessionId;
    const pendingAttachments = attachments;
    if (!activeSession?.title || ["新对话", "新会话"].includes(activeSession.title)) {
      setSessionTitle(sessionId, summarize(message, 20));
    }
    const selectedAgent = agents.find((agent) => agent.id === activeSession?.agent_id);
    pushMessage(sessionId, { role: "user", content: message, attachments: pendingAttachments });
    pushMessage(sessionId, {
      role: "assistant", content: "", agentId: selectedAgent?.id, agentName: selectedAgent?.name,
      run: { status: "running", startedAt: Date.now(), steps: [] },
    });
    setText("");
    setAttachments([]);
    setPlusOpen(false);
    setModelOpen(false);
    setError(sessionId, null);
    try {
      await chatRunManager.create(sessionId, {
        message,
        ...runtimeOptions(),
        attachment_ids: pendingAttachments.map((item) => item.id),
        approval_mode: approvalMode,
      });
    } catch (error) {
      setError(sessionId, error instanceof Error ? error.message : "创建运行失败");
      useSessionStore.getState().failRun(sessionId);
      removeEmptyAssistant(sessionId);
    } finally {
      void fetchSessions();
    }
  };

  const decideApproval = async (decision: "approve" | "reject", input: string) => {
    if (!currentSessionId || !currentApproval || approvalSubmitting) return;
    setApprovalSubmitting(true);
    try {
      await chatRunManager.resume(
        currentSessionId,
        decision,
        input.trim() || (decision === "approve" ? "批准执行" : "拒绝执行"),
      );
    } catch (error) {
      setError(currentSessionId, error instanceof Error ? error.message : "恢复执行失败");
    } finally {
      setApprovalSubmitting(false);
    }
  };

  const stop = async () => {
    if (!currentSessionId) return;
    try {
      await chatRunManager.stop(currentSessionId);
    } catch (error) {
      setError(currentSessionId, error instanceof Error ? error.message : "停止任务失败");
    }
  };

  const canSend = Boolean(currentSessionId && !active && !uploading && (text.trim() || attachments.length));
  const statusLabel = currentRun?.status === "queued" ? "排队中"
    : currentRun?.status === "running" ? "正在生成"
      : currentRun?.status === "pending_approval" ? "等待审批"
        : currentRun?.status === "failed" ? "运行失败"
          : currentRun?.status === "canceled" ? "已停止"
            : activeAgent?.name || (currentSessionId ? "默认助手" : "等待会话");

  return (
    <div className="chat-window">
      <header className="chat-header">
        <div className="chat-title"><div className="header-icon"><span className="material-symbols-rounded">forum</span></div><h1>与 Harness Agent 对话</h1></div>
        <div className="chat-header-controls">
          <Select
            className="approval-mode-select" classNames={{ popup: { root: "approval-mode-menu" } }}
            value={currentApproval || approvalSubmitting ? "manual" : approvalMode}
            disabled={active} onChange={(value: "manual" | "auto") => setApprovalMode(value)}
            aria-label="工具审批模式"
            options={[
              { value: "manual", label: <span className="approval-mode-option"><span className="material-symbols-rounded">approval</span>请求审批</span> },
              { value: "auto", label: <span className="approval-mode-option"><span className="material-symbols-rounded">bolt</span>自动审批</span> },
            ]}
            suffixIcon={<span className="material-symbols-rounded">expand_more</span>}
          />
          <Select
            className="chat-agent-select" value={activeSession?.agent_id || ""}
            disabled={!currentSessionId || active} onChange={(value) => void switchAgent(value)}
            aria-label="选择会话 Agent"
            options={[{ value: "", label: "默认助手（不使用 Agent）" }, ...agents.map((agent) => ({ value: agent.id, label: agent.name }))]}
            suffixIcon={<span className="material-symbols-rounded">smart_toy</span>}
          />
          <div className={`status-pill${currentSessionId ? " active" : ""}${currentRun?.status === "queued" ? " queued" : ""}`} aria-live="polite"><i />{statusLabel}</div>
        </div>
      </header>

      {lastError ? <div className="chat-error" role="alert"><span className="material-symbols-rounded" aria-hidden="true">error</span>{lastError}<button type="button" aria-label="关闭错误提示" onClick={() => currentSessionId && setError(currentSessionId, null)}>×</button></div> : null}
      {currentApproval ? (
        <div className="tool-approval-layer">
          <ToolApprovalCard approval={currentApproval} submitting={approvalSubmitting} onDecision={(decision, input) => void decideApproval(decision, input)} />
        </div>
      ) : null}
      <MessageList
        messages={messages} streaming={streaming}
        onClarificationSelect={(option) => {
          if (currentSessionId) selectClarification(currentSessionId, option);
          void send(option);
        }}
        onClarificationOther={() => inputRef.current?.focus()}
      />

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
              <button type="button" className={`composer-plus${plusOpen ? " active" : ""}`} title="附加" aria-label="附加文件或图片" aria-expanded={plusOpen} onClick={() => { setPlusOpen((open) => !open); setModelOpen(false); }}><span className="material-symbols-rounded">add</span></button>
              {plusOpen ? <div className="composer-popover attachment-menu" role="menu"><div className="popover-eyebrow">添加</div><button type="button" role="menuitem" onClick={() => fileInputRef.current?.click()}><span className="material-symbols-rounded">attach_file</span><span><b>文件和图片</b><small>最多 6 个，单个不超过 20MB</small></span></button></div> : null}
            </div>
            <textarea
              ref={inputRef} value={text} rows={1} disabled={active || !currentSessionId}
              aria-label="发送给 Harness Agent 的消息"
              placeholder={currentApproval ? "请先处理上方的工具审批" : "输入消息，Enter 发送，Shift+Enter 换行"}
              onChange={(event) => { setText(event.target.value); if (lastError && currentSessionId) setError(currentSessionId, null); }}
              onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void send(); } }}
            />
            <div className="composer-model-anchor">
              <button type="button" className="composer-model-trigger" aria-label={`当前模型 ${activeModelLabel}，${model}`} aria-expanded={modelOpen} title={`${activeModelLabel} · ${model}`} onClick={() => { setModelOpen((open) => !open); setPlusOpen(false); }}><span className="material-symbols-rounded">memory</span><b>{activeModelLabel}</b><span className="material-symbols-rounded">expand_more</span></button>
              {modelOpen ? <div className="composer-popover model-menu" role="menu"><div className="popover-eyebrow">选择模型</div>{modelOptions.map((item) => <button type="button" role="menuitemradio" aria-checked={model === item.value} className={model === item.value ? "selected" : ""} key={item.value} onClick={() => { setModel(item.value); setModelOpen(false); inputRef.current?.focus(); }}><span className="model-menu-icon material-symbols-rounded">{item.custom ? "deployed_code" : item.value === VISION_CHAT_MODEL ? "image" : "bolt"}</span><span><b>{item.label}{item.custom ? <em>自定义</em> : null}</b><small title={item.value}>{item.description}</small></span>{model === item.value ? <span className="material-symbols-rounded model-check">check</span> : null}</button>)}<button type="button" className="model-menu-manage" onClick={() => { setModelOpen(false); navigate("/settings"); }}><span className="material-symbols-rounded">settings</span><span><b>管理模型</b><small>添加、编辑或移除自定义模型</small></span><span className="material-symbols-rounded model-check">arrow_forward</span></button></div> : null}
            </div>
            {streaming ? <button type="button" className="send-button stop" title="停止生成" aria-label="停止生成" disabled={currentRun?.stopping} onClick={() => void stop()}><span className="material-symbols-rounded">stop</span></button> : <button type="button" className="send-button" title="发送" aria-label="发送消息" disabled={!canSend} onClick={() => void send()}><span className="material-symbols-rounded">arrow_upward</span></button>}
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
