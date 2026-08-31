import {
  cancelChatRun, createChatRun, getActiveChatRun, getChatRun, listActiveChatRuns,
  resumeChatRun, subscribeChatRun,
  type CreateRunRequest,
} from "@/api/runs";
import { useSessionStore, type ChatRunViewState, type DurableRunStatus } from "@/stores/sessionStore";
import type {
  InterruptRequested, MessageEnd, OrchestrationClarification, OrchestrationPlan,
  OrchestrationRouterDecision, OrchestrationSynthesis, OrchestrationWorkerFinished,
  OrchestrationWorkerStarted, RunEnd, RunStepEnd, RunStepStart, RunStepUpdate, TraceStart,
} from "@/types/sse";
import type { ChatRunItem } from "@/types/api";

interface Subscription {
  sessionId: string;
  controller: AbortController;
  promise: Promise<void>;
}

const TERMINAL = new Set<DurableRunStatus>(["succeeded", "failed", "canceled"]);

class ChatRunManager {
  private subscriptions = new Map<string, Subscription>();
  private channel = typeof BroadcastChannel === "undefined" ? null : new BroadcastChannel("agent-harness-runs");

  constructor() {
    this.channel?.addEventListener("message", (event) => {
      const runId = event.data?.run_id;
      if (typeof runId === "string") void this.refresh(runId);
    });
  }

  async create(sessionId: string, request: CreateRunRequest): Promise<ChatRunItem> {
    const run = await createChatRun(sessionId, request);
    this.syncSnapshot(run, true);
    this.subscribe(run.run_id, sessionId, run.last_event_sequence);
    return run;
  }

  async restoreSession(sessionId: string): Promise<ChatRunItem | null> {
    const run = await getActiveChatRun(sessionId);
    if (!run) return null;
    this.syncSnapshot(run, false);
    this.subscribe(run.run_id, sessionId, run.last_event_sequence);
    return run;
  }

  async restoreAllActiveRuns(): Promise<void> {
    const runs = await listActiveChatRuns();
    for (const run of runs) {
      this.syncSnapshot(run, false);
      this.subscribe(run.run_id, run.session_id, run.last_event_sequence);
    }
  }

  async stop(sessionId: string): Promise<void> {
    const run = useSessionStore.getState().runsBySession[sessionId];
    if (!run || TERMINAL.has(run.status)) return;
    useSessionStore.getState().patchRunView(sessionId, { stopping: true });
    try {
      const current = await cancelChatRun(run.runId);
      this.syncSnapshot(current, false);
    } catch (error) {
      const current = await getChatRun(run.runId);
      this.syncSnapshot(current, false);
      if (current.status !== "canceled") throw error;
    }
  }

  async resume(sessionId: string, decision: "approve" | "reject", input: string): Promise<void> {
    const run = useSessionStore.getState().runsBySession[sessionId];
    if (!run || run.status !== "pending_approval") return;
    useSessionStore.getState().patchRunView(sessionId, { pendingApproval: null, status: "queued" });
    try {
      const current = await resumeChatRun(run.runId, decision, input);
      this.syncSnapshot(current, false);
      this.subscribe(run.runId, sessionId, run.lastEventSequence);
    } catch (error) {
      await this.refresh(run.runId);
      throw error;
    }
  }

  subscribe(runId: string, sessionId: string, afterSequence: number): Promise<void> {
    const existing = this.subscriptions.get(runId);
    if (existing) return existing.promise;
    const controller = new AbortController();
    const promise = subscribeChatRun(runId, afterSequence, {
      onEvent: (event, data, sequence) => this.handleEvent(sessionId, runId, event, data, sequence),
      onClose: () => this.subscriptions.delete(runId),
    }, controller.signal).catch((error) => {
      if (!controller.signal.aborted) {
        useSessionStore.getState().setError(sessionId, error instanceof Error ? error.message : "运行订阅中断");
      }
    });
    this.subscriptions.set(runId, { sessionId, controller, promise });
    return promise;
  }

  private async refresh(runId: string): Promise<void> {
    try {
      const run = await getChatRun(runId);
      this.syncSnapshot(run, false, false);
      if (!TERMINAL.has(run.status)) this.subscribe(run.run_id, run.session_id, run.last_event_sequence);
    } catch { /* 跨窗口通知仅作为提示，后端查询失败时保留现状。 */ }
  }

  private syncSnapshot(run: ChatRunItem, unseen: boolean, notify = true) {
    const state = useSessionStore.getState();
    const previous = state.runsBySession[run.session_id];
    const view: ChatRunViewState = {
      runId: run.run_id,
      sessionId: run.session_id,
      status: run.status,
      lastEventSequence: run.last_event_sequence,
      content: run.content || previous?.content || "",
      pendingApproval: run.pending_approval,
      error: run.error_message,
      unseen: unseen || (state.currentSessionId !== run.session_id && run.status === "succeeded"),
      stopping: false,
    };
    state.setRunView(run.session_id, view);
    if (run.content) state.replaceAssistantText(run.session_id, run.content);
    if (notify) this.broadcast(view);
  }

  private handleEvent(sessionId: string, runId: string, event: string, data: Record<string, unknown>, sequence: number) {
    const state = useSessionStore.getState();
    const current = state.runsBySession[sessionId];
    const snapshotSequence = current?.lastEventSequence ?? 0;
    if (event === "run_snapshot") {
      this.syncSnapshot(data as unknown as ChatRunItem, false);
      return;
    }
    if (current && current.runId !== runId) return;
    if (event === "run_reconnected") return;
    if (sequence > 0 && current && sequence <= current.lastEventSequence && event === "message_delta") return;
    const patch = { lastEventSequence: Math.max(sequence, snapshotSequence) };
    state.patchRunView(sessionId, patch);
    switch (event) {
      case "run_status": {
        const status = String(data.status || "running") as DurableRunStatus;
        state.patchRunView(sessionId, {
          status,
          stopping: status === "running" ? current?.stopping ?? false : false,
          unseen: status === "succeeded" && state.currentSessionId !== sessionId,
        });
        break;
      }
      case "trace_start": state.beginRun(sessionId, data as unknown as TraceStart); break;
      case "message_delta": {
        const content = String(data.content || "");
        state.appendAssistantText(sessionId, content);
        state.patchRunView(sessionId, { content: `${current?.content ?? ""}${content}` });
        break;
      }
      case "run_step_start": state.startRunStep(sessionId, data as unknown as RunStepStart); break;
      case "run_step_update": state.updateRunStep(sessionId, data as unknown as RunStepUpdate); break;
      case "run_step_end": state.endRunStep(sessionId, data as unknown as RunStepEnd); break;
      case "message_end": state.setRunTokenUsage(sessionId, data as unknown as MessageEnd); break;
      case "interrupt_requested": state.patchRunView(sessionId, {
        status: "pending_approval", pendingApproval: data as unknown as InterruptRequested,
      }); break;
      case "orchestration.router_decision": state.setOrchestrationRouter(sessionId, data as unknown as OrchestrationRouterDecision); break;
      case "orchestration.clarification": state.setOrchestrationClarification(sessionId, data as unknown as OrchestrationClarification); break;
      case "orchestration.plan": state.setOrchestrationPlan(sessionId, data as unknown as OrchestrationPlan); break;
      case "orchestration.worker_started": state.startOrchestrationWorker(sessionId, data as unknown as OrchestrationWorkerStarted); break;
      case "orchestration.worker_finished": state.finishOrchestrationWorker(sessionId, data as unknown as OrchestrationWorkerFinished); break;
      case "orchestration.synthesis": state.startOrchestrationSynthesis(sessionId, data as unknown as OrchestrationSynthesis); break;
      case "run_end": {
        state.finishRun(sessionId, data as unknown as RunEnd);
        const runtimeStatus = String(data.status || "failed");
        const status: DurableRunStatus = runtimeStatus === "success" ? "succeeded"
          : runtimeStatus === "canceled" ? "canceled" : "failed";
        state.patchRunView(sessionId, { status, stopping: false, unseen: status === "succeeded" && state.currentSessionId !== sessionId });
        break;
      }
      case "error": state.setError(sessionId, String(data.message || "运行失败")); break;
      default: break;
    }
    const updated = useSessionStore.getState().runsBySession[sessionId];
    if (updated) this.broadcast(updated);
  }

  private broadcast(run: ChatRunViewState) {
    this.channel?.postMessage({
      run_id: run.runId, session_id: run.sessionId, status: run.status, last_event_sequence: run.lastEventSequence,
    });
  }
}

export const chatRunManager = new ChatRunManager();
