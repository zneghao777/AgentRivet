# 企业级 Agentic RAG 升级 · 技术规格书（Agentic RAG Upgrade Spec）

- 版本：v1.0.0
- 日期：2026-08-21
- 关联：`docs/RAG_SPEC.md`（v1.0.0）、`docs/KB_MANAGEMENT_SPEC.md`（v2.0.0）、`docs/RAG_RESEARCH.md`、`docs/ENTERPRISE_RAG_GAP_ANALYSIS.md`
- 状态：**待开发**（依据本 SPEC 单窗口实施，实施后按 §9 验收）
- 开发方式：**新开窗口按本 SPEC 逐里程碑实现**

---

## 0. 目标与范围

在现有 **L1 Agentic RAG**（`knowledge_search` 工具 + ReAct 循环 + pgvector 纯向量检索 + 溯源闭环）基础上，升级为**企业级 L2 Agentic RAG**，补齐三条链路：

1. **检索管线升级（Naive → Advanced）**：纯向量检索升级为 **混合检索（BM25 关键词 + 向量 + RRF 融合）+ Rerank 精排 + Query 改写（可开关）**，解决专有名词/型号/数值/代码等向量失效场景，提升召回与排序质量。
2. **Agentic 深度升级（L1 → L2）**：`knowledge_search` 从"单策略工具"升级为**检索代理**——工具暴露 `strategy / rerank / expand_query` 参数，由 LLM 自主决定检索策略与是否精排；多路召回在同一工具内编排。
3. **RAG 评测闭环（Evaluation）**：新增**检索评测**（HitRate / MRR@k）+ **端到端评测**（LLM-as-judge：faithfulness / relevancy，可选），复用现有 `EvaluationRun` / `TraceScore` 表，让"换 embedding / 换分块参数 / 换检索策略是否变好"可量化。

**明确不做（本期非目标，后续版本）**：
- 不做 schema 迁移工具（Alembic）、持久化任务队列、对象存储、文档级 ACL、复杂版面解析（见 `ENTERPRISE_RAG_GAP_ANALYSIS.md` P0/P2 项）。
- 不做 GraphRAG、语义分块、父子分块（保留扩展点）。

---

## 1. 技术选型（已实地验证 / 明确落地方式）

### 1.1 BM25 关键词检索：Postgres `pg_trgm`（零新增扩展）

- **扩展**：`CREATE EXTENSION IF NOT EXISTS pg_trgm`（Postgres 16 自带）。
- **索引**：`knowledge_chunks.chunk_text` 上建 **GIN trgm 索引**：`CREATE INDEX ix_chunks_trgm ON knowledge_chunks USING gin (chunk_text gin_trgm_ops)`。
- **检索**：`similarity(chunk_text, :q) >= :threshold`（阈值可配）或 `chunk_text % :q`，按相似度降序取 `KEYWORD_TOP_K`。
- **中文友好性**：pg_trgm 按 3-gram 切分，对中文/英文/数字/型号均有效，不依赖分词插件；短查询、专有名词命中优于纯向量。已确认 `pg_trgm` 随 PG 自带，零运维成本。
- **扩展点**（本期不做）：`zhparser` 中文分词 + tsvector 实现真正词级 BM25，`KEYWORD_BACKEND` 配置预留 `pg_trgm | tsvector`。

### 1.2 Rerank 精排：cross-encoder（bge-reranker-v2-m3）

- **模型**：`bge-reranker-v2-m3`（中文精排业界主流）。
- **服务方式（二选一，可降级）**：
  - **HTTP 端点（首选）**：OpenAI 兼容 `/rerank` 服务（LM Studio / vLLM rerank 支持）。请求 `{model, query, documents, top_n}` → `{results:[{index, relevance_score}]}`。`RERANK_BASE_URL` 指向用户本地推理服务（如 `http://192.168.31.185:1234/v1`）。
  - **进程内 fallback（可选）**：`FlagEmbedding` 库本地加载 `bge-reranker-v2-m3`（需模型权重，标注为可选依赖）。
- **降级红线**：Rerank 服务不可达 / 未配置 → **跳过 rerank，直接返回粗排结果**（与现行为一致），不阻塞主流程；指标记录 `kb_rerank_total{status=skipped}`。
- **调用时机**：粗排（混合或纯向量）取 top-k（默认 20）→ rerank → 取 `RERANK_TOP_N`（默认 5）返回给 Agent。

### 1.3 Query 改写：复用现有 LLM（`ChatOpenAI`）

- 用 `create_llm()`（`base_agent.py`）调用一次轻量改写：把口语/指代不清的问题改写成检索友好表述。
- **默认关闭**（`QUERY_EXPAND_ENABLED=False`），因为增加一次 LLM 调用成本与延迟；开启后仅作用于检索 query，不回写对话。
- 改写失败/超时 → 使用原始 query，不影响主流程。

### 1.4 评测：LLM-as-judge（可选）+ 指标计算（纯函数）

- **检索评测指标**：`hit_rate@k`、`mrr@k`，纯 Python 计算，无外部依赖。
- **端到端评测（可选）**：调用 `create_llm()` 作为 judge，对 `(query, contexts, answer)` 打 `faithfulness / relevancy` 分数（1-5），存 `TraceScore`（`source="rag_eval"`）。
- **评测集存储**：复用现有 `EvaluationRun` 表（`dataset` JSON 存用例，`results` JSON 存指标），复用 `router_observability.py` 的 `create_evaluation` / 列表接口风格。

---

## 2. 总体架构

```
┌────────────────────────── 前端（React/TS）──────────────────────────┐
│  TestRetrievePanel（增强）：strategy 选择 + Rerank 开关 + 命中策略徽标 │
│  KnowledgeDetailPage（可选）：评测入口 + 评测结果面板                 │
└──────────────┬──────────────────────────────────────────────────────┘
               │ REST
┌──────────────▼──────────────────────────────────────────────────────┐
│  backend/app/api/router_knowledge.py                                  │
│   + /test-search（改造：strategy / rerank / expand_query 参数）       │
│   + /evaluate（新增：触发检索评测 runner）                            │
├──────────────────────────────────────────────────────────────────────┤
│  knowledge 服务层（本次核心）                                          │
│   ├─ retriever.py         重构：retrieve(strategy, rerank, expand)    │
│   │    ├─ 向量路：现有 pgvector 余弦（保留）                          │
│   │    ├─ 关键词路：pg_trgm similarity（新增）                        │
│   │    └─ RRF 融合：rank 融合（新增）                                 │
│   ├─ reranker.py          新增：cross-encoder 精排（HTTP/进程内）     │
│   ├─ query_expander.py    新增：LLM Query 改写（可开关）              │
│   └─ evaluator.py         新增：HitRate/MRR 计算 + LLM-judge          │
├──────────────────────────────────────────────────────────────────────┤
│  storage/（Postgres + pgvector + pg_trgm）                            │
│   - knowledge_chunks 增加 GIN trgm 索引（无 schema 列变更）           │
│   - 复用 EvaluationRun / TraceScore（无新表）                         │
└──────────────────────────────────────────────────────────────────────┘
```

**Agentic 决策链（L2）**：
```
用户提问 → ReAct 循环 → LLM 判定需要知识 → 调 knowledge_search(strategy=?, rerank=?, expand_query=?)
  → 工具内部：改写(可选) → 混合检索(向量+关键词+RRF) → Rerank(可选) → 拼 markdown(带策略/分数标注)
  → Agent 基于片段回答（保留溯源锚点）
```

---

## 3. 后端设计

### 3.1 配置项（`config.py` 追加）

```python
# --- 混合检索 / Rerank / Query 改写（AGENTIC_RAG_UPGRADE_SPEC §1）---
HYBRID_RETRIEVAL_ENABLED: bool = True      # 总开关：false 时退回纯向量（兼容旧行为）
KEYWORD_BACKEND: str = "pg_trgm"           # pg_trgm | tsvector（本期仅 pg_trgm，tsvector 预留）
KEYWORD_TOP_K: int = 20                    # 关键词路召回数
VECTOR_TOP_K: int = 20                     # 向量路召回数（混合时粗排候选数）
KEYWORD_SCORE_THRESHOLD: float = 0.08      # pg_trgm similarity 阈值
RRF_K: int = 60                            # RRF 融合常数（标准 60）

RERANK_ENABLED: bool = True                # Rerank 总开关（服务不可达自动跳过）
RERANK_BASE_URL: str = "http://192.168.31.185:1234/v1"   # OpenAI 兼容 /rerank
RERANK_MODEL: str = "bge-reranker-v2-m3"
RERANK_TOP_N: int = 5                      # 精排后返回条数
RERANK_HTTP_TIMEOUT_SECONDS: int = 30
RERANK_USE_FLAG_EMBEDDING: bool = False    # 进程内 FlagEmbedding fallback（可选）

QUERY_EXPAND_ENABLED: bool = False         # Query 改写开关（默认关）
QUERY_EXPAND_TEMPLATE: str = ""            # 可选改写提示词模板，空则用内置
```

> 命名遵循现有 `LLM_* / RAG_* / KB_*` 大写常量风格；业务代码禁止散落 `os.environ`。

### 3.2 数据模型（`storage/models.py` / `db.py`）

**无 schema 列变更**，仅索引：

```sql
-- db.py create_tables() 幂等追加
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX IF NOT EXISTS ix_chunks_trgm
  ON knowledge_chunks USING gin (chunk_text gin_trgm_ops);
```

**复用现有表**：
- `EvaluationRun`：`dataset` JSON 存评测集，`variants` JSON 存对比组（如 `baseline / hybrid / hybrid+rerank`），`results` JSON 存 `{variant, hit_rate, mrr, ...}`。
- `TraceScore`：LLM-judge 分数（`source="rag_eval"`，`score_name="faithfulness|relevancy"`）。

### 3.3 检索器重构（`retriever.py`）

**保留**：`retrieve()`（纯向量）——供 `HYBRID_RETRIEVAL_ENABLED=False` 时退回、以及兼容既有调用（若 memory 等复用则不动签名）。

**新增统一入口**：

```python
async def retrieve_hybrid(
    db: AsyncSession,
    *,
    tenant_id: str,
    query: str,
    kb_ids: list[uuid.UUID] | None = None,
    top_k: int | None = None,
    threshold: float | None = None,
    strategy: str = "auto",       # auto | vector | keyword | hybrid
    rerank: bool = True,          # 是否 Rerank 精排（受 RERANK_ENABLED 与服务可用性约束）
    expand_query: bool = False,   # 是否 Query 改写（受 QUERY_EXPAND_ENABLED 约束）
) -> list[Hit]:
```

**`Hit` 结构扩展**（向后兼容：新增字段均带默认值）：

```python
@dataclass
class Hit:
    text: str
    source_file: str
    chunk_index: int
    score: float              # 最终展示分（rerank 后为 relevance_score；否则为融合/相似分）
    kb_id: uuid.UUID
    chunk_id: int
    strategy: str = "vector"  # 新增：命中来源 vector | keyword | hybrid | rerank
    rerank_score: float | None = None  # 新增：rerank 原始分
```

**`strategy` 决策逻辑**：
| strategy | 行为 |
|---|---|
| `auto` | `HYBRID_RETRIEVAL_ENABLED` 时走 hybrid，否则 vector（LLM 不指定时的默认） |
| `vector` | 仅向量路（现 `retrieve()` 逻辑），score=余弦 |
| `keyword` | 仅 `pg_trgm` 关键词路，score=similarity |
| `hybrid` | 双路召回 + RRF 融合，score=RRF 融合分 |

**关键词路 SQL**（`keyword_retriever` 内部函数，SQL 必须带租户/知识库/enabled 过滤——安全红线沿用）：

```sql
SELECT chunk_text, source_file, chunk_index, similarity(chunk_text, :q) AS score,
       kb_id, id
FROM knowledge_chunks
WHERE tenant_id = :tenant AND enabled = true
  AND (:kb_ids IS NULL OR kb_id = ANY(:kb_ids))
  AND similarity(chunk_text, :q) >= :threshold
ORDER BY score DESC
LIMIT :top_k
```

**RRF 融合**（纯函数）：

```python
def rrf_fuse(
    vector_hits: list[Hit],
    keyword_hits: list[Hit],
    k: int = settings.RRF_K,
) -> list[Hit]:
    """Reciprocal Rank Fusion：score(d) = Σ 1/(k + rank_r(d))。
    合并去重（按 chunk_id），输出融合后排序的 Hit（保留各路原始 score 供前端展示）。
    """
```

**Rerank 编排**（在 `retrieve_hybrid` 内）：
1. 粗排：按 strategy 得候选（`VECTOR_TOP_K`/`KEYWORD_TOP_K`，默认各 20）→ 融合后取 top `max(VECTOR_TOP_K, KEYWORD_TOP_K)`。
2. `rerank=True` 且 Rerank 可用 → `reranker.rerank(query, candidates)` → 取 `RERANK_TOP_N`，`score=relevance_score`，`strategy="rerank"`。
3. `rerank` 不可用 → 直接返回融合 top_k，`strategy` 标记来源路。
4. 所有 SQL 强制 `tenant_id` 过滤；`kb_ids` 白名单与 `knowledge_search` 现有语义一致。

### 3.4 Rerank 封装（新增 `reranker.py`）

```python
class RerankError(Exception): ...

async def is_rerank_available() -> bool:
    """探测 RERANK_BASE_URL/model，缓存探测结果（如 60s）。"""

async def rerank(query: str, candidates: list[Hit], top_n: int) -> list[Hit]:
    """HTTP：POST {base}/rerank {"model", "query", "documents": [...], "top_n": N}
    返回按 relevance_score 排序的前 top_n Hit（保留 chunk_id/kb_id 溯源主键）。
    失败抛 RerankError → 调用方降级。
    """
```

- HTTP 响应解析：`body["results"] = [{index, relevance_score}]`；`index` 映射回候选列表。
- **审计/指标**：`metrics` 增加 `kb_rerank_total{status=ok|error|skipped}`、`kb_rerank_latency_ms`。
- 进程内 `FlagEmbedding` 后端仅在 `RERANK_USE_FLAG_EMBEDDING=True` 且依赖可选时启用（`requirements.txt` 用 `# optional` 注释，默认不装）。

### 3.5 Query 改写（新增 `query_expander.py`）

```python
async def expand_query(query: str, llm: BaseChatModel | None = None) -> str:
    """LLM 单轮改写：把口语问题改写为检索友好 query。
    失败/超时/空白 → 原样返回。仅当 QUERY_EXPAND_ENABLED 时由 retrieve_hybrid 调用。
    """
```

- 内置模板：`把以下用户问题改写为适合知识库检索的关键词化表述，只输出改写结果：\n{query}`
- `QUERY_EXPAND_TEMPLATE` 非空时优先用配置模板。
- **注意**：改写结果只用于检索，不进入对话消息；审计 `knowledge.query_expand` 记录原始/改写 query（可选）。

### 3.6 检索工具升级（`tools/knowledge_search.py`）

**`KnowledgeSearchArgs` 扩展**：

```python
class KnowledgeSearchArgs(BaseModel):
    query: str = Field(description="检索问题（建议拆分为单个明确意图）")
    knowledge_base: str | None = Field(default=None, description="知识库名称")
    top_k: int = Field(default=5, ge=1, le=10)
    strategy: str = Field(default="auto", description="auto|vector|keyword|hybrid，检索策略")
    rerank: bool = Field(default=True, description="是否对结果做 Rerank 精排")
    expand_query: bool = Field(default=False, description="是否先对 query 做改写（成本较高，慎用）")
```

**实现改造**：`_knowledge_search_async` 内部调用 `knowledge_service.retrieve_v2(...)`（或 `retrieve_hybrid`），透传 `strategy/rerank/expand_query`；`hits_to_markdown` 升级为带策略标注：

```markdown
[片段 1 | 来源: manual.pdf | 相关度 0.87 | 策略: rerank | 引用: KB-6f0e…/12]
```

**工具 description 更新**：说明支持策略选择与精排，供 LLM 自主决策（L2 的关键——LLM 可在多轮对话中根据问题类型切换 `strategy`）。

**注册**：`registry.py` 不变（同名替换即可，`register_builtin` 幂等）。

### 3.7 服务层（`knowledge/service.py`）

新增（`search` / `retrieve` 保留兼容）：

```python
async def retrieve_v2(
    self, db, *, tenant_id, query, kb_ids=None, kb_name=None,
    top_k=5, strategy="auto", rerank=True, expand_query=False,
) -> list[Hit]:
    """供 knowledge_search 工具使用：校验租户/知识库 → 改写(可选) → 混合检索 → Rerank。
    kb_name 指定时校验归属当前租户（防越权，沿用现逻辑）。
    """

async def evaluate(
    self, db, *, tenant_id, kb_id, name, dataset: list[dict],
    variants: list[str] | None = None, judge: bool = False,
) -> EvaluationRun:
    """检索评测 runner：
    1. 对每个用例跑 variants（如 baseline / hybrid / hybrid+rerank）检索，
       用 expected_chunk_ids/expected_doc_ids 计算 hit_rate@k / mrr@k。
    2. judge=True 时追加 LLM-as-judge 端到端评分（faithfulness/relevancy），写 TraceScore。
    3. 落 EvaluationRun（dataset / variants / results）。
    """
```

### 3.8 API（`router_knowledge.py` 扩展）

```
POST /kb/{kb_id}/test-search   改造：body 增加 strategy / rerank / expand_query（默认值兼容旧前端）
POST /kb/{kb_id}/evaluate      新增：body {name, dataset: [{query, expected_chunk_ids?,
                               expected_doc_ids?, reference_answer?}], variants?, judge?}
                               → 触发 evaluate()，返回 EvaluationRun（含 results 汇总）
GET  /kb/{kb_id}/evaluations   新增：列出该 kb 的评测记录（复用 EvaluationRun 查询）
```

**`TestSearchRequest` / `KnowledgeSearchRequest` 扩展**（`schemas.py`）：

```python
class TestSearchRequest(BaseModel):
    query: str = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=10)
    strategy: str = Field(default="auto")       # auto|vector|keyword|hybrid
    rerank: bool = True
    expand_query: bool = False
```

**`KnowledgeHit` 扩展**：`strategy: str = "vector"`、`rerank_score: float | None = None`（前端展示策略徽标）。

**安全红线（沿用）**：所有操作先 `_require_kb`；评测数据集仅限当前租户 kb，越权 404。

### 3.9 评测计算（新增 `evaluator.py`）

```python
def hit_rate(results: list[list[str]], expected: list[set[str]]) -> float
def mrr(results: list[list[str]], expected: list[set[str]]) -> float
# 用例 i 的 expected 为命中目标（chunk_id 集合）；results[i] 为 top-k 命中的 chunk_id 列表。

def parse_dataset(dataset: list[dict]) -> list[EvalCase]  # 校验必填字段，非法用例跳过并计数
```

- `hit_rate@k = |{i : results[i] ∩ expected[i] ≠ ∅}| / n`；`mrr@k = mean(1 / first_hit_rank_i)`。
- LLM-judge：`judge_answer(query, contexts, answer) -> {faithfulness, relevancy}`，调用 `create_llm()`，结构化输出（JSON），失败该用例记 0 并标注。

### 3.10 审计与指标

- 审计 action：`knowledge.test_search`（detail 增加 strategy/rerank）、`knowledge.evaluate`、`knowledge.query_expand`（可选）。
- 指标：`kb_rerank_total{status}`、`kb_rerank_latency_ms`、`kb_retrieval_total{strategy}`（在 `retrieve_hybrid` 内 `metrics.record`）。

---

## 4. 前端设计

### 4.1 TestRetrievePanel 增强（`components/knowledge/TestRetrievePanel.tsx`）

- 工具栏新增 **策略 Select**：`自动 / 向量 / 关键词 / 混合`（映射 `auto/vector/keyword/hybrid`）。
- 新增 **Rerank 开关**（`Switch`，默认开）。
- 命中卡片头部新增**策略徽标**：`rerank`（品牌色）/ `hybrid` / `vector` / `keyword`（参考现有 `SourceReferenceBadge` 质感）。
- 分数展示：有 `rerank_score` 时显示精排分，否则显示原始分。
- 请求体带 `strategy / rerank`，`expand_query` 本期前端不暴露（工具参数保留）。
- 类型扩展：`types/api.ts` 的 `TestSearchRequest` / `RetrieveHit` 同步加 `strategy / rerank / rerank_score`。

### 4.2 评测入口（`components/knowledge/KnowledgeEvalPanel.tsx`，新建，最小实现）

- 位置：`KnowledgeDetailPage` 新增「评测」tab（或独立抽屉入口）。
- 功能：文本域粘贴评测集 JSON（`[{query, expected_chunk_ids}]`）→ 选择 variants（默认 `["baseline","hybrid","hybrid+rerank"]`）→ 触发 `POST /evaluate` → 结果表格展示各 variant 的 `hit_rate / mrr` + 逐用例明细（展开）。
- 空态/加载态/失败提示对齐现有组件风格；响应式沿用三档断点。

### 4.3 交互细节

- 检索中策略 Select / Rerank Switch 置灰（`loading`）。
- 评测运行期间显示进度（`Spin` + 文案），完成后 `message.success`。
- 策略徽标 hover 显示 tooltip 说明。

---

## 5. 实现文件清单

```
backend/app/
  knowledge/
    retriever.py          重构：+ retrieve_hybrid / keyword_retriever / rrf_fuse；Hit 扩展
    reranker.py           新增：HTTP /rerank 封装 + 探测缓存 + 降级
    query_expander.py     新增：LLM 改写
    evaluator.py          新增：hit_rate / mrr / LLM-judge / dataset 解析
    service.py            新增 retrieve_v2 / evaluate；search 兼容保留
  tools/knowledge_search.py  扩展 args（strategy/rerank/expand_query）+ 策略标注 markdown
  api/router_knowledge.py    改造 test-search + 新增 evaluate / evaluations
  api/schemas.py             扩展 TestSearchRequest/KnowledgeHit + 新增 EvaluateRequest
  config.py                  新增 HYBRID_/KEYWORD_/RRF_/RERANK_/QUERY_ 配置
  storage/db.py              create_tables() 追加 pg_trgm 扩展 + GIN trgm 索引
  observability/metrics.py   新增 kb_rerank_* / kb_retrieval_total

backend/tests/
  test_hybrid_retrieval.py   关键词路 SQL 过滤（租户/enabled）、RRF 融合去重、strategy 开关
  test_reranker.py           mock HTTP：成功/失败降级/探测缓存
  test_query_expander.py     改写成功/失败原样返回
  test_rag_evaluator.py      hit_rate / mrr 纯函数 + dataset 解析 + LLM-judge mock
  test_knowledge_api_v2.py   test-search 带 strategy/rerank；evaluate 接口 + 越权 404
  test_knowledge_tool_v2.py  工具透传策略参数；markdown 策略标注

frontend/src/
  components/knowledge/TestRetrievePanel.tsx   增强（策略选择/Rerank 开关/策略徽标）
  components/knowledge/KnowledgeEvalPanel.tsx  新建（评测面板，最小实现）
  pages/KnowledgeDetailPage.tsx                挂载评测 tab
  types/api.ts                                 TestSearchRequest / RetrieveHit / EvalRun 类型
  styles/global.css                            策略徽标 / 评测表格样式（复用 CSS 变量）
```

---

## 6. 测试计划

**单元（无外部依赖，mock LLM / rerank / pg_trgm 结果）**：
- `test_hybrid_retrieval.py`：`rrf_fuse` 融合顺序与去重；`keyword_retriever` SQL 参数（租户/enabled/threshold）；`strategy=vector` 与旧行为一致。
- `test_reranker.py`：HTTP 成功解析 `{results:[{index,relevance_score}]}`；HTTP 错误/超时抛 `RerankError`；`is_rerank_available` 缓存。
- `test_query_expander.py`：改写返回非空、失败原样、空白原样。
- `test_rag_evaluator.py`：`hit_rate/mrr` 边界（全中/全不中/多命中取首）；`parse_dataset` 非法用例跳过。

**API（app_client，mock embedding/rerank）**：
- `test_knowledge_api_v2.py`：`test-search` 带 `strategy/rerank` 返回命中含 `strategy` 字段；`evaluate` 创建 EvaluationRun 且 results 含 `hit_rate/mrr`；跨租户 evaluate 404。
- `test_knowledge_tool_v2.py`：工具 args 透传 `strategy=hybrid`；markdown 含 `策略: hybrid` 标注。

**端到端（真实本地服务，如可用）**：
- 联调脚本：上传文档 → 混合检索命中专有名词（对比纯向量）→ Rerank 排序变化 → 评测 `baseline vs hybrid vs hybrid+rerank` 的 hit_rate/mrr 对比。

**前端**：`tsc -b` + `vite build` 通过；1280/900/375 三档视口布局无溢出。

---

## 7. 里程碑（G-RAG 编号）

| 里程碑 | 内容 | 验收 |
|---|---|---|
| **G-RAG-1 混合检索** | pg_trgm 扩展/索引 + keyword_retriever + RRF 融合 + `retrieve_hybrid`（strategy 支持 vector/keyword/hybrid）+ 配置开关 | 单测过；`HYBRID_RETRIEVAL_ENABLED=False` 时行为与旧版一致 |
| **G-RAG-2 Rerank** | `reranker.py`（HTTP + 探测缓存 + 降级）+ `retrieve_hybrid` 编排 + 指标 | mock 单测过；rerank 不可用时自动降级不报错 |
| **G-RAG-3 Agentic 工具升级** | `knowledge_search` 加 strategy/rerank/expand_query + 策略标注 markdown + 前端 TestRetrievePanel 增强 | 工具可传参；LLM 可按问题类型自主选策略；前端 build 过 |
| **G-RAG-3.5 Query 改写** | `query_expander.py` + 配置开关 + 审计 | 开关关闭零开销；开启后改写失败原样降级 |
| **G-RAG-4 评测闭环** | `evaluator.py` + `evaluate`/`evaluations` API + 前端评测面板 + TraceScore 落库 | HitRate/MRR 计算正确；评测集可跑多 variant 对比；越权 404 |

---

## 8. 风险与决策点

| 风险/决策 | 说明 | 处理 |
|---|---|---|
| **pg_trgm 中文效果** | 3-gram 对长查询/语义匹配不如向量 | 仅作关键词路与向量互补；阈值可调；预留 `tsvector+zhparser` 扩展点 |
| **Rerank 服务可用性** | 本地 `/rerank` 端点未部署/离线 | 探测缓存 + 优雅降级（跳过精排），指标记录 skipped |
| **Query 改写成本** | 每次检索多一次 LLM 调用 | 默认关闭；开关控制；仅改检索 query 不污染对话 |
| **混合检索延迟** | 双路 + rerank 增加耗时 | 粗排 top 各 20（可配）；rerank 仅精排 top；超时兜底 |
| **兼容性** | 旧前端不带新字段 | 默认值（auto/True/False）保证旧请求兼容 |
| **评测集质量** | 手工标注 expected_chunk_ids 耗时 | 支持 `expected_doc_ids` 宽松匹配 + LLM-judge 辅助；评测集可复用可导出 |
| **schema 无迁移** | 本期无表结构变更（仅加索引/扩展） | 不需 Alembic；`db.py` 幂等 DDL |

---

## 9. 验收标准（最终效果）

1. **混合检索生效**：`strategy=hybrid` 时专有名词/型号类 query 召回明显优于纯向量（评测 hit_rate 对比可见提升）。
2. **Rerank 生效**：开启后 top-k 排序质量提升（`strategy="rerank"` 徽标正确展示）；Rerank 服务离线时自动降级，检索不失败。
3. **Agentic L2**：LLM 能自主选择检索策略与精排开关，工具调用参数透传正确；`knowledge_search` markdown 带策略/溯源双标注。
4. **评测闭环**：评测集可一键跑 `baseline vs hybrid vs hybrid+rerank`，输出 `hit_rate / mrr` 对比表；`TraceScore`（rag_eval）落库可查。
5. **无回归**：`HYBRID_RETRIEVAL_ENABLED=False` 时行为与升级前一致；旧前端请求兼容；`pytest -v` 全绿；前端 `tsc` + `vite build` 通过；多租户隔离与审计无回归。
6. **安全红线**：所有新增 SQL 带 `tenant_id` 过滤；评测接口越权 404；Rerank/改写失败均降级不抛 5xx。

---

## 附：后续建议（非本期）

- `ENTERPRISE_RAG_GAP_ANALYSIS.md` P0/P2 项：Alembic 迁移、持久化任务队列、对象存储 + 文件鉴权、文档级 ACL、复杂版面解析、向量库抽象（Qdrant）。
- Advanced 检索再增强：Query 多路（multi-query）、HyDE、父子分块、语义分块。
- GraphRAG（强关联知识/全局问题）。
