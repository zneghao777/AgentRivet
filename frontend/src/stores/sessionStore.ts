// sessionStore：会话列表、当前会话、消息、流式状态
import { create } from "zustand";
import type { SessionItem } from "@/types/api";
import type { ToolStart, ToolEnd } from "@/types/sse";

export interface ChatToolCall {
  name: string;
  arguments?: Record<string, unknown>;
  result?: string;
}

export interface ChatMessage {
  role: "user" | "assistant" | "system";
  content: string;
  tools?: ChatToolCall[];
  createdAt?: string;
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
  addToolCall: (t: ToolStart) => void;
  completeToolCall: (t: ToolEnd) => void;
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
      if (last && last.role === "assistant" && !last.content && !last.tools?.length) {
        messages.pop();
      }
      return { messages };
    }),
  addToolCall: (t) =>
    set((state) => {
      const messages = [...state.messages];
      const last = messages[messages.length - 1];
      const tools = [...(last?.tools ?? []), { name: t.name, arguments: t.arguments }];
      if (last && last.role === "assistant") {
        messages[messages.length - 1] = { ...last, tools };
      } else {
        messages.push({ role: "assistant", content: "", tools });
      }
      return { messages };
    }),
  completeToolCall: (t) =>
    set((state) => {
      const messages = [...state.messages];
      for (let index = 0; index < messages.length; index += 1) {
        const m = messages[index];
        if (m.tools) {
          const toolIndex = m.tools.findIndex((x) => x.name === t.name);
          if (toolIndex >= 0) {
            const tools = [...m.tools];
            tools[toolIndex] = { ...tools[toolIndex], result: t.result };
            messages[index] = { ...m, tools };
          }
        }
      }
      return { messages };
    }),
  setStreaming: (streaming) => set({ streaming }),
  setError: (lastError) => set({ lastError }),
  reset: () =>
    set({ currentSessionId: null, messages: [], streaming: false, lastError: null }),
}));
