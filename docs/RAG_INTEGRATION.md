# RAG 知识库能力集成方案

> 目标：基于当前 Agent Harness 底座（FastAPI + LangGraph ReAct + 工具/Skill 体系），以**符合现有架构约定**的形式加入 RAG 知识库能力。

## 一、现状与约束

当前底座已有以下关键机制，RAG 必须**复用而非绕过**它们：

| 机制 | 位置 | 对 RAG 的意义 |
|---|---|---|
| 工具注册中心 `ToolRegistry` | `backend/app/tools/registry.py` | RAG 检索应封装为一个**检索工具**，走 `bind_tools` 的 ReAct 循环 |
| Skill 体系 `SkillManager` | `backend/app/skills/` | 知识库可以作为"可加载的 Skill 上下文"，按需注入 system prompt |
| LangGraph 状态图 | `backend/app/harness/graph_builder.py` | 可扩展独立 RAG 节点（预检索）或纯工具式检索 |
| 多租户隔离 `tenant_id` | `HarnessState` / `Session` | 知识库必须**按租户隔离**，检索时注入 `tenant_id` 过滤 |
| 会话持久化 + Checkpointer | `storage/` | RAG 引用来源（citation）可随消息持久化 |
| 可观测性（审计/指标/LangSmith） | `observability/` | 检索调用需纳入审计与指标 |

### 关键架构原则

1. **工具是底座一等公民**：RAG 检索做成 `StructuredTool` 是最"原生"的接入方式，Agent 自主决定何时检索。
2. **Skill 是上下文注入的一等公民**：若需要"固定知识库"（每次对话都带某个知识域），用 Skill 注入比用工具更合适。
3. **向量检索是新增依赖**：需要引入 embedding 模型 + 向量库。底座当前用 Postgres，最优解是 **Postgres + pgvector**（复用现有基础设施，无需新增数据库服务）。

---

## 二、三种接入形式对比（推荐度排序）

### 形式 A：检索工具（RAG-as-Tool）⭐ 推荐首选

把"知识库检索"封装成一个工具，Agent 通过 ReAct 自主决定调用。

```
用户提问 ──▶ agent_node(LLM) ──tool_call──▶ knowledge_retrieval 工具
                                              │
                                    embed(question) ─▶ pgvector 相似度检索（按 tenant 过滤）
                                              │
                                      返回 top-k 命中片段
                                              ▼
                                    agent_node(LLM) 基于片段生成带引用的回答 ──▶ END
```

**优点**
- 接入成本最低，只需新增一个工具文件并在注册中心注册。
- 遵循底座现有 ReAct 机制，Agent 自动判断"要不要查知识库"。
- 检索是有副作用的（消耗 embedding 算力），工具化便于审计、限流、按租户计量。

**缺点**
- 检索依赖 LLM 决定触发，冷门问题可能不触发检索（可通过 system prompt 强化指令缓解）。

### 形式 B：RAG 专用图节点（预检索节点）⭐ 适合"必须检索"场景

在图状态 `HarnessState` 中增加 `retrieved_context` 字段，并在图开头插入一个**预检索节点**，每次对话强制先检索再进 Agent。

```
__start__ ─▶ retrieve_node ─▶ agent_node ──(tool_calls)──▶ tools_node ──▶ agent_node
                │                 ▲
                ▼                 │
        embed(query)              │
        pgvector 检索              │
        state["retrieved_context"]┘ 注入 system prompt
```

**优点**
- 每次必检索，适合"回答必须基于知识库"的强约束场景（如内部客服、合规问答）。
- 检索结果稳定注入 system prompt，不依赖 LLM 判断。
- 检索可复用 `tenant_id`/`skill_loaded` 决定检索范围。

**缺点**
- 改造图结构，需修改 `graph_builder.py` 与 `state.py`，侵入性略高。

### 形式 C：知识库 Skill（Skill 上下文注入）

把"某个知识库"建模为一个 **Skill**，加载后其知识摘要/向量索引信息注入 system prompt。

**优点**
- 与现有 Skill 体系完全一致，用户/会话可通过 `skills` 参数指定加载哪个知识库。
- 多知识库切换天然复用 `skill_loaded` 机制。

**缺点**
- Skill 本质是**静态指令注入**，不适合承载大规模向量检索；检索能力仍需工具或节点完成。
- 适合"知识库路由/选择"层，而非检索实现层。

---

## 三、推荐方案（形式 A + C 组合）

- **检索实现**：形式 A（检索工具），实现检索的"能力底座"。
- **知识库路由**：形式 C（Skill 声明知识库范围），决定"本次对话检索哪个/哪些知识库"。
- **存储**：Postgres + pgvector 扩展，复用现有 Postgres 服务。

组合后的完整链路：

```
1. 会话携带 skills=["hr-policy"]（或由 Agent 自主决定）
2. state["skill_loaded"] = ["hr-policy"]，system prompt 注入该知识库描述与检索指令
3. Agent 决定调用 tool: knowledge_search(query="请假制度", knowledge_base="hr-policy", top_k=5)
4. 工具按 tenant_id + knowledge_base 过滤，pgvector 检索 top-k 片段
5. Agent 基于片段生成回答，并在回答中附带引用（[1] 来源）
```

---

## 四、落地步骤（代码改动清单）

### 1. 数据库与 ORM

**启用 pgvector**：在 `deploy/postgres/init.sql` 增加扩展：

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

**新增表**：`knowledge_documents`（文档/片段元数据 + 向量）。在 `backend/app/storage/models.py` 增加模型：

```python
from sqlalchemy.dialects.postgresql import ARRAY

class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    knowledge_base: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    source_file: Mapped[str] = mapped_column(String(255))
    chunk_text: Mapped[str] = mapped_column(Text, nullable=False)
    # 向量列为动态维度，建议用 Raw DDL 创建，见 init.sql 与索引
    metadata: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
```

> 说明：pgvector 的 `vector` 列类型需要 `Vector` 方言（`sqlalchemy` 通过 `pgvector` 包），或直接在 `init.sql` 用原生 SQL 建表 + HNSW 索引。推荐前者，代码内用 `from pgvector.sqlalchemy import Vector`。

### 2. 依赖

在 `backend/requirements.txt` 增加：

```
pgvector>=0.2
# embedding 可选实现（二选一）：
langchain-openai 已含 OpenAI 兼容接口，可直接用现有 LLM_BASE_URL
```

### 3. 配置

在 `backend/app/config.py` 增加：

```python
# --- RAG / 向量检索 ---
EMBEDDING_MODEL: str = "text-embedding-3-small"
EMBEDDING_BASE_URL: str = "https://api.deepseek.com"  # 复用或单独配置
EMBEDDING_API_KEY: str = ""
EMBEDDING_DIM: int = 1536
RAG_TOP_K: int = 5
RAG_SCORE_THRESHOLD: float = 0.7
```

### 4. 检索服务层

新建 `backend/app/knowledge/` 包：

```
backend/app/knowledge/
├── __init__.py
├── retriever.py      # 检索器：embed + pgvector 相似度查询（按 tenant/kb 过滤）
├── ingest.py         # 文档摄取：分块(chunking) + 入库
└── embedding.py      # embedding 客户端封装（复用 create_llm 的 base_url 思路）
```

`retriever.py` 核心逻辑：

```python
from pgvector.sqlalchemy import Vector
from sqlalchemy import select, text

async def retrieve(db, *, tenant_id, knowledge_base, query, top_k, threshold):
    qvec = await embed(query)
    # pgvector 余弦相似度，按租户+知识库过滤
    stmt = text("""
        SELECT chunk_text, metadata, 1 - (embedding <=> :qvec) AS score
        FROM knowledge_chunks
        WHERE tenant_id = :tenant AND knowledge_base = :kb
          AND 1 - (embedding <=> :qvec) >= :thr
        ORDER BY embedding <=> :qvec
        LIMIT :k
    """)
    # ... 执行并返回 [{text, score, source}] 列表
```

### 5. 检索工具（形式 A 核心）

新建 `backend/app/tools/knowledge_search.py`，遵循内置工具同款写法（`StructuredTool.from_function` + pydantic `args_schema`）：

```python
class KnowledgeSearchArgs(BaseModel):
    query: str = Field(description="检索问题")
    knowledge_base: str = Field(description="知识库名称，如 hr-policy")
    top_k: int = Field(default=5, description="返回片段数")

def _knowledge_search(query: str, knowledge_base: str, top_k: int = 5) -> str:
    # 注入当前 tenant_id（从运行上下文）
    # 调用 retriever，拼接 top-k 片段文本返回给 LLM
    ...

knowledge_search = StructuredTool.from_function(
    func=_knowledge_search,
    name="knowledge_search",
    description="在企业知识库中进行语义检索。当用户询问文档/制度/FAQ 等内部知识时使用。",
    args_schema=KnowledgeSearchArgs,
)
```

### 6. 注册工具

在 `backend/app/tools/registry.py` 的 `register_builtin()` 加入该工具：

```python
from .knowledge_search import knowledge_search

def register_builtin(self) -> None:
    for tool in (builtin_echo, builtin_get_time, builtin_calculator, knowledge_search):
        ...
```

### 7. 图节点 / State（仅形式 B 需要）

若选形式 B，修改：

- `backend/app/harness/state.py`：`HarnessState` 增加 `retrieved_context: Annotated[list, lambda a, b: a + b] = []`
- `backend/app/harness/graph_builder.py`：插入 `retrieve_node`，在 `agent_node` 之前执行检索并把 `retrieved_context` 拼入 system prompt。

`base_agent.py` 的 `build_system_prompt` 增加一个可选参数接收检索上下文：

```python
def build_system_prompt(skill_names, retrieved_context=None):
    ...
    if retrieved_context:
        bodies.append("### 知识库检索结果（引用时标注来源）：\n" + "\n".join(retrieved_context))
```

### 8. 租户隔离与审计

- 检索工具必须从运行上下文拿到当前 `tenant_id`（可从 `graph_builder` 注入 state，或经 tool 闭包绑定），确保不越权检索。
- 在检索工具内部 / `router_chat` 处追加 `audit.audit(tenant, "knowledge.retrieve", ...)` 审计。
- 指标：`metrics.record_request` 或独立计数器统计检索次数与命中数。

### 9. 前端（可选）

- `frontend/src/` 增加"知识库管理"页面（上传文档、查看已加载知识库）。
- 对话界面增加"引用来源"渲染（回答中的 `[1]` 链接到片段来源）。

---

## 五、最小实现路线图

| 步骤 | 内容 | 改动范围 |
|---|---|---|
| 1 | pgvector 扩展 + `KnowledgeChunk` 表 | `init.sql`、`models.py` |
| 2 | `knowledge/` 服务包（embedding + retriever + ingest） | 新增包 |
| 3 | `knowledge_search` 工具 + 注册 | `tools/knowledge_search.py`、`registry.py` |
| 4 | 上传/摄取 API（`router_knowledge.py`） | 新增路由 + `schemas.py` |
| 5 | （可选）RAG 图节点强化 | `state.py`、`graph_builder.py`、`base_agent.py` |
| 6 | 审计 + 指标 + 前端管理页 | `observability`、前端 |

## 六、注意事项

- **向量列维度**：`EMBEDDING_DIM` 必须与 embedding 模型输出维度一致，建 HNSW 索引前先建表。
- **多租户隔离**：所有检索 SQL 必须带 `tenant_id` 过滤，这是当前底座的安全红线。
- **数据分块策略**：摄取时按语义/固定长度分块（如 500 字符 + 重叠 50），保证检索粒度。
- **引用溯源**：工具返回片段时带 `source_file`，回答要求 LLM 标注来源，便于审计与可信度。
- **Embedding 缓存**：重复问题可缓存 query 向量，降低算力成本。
