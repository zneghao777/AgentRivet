# 企业级 RAG 能力差距调研报告

> 调研对象：当前知识库 / RAG 体系（`backend/app/knowledge/**`、`docs/KB_MANAGEMENT_SPEC.md`、`docs/RAG_SPEC.md`、`docs/RAG_RESEARCH.md`）
> 目标：对照企业级 RAG 交付标准，梳理当前 demo 级实现到生产可用的差距清单。
> 调研日期：2026-08-21

---

## 0. 结论速览（TL;DR）

当前体系已具备**可用的 Naive/Advanced-RAG 骨架**：文档上传→异步解析（GLM/本地）→分块→向量化→检索→溯源闭环，且含多租户隔离与审计。但它仍是**单机、单向量库、纯向量检索的 demo**，距离企业级生产 RAG 差 6 大类能力：

1. **文档解析与治理**（复杂版面/表格/多格式、解析质量校验、重试/幂等）——**目前仅文本级解析，无版面分析**
2. **分块与元数据**（结构化元数据、父子分块、语义分块、元数据过滤/过滤检索）——**当前分块简单、元数据仅 3 个字段**
3. **检索质量**（混合检索 BM25+向量、Rerank 精排、Query 改写、多路召回）——**当前仅纯向量 top-k**
4. **RAG 评测与可观测**（检索命中率/精度评测集、在线评估、反馈闭环）——**当前仅溯源，无评测**
5. **数据治理与运维**（schema 迁移 Alembic、异步任务队列/持久化、文件存储、清理与生命周期）——**当前 create_tables 重建 + 内存任务队列 + 本地磁盘**
6. **权限与安全**（文档级/分块级 ACL、脱敏、文件下载鉴权）——**当前仅租户级隔离**

各差距的详细现状与建议见下。

---

## 1. 差距盘点：按企业级 RAG 关键链路逐项对照

### 1.1 文档解析与治理（Ingestion）

**企业级要求**：复杂版面解析（多栏、表格结构、扫描件 OCR）、批量/流式导入、解析质量校验、失败重试与幂等、来源格式丰富（Excel/PPT/扫描件）。

**当前现状**（`local_parser.py` / `glm_parser.py`）：
- ✅ 支持 GLM 异步解析（PDF/DOCX/XLS/PPT/图片 OCR）+ 本地降级（pypdf / python-docx / txt/md）。
- ✅ 有解析状态机 `pending→parsing→parsed/failed/reparsing`、异步调度（Semaphore 限并发 3）。
- ⚠️ **本地解析仅提取纯文本**（`pypdf.extract_text` / `docx` 段落文本），**无版面分析、无表格结构提取、无章节层级还原**；扫描件 PDF 本地直接失效（无 OCR）。
- ⚠️ **无批量/目录导入**（只能单文件上传），无 zip/批量接口。
- ⚠️ **解析质量无自动校验**（如提取文本长度/乱码率阈值），`raw_text` 存了但不参与质量判断。
- ⚠️ **任务调度为进程内存中的 `asyncio.Task`**（`service._parse_tasks`），服务重启即丢、无法横向扩展、无任务持久化/队列（Celery/Redis/RQ）、无重试退避。

**差距结论**：解析能力可用但偏文本级；**任务队列 + 复杂版面解析是最大短板**。

### 1.2 分块与元数据（Chunking & Metadata）—— 用户点名核心

**企业级要求**：分块策略可配置（标题/递归/语义/父子分块）、**每块携带结构化元数据**（来源页/章节/时间/标签/作者等）并支持**元数据过滤检索**。

**当前现状**（`chunker.py` / `models.KnowledgeChunk.meta` / `ingest._embed_chunks`）：
- ✅ 标题层级分块 + 固定长度重叠兜底，表格独立成块；`chunk_size` 可配置（256/500/1000）。
- ✅ 分块治理（预览/编辑/启停/重算）。
- ⚠️ **元数据过于单薄**：`meta` 仅存 `{source_file, chunk_index, heading}` 三个字段（`ingest.py` L128-132）。**企业要求的"分块元数据"远不止此**：缺 `page_no / 章节路径 / 文档标题 / 创建/更新时间 / 标签 / 文档类型 / 原始 URL / 上传人 / 版本` 等。且这些字段未作为**检索过滤条件**暴露（检索 SQL 只按 `kb_id/tenant/enabled` 过滤，见 `retriever.py`）。
- ⚠️ **无父子分块**（小块召回 + 父块注入上下文），无上下文增强分块。
- ⚠️ **元数据存储结构**：`meta` 是 `JSON` 列，非结构化索引字段，**无法高效做 `WHERE meta->>'tag'=?` 类过滤**（未建 GIN 索引），影响企业级按维度检索。

**差距结论**：**分块元数据设计与企业级差距最大**——需从"只存来源"升级为"结构化多字段元数据 + 过滤检索"。

### 1.3 检索质量（Retrieval）

**企业级要求**：**混合检索（BM25 + 向量 + RRF 融合）**、**Rerank 精排**（cross-encoder）、Query 改写 / Multi-query / HyDE、元数据过滤、父子检索。

**当前现状**（`retriever.py`）：
- ✅ 纯向量余弦检索，支持 `tenant/kb/enabled` 过滤、score threshold、top_k。
- ⚠️ **纯向量检索**：专有名词、型号、数值、代码等场景召回差（`RAG_RESEARCH.md` §二-4 明确点名此短板）。
- ⚠️ **无 Rerank**：top-k 直接从向量距离取，无 cross-encoder 精排。
- ⚠️ **无 Query 增强**：不做改写/多查询/上下文压缩。
- ⚠️ **无混合检索**：`RAG_RESEARCH.md` 已规划"二期 BM25+向量、三期 Rerank"，但**均未落地**（retriever 只有 vector 一条路径）。

**差距结论**：检索质量是"召回准不准"的决定因素，**混合检索 + Rerank 是企业级硬门槛**，当前完全缺失（`RAG_SPEC.md` 明确列为非目标）。

### 1.4 RAG 评测与可观测（Evaluation & Observability）

**企业级要求**：**检索评测**（命中率 HitRate、MRR、NDCG）、**端到端评测**（忠实度/相关性/答案正确性，含 LLM-as-judge）、评测数据集、在线反馈闭环、检索成本/延迟埋点。

**当前现状**：
- ✅ 检索溯源锚点（`kb_id/chunk_id`）+ 前端深链定位，实现"答案→来源→分块"闭环（`KB_MANAGEMENT_SPEC` §3.6）。
- ✅ 有 `TraceScore.source="rag_eval"`、`EvaluationRun` 表（`models.py`）——**但仅为 schema 预留，未接任何评测/打分逻辑**（仅 `router_observability.py` 提到 schema）。
- ⚠️ **无检索质量评测**：没有 HitRate/MRR 等指标，无法量化"换 embedding / 换分块参数是否变好"。
- ⚠️ **无端到端 RAG 评测**：无 faithfulness / relevancy / answer_correctness 自动评测，无 LLM-as-judge。
- ⚠️ **无反馈闭环**：用户对回答"点赞/点踩/纠错"数据未回流到评测或分块治理。
- ⚠️ **无检索质量监控指标**：无 `hit_rate / retrieval_latency / rerank_latency / 检索失败率` 等（`agent_metrics_*` 只有通用指标）。

**差距结论**：有溯源但**无评测**——企业要"越用越准"必须有评测体系，这块基本空白（仅有表结构预留）。

### 1.5 数据治理与运维（Governance & Ops）

**企业级要求**：schema 迁移（Alembic）、**异步/持久化任务队列**（可重试、可观测、断点续传）、对象存储、生命周期（TTL/归档/垃圾回收）、知识库版本/快照、批量重索引。

**当前现状**：
- ⚠️ **无迁移工具**：`db.create_tables()` 用 `create_all + 一堆 IF NOT EXISTS ALTER` 硬编码（`db.py`），加列靠手写 DDL。**生产环境必须 Alembic**（`KB_MANAGEMENT_SPEC` §8 已自认"生产建议引入 Alembic，本期不阻塞"，但未实施）。
- ⚠️ **无持久化任务队列**：解析任务在进程内 `asyncio.Task`，重启丢失、不能水平扩展、无重试退避。
- ⚠️ **本地磁盘存储**（`data/uploads/{tenant}/{kb}/...`）：无对象存储（S3/MinIO/OSS）、无容量管理、无跨实例共享。
- ⚠️ **无生命周期管理**：无 TTL/归档/清理旧版本策略（文档删除后文件即删，但无自动 GC）。
- ⚠️ **无批量重索引 / 向量引擎切换**：embedding 模型变更（如 BGE-M3 换新模型）需全库重算，无增量重索引编排。
- ⚠️ **向量库无扩展性设计**：`retriever.py` 直写 pgvector SQL，**未抽象检索接口**（`RAG_RESEARCH.md` 规划"阶段二迁移 Qdrant"，但 retriever 层无抽象）。

**差距结论**：**运维底座整体偏 demo**（无迁移/无队列/无对象存储），是企业落地前必须先补齐的"地基"。

### 1.6 权限与安全（ACL & Security）

**企业级要求**：**文档级 / 分块级细粒度 ACL**（不止租户）、数据脱敏（敏感字段/文件）、文件下载鉴权、检索结果权限过滤、审计。

**当前现状**：
- ✅ 租户级隔离（`tenant_id` 强过滤 + 越权 404）+ 操作审计（upload/parse/delete/search 等）。
- ⚠️ **无文档级/分块级 ACL**：同一租户内所有用户权限相同，无按角色/部门/文档敏感级别的隔离（`RAG_SPEC.md` 明确"仅租户级隔离，用户级 ACL 后续版本"）。
- ⚠️ **无内容脱敏**：解析出的 `raw_text` / chunk 文本未做敏感信息（身份证/手机号/密钥）检测与脱敏。
- ⚠️ **文件下载无鉴权**：`build_file_url` 返回 `/uploads/...`，无 URL 签名/token 校验。
- ⚠️ **上传无恶意内容扫描**：仅格式/大小校验（`RAG_SPEC.md` §3.8 自认"无恶意内容扫描"）。

**差距结论**：租户隔离够用，但**文档级权限、脱敏、文件下载鉴权**缺失，是安全审计通不过的点。

---

## 2. 差距优先级排序（P0 / P1 / P2）

| 优先级 | 差距项 | 理由 |
|---|---|---|
| **P0** | 分块元数据结构化 + 元数据过滤检索 | 用户点名的核心；企业检索必须按维度过滤 |
| **P0** | schema 迁移（Alembic） | 无迁移工具，任何演进都不可控，是地基 |
| **P0** | 持久化/可重试解析任务队列 | 进程内 Task 重启即丢，生产不可接受 |
| **P1** | 混合检索（BM25 + 向量 + RRF） | 召回质量决定 RAG 成败，纯向量不够 |
| **P1** | Rerank 精排（bge-reranker-v2-m3） | Advanced RAG 关键，直接提精度 |
| **P1** | 检索/端到端评测（HitRate/MRR + LLM-judge） | 没有评测无法量化优化，无法"越用越准" |
| **P1** | 复杂版面解析（表格/扫描 OCR 落地） | 企业文档多为扫描件/复杂表格 |
| **P2** | 对象存储（S3/MinIO）+ 文件下载鉴权 | 有量级后才必须，本地盘可先用 |
| **P2** | 文档级 ACL + 内容脱敏 | 强合规场景需要 |
| **P2** | 向量库接口抽象（pgvector→Qdrant 平滑迁移） | 数据量大才触发，但要提前留好抽象 |
| **P2** | 生命周期管理（TTL/归档/批量重索引） | 长期数据治理 |

---

## 3. 分阶段落地建议（对齐项目 Milestone 风格）

```
E-RAG-1  地基：Alembic 迁移 + 持久化任务队列（Celery/Redis 或 pg 队列） + 文档级元数据字段设计
E-RAG-2  元数据检索：meta 结构化 + GIN 索引 + 检索 SQL 支持元数据过滤（tag/时间/文档类型/章节）
E-RAG-3  检索质量：混合检索（pg_trgm tsvector + 向量 + RRF）→ Rerank（bge-reranker）
E-RAG-4  评测闭环：检索评测集（HitRate/MRR）+ 端到端 LLM-judge + 用户反馈回流 + 监控指标
E-RAG-5  运维/安全（按需）：对象存储 + 文件鉴权 + 文档级 ACL/脱敏 + 向量库抽象
```

---

## 4. 一句话总结

> 当前是**"能跑通但不够生产"**的 RAG 底座：解析/分块/检索/溯源/租户隔离已成型，但**分块元数据、混合检索+Rerank、评测闭环、迁移与任务队列**四块是企业级落地的核心差距；建议优先补 **元数据体系 + Alembic + 任务队列（P0）**，再推进 **混合检索 + 评测（P1）**。

---

## 附：参考

- `docs/RAG_RESEARCH.md` —— 已明确规划混合检索/Rerank/父子分块/向量库迁移路线（未落地）。
- `docs/RAG_SPEC.md` §1.3 —— 明确将 Rerank、混合检索、ACL 列为"本期不做"。
- `docs/KB_MANAGEMENT_SPEC.md` §8 —— 自认无迁移工具、任务队列为进程内、需 Alembic。
- `backend/app/storage/models.py` —— `TraceScore.source="rag_eval"`、`EvaluationRun` 仅为 schema 预留。
