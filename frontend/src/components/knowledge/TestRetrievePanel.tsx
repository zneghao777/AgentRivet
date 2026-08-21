// 测试检索面板（KB_MANAGEMENT_SPEC §4.3-C + AGENTIC_RAG_UPGRADE_SPEC §4.1）：
// prompt-bar + context-cards 命中卡片 + 策略选择 + Rerank 开关 + 命中策略徽标
import { useState } from "react";
import { Button, Select, Switch, Tooltip, message } from "antd";
import { postData } from "@/api/http";
import type { RetrieveHit } from "@/types/api";
import { buildTraceUrl } from "@/utils/trace";
import { useNavigate } from "react-router-dom";

const STRATEGY_OPTIONS = [
  { value: "auto", label: "自动" },
  { value: "vector", label: "向量" },
  { value: "keyword", label: "关键词" },
  { value: "hybrid", label: "混合" },
];

const STRATEGY_LABELS: Record<string, string> = {
  vector: "向量",
  keyword: "关键词",
  hybrid: "混合",
  rerank: "精排",
};

const STRATEGY_TOOLTIPS: Record<string, string> = {
  vector: "pgvector 余弦相似度检索（语义匹配）",
  keyword: "pg_trgm 关键词检索（专有名词 / 型号 / 数值）",
  hybrid: "向量 + 关键词双路召回，RRF 融合排序",
  rerank: "cross-encoder 精排（bge-reranker-v2-m3）",
};

function highlight(text: string, keyword: string): React.ReactNode {
  if (!keyword.trim()) return text;
  const escaped = keyword.trim().replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const parts = text.split(new RegExp(`(${escaped})`, "gi"));
  return parts.map((part, index) =>
    part.toLowerCase() === keyword.trim().toLowerCase() ? (
      <mark key={index} className="hit-keyword">{part}</mark>
    ) : (
      <span key={index}>{part}</span>
    )
  );
}

function StrategyBadge({ strategy }: { strategy: string }) {
  const label = STRATEGY_LABELS[strategy] ?? strategy;
  const tooltip =
    STRATEGY_TOOLTIPS[strategy] ?? "未知检索策略";
  return (
    <Tooltip title={tooltip}>
      <span className={`hit-strategy-badge hit-strategy-badge--${strategy}`}>
        {strategy === "rerank" ? "sort" : strategy === "keyword" ? "match_word" : "layers"}
        {label}
      </span>
    </Tooltip>
  );
}

export default function TestRetrievePanel({ kbId }: { kbId: string }) {
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [topK, setTopK] = useState(5);
  const [strategy, setStrategy] = useState("auto");
  const [rerank, setRerank] = useState(true);
  const [hits, setHits] = useState<RetrieveHit[]>([]);
  const [searching, setSearching] = useState(false);
  const [searched, setSearched] = useState(false);

  const search = async () => {
    if (!query.trim()) return;
    setSearching(true);
    try {
      const result = await postData<RetrieveHit[]>(
        `/knowledge-bases/${kbId}/test-search`,
        { query: query.trim(), top_k: topK, strategy, rerank }
      );
      setHits(result);
      setSearched(true);
    } catch (e) {
      message.error(e instanceof Error ? e.message : "检索失败");
    } finally {
      setSearching(false);
    }
  };

  return (
    <div className="kb-test-panel">
      <div className="kb-panel-toolbar">
        <div className="kb-panel-title">
          <span className="material-symbols-rounded">travel_explore</span>
          <b>测试检索</b>
        </div>
        <div className="kb-test-controls">
          <span className="kb-test-control-label">策略</span>
          <Select
            size="large"
            value={strategy}
            onChange={setStrategy}
            disabled={searching}
            style={{ width: 104 }}
            options={STRATEGY_OPTIONS}
          />
          <span className="kb-test-control-label">
            Rerank
            <Tooltip title="cross-encoder 精排：服务不可用时自动跳过">
              <span className="material-symbols-rounded kb-test-control-help">help</span>
            </Tooltip>
          </span>
          <Switch checked={rerank} onChange={setRerank} disabled={searching} />
        </div>
      </div>
      <div className="kb-test-bar">
        <input
          className="kb-test-input"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") void search();
          }}
          placeholder="输入检索问题，验证分块召回效果…（Enter 检索）"
        />
        <Select
          size="large"
          value={topK}
          onChange={setTopK}
          style={{ width: 92 }}
          options={[2, 3, 5, 8, 10].map((n) => ({ value: n, label: `Top ${n}` }))}
        />
        <Button
          type="primary"
          size="large"
          loading={searching}
          onClick={() => void search()}
          disabled={!query.trim()}
          icon={<span className="material-symbols-rounded" style={{ fontSize: 16 }}>search</span>}
        >
          检索
        </Button>
      </div>

      {searched && hits.length === 0 ? (
        <div className="kb-doc-empty">
          <span className="material-symbols-rounded">search_off</span>
          <p>未检索到相关内容，可尝试调整查询词或上传更多文档。</p>
        </div>
      ) : null}

      <div className="kb-hit-list">
        {hits.map((hit) => (
          <div className="kb-hit-card" key={hit.chunk_id}>
            <div className="kb-hit-head">
              <span className="kb-hit-source">
                <span className="material-symbols-rounded">description</span>
                {hit.source_file}
              </span>
              {hit.strategy ? <StrategyBadge strategy={hit.strategy} /> : null}
              <span className="kb-hit-score">
                {typeof hit.rerank_score === "number"
                  ? `精排 ${hit.rerank_score.toFixed(2)}`
                  : hit.score.toFixed(2)}
              </span>
              <span className="kb-hit-index">#{hit.chunk_index + 1}</span>
              <button
                type="button"
                className="kb-hit-locate"
                onClick={() => navigate(buildTraceUrl(hit.kb_id, hit.chunk_id))}
                title="在分块管理页定位"
              >
                <span className="material-symbols-rounded">open_in_new</span>
                在分块中定位
              </button>
            </div>
            <div className="kb-hit-text">{highlight(hit.text, query)}</div>
          </div>
        ))}
      </div>
    </div>
  );
}
