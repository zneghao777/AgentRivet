import { useCallback, useDeferredValue, useEffect, useMemo, useState } from "react";
import { Button, Empty, Popconfirm, Select, Skeleton, Switch, message } from "antd";
import { useNavigate, useSearchParams } from "react-router-dom";
import { deleteData, getData, patchData } from "@/api/http";
import type { IndexMode, IndexStatus, KnowledgeBaseItem } from "@/types/api";
import { formatTime } from "@/utils/format";
import { WorkspaceHeader } from "@/components/common/Workspace";
import KnowledgeCreateModal from "@/components/knowledge/KnowledgeCreateModal";
import KnowledgeUploadModal from "@/components/knowledge/KnowledgeUploadModal";
import KnowledgeModeBadge from "@/components/knowledge/KnowledgeModeBadge";
import IndexHealthBadge from "@/components/knowledge/IndexHealthBadge";

type ModeFilter = "all" | IndexMode;
type HealthFilter = "all" | "issues" | IndexStatus;

const ISSUE_STATUSES: IndexStatus[] = ["partial", "failed", "stale"];
const MODE_FILTERS: { value: ModeFilter; label: string; icon: string }[] = [
  { value: "all", label: "全部", icon: "apps" },
  { value: "standard", label: "标准检索", icon: "manage_search" },
  { value: "graph_enhanced", label: "图谱增强", icon: "account_tree" },
];

function overallHealth(kb: KnowledgeBaseItem): IndexStatus {
  if (kb.index_status !== "ready") return kb.index_status;
  if (kb.index_mode !== "graph_enhanced") return kb.index_status;
  if (kb.graph_status === "building" || kb.graph_status === "pending") return "indexing";
  if (kb.graph_status === "partial" || kb.graph_status === "failed" || kb.graph_status === "stale") return kb.graph_status;
  return kb.index_status;
}

function KnowledgeCard({ kb, busy, onOpen, onUpload, onToggle, onDelete }: { kb: KnowledgeBaseItem; busy: boolean; onOpen: () => void; onUpload: () => void; onToggle: (enabled: boolean) => void; onDelete: () => void }) {
  const health = overallHealth(kb);
  const issue = ISSUE_STATUSES.includes(health);
  return (
    <article className={`knowledge-library-card mode-${kb.index_mode}${issue ? " has-issue" : ""}`}>
      <div className="knowledge-card-accent" />
      <header className="knowledge-card-header">
        <span className="knowledge-card-icon material-symbols-rounded">{kb.index_mode === "graph_enhanced" ? "account_tree" : "database"}</span>
        <button type="button" className="knowledge-card-title" onClick={onOpen}><strong>{kb.name}</strong><span>{kb.description || "尚未添加知识库描述"}</span></button>
        <Switch size="small" checked={kb.status === "active"} loading={busy} onChange={onToggle} aria-label={`${kb.status === "active" ? "停用" : "启用"}${kb.name}`} />
      </header>
      <div className="knowledge-card-badges">
        <KnowledgeModeBadge mode={kb.index_mode} />
        <IndexHealthBadge status={health} />
        {kb.active_job_count > 0 ? <span className="knowledge-job-pill"><i />{kb.active_job_count} 个任务进行中</span> : null}
      </div>
      {issue ? <button type="button" className="knowledge-card-alert" onClick={onOpen}><span className="material-symbols-rounded">error</span><span><b>索引需要处理</b><small>查看状态、失败原因与恢复操作</small></span><span className="material-symbols-rounded">arrow_forward</span></button> : null}
      <div className="knowledge-card-metrics" aria-label={`${kb.name} 索引统计`}>
        <div><span className="material-symbols-rounded">description</span><b>{kb.doc_count}</b><small>文档</small></div>
        <div><span className="material-symbols-rounded">view_agenda</span><b>{kb.chunk_count}</b><small>可检索分块</small></div>
        <div><span className="material-symbols-rounded">hub</span><b>{kb.index_mode === "graph_enhanced" ? kb.entity_count : "—"}</b><small>{kb.index_mode === "graph_enhanced" ? "图谱实体" : "未启用图谱"}</small></div>
      </div>
      <footer className="knowledge-card-footer">
        <span><span className="material-symbols-rounded">schedule</span>{formatTime(kb.updated_at ?? kb.created_at)} 更新</span>
        <div>
          <Button type="text" icon={<span className="material-symbols-rounded">upload</span>} onClick={onUpload}>上传</Button>
          <Popconfirm title="删除知识库" description="将删除原文件、分块、向量与图谱数据。" okText="删除" cancelText="取消" okButtonProps={{ danger: true }} onConfirm={onDelete}><Button type="text" danger aria-label={`删除${kb.name}`} icon={<span className="material-symbols-rounded">delete</span>} /></Popconfirm>
          <Button type="primary" onClick={onOpen}>进入工作台</Button>
        </div>
      </footer>
    </article>
  );
}

export default function KnowledgeCenterPage() {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const [kbs, setKbs] = useState<KnowledgeBaseItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [uploadTarget, setUploadTarget] = useState<KnowledgeBaseItem | null>(null);
  const [togglingKb, setTogglingKb] = useState<string | null>(null);
  const [keyword, setKeyword] = useState(params.get("q") ?? "");
  const [modeFilter, setModeFilter] = useState<ModeFilter>((params.get("mode") as ModeFilter) ?? "all");
  const [healthFilter, setHealthFilter] = useState<HealthFilter>((params.get("health") as HealthFilter) ?? "all");
  const [statusFilter, setStatusFilter] = useState(params.get("status") ?? "all");
  const [sort, setSort] = useState(params.get("sort") ?? "updated");
  const deferredKeyword = useDeferredValue(keyword.trim().toLowerCase());

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try { setKbs(await getData<KnowledgeBaseItem[]>("/knowledge-bases")); }
    catch (error) { setLoadError(error instanceof Error ? error.message : "加载知识库失败"); }
    finally { setLoading(false); }
  }, []);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    const next = new URLSearchParams();
    if (keyword) next.set("q", keyword);
    if (modeFilter !== "all") next.set("mode", modeFilter);
    if (healthFilter !== "all") next.set("health", healthFilter);
    if (statusFilter !== "all") next.set("status", statusFilter);
    if (sort !== "updated") next.set("sort", sort);
    setParams(next, { replace: true });
  }, [healthFilter, keyword, modeFilter, setParams, sort, statusFilter]);

  const toggleKb = async (kb: KnowledgeBaseItem, enabled: boolean) => {
    setTogglingKb(kb.kb_id);
    try {
      const updated = await patchData<KnowledgeBaseItem>(`/knowledge-bases/${kb.kb_id}/status`, { enabled });
      setKbs((current) => current.map((entry) => entry.kb_id === updated.kb_id ? updated : entry));
      message.success(enabled ? "知识库已启用" : "知识库已停用");
    } catch (error) { message.error(error instanceof Error ? error.message : "更新状态失败"); }
    finally { setTogglingKb(null); }
  };

  const removeKb = async (kb: KnowledgeBaseItem) => {
    try { await deleteData(`/knowledge-bases/${kb.kb_id}`); message.success("知识库已删除"); await load(); }
    catch (error) { message.error(error instanceof Error ? error.message : "删除失败"); }
  };

  const visibleKbs = useMemo(() => {
    const items = kbs.filter((kb) => {
      const matchesQuery = !deferredKeyword || `${kb.name} ${kb.description ?? ""} ${kb.kb_id}`.toLowerCase().includes(deferredKeyword);
      const health = overallHealth(kb);
      const matchesHealth = healthFilter === "all" || (healthFilter === "issues" ? ISSUE_STATUSES.includes(health) : health === healthFilter);
      return matchesQuery && (modeFilter === "all" || kb.index_mode === modeFilter) && matchesHealth && (statusFilter === "all" || kb.status === statusFilter);
    });
    return [...items].sort((a, b) => {
      if (sort === "name") return a.name.localeCompare(b.name, "zh-CN");
      if (sort === "documents") return b.doc_count - a.doc_count;
      if (sort === "issues") return Number(ISSUE_STATUSES.includes(overallHealth(b))) - Number(ISSUE_STATUSES.includes(overallHealth(a)));
      return String(b.updated_at ?? b.created_at).localeCompare(String(a.updated_at ?? a.created_at));
    });
  }, [deferredKeyword, healthFilter, kbs, modeFilter, sort, statusFilter]);

  const graphKbs = kbs.filter((kb) => kb.index_mode === "graph_enhanced");
  const issueCount = kbs.filter((kb) => ISSUE_STATUSES.includes(overallHealth(kb))).length;
  const totalDocs = kbs.reduce((sum, kb) => sum + kb.doc_count, 0);
  const totalChunks = kbs.reduce((sum, kb) => sum + kb.chunk_count, 0);
  const clearFilters = () => { setKeyword(""); setModeFilter("all"); setHealthFilter("all"); setStatusFilter("all"); };

  return (
    <div className="page-scroll workspace-page knowledge-page knowledge-card-page">
      <WorkspaceHeader icon="database" title="知识库" description="管理标准检索与知识图谱，让 Agent 基于可追溯的企业知识回答。" actions={<><Button type="primary" icon={<span className="material-symbols-rounded">add</span>} onClick={() => setCreateOpen(true)}>新建知识库</Button><Button icon={<span className="material-symbols-rounded">refresh</span>} onClick={() => void load()} loading={loading}>刷新</Button></>} />
      <section className="knowledge-summary-band" aria-label="知识库关键指标">
        <div className="tone-blue"><span className="material-symbols-rounded">database</span><p>知识库<strong>{kbs.length}</strong><small>{kbs.filter((kb) => kb.status === "active").length} 个已启用</small></p></div>
        <div className="tone-violet"><span className="material-symbols-rounded">account_tree</span><p>图谱增强<strong>{graphKbs.length}</strong><small>{graphKbs.filter((kb) => kb.graph_status === "ready").length} 个图谱就绪</small></p></div>
        <div className="tone-teal"><span className="material-symbols-rounded">description</span><p>知识内容<strong>{totalDocs}</strong><small>{totalChunks} 个可检索分块</small></p></div>
        <button type="button" className="tone-amber" onClick={() => setHealthFilter("issues")}><span className="material-symbols-rounded">error</span><p>需要处理<strong>{issueCount}</strong><small>失败、部分可用或过期</small></p></button>
      </section>
      <section className="knowledge-discovery-bar">
        <label className="kb-search-field"><span className="sr-only">搜索知识库</span><input value={keyword} onChange={(event) => setKeyword(event.target.value)} placeholder="搜索知识库名称、描述或 ID" /><span className="material-symbols-rounded">search</span></label>
        <div className="knowledge-mode-filters" role="group" aria-label="知识库类型">{MODE_FILTERS.map((filter) => <button key={filter.value} type="button" aria-pressed={modeFilter === filter.value} onClick={() => setModeFilter(filter.value)}><span className="material-symbols-rounded">{filter.icon}</span>{filter.label}</button>)}</div>
        <Select aria-label="索引健康度" value={healthFilter} onChange={setHealthFilter} options={[{ value: "all", label: "全部健康度" }, { value: "issues", label: "需要处理" }, { value: "ready", label: "已就绪" }, { value: "indexing", label: "处理中" }, { value: "partial", label: "部分可用" }, { value: "failed", label: "失败" }, { value: "stale", label: "已过期" }]} />
        <Select aria-label="启用状态" value={statusFilter} onChange={setStatusFilter} options={[{ value: "all", label: "全部状态" }, { value: "active", label: "已启用" }, { value: "inactive", label: "已停用" }]} />
        <Select aria-label="排序方式" value={sort} onChange={setSort} options={[{ value: "updated", label: "最近更新" }, { value: "name", label: "名称排序" }, { value: "documents", label: "文档最多" }, { value: "issues", label: "问题优先" }]} />
      </section>
      {loading && kbs.length === 0 ? <div className="knowledge-card-grid">{Array.from({ length: 4 }, (_, index) => <div className="knowledge-library-card" key={index}><Skeleton active paragraph={{ rows: 4 }} /></div>)}</div> : null}
      {loadError ? <div className="kb-inline-error" role="alert"><span>{loadError}</span><Button onClick={() => void load()}>重新加载</Button></div> : null}
      {!loading && !loadError && visibleKbs.length === 0 ? <div className="knowledge-empty-surface"><Empty description={kbs.length ? "没有符合筛选条件的知识库" : "还没有知识库"}>{kbs.length ? <Button onClick={clearFilters}>清除筛选</Button> : <Button type="primary" onClick={() => setCreateOpen(true)}>新建知识库</Button>}</Empty></div> : null}
      {visibleKbs.length > 0 ? <section className="knowledge-card-grid" aria-label="知识库列表">{visibleKbs.map((kb) => <KnowledgeCard key={kb.kb_id} kb={kb} busy={togglingKb === kb.kb_id} onOpen={() => navigate(`/knowledge/${kb.kb_id}?tab=overview`)} onUpload={() => setUploadTarget(kb)} onToggle={(enabled) => void toggleKb(kb, enabled)} onDelete={() => void removeKb(kb)} />)}</section> : null}
      {visibleKbs.length > 0 ? <footer className="knowledge-grid-footer">显示 {visibleKbs.length} / {kbs.length} 个知识库</footer> : null}
      <KnowledgeCreateModal open={createOpen} onClose={() => setCreateOpen(false)} onCreated={() => void load()} />
      <KnowledgeUploadModal kb={uploadTarget} open={uploadTarget !== null} onClose={() => setUploadTarget(null)} onUploaded={() => void load()} />
    </div>
  );
}
