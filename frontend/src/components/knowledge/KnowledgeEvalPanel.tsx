// 检索评测面板（AGENTIC_RAG_UPGRADE_SPEC §4.2，最小实现）：
// 粘贴评测集 JSON → 选择 variants → 触发评测 → variant 汇总表 + 逐用例明细 + 历史记录
import { useCallback, useEffect, useState } from "react";
import { Button, Select, Spin, Table, Tag, message } from "antd";
import type { ColumnsType } from "antd/es/table";
import { getData, postData } from "@/api/http";
import type { EvaluationRunItem, EvaluationVariantResult } from "@/types/api";

const DEFAULT_VARIANTS = ["baseline", "hybrid", "hybrid+rerank"];

const VARIANT_OPTIONS = [
  { value: "baseline", label: "baseline（纯向量）" },
  { value: "hybrid", label: "hybrid（混合检索）" },
  { value: "hybrid+rerank", label: "hybrid+rerank（混合+精排）" },
  { value: "vector", label: "vector（向量）" },
  { value: "keyword", label: "keyword（关键词）" },
];

const DATASET_PLACEHOLDER = `[
  { "query": "GW-2000 网关功耗", "expected_chunk_ids": [12, 13] },
  { "query": "整机保修多久", "expected_doc_ids": [3] }
]`;

function VariantNameTag({ variant }: { variant: string }) {
  const color =
    variant === "baseline" ? "default" : variant === "hybrid" ? "blue" : "geekblue";
  return <Tag color={color}>{variant}</Tag>;
}

export default function KnowledgeEvalPanel({ kbId }: { kbId: string }) {
  const [datasetText, setDatasetText] = useState("");
  const [variants, setVariants] = useState<string[]>(DEFAULT_VARIANTS);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<EvaluationRunItem | null>(null);
  const [history, setHistory] = useState<EvaluationRunItem[]>([]);
  const [loadingHistory, setLoadingHistory] = useState(false);

  const loadHistory = useCallback(async () => {
    if (!kbId) return;
    setLoadingHistory(true);
    try {
      const items = await getData<EvaluationRunItem[]>(
        `/knowledge-bases/${kbId}/evaluations`
      );
      setHistory(items ?? []);
    } catch {
      // 历史加载失败不打断主流程
    } finally {
      setLoadingHistory(false);
    }
  }, [kbId]);

  useEffect(() => {
    void loadHistory();
  }, [loadHistory]);

  const run = async () => {
    const text = datasetText.trim();
    if (!text) {
      message.warning("请先粘贴评测集 JSON");
      return;
    }
    let dataset: unknown;
    try {
      dataset = JSON.parse(text);
    } catch {
      message.error("评测集 JSON 解析失败，请检查格式");
      return;
    }
    if (!Array.isArray(dataset) || dataset.length === 0) {
      message.error("评测集必须是非空 JSON 数组");
      return;
    }
    if (variants.length === 0) {
      message.warning("请至少选择一个评测 variant");
      return;
    }
    setRunning(true);
    try {
      const runResult = await postData<EvaluationRunItem>(
        `/knowledge-bases/${kbId}/evaluate`,
        {
          name: `评测 ${new Date().toLocaleString("zh-CN", { hour12: false })}`,
          dataset,
          variants,
        }
      );
      setResult(runResult);
      message.success("评测完成");
      void loadHistory();
    } catch (e) {
      message.error(e instanceof Error ? e.message : "评测失败");
    } finally {
      setRunning(false);
    }
  };

  const variantColumns: ColumnsType<EvaluationVariantResult> = [
    {
      title: "Variant",
      dataIndex: "variant",
      key: "variant",
      width: 160,
      render: (variant: string) => <VariantNameTag variant={variant} />,
    },
    {
      title: "HitRate@k",
      dataIndex: "hit_rate",
      key: "hit_rate",
      width: 120,
      render: (value: number) => <b>{(value * 100).toFixed(1)}%</b>,
    },
    {
      title: "MRR@k",
      dataIndex: "mrr",
      key: "mrr",
      width: 120,
      render: (value: number) => value.toFixed(4),
    },
    {
      title: "用例命中",
      key: "hits",
      render: (_, record) =>
        `${record.cases.filter((c) => c.hit).length}/${record.cases.length}`,
    },
  ];

  const current = result;
  const judge = current?.results?.judge ?? null;

  return (
    <div className="kb-eval-panel">
      <div className="kb-panel-toolbar">
        <div className="kb-panel-title">
          <span className="material-symbols-rounded">fact_check</span>
          <b>检索评测</b>
        </div>
      </div>

      <div className="kb-eval-editor">
        <textarea
          className="kb-eval-input"
          value={datasetText}
          onChange={(event) => setDatasetText(event.target.value)}
          placeholder={DATASET_PLACEHOLDER}
          spellCheck={false}
          rows={8}
        />
        <div className="kb-eval-actions">
          <span className="kb-test-control-label">Variants</span>
          <Select
            mode="multiple"
            value={variants}
            onChange={setVariants}
            disabled={running}
            style={{ minWidth: 320 }}
            options={VARIANT_OPTIONS}
            maxTagCount="responsive"
          />
          <Button
            type="primary"
            size="large"
            loading={running}
            onClick={() => void run()}
            icon={
              <span className="material-symbols-rounded" style={{ fontSize: 16 }}>
                play_arrow
              </span>
            }
          >
            运行评测
          </Button>
        </div>
        <p className="kb-eval-hint">
          <span className="material-symbols-rounded">lightbulb</span>
          每个用例需包含 query 与 expected_chunk_ids（或 expected_doc_ids 宽松匹配文档）；
          可选 reference_answer 供 LLM-judge 使用。非法用例将自动跳过并计数。
        </p>
      </div>

      {running ? (
        <div className="kb-eval-running">
          <Spin size="small" />
          评测运行中，逐用例执行 variants 检索…
        </div>
      ) : null}

      {current?.results ? (
        <div className="kb-eval-result">
          <div className="kb-eval-result-head">
            <b>评测结果 · {current.name}</b>
            <span className="kb-eval-stats">
              共 {current.results.stats.total} 条 · 有效 {current.results.stats.valid} · 跳过{" "}
              {current.results.stats.skipped}
            </span>
          </div>
          <Table<EvaluationVariantResult>
            rowKey="variant"
            size="small"
            columns={variantColumns}
            dataSource={current.results.variants}
            pagination={false}
            expandable={{
              defaultExpandAllRows: true,
              expandedRowRender: (record) => (
                <div className="kb-eval-cases">
                  {record.cases.map((c, i) => (
                    <div className="kb-eval-case" key={`${record.variant}-${i}`}>
                      <span className="kb-eval-case-query">{c.query}</span>
                      {c.hit ? (
                        <Tag color="success">命中 #{c.rank}</Tag>
                      ) : (
                        <Tag color="default">未命中</Tag>
                      )}
                      <span className="kb-eval-case-ids">
                        top: {c.top_chunk_ids.join(", ") || "—"}
                      </span>
                    </div>
                  ))}
                </div>
              ),
            }}
          />
          {judge ? (
            <div className="kb-eval-judge">
              <b>LLM-judge 端到端评分</b>
              <span>
                faithfulness 均分 {judge.avg_faithfulness.toFixed(2)} · relevancy 均分{" "}
                {judge.avg_relevancy.toFixed(2)}
              </span>
              <div className="kb-eval-cases">
                {judge.cases.map((c, i) => (
                  <div className="kb-eval-case" key={`judge-${i}`}>
                    <span className="kb-eval-case-query">{c.query}</span>
                    <Tag color="blue">忠实 {c.faithfulness}</Tag>
                    <Tag color="geekblue">切题 {c.relevancy}</Tag>
                  </div>
                ))}
              </div>
            </div>
          ) : null}
        </div>
      ) : null}

      <div className="kb-eval-history">
        <div className="kb-panel-title">
          <span className="material-symbols-rounded">history</span>
          <b>历史评测</b>
        </div>
        {loadingHistory ? (
          <div className="kb-eval-running">
            <Spin size="small" /> 加载中…
          </div>
        ) : history.length === 0 ? (
          <div className="kb-doc-empty">
            <span className="material-symbols-rounded">inbox</span>
            <p>暂无评测记录，粘贴评测集并运行首次评测。</p>
          </div>
        ) : (
          <div className="kb-eval-cases">
            {history.map((run) => (
              <button
                type="button"
                className="kb-eval-case kb-eval-history-item"
                key={run.evaluation_id}
                onClick={() => setResult(run)}
              >
                <span className="kb-eval-case-query">{run.name}</span>
                <span className="kb-eval-stats">
                  {(run.variants ?? []).join(" / ")}
                </span>
                <span className="kb-eval-stats">
                  {run.results?.variants
                    .map((v) => `${v.variant}: ${(v.hit_rate * 100).toFixed(0)}%`)
                    .join(" · ") || "—"}
                </span>
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
