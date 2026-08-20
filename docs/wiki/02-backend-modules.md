# 02 后端模块职责

本文档描述 `backend/app/` 下各模块的职责边界与协作方式。所有配置集中在 [config.py](../../backend/app/config.py)（`settings` 全局单例，pydantic-settings 从环境变量 / `.env` 读取）。

## 模块总览

| 模块 | 目录 | 一句话职责 |
|---|---|---|
| 入口 | `main.py` | FastAPI 实例、CORS、路由挂载、lifespan 启动钩子 |
| 配置 | `config.py` | 全局配置中心，所有环境变量的唯一定义处 |
| API | `api/` | REST 路由、请求/响应 schema、鉴权依赖 |
| Harness | `harness/` | 图编排核心：建图、编译、运行时、会话管理、SSE 事件 |
| Agents | `agents/` | 主 Agent 节点、子 Agent 定义与注册中心 |
| Tools | `tools/` | 工具注册中心、内置工具、知识/记忆检索工具、MCP 适配 |
| Skills | `skills/` | SKILL.md 发现、解析、启停、脚本工具注册 |
| Knowledge | `knowledge/` | RAG 全链路：解析、分块、向量化、入库、检索 |
| Memory | `memory/` | 长期记忆：提取、检索、卫生（去重/冲突/TTL）、后台清理 |
| Observability | `observability/` | tracing、审计、指标、聚合与保留清理、SLO 告警数据 |
| Security | `security/` | API Key 哈希校验、租户解析 |
| Storage | `storage/` | async engine/session、ORM 模型、仓储函数、Checkpointer |

## api/ — REST 路由层

| 文件 | 前缀 | 职责 |
|---|---|---|
| `deps.py` | — | `require_auth()` 依赖：读取 `X-API-Key` / `X-Tenant-Id` → `verify_api_key()` → `resolve_tenant()` → 返回 `AuthContext`；`get_db()` 提供数据库会话 |
| `schemas.py` | — | 统一 `ApiResponse` 包装、`MessageSendRequest` / `MessageStreamRequest`（message / skills / knowledge_bases）等模型 |
| `router_sessions.py` | `/sessions` | 会话 CRUD 与消息历史 |
| `router_chat.py` | `/sessions` | 对话三入口：非流式 `POST /{id}/messages`、流式 `POST /{id}/messages/stream`（SSE）、HITL 恢复 `POST /{id}/resume` |
| `router_skills.py` | `/skills` | Skill 列表/详情/启停/重载 |
| `router_tools.py` | `/tools` | 已注册工具元数据快照 |
| `router_knowledge.py` | `/knowledge-bases` | 知识库 CRUD、文档上传/重解析/删除、分块管理（编辑/启停/重嵌入）、检索与测试检索 |
| `router_memory.py` | `/memory` | 记忆列表/时间线/总览/设置/单条更新删除/清空 |
| `router_admin.py` | （无前缀） | `/health`、`/health/ready`、`/audit`、`/metrics` |
| `router_observability.py` | `/observability` | 观测台全部数据接口：总览、traces、calls、logs、成本、评分、评测、告警、事件、工具日志、趋势 |

路由层职责边界：参数校验、鉴权、组装调用 harness/knowledge/memory 服务层，不包含业务逻辑。

## harness/ — 编排核心

| 文件 | 职责 |
|---|---|
| `app_runtime.py` | `HarnessApp` 单例（`harness_app`）：`build()` 调 `build_supervisor_graph()` 生成编译图并初始化 `GraphRuntime`，供 API 层调用 |
| `graph_builder.py` | 主 Agent ReAct 图：`build_main_graph()` 添加 `agent`/`tools` 节点与条件边；`should_continue()` 判断最后消息是否含 `tool_calls`；编译时注入 checkpointer 与 interrupt 配置 |
| `supervisor.py` | `build_supervisor_graph()`：`MULTI_AGENT_ENABLED` 关闭时回退主图；开启时构建 Supervisor 路由（main + `sub_<agent>` 条件边） |
| `graph_runtime.py` | `GraphRuntime`：`astream()`（流式，SSE 事件 + span 采集）、`ainvoke()`（非流式，返回最终状态 + `_observability` 摘要）、`resume()`（HITL，`Command(resume=...)`）。run_id→span_id 映射维护父子链；每次 LLM 独立记 token，根只汇总 |
| `events.py` | SSE 帧构造：`sse_frame()` + 各事件生成函数（session_start / trace_start / message_start / message_delta / tool_start / tool_end / node_event / interrupt_requested / message_end / run_end / error） |
| `state.py` | `HarnessState(MessagesState)`：图状态扩展 `tenant_id` / `user_id` / `skill_loaded` / `memory_context` / `tool_calls_total` / `interrupt_count` / `final_answer` 等字段 |
| `context.py` | `ToolContext` + `set/get/reset_tool_context()`：请求级上下文（tenant/user/知识库白名单），供工具在 ReAct 循环内做防越权校验（contextvars 传递） |
| `session_manager.py` | `SessionManager`：会话创建/恢复/删除/列表/历史读取；`gen_thread_id()`、`derive_session_title()`（首条消息截断生成标题） |

## agents/ — Agent 定义

| 文件 | 职责 |
|---|---|
| `base_agent.py` | `create_llm()` 工厂（OpenAI 兼容 `ChatOpenAI`，读 settings）；`build_system_prompt()`（注入已加载 Skill 描述与正文、记忆上下文）；`agent_node()` 主 Agent 节点：绑定 tools → 调 LLM → 返回 AIMessage（含 tool_calls 或最终回复） |
| `registry.py` | `AgentEntry(name, description, graph)` 与 `AgentRegistry`（register/get/all/clear），全局单例 `agent_registry`，供 Supervisor 构建路由 |
| `demo_subagent.py` | 示例子 Agent：`build_demo_subagent()` 构造独立 StateGraph 返回 `AgentEntry`，用于验证多 Agent 链路 |

## tools/ — 工具体系

| 文件 | 职责 |
|---|---|
| `registry.py` | `ToolRegistry`：`register()`（记录工具与来源）、`snapshot()`（元数据快照供 API/前端展示）、`register_builtin()`（注册全部内置与检索工具，`memory_search` 受 `MEMORY_SEARCH_TOOL` 开关控制） |
| `builtin/calculator.py`、`echo.py`、`get_time.py` | 演示用内置工具（`@tool` 装饰器实现） |
| `knowledge_search.py` | RAG-as-Tool：`_knowledge_search_async()` 从 ToolContext 取租户与 KB 白名单 → `KnowledgeService.retrieve()` → markdown 结果；同步包装为 LangChain tool |
| `memory_search.py` | 记忆检索工具：`_memory_search_async()` → `MemoryService.search_as_tool()` |
| `mcp_adapter.py` | MCP 适配占位：`register_mcp_tools()` 当前返回 0，预留通过 `langchain_mcp_adapters.client.MultiServerMCPClient` 接入外部 MCP 服务器 |

## skills/ — Skill 体系

| 文件 | 职责 |
|---|---|
| `loader.py` | 解析 `SKILL.md` frontmatter（name/description/version/author/tags/enabled）；`discover_skill_dirs()` 扫描含 `SKILL.md` 的目录（内置 `builtin_skills/` + 外部 `skills/`） |
| `manager.py` | `SkillManager`（`skill_manager` 单例）：`discover()` 扫描并同步 DB；`load()` 启用 Skill 并把 `scripts/*.py` 中顶层 `run()` 用 `StructuredTool.from_function()` 注册为 `skill_{name}__{script}` 工具，停用按前缀卸载 |
| `models.py` | `SkillModel` 数据类 |
| `builtin_skills/skill_hello/` | 内置示例 Skill |

Skill 与 tools 的关系：Skill 是"提示词扩展 + 可选脚本工具"的打包单位；启停即时生效并反映到 system prompt 与工具列表。

## knowledge/ — RAG 知识库

| 文件 | 职责 |
|---|---|
| `service.py` | `KnowledgeService`：KB CRUD、文档上传/重解析/删除、分块管理（update/toggle/re-embed）、`retrieve()`/`search()`、`preview_document()`；`_schedule_parse()`/`_run_parse()` 后台异步解析任务 |
| `parser/glm_parser.py` | 智谱异步解析：`create_parse_task()`（multipart 直传）→ `poll_parse_status()` 轮询（`GLM_FILES_POLL_SECONDS`，超时 `GLM_FILES_TIMEOUT_SECONDS`）；`ParseResult`；`file_type_for()`/`tool_supports()`（lite/expert/prime 与文件类型匹配，图片 OCR 需 prime/lite） |
| `parser/local_parser.py` | 本地解析：`parse_local()` 用 pypdf / python-docx 提取文本（GLM Key 未配置时的回退路径） |
| `chunker.py` | `chunk_text()`：按 Markdown 标题层级切分，超长段落回退固定长度 + 重叠（`RAG_CHUNK_SIZE`/`RAG_CHUNK_OVERLAP`），表格独立成块；`preview_chunks()` 前端预览 |
| `embedder.py` | `embed_texts()` 批量 / `embed_query()` 单条；`_post_embeddings()` 调 OpenAI 兼容 `/embeddings`；`_validate_dim()` 校验 `EMBEDDING_DIM`（1024） |
| `embedding.py` | embedding 相关共享工具 |
| `retriever.py` | `retrieve()`：pgvector 余弦距离检索（`embedding <=> CAST(:qvec AS vector)`），强制 `tenant_id` + `enabled` + `kb_ids` 过滤，返回 `Hit`；`hits_to_markdown()` 生成带引用锚点结果 |
| `ingest.py` | `parse_and_ingest()` 入库主流程：`_resolve_text()` 选解析器 → 分块 → `_embed_chunks()` → 写 `knowledge_chunks`；同名文档幂等重算 |
| `files.py` | 上传文件落盘（`KB_UPLOAD_DIR`，按租户/KB 分目录）：`save_upload()` / `read_upload()` / `remove_upload()` / `build_file_url()` |

## memory/ — 长期记忆

| 文件 | 职责 |
|---|---|
| `service.py` | `MemoryService`：`extract_from_conversation()`（对话后提炼）、`_consolidate_one()`（逐条调和）、`retrieve_relevant()`（注入检索）、`format_for_prompt()`、列表/时间线/总览/设置/增删改、`search_as_tool()`、`run_ttl_cleanup()` |
| `extractor.py` | `extract()`：构造 prompt 调 LLM，解析 JSON 数组并规范化（content/memory_type/confidence/source_summary），单次上限 `MEMORY_EXTRACT_MAX_ITEMS` |
| `retriever.py` | `retrieve()`：embed_query → SQL（tenant+user+status=active），`final_score = similarity × recency_weight` 排序；`memories_to_markdown()`；`MemoryHit` |
| `hygiene.py` | 记忆卫生：`resolve_duplicate()`（ADD/NOOP/CONFLICT 判定）、`strengthen()`（更新 access_count/last_accessed_at）、`expire_stale()`（TTL 过期，高访问记忆豁免） |
| `cleanup.py` | 后台任务：`_run_ttl_scan_once()` 遍历全部 (tenant, user) 执行过期；`start_ttl_scan()` / `stop_ttl_scan()` 周期调度 |

## observability/ — 可观测性

| 文件 | 职责 |
|---|---|
| `tracing.py` | 采集核心：`TraceContext`、`start_root_span()` / `record_child_span()`、采样传播、`sanitize_attrs()` / `redact_object()` / `sanitize_text()`（删密钥、遮 PII、截断）、`init_tracing()` / `shutdown_tracing()`、`get/set_trace_context()` |
| `audit.py` | `audit()` 异步队列写 `audit_logs`（队列满降级同步）；`sanitize_detail()` 过滤敏感字段；`init_audit()` 注入 session 工厂 |
| `metrics.py` | 内存聚合器：`record_request()`（请求/错误/延迟/token）、`record_tool()`、`summary()`（成功率、P50/P95、活跃会话） |
| `aggregation.py` | 后台 worker：`start_metric_worker()`；`_run_hourly_aggregation()` 小时桶聚合；`_run_aggregation()` 日聚合（root span → `agent_metrics_daily`）；`_run_trace_summary()` 补 trace 汇总；`_run_cleanup()` 按保留天数清理 |

## security/ — 鉴权与租户

| 文件 | 职责 |
|---|---|
| `api_key.py` | `hash_api_key()` SHA256 存储；`verify_api_key()` 用 `hmac.compare_digest()` 常量时间比较；区分 prod（必须显式配置）/ dev（默认 key）策略 |
| `tenant.py` | `resolve_tenant()`：从 API Key 前缀（含 `_` 且非 `sk`/`sk-dev` 前缀时取前缀）或 `X-Tenant-Id` 头解析租户 ID |

## storage/ — 存储层

| 文件 | 职责 |
|---|---|
| `db.py` | async engine + `AsyncSessionLocal`；`get_db()` FastAPI 依赖；`create_tables()` 建表、启用 pgvector 扩展、创建 HNSW 索引、旧表迁移 |
| `models.py` | 21 张 ORM 表（详见 [05-data-model.md](05-data-model.md)） |
| `repositories.py` | 函数式仓储：`create_session()` / `get_session()` / `list_sessions()` / `add_message()`（记录 tool_name/token_usage）/ `upsert_skill()` / `get_skill()` / `list_skills()` / `set_skill_enabled()` 等，全部按 `tenant_id` 隔离查询 |
| `checkpointer.py` | `get_checkpointer()`：LangGraph `AsyncPostgresSaver`（会话状态持久化，支持中断恢复）；`_pg_conninfo()` 转换连接串 |
| `store.py` | `get_store()`：LangGraph `InMemoryStore`（跨会话键值存储入口） |
