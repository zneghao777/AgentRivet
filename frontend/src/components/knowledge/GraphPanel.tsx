// 知识图谱面板（GRAPH_RAG_SPEC §6）：图谱构建 / 状态 / 实体列表 / 语义图检索
import { lazy, Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { Button, Dropdown, Input, Pagination, Segmented, Select, Tag, message } from "antd";
import { getData, postData } from "@/api/http";
import type {
  GraphEntityItem,
  GraphEventListData,
  GraphEventItem,
  GraphRelation,
  GraphRelationListData,
  GraphSearchResult,
  GraphStatus,
  GraphSubgraph,
} from "@/types/api";
import type { ForceGraphLink, ForceGraphNode } from "@/components/graph/CosmographCanvas";

const CosmographCanvas = lazy(() => import("@/components/graph/CosmographCanvas"));

const EXPLORER_PAGE_SIZE = 10;
const SEARCH_RESULT_PAGE_SIZE = 5;
type GraphResultView = "实体" | "关系" | "事件" | "社区";

const GRAPH_STRATEGY_OPTIONS = [
  { value: "auto", label: "自动" },
  { value: "entity", label: "实体" },
  { value: "hop", label: "多跳扩散" },
  { value: "global", label: "全局社区" },
  { value: "timeline", label: "时间线" },
];

const ENTITY_COLORS = ["#5f8cff", "#8b7cf6", "#22c8b5", "#e9a23b", "#ef6f91", "#58b8ff"];

function colorForType(type: string) {
  let hash = 0;
  for (let index = 0; index < type.length; index += 1) hash = (hash * 31 + type.charCodeAt(index)) | 0;
  return ENTITY_COLORS[Math.abs(hash) % ENTITY_COLORS.length];
}

function ExplorerPager({ current, total, label, onChange, pageSize = EXPLORER_PAGE_SIZE }: { current: number; total: number; label: string; onChange: (page: number) => void; pageSize?: number }) {
  if (total <= pageSize) return null;
  return (
    <footer className="kb-graph-pagination">
      <span>共 {total} 条{label}</span>
      <Pagination
        size="small"
        current={current}
        pageSize={pageSize}
        total={total}
        showSizeChanger={false}
        onChange={onChange}
      />
    </footer>
  );
}

function GraphStatusTag({ status }: { status?: string }) {
  if (status === "ready") return <Tag color="success">图谱已就绪</Tag>;
  if (status === "building") return <Tag color="processing">构建中</Tag>;
  if (status === "pending") return <Tag color="warning">待构建</Tag>;
  if (status === "partial") return <Tag color="warning">部分可用</Tag>;
  if (status === "stale") return <Tag color="warning">已过期</Tag>;
  if (status === "failed") return <Tag color="error">构建失败</Tag>;
  if (status === "disabled") return <Tag>图谱服务不可用</Tag>;
  return <Tag>未构建</Tag>;
}

export default function GraphPanel({ kbId }: { kbId: string }) {
  const [status, setStatus] = useState<GraphStatus | null>(null);
  const [building, setBuilding] = useState(false);
  const [entities, setEntities] = useState<GraphEntityItem[]>([]);
  const [entityTotal, setEntityTotal] = useState(0);
  const [entityPage, setEntityPage] = useState(1);
  const [relations, setRelations] = useState<GraphRelation[]>([]);
  const [relationTotal, setRelationTotal] = useState(0);
  const [relationPage, setRelationPage] = useState(1);
  const [relationsLoaded, setRelationsLoaded] = useState(false);
  const [events, setEvents] = useState<GraphEventItem[]>([]);
  const [eventTotal, setEventTotal] = useState(0);
  const [eventPage, setEventPage] = useState(1);
  const [query, setQuery] = useState("");
  const [strategy, setStrategy] = useState("auto");
  const [hop, setHop] = useState(2);
  const [searching, setSearching] = useState(false);
  const [result, setResult] = useState<GraphSearchResult | null>(null);
  const [resultView, setResultView] = useState<GraphResultView>("实体");
  const [resultPage, setResultPage] = useState(1);
  const [view, setView] = useState<string>("拓扑");
  const [explorerSearchInput, setExplorerSearchInput] = useState("");
  const [explorerQuery, setExplorerQuery] = useState("");
  const [subgraph, setSubgraph] = useState<GraphSubgraph | null>(null);
  const [selectedEntity, setSelectedEntity] = useState<GraphEntityItem | null>(null);

  const loadStatus = useCallback(async () => {
    if (!kbId) return;
    try {
      const data = await getData<GraphStatus>(`/knowledge-bases/${kbId}/graph/status`);
      setStatus(data);
    } catch {
      /* 知识库可能无图，忽略 */
    }
  }, [kbId]);

  const loadEntities = useCallback(async () => {
    if (!kbId) return;
    try {
      const data = await getData<{ items: GraphEntityItem[]; total: number }>(
        `/knowledge-bases/${kbId}/graph/entities?page=${entityPage}&page_size=${EXPLORER_PAGE_SIZE}${explorerQuery ? `&q=${encodeURIComponent(explorerQuery)}` : ""}`
      );
      setEntities(data.items);
      setEntityTotal(data.total);
    } catch {
      /* ignore */
    }
  }, [entityPage, explorerQuery, kbId]);

  const loadRelations = useCallback(async () => {
    if (!kbId) return;
    try {
      const data = await getData<GraphRelationListData>(
        `/knowledge-bases/${kbId}/graph/relations?page=${relationPage}&page_size=${EXPLORER_PAGE_SIZE}${explorerQuery ? `&q=${encodeURIComponent(explorerQuery)}` : ""}`
      );
      setRelations(data.items);
      setRelationTotal(data.total);
    } catch {
      /* ignore */
    } finally {
      setRelationsLoaded(true);
    }
  }, [explorerQuery, kbId, relationPage]);

  const loadEvents = useCallback(async () => {
    if (!kbId) return;
    try {
      const data = await getData<GraphEventListData>(
        `/knowledge-bases/${kbId}/graph/events?page=${eventPage}&page_size=${EXPLORER_PAGE_SIZE}${explorerQuery ? `&q=${encodeURIComponent(explorerQuery)}` : ""}`
      );
      setEvents(data.items);
      setEventTotal(data.total);
    } catch {
      /* ignore */
    }
  }, [eventPage, explorerQuery, kbId]);

  useEffect(() => {
    void loadStatus();
  }, [loadStatus]);

  useEffect(() => { void loadEntities(); }, [loadEntities]);
  useEffect(() => { if (view === "关系" || view === "拓扑") void loadRelations(); }, [loadRelations, view]);
  useEffect(() => { if (view === "事件") void loadEvents(); }, [loadEvents, view]);

  useEffect(() => {
    if (status?.graph_status !== "building" && status?.graph_status !== "pending") return undefined;
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") {
        void loadStatus();
        if (view === "实体") void loadEntities();
        if (view === "关系") void loadRelations();
        if (view === "事件") void loadEvents();
      }
    }, 3000);
    return () => window.clearInterval(timer);
  }, [loadEntities, loadEvents, loadRelations, loadStatus, status?.graph_status, view]);

  const triggerBuild = async (rebuild: boolean) => {
    setBuilding(true);
    try {
      await postData(`/knowledge-bases/${kbId}/graph/${rebuild ? "rebuild" : "build-pending"}`, {});
      message.success("已触发图谱构建，稍后刷新状态");
      window.setTimeout(() => void loadStatus(), 1500);
    } catch (e) {
      message.error(e instanceof Error ? e.message : "触发构建失败");
    } finally {
      setBuilding(false);
    }
  };

  const search = async () => {
    if (!query.trim()) return;
    setSearching(true);
    try {
      const data = await postData<GraphSearchResult>(`/knowledge-bases/${kbId}/graph/search`, {
        query: query.trim(),
        strategy,
        hop,
      });
      setResult(data);
      setResultPage(1);
      if (data.entities?.length) setResultView("实体");
      else if (data.relations.length) setResultView("关系");
      else if (data.events?.length) setResultView("事件");
      else setResultView("社区");
    } catch (e) {
      message.error(e instanceof Error ? e.message : "图检索失败");
    } finally {
      setSearching(false);
    }
  };

  const submitExplorerSearch = (value: string) => {
    const nextQuery = value.trim();
    setExplorerSearchInput(value);
    setExplorerQuery(nextQuery);
    setEntityPage(1);
    setRelationPage(1);
    setEventPage(1);
  };

  const openEntity = useCallback(async (entity: GraphEntityItem) => {
    setSelectedEntity(entity);
    setView("拓扑");
    try {
      const data = await getData<GraphSubgraph>(`/knowledge-bases/${kbId}/graph/subgraph?entity_id=${entity.id}&hop=${hop}&limit=100`);
      setSubgraph(data);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "加载子图失败");
    }
  }, [hop, kbId]);

  useEffect(() => {
    if (selectedEntity || subgraph || !relationsLoaded) return;
    const relationDegree = new Map<string, { count: number; name: string; description: string | null }>();
    relations.forEach((relation) => {
      const source = relationDegree.get(relation.source_entity_id);
      relationDegree.set(relation.source_entity_id, { count: (source?.count ?? 0) + 1, name: relation.source_name, description: relation.description ?? null });
      const target = relationDegree.get(relation.target_entity_id);
      relationDegree.set(relation.target_entity_id, { count: (target?.count ?? 0) + 1, name: relation.target_name, description: relation.description ?? null });
    });
    const connected = [...relationDegree.entries()].sort((left, right) => right[1].count - left[1].count)[0];
    if (connected) {
      const [entityId, relationMeta] = connected;
      const source = entities.find((entity) => entity.id === entityId);
      void openEntity(source ?? {
        id: entityId,
        kb_id: kbId,
        doc_id: null,
        entity_type: "entity",
        name: relationMeta.name,
        canonical_name: relationMeta.name,
        description: relationMeta.description,
      });
      return;
    }
    if (entities.length) void openEntity(entities[0]);
  }, [entities, kbId, openEntity, relations, relationsLoaded, selectedEntity, subgraph]);

  const topology = useMemo(() => {
    const graphNodes = [...new Map((subgraph?.nodes ?? []).map((node) => [node.id, node])).values()];
    const graphEdges = [...new Map((subgraph?.edges ?? []).map((edge) => [edge.id || `${edge.source}-${edge.relation_type}-${edge.target}`, edge])).values()];
    const degree = new Map<string, number>();
    graphEdges.forEach((edge) => {
      degree.set(edge.source, (degree.get(edge.source) ?? 0) + 1);
      degree.set(edge.target, (degree.get(edge.target) ?? 0) + 1);
    });
    const nodes: ForceGraphNode[] = graphNodes.map((node) => ({
      id: node.id,
      label: node.name.length > 16 ? `${node.name.slice(0, 16)}…` : node.name,
      kind: node.entity_type || "实体",
      color: node.id === selectedEntity?.id ? "#f6c65b" : colorForType(node.entity_type || "实体"),
      size: node.id === selectedEntity?.id ? 18 : Math.min(16, 9 + Math.log2((degree.get(node.id) ?? 0) + 1) * 2),
      detail: node.description || `${degree.get(node.id) ?? 0} 条直接关系`,
    }));
    const links: ForceGraphLink[] = graphEdges.map((edge) => ({
      id: edge.id,
      source: edge.source,
      target: edge.target,
      kind: edge.relation_type,
      color: "#7187bd",
      width: 1.25,
      strength: 1,
    }));
    return { nodes, links };
  }, [selectedEntity?.id, subgraph]);

  const handleTopologySelect = useCallback((node: ForceGraphNode) => {
    const source = subgraph?.nodes.find((item) => item.id === node.id);
    if (!source) return;
    void openEntity({
      id: source.id,
      kb_id: kbId,
      doc_id: null,
      entity_type: source.entity_type,
      name: source.name,
      canonical_name: source.name,
      description: source.description ?? null,
    });
  }, [kbId, openEntity, subgraph]);

  const meta = status?.graph_meta;
  const searchEntities = result?.entities ?? [];
  const searchRelations: GraphRelation[] = result?.relations ?? [];
  const searchEvents: GraphEventItem[] = result?.events ?? [];
  const searchCommunities = result?.communities ?? [];
  const resultCounts: Record<GraphResultView, number> = {
    实体: searchEntities.length,
    关系: searchRelations.length,
    事件: searchEvents.length,
    社区: searchCommunities.length,
  };
  const resultStart = (resultPage - 1) * SEARCH_RESULT_PAGE_SIZE;
  const pagedSearchEntities = searchEntities.slice(resultStart, resultStart + SEARCH_RESULT_PAGE_SIZE);
  const pagedSearchRelations = searchRelations.slice(resultStart, resultStart + SEARCH_RESULT_PAGE_SIZE);
  const pagedSearchEvents = searchEvents.slice(resultStart, resultStart + SEARCH_RESULT_PAGE_SIZE);
  const pagedSearchCommunities = searchCommunities.slice(resultStart, resultStart + SEARCH_RESULT_PAGE_SIZE);

  return (
    <div className="kb-graph-panel kb-graph-workbench">
      <section className={`kb-graph-hero status-${status?.graph_status ?? "none"}`}>
        <div className="kb-graph-hero-icon"><span className="material-symbols-rounded">account_tree</span></div>
        <div className="kb-graph-hero-copy">
          <span className="eyebrow">KNOWLEDGE GRAPH</span>
          <h2>关系知识工作区</h2>
          <p>探索实体、关系、事件与社区；查询结果保留到原始分块的证据链。</p>
        </div>
        <div className="kb-graph-actions">
          <GraphStatusTag status={status?.graph_status} />
          <Button
            type="primary"
            loading={building}
            disabled={status?.platform_enabled === false}
            onClick={() => void triggerBuild(false)}
            icon={<span className="material-symbols-rounded" style={{ fontSize: 16 }}>build</span>}
          >
            {status?.graph_status === "failed" ? "重试失败项" : "构建待处理文档"}
          </Button>
          <Dropdown menu={{ items: [{ key: "rebuild", label: "全量重建图谱", danger: true }], onClick: () => void triggerBuild(true) }}><Button aria-label="更多图谱操作" icon={<span className="material-symbols-rounded">more_horiz</span>} /></Dropdown>
        </div>
      </section>

      {status?.graph_status === "building" ? (
        <div className="kb-doc-empty">
          <span className="material-symbols-rounded spin">sync</span>
          <p>图谱构建中…（实体/关系/事件抽取为异步任务，可稍后刷新）</p>
        </div>
      ) : null}

      {status?.platform_enabled === false ? <div className="kb-capability-notice" role="status"><span className="material-symbols-rounded">cloud_off</span><div><b>图谱服务不可用</b><p>历史图谱数据仍可查看，文本检索继续可用；在线构建与查询已禁用。</p></div></div> : null}

      {meta ? (
        <div className="kb-graph-stats" aria-label="图谱统计">
          <span className="tone-blue"><i className="material-symbols-rounded">diversity_1</i><small>实体</small><b>{meta.entities ?? 0}</b></span>
          <span className="tone-violet"><i className="material-symbols-rounded">conversion_path</i><small>关系</small><b>{meta.relations ?? 0}</b></span>
          <span className="tone-amber"><i className="material-symbols-rounded">event_note</i><small>事件</small><b>{meta.events ?? 0}</b></span>
          <span className="tone-teal"><i className="material-symbols-rounded">communities</i><small>社区</small><b>{meta.communities ?? 0}</b></span>
          {typeof meta.chunks_processed === "number" ? (
            <span className="tone-slate"><i className="material-symbols-rounded">view_agenda</i><small>处理分块</small><b>{meta.chunks_processed}</b></span>
          ) : null}
          {meta.error ? <span className="kb-graph-error">错误：{meta.error}</span> : null}
        </div>
      ) : null}

      {/* 图语义检索 */}
      <section className="kb-graph-query-card">
        <header><div><span className="material-symbols-rounded">travel_explore</span><div><h3>图谱语义查询</h3><p>适合全局总结、关系追踪、多跳推理和事件时间线问题。</p></div></div><small>查询会返回关联实体和证据分块</small></header>
        <div className="kb-test-bar">
        <input
          className="kb-test-input"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") void search();
          }}
          placeholder="输入全局性 / 多跳 / 关系型问题，如：韩立的成长经历了哪些阶段？"
        />
        <Select
          size="large"
          value={strategy}
          onChange={setStrategy}
          style={{ width: 130 }}
          options={GRAPH_STRATEGY_OPTIONS}
        />
        <Select
          size="large"
          value={hop}
          onChange={setHop}
          style={{ width: 90 }}
          options={[1, 2, 3].map((n) => ({ value: n, label: `${n} 跳` }))}
        />
        <Button
          type="primary"
          size="large"
          loading={searching}
          onClick={() => void search()}
          disabled={!query.trim() || status?.platform_enabled === false}
          aria-disabled={!query.trim() || status?.platform_enabled === false}
          icon={<span className="material-symbols-rounded" style={{ fontSize: 16 }}>hub</span>}
        >
          图检索
        </Button>
        </div>
      </section>

      {result ? (
        <section className="kb-graph-search-results" aria-label="图检索结果">
          <header className="kb-graph-result-header">
            <div>
              <span className="material-symbols-rounded">manage_search</span>
              <div><span className="eyebrow">GRAPH SEARCH RESULT</span><h3>“{query.trim()}” 的检索结果</h3></div>
            </div>
            <div className="kb-graph-result-meta"><Tag>{result.strategy}</Tag><span>{result.evidence_chunk_ids.length} 个证据分块</span></div>
          </header>
          <div className="kb-graph-result-switch">
            <Segmented
              block
              value={resultView}
              onChange={(value) => { setResultView(value as GraphResultView); setResultPage(1); }}
              options={(["实体", "关系", "事件", "社区"] as GraphResultView[]).map((item) => ({ label: `${item} ${resultCounts[item]}`, value: item }))}
            />
          </div>
          <div className="kb-graph-result-list">
            {resultView === "实体" ? pagedSearchEntities.map((entity) => (
              <button type="button" className="kb-graph-result-row entity" key={entity.id} onClick={() => void openEntity(entity)}>
                <i className="material-symbols-rounded">category</i>
                <span><b>{entity.canonical_name || entity.name}</b><small>{entity.description || "暂无实体描述"}</small></span>
                <span className="kb-graph-type-pill">{entity.entity_type}</span>
                <span className="material-symbols-rounded action">arrow_outward</span>
              </button>
            )) : null}
            {resultView === "关系" ? pagedSearchRelations.map((relation) => (
              <div className="kb-graph-result-row relation" key={relation.relation_id ?? `${relation.source_entity_id}-${relation.relation_type}-${relation.target_entity_id}`}>
                <i className="material-symbols-rounded">conversion_path</i>
                <span><b>{relation.source_name} <em>— {relation.relation_type} →</em> {relation.target_name}</b><small>{relation.description || "暂无关系证据描述"}</small></span>
                <span className="kb-graph-evidence">{relation.source_chunk_id ? `分块 #${relation.source_chunk_id}` : "无分块锚点"}</span>
              </div>
            )) : null}
            {resultView === "事件" ? pagedSearchEvents.map((event, index) => (
              <div className="kb-graph-result-row event" key={event.event_id}>
                <span className="kb-graph-order-mark">{event.order_no ?? resultStart + index + 1}</span>
                <span><b>{event.subject_name || event.predicate || "事件"}</b><small>{event.summary || event.object || "暂无事件摘要"}</small></span>
                <span className="kb-graph-type-pill">{event.predicate || event.event_type || "事件"}</span>
              </div>
            )) : null}
            {resultView === "社区" ? pagedSearchCommunities.map((community, index) => (
              <div className="kb-graph-result-row community" key={`${community.level ?? 0}-${resultStart + index}`}>
                <i className="material-symbols-rounded">communities</i>
                <span><b>第 {community.level ?? 0} 层社区</b><small>{community.summary}</small></span>
                <span className="kb-graph-community-keywords">{community.keywords?.slice(0, 3).map((keyword) => <Tag key={keyword}>{keyword}</Tag>)}</span>
              </div>
            )) : null}
            {resultCounts[resultView] === 0 ? <div className="kb-graph-result-empty"><span className="material-symbols-rounded">search_off</span><p>此分类没有匹配结果，可切换分类或调整检索策略。</p></div> : null}
          </div>
          <ExplorerPager current={resultPage} total={resultCounts[resultView]} label="检索结果" pageSize={SEARCH_RESULT_PAGE_SIZE} onChange={setResultPage} />
        </section>
      ) : null}

      <div className="kb-graph-workspace">
        <div className="kb-graph-viewbar">
          <Segmented value={view} onChange={(value) => setView(String(value))} options={["拓扑", "实体", "关系", "事件", "社区"]} />
          <span>{view === "拓扑" ? "按需加载 · 最多 100 节点 / 200 边" : "分页浏览 · 点击实体可展开关系"}</span>
        </div>

        {["实体", "关系", "事件"].includes(view) ? (
          <div className="kb-graph-explorer-search">
            <Input.Search
              allowClear
              aria-label={`搜索${view}`}
              value={explorerSearchInput}
              onChange={(event) => {
                const value = event.target.value;
                setExplorerSearchInput(value);
                if (!value && explorerQuery) submitExplorerSearch("");
              }}
              onSearch={submitExplorerSearch}
              enterButton="搜索"
              placeholder={view === "实体" ? "搜索实体名称" : view === "关系" ? "搜索起点、终点、关系类型或证据" : "搜索事件主体、动作或摘要"}
            />
            <span>{explorerQuery ? `正在筛选“${explorerQuery}”` : `在全部${view}中搜索`}</span>
          </div>
        ) : null}

        {view === "拓扑" ? (
          <div className="kb-topology-surface">
            {topology.nodes.length ? (
              <Suspense fallback={<div className="force-graph-loading"><span className="material-symbols-rounded spin">progress_activity</span><p>正在启动 GPU 图谱引擎…</p></div>}>
                <CosmographCanvas
                  nodes={topology.nodes}
                  links={topology.links}
                  selectedId={selectedEntity?.id}
                  ariaLabel={`以 ${selectedEntity?.canonical_name ?? selectedEntity?.name ?? "当前实体"} 为中心的知识图谱`}
                  legend={[
                    { label: "当前实体", color: "#f6c65b" },
                    { label: "关联实体", color: "#5f8cff" },
                    { label: "有向关系", color: "#7187bd", variant: "line" },
                  ]}
                  note="节点大小表示连接度；箭头表示关系方向。点击任意节点可继续展开，以当前跳数加载新的局部关系网。"
                  onSelect={handleTopologySelect}
                />
              </Suspense>
            ) : <div className="kb-doc-empty"><span className="material-symbols-rounded">hub</span><p>从实体表选择一个实体，加载受限规模的关系子图。</p></div>}
          </div>
        ) : null}

        {view === "实体" ? (
          <section className="kb-graph-data-view" aria-label="实体列表">
            <div className="kb-graph-table entity-table" role="table">
              <div role="row" className="head"><span>实体</span><span>类型</span><span>描述</span><span>证据</span></div>
              {entities.map((entity) => (
                <button role="row" key={entity.id} onClick={() => void openEntity(entity)}>
                  <span className="kb-graph-primary-cell"><i className="material-symbols-rounded">category</i><b>{entity.canonical_name || entity.name}</b></span>
                  <span className="kb-graph-type-pill">{entity.entity_type}</span>
                  <span className="kb-graph-description">{entity.description || "暂无描述"}</span>
                  <span className="kb-graph-evidence">{entity.chunk_ids?.length ?? 0}<i className="material-symbols-rounded">arrow_outward</i></span>
                </button>
              ))}
            </div>
            <ExplorerPager current={entityPage} total={entityTotal} label="实体" onChange={setEntityPage} />
          </section>
        ) : null}

        {view === "关系" ? (
          <section className="kb-graph-data-view" aria-label="关系列表">
            <div className="kb-graph-table relation-table" role="table">
              <div role="row" className="head"><span>起点实体</span><span>关系类型</span><span>终点实体</span><span>证据</span></div>
              {relations.length ? relations.map((relation) => (
                <div role="row" key={relation.relation_id ?? `${relation.source_entity_id}-${relation.relation_type}-${relation.target_entity_id}`}>
                  <span className="kb-graph-primary-cell"><i className="material-symbols-rounded">trip_origin</i><b>{relation.source_name}</b></span>
                  <span className="kb-graph-relation-pill"><i className="material-symbols-rounded">arrow_forward</i>{relation.relation_type}</span>
                  <span className="kb-graph-target-cell">{relation.target_name}</span>
                  <span className="kb-graph-evidence">{relation.source_chunk_id ? `#${relation.source_chunk_id}` : "—"}</span>
                </div>
              )) : <div className="kb-graph-empty-row"><span className="material-symbols-rounded">conversion_path</span><p>暂无关系数据</p></div>}
            </div>
            <ExplorerPager current={relationPage} total={relationTotal} label="关系" onChange={setRelationPage} />
          </section>
        ) : null}

        {view === "事件" ? (
          <section className="kb-graph-data-view" aria-label="事件列表">
            <div className="kb-graph-table event-table" role="table">
              <div role="row" className="head"><span>顺序</span><span>主体 / 动作</span><span>事件摘要</span><span>证据</span></div>
              {events.length ? events.map((event, index) => (
                <div role="row" key={event.event_id}>
                  <span className="kb-graph-order-mark">{event.order_no ?? (eventPage - 1) * EXPLORER_PAGE_SIZE + index + 1}</span>
                  <span className="kb-graph-event-action"><b>{event.subject_name || "未知主体"}</b><small>{event.predicate || event.event_type || "事件"}</small></span>
                  <span className="kb-graph-description">{event.summary || event.object || "暂无摘要"}</span>
                  <span className="kb-graph-evidence">{event.source_chunk_id ? `#${event.source_chunk_id}` : "—"}</span>
                </div>
              )) : <div className="kb-graph-empty-row"><span className="material-symbols-rounded">event_note</span><p>暂无事件数据</p></div>}
            </div>
            <ExplorerPager current={eventPage} total={eventTotal} label="事件" onChange={setEventPage} />
          </section>
        ) : null}

        {view === "社区" ? (
          <section className="kb-graph-data-view" aria-label="社区摘要">
            <div className="kb-graph-table community-table" role="table">
              <div role="row" className="head"><span>层级</span><span>关键词</span><span>摘要</span><span>成员</span></div>
              {(result?.communities ?? []).map((community, index) => <div role="row" key={index}><span>{community.level ?? 0}</span><span>{community.keywords?.join("、") || "—"}</span><span>{community.summary}</span><span>—</span></div>)}
              {!result?.communities?.length ? <div className="kb-graph-empty-row"><span className="material-symbols-rounded">communities</span><p>执行一次全局社区查询后，这里会展示匹配摘要。</p></div> : null}
            </div>
          </section>
        ) : null}
      </div>
    </div>
  );
}
