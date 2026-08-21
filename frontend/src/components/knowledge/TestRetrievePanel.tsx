import { useState } from "react";
import { Button, Segmented, Select, Switch, Tag, Tooltip, message } from "antd";
import { useNavigate } from "react-router-dom";
import { postData } from "@/api/http";
import type { IndexMode, UnifiedRetrieveResult } from "@/types/api";
import { buildTraceUrl } from "@/utils/trace";

const MODE_LABELS: Record<string, string> = {
  auto: "自动",
  text: "文本",
  graph: "图谱",
  fused: "融合",
};

const DEGRADATION_LABELS: Record<string, string> = {
  graph_not_ready: "图谱尚未就绪，本次使用文本检索",
  graph_channel_failed: "图谱通道失败，已返回文本结果",
  no_knowledge_base: "没有可检索的知识库",
};

export default function TestRetrievePanel({
  kbId,
  indexMode,
}: {
  kbId: string;
  indexMode: IndexMode;
}) {
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [topK, setTopK] = useState(5);
  const [mode, setMode] = useState<"auto" | "text" | "graph" | "fused">("auto");
  const [rerank, setRerank] = useState(true);
  const [hop, setHop] = useState(2);
  const [view, setView] = useState("综合结果");
  const [result, setResult] = useState<UnifiedRetrieveResult | null>(null);
  const [searching, setSearching] = useState(false);

  const search = async () => {
    if (!query.trim()) return;
    setSearching(true);
    try {
      const data = await postData<UnifiedRetrieveResult>(
        `/knowledge-bases/${kbId}/test-search`,
        { query: query.trim(), top_k: topK, mode, rerank, graph_hop: hop }
      );
      setResult(data);
    } catch (error) {
      message.error(error instanceof Error ? error.message : "检索失败");
    } finally {
      setSearching(false);
    }
  };

  const hits: UnifiedRetrieveResult["hits"] = view === "文本命中"
    ? (result?.text_hits ?? []).map((hit) => ({ ...hit, channels: ["text"] }))
    : result?.hits ?? [];

  return (
    <div className="kb-test-panel">
      <div className="kb-panel-toolbar">
        <div className="kb-panel-title">
          <span className="material-symbols-rounded">travel_explore</span>
          <b>测试检索</b>
        </div>
        <div className="kb-test-controls">
          <span className="kb-test-control-label">模式</span>
          <Select
            value={mode}
            onChange={setMode}
            disabled={searching}
            options={[
              { value: "auto", label: "自动" },
              { value: "text", label: "文本" },
              { value: "graph", label: "图谱", disabled: indexMode === "standard" },
              { value: "fused", label: "融合", disabled: indexMode === "standard" },
            ]}
          />
          <span className="kb-test-control-label">
            Rerank
            <Tooltip title="服务不可用时自动跳过">
              <span className="material-symbols-rounded kb-test-control-help">help</span>
            </Tooltip>
          </span>
          <Switch checked={rerank} onChange={setRerank} disabled={searching} />
        </div>
      </div>
      <form
        className="kb-test-bar"
        onSubmit={(event) => {
          event.preventDefault();
          void search();
        }}
      >
        <label className="sr-only" htmlFor="kb-test-query">检索问题</label>
        <input
          id="kb-test-query"
          className="kb-test-input"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="输入问题，比较文本与图谱通道…"
        />
        <Select
          aria-label="返回数量"
          value={topK}
          onChange={setTopK}
          options={[2, 3, 5, 8, 10].map((value) => ({ value, label: `Top ${value}` }))}
        />
        {mode === "graph" || mode === "fused" ? (
          <Select
            aria-label="图谱跳数"
            value={hop}
            onChange={setHop}
            options={[1, 2, 3].map((value) => ({ value, label: `${value} 跳` }))}
          />
        ) : null}
        <Button
          htmlType="submit"
          type="primary"
          loading={searching}
          disabled={!query.trim()}
          icon={<span className="material-symbols-rounded">search</span>}
        >
          检索
        </Button>
      </form>

      {result ? (
        <>
          <div className="kb-retrieve-summary" aria-live="polite">
            <span>实际模式 <b>{MODE_LABELS[result.resolved_mode]}</b></span>
            <span>文本召回 <b>{result.text_hits.length}</b></span>
            <span>图谱召回 <b>{result.graph_context.relations.length} 条关系 / {result.evidence_chunks.length} 个证据</b></span>
            <span>总耗时 <b>{result.timings.total_ms}ms</b></span>
            {result.degradations.length ? (
              <span className="warning">
                {result.degradations.map((item) => DEGRADATION_LABELS[item] ?? item).join("；")}
              </span>
            ) : <span>降级 <b>无</b></span>}
          </div>
          <Segmented
            value={view}
            onChange={(value) => setView(String(value))}
            options={["综合结果", "文本命中", "图谱上下文"]}
          />
          {view === "图谱上下文" ? (
            <div className="kb-graph-context">
              <h4>关系路径</h4>
              {result.graph_context.relations.length ? result.graph_context.relations.map((relation, index) => (
                <div key={`${relation.source_entity_id}-${relation.target_entity_id}-${index}`}>
                  <b>{relation.source_name}</b>
                  <span>—[{relation.relation_type}]→</span>
                  <b>{relation.target_name}</b>
                  {relation.source_chunk_id != null ? (
                    <button onClick={() => navigate(buildTraceUrl(kbId, relation.source_chunk_id!))}>
                      定位证据
                    </button>
                  ) : <Tag>暂无原文证据</Tag>}
                </div>
              )) : <p>当前查询没有返回图谱关系。</p>}
            </div>
          ) : (
            <div className="kb-hit-list">
              {hits.map((hit) => (
                <article className="kb-hit-card" key={`${hit.chunk_id}-${hit.strategy}`}>
                  <div className="kb-hit-head">
                    <span className="kb-hit-source">
                      <span className="material-symbols-rounded">description</span>
                      {hit.source_file}
                    </span>
                    {hit.channels.map((channel) => (
                      <Tag key={channel} color={channel === "graph" ? "blue" : undefined}>
                        {channel === "graph" ? "图谱" : "文本"}
                      </Tag>
                    ))}
                    <span className="kb-hit-score">{hit.score.toFixed(2)}</span>
                    <button
                      type="button"
                      className="kb-hit-locate"
                      onClick={() => navigate(buildTraceUrl(hit.kb_id, hit.chunk_id))}
                    >
                      <span className="material-symbols-rounded">open_in_new</span>
                      定位证据
                    </button>
                  </div>
                  <div className="kb-hit-text">{hit.text}</div>
                </article>
              ))}
            </div>
          )}
        </>
      ) : null}
      {result && result.hits.length === 0 ? (
        <div className="kb-doc-empty">
          <span className="material-symbols-rounded">search_off</span>
          <p>未检索到相关内容，可调整问题、模式或索引状态。</p>
        </div>
      ) : null}
    </div>
  );
}
