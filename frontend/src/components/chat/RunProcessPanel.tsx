import { memo, useEffect, useMemo, useState } from "react";
import type { ChatRun, ChatRunStep } from "@/types/chat";

const STATUS_COPY: Record<ChatRun["status"], { label: string; tone: string }> = {
  running: { label: "正在运行", tone: "running" },
  success: { label: "运行完成", tone: "success" },
  error: { label: "运行失败", tone: "error" },
  timeout: { label: "运行超时", tone: "error" },
  canceled: { label: "已停止", tone: "muted" },
  interrupted: { label: "等待继续", tone: "warning" },
};

const NAME_COPY: Record<string, string> = {
  "llm.chat": "调用模型",
  "node.agent": "分析请求并组织回答",
  "node.tools": "调度工具",
  "node.approval": "等待人工确认",
  knowledge_search: "统一知识检索（文本 / 图谱自动路由）",
  knowledge_graph_search: "知识图谱专用检索",
  builtin_read_skill_file: "读取 Skill 资源",
  builtin_memory_search: "检索长期记忆",
  builtin_calculate: "执行计算",
  builtin_get_time: "获取当前时间",
  builtin_echo: "调用回显工具",
};

const HIDDEN_NODE_NAMES = new Set([
  "node.LangGraph",
  "node.__start__",
  "node.__end__",
  "node.ChannelWrite",
  "node.route",
  "node.should_continue",
]);

function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.max(0, Math.round(ms))}ms`;
  const seconds = ms / 1000;
  return `${seconds.toFixed(seconds >= 10 ? 1 : 2)}s`;
}

function stepLabel(step: ChatRunStep): string {
  const direct = NAME_COPY[step.name];
  if (direct) return direct;
  const normalized = step.name.replace(/^node\./, "").replace(/^tool\./, "");
  const normalizedCopy = NAME_COPY[normalized];
  if (normalizedCopy) return normalizedCopy;
  return normalized.replace(/[_-]+/g, " ");
}

function stepIcon(step: ChatRunStep): string {
  if (step.kind === "tool") {
    if (step.name.includes("knowledge_graph")) return "account_tree";
    if (step.name.includes("knowledge")) return "find_in_page";
    if (step.name.includes("skill")) return "handyman";
    if (step.name.includes("memory")) return "memory";
    return "build";
  }
  return step.kind === "llm" ? "neurology" : "account_tree";
}

function compactText(value: string, limit = 360): string {
  const compact = value.replace(/\s+/g, " ").trim();
  return compact.length > limit ? `${compact.slice(0, limit)}…` : compact;
}

function extractQuery(summary: string | undefined): string | undefined {
  if (!summary) return undefined;
  const jsonQuery = summary.match(/["']query["']\s*:\s*["']([^"']+)["']/)?.[1];
  if (jsonQuery) return jsonQuery;
  return summary.match(/knowledge_(?:graph_)?search\([^\n]*query[^\n]*\)/)?.[0];
}

function StepDetail({ step }: { step: ChatRunStep }) {
  if (step.error) return <div className="run-step-detail error">{compactText(step.error)}</div>;
  if (step.name.includes("knowledge_search") || step.name.includes("knowledge_graph_search")) {
    const query = extractQuery(step.summary);
    const references = step.references ?? [];
    const sources = Array.from(new Set(references.map((reference) => reference.sourceFile)));
    return (
      <div className="run-step-detail knowledge">
        <div className="run-step-detail-row">
          <span>检索问题</span>
          <strong>{query || "使用当前问题进行知识库检索"}</strong>
        </div>
        <div className="run-step-detail-row">
          <span>检索结果</span>
          <strong>{references.length ? `命中 ${references.length} 个相关片段` : "检索已完成"}</strong>
        </div>
        {sources.length ? (
          <div className="run-step-source-chips">
            {sources.slice(0, 3).map((source) => <span key={source}>{source}</span>)}
            {sources.length > 3 ? <span>+{sources.length - 3}</span> : null}
          </div>
        ) : null}
      </div>
    );
  }
  if (step.kind === "tool" && step.summary) {
    return <div className="run-step-detail">{compactText(step.summary)}</div>;
  }
  if (step.kind === "llm") {
    const ttft = Number(step.detail?.ttft_ms);
    return (
      <div className="run-step-detail model">
        <div className="run-model-output-head">
          <span><span className="material-symbols-rounded">notes</span>模型实际返回</span>
          {Number.isFinite(ttft) ? <small>首个片段 {formatDuration(ttft)}</small> : null}
        </div>
        <div className={`run-model-output${step.output ? "" : " pending"}`}>
          {step.output ? compactText(step.output, 512) : "正在接收模型输出…"}
        </div>
      </div>
    );
  }
  return <div className="run-step-detail">步骤已完成</div>;
}

function StepState({ step }: { step: ChatRunStep }) {
  if (step.status === "running") return <span className="run-step-spinner" aria-label="执行中" />;
  if (step.status === "ok") return <span className="material-symbols-rounded run-step-check" aria-label="完成">check</span>;
  if (step.status === "interrupted") return <span className="material-symbols-rounded run-step-warn" aria-label="已中断">pause</span>;
  return <span className="material-symbols-rounded run-step-error" aria-label="失败">close</span>;
}

function LoaderGrid() {
  return (
    <span className="run-loader-grid" aria-hidden="true">
      {Array.from({ length: 9 }, (_, index) => <i key={index} style={{ animationDelay: `${(index % 3 + Math.abs(Math.floor(index / 3) - 1)) * 90}ms` }} />)}
    </span>
  );
}

function RunProcessPanel({ run }: { run: ChatRun }) {
  const working = run.status === "running";
  const [manualExpanded, setManualExpanded] = useState<boolean | null>(null);
  const [openSteps, setOpenSteps] = useState<Set<string>>(new Set());
  const [now, setNow] = useState(Date.now());
  const expanded = manualExpanded ?? working;

  useEffect(() => {
    if (!working) return undefined;
    const timer = window.setInterval(() => setNow(Date.now()), 100);
    return () => window.clearInterval(timer);
  }, [working]);

  const steps = useMemo(
    () => run.steps.filter((step) => !HIDDEN_NODE_NAMES.has(step.name)),
    [run.steps],
  );
  const elapsed = run.durationMs ?? Math.max(0, now - run.startedAt);
  const totalTokens = run.tokenUsage?.total ?? run.tokenUsage?.prompt ?? 0;
  const status = STATUS_COPY[run.status];
  const completed = steps.filter((step) => step.status === "ok").length;

  const toggleStep = (stepId: string) => {
    setOpenSteps((current) => {
      const next = new Set(current);
      if (next.has(stepId)) next.delete(stepId);
      else next.add(stepId);
      return next;
    });
  };

  return (
    <section className={`run-process ${status.tone}`} aria-label="Agent 运行过程">
      <button
        type="button"
        className="run-process-trigger"
        aria-expanded={expanded}
        onClick={() => setManualExpanded((current) => !(current ?? working))}
      >
        <span className="run-process-leading">
          {working ? <LoaderGrid /> : <span className="material-symbols-rounded">{run.status === "success" ? "check_circle" : run.status === "interrupted" ? "pause_circle" : "error"}</span>}
        </span>
        <span className="run-process-title" role="status">
          <strong>{status.label}</strong>
          <small>
            {formatDuration(elapsed)}
            {steps.length ? ` · ${completed}/${steps.length} 个步骤` : ""}
            {run.toolCalls ? ` · ${run.toolCalls} 次工具调用` : ""}
            {totalTokens ? ` · ${totalTokens.toLocaleString()} tokens` : ""}
          </small>
        </span>
        <span className={`material-symbols-rounded run-process-chevron${expanded ? " expanded" : ""}`}>expand_more</span>
      </button>

      <div className="run-process-collapse" style={{ gridTemplateRows: expanded ? "1fr" : "0fr", opacity: expanded ? 1 : 0 }}>
        <div className="run-process-overflow">
          <div className="run-step-list">
            {!steps.length && working ? (
              <div className="run-step-placeholder"><span className="run-step-spinner" />准备运行环境…</div>
            ) : null}
            {steps.map((step) => {
              const detailOpen = openSteps.has(step.id);
              const hasDetail = Boolean(
                step.error || (step.kind === "tool" && step.summary) || step.kind === "llm",
              );
              return (
                <div className={`run-step ${step.status}`} key={step.id}>
                  <div className="run-step-rail"><StepState step={step} /></div>
                  <button
                    type="button"
                    className="run-step-main"
                    disabled={!hasDetail}
                    aria-expanded={hasDetail ? detailOpen : undefined}
                    onClick={() => hasDetail && toggleStep(step.id)}
                  >
                    <span className="material-symbols-rounded run-step-icon">{stepIcon(step)}</span>
                    <span className="run-step-copy">
                      <strong>{stepLabel(step)}</strong>
                      {step.kind === "llm" && step.model ? <small>{step.model}</small> : null}
                    </span>
                    {step.durationMs != null ? <span className="run-step-duration">{formatDuration(step.durationMs)}</span> : null}
                    {hasDetail ? <span className={`material-symbols-rounded run-step-chevron${detailOpen ? " expanded" : ""}`}>chevron_right</span> : null}
                  </button>
                  {hasDetail ? (
                    <div className="run-step-collapse" style={{ gridTemplateRows: detailOpen ? "1fr" : "0fr", opacity: detailOpen ? 1 : 0 }}>
                      <div>
                        <StepDetail step={step} />
                      </div>
                    </div>
                  ) : null}
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </section>
  );
}

export default memo(RunProcessPanel);
