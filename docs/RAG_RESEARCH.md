# 主流 RAG 方案调研报告

> 调研时间：2026-08。面向当前 Agent Harness 底座（FastAPI + LangGraph + Postgres + Redis），重点服务于**形式 A（RAG-as-Tool 检索工具）**的落地选型。

---

## 一、RAG 范式演进：从 Naive 到 Agentic（5 代）

当前业界公认 RAG 经历了五代范式演进：

| 范式 | 核心思想 | 典型能力 | 适用阶段 |
|---|---|---|---|
| **1. Naive RAG** | 文档 → 分块 → Embedding → 向量库 → 检索 top-k → 拼接 Prompt → 生成 | 基础问答 | 起步 MVP |
| **2. Advanced RAG** | 在"检索前 + 检索后"做精细化优化 | 查询改写、混合检索、Rerank 精排 | 生产可用 |
| **3. Modular RAG** | 将系统拆成可复用模块，乐高式编排 | 检索/记忆/路由/融合模块自由组合 | 复杂系统 |
| **4. GraphRAG** | 微软开源（2024），把文档建模成**图结构**（实体-关系）注入索引与检索 | 全局性、多跳问题、关系推理 | 强关联知识 |
| **5. Agentic RAG** | Agent 接管检索策略：自主决定**何时检索、检索什么、如何验证**，支持多步检索与工具调用 | 复杂多跳问题、动态规划检索 | 当前最前沿 |

**对本项目（形式 A）的定位**：形式 A 本质上是 **Agentic RAG 的简化形态**——把"知识检索"作为一个工具暴露给 Agent（Harness 的 ReAct 循环），由 LLM 自主决策调用。这也正是 2025-2026 企业 RAG 的主流方向（Google Research 称 Agentic RAG 相比标准 RAG 在事实性数据集准确率提升最高达 34%）。

### Agentic RAG 的三种常见实现深度

```
L1 单轮工具式检索   ← 形式A即此档：LLM 调一次 knowledge_search 工具
L2 检索代理 + 多工具 ← 检索代理可调用 向量库/关键词/网页 等多个检索源
L3 规划-执行-验证     ← LLM 先规划检索蓝图，分步检索，自我验证/修正
```

建议：**从 L1 起步（形式 A）**，跑通后再视效果升级 L2（增加混合检索、Rerank 等工具）。

---

## 二、RAG 全链路关键技术点

一条生产级 RAG 链路包含以下环节，每个环节都有主流做法：

### 1. 文档解析（Ingestion）
- **PDF/Word/扫描件**：`unstructured`（生态全）、`pdfplumber`（版面/表格）、`PyMuPDF`（轻量）。
- **复杂版面（多栏/表格/扫描 OCR）**：RAGFlow 的 DeepDoc（版面分析 + 表格结构提取 + OCR）业界口碑最好，但较重。
- 中文场景重点：表格结构提取、标题层级识别（供结构化分块）。

### 2. 分块策略（Chunking）—— 影响最大的环节
| 策略 | 做法 | 适用 |
|---|---|---|
| 固定长度 + 重叠 | 500 字符 / 重叠 50 | 起步最稳 |
| 递归分块 | 按分隔符层级（段落→句→字）逐级切 | 通用 |
| 标题层级分块 | 按 Markdown/文档标题结构切 | 长文档/制度类 |
| 语义分块 | 按语义完整单元（句法树）切 | 追求高质量 |
| 上下文增强 | 块内附加前后邻接段落 | 提升上下文完整性 |

> 结论：**中文制度/文档类场景优先"标题层级 + 固定长度重叠"组合**，表格单独成块。

### 3. Embedding 模型
- 主流：OpenAI `text-embedding-3-small/large`、`BGE-M3`（中文强）、`text2vec-large-chinese`、`bge-large-zh`。
- 中文企业场景：**BGE 系列 / text2vec 系列**本地化效果与性价比俱佳；若走 API 则 `text-embedding-3-small` 最省。
- 注意：query 与 document 常需**不对称 embedding**（如 bge 的 query 指令前缀）。

### 4. 检索（Retrieval）—— 形式 A 的核心
| 方式 | 说明 |
|---|---|
| 纯向量检索 | 余弦相似度 top-k，最基础 |
| **混合检索（Hybrid）** | BM25 关键词 + 向量召回，**RRF（Reciprocal Rank Fusion）** 融合排序。解决专有名词/型号/数值/代码场景向量失效问题 |
| 元数据过滤 | 按 `tenant_id`、`knowledge_base`、时间等过滤（本底座多租户必选） |
| 父子分块 | 小块召回、父块注入，保精度又保上下文 |

### 5. 重排序（Rerank）—— Advanced RAG 的关键
- 向量召回 50-100 条粗排 → **cross-encoder rerank 模型精排** top-5。
- 主流：`bge-reranker-v2-m3`（中文）、`Cohere Rerank`、`Jina Reranker`。
- 比纯向量距离更细粒度判断"问题 vs 片段"真实匹配度，召回质量提升明显。

### 6. Query 增强（检索前）
- **Query 改写**：LLM 把口语问题改写成检索友好表述。
- **Multi-query**：一个问题拆多个子查询并行检索后去重合并。
- **HyDE**：先生成一个"假设性答案"再用它去检索，弥合语义鸿沟（成本高，非首选）。

### 7. 生成（Generation）
- 检索片段注入 system prompt / context，要求 LLM **基于片段回答 + 标注引用来源**（`[1]` 对应 `source_file`）。
- 可叠加 **Self-RAG**：LLM 自我反思检索内容是否足够、回答是否忠于片段。

---

## 三、主流 RAG 框架对比

| 框架 | 定位 | 优点 | 缺点 | 与底座契合度 |
|---|---|---|---|---|
| **LangGraph / LangChain** | 通用 Agent/流程编排 | 底座已用；图编排 + 工具 + 生态最全 | 封装较少，RAG 细节要自己写 | ⭐⭐⭐⭐⭐ |
| **LlamaIndex** | RAG 专精数据框架 | 数据连接器/索引/检索器开箱即用 | 与 LangGraph 编排层重叠 | ⭐⭐⭐ |
| **Haystack** | 生产级组件化管道 | 组件成熟、测试完善 | 需引入新编排体系 | ⭐⭐ |
| **DSPy** | 声明式优化 | 自动优化 prompt/检索策略 | 学习曲线陡 | ⭐⭐ |
| **RAGFlow** | 文档深度解析 RAG 引擎 | DeepDoc 版面/表格解析业界第一；端到端 | 重平台、自成体系，难嵌入现有底座 | ⭐⭐ |
| **Dify / MaxKB / FastGPT** | 低代码知识库平台 | 开箱即用、UI 完善 | 是独立产品而非可嵌入底座的能力 | ⭐ |

**结论（关键）**：底座已深度绑定 LangGraph + langchain-core，RAG 检索能力**直接用 LangChain 生态实现**（`langchain-openai` embedding + 自研 retriever 工具）即可，**不必引入 LlamaIndex / RAGFlow 等新框架**——它们是"重平台"，与形式 A 的轻量工具式目标冲突。RAGFlow 的 DeepDoc 可作为**可选的外部文档解析服务**单独部署。

---

## 四、向量数据库对比与选型

| 方案 | 架构 | 数据规模 | 混合检索 | 运维成本 | 适合谁 |
|---|---|---|---|---|---|
| **pgvector** | Postgres 扩展 | 百万级（HNSW） | 需配合 BM25（pg_trgm/tsvector） | ★ 最低（复用现有 PG） | **已有 Postgres 的中小规模（本项目首选）** |
| Qdrant | Rust 原生 | 千万级 | 内置 | 低-中 | 中等规模独立部署 |
| Milvus | 分布式 | 亿级 | 内置+标量过滤强 | 高 | 大规模集群 |
| Weaviate | Go | 千万级 | 内置 | 中 | 需混合检索开箱即用 |
| Chroma | 嵌入式 | 十万级 | 弱 | 最低 | 本地开发原型 |
| Pinecone | SaaS 托管 | 弹性 | 有 | 无（云） | 不想运维 |

**选型结论**：
- **阶段一（推荐）**：**pgvector**。项目已部署 Postgres 16，加一个扩展 + HNSW 索引即可，零新增基础设施；百万级 chunk 完全够用；`tenant_id` 等标量过滤天然走 SQL。
- **阶段二（超百万级 / 亿级 / 需高性能混合检索时）**：迁移 **Qdrant**（千万级、独立服务、内置混合检索 + 过滤），或 Milvus（亿级分布式）。
- 过渡方案：**retriever 层做接口抽象**，`pgvector` 与 `Qdrant` 实现可互换，未来平滑迁移。

---

## 五、面向本项目（形式 A）的推荐落地方案

### 推荐组合（按优先级）

| 决策点 | 推荐 | 理由 |
|---|---|---|
| 实现范式 | **形式 A（Agentic RAG L1）**，预留 L2 升级 | 与底座 ReAct 天然契合 |
| 检索框架 | **LangChain 生态**（不引入新框架） | 底座已用，封装成本最低 |
| 向量库 | **pgvector**（阶段一）→ Qdrant（可选阶段二） | 复用 Postgres，零新增服务 |
| Embedding | API：`text-embedding-3-small`；本地：**BGE-M3 / text2vec-large-chinese** | 中文效果好、性价比高 |
| 检索增强 | 起步纯向量 → 二期加 **BM25+向量 混合检索 + RRF** → 三期加 **Rerank** | 渐进，按召回质量驱动 |
| 分块 | **标题层级 + 固定长度(500)重叠(50)** | 制度/文档类中文场景最优 |
| 文档解析 | `unstructured`/`pdfplumber`；复杂版面可选 RAGFlow DeepDoc | 按文档复杂度 |
| 检索过滤 | `tenant_id` + `knowledge_base`（形式 C Skill 路由） | 多租户安全红线 |
| 引用溯源 | 片段带 `source_file`，回答标注 `[1]` | 可信度与审计 |

### 渐进式实施路线（与项目 Milestone 风格一致）

```
M-RAG-1  MVP：pgvector + 分块入库 + knowledge_search 工具 + 上传 API
M-RAG-2  增强：混合检索(BM25+向量+RRF) + 元数据过滤 + 引用溯源
M-RAG-3  精排：Rerank 重排序 + Query 改写 + 父子分块
M-RAG-4  Agentic 升级：检索代理（多检索源）+ 自我验证（可选）
```

### 一句话推荐

> **用 LangChain 生态 + pgvector 实现一个 `knowledge_search` 检索工具（形式 A），起步 Naive RAG（标题分块 + 向量检索 + 租户过滤），随后按"混合检索→Rerank→Agentic 检索代理"的路径渐进增强。不引入重量级 RAG 平台，保持底座"能力可插拔"的架构风格。**

---

## 六、参考来源

- 腾讯云开发者社区：RAG 16 种方案详解 / RAG 架构演进核心要点
- 智源社区：最全梳理——RAG 技术的 5 种范式（Naive/Advanced/Modular/GraphRAG/Agentic）
- Google Research（2026-06）：Gemini Enterprise Agent Platform —— Agentic RAG 准确率提升 34%
- AIMultiple / AI Wiki：RAG Frameworks Benchmark（LangChain/LangGraph/LlamaIndex/Haystack/DSPy）
- 向量数据库横评：Pinecone/Weaviate/Milvus/Qdrant/pgvector 多篇对比
- RAG 分块策略实战（得物/京东云）：中文分块、标题层级、表格解析
- 低代码平台横评：Dify / RAGFlow / MaxKB / FastGPT 对比
- 混合检索 + Rerank 企业级优化方案（QubitTool）
