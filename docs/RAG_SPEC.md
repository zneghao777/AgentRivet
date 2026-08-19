# RAG 知识库能力 · 需求规格说明书（SPEC）v1.0.0

> 基于 Agentic RAG 方向，采用**形式 A（检索工具 RAG-as-Tool）** 实现。
> 本 SPEC 严格依据现有项目代码结构（`docs/SPEC.md`、`backend/app/**`、`frontend/src/**`）约定撰写，开发实现不得偏离既有技术选型与编码风格。
> 前置调研参考：`docs/RAG_RESEARCH.md`、`docs/RAG_INTEGRATION.md`。

---

## 1. 需求背景与目标

### 1.1 背景

当前底座已完成 M1-M5 里程碑，具备会话管理、图编排（LangGraph ReAct）、工具注册中心、Skill 体系、多 Agent、HITL、多租户隔离、审计指标等能力。底座定位为**不绑定业务的 Agent 开发底座**，RAG 知识库能力是底座应提供的通用能力之一。

### 1.2 目标

以**检索工具（RAG-as-Tool）** 形式为底座加入知识库能力：

1. **能力底座**：支持知识库的创建、文档上传、解析分块、向量化入库、语义检索。
2. **Agent 集成**：通过 `knowledge_search` 检索工具接入既有 ReAct 循环，Agent 自主决定何时检索、检索哪个知识库。
3. **多租户隔离**：知识库数据按 `tenant_id` 严格隔离，检索 SQL 必须带租户过滤（继承底座安全红线）。
4. **前端闭环**：新增知识库管理页 + 对话侧栏知识库状态 + 工具调用可视化，风格严格遵循现有前端约定。
5. **可观测**：检索行为纳入审计日志与指标（继承 `observability/` 机制）。

### 1.3 非目标（本期不做）

- 不做 Rerank 精排、混合检索（BM25+向量）、Query 改写、GraphRAG 等增强（预留扩展点）。
- 不引入 LlamaIndex / RAGFlow / Dify 等重平台；检索用 LangChain 生态自研实现。
- 不做知识库权限分级（仅租户级隔离，用户级 ACL 后续版本）。

---

## 2. 总体架构

```
┌─────────────────────────── 前端（React + AntD + Zustand）──────────────────────────┐
│  Sidebar 新增「知识库」入口                                                           │
│  KnowledgePage（知识库管理：创建/上传/删除/文档列表）                                  │
│  ContextPanel 增加「已启用知识库」卡片                                                  │
│  MessageBubble 对 knowledge_search 工具调用做增强展示（来源引用）                     │
└──────────────┬──────────────────────────────────────────────┬───────────────────────┘
               │ REST /api/v1/knowledge-bases/*               │ SSE（对话流）
┌──────────────▼──────────────────────────────────────────────▼───────────────────────┐
│  后端 FastAPI                                                                         │
│  router_knowledge.py ──▶ KnowledgeService（摄取/检索）                                │
│        │                     └─ embedding 客户端（OpenAI 兼容）                        │
│        │                     └─ 分块器 chunker.py（标题层级 + 固定长度重叠）           │
│        ▼                                                                             │
│  ToolRegistry ──▶ knowledge_search 工具 ──▶ KnowledgeService.retrieve                 │
│        │                                                                             │
│  LangGraph 主图（graph_builder.py 不变，工具随注册自动 bind_tools）                    │
│        │                                                                             │
│  Postgres（pgvector 扩展）：knowledge_bases / knowledge_chunks 表                       │
└───────────────────────────────────────────────────────────────────────────────────────┘
```

**设计要点**

- **工具注册零侵入**：`knowledge_search` 与 `builtin_calculator` 等内置工具完全同构（`StructuredTool.from_function` + pydantic `args_schema`），仅需在 `register_builtin()` 中追加，主图 `graph_builder.py` **无需改动**。
- **检索上下文注入**：工具闭包中绑定当前请求上下文（`tenant_id`、会话绑定的知识库白名单），由路由层在调用前设置（与 `_llm`、`_tools` 注入 `state` 的方式一致）。
- **存储复用**：使用 Postgres 16 + pgvector 扩展，零新增基础设施；后续数据量超百万级再平滑迁移 Qdrant（`retriever.py` 抽象检索接口）。

---

## 3. 后端设计

### 3.1 新增依赖（`backend/requirements.txt`）

```
pgvector>=0.2
```

Embedding 复用 OpenAI 兼容协议（`langchain-openai` 已具备，新增 `ChatOpenAI` 同款 `OpenAIEmbeddings` 用法）。

### 3.2 配置项（`backend/app/config.py` 追加）

```python
# --- RAG / 向量检索 ---
EMBEDDING_MODEL: str = "text-embedding-3-small"
EMBEDDING_BASE_URL: str = ""            # 缺省复用 LLM_BASE_URL
EMBEDDING_API_KEY: str = ""             # 缺省复用 LLM_API_KEY
EMBEDDING_DIM: int = 1536
RAG_TOP_K: int = 5
RAG_SCORE_THRESHOLD: float = 0.0
RAG_CHUNK_SIZE: int = 500
RAG_CHUNK_OVERLAP: int = 50
RAG_MAX_FILE_SIZE_MB: int = 20
RAG_ALLOWED_EXTS: str = ".md,.txt,.pdf,.docx"
```

> 命名遵循现有 `LLM_*` 风格（`LLM_MODEL / LLM_BASE_URL / LLM_API_KEY` 均为大写常量），配置集中在 `config.py`，业务代码禁止散落 `os.environ`。

### 3.3 数据模型（`backend/app/storage/models.py` 追加）

```python
class KnowledgeBase(Base):
    """知识库：租户下的命名空间 + 统计信息。"""
    __tablename__ = "knowledge_bases"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    doc_count: Mapped[int] = mapped_column(default=0)
    chunk_count: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")  # active / deleted
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    __table_args__ = (Index("ix_kb_tenant_name", "tenant_id", "name", unique=True),)


class KnowledgeChunk(Base):
    """文档分块 + 向量。"""
    __tablename__ = "knowledge_chunks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    kb_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("knowledge_bases.id"), index=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_file: Mapped[str] = mapped_column(String(255))
    chunk_index: Mapped[int] = mapped_column(default=0)
    chunk_text: Mapped[str] = mapped_column(Text, nullable=False)
    # embedding 向量列：pgvector.Vector(dim)，实际维度取 EMBEDDING_DIM
    metadata: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
```

> 表/索引 DDL 由 `init.sql` 与 `create_tables()` 统一处理：`init.sql` 增加 `CREATE EXTENSION IF NOT EXISTS vector;`，`models.py` 中用 `Vector(EMBEDDING_DIM)` 声明向量列，并加 HNSW 索引：
> `CREATE INDEX ix_chunks_hnsw ON knowledge_chunks USING hnsw (embedding vector_cosine_ops);`

### 3.4 服务层（新增 `backend/app/knowledge/` 包）

```
backend/app/knowledge/
├── __init__.py
├── embedding.py    # OpenAIEmbeddings 客户端封装（复用 settings，幂等单例）
├── chunker.py      # 分块：标题层级切分 + 固定长度(500)重叠(50) 兜底
├── retriever.py    # 检索器：embed(query) + pgvector 余弦检索（tenant/kb 过滤）
├── ingest.py       # 摄取编排：解析文件 → chunker → embed → 批量入库 → 更新统计
└── service.py      # KnowledgeService 门面：面向路由与工具的统一入口
```

**`retriever.py` 核心约定（SQL 必须带租户过滤）**

```sql
SELECT chunk_text, source_file, chunk_index, 1 - (embedding <=> :qvec) AS score
FROM knowledge_chunks
WHERE tenant_id = :tenant AND kb_id = ANY(:kb_ids)
  AND 1 - (embedding <=> :qvec) >= :threshold
ORDER BY embedding <=> :qvec
LIMIT :top_k
```

> `kb_ids` 由"会话绑定的知识库白名单"决定：若白名单为空则查询该租户全部知识库；工具参数 `knowledge_base` 指定时必须同时校验该 kb 属于当前租户（防越权）。

### 3.5 检索工具（新增 `backend/app/tools/knowledge_search.py`）

严格模仿 `backend/app/tools/builtin/calculator.py` 的写法：

```python
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field


class KnowledgeSearchArgs(BaseModel):
    query: str = Field(description="检索问题（建议拆分为单个明确意图）")
    knowledge_base: str | None = Field(default=None, description="知识库名称；不指定则检索当前租户全部知识库")
    top_k: int = Field(default=5, description="返回片段数，1-10")


def _knowledge_search(query: str, knowledge_base: str | None, top_k: int = 5) -> str:
    # 1. 从当前调用上下文读取 tenant_id 与知识库白名单（ContextVar，由路由注入）
    # 2. 调用 KnowledgeService.retrieve(...)
    # 3. 返回 markdown 格式片段列表（带 [来源] source_file 标注，供 Agent 引用）
    ...


knowledge_search = StructuredTool.from_function(
    func=_knowledge_search,
    name="knowledge_search",
    description=(
        "在企业知识库中进行语义检索。当用户询问文档、制度、FAQ、内部知识，"
        "或需要基于给定文档回答时使用。返回片段会标注来源文件。"
    ),
    args_schema=KnowledgeSearchArgs,
)
```

**注册**：在 `backend/app/tools/registry.py` 的 `register_builtin()` 中追加 `knowledge_search`。

**运行时上下文**：新增 `backend/app/harness/context.py`，用 `contextvars.ContextVar` 保存 `(tenant_id, allowed_kb_ids, user_id)`；`router_chat.py` 在调用 `harness_app.runtime.ainvoke/astream` 前 `set()`，`knowledge_search` 工具内 `get()`。这与现有"工具由 `graph_builder` 注入 `_tools` 到 state"的机制互补，避免改动图结构。

### 3.6 API 设计（新增 `backend/app/api/router_knowledge.py`）

遵循现有 `router_skills.py` 风格：`ApiResponse` 包装、`require_auth` 鉴权、`audit.audit` 审计、`@router` 前缀。

| 方法 | 路径 | 说明 | 请求体 |
|---|---|---|---|
| GET | `/knowledge-bases` | 知识库列表（含 doc_count/chunk_count） | - |
| POST | `/knowledge-bases` | 创建知识库 | `{name, description?}` |
| GET | `/knowledge-bases/{kb_id}` | 知识库详情 + 文档列表 | - |
| DELETE | `/knowledge-bases/{kb_id}` | 删除知识库（级联删 chunks） | - |
| POST | `/knowledge-bases/{kb_id}/documents` | 上传文档并摄取（multipart file） | `Form` 文件 + `?replace=true/false` |
| DELETE | `/knowledge-bases/{kb_id}/documents/{doc_id}` | 删除单个文档（重建索引/删 chunk） | - |
| POST | `/knowledge-bases/{kb_id}/search` | 检索调试（测试检索效果，返回 top-k 片段） | `{query, top_k?}` |

**响应模型（`backend/app/api/schemas.py` 追加，与前端 `types/api.ts` 一一对应）**

```python
class KnowledgeBaseResponse(BaseModel):
    kb_id: str
    name: str
    description: str | None = None
    doc_count: int = 0
    chunk_count: int = 0
    status: str
    created_at: str | None = None
    updated_at: str | None = None


class KnowledgeDocResponse(BaseModel):
    doc_id: int
    source_file: str
    chunk_count: int = 0
    created_at: str | None = None


class KnowledgeSearchRequest(BaseModel):
    query: str = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=10)


class KnowledgeHit(BaseModel):
    text: str
    source_file: str
    chunk_index: int
    score: float
```

### 3.7 对话链路集成

- `MessageSendRequest` / `MessageStreamRequest` 增加可选字段 `knowledge_bases: list[str] | None`（默认不启用 RAG）。
- `router_chat.py`：读取 `body.knowledge_bases` → 解析为 kb_id 白名单 → `context.set(...)` 注入 → 调 `ainvoke/astream`。
- `agent_node`（`base_agent.py`）**不改动**：工具已注册进 `_tools`，LLM 自然 `bind_tools` 后按需调用 `knowledge_search`。
- SSE 事件沿用 `tool_start` / `tool_end`（`events.py` 已有），前端据此渲染工具调用卡片；`tool_end.result` 即检索片段 markdown。

### 3.8 多租户与安全（继承红线）

- 所有 `knowledge_bases` / `knowledge_chunks` 查询必须带 `tenant_id` 条件。
- 上传/删除/检索接口均校验 `kb.tenant_id == auth.tenant.tenant_id`，不匹配返回 404（不泄露存在性）。
- 文件上传校验：扩展名白名单（`RAG_ALLOWED_EXTS`）、大小上限（`RAG_MAX_FILE_SIZE_MB`）、无恶意内容扫描（本期仅格式校验）。
- `knowledge_search` 工具内同样执行租户校验，防 Agent 越权检索他租户知识。

### 3.9 可观测

- 审计：`knowledge.upload` / `knowledge.delete` / `knowledge.search`（含 kb 名、文件名、命中数）。
- 指标：`metrics.record_request` 沿用；新增检索延迟/命中数维度可在 `observability/metrics.py` 追加计数器（本期最小实现）。
- LangSmith：检索工具调用自然进入图执行轨迹，无需额外接入。

---

## 4. 前端设计（严格遵循现有风格）

### 4.1 总体约定（继承现有前端规范）

- **技术栈**：React 18 + Vite + TS + Ant Design 5 + Zustand；样式变量全部复用 `styles/global.css` 的 `--brand-*` / `--gray-*` / `--r-*` / `--shadow-*` / `--dur-*`。
- **图标**：统一 `material-symbols-rounded`。
- **API 调用**：统一走 `@/api/http` 的 `getData / postData / deleteData`，类型定义在 `@/types/api`。
- **页面骨架**：所有页面对齐 `SkillsPage.tsx` / `SettingsPage.tsx` —— 外层 `div`（`flex:1; minWidth:0; height:100%; overflowY:auto; padding:"28px 32px 60px"`）+ 渐变 header 卡片（`linear-gradient(135deg, rgba(0,80,203,0.06), rgba(0,184,169,0.04))` + `var(--r-2xl)` + 44px 品牌图标块 + 标题/副标题 + 右侧操作按钮）。
- **卡片风格**：`background: var(--bg-card); borderRadius: var(--r-xl); border: var(--border-soft); boxShadow: var(--shadow-sm)`。
- **页面路由**：`App.tsx` 在 `AppShell` 下新增 `<Route path="/knowledge" element={<KnowledgePage />} />`。

### 4.2 导航入口（`components/layout/Sidebar.tsx`）

在 `FEATURES` 数组追加一项：

```tsx
{ label: "知识库", icon: "database", path: "/knowledge" },
```

### 4.3 知识库管理页（新增 `pages/KnowledgePage.tsx`）

**布局与 SkillsPage 一致**：

- **header 卡片**：icon `database`、标题「知识库」、副标题「上传文档构建企业知识，让 Agent 基于知识回答」、右侧「刷新」按钮。
- **知识库列表**：网格卡片（复用 SkillsPage 的 `grid` 布局与卡片样式）：
  - 卡片头：图标块 + 知识库名 + 启用状态点 + 操作（上传/删除）。
  - 卡片体：description、统计行（`doc_count` 文档 / `chunk_count` 分块）。
- **创建知识库**：header 或卡片区「新建知识库」按钮 → AntD `Modal` + `Form`（name、description）。
- **上传文档**：AntD `Upload`（`accept=".md,.txt,.pdf,.docx"`、`beforeUpload` 校验大小），上传成功后 `load()` 刷新统计并 `message.success`；摄取中 `loading` 态。
- **空状态**：`Empty` 组件（对齐 SkillsPage `Empty description="暂无知识库"`）。

### 4.4 对话页集成

- **`components/layout/ContextPanel.tsx`**：在「已加载技能」下方新增「知识库」卡片：
  - 数据源 `GET /knowledge-bases`（仅 active）。
  - 展示每个知识库名称 + chunk 数；底部「管理知识库」按钮 `navigate("/knowledge")`（对齐现有「管理技能」按钮样式）。
- **`ChatWindow.tsx` / 消息发送**：发送栏保留 skills 选择能力的位置，新增知识库多选（AntD `Select mode="multiple"`，选项来自 `/knowledge-bases`），随 `MessageSendRequest.knowledge_bases` 提交；无选择时不上送该字段（默认不启用 RAG）。
- **`components/chat/MessageBubble.tsx` 工具调用增强**：当 `tool.name === "knowledge_search"` 时，折叠面板展示检索片段摘要与来源（对齐现有 `ToolCall` 折叠交互，icon 用 `database`，结果区渲染 `source_file` 列表），其余工具沿用现有 `build_circle` 样式。

### 4.5 类型与状态扩展

- **`types/api.ts` 追加**（与 `schemas.py` 一一对应）：

```ts
export interface KnowledgeBaseItem {
  kb_id: string;
  name: string;
  description: string | null;
  doc_count: number;
  chunk_count: number;
  status: string;
  created_at: string | null;
  updated_at: string | null;
}

export interface KnowledgeDocItem {
  doc_id: number;
  source_file: string;
  chunk_count: number;
  created_at: string | null;
}

export interface KnowledgeSearchRequest {
  query: string;
  top_k?: number;
}

export interface KnowledgeHit {
  text: string;
  source_file: string;
  chunk_index: number;
  score: number;
}
```

- **`types/sse.ts`**：无需新增事件类型（复用 `ToolStart / ToolEnd`）。
- **`stores/sessionStore.ts`**：`ChatMessage` 不变；`ContextPanel` / 发送栏的知识库数据用页面局部 state 管理（与 `SkillsPage` 一致），不新增全局 store。

### 4.6 样式

- 新增样式一律写入 `styles/global.css`，命名沿用现有 kebab-case（如 `.kb-card`、`.kb-stat`、`.kb-upload`），复用现有变量，禁止硬编码色值。
- 工具调用折叠面板样式复用现有 `.tool-call`，仅新增 `knowledge_search` 的 source 列表样式。

---

## 5. 数据流与交互时序

### 5.1 文档摄取时序

```
前端 选择文件 → POST /knowledge-bases/{kb_id}/documents (multipart)
  → router: 鉴权 + 租户校验 + 文件格式/大小校验
  → ingest.ingest_document():
      解析文本(按扩展名) → chunker 分块 → embedding.embed(chunks)
      → 批量 INSERT knowledge_chunks → 更新 knowledge_bases 统计
  → audit.audit("knowledge.upload") → ApiResponse(doc_id, chunk_count)
```

### 5.2 对话检索时序

```
前端 发送消息(message + knowledge_bases)
  → router_chat: context.set(tenant_id, kb_ids) → harness astream
  → agent_node: LLM 判断需要知识 → tool_call knowledge_search(query, top_k)
  → tools_node 执行: retriever.retrieve(tenant, kb_ids, query)
      embed(query) → pgvector 余弦 top-k → 拼 markdown 片段(带来源)
  → SSE tool_start/tool_end → 前端渲染折叠卡片
  → agent_node: 基于片段生成回答(标注 [来源]) → message_delta → run_end
```

---

## 6. 分块与摄取策略

- **解析**：`.md/.txt` 直接读文本；`.pdf` 用 `pypdf`（纯文本提取，本期不做版面分析）；`.docx` 用 `python-docx` 提取段落。复杂版面/OCR 不在本期范围（预留 `RAGFLOW_DEEPDOC` 扩展点）。
- **分块**：优先按 Markdown 标题层级切分（`# / ## / ###`），每块控制在 `RAG_CHUNK_SIZE`(500) 内；超长段再按固定长度 + `RAG_CHUNK_OVERLAP`(50) 重叠切分；表格内容独立成块。
- **元数据**：chunk 的 `metadata` 记录 `{source_file, chunk_index, heading}`，供引用溯源与审计。
- **重复上传**：`replace=true`（默认）先删除该文件旧 chunks 再写入，保证幂等。

---

## 7. 验收标准（MVP）

- [ ] `docker compose up` 一键启动，Postgres 自动启用 pgvector 扩展。
- [ ] 知识库 CRUD：创建/列表/详情/删除，数据按租户隔离（跨租户访问返回 404）。
- [ ] 文档上传：支持 `.md/.txt`（及 `.pdf/.docx` 若依赖就绪），上传后 doc_count/chunk_count 正确更新。
- [ ] 检索工具：对话中 Agent 能自主调用 `knowledge_search`，回答引用知识片段并标注来源。
- [ ] SSE：`tool_start`/`tool_end` 正常下发，前端折叠卡片展示检索参数与结果。
- [ ] 前端：知识库页 + 侧栏入口 + ContextPanel 知识库卡片 + 发送栏知识库多选，风格与现有页面一致（复用 CSS 变量、无硬编码色值）。
- [ ] 审计：upload / delete / search 均有 audit 记录。
- [ ] 测试：`backend/tests/` 新增 knowledge 相关单测（摄取、检索、租户隔离、工具调用），`pytest -v` 全绿。

## 8. 里程碑

| 里程碑 | 内容 | 交付物 |
|---|---|---|
| R1 | 基础设施：pgvector + 模型 + 配置 + 服务层（embedding/chunker/retriever/ingest） | 后端可入库可检索 |
| R2 | 检索工具接入 ReAct + API 路由 + 审计 | 对话可基于知识回答 |
| R3 | 前端：知识库页 + 侧栏 + ContextPanel + 发送栏多选 + 工具卡片增强 | 前后端闭环 |
| R4 | 测试 + 文档 + Docker 联调 | 验收通过 |

## 9. 风险与注意事项

- **向量维度一致性**：`EMBEDDING_DIM` 必须与 embedding 模型输出一致，不一致会导致入库/检索报错。
- **检索质量依赖分块**：标题层级分块质量直接影响召回，R2 后用 `/search` 调试接口人工验证。
- **租户隔离是安全红线**：任何新 SQL 都必须带 `tenant_id`，Code Review 重点检查。
- **上传并发**：摄取为同步阻塞（本期），大文件建议后续改任务队列；`doc_count` 统计在事务内更新。
- **模型成本**：Embedding API 计费，建议对重复 query 做向量缓存（本期不做，预留）。
- **不引入新框架**：检索全部基于 LangChain 生态 + pgvector 原生 SQL，保持底座"能力可插拔"。
