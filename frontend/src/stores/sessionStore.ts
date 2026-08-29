// sessionStore：会话列表、当前会话、消息、流式状态
import { create } from "zustand";
import type { SessionItem, SessionListData } from "@/types/api";
import type { ChatMessage, ChatRun, ChatRunStatus, ChatRunStepStatus } from "@/types/chat";
import type {
  MessageEnd,
  OrchestrationClarification,
  OrchestrationPlan,
  OrchestrationRouterDecision,
  OrchestrationSynthesis,
  OrchestrationWorkerFinished,
  OrchestrationWorkerStarted,
  RunEnd,
  RunStepEnd,
  RunStepStart,
  RunStepUpdate,
  TraceStart,
} from "@/types/sse";
import { runStepReferencesToKnowledgeReferences } from "@/utils/chatRun";
import { getData } from "@/api/http";

export type { ChatMessage } from "@/types/chat";

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

function updateLastAssistant(
  messages: ChatMessage[],
  update: (message: ChatMessage) => ChatMessage,
): ChatMessage[] {
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
  messages: ChatMessage[];
  streaming: boolean;
  lastError: string | null;
  setSessions: (s: SessionItem[]) => void;
  fetchSessions: () => Promise<void>;
  setSessionTitle: (id: string, title: string) => void;
  setSessionAgent: (id: string, agentId: string | null) => void;
  setCurrentSession: (id: string | null) => void;
  setMessages: (m: ChatMessage[]) => void;
  pushMessage: (m: ChatMessage) => void;
  updateLastAssistant: (text: string) => void;
  removeEmptyAssistant: () => void;
  beginRun: (event: TraceStart) => void;
  startRunStep: (event: RunStepStart) => void;
  updateRunStep: (event: RunStepUpdate) => void;
  endRunStep: (event: RunStepEnd) => void;
  setRunTokenUsage: (event: MessageEnd) => void;
  finishRun: (event: RunEnd) => void;
  setOrchestrationRouter: (event: OrchestrationRouterDecision) => void;
  setOrchestrationClarification: (event: OrchestrationClarification) => void;
  setOrchestrationPlan: (event: OrchestrationPlan) => void;
  startOrchestrationWorker: (event: OrchestrationWorkerStarted) => void;
  finishOrchestrationWorker: (event: OrchestrationWorkerFinished) => void;
  startOrchestrationSynthesis: (event: OrchestrationSynthesis) => void;
  selectClarification: (option: string) => void;
  disconnectOrchestration: () => void;
  failRun: () => void;
  cancelRun: () => void;
  setStreaming: (b: boolean) => void;
  setError: (e: string | null) => void;
  reset: () => void;
}

export const useSessionStore = create<SessionState>((set) => ({
  sessions: [],
  currentSessionId: null,
  messages: [],
  streaming: false,
  lastError: null,
  setSessions: (sessions) => set({ sessions }),
  fetchSessions: async () => {
    try {
      const data = await getData<SessionListData>("/sessions?page=1&page_size=20");
      set({ sessions: data.items });
    } catch {
      // 刷新失败时静默保留现有列表，不打断当前对话
    }
  },
  setSessionTitle: (id, title) =>
    set((state) => ({
      sessions: state.sessions.map((session) =>
        session.session_id === id ? { ...session, title } : session
      ),
    })),
  setSessionAgent: (id, agentId) =>
    set((state) => ({
      sessions: state.sessions.map((session) =>
        session.session_id === id ? { ...session, agent_id: agentId } : session
      ),
    })),
  setCurrentSession: (currentSessionId) => set({ currentSessionId }),
  setMessages: (messages) => set({ messages }),
  pushMessage: (msg) =>
    set((state) => ({ messages: [...state.messages, msg] })),
  updateLastAssistant: (text) =>
    set((state) => {
      const messages = [...state.messages];
      const last = messages[messages.length - 1];
      if (last && last.role === "assistant") {
        messages[messages.length - 1] = { ...last, content: last.content + text };
      } else {
        messages.push({ role: "assistant", content: text });
      }
      return { messages };
    }),
  removeEmptyAssistant: () =>
    set((state) => {
      const messages = [...state.messages];
      const last = messages[messages.length - 1];
      if (last && last.role === "assistant" && !last.content && !last.run) {
        messages.pop();
      }
      return { messages };
    }),
  beginRun: (event) => set((state) => ({
    messages: updateLastAssistant(state.messages, (message) => ({
      ...message,
      run: {
        ...ensureRun(message),
        traceId: event.trace_id,
        requestId: event.request_id ?? undefined,
      },
    })),
  })),
  startRunStep: (event) => set((state) => ({
    messages: updateLastAssistant(state.messages, (message) => {
      const run = ensureRun(message);
      const step = {
        id: event.id,
        kind: event.kind,
        name: event.name,
        parentId: event.parent_id,
        status: "running" as const,
        model: event.kind === "llm" ? event.summary ?? undefined : undefined,
        summary: event.kind === "llm" ? undefined : event.summary ?? undefined,
        startedAt: event.started_at ?? undefined,
      };
      const existing = run.steps.findIndex((item) => item.id === event.id);
      const steps = [...run.steps];
      if (existing >= 0) steps[existing] = { ...steps[existing], ...step };
      else steps.push(step);
      return { ...message, run: { ...run, status: "running", steps } };
    }),
  })),
  updateRunStep: (event) => set((state) => ({
    messages: updateLastAssistant(state.messages, (message) => {
      const run = ensureRun(message);
      return {
        ...message,
        run: {
          ...run,
          steps: run.steps.map((step) => step.id === event.id ? {
            ...step,
            phase: event.phase,
            detail: { ...step.detail, ...event.detail },
          } : step),
        },
      };
    }),
  })),
  endRunStep: (event) => set((state) => ({
    messages: updateLastAssistant(state.messages, (message) => {
      const run = ensureRun(message);
      return {
        ...message,
        run: {
          ...run,
          steps: run.steps.map((step) => step.id === event.id ? {
            ...step,
            status: normalizeStepStatus(event.status),
            durationMs: event.duration_ms,
            output: step.kind === "llm" ? event.summary ?? undefined : step.output,
            summary: step.kind !== "llm" && event.summary
              ? [step.summary, event.summary].filter(Boolean).join("\n")
              : step.summary,
            error: event.error ?? undefined,
            tokenUsage: event.token_usage ?? undefined,
            references: runStepReferencesToKnowledgeReferences(event.references),
          } : step),
        },
      };
    }),
  })),
  setRunTokenUsage: (event) => set((state) => ({
    messages: updateLastAssistant(state.messages, (message) => ({
      ...message,
      run: { ...ensureRun(message), tokenUsage: event.token_usage },
    })),
  })),
  finishRun: (event) => set((state) => ({
    messages: updateLastAssistant(state.messages, (message) => ({
      ...message,
      run: {
        ...ensureRun(message),
        status: normalizeRunStatus(event.status),
        durationMs: event.duration_ms,
        toolCalls: event.tool_calls,
      },
      orchestration: message.orchestration ? {
        ...message.orchestration,
        phase: "done",
        connectionState: "connected",
      } : undefined,
    })),
  })),
  setOrchestrationRouter: (event) => set((state) => ({
    messages: updateLastAssistant(state.messages, (message) => ({
      ...message,
      orchestration: {
        mode: event.mode,
        targetAgent: event.target_agent,
        confidence: event.confidence,
        reason: event.reason,
        workers: {},
        phase: event.mode === "orchestrate" ? "planning" : "executing",
        connectionState: "connected",
      },
    })),
  })),
  setOrchestrationClarification: (event) => set((state) => ({
    messages: updateLastAssistant(state.messages, (message) => ({
      ...message,
      orchestration: {
        ...(message.orchestration ?? {
          mode: "direct" as const,
          targetAgent: "main",
          confidence: 0,
          reason: "需要补充信息",
          workers: {},
          phase: "routing" as const,
        }),
        clarification: { question: event.question, options: event.options },
      },
    })),
  })),
  setOrchestrationPlan: (event) => set((state) => ({
    messages: updateLastAssistant(state.messages, (message) => ({
      ...message,
      orchestration: {
        ...(message.orchestration ?? {
          mode: "orchestrate" as const,
          targetAgent: "main",
          confidence: 0,
          reason: "已生成执行计划",
          workers: {},
          phase: "planning" as const,
        }),
        mode: "orchestrate",
        summary: event.summary,
        execution: event.execution,
        phase: "executing",
        workers: Object.fromEntries(event.tasks.map((task) => [task.task_id, {
          taskId: task.task_id,
          agentName: task.target_agent,
          goal: task.goal,
          constraints: task.constraints ?? [],
          status: "pending" as const,
        }])),
      },
    })),
  })),
  startOrchestrationWorker: (event) => set((state) => ({
    messages: updateLastAssistant(state.messages, (message) => {
      if (!message.orchestration) return message;
      const current = message.orchestration.workers[event.task_id];
      return {
        ...message,
        orchestration: {
          ...message.orchestration,
          phase: "executing",
          workers: {
            ...message.orchestration.workers,
            [event.task_id]: {
              taskId: event.task_id,
              agentName: event.agent_name,
              goal: current?.goal ?? "执行子任务",
              constraints: current?.constraints ?? [],
              status: "running",
            },
          },
        },
      };
    }),
  })),
  finishOrchestrationWorker: (event) => set((state) => ({
    messages: updateLastAssistant(state.messages, (message) => {
      if (!message.orchestration) return message;
      const current = message.orchestration.workers[event.task_id];
      const workers = {
        ...message.orchestration.workers,
        [event.task_id]: {
          taskId: event.task_id,
          agentName: event.agent_name,
          goal: current?.goal ?? "执行子任务",
          constraints: current?.constraints ?? [],
          status: event.status,
          durationMs: event.duration_ms,
          resultPreview: event.result_preview,
          error: event.error ?? undefined,
        },
      };
      const allFinished = Object.values(workers).every((worker) => worker.status === "done" || worker.status === "failed");
      return {
        ...message,
        orchestration: {
          ...message.orchestration,
          workers,
          phase: allFinished ? "verifying" : "executing",
        },
      };
    }),
  })),
  startOrchestrationSynthesis: () => set((state) => ({
    messages: updateLastAssistant(state.messages, (message) => message.orchestration ? {
      ...message,
      orchestration: { ...message.orchestration, phase: "synthesizing" },
    } : message),
  })),
  selectClarification: (option) => set((state) => ({
    messages: updateLastAssistant(state.messages, (message) => message.orchestration?.clarification ? {
      ...message,
      orchestration: {
        ...message.orchestration,
        clarification: { ...message.orchestration.clarification, selected: option },
      },
    } : message),
  })),
  disconnectOrchestration: () => set((state) => ({
    messages: updateLastAssistant(state.messages, (message) => message.orchestration ? {
      ...message,
      orchestration: { ...message.orchestration, connectionState: "disconnected" },
    } : message),
  })),
  failRun: () => set((state) => ({
    messages: updateLastAssistant(state.messages, (message) => ({
      ...message,
      run: { ...ensureRun(message), status: "error" },
    })),
  })),
  cancelRun: () => set((state) => ({
    messages: updateLastAssistant(state.messages, (message) => ({
      ...message,
      run: { ...ensureRun(message), status: "canceled" },
      orchestration: message.orchestration ? {
        ...message.orchestration,
        connectionState: "stopped",
      } : undefined,
    })),
  })),
  setStreaming: (streaming) => set({ streaming }),
  setError: (lastError) => set({ lastError }),
  reset: () =>
    set({ currentSessionId: null, messages: [], streaming: false, lastError: null }),
}));
