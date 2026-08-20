# 01 整体架构

本文描述 agent-harness 的分层架构、核心数据流与应用启动生命周期。

## 分层架构总览

```
┌─────────────────────────────────────────────────────────────────────┐
│  前端工作台 (React 18 + Vite + AntD 5 + Zustand)                     │
│  ChatPage / KnowledgeCenter / MemoryPage / SkillsPage /             │
│  SettingsPage / Observability(总览·Trace·工具日志)                   │
│  - http.ts: axios 封装，自动注入 X-API-Key                            │
│  - sse.ts: fetch + ReadableStream 解析 SSE 流                        │
└──────────────────────────┬──────────────────────────────────────────┘
                           │ REST /api/v1/* + SSE (X-API-Key)
┌──────────────────────────▼──────────────────────────────────────────┐
│  API 层 (FastAPI routers, backend/app/api/)                          │
│  sessions / chat / skills / tools / knowledge-bases / memory /       │
│  observability / admin(health·audit·metrics)                         │
│  deps.require_auth(): API Key 校验 + 租户解析 → AuthContext           │
└──────────────────────────┬──────────────────────────────────────────┘
┌──────────────────────────▼──────────────────────────────────────────┐
│  Harness 编排层 (backend/app/harness/)                               │
│  HarnessApp(单例) → Supervisor/主 Agent 图(LangGraph) →              │
│  GraphRuntime(astream / ainvoke / resume) → SSE 事件 + Span 采集     │
│  ReAct 循环: agent ⇄ tools（工具由 ToolRegistry 提供）                │
└───┬───────────┬───────────┬───────────┬──────────────┬──────────────┘
    │           │           │           │              │
┌───▼───┐ ┌────▼─────┐ ┌───▼────┐ ┌────▼─────┐ ┌──────▼──────┐
│ tools │ │ knowledge│ │ memory │ │ skills   │ │observability│
│ 注册中 │ │ RAG 流水 │ │ 长期记忆│ │ SKILL.md │ │ trace/审计/ │
│ 心+内 │ │ 线+检索器│ │ 提取/检 │ │ 加载/脚本 │ │ 指标/聚合/  │
│ 置工具 │ │          │ │ 索/卫生 │ │ 工具注册  │ │ 告警        │
└───┬───┘ └────┬─────┘ └───┬────┘ └────┬─────┘ └──────┬──────┘
    │           │           │           │              │
┌───▼───────────▼───────────▼───────────▼──────────────▼──────────────┐
│  存储层 (backend/app/storage/)                                       │
│  SQLAlchemy 2.x async + psycopg │ LangGraph AsyncPostgresSaver       │
│  PostgreSQL 16 + pgvector(HNSW) │ 21 张业务/观测表                    │
└──────────────────────────┬──────────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────────┐
│  外部服务                                                            │
│  LLM API(OpenAI 兼容, 默认 DeepSeek) │ Embedding API(BGE-M3,1024维) │
│  GLM 文件解析(智谱, 异步+OCR, 可回退本地) │ Redis(可选)               │
└─────────────────────────────────────────────────────────────────────┘
```

## 模块依赖方向

后端模块严格单向依赖，避免循环：

```
main.py ──► api ──► harness ──► agents ──► tools/skills
   │          │        │                     │
   │          │        └──► observability ◄──┘(tracing 采集)
   │          ├──► knowledge ──► storage
   │          ├──► memory ─────► storage
   │          └──► security(鉴权依赖)
   └──► storage(建表) / config(全局)
```

- `config.py` 被所有模块引用（全局 `settings` 单例）
- `storage` 是最底层，仅依赖 `config`
- `observability.tracing` 被 `harness`（图运行时）与 `tools`（工具执行）调用，处于横切位置

## 核心数据流

### 1. 流式对话（最主链路）

```
前端 ChatWindow.sendMessage()
  → sse.ts streamChat(): POST /api/v1/sessions/{id}/messages/stream
    → deps.require_auth(): 校验 X-API-Key, 解析 tenant
    → router_chat: 校验 session → 写入用户消息(add_message)
      → 解析请求中的 skills / knowledge_bases
      → MemoryService.retrieve_relevant() 生成记忆上下文(可选, MEMORY_AUTO_INJECT)
      → 设置 ToolContext(tenant/user/kb 白名单) 供工具内部校验
    → harness_app.runtime.astream()
      → start_root_span("agent.run") → SSE trace_start
      → LangGraph astream_events(v2) 消费事件队列:
          on_chat_model_stream → SSE message_start/message_delta (TTFT 记录)
          on_chat_model_start/end → llm span(独立 token usage)
          on_tool_start/end/error → tool span + SSE tool_start/tool_end
          on_chain_start/end → node span
      → agent_node 内: system prompt(含 skill 正文+记忆) + 绑定 tools → LLM
      → should_continue(): 有 tool_calls → tools 节点执行 → 回 agent; 否则 END
      → SSE message_end(token 汇总) / run_end(status, duration, tool_calls)
      → root_span.finalize() 落库 agent_traces + agent_spans
    → 前端按事件增量渲染(流式文本/工具卡片)
  → 会话结束后(非流式): MemoryService.extract_from_conversation() 提炼记忆
```

SSE 事件序列（[events.py](../../backend/app/harness/events.py)）：
`session_start → trace_start → (message_start → message_delta* → message_end) 与 (tool_start/tool_end)* 交替 → run_end`；异常时发 `error` 事件。

### 2. RAG 知识入库

```
POST /knowledge-bases/{kb_id}/documents/upload (multipart)
  → KnowledgeService.upload_document()
    → 校验扩展名(.md/.txt/.pdf/.docx)/大小 → files.save_upload() 落盘
    → 创建 DocumentMeta(parse_status=pending)
    → _schedule_parse(doc_id) 创建后台 asyncio task
  → _run_parse(): AsyncSessionLocal 中执行 ingest.parse_and_ingest()
    → parse_status=parsing
    → _resolve_text(): GLM_FILES_API_KEY 有值 → glm_parser.parse_file()
                       (create_parse_task → poll_parse_status 轮询至完成)
                       否则 → local_parser.parse_local()(pypdf/python-docx)
    → chunker.chunk_text(): Markdown 标题层级分块, 超长回退固定长度+重叠
    → embedder.embed_texts(): 批量调 /embeddings(BGE-M3), 校验 1024 维
    → 写入 knowledge_chunks(embedding 向量列), 更新 KB doc/chunk 统计
    → parse_status=parsed / failed(记录 error)
```

### 3. RAG 检索（RAG-as-Tool）

```
Agent ReAct 循环决定调用 knowledge_search(query, knowledge_base?, top_k)
  → tools/knowledge_search._knowledge_search_async()
    → get_tool_context() 取 tenant/user/kb 白名单(防越权二次校验)
    → KnowledgeService.retrieve(): embedder.embed_query() → 1024 维向量
    → retriever.retrieve(): SQL 按 tenant_id + enabled + kb_ids 过滤,
      ORDER BY embedding <=> CAST(:qvec AS vector) LIMIT top_k  (余弦距离)
    → hits_to_markdown(): 生成带引用锚点的结果文本返回给 LLM
  → 回答时消息气泡展示检索来源(SourceReferenceBadge)
```

### 4. 长期记忆

```
提取: 对话结束 → MemoryService.extract_from_conversation()
  → memory/extractor.extract(): LLM 从对话提炼 JSON 数组
    (content/memory_type=semantic|episodic/confidence/source_summary)
  → 逐条 _consolidate_one():
      _retrieve_similar() 找相似记忆 → hygiene.resolve_duplicate()
        相似度 ≥ MEMORY_SIM_DUP(0.92) → NOOP(去重)
        ≥ MEMORY_SIM_CONFLICT(0.70) → CONFLICT → _supersede_and_update()
        否则 → _insert(); confidence < 0.6 → status=pending

注入: 对话开始 → retrieve_relevant()
  → embed_query → SQL(tenant+user+status=active)
  → final_score = similarity × recency_weight(时间加权) → top_k
  → format_for_prompt() 注入 system prompt

维护: cleanup.start_ttl_scan() 周期后台扫描
  → hygiene.expire_stale(): 超过 TTL 且 access_count
    < MEMORY_STRENGTHEN_THRESHOLD(3) 的记忆置 expired
```

### 5. 可观测采集

```
请求进入 → tracing.start_root_span() (采样决策在根 Trace 统一传播)
  → GraphRuntime 维护 run_id→span_id 映射, 按 parent_ids 挂父子链
  → 每次 LLM 调用独立 llm span(token/ttft/模型名/finish_reason)
  → 工具调用 tool span(入参脱敏 redact_object, 出参摘要 200 字)
  → root_span.finalize() → 异步批量写 agent_traces + agent_spans
审计: audit.audit() 队列化写 audit_logs(队列满降级同步写)
聚合: aggregation.start_metric_worker() 后台
  → _run_hourly_aggregation(): 按小时桶聚合 agent_metrics_hourly
  → _run_aggregation(): root span 聚合到 agent_metrics_daily
  → _run_trace_summary(): 为缺失 trace 生成汇总行
  → _run_cleanup(): 按 retention 天数清理过期 span/trace/log/metric
SLO: AlertRule 阈值(错误率/P95/TTFT/成本/工具错误率) → AlertIncident
     (pending → firing → acknowledged → resolved)
```

## 应用启动生命周期（main.py lifespan）

```
1. _validate_config()      校验 LLM_API_KEY / API_KEYS（prod 缺失直接拒绝启动）
2. create_tables()         建表 + pgvector 扩展 + HNSW 索引 + 旧表迁移
3. tool_registry.register_builtin()   注册内置工具(calculator/echo/get_time
                                      /knowledge_search/memory_search)
4. skill_manager.discover(db)         扫描内置+外部 SKILL.md，同步 skills 表
5. agent_registry.register(build_demo_subagent())   注册示例子 Agent
6. get_checkpointer()      初始化 AsyncPostgresSaver（失败降级 None）
7. harness_app.build(tool_registry, checkpointer)
                          build_supervisor_graph() → 编译 LangGraph → GraphRuntime
8. init_tracing()/init_audit()       初始化观测采集器（注入 session 工厂）
9. start_ttl_scan()        启动记忆 TTL 后台扫描
10. start_metric_worker()  启动指标聚合/清理后台任务（OBSERVABILITY_ENABLED 时）
──── yield（服务运行）────
关闭: stop_metric_worker → stop_ttl_scan → shutdown_tracing(flush ≤5s)
```

路由挂载：所有 router 挂在 `settings.API_PREFIX`（默认 `/api/v1`）下；CORS 在非 prod 环境放开 `*`，prod 环境白名单为空（需按需配置）。

## 多 Agent 模式

`MULTI_AGENT_ENABLED=false`（默认）时，`build_supervisor_graph()` 直接回退主 Agent 单图；开启后构建 Supervisor 路由图：

```
          ┌────────────┐  route   ┌──────────────┐
user ───► │ supervisor │ ───────► │ sub_<agent>  │ ──┐
          │  (路由节点) │ ◄─────── │ (子 Agent 图) │   │ 汇聚
          └─────┬──────┘  result  └──────────────┘   │
                │ main                                │
          ┌─────▼──────────┐                          │
          │ 主 Agent(ReAct)│ ─────────────────────────┘
          └────────────────┘
```

子 Agent 通过 `agent_registry`（`AgentEntry{name, description, graph}`）注册，Supervisor 依据描述做条件路由到 `sub_<name>` 或回主 Agent。

## HITL（人工介入）

- `GRAPH_INTERRUPT_TOOLS=true` 时图编译带 `interrupt_before=["tools"]`，工具调用前挂起
- 流式结束时若仍有未关闭节点 → run_status 标记 `interrupted`
- 恢复：`POST /sessions/{session_id}/resume` → `GraphRuntime.resume()` 以 `Command(resume=user_input)` 继续执行，新 trace 通过 `links` 关联原 trace
