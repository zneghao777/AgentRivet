# 记忆体系调研报告

> 目标：① 厘清当前项目 `MyAgentDemo1` 的记忆体系是怎么设计的；② 调研 2026 年市面上 Harness Agent 系统的记忆系统设计。

---

## 一、当前项目记忆体系

`MyAgentDemo1` 是一个「企业级 Agent Harness 底座」（FastAPI + LangGraph ReAct + Postgres + Skill 体系 + RAG）。**项目里并没有一个叫"记忆（Memory）"的独立模块**，但记忆能力被拆散分布在四层里。下面按"记忆的类型"来梳理。

### 1.1 短期记忆（对话上下文）—— 由 LangGraph 状态 + Postgres Checkpointer 承担

- **图状态**：`backend/app/harness/state.py` 定义了 `HarnessState(MessagesState)`，`messages` 由 `add_messages` 自动追加，即"当前对话的完整消息序列"，这就是**工作记忆 / 短期记忆**。
- **持久化**：`backend/app/harness/session_manager.py` 实现 `Session → thread_id` 一对一，每次对话把用户消息追加进同一个 `thread_id`。
- **Checkpointer**：`backend/app/storage/checkpointer.py` 用 **LangGraph `AsyncPostgresSaver`** 把每一轮图运行状态（含全部 messages）快照落库（独立 schema `checkpoints`）。**这是跨请求恢复记忆的关键**——服务重启后，同一 `thread_id` 能基于上次的 checkpoint 继续，实现多轮对话记忆。
- **消息表**：`Message` ORM 持久化每条 user/assistant/tool 消息 + `token_usage`，供历史查询、审计。

### 1.2 语义记忆（跨会话知识）—— 由 RAG 知识库承担（RAG-as-Tool）

- **存储**：`KnowledgeChunk` 表（`knowledge_chunks`），用 **Postgres + pgvector** 存文本分块 + embedding 向量（`Vector(EMBEDDING_DIM)`），按 `tenant_id` 隔离。
- **摄取**：`knowledge/service.py` 文档上传 → 异步解析 → 分块 → 向量化入库。
- **读取**：`knowledge/retriever.py` 做余弦相似度检索（仅 `enabled=true` 分块，带租户过滤 + 阈值），命中片段经 `hits_to_markdown` 转成带引用锚点（`KB-{kb_id}/{chunk_id}`）的文本。
- **注入方式**：封装成 `knowledge_search` 工具（`tools/knowledge_search.py`），走 `bind_tools` 的 ReAct 循环，**由 Agent 自主决定何时检索**。运行上下文（tenant_id / 知识库白名单）经 `harness/context.py` 的 `ContextVar` 注入，工具内部再次校验租户防越权。
- 这本质上是"**按需检索式语义记忆**"——但不具备跨会话自动沉淀用户偏好/事实的能力，检索内容是**人为导入的文档**，不是 Agent 从对话中提炼的。

### 1.3 程序记忆（怎么做任务的规则）—— 由 Skill 体系承担

- `backend/app/skills/` 的 `SkillManager`：遵循 Anthropic `SKILL.md` 规范，可加载/启停。
- `base_agent.py` 的 `build_system_prompt` 把已加载 Skill 的 description + body 注入 system prompt（渐进式：description 常驻，正文首触发注入）。
- 这就是**程序记忆**：把"如何完成任务的操作指令"静态注入上下文。注意它是**人为编写、静态加载**的，不是 Agent 自我更新学习到的。

### 1.4 记忆 Store —— 仅是一个预留占位

- `backend/app/storage/store.py` 明确写着「**记忆 Store（预留）**」，返回 `langgraph.store.memory.InMemoryStore`，注释：*"后续接入 Postgres/Redis 语义记忆（SHOULD）"*。
- 目前**未被真正使用**——没有代码往里面写入/读取语义记忆。

### 1.5 审计/可观测 —— 记忆的"外部凭证"

- `AuditLog` 表记录 `knowledge.search` 等动作，`Message` 记录 token_usage，配合 `observability/` 的 metrics / LangSmith，构成可回溯的记忆凭证（但非主动记忆）。

### 1.6 小结：项目记忆体系图谱

| 记忆类型 | 当前实现 | 载体 | 状态 |
|---|---|---|---|
| 短期/工作记忆 | 多轮消息 + 图状态 | LangGraph MessagesState + Postgres Checkpointer | ✅ 完整 |
| 语义记忆 | RAG-as-Tool 知识库检索 | Postgres + pgvector (`knowledge_search` 工具) | ✅ 完整（但为文档检索，非对话提炼） |
| 程序记忆 | Skill 指令注入 | SKILL.md + system prompt | ✅ 完整（静态） |
| 跨会话语义记忆（对话提炼） | 预留 `InMemoryStore` | LangGraph BaseStore | ⚠️ 占位，未启用 |
| 记忆卫生（去重/冲突/遗忘） | 无 | — | ❌ 缺失 |

**一句话概括当前项目**：短期记忆与 RAG 文档语义记忆是完整的，但**"Agent 从跨会话对话中自主沉淀并检索语义记忆"这一核心记忆能力目前只有占位符**，记忆卫生（去重、冲突、TTL 遗忘）也完全没有——这恰好对应了调研中"语义记忆"与"记忆卫生"这两块，是项目未来最该补的洞。

---

## 二、2026 年 Harness Agent 记忆系统设计调研

### 2.1 共识：LLM 无状态，记忆来自外部系统；分层是主流

- LLM 每次调用从零开始，"记忆"必须由外部记忆系统承担。
- **核心矛盾**：短期（上下文窗口）快但贵、有上限、会"上下文腐烂"；长期（外部存储）便宜但必须检索正确内容再注入。
- **为什么不能只扩窗口**：Transformer 注意力 n² 关系 + "lost in the middle"（Liu et al. 2024 中间丢失 U 型曲线），token 增多时精确回忆下降；成本也不经济（分层记忆可降约 4 倍成本）。

### 2.2 记忆的四种类型（Anthropic / LangMem / Letta / Mem0 共识）

| 类型 | 存什么 | 典型写 | 典型读 |
|---|---|---|---|
| **工作记忆 Working** | 当前轮次上下文（system + 近期消息） | 每轮自动 | 常驻上下文 |
| **情景记忆 Episodic** | 带时间戳的历史事件/经历 | 从已完成事件提炼 | 检索相似历史事件（few-shot） |
| **语义记忆 Semantic** | 事实、用户偏好、关系 | 即时/后台提取 | 向量相似度检索 |
| **程序记忆 Procedural** | 怎么做任务的操作指令/规则 | 反思并重写指令 | 加载进 system prompt |

### 2.3 主流架构：OS 式分层记忆（MemGPT → Letta 产品化）

由 MemGPT 论文提出（Deep Memory Retrieval 达 **93.4%** 准确率），是 2026 年最主流的参考范式：

- **核心记忆 Core**（≈RAM，<1k tokens，常驻上下文）：用户画像、Agent 画像、当前任务。
- **回忆记忆 Recall**（对话历史，可滚动/搜索/换入换出）。
- **存档记忆 Archival**（≈磁盘，外部向量存储，按嵌入搜索按需换入）。
- **关键设计**：**Agent 自主调用记忆工具**（`memory.insert()` / `memory.search()` / `memory.swap()`）管理自己的记忆层级，像操作系统管理虚拟内存一样管理上下文。

### 2.4 写入与读取路径（可复制的工程模板）

- **写入路径**：Agent 对话 → **后台提取进程** → 语义记忆（带来源 provenance + 置信度）→ 定期去重 / 冲突解析 / 清理。
- **读取路径**：核心记忆常驻 → 按需从情景/存档记忆检索 → **时间加权排序** → 注入上下文。
- **提取策略**：热路径同步（即时但增延迟）vs 后台异步（最终一致、便宜）。
- **写入策略**：提取-追加（低延迟，需自行去重）vs 提取-调和（自纠错，但 2 次 LLM 调用）。

### 2.5 记忆卫生（Memory Hygiene）—— 生产最大的坑

- 需**显式遗忘策略**：TTL（如情景记忆 90 天）、衰减分数、未强化则置信度下降。
- **去重与冲突解决**：矛盾事实触发写入前解析。
- **"最后更新时间"比"匹配分数"更重要**：检索需按时间加权。
- 隐私（`forget me` 请求）与检索是两个不同问题。
- 嵌入模型漂移 → 换模型需重索引全部存档。

### 2.6 上下文窗口管理的四种技术（配合记忆）

1. **压缩 Compaction**：窗口临近上限时总结重初始化（由 Agent 实现，非平台）。压缩提示词："先最大化回忆，再迭代提升精确度"。
2. **上下文编辑 Context Editing**：程序化清除过期工具调用/结果（Anthropic 自动行为，内部评估 100 轮搜索降 84% token）。
3. **结构化笔记**：定期把笔记写窗口**外**文件，按需读回（TODO / progress.md，压缩后仍存活可重新锚定）。
4. **干净上下文的子 Agent**：子任务用独立新窗口，仅返回精简摘要，避免污染主窗口。

**压缩安全线**：压缩"已解决的中段"，**保留活跃尾部逐字**（未解决决策/最近几轮）。

### 2.7 2026 主要框架对比

| 框架 | 记忆模型 | 存储 | 独特设计 |
|---|---|---|---|
| **Anthropic 记忆工具** | 窗口外文件笔记 | 客户端托管文件 | 模型发 CRUD 文件操作；配合上下文编辑+压缩 |
| **Letta (MemGPT)** | OS 式分层、自编辑 | 主上下文 + recall + archival(向量) | 虚拟内存分页，LLM 自管换入换出、自编辑 prompt |
| **Mem0** | 提取-追加事实库（v2.0 起仅 ADD） | 向量 + KV + 内置实体图 | 单次提取；`update()/delete()` 为显式调用 |
| **LangMem/LangChain** | 语义/情景/程序 API | 可插拔存储 | 一个 SDK 覆盖三类型；"热路径" vs 后台提取 |
| **Zep** | 时间感知知识图谱 | 云/自托管 | 时间推理 + 知识图谱 |

**选型决策树**：
- 已在用 LangGraph → **LangMem**
- 需 Agent 自主管理记忆层级 → **Letta**
- 想快速集成托管服务 → **Mem0**
- 需要时间推理 / 知识图谱 → **Zep**
- 硬性本地部署 → **自建**（Pinecone/Qdrant/**Postgres+pgvector**）

---

## 三、对本项目的启示（Gap 分析）

结合项目现状与行业范式，`MyAgentDemo1` 的记忆体系可以做这些增强（按优先级）：

1. **把 `InMemoryStore` 占位升级为真实语义记忆**（最贴合现有 LangGraph 底座 → 选 **LangMem** 思路）：
   - 后台异步从对话中提取事实/偏好 → 写入语义记忆集合，带来源 + 置信度 + 时间戳。
   - 用现有 `knowledge_chunks` 同款 **Postgres+pgvector** 承载，而非另起 Redis（复用基础设施，符合 RAG_INTEGRATION 结论）。
2. **加入记忆卫生**：去重（相似度/MD5）、冲突解析（UPDATE/DELETE）、TTL 遗忘、时间加权检索。
3. **程序记忆从"静态 Skill"升级为"自我更新"**：让 Agent 通过受控工具把学到的最佳实践追加回 Skill / 系统提示（带回滚日志），对齐"程序记忆"范式。
4. **情景记忆**：把历史会话（已解决工单）提炼为 few-shot 示例，支持相似场景复现。
5. **上下文窗口管理**：当前 `agent_node` 是全量 messages 进 prompt，可补"压缩 + 子 Agent"策略防上下文腐烂。

---

## 四、参考来源

- How AI Agent Memory Works in 2026（HeyCC AI Workflow Lab，2026-06，数据快照核验自 Anthropic 文档 / Mem0 系统论文 / LangMem 指南 / Letta 文档）
- AI Agent Memory Systems: A 2026 Engineering Guide（JobsByCulture）
- 本项目：`docs/SPEC.md`、`docs/RAG_INTEGRATION.md`、`backend/app/harness/*`、`backend/app/storage/*`、`backend/app/knowledge/*`
