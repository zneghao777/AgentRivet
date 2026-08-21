import type { ChatRun, ChatRunStatus, ChatRunStepStatus, KnowledgeReference } from "@/types/chat";
import type { TraceEventsData } from "@/types/observability";
import type { RunStepReference } from "@/types/sse";

export function runStepReferencesToKnowledgeReferences(
  references: RunStepReference[] | null | undefined,
): KnowledgeReference[] {
  return (references ?? []).map((reference) => ({
    rank: reference.rank,
    kbId: reference.kb_id,
    chunkId: reference.chunk_id,
    docId: reference.doc_id ?? undefined,
    sourceFile: reference.source_file,
    chunkIndex: reference.chunk_index ?? undefined,
    heading: reference.heading ?? undefined,
    lineStart: reference.line_start ?? undefined,
    lineEnd: reference.line_end ?? undefined,
    score: reference.score ?? undefined,
    strategy: reference.strategy ?? undefined,
    excerpt: reference.excerpt ?? undefined,
  }));
}

function runStatus(status: string): ChatRunStatus {
  if (status === "ok" || status === "success") return "success";
  if (status === "timeout" || status === "canceled" || status === "interrupted") return status;
  return "error";
}

function stepStatus(status: string): ChatRunStepStatus {
  if (status === "ok" || status === "success") return "ok";
  if (status === "running") return "running";
  if (status === "timeout" || status === "canceled" || status === "interrupted") return status;
  return "error";
}

export function traceEventsToChatRun(data: TraceEventsData): ChatRun {
  const startedAt = data.trace.started_at ? new Date(data.trace.started_at).getTime() : Date.now();
  return {
    traceId: data.trace.trace_id,
    requestId: data.trace.request_id ?? undefined,
    status: runStatus(data.trace.status),
    startedAt: Number.isFinite(startedAt) ? startedAt : Date.now(),
    durationMs: data.trace.duration_ms,
    toolCalls: data.trace.tool_calls,
    tokenUsage: data.trace.token_usage ?? undefined,
    steps: data.events
      .filter((event) => event.event_id.startsWith("span:") && event.span_id)
      .map((event) => ({
        id: event.span_id as string,
        kind: event.kind === "tool" ? "tool" as const : event.kind === "node" ? "node" as const : "llm" as const,
        name: event.name,
        parentId: event.parent_span_id,
        status: stepStatus(event.status),
        model: event.kind === "assistant" ? event.model ?? undefined : undefined,
        output: event.kind === "assistant" ? event.output_summary ?? undefined : undefined,
        summary: event.kind === "tool"
          ? ([event.input_summary, event.output_summary].filter(Boolean).join("\n") || event.summary || undefined)
          : undefined,
        error: event.error ?? undefined,
        startedAt: event.started_at ?? undefined,
        durationMs: event.duration_ms,
        tokenUsage: event.token_usage ?? undefined,
        detail: event.ttft_ms == null ? undefined : { ttft_ms: event.ttft_ms },
        phase: event.ttft_ms == null ? undefined : "first_token",
        references: runStepReferencesToKnowledgeReferences(event.references),
      })),
  };
}
