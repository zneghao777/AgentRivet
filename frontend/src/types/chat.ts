import type { RunStepKind } from "@/types/sse";

export type ChatRunStatus =
  | "running"
  | "success"
  | "error"
  | "timeout"
  | "canceled"
  | "interrupted";

export type ChatRunStepStatus =
  | "running"
  | "ok"
  | "error"
  | "timeout"
  | "canceled"
  | "interrupted";

export interface KnowledgeReference {
  rank: number;
  kbId: string;
  chunkId: number;
  docId?: number;
  sourceFile: string;
  chunkIndex?: number;
  heading?: string;
  lineStart?: number;
  lineEnd?: number;
  score?: number;
  strategy?: string;
  excerpt?: string;
}

export interface ChatRunStep {
  id: string;
  kind: RunStepKind;
  name: string;
  parentId: string | null;
  status: ChatRunStepStatus;
  model?: string;
  output?: string;
  summary?: string;
  error?: string;
  startedAt?: string;
  durationMs?: number;
  tokenUsage?: Record<string, number>;
  phase?: string;
  detail?: Record<string, unknown>;
  references?: KnowledgeReference[];
}

export interface ChatRun {
  traceId?: string;
  requestId?: string;
  status: ChatRunStatus;
  startedAt: number;
  durationMs?: number;
  toolCalls?: number;
  tokenUsage?: Record<string, number>;
  steps: ChatRunStep[];
}

export type OrchestrationWorkerStatus = "pending" | "running" | "done" | "failed";

export interface OrchestrationWorkerState {
  taskId: string;
  agentName: string;
  goal: string;
  constraints: string[];
  status: OrchestrationWorkerStatus;
  durationMs?: number;
  resultPreview?: string;
  error?: string;
}

export interface ChatOrchestration {
  mode: "direct" | "orchestrate";
  targetAgent: string;
  confidence: number;
  reason: string;
  summary?: string;
  execution?: "parallel" | "sequential";
  workers: Record<string, OrchestrationWorkerState>;
  phase: "routing" | "planning" | "executing" | "verifying" | "synthesizing" | "done";
  clarification?: { question: string; options: string[]; selected?: string };
  connectionState?: "connected" | "disconnected" | "stopped";
}

export interface ChatMessage {
  id?: number | string;
  role: "user" | "assistant" | "system";
  content: string;
  createdAt?: string;
  run?: ChatRun;
  attachments?: ChatAttachment[];
  agentId?: string;
  agentName?: string;
  orchestration?: ChatOrchestration;
}

export interface ChatAttachment {
  id: string;
  name: string;
  mime_type: string;
  size: number;
  kind: "image" | "document";
  previewUrl?: string;
}
