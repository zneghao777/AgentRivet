# 知识库管理体系完善 · 技术规格书（KB Management Spec）

- 版本：v2.0.0
- 日期：2026-08-19
- 关联：`docs/RAG_SPEC.md`（v1.0.0，本规格在其基础上演进）
- 状态：**待评审**（K1–K4 全部实施后再评审闭环）

---

## 0. 目标与范围

在现有 RAG 能力（`docs/RAG_SPEC.md` v1.0.0：`knowledge_search` 检索工具 + pgvector 余弦检索 + 租户隔离）基础上，将知识库从"轻量 CRUD"升级为**主流产品级知识库管理体系**，补齐三条关键链路：

1. **文档智能解析**：用户自主上传 → 优先调用 **GLM 文件解析模型**（异步解析 PDF/DOCX/…）提取高质量文本，无 key 时回退到本地解析器。
2. **分块治理**：解析结果 → 自动分块 → **分块预览 / 手动编辑 / 启用停用 / 重新向量化**，让用户对"喂给 Agent 的知识"完全可控。
3. **向量引擎切换**：embedding 从 OpenAI 缺省切换为 **本地 BGE-M3**（OpenAI 兼容，1024 维），无需外网、数据不出内网。

交付物：一份完整的知识库体系页面（参考 beautiful-ui 组件的动画 / 交互 / 质感，并复用本项目 CSS 变量），后端对应 API + 全链路测试，最终 **RAG 流程端到端跑通**（上传→解析→分块→向量化→检索→Agent 引用）。

本规格在 v1.0.0 基础上额外强化两大能力（用户补充要求）：

- **RAG 回复追踪溯源（Traceability）**：Agent 的回答必须可追溯到具体知识片段，并能**一键定位回知识库管理页的对应分块**（编辑/核对），形成「答案 → 来源片段 → 原文档分块」的闭环。
- **页面 UI 交互与适配（UX & Responsive）**：交互细节打磨（乐观更新、骨架屏、状态轮询、错误回滚、空态/加载态）+ **响应式适配**（桌面/平板/窄屏三档布局，兼顾 beqautiful-ui 动画质感）。

---

## 1. 技术选型与外部依赖（已实地验证）

### 1.1 GLM 文件解析模型（异步）

**官网**：https://docs.bigmodel.cn/cn/guide/tools/file-parser

采用智谱 **文件异步解析 API**，支持 PDF / DOCX / DOC / XLS / XLSX / PPT / TXT / MD / CSV / 图片。

**认证**：请求头 `Authorization: Bearer <GLM_API_KEY>`（用户持有 key）。

**流程**（异步二段式）：

1. 创建解析任务：

   ```
   POST https://open.bigmodel.cn/api/paas/v4/files/parser/create
   Content-Type: application/json
   Authorization: Bearer <key>
   {
     "model": "glm-files-parser",
     "tool_type": "expert",      // lite=免费精简 | expert=标准 | prime=旗舰
     "file_url": "https://.../xxx.pdf"
   }
   ```
   响应：`{ "id": "182151...", "status": "SUCCESS" }`（status 为 `SUCCESS` 表示任务已受理）。

2. 轮询任务状态：

   ```
   POST https://open.bigmodel.cn/api/paas/v4/files/parser/status
   Authorization: Bearer <key>
   { "id": "182151..." }
   ```
   响应：`{ "status": "SUCCESS"|"FAILED"|"IN_PROGRESS", "content": "<解析出的纯文本>", "md_results": "<Markdown>" }`

> **补充（同步）**：对纯图片或单页 PDF 也可用同步接口 `POST /api/paas/v4/layout_parsing`（multipart 或 JSON，返回 `md_results`），用于"图片/扫描件 OCR"。本规格优先使用异步文件解析；同步仅作为 `expert` 解析失败时的降级选项（可选实现）。

**关键约束**：
- 文件需可公网/内网可达的 URL（`file_url`）。后端在**上传时先把文件存入本地存储**并生成临时访问 URL（见 §3.3），再交给 GLM。
- `content`（纯文本）与 `md_results`（Markdown）二选一入库。分块器对 Markdown 质量更好（标题层级），**优先存 `md_results`，缺失时回退 `content`**。

### 1.2 BGE-M3 本地向量模型（OpenAI 兼容）

- **服务地址**：`http://192.168.31.185:1234`（用户已本地部署，已验证在线）。
- **兼容接口**：`POST /v1/embeddings`，请求体 `{ "model": "text-embedding-bge-m3", "input": [...] }`。
- **模型 ID**：`text-embedding-bge-m3`（已验证可调用）。
- **输出维度**：**1024**（实测，替换原 `EMBEDDING_DIM=1536`）。
- **接入方式**：与现 `OpenAIEmbeddings` 完全兼容（`base_url` 指向本地服务、`api_key` 随意占位即可，本服务无鉴权）。

### 1.3 前端组件（beautiful-ui skill）

复用（**参考其动画 / 质感 / 结构，用本项目 CSS 变量适配，不整文件照抄**）：
- `context-cards` → 检索命中/分块来源卡片（渐变描边、hover 抬升、来源徽标）。
- `task-rows` → 文档解析任务列表（状态点、进度动效、失败重试）。
- `records-table` → 分块列表（hover 高亮、操作气泡）。
- `loading-state` / `skeleton` / `spinner` → 异步加载骨架屏。
- `search` → 知识库内搜索/分块过滤。
- `sidebar-nav` → 二级导航（知识库 → 文档 → 分块）。
- `prompt-bar` → "测试检索"输入框。

本项目规范：CSS 变量（`--brand-*`, `--gray-*`, `--shadow-*`, `--r-*`, `--dur-base` 等）+ `material-symbols-rounded` 图标 + antd 组件（`Select/Modal/Tag/Upload` 等）。所有自定义样式写进 `frontend/src/styles/global.css`，避免内联魔法值。

---

## 2. 总体架构

```
┌───────────────────────────── 前端（React/TS）────────────────────────────┐
│  KnowledgeCenterPage（知识库体系总览）                                     │
│   ├─ KnowledgeBaseList（知识库卡片网格，beautiful-ui context-cards 质感） │
│   ├─ KnowledgeDetail（单库详情：文档列表 + 分块治理）                      │
│   │   ├─ DocumentTaskRows（文档/解析任务，task-rows 动效）                │
│   │   ├─ ChunkRecordsTable（分块列表，records-table，可编辑/启停/重算）    │
│   │   └─ ChunkEditModal（分块内容编辑，重新向量化）                       │
│   └─ TestRetrievePanel（测试检索，prompt-bar + 命中来源卡片）              │
└──────────────┬──────────────────────────────────────────────────────────┘
               │ REST（多租户鉴权）
┌──────────────▼──────────────────────────────────────────────────────────┐
│  backend/app/api/router_knowledge.py（扩展现有）                          │
│   + /documents/upload（改为：存储原文件 + 触发解析）                       │
│   + /documents/{doc_id}/reparse、/chunks/*（CRUD/启停/重新向量化）         │
│   + /test-search（跨库检索调试）                                          │
├──────────────────────────────────────────────────────────────────────────┤
│  knowledge 服务层                                                         │
│   ├─ parser/glm_parser.py      （GLM 异步文件解析封装）                    │
│   ├─ parser/local_parser.py    （pypdf/python-docx 等本地解析降级）         │
│   ├─ chunker.py                （复用，分块预览不落库）                    │
│   ├─ embedder.py               （BGE-M3 OpenAI 兼容封装，1024 维）         │
│   ├─ ingest.py                 （解析→分块→向量化→入库，支持增量）          │
│   └─ service.py                （门面：任务状态机 / 分块治理 / 测试检索）    │
├──────────────────────────────────────────────────────────────────────────┤
│  storage/（Postgres + pgvector）                                           │
│   新增表：document_meta（原文件元数据 + 解析状态）、chunk 改造（enabled）    │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 3. 后端设计

### 3.1 数据模型（`models.py` 扩展）

现有 `KnowledgeBase`、`KnowledgeChunk` 保留；新增/调整：

**新增 `DocumentMeta`（原文件/解析任务元数据）：**

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | int PK | 文档 id（现 `KnowledgeChunk` 按 `source_file` 分组，改为独立实体） |
| `kb_id` | UUID FK | 所属知识库 |
| `tenant_id` | str | 租户隔离 |
| `source_file` | str | 原始文件名 |
| `file_path` | str | 本地存储路径（`data/uploads/{tenant}/{kb}/{uuid}.ext`） |
| `file_size` | int | 字节 |
| `mime_type` | str | MIME |
| `parse_status` | str | `pending` → `parsing` → `parsed` / `failed` / `reparsing` |
| `parse_source` | str | `glm` / `local`（解析来源） |
| `raw_text` | text | 解析出的原始文本（便于分块预览/重算） |
| `parser_tool` | str | GLM tool_type（`lite/expert/prime`） |
| `glm_task_id` | str | GLM 异步任务 id（用于轮询） |
| `error` | text | 解析失败原因 |
| `created_at / updated_at` | datetime | 时间戳 |

**改造 `KnowledgeChunk`：**
- 新增 `doc_id`（FK → `document_meta.id`），替代/兼容现有按 `source_file` 分组逻辑。
- 新增 `enabled: bool`（默认 true），检索 `WHERE enabled`。
- 新增 `updated_at`（手动编辑后刷新，触发重算时更新）。
- 现有 `embedding`（Vector 1024 维）保留；`RAG_SPEC` 建表时用 `EMBEDDING_DIM`，此处改为 1024。

**索引**：`knowledge_chunks` 上 `ix_chunks_hnsw`（embedding 余弦）沿用；新增 `(kb_id, doc_id)`、`(kb_id, enabled)` 复合索引。

> **迁移说明**：本项目为开发底座，`create_tables()` 建表。字段变更通过修改模型 + `create_tables` 重建（开发期可接受；生产建议迁移脚本，见 §8 风险）。

### 3.2 配置（`config.py` 扩展）

```
# GLM 文件解析
GLM_FILES_API_KEY: str = ""            # 智谱 key，空则回退本地解析
GLM_FILES_BASE_URL: str = "https://open.bigmodel.cn/api/paas/v4"
GLM_FILES_MODEL: str = "glm-files-parser"
GLM_FILES_TOOL: str = "expert"         # lite|expert|prime
GLM_FILES_POLL_SECONDS: int = 3        # 异步轮询间隔
GLM_FILES_TIMEOUT_SECONDS: int = 180   # 单文件解析超时

# 向量引擎（BGE-M3 本地）
EMBEDDING_BASE_URL: str = "http://192.168.31.185:1234/v1"   # 覆盖原有
EMBEDDING_MODEL: str = "text-embedding-bge-m3"
EMBEDDING_API_KEY: str = "local-bge-m3"   # 无鉴权占位
EMBEDDING_DIM: int = 1024                  # BGE-M3 输出维度

# 上传 / 解析
RAG_ALLOWED_EXTS: str = ".md,.txt,.pdf,.docx"
RAG_MAX_FILE_SIZE_MB: int = 50
KB_UPLOAD_DIR: str = "data/uploads"        # 原文件落盘目录
KB_PUBLIC_BASE_URL: str = ""               # 文件对外下载地址前缀（GLM 需 URL）
```

### 3.3 文件上传与存储

1. `POST /api/v1/knowledge-bases/{kb_id}/documents/upload`（multipart `file`）
   - 校验扩展名 / 大小 → 落盘到 `KB_UPLOAD_DIR/{tenant}/{kb_id}/{uuid}.{ext}`。
   - 写入 `DocumentMeta`，`parse_status=pending`。
   - **返回**：`{ doc_id, source_file, parse_status }`，解析异步进行。
   - 审计 `knowledge.upload`。

2. **解析任务分发**（后台异步，`asyncio.create_task` 后台 worker，用 `asyncio.Semaphore` 限并发）：
   - `parse_status=pending|failed → parsing`。
   - 有 `GLM_FILES_API_KEY` 且文件类型受支持 → **GLM 异步解析**：
     a. 生成本地文件临时访问 URL（`KB_PUBLIC_BASE_URL` + 相对路径；开发环境可用本地 HTTP 静态目录或内网可达地址）。
     b. `create` → 得 `glm_task_id` → 轮询 `status`（`GLM_FILES_POLL_SECONDS` 间隔，超时 `GLM_FILES_TIMEOUT_SECONDS`）。
     c. 成功 → `raw_text = md_results or content`，`parse_source="glm"`。
   - **回退/无 key** → **本地解析器**：`.pdf` 用 pypdf、`.docx` 用 python-docx、`.md/.txt` 直接读文本；`parse_source="local"`。
   - 成功 → `parse_status=parsed` → **触发分块 + 向量化入库**。
   - 失败 → `parse_status=failed` + `error`，允许重新解析。

3. **重新解析**：`POST /documents/{doc_id}/reparse` → 重跑第 2 步（先清旧 chunk）。

### 3.4 分块与向量化（`chunker.py` / `embedder.py` / `ingest.py`）

- **分块**：复用现有标题层级分块逻辑；新增能力：
  - `preview_chunks(text, chunk_size)`：纯函数，返回 `[{index, heading, text, char_count}]` 供**前端分块预览（不落库）**。
  - 支持 `chunk_size` 可配置（默认 500，窗口可选 256/500/1000）。
- **向量化**：`embedder.py` 封装 BGE-M3：
  - `embed_texts(texts)` / `embed_query(query)`，`OpenAIEmbeddings(model="text-embedding-bge-m3", base_url="http://192.168.31.185:1234/v1", api_key="local-bge-m3")`。
  - 校验输出维度 == `EMBEDDING_DIM(1024)`，不符抛 `EmbeddingError`。
- **入库**：解析完成 → 分块 → 逐块 `embed_texts` → 写 `KnowledgeChunk`（含 `doc_id`）。同文档重算时**先删旧 chunk 再写新**（保持幂等）。
- **局部重算**：编辑某个 chunk 文本 → 仅重算该 chunk 的 embedding（`PUT /chunks/{chunk_id}`）。

### 3.5 分块治理 API

```
GET    /kb/{kb_id}/documents                    文档列表（含 parse_status/进度）
POST   /kb/{kb_id}/documents/upload             上传 + 触发异步解析
POST   /kb/{kb_id}/documents/{doc_id}/reparse   重新解析
DELETE /kb/{kb_id}/documents/{doc_id}           删除文档（连同分块/向量）

GET    /kb/{kb_id}/chunks?doc_id=&q=&enabled=   分块列表（预览文本，分页，关键词过滤）
POST   /kb/{kb_id}/documents/{doc_id}/preview   分块预览：{text, chunk_size} → [{index,heading,text}]
PUT    /kb/{kb_id}/chunks/{chunk_id}            {text, enabled} 手动编辑 + 重新向量化
PATCH  /kb/{kb_id}/chunks/{chunk_id}/enabled    {enabled} 启用/停用该分块（不重算）
POST   /kb/{kb_id}/chunks/{chunk_id}/re-embed   重新向量化该分块

POST   /kb/{kb_id}/test-search                  {query, top_k} 检索调试（返回命中+来源）
```

**安全红线（沿用）**：所有操作先 `_require_kb` 校验 `kb.tenant_id == auth.tenant`，越权 404；分块级同样校验其所属 `kb` 与 `tenant_id`。

### 3.6 RAG 流程整合（含追踪溯源）

- `knowledge_search` 工具：检索 SQL 增加 `AND enabled = true` 过滤；其余不变（租户白名单、top_k、来源标注）。
- **溯源增强**：`retriever.retrieve` 返回的 `Hit` 扩展为携带 `kb_id` + `chunk_id`（稳定主键），不再只有 `source_file`。工具拼装的 markdown 增加**结构化引用锚点**（见 §3.6.1），使每条命中都可定位。
- `ContextPanel` / 发送栏知识库多选逻辑不变。
- 检索命中展示：前端来源引用增强（命中文件 + 分块摘要 + **「在知识库中定位」深链**）。

#### 3.6.1 溯源数据与锚点格式

`Hit` 数据结构扩展：

```python
@dataclass
class Hit:
    text: str
    source_file: str
    chunk_index: int
    score: float
    kb_id: uuid.UUID        # 新增：知识库 id（深链用）
    chunk_id: int           # 新增：分块稳定主键（定位/编辑用）
```

检索 SQL 同步 `SELECT kb_id, id, chunk_text, source_file, chunk_index, score`，并按 `enabled = true` 过滤。

`hits_to_markdown` 输出带机器可读锚点的 markdown（`[KB-{kb_id}/{chunk_id}]`）：

```markdown
[片段 1 | 来源: manual.pdf | 相关度 0.87 | 引用: KB-6f0e…/12]
<片段文本>
```

**溯源闭环（答案 → 来源 → 管理页分块）**：
1. 前端渲染 `knowledge_search` 工具调用时，解析 markdown 中的 `引用: KB-{kb_id}/{chunk_id}` 锚点，渲染为可点击的「来源」徽标（beautiful-ui context-cards 质感）。
2. 点击 → 跳转 `/knowledge/{kb_id}?tab=chunks&chunk={chunk_id}&doc={doc_id}`，进入该知识库**分块管理**页并自动滚动高亮到对应分块行（`scrollIntoView` + 高亮描边动效）。
3. 该分块行直接展示 `heading`、来源、文本，用户可一键「编辑」核对或修正 —— 实现答案可追溯、可纠错。

> **安全红线**：锚点中的 `kb_id` 属于当前租户（检索已强过滤）；前端跳转时路由层仍会经 `_require_kb` 二次校验，越权访问返回 404，杜绝通过 URL 猜测他人知识库。

### 3.7 审计与指标

- 新增审计 action：`knowledge.upload`、`knowledge.parse`（success/fail + 来源）、`knowledge.reparse`、`knowledge.chunk.edit`、`knowledge.chunk.toggle`、`knowledge.chunk.reembed`、`knowledge.test_search`。
- 指标：`kb_parse_duration_seconds`、`kb_parse_total{source,status}`、`kb_embed_requests_total`。

---

## 4. 前端设计

### 4.1 路由与页面

```
/knowledge                             知识库体系总览（KnowledgeCenterPage）
/knowledge/:kbId                       知识库详情（文档 + 分块治理 + 测试检索）
/knowledge/:kbId?tab=chunks&chunk={id} 溯源深链：定位到指定分块并高亮
```

现有 `KnowledgePage`（卡片列表）升级为 `KnowledgeCenterPage`，点击卡片进入详情；详情页为本次重点。路由支持 `tab` / `chunk` / `doc` 查询参数（溯源深链，见 §4.6）。

### 4.2 KnowledgeCenterPage（总览，beautiful-ui context-cards 质感）

- 顶部品牌 Header（沿用现有渐变横幅）：标题「知识库」+ 副文案 +「新建知识库」+「刷新」。
- 知识库卡片网格：每张卡片展示
  - 渐变图标 + 名称 + 启用/停用 `Tag` + 启用 `Switch`；
  - 描述；
  - 统计（文档数 / 分块数 / 解析中任务数）+ 最近更新时间；
  - 操作：上传、详情、删除；
  - hover 抬升 + 阴影过渡（参考 context-cards）。
- 空状态（`Empty`）+ 加载骨架屏（`skeleton`）。

### 4.3 KnowledgeDetail（详情，本次核心页面）

**布局**：左侧二级导航（`sidebar-nav` 质感）：知识库信息 → 文档管理 → 分块管理 → 测试检索；右侧内容区。

**A. 文档管理（DocumentTaskRows）**
- 上传区：`Upload` 拖拽 + 选择，扩展名/大小校验；上传后立即进入任务列表。
- 文档/解析任务列表（task-rows 质感）：每行 = 图标 + 文件名 + 大小 + **解析状态徽标**：
  - `pending/parsing`：转圈 spinner + 「解析中…」+ 进度动效（向量化进行中）；
  - `parsed`：绿点「已就绪」+ 分块数；
  - `failed`：红点「解析失败」+ 错误 tooltip +「重试」。
- 行操作：分块预览（进入分块 tab 并过滤该文档）、重新解析、删除。
- 解析状态轮询（前端 `setInterval` 拉取任务列表，`parse_status=parsing` 时 3s 刷新，就绪后停止）。

**B. 分块管理（ChunkRecordsTable）**
- 顶部：文档过滤 `Select`（全部/某文档）、关键词搜索、分块总数。
- 分块列表（records-table 质感）：hover 高亮；每行 =
  - 序号 + `heading` 徽标 + 文本摘要（超出折叠，可展开预览全文）；
  - 字符数 + 来源文件；
  - 启用 `Switch` + 「编辑」「重新向量化」操作。
- **分块编辑 Modal（ChunkEditModal）**：
  - 大文本域可编辑 `text`；
  - 展示当前 chunk 元信息（来源、序号、字符数）；
  - 保存 → `PUT /chunks/{id}`（自动重新向量化，按钮带 loading 转圈）；
  - 底部提示「保存后将重新生成该分块向量」。

**C. 测试检索（TestRetrievePanel，prompt-bar + context-cards）**
- 顶部输入框 + 「检索」按钮（支持 Enter），选择 top_k；
- 下方命中结果卡片列表（context-cards 质感）：
  - 来源文件徽标 + 相似度 score + 分块序号；
  - 命中文本（高亮关键词）+ hover 抬升；
  - 点击「在分块中定位」跳转到分块 tab 对应行。

### 4.4 交互细节（beautiful-ui 动画借鉴）

- 卡片/列表行：`transition: transform/border/shadow`，hover 轻微上浮（2-4px）+ 品牌色描边。
- 解析中：spinner 旋转 + 渐变进度条脉冲动画。
- 编辑保存：按钮 loading 旋转 + 成功 `message.success` 淡入。
- 启用/停用 Switch：即时乐观更新 + 失败回滚。
- 分块展开：高度过渡 + 内容淡入（`@keyframes fade-in`）。

### 4.6 RAG 回复追踪溯源（Traceability）

**目标**：让「Agent 答案 → 依据来源 → 管理页分块」形成可点击、可纠错的闭环（对接 §3.6.1 的引用锚点）。

**A. 聊天页来源引用（`MessageBubble` 增强）**
- 解析 `knowledge_search` 工具结果 markdown 中的 `引用: KB-{kb_id}/{chunk_id}` 锚点（正则提取），渲染为**可点击的来源徽标**（context-cards 质感）：`来源 · {source_file} · {score}` + 定位图标。
- 点击徽标 → 跳转溯源深链 `/knowledge/{kb_id}?tab=chunks&chunk={chunk_id}&doc={doc_id}`。
- 无锚点时（旧数据）退化为普通文本来源标注，不报错。

**B. 溯源深链落地（`KnowledgeDetail` 分块页）**
- 路由读取 `tab` / `chunk` / `doc` 参数：
  - `tab=chunks` 自动切换到「分块管理」；
  - `doc` 预选对应文档过滤；
  - `chunk` 定位：分块列表加载后 `scrollIntoView({behavior:'smooth'})` + 目标行加**品牌色高亮描边**（2-3s 后淡出）。
- 定位行直接呈现 `heading` / 来源 / 文本，并提供「编辑」按钮 —— 用户可当场核对或修正该分块（修正后自动重算向量，溯源始终指向最新内容）。

**C. 溯源链路标识（前端状态）**
- 路由参数 `chunk` 存在时，高亮对应行并短暂显示提示条「已定位到来源分块 #id」。

### 4.7 响应式适配与交互可用性

本项目采用「桌面优先 + 自适应降级」，遵循现有 CSS 变量体系，**三档断点**：

| 断点 | 宽度 | 布局策略 |
|---|---|---|
| 桌面 | `>= 1200px` | 全功能：左侧二级导航 + 右侧内容区并排；卡片网格 3-4 列 |
| 平板 | `768px ~ 1199px` | 二级导航折叠为顶部 `Tabs`/面包屑；卡片网格 2 列；分块表格操作收敛为图标按钮 |
| 窄屏（手机） | `< 768px` | 单列卡片；测试检索输入框全宽；分块列表隐藏次要列（字符数/来源，点展开查看）；`Modal` 全屏化 |

**适配细则**
- 容器均用 `min-width:0` + `flex` 布局，禁止横向溢出；长文件名/文本用 `overflow-wrap` + 多行省略。
- 上传区、检索输入框、Modal 宽度使用 `clamp()`/百分比，随视口收缩。
- 触控优先：操作按钮最小触达尺寸 ≥ 40px；hover 效果在窄屏降级为 `@media (hover:hover)` 才启用。
- 骨架屏、空态、错误态在窄屏同样完整呈现，不影响功能可用性。
- 动效遵循 `prefers-reduced-motion`：用户开启「减少动态效果」时停用滚动高亮 / 脉冲动画。

### 4.8 类型与 API 客户端

`types/api.ts` 新增（含溯源所需 `kb_id` / `chunk_id`）：
```ts
interface DocumentMetaItem { doc_id; kb_id; source_file; file_size; mime_type;
  parse_status; parse_source; chunk_count; error?; created_at; updated_at }
interface KnowledgeChunkItem { chunk_id; kb_id; doc_id; source_file; heading?;
  text; char_count; enabled; score?; chunk_index; updated_at }
interface ChunkPreviewItem { index; heading?; text; char_count }
interface RetrieveHit { chunk_id; kb_id; source_file; text; score; chunk_index }
```
`api/http.ts` 增补对应请求方法（沿用 `http.get/post/put/patch/delete` + `getData` 解包）。溯源锚点解析工具函数（`utils/trace.ts`）：`parseReferenceAnchors(markdown): {kbId, chunkId, sourceFile, score}[]`。

---

## 5. 后端实现文件清单

```
backend/app/knowledge/
  parser/__init__.py
  parser/glm_parser.py      GLM 异步文件解析（create/status 轮询）
  parser/local_parser.py    本地解析降级（pypdf/docx/txt/md）
  embedder.py               BGE-M3 OpenAI 兼容封装（1024 维）
  chunker.py                分块（新增 preview_chunks / chunk_size 参数）
  ingest.py                 解析→分块→向量化→入库（支持 doc_id / 增量重算）
  service.py                门面：文档任务、分块治理、测试检索
backend/app/storage/
  models.py                 新增 DocumentMeta；KnowledgeChunk 加 doc_id/enabled/updated_at
  repositories.py           扩展文档/分块仓库方法
backend/app/api/
  router_knowledge.py       扩展：upload/reparse/chunks/test-search
  schemas.py                新增 DocumentMetaResponse/ChunkResponse/PreviewRequest 等
backend/app/harness/context.py   （不变，工具上下文沿用）
backend/app/tools/knowledge_search.py  （加 enabled 过滤 + 溯源锚点 markdown）
backend/app/config.py        新增 GLM_/KB_/EMBEDDING_ 配置
backend/deploy/...           init.sql 增加新表/字段（如需）

frontend/src/
  pages/KnowledgeCenterPage.tsx   知识库总览（升级自 KnowledgePage）
  pages/KnowledgeDetailPage.tsx   知识库详情（文档/分块/测试检索 + 溯源深链）
  components/knowledge/...        DocumentTaskRows / ChunkRecordsTable / ChunkEditModal /
                                   TestRetrievePanel / SourceReferenceBadge
  utils/trace.ts                  溯源锚点解析 + 深链构建
  types/api.ts                    DocumentMetaItem / KnowledgeChunkItem / RetrieveHit
  styles/global.css               响应式断点 + 溯源高亮 + 动效
```

---

## 6. 测试计划

**单元（无外部依赖，mock BGE-M3/GLM）**：
- `test_knowledge_chunker.py`：`preview_chunks`、自定义 `chunk_size`。
- `test_parser_glm.py`：mock HTTP，验证 create→poll→raw_text 提取、失败/超时路径、tool_type 透传。
- `test_parser_local.py`：pypdf/docx 解析、`md_results/content` 回退逻辑。
- `test_ingest.py`：解析→分块→向量化入库、同文档幂等重算、分块编辑重算。

**API（app_client，mock embedding）**：
- `test_knowledge_api.py` 扩展：上传返回 pending、轮询列表出现 parse_status、分块列表/启停/编辑/重算、test-search、租户隔离（越权 404）、非法文件 400。

**工具/端到端（RAG 跑通）**：
- `test_knowledge_tool.py`：`enabled=false` 的分块不被检索到。
- `test_traceability.py`：`Hit` 携带 `kb_id`/`chunk_id`；`hits_to_markdown` 输出 `引用: KB-{kb_id}/{chunk_id}` 锚点；解析函数能从 markdown 提取锚点集合。
- 联调脚本：上传 PDF → 等解析 → 分块预览 → 编辑 → 检索命中 → 解析来源锚点 → 前端深链定位（真实 BGE-M3 本地服务）。

**前端（build/类型）**：
- `tsc -b` + `vite build` 通过；`utils/trace.ts` 锚点解析单测（可选用 Vitest，若无测试框架则以类型检查为准）。
- 响应式验证：开发环境下用 1280 / 900 / 375 三档视口人工核查布局（无横向溢出、触控可达）。

---

## 7. 里程碑（K 编号）

| 里程碑 | 内容 | 验收 |
|---|---|---|
| **K1 后端解析+向量引擎** | GLM 异步解析封装 + 本地解析降级 + BGE-M3 embedder（1024）+ 上传落盘/异步任务 | 单测过；上传→parse_status 流转正确 |
| **K2 后端分块治理 API** | DocumentMeta 模型 + 分块列表/预览/编辑/启停/重算 + test-search + 工具 enabled 过滤 | API 单测过；越权 404 |
| **K3 前端知识库体系页** | KnowledgeCenterPage + KnowledgeDetail（文档/分块/测试检索）+ beautiful-ui 质感 + 交互动效 + 响应式适配 | 页面完整；三档断点适配；类型/build 过 |
| **K3.5 溯源闭环** | 检索 Hit 带 kb_id/chunk_id + 引用锚点 + 聊天来源徽标深链 + 分块页定位高亮 | 答案可一键定位回分块；越权 404 无回归 |
| **K4 联调闭环** | 本地 BGE-M3 + GLM（或降级）端到端：上传→解析→分块→编辑→向量化→Agent 检索引用 | RAG 全链路跑通 |

---

## 8. 风险与决策点

| 风险/决策 | 说明 | 处理 |
|---|---|---|
| **GLM `file_url` 可达性** | 异步解析需文件 URL；本地/内网文件不可直接访问公网 | 后端落盘后暴露本地 HTTP 静态下载地址（`KB_PUBLIC_BASE_URL`）；无公网时**降级本地解析**并提示 |
| **`md_results` vs `content`** | 分块质量差异 | 优先 `md_results`，缺失回退 `content`，`parse_source` 记录 |
| **GLM key 缺失** | 不能阻塞功能 | 无 key 自动回退本地解析器（pypdf/docx），`parse_source="local"`，前端徽标区分 |
| **schema 迁移** | 加列/新表，开发底座无迁移工具 | 修改模型 + `create_tables` 重建；生产建议引入 Alembic（本期不阻塞） |
| **BGE-M3 稳定性** | 本地服务离线/维度变化 | embedder 校验维度；连接失败抛 `EmbeddingError` → 前端友好提示 |
| **解析异步化边界** | 大文件 + 多文档并发 | 用有界 `asyncio.Semaphore` 控制并发解析任务数（如 3） |
| **分块编辑 → 向量漂移** | 编辑后 embedding 过期 | 保存时自动 `re-embed`，禁止只改文本不重算 |

---

## 9. 验收标准（最终效果）

1. **知识库体系页面相对完整**：总览卡片 + 详情（文档任务/分块治理/测试检索）三大块齐全，交互流畅、动画质感与项目一致。
2. **RAG 流程跑通**：用户上传文档 → 后台解析（GLM 或本地降级）→ 分块可视化 → 手动编辑分块 → 向量化入库 → 对话/测试检索命中并带来源引用 → Agent 基于知识回答。
3. **追踪溯源闭环**：Agent 回复中的来源引用可点击，一键跳转到知识库分块管理页并滚动高亮到对应分块；可当场编辑修正。
4. **响应式适配**：桌面 / 平板 / 窄屏三档断点下页面布局、触控、动效均正常可用，无横向溢出。
5. **全量后端测试通过**、前端 `tsc` + `vite build` 通过。
6. 多租户隔离与审计无回归。
