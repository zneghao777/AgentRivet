import { useMemo, useState, type CSSProperties } from "react";
import type { ChatOrchestration, OrchestrationWorkerState } from "@/types/chat";
import { agentColor } from "@/utils/agentColor";

const STAGES = [
  { key: "routing", label: "路由", icon: "route" },
  { key: "planning", label: "计划", icon: "account_tree" },
  { key: "executing", label: "执行", icon: "groups" },
  { key: "verifying", label: "校验", icon: "verified" },
  { key: "synthesizing", label: "综合", icon: "merge" },
] as const;

const PHASE_INDEX: Record<ChatOrchestration["phase"], number> = {
  routing: 0,
  planning: 1,
  executing: 2,
  verifying: 3,
  synthesizing: 4,
  done: 5,
};

const WORKER_META: Record<OrchestrationWorkerState["status"], { label: string; icon: string }> = {
  pending: { label: "排队", icon: "schedule" },
  running: { label: "运行中", icon: "progress_activity" },
  done: { label: "完成", icon: "check_circle" },
  failed: { label: "失败", icon: "error" },
};

export default function OrchestrationPanel({ orchestration }: { orchestration: ChatOrchestration }) {
  const [reasonOpen, setReasonOpen] = useState(false);
  const [expandedWorkers, setExpandedWorkers] = useState<Set<string>>(() => new Set());
  const workers = useMemo(() => Object.values(orchestration.workers), [orchestration.workers]);
  const failed = workers.filter((worker) => worker.status === "failed").length;
  const phaseIndex = PHASE_INDEX[orchestration.phase];

  const toggleWorker = (taskId: string) => {
    setExpandedWorkers((current) => {
      const next = new Set(current);
      if (next.has(taskId)) next.delete(taskId);
      else next.add(taskId);
      return next;
    });
  };

  return (
    <section className="orch-panel" role="status" aria-live="polite" aria-label="多 Agent 编排执行状态">
      <header className="orch-panel-head">
        <span className="orch-panel-mark material-symbols-rounded" aria-hidden="true">hub</span>
        <div><b>编排执行</b><small>{orchestration.execution === "sequential" ? "顺序模式" : "并行模式"}</small></div>
        <span className="orch-panel-confidence">置信度 {Math.round(orchestration.confidence * 100)}%</span>
      </header>

      <div className="orch-stages" aria-label={`当前阶段：${orchestration.phase}`}>
        {STAGES.map((stage, index) => {
          const state = index < phaseIndex ? "done" : index === phaseIndex ? "running" : "pending";
          const label = stage.key === "executing" && workers.length ? `${stage.label}(${workers.length})` : stage.label;
          return (
            <div className={`orch-stage ${state}`} key={stage.key}>
              <span className="orch-stage-dot"><span className="material-symbols-rounded">{stage.icon}</span></span>
              <span>{label}</span>
            </div>
          );
        })}
      </div>

      {orchestration.summary ? <p className="orch-summary">计划：{orchestration.summary}</p> : null}
      {orchestration.connectionState === "disconnected" ? <div className="orch-state-banner neutral"><span className="material-symbols-rounded">cloud_off</span>连接中断，已保留当前状态</div> : null}
      {orchestration.connectionState === "stopped" ? <div className="orch-state-banner neutral"><span className="material-symbols-rounded">stop_circle</span>编排已停止</div> : null}
      {failed ? <div className="orch-degraded-banner"><span className="material-symbols-rounded">warning</span>{failed}/{workers.length} 子任务失败，已基于成功结果回答</div> : null}

      <div className="orch-worker-grid">
        {workers.length ? workers.map((worker) => {
          const meta = WORKER_META[worker.status];
          const expanded = expandedWorkers.has(worker.taskId);
          const canExpand = Boolean(worker.resultPreview || worker.error);
          return (
            <article
              className={`orch-worker-card ${worker.status}`}
              key={worker.taskId}
              style={{ "--agent-color": agentColor(worker.agentName) } as CSSProperties}
            >
              <header>
                <span className="orch-worker-avatar material-symbols-rounded" aria-hidden="true">smart_toy</span>
                <b>{worker.agentName}</b>
              </header>
              <p className="orch-worker-goal" title={worker.goal}>{worker.goal}</p>
              <div className="orch-worker-status">
                <span className="material-symbols-rounded" aria-hidden="true">{meta.icon}</span>
                <span>{meta.label}</span>
                {worker.durationMs != null ? <time>{worker.durationMs}ms</time> : null}
              </div>
              {canExpand ? (
                <button type="button" className="orch-worker-toggle" aria-expanded={expanded} onClick={() => toggleWorker(worker.taskId)}>
                  {expanded ? "收起摘要" : "查看摘要"}
                  <span className="material-symbols-rounded">{expanded ? "expand_less" : "expand_more"}</span>
                </button>
              ) : null}
              {expanded ? <p className="orch-worker-preview">{worker.resultPreview || worker.error}</p> : null}
            </article>
          );
        }) : Array.from({ length: 3 }, (_, index) => <div className="orch-worker-card skeleton" key={index} aria-hidden="true" />)}
      </div>

      <button type="button" className="orch-reason-toggle" aria-expanded={reasonOpen} onClick={() => setReasonOpen((value) => !value)}>
        <span className="material-symbols-rounded">{reasonOpen ? "expand_less" : "chevron_right"}</span>
        {reasonOpen ? "收起路由详情" : "查看路由理由"}
      </button>
      {reasonOpen ? (
        <div className="orch-reason">
          <p>{orchestration.reason}</p>
          {workers.length ? <ul>{workers.map((worker) => <li key={worker.taskId}><b>{worker.agentName}</b>：{worker.goal}</li>)}</ul> : null}
        </div>
      ) : null}
    </section>
  );
}
