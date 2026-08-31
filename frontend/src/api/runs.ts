import { http, getData, postData } from "@/api/http";
import { API_KEY_STORAGE_KEY, DEFAULT_API_KEY } from "@/constants";
import type { ChatRunItem, ChatRunListData } from "@/types/api";
import type { ChatRuntimeOptions } from "@/api/sse";

export interface CreateRunRequest extends ChatRuntimeOptions {
  message: string;
  approval_mode?: "manual" | "auto";
}

export interface RunEventHandlers {
  onEvent(event: string, data: Record<string, unknown>, sequence: number): void;
  onClose?(): void;
}

export async function createChatRun(
  sessionId: string,
  body: CreateRunRequest,
  idempotencyKey = crypto.randomUUID(),
): Promise<ChatRunItem> {
  const response = await http.post(`/sessions/${sessionId}/runs`, body, {
    headers: { "Idempotency-Key": idempotencyKey },
  });
  return response.data.data as ChatRunItem;
}

export function getChatRun(runId: string): Promise<ChatRunItem> {
  return getData<ChatRunItem>(`/runs/${runId}`);
}

export function getActiveChatRun(sessionId: string): Promise<ChatRunItem | null> {
  return getData<ChatRunItem | null>(`/sessions/${sessionId}/active-run`);
}

export async function listActiveChatRuns(): Promise<ChatRunItem[]> {
  const data = await getData<{ items: ChatRunItem[] }>("/runs/active");
  return data.items;
}

export function listChatRuns(sessionId: string): Promise<ChatRunListData> {
  return getData<ChatRunListData>(`/sessions/${sessionId}/runs?page=1&page_size=20`);
}

export function cancelChatRun(runId: string): Promise<ChatRunItem> {
  return postData<ChatRunItem>(`/runs/${runId}/cancel`);
}

export function resumeChatRun(
  runId: string,
  decision: "approve" | "reject",
  input: string,
): Promise<ChatRunItem> {
  return postData<ChatRunItem>(`/runs/${runId}/resume`, { decision, input });
}

export async function subscribeChatRun(
  runId: string,
  afterSequence: number,
  handlers: RunEventHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const apiKey = localStorage.getItem(API_KEY_STORAGE_KEY) || DEFAULT_API_KEY;
  const base = import.meta.env.VITE_API_BASE ?? "/api/v1";
  const response = await fetch(`${base}/runs/${runId}/events?after_sequence=${Math.max(0, afterSequence)}`, {
    headers: {
      Accept: "text/event-stream",
      "Last-Event-ID": String(Math.max(0, afterSequence)),
      ...(apiKey ? { "X-API-Key": apiKey } : {}),
    },
    signal,
  });
  if (!response.ok || !response.body) throw new Error(`订阅运行失败: HTTP ${response.status}`);
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let boundary = buffer.indexOf("\n\n");
      while (boundary >= 0) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        let event = "";
        let sequence = 0;
        const data: string[] = [];
        for (const rawLine of frame.split("\n")) {
          const line = rawLine.replace(/\r$/, "");
          if (line.startsWith("id:")) sequence = Number(line.slice(3).trim()) || 0;
          else if (line.startsWith("event:")) event = line.slice(6).trim();
          else if (line.startsWith("data:")) data.push(line.slice(5).trim());
        }
        if (event && data.length) {
          try {
            const payload = JSON.parse(data.join("\n"));
            if (payload && typeof payload === "object") handlers.onEvent(event, payload, sequence);
          } catch { /* 忽略单个损坏帧，下一次游标查询会补播。 */ }
        }
        boundary = buffer.indexOf("\n\n");
      }
    }
  } finally {
    handlers.onClose?.();
  }
}
