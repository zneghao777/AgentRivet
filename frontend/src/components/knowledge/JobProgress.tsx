import { Button, Progress } from "antd";
import type { KnowledgeIndexJob } from "@/types/api";

const JOB_LABELS: Record<string, string> = {
  parse: "解析与文本索引",
  vectorize: "文本索引",
  graph_build: "图谱构建",
  graph_cleanup: "图谱清理",
};

export default function JobProgress({ job, onCancel }: { job: KnowledgeIndexJob; onCancel?: (job: KnowledgeIndexJob) => void }) {
  return (
    <div className="kb-job-progress" aria-live="polite">
      <div><b>{JOB_LABELS[job.job_type] ?? job.job_type}</b><span>{job.stage}</span><code>{job.job_id.slice(0, 8)}</code></div>
      <Progress percent={job.progress} size="small" status={job.status === "failed" ? "exception" : job.status === "succeeded" ? "success" : "active"} />
      {job.status === "queued" && onCancel ? <Button type="text" size="small" onClick={() => onCancel(job)}>取消</Button> : null}
    </div>
  );
}
