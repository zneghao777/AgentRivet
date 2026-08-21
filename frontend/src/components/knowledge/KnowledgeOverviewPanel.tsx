import { Button, Progress } from "antd";
import type { KnowledgeBaseItem, KnowledgeDocItem, KnowledgeIndexJob } from "@/types/api";
import { formatTime } from "@/utils/format";
import IndexHealthBadge from "./IndexHealthBadge";
import JobProgress from "./JobProgress";

function ratio(ready: number, total: number) {
  return total === 0 ? 0 : Math.round((ready / total) * 100);
}

function StageRow({ icon, title, caption, ready, total, tone }: { icon: string; title: string; caption: string; ready: number; total: number; tone: "blue" | "teal" | "violet" }) {
  const percent = ratio(ready, total);
  return (
    <div className={`kb-health-stage tone-${tone}`}>
      <span className="material-symbols-rounded">{icon}</span>
      <div><b>{title}</b><small>{caption}</small><Progress percent={percent} showInfo={false} strokeColor="currentColor" /></div>
      <strong>{ready}<small> / {total}</small></strong>
    </div>
  );
}

export default function KnowledgeOverviewPanel({ kb, documents, jobs, onOpenDocuments, onOpenGraph }: { kb: KnowledgeBaseItem; documents: KnowledgeDocItem[]; jobs: KnowledgeIndexJob[]; onOpenDocuments: () => void; onOpenGraph: () => void }) {
  const parsed = documents.filter((doc) => doc.parse_status === "parsed").length;
  const vectorReady = documents.filter((doc) => doc.vector_status === "ready").length;
  const graphDocs = documents.filter((doc) => doc.graph_status !== "excluded");
  const graphReady = graphDocs.filter((doc) => doc.graph_status === "ready").length;
  const graphStale = graphDocs.filter((doc) => doc.graph_status === "stale").length;
  const failedJobs = jobs.filter((job) => job.status === "failed");
  const activeJobs = jobs.filter((job) => job.status === "queued" || job.status === "running");
  const graphNeedsAttention = kb.index_mode === "graph_enhanced" && ["stale", "failed", "partial"].includes(kb.graph_status);

  const metrics = [
    { label: "知识文档", value: documents.length, meta: `${parsed} 个解析完成`, icon: "description", tone: "blue" },
    { label: "可检索分块", value: kb.chunk_count, meta: `${vectorReady}/${documents.length} 文档可检索`, icon: "view_agenda", tone: "teal" },
    { label: "图谱实体", value: kb.index_mode === "graph_enhanced" ? kb.entity_count : "—", meta: kb.index_mode === "graph_enhanced" ? `${Number(kb.graph_meta.relations ?? 0)} 条关系` : "标准检索模式", icon: "hub", tone: "violet" },
    { label: "活动任务", value: activeJobs.length, meta: failedJobs.length ? `${failedJobs.length} 个最近失败` : "队列运行正常", icon: "schedule", tone: activeJobs.length ? "amber" : "slate" },
  ];

  return (
    <div className="kb-overview-dashboard">
      <section className={`kb-overview-hero${graphNeedsAttention ? " warning" : ""}`}>
        <div className="kb-overview-hero-icon"><span className="material-symbols-rounded">{graphNeedsAttention ? "warning" : "verified"}</span></div>
        <div><span>索引健康摘要</span><h2>{graphNeedsAttention ? "文本检索正常，图谱数据需要更新" : "知识内容已准备好供 Agent 使用"}</h2><p>{graphNeedsAttention ? `${graphStale} 个文档的图谱版本已过期，标准问答不受影响。` : "解析、向量和图谱阶段均按独立状态持续监控。"}</p></div>
        <div className="kb-overview-hero-actions"><IndexHealthBadge status={kb.index_status} prefix="文本" />{kb.index_mode === "graph_enhanced" ? <IndexHealthBadge status={kb.graph_status} prefix="图谱" /> : null}{graphNeedsAttention ? <Button type="primary" onClick={onOpenGraph}>处理图谱</Button> : <Button onClick={onOpenDocuments}>管理文档</Button>}</div>
      </section>

      <section className="kb-overview-stat-grid" aria-label="知识库统计">
        {metrics.map((metric) => <article className={`tone-${metric.tone}`} key={metric.label}><span className="material-symbols-rounded">{metric.icon}</span><div><small>{metric.label}</small><strong>{metric.value}</strong><p>{metric.meta}</p></div></article>)}
      </section>

      <div className="kb-overview-main-grid">
        <section className="kb-overview-surface kb-pipeline-card">
          <header><div><span className="eyebrow">PIPELINE HEALTH</span><h3>索引流水线</h3><p>每个阶段独立运行，图谱失败不会阻断文本检索。</p></div><Button type="text" onClick={onOpenDocuments}>查看文档</Button></header>
          <StageRow icon="article" title="文档解析" caption="提取正文、标题与结构信息" ready={parsed} total={documents.length} tone="blue" />
          <StageRow icon="database" title="文本索引" caption="分块与向量已写入检索库" ready={vectorReady} total={documents.length} tone="teal" />
          {kb.index_mode === "graph_enhanced" ? <StageRow icon="account_tree" title="知识图谱" caption={`${graphStale} 个已过期 · ${graphDocs.filter((doc) => doc.graph_status === "pending").length} 个待构建`} ready={graphReady} total={graphDocs.length} tone="violet" /> : null}
        </section>

        <section className="kb-overview-surface kb-source-card">
          <header><div><span className="eyebrow">RECENT SOURCES</span><h3>最近知识来源</h3></div><span>{documents.length} 个文档</span></header>
          {documents.length ? <div className="kb-source-list">{documents.slice(0, 4).map((doc) => <button type="button" key={doc.doc_id} onClick={onOpenDocuments}><span className="material-symbols-rounded">description</span><span><b>{doc.source_file}</b><small>{doc.chunk_count} 个分块 · {formatTime(doc.updated_at ?? doc.created_at)}</small></span><IndexHealthBadge status={doc.vector_status} /></button>)}</div> : <div className="kb-overview-empty"><span className="material-symbols-rounded">note_add</span><p>上传第一份文档，开始构建知识索引。</p><Button type="primary" onClick={onOpenDocuments}>前往文档管理</Button></div>}
        </section>
      </div>

      <section className="kb-overview-surface kb-activity-card">
        <header><div><span className="eyebrow">INDEX ACTIVITY</span><h3>任务活动</h3></div><span>{activeJobs.length} 进行中 · {failedJobs.length} 失败</span></header>
        {activeJobs.length || failedJobs.length ? <div>{[...activeJobs, ...failedJobs.slice(0, 3)].map((job) => <JobProgress key={job.job_id} job={job} />)}</div> : <div className="kb-activity-quiet"><span className="material-symbols-rounded">check_circle</span><div><b>当前没有活动或失败任务</b><small>索引队列处于稳定状态。</small></div></div>}
      </section>
    </div>
  );
}
