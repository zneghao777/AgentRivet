// sessionStore：按会话隔离消息、运行、加载和错误状态。
import { create } from "zustand";
import { getData } from "@/api/http";
import type { SessionItem, SessionListData } from "@/types/api";
import type { ChatMessage, ChatRun, ChatRunStatus, ChatRunStepStatus } from "@/types/chat";
import type {
  InterruptRequested, MessageEnd, OrchestrationClarification, OrchestrationPlan,
  OrchestrationRouterDecision, OrchestrationSynthesis, OrchestrationWorkerFinished,
  OrchestrationWorkerStarted, RunEnd, RunStepEnd, RunStepStart, RunStepUpdate, TraceStart,
} from "@/types/sse";
import { runStepReferencesToKnowledgeReferences } from "@/utils/chatRun";

export type { ChatMessage } from "@/types/chat";
export type DurableRunStatus = "queued" | "running" | "pending_approval" | "succeeded" | "failed" | "canceled";

export interface ChatRunViewState {
  runId: string;
  sessionId: string;
  status: DurableRunStatus;
  lastEventSequence: number;
  content: string;
  pendingApproval: InterruptRequested | null;
  error: string | null;
  unseen: boolean;
  stopping: boolean;
}

export const EMPTY_MESSAGES: ChatMessage[] = [];

function normalizeRunStatus(status: string): ChatRunStatus {
  if (status === "success" || status === "ok") return "success";
  if (status === "timeout" || status === "canceled" || status === "interrupted") return status;
  return "error";
}

function normalizeStepStatus(status: string): ChatRunStepStatus {
  if (status === "success" || status === "ok") return "ok";
  if (status === "timeout" || status === "canceled" || status === "interrupted") return status;
  return status === "running" ? "running" : "error";
}

function updateLastAssistant(messages: ChatMessage[], update: (message: ChatMessage) => ChatMessage): ChatMessage[] {
  const next = [...messages];
  for (let index = next.length - 1; index >= 0; index -= 1) {
    if (next[index].role === "assistant") {
      next[index] = update(next[index]);
      return next;
    }
  }
  next.push(update({ role: "assistant", content: "" }));
  return next;
}

function ensureRun(message: ChatMessage): ChatRun {
  return message.run ?? { status: "running", startedAt: Date.now(), steps: [] };
}

interface SessionState {
  sessions: SessionItem[];
  currentSessionId: string | null;
  messagesBySession: Record<string, ChatMessage[]>;
  runsBySession: Record<string, ChatRunViewState>;
  loadingBySession: Record<string, boolean>;
  errorsBySession: Record<string, string | null>;
  setSessions: (sessions: SessionItem[]) => void;
  fetchSessions: () => Promise<void>;
  setSessionTitle: (id: string, title: string) => void;
  setSessionAgent: (id: string, agentId: string | null) => void;
  setCurrentSession: (id: string | null) => void;
  setMessages: (sessionId: string, messages: ChatMessage[]) => void;
  pushMessage: (sessionId: string, message: ChatMessage) => void;
  appendAssistantText: (sessionId: string, text: string) => void;
  replaceAssistantText: (sessionId: string, text: string) => void;
  removeEmptyAssistant: (sessionId: string) => void;
  setRunView: (sessionId: string, run: ChatRunViewState) => void;
  patchRunView: (sessionId: string, patch: Partial<ChatRunViewState>) => void;
  markRunViewed: (sessionId: string) => void;
  setLoading: (sessionId: string, loading: boolean) => void;
  setError: (sessionId: string, error: string | null) => void;
  beginRun: (sessionId: string, event: TraceStart) => void;
  startRunStep: (sessionId: string, event: RunStepStart) => void;
  updateRunStep: (sessionId: string, event: RunStepUpdate) => void;
  endRunStep: (sessionId: string, event: RunStepEnd) => void;
  setRunTokenUsage: (sessionId: string, event: MessageEnd) => void;
  finishRun: (sessionId: string, event: RunEnd) => void;
  setOrchestrationRouter: (sessionId: string, event: OrchestrationRouterDecision) => void;
  setOrchestrationClarification: (sessionId: string, event: OrchestrationClarification) => void;
  setOrchestrationPlan: (sessionId: string, event: OrchestrationPlan) => void;
  startOrchestrationWorker: (sessionId: string, event: OrchestrationWorkerStarted) => void;
  finishOrchestrationWorker: (sessionId: string, event: OrchestrationWorkerFinished) => void;
  startOrchestrationSynthesis: (sessionId: string, event: OrchestrationSynthesis) => void;
  selectClarification: (sessionId: string, option: string) => void;
  disconnectOrchestration: (sessionId: string) => void;
  failRun: (sessionId: string) => void;
  cancelRun: (sessionId: string) => void;
  reset: () => void;
}

type StoreSet = (partial: Partial<SessionState> | ((state: SessionState) => Partial<SessionState>)) => void;

function mutateMessages(set: StoreSet, sessionId: string, update: (messages: ChatMessage[]) => ChatMessage[]) {
  set((state) => ({
    messagesBySession: {
      ...state.messagesBySession,
      [sessionId]: update(state.messagesBySession[sessionId] ?? EMPTY_MESSAGES),
    },
  }));
}

export const useSessionStore = create<SessionState>((set) => ({
  sessions: [], currentSessionId: null, messagesBySession: {}, runsBySession: {}, loadingBySession: {}, errorsBySession: {},
  setSessions: (sessions) => set({ sessions }),
  fetchSessions: async () => {
    try {
      const data = await getData<SessionListData>("/sessions?page=1&page_size=100");
      set({ sessions: data.items });
    } catch { /* 保留各会话已有状态。 */ }
  },
  setSessionTitle: (id, title) => set((state) => ({
    sessions: state.sessions.map((session) => session.session_id === id ? { ...session, title } : session),
  })),
  setSessionAgent: (id, agentId) => set((state) => ({
    sessions: state.sessions.map((session) => session.session_id === id ? { ...session, agent_id: agentId } : session),
  })),
  setCurrentSession: (currentSessionId) => set((state) => ({
    currentSessionId,
    runsBySession: currentSessionId && state.runsBySession[currentSessionId]
      ? { ...state.runsBySession, [currentSessionId]: { ...state.runsBySession[currentSessionId], unseen: false } }
      : state.runsBySession,
  })),
  setMessages: (sessionId, messages) => set((state) => ({ messagesBySession: { ...state.messagesBySession, [sessionId]: messages } })),
  pushMessage: (sessionId, message) => mutateMessages(set, sessionId, (messages) => [...messages, message]),
  appendAssistantText: (sessionId, text) => mutateMessages(set, sessionId, (messages) => {
    const next = [...messages];
    const last = next[next.length - 1];
    if (last?.role === "assistant") next[next.length - 1] = { ...last, content: last.content + text };
    else next.push({ role: "assistant", content: text });
    return next;
  }),
  replaceAssistantText: (sessionId, text) => mutateMessages(set, sessionId, (messages) =>
    updateLastAssistant(messages, (message) => ({ ...message, content: text }))),
  removeEmptyAssistant: (sessionId) => mutateMessages(set, sessionId, (messages) => {
    const next = [...messages]; const last = next[next.length - 1];
    if (last?.role === "assistant" && !last.content && !last.run) next.pop();
    return next;
  }),
  setRunView: (sessionId, run) => set((state) => ({ runsBySession: { ...state.runsBySession, [sessionId]: run } })),
  patchRunView: (sessionId, patch) => set((state) => {
    const current = state.runsBySession[sessionId];
    return current ? { runsBySession: { ...state.runsBySession, [sessionId]: { ...current, ...patch } } } : {};
  }),
  markRunViewed: (sessionId) => set((state) => {
    const current = state.runsBySession[sessionId];
    return current ? { runsBySession: { ...state.runsBySession, [sessionId]: { ...current, unseen: false } } } : {};
  }),
  setLoading: (sessionId, loading) => set((state) => ({ loadingBySession: { ...state.loadingBySession, [sessionId]: loading } })),
  setError: (sessionId, error) => set((state) => ({ errorsBySession: { ...state.errorsBySession, [sessionId]: error } })),
  beginRun: (sessionId, event) => mutateMessages(set, sessionId, (messages) => updateLastAssistant(messages, (message) => ({
    ...message, run: { ...ensureRun(message), traceId: event.trace_id, requestId: event.request_id ?? undefined },
  }))),
  startRunStep: (sessionId, event) => mutateMessages(set, sessionId, (messages) => updateLastAssistant(messages, (message) => {
    const run = ensureRun(message);
    const step = { id: event.id, kind: event.kind, name: event.name, parentId: event.parent_id, status: "running" as const,
      model: event.kind === "llm" ? event.summary ?? undefined : undefined,
      summary: event.kind === "llm" ? undefined : event.summary ?? undefined, startedAt: event.started_at ?? undefined };
    const steps = [...run.steps]; const existing = steps.findIndex((item) => item.id === event.id);
    if (existing >= 0) steps[existing] = { ...steps[existing], ...step }; else steps.push(step);
    return { ...message, run: { ...run, status: "running", steps } };
  })),
  updateRunStep: (sessionId, event) => mutateMessages(set, sessionId, (messages) => updateLastAssistant(messages, (message) => {
    const run = ensureRun(message);
    return { ...message, run: { ...run, steps: run.steps.map((step) => step.id === event.id
      ? { ...step, phase: event.phase, detail: { ...step.detail, ...event.detail } } : step) } };
  })),
  endRunStep: (sessionId, event) => mutateMessages(set, sessionId, (messages) => updateLastAssistant(messages, (message) => {
    const run = ensureRun(message);
    return { ...message, run: { ...run, steps: run.steps.map((step) => step.id === event.id ? {
      ...step, status: normalizeStepStatus(event.status), durationMs: event.duration_ms,
      output: step.kind === "llm" ? event.summary ?? undefined : step.output,
      summary: step.kind !== "llm" && event.summary ? [step.summary, event.summary].filter(Boolean).join("\n") : step.summary,
      error: event.error ?? undefined, tokenUsage: event.token_usage ?? undefined,
      references: runStepReferencesToKnowledgeReferences(event.references),
    } : step) } };
  })),
  setRunTokenUsage: (sessionId, event) => mutateMessages(set, sessionId, (messages) => updateLastAssistant(messages, (message) => ({
    ...message, run: { ...ensureRun(message), tokenUsage: event.token_usage },
  }))),
  finishRun: (sessionId, event) => mutateMessages(set, sessionId, (messages) => updateLastAssistant(messages, (message) => ({
    ...message,
    run: { ...ensureRun(message), status: normalizeRunStatus(event.status), durationMs: event.duration_ms, toolCalls: event.tool_calls },
    orchestration: message.orchestration ? { ...message.orchestration, phase: "done", connectionState: "connected" } : undefined,
  }))),
  setOrchestrationRouter: (sessionId, event) => mutateMessages(set, sessionId, (messages) => updateLastAssistant(messages, (message) => ({
    ...message, orchestration: { mode: event.mode, targetAgent: event.target_agent, confidence: event.confidence, reason: event.reason,
      workers: {}, phase: event.mode === "orchestrate" ? "planning" : "executing", connectionState: "connected" },
  }))),
  setOrchestrationClarification: (sessionId, event) => mutateMessages(set, sessionId, (messages) => updateLastAssistant(messages, (message) => ({
    ...message, orchestration: { ...(message.orchestration ?? { mode: "direct" as const, targetAgent: "main", confidence: 0,
      reason: "需要补充信息", workers: {}, phase: "routing" as const }), clarification: { question: event.question, options: event.options } },
  }))),
  setOrchestrationPlan: (sessionId, event) => mutateMessages(set, sessionId, (messages) => updateLastAssistant(messages, (message) => ({
    ...message, orchestration: { ...(message.orchestration ?? { mode: "orchestrate" as const, targetAgent: "main", confidence: 0,
      reason: "已生成执行计划", workers: {}, phase: "planning" as const }), mode: "orchestrate", summary: event.summary,
      execution: event.execution, phase: "executing", workers: Object.fromEntries(event.tasks.map((task) => [task.task_id,
        { taskId: task.task_id, agentName: task.target_agent, goal: task.goal, constraints: task.constraints ?? [], status: "pending" as const }])) },
  }))),
  startOrchestrationWorker: (sessionId, event) => mutateMessages(set, sessionId, (messages) => updateLastAssistant(messages, (message) => {
    if (!message.orchestration) return message; const current = message.orchestration.workers[event.task_id];
    return { ...message, orchestration: { ...message.orchestration, phase: "executing", workers: { ...message.orchestration.workers,
      [event.task_id]: { taskId: event.task_id, agentName: event.agent_name, goal: current?.goal ?? "执行子任务",
        constraints: current?.constraints ?? [], status: "running" } } } };
  })),
  finishOrchestrationWorker: (sessionId, event) => mutateMessages(set, sessionId, (messages) => updateLastAssistant(messages, (message) => {
    if (!message.orchestration) return message; const current = message.orchestration.workers[event.task_id];
    const workers = { ...message.orchestration.workers, [event.task_id]: { taskId: event.task_id, agentName: event.agent_name,
      goal: current?.goal ?? "执行子任务", constraints: current?.constraints ?? [], status: event.status,
      durationMs: event.duration_ms, resultPreview: event.result_preview, error: event.error ?? undefined } };
    const allFinished = Object.values(workers).every((worker) => worker.status === "done" || worker.status === "failed");
    return { ...message, orchestration: { ...message.orchestration, workers, phase: allFinished ? "verifying" : "executing" } };
  })),
  startOrchestrationSynthesis: (sessionId) => mutateMessages(set, sessionId, (messages) => updateLastAssistant(messages, (message) =>
    message.orchestration ? { ...message, orchestration: { ...message.orchestration, phase: "synthesizing" } } : message)),
  selectClarification: (sessionId, option) => mutateMessages(set, sessionId, (messages) => updateLastAssistant(messages, (message) =>
    message.orchestration?.clarification ? { ...message, orchestration: { ...message.orchestration,
      clarification: { ...message.orchestration.clarification, selected: option } } } : message)),
  disconnectOrchestration: (sessionId) => mutateMessages(set, sessionId, (messages) => updateLastAssistant(messages, (message) =>
    message.orchestration ? { ...message, orchestration: { ...message.orchestration, connectionState: "disconnected" } } : message)),
  failRun: (sessionId) => mutateMessages(set, sessionId, (messages) => updateLastAssistant(messages, (message) => ({
    ...message, run: { ...ensureRun(message), status: "error" },
  }))),
  cancelRun: (sessionId) => mutateMessages(set, sessionId, (messages) => updateLastAssistant(messages, (message) => ({
    ...message, run: { ...ensureRun(message), status: "canceled" },
    orchestration: message.orchestration ? { ...message.orchestration, connectionState: "stopped" } : undefined,
  }))),
  reset: () => set({ currentSessionId: null, messagesBySession: {}, runsBySession: {}, loadingBySession: {}, errorsBySession: {} }),
}));
