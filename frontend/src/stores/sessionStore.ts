// sessionStore：会话列表、当前会话、消息、流式状态
import { create } from "zustand";
import type { SessionItem } from "@/types/api";
import type { ChatMessage, ChatRun, ChatRunStatus, ChatRunStepStatus } from "@/types/chat";
import type { MessageEnd, RunEnd, RunStepEnd, RunStepStart, RunStepUpdate, TraceStart } from "@/types/sse";
import { runStepReferencesToKnowledgeReferences } from "@/utils/chatRun";

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
  setSessionTitle: (id: string, title: string) => void;
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
  setSessionTitle: (id, title) =>
    set((state) => ({
      sessions: state.sessions.map((session) =>
        session.session_id === id ? { ...session, title } : session
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
    })),
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
    })),
  })),
  setStreaming: (streaming) => set({ streaming }),
  setError: (lastError) => set({ lastError }),
  reset: () =>
    set({ currentSessionId: null, messages: [], streaming: false, lastError: null }),
}));
