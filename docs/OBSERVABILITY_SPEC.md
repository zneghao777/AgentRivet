# Agent 可观测体系技术规格书

版本：v2.0.0  
日期：2026-08-20  
状态：待评审  
关联文档：`docs/SPEC.md`、`docs/KB_MANAGEMENT_SPEC.md`、
`docs/MEMORY_SPEC.md`

本文定义 Agent Harness 可观测体系 v2 的产品范围、数据模型、采集链路、
接口、前端信息架构、交互动效、测试和验收标准。v2 的目标不是继续扩充一个
展示型 Dashboard，而是建立一套数据可信、链路可还原、问题可定位、质量可
评估、异常可告警的本地可观测产品。

> **注意：** 本文描述的是目标态规格。当前代码只实现了部分 Dashboard、
> Trace、工具日志和指标能力。所有标记为“必须”的目标都需要通过本规格的
> 端到端验收后，才能视为已完成。

> **兼容说明：** 当前源码注释中的 `OBSERVABILITY_SPEC §...` 编号来自 v1。
> v2 评审通过后，M0 必须统一更新这些引用；在此之前以本文标题和字段名为准，
> 不以旧章节编号判断实现状态。

## 1. 背景与问题定义

当前观测台已经具备基础页面和本地 span 存储，但它仍停留在演示型 MVP。
主要问题不是视觉不够丰富，而是观测数据的覆盖率、准确性和关联性不足，导致
页面即使继续增加图表，也不能可靠回答一次 Agent 调用究竟发生了什么。

### 1.1 当前实现基线

当前代码已经实现以下能力，这些能力作为 v2 的迁移起点保留：

- `/observability` 总览页面；
- `/observability/traces` Trace 列表；
- `/observability/traces/:id` Trace 树和时间线；
- `/observability/tool-logs` 工具调用日志；
- `agent_spans` 和 `agent_metrics_daily` 数据表；
- 本地异步队列写入、基础脱敏、租户过滤和保留期清理；
- 请求量、错误量、Token、P50/P95 延迟和工具分布；
- 观测专项单元测试和前端构建验证。

### 1.2 已确认的正确性缺陷

v2 必须先修复以下缺陷。未修复前，不得将现有指标用于容量、成本或质量决策。

| 编号 | 缺陷 | 影响 |
| --- | --- | --- |
| C-01 | 非流式 `ainvoke` 没有完整 Trace | REST 调用、后台 LLM 调用不可见 |
| C-02 | 所有子 span 都直接挂在根 span 下 | 无法还原 Agent、LLM、工具的真实因果链 |
| C-03 | 多次 LLM 调用的 Token 被后一次覆盖 | 单次 Trace Token 不准确 |
| C-04 | 详情接口同时汇总根和 LLM 子 span Token | Token 可能重复计算 |
| C-05 | 根 span 不存模型，列表却读取根 span 模型 | Trace 列表模型长期为空 |
| C-06 | SSE 指标固定写入 `duration_ms=1` | 延迟指标失真 |
| C-07 | SSE 指标固定按成功写入 | 错误率和成功率失真 |
| C-08 | 子 span 缺少 error/cancel/timeout 闭环 | 失败节点可能残留或显示成功 |
| C-09 | 采样决策没有由根 Trace 统一向下传播 | 可能产生没有根节点的孤儿 span |
| C-10 | 每个 span 单独提交事务，未真正批量写入 | 高并发下写放大明显 |
| C-11 | 多租户每日聚合混在一起后写入首个租户 | 存在指标串租风险 |
| C-12 | 日志、审计和 Trace 没有关联 ID | 无法从错误日志下钻到调用链 |
| C-13 | 页面标注 OpenLLMetry，但没有实际初始化 | 技术描述与实现不一致 |
| C-14 | 现有单测主要插入手工 span | 无法证明 LangGraph 实际事件映射正确 |

### 1.3 专业观测体系必须回答的问题

完成后的系统必须让使用者在一个工作流中回答以下问题：

1. 哪个用户、会话和请求触发了这次运行？
2. 本次运行调用了哪些 Agent、节点、模型、工具、知识库和记忆？
3. 每次 LLM 调用实际使用了什么模型、参数、Prompt 版本和 Token？
4. LLM 返回了什么，为什么随后选择了某个工具？
5. 工具以什么参数调用、返回了什么、是否重试或产生副作用？
6. 时间消耗在哪个步骤，首 Token 延迟是多少？
7. 本次运行和每次 LLM 调用花费了多少成本？
8. 错误发生在哪个 span，对应有哪些应用日志和异常堆栈？
9. 回答质量如何，用户是否满意，哪个 Prompt 或模型版本发生回退？
10. 系统是否正在接近错误率、延迟、成本或质量 SLO？

## 2. 目标、非目标与设计原则

本节定义 v2 的范围边界。实现团队必须优先保证数据正确和链路完整，再处理
视觉呈现和高级分析。

### 2.1 目标

v2 必须实现以下目标：

- 覆盖流式、非流式、HITL、后台任务、记忆和 RAG 的统一 Trace；
- 保存真实父子 span，支持串行、并行、重试和恢复链路；
- 展示每次 LLM、工具、检索、Embedding 和记忆调用；
- 提供可信的 Token、成本、延迟、错误和成功率指标；
- 将结构化应用日志、审计记录和 Trace 关联；
- 支持用户反馈、人工标注、自动评测和版本对比；
- 支持 SLO、告警、异常聚合和数据采集健康监控；
- 提供专业、高信息密度、低干扰、可访问的前端工作台；
- 默认本地存储，保留可选 OTLP 导出能力，不强制依赖外部平台。

### 2.2 非目标

v2 不包含以下内容：

- 不保存模型隐藏思维过程或供应商不可见的内部推理；
- 不将原始 API Key、Authorization Header 或未脱敏隐私信息写入观测库；
- 不自动执行告警后的破坏性操作；
- 不在 v2 首期引入 ClickHouse、Elasticsearch 或外部 Collector 作为必需依赖；
- 不用复杂动画掩盖空数据、错误数据或长时间加载；
- 不复制 Beautiful UI 的演示数据、模拟计时器、视频或分析回调。

### 2.3 设计原则

实现和评审必须遵循以下原则：

1. **数据可信优先。** 任何聚合指标都必须能下钻到原始 Trace 验证。
2. **一次采集，多种视图。** Trace、调用列表、日志和指标共享统一事件模型。
3. **真实因果关系。** UI 不得通过排序伪造父子链路。
4. **默认安全。** 内容采集遵循最小化、脱敏、授权和保留策略。
5. **标准兼容。** 字段命名对齐 OpenTelemetry 和 GenAI 语义约定。
6. **本地可运行。** 没有外部观测平台时，核心能力仍可完整使用。
7. **动效服务状态。** 动画只解释变化、层级和反馈，不承担装饰作用。
8. **渐进披露。** 列表先显示决策信息，原始细节按需展开。

## 3. 产品信息架构

观测台从四个展示页面升级为七个协同工作区。导航名称必须使用稳定、工程化的
术语，避免“智能洞察”“魔法分析”等营销式文案。

| 路由 | 页面 | 核心任务 |
| --- | --- | --- |
| `/observability` | 总览 | 判断系统健康、趋势和待处理异常 |
| `/observability/traces` | 运行记录 | 查找每次 Agent 运行并进入详情 |
| `/observability/traces/:id` | Trace Explorer | 还原单次运行完整链路 |
| `/observability/calls` | 调用分析 | 分析 LLM、工具、检索和 Embedding 调用 |
| `/observability/logs` | 日志 | 搜索结构化日志并关联 Trace |
| `/observability/costs` | 成本 | 按模型、用户、租户和版本分析成本 |
| `/observability/quality` | 质量 | 查看反馈、评分、评测和版本回归 |
| `/observability/alerts` | 告警与规则 | 配置 SLO、告警、采样和保留策略 |

`/observability/tool-logs` 在兼容期保留，并重定向到
`/observability/calls?type=tool`。历史链接不得产生 404。

## 4. 统一观测模型

v2 使用 Trace、Span、Event、Log 和 Metric 五种一等对象。所有对象必须携带
统一关联字段，避免为 Dashboard、工具日志和审计分别维护不同口径。

### 4.1 对象关系

以下关系定义一次运行内各类观测对象的归属和下钻方向。

```text
Trace: 一次完整 Agent 运行
  ├─ Span: agent / workflow / node / llm / tool / retriever / memory / embedding
  │    ├─ Event: retry / first_token / cache_hit / interrupt / resume / error
  │    └─ Log: 与当前 trace_id + span_id 关联的结构化日志
  ├─ Score: 用户反馈、人工标注或自动评测分数
  └─ Metric: 从 Trace 和 Span 聚合出的时间序列
```

### 4.2 Trace 状态机

Trace 和 Span 必须使用同一组受控状态：

```text
queued → running → ok
                 ↘ error
                 ↘ timeout
                 ↘ canceled
                 ↘ interrupted → running → ok|error|timeout|canceled
```

状态必须单向推进。重复结束事件必须幂等，不能覆盖首个终态。客户端断连记录为
`canceled`，服务端超时记录为 `timeout`，HITL 暂停记录为 `interrupted`。

### 4.3 Span 类型

Span 类型用于统一采集、筛选、着色和聚合口径。

| 类型 | 说明 | 必须采集的示例 |
| --- | --- | --- |
| `agent` | 一次 Agent 运行或子 Agent 委派 | 主 Agent、Supervisor、子 Agent |
| `workflow` | LangGraph 或业务工作流 | graph invoke、resume |
| `node` | 图节点 | agent node、tools node、router node |
| `llm` | 模型调用 | chat、completion、judge |
| `tool` | 工具调用 | knowledge_search、memory_search |
| `retriever` | 文档检索或 rerank | vector search、reranker |
| `embedding` | Embedding 调用 | 文档和查询向量化 |
| `memory` | 记忆检索、提炼、写入 | retrieve、extract、merge |
| `hitl` | 人工审批和恢复 | interrupt、decision、resume |
| `internal` | 其他可观测内部步骤 | context build、prompt render |

### 4.4 统一关联字段

每个 Trace、Span、Log 和 Score 必须尽可能包含以下字段：

- `trace_id`、`span_id`、`parent_span_id`；
- `request_id`、`session_id`、`thread_id`；
- `tenant_id`、`user_id`；
- `environment`、`service_name`、`service_version`、`instance_id`；
- `agent_name`、`agent_version`、`workflow_name`；
- `prompt_name`、`prompt_version`；
- `model_provider`、`model_name`；
- `tags`、`metadata`；
- `started_at`、`ended_at`、`duration_ms`、`status`。

## 5. 采集架构

采集层必须覆盖真实执行路径，并在进入业务运行时之前创建 Trace 上下文。不得依赖
前端推断链路，也不得只在成功结束后补写一条汇总记录。

### 5.1 总体链路

采集链路从请求入口贯穿到本地存储，并保留可选标准导出路径。

```text
HTTP/SSE/Background Job
  → Observability middleware 创建 request_id 和 TraceContext
  → LangGraph / LLM / Tool / Retriever / Memory instrumentation
  → 内存事件缓冲区
  → 脱敏与字段标准化
  → 有界批量队列
  → Postgres 本地存储
  → 小时/日聚合、评测、告警
  → Observability API
  → Web Console
  ↘ 可选 OTLP exporter
```

### 5.2 Trace 上下文

新增 `TraceContext`，并通过 `contextvars.ContextVar` 在异步调用中传播：

```python
class TraceContext:
    trace_id: str
    current_span_id: str
    request_id: str
    tenant_id: str
    user_id: str | None
    session_id: UUID | None
    sampled: bool
    content_capture_mode: str
```

采样决策必须在根 Trace 创建时只执行一次，所有子 span 继承该结果。后台任务创建
新的 Trace，但必须通过 `links` 关联原始对话 Trace。

### 5.3 LangGraph 事件映射

`astream_events(version="v2")` 的 `run_id`、`parent_ids` 和事件名是链路映射的
唯一事实来源。实现必须维护 `run_id → span_id` 映射，按最近父 `run_id` 计算
`parent_span_id`，不得把所有子 span 直接挂到根节点。

| LangChain/LangGraph 事件 | Span 行为 |
| --- | --- |
| `on_chain_start` | 创建 `workflow` 或 `node` span |
| `on_chat_model_start` | 创建 `llm` span，保存模型和参数 |
| `on_chat_model_stream` 首个非空 chunk | 记录 `first_token` event 和 TTFT |
| `on_chat_model_end` | 写入响应、usage、finish reason，结束 `llm` span |
| `on_tool_start` | 创建 `tool` span，保存脱敏结构化参数 |
| `on_tool_end` | 保存结果摘要和副作用标识，结束 `tool` span |
| `on_retriever_start/end` | 创建和结束 `retriever` span |
| `on_*_error` | 写异常事件并以 `error` 结束对应 span |
| interrupt | 创建 `hitl` span，并将 Trace 标记为 `interrupted` |
| resume | 创建关联 span，继续原 Trace 或通过 link 连接恢复 Trace |

### 5.4 覆盖矩阵

以下入口全部必须纳入观测。任一入口缺失都视为 Trace 覆盖不完整。

| 执行入口 | 根 Span | 子 Span | Token/成本 | 日志关联 |
| --- | --- | --- | --- | --- |
| SSE 对话 | 必须 | 必须 | 必须 | 必须 |
| REST 非流式对话 | 必须 | 必须 | 必须 | 必须 |
| HITL resume | 必须 | 必须 | 必须 | 必须 |
| 记忆检索 | 必须关联 | 必须 | 如调用 LLM 则必须 | 必须 |
| 记忆提炼后台任务 | 独立 Trace + link | 必须 | 必须 | 必须 |
| 知识库检索 | 必须关联 | 必须 | 非 LLM 可为空 | 必须 |
| 文档解析和 Embedding | 独立 Trace + link | 必须 | 必须 | 必须 |
| 自动评测 | 独立 Trace + link | 必须 | 必须 | 必须 |

### 5.5 批量写入与可靠性

写入器必须满足以下要求：

- 每批最多 100 条或最多等待 200ms，任一条件满足即提交；
- 同一批次使用一个数据库事务；
- 队列默认容量 10,000，可配置；
- 队列满时优先保留 error、timeout、canceled 和人工反馈 Trace；
- 被丢弃的 span 必须增加 `observability_spans_dropped_total`；
- 进程关闭时最多等待 5 秒 flush，超时后记录未写入数量；
- 写入失败使用指数退避，最多重试 3 次；
- 所有写入必须幂等，唯一键为 `(trace_id, span_id)`；
- 采集器自身的队列深度、写入延迟和失败率必须进入健康页面。

### 5.6 OpenTelemetry 兼容策略

v2 使用 OpenTelemetry 兼容字段和 ID。项目可以使用原生 OpenTelemetry SDK，
也可以保留本地 recorder，但必须满足相同数据契约。

- 如果启用 OpenTelemetry SDK，必须初始化真实 `TracerProvider` 和 exporter；
- 如果没有初始化 SDK，产品文案不得使用“OpenLLMetry 已启用”；
- `traceloop-sdk` 只有在实际初始化并通过集成测试后才能保留为运行依赖；
- 本地 Postgres exporter 是默认路径；
- `OTLP_EXPORT_ENABLED=true` 时可以并行导出，但外部失败不得阻塞主请求；
- 自定义属性必须使用 `app.*` 命名空间，避免污染标准字段。

## 6. LLM 调用观测

LLM span 是成本和质量分析的最小单位。每次模型调用必须独立记录，根 Trace 只保存
汇总值，禁止把最后一次调用值覆盖为全 Trace 值。

### 6.1 模型与请求字段

每个 `llm` span 必须记录：

- `model_provider`：OpenAI、DeepSeek、Anthropic、local 等；
- `model_name_requested` 和 `model_name_resolved`；
- `base_url_host`，不得保存 URL 中的凭据；
- `temperature`、`top_p`、`max_tokens`、`stop`；
- `streaming`、`response_format`、`tool_choice`；
- `provider_request_id`、`finish_reason`；
- `retry_count`、`rate_limit_wait_ms`；
- `prompt_name`、`prompt_version`、`prompt_hash`；
- 脱敏后的 input/output 内容或摘要；
- TTFT、总耗时和流式持续时间。

### 6.2 Token 字段

Token 使用统一字段，适配不同供应商的可选用量：

```json
{
  "input": 1200,
  "output": 340,
  "total": 1540,
  "cached_input": 800,
  "cache_write": 0,
  "reasoning": 120
}
```

`total` 必须由供应商返回值优先确定；供应商不返回时才使用已验证 tokenizer 推算，
并将 `token_usage_estimated=true`。Trace 总量只汇总 `llm` span，不与根 span
重复相加。

### 6.3 Prompt 和响应内容

产品必须支持三种内容采集模式：

| 模式 | 行为 | 默认环境 |
| --- | --- | --- |
| `metadata_only` | 只保存长度、角色、hash 和字段名 | 生产默认 |
| `redacted` | 保存经过规则脱敏和截断的结构化内容 | 开发和受控生产 |
| `full` | 保存完整内容，要求显式授权、加密和审计 | 临时诊断 |

界面必须明确显示当前采集模式。没有权限时显示“内容未采集”或“内容已脱敏”，不能
用空字符串让用户误以为模型没有输入或输出。

### 6.4 成本计算

成本必须按价格版本计算，不能将供应商当前价格回填到历史调用。

```text
input_cost = billable_input_tokens × input_price_per_million / 1,000,000
cached_cost = cached_input_tokens × cached_input_price_per_million / 1,000,000
output_cost = output_tokens × output_price_per_million / 1,000,000
total_cost = input_cost + cached_cost + output_cost + provider_surcharges
```

每个 span 保存 `price_version_id`、币种和分项成本。未知模型显示“价格未配置”，
不得按 0 成本统计。

## 7. 工具、RAG 和记忆观测

工具和上下文调用必须提供足够的调试信息，同时避免把敏感参数无条件落库。

### 7.1 工具调用

每个 `tool` span 必须记录：

- 工具名、工具版本、来源和 schema hash；
- 脱敏后的结构化参数，而不只是参数 key；
- 输出类型、输出大小、脱敏摘要和结果状态；
- 重试次数、超时、异常类型和短错误栈；
- 是否需要审批、审批人、审批决定和等待时间；
- 是否产生外部副作用；
- 幂等键和外部资源 ID，但不得保存凭据。

工具参数必须按 JSON Path 配置脱敏规则。密码、Token、密钥和认证头必须删除，
邮箱、手机号和身份证类字段必须按策略遮罩或 hash。

### 7.2 RAG 与知识库

每个 `retriever` span 必须记录：

- 查询原文摘要、查询改写版本和 Embedding 模型；
- 知识库 ID、过滤条件、`top_k` 和阈值；
- 检索耗时、候选数量、最终命中数量；
- 每个命中文档的文档 ID、chunk ID、分数、排名和 rerank 分数；
- 进入 Prompt 的上下文长度和被截断数量；
- 最终回答引用了哪些 chunk；
- 无结果、低分结果和引用错误的诊断标志。

### 7.3 长期记忆

每个 `memory` span 必须记录：

- 操作类型：search、inject、extract、merge、delete；
- 命中的记忆 ID、类型、分数和注入数量；
- 被注入 Prompt 的字符数或 Token 数；
- 提炼模型、候选条数、接受条数和冲突条数；
- 用户关闭记忆或策略跳过的原因；
- 关联的原始 Trace 和后台提炼 Trace。

## 8. 结构化日志与审计

应用日志、审计日志和 Trace 必须在同一界面中关联，但三者语义不能混为一谈。
日志用于运行诊断，审计用于记录谁做了什么，Trace 用于还原请求链路。

### 8.1 日志模型

新增 `observability_logs`，字段至少包括：

| 字段 | 说明 |
| --- | --- |
| `timestamp`、`observed_at` | 事件时间和采集时间 |
| `severity_text`、`severity_number` | DEBUG、INFO、WARN、ERROR、FATAL |
| `body` | 结构化或文本日志主体 |
| `trace_id`、`span_id` | 请求和具体步骤关联 |
| `request_id`、`session_id` | HTTP 和会话关联 |
| `tenant_id`、`user_id` | 权限隔离 |
| `logger_name`、`module`、`function` | 代码来源 |
| `service_name`、`instance_id`、`environment` | 运行实例 |
| `exception_type`、`exception_message`、`stacktrace` | 异常信息 |
| `attributes` | 脱敏后的扩展属性 |

日志中间件必须自动注入当前 `TraceContext`。日志页面点击任一有关联 ID 的记录，
可以在新页或侧栏打开对应 Trace 和 Span。

### 8.2 审计日志

现有 `audit_logs` 保留，并增加 `trace_id`、`request_id` 和 `resource_id`。审计查询
必须强制使用当前租户，只有平台管理员角色可以跨租户查询。

以下动作必须审计：

- 查看完整 Prompt 或工具参数；
- 导出 Trace、日志和评分；
- 修改内容采集、采样、保留期和告警规则；
- 删除或恢复观测数据；
- 修改模型价格；
- 添加、修改或删除人工评分。

## 9. 数据模型

v2 将 Trace 汇总与 Span 明细分表，避免每次列表查询都扫描根 span 和所有子节点。
数据库迁移必须保留现有 `agent_spans` 数据。

### 9.1 `agent_traces`

该表保存一次运行的列表和聚合字段。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `trace_id` | String PK | 32 位 Trace ID |
| `request_id` | String | HTTP 请求 ID |
| `tenant_id`、`user_id` | String | 权限归属 |
| `session_id`、`thread_id` | UUID/String | 会话关联 |
| `name` | String | `agent.run` 等 |
| `status` | String | 统一状态机 |
| `model_names` | JSON | 本次运行涉及的模型集合 |
| `span_count`、`llm_calls`、`tool_calls` | Integer | 调用数量 |
| `token_usage` | JSON | 只由 LLM 子 span 聚合 |
| `cost` | Numeric | Trace 总成本 |
| `duration_ms`、`ttft_ms` | Integer | 性能 |
| `input_preview`、`output_preview` | Text | 按采集策略保存 |
| `prompt_versions` | JSON | 使用的 Prompt 版本 |
| `tags`、`metadata` | JSON | 检索和分组字段 |
| `error_type`、`error_message` | Text | 根错误摘要 |
| `started_at`、`ended_at` | DateTime | 时间范围 |
| `sampled`、`content_capture_mode` | Boolean/String | 采集策略 |

索引必须包含：

- `(tenant_id, started_at DESC)`；
- `(tenant_id, status, started_at DESC)`；
- `(tenant_id, session_id, started_at DESC)`；
- `(tenant_id, user_id, started_at DESC)`；
- `model_names` 和 `tags` 的 GIN 索引；
- `trace_id` 唯一索引。

### 9.2 `agent_spans`

在现有字段基础上新增：

- `run_id`、`parent_run_id` 和 `links`；
- `status_code`、`status_message`；
- `model_provider`、`model_name_requested`、`model_name_resolved`；
- `model_parameters`、`finish_reason`、`provider_request_id`；
- `prompt_name`、`prompt_version`、`prompt_hash`；
- `ttft_ms`、`retry_count`；
- `cost_input`、`cost_output`、`cost_total`、`currency`；
- `events` 或独立 `span_events` 表；
- `content_capture_mode`、`redaction_version`；
- 唯一约束 `(trace_id, span_id)`。

### 9.3 其他表

以下配套表承载日志、价格、评分、告警和聚合数据。

| 表 | 用途 |
| --- | --- |
| `observability_logs` | 结构化应用日志 |
| `model_price_versions` | 带生效时间的模型价格 |
| `trace_scores` | 用户、人工和自动评分 |
| `evaluation_runs` | 数据集评测和版本对比 |
| `alert_rules` | SLO 和告警规则 |
| `alert_incidents` | 告警触发、确认和恢复 |
| `observability_saved_views` | 用户保存的筛选与列配置 |
| `agent_metrics_hourly` | 小时聚合 |
| `agent_metrics_daily` | 日聚合 |

### 9.4 保留与删除

默认保留策略如下：

- Trace 和 Span：30 天；
- 结构化日志：14 天；
- 小时聚合：90 天；
- 日聚合：365 天；
- 审计日志：按组织合规策略，默认 365 天；
- 人工评分和评测结果：随数据集生命周期保留。

删除必须按租户执行，并同时处理 Trace、Span、Log 和 Score。聚合表可以保留匿名
统计，但必须去除用户和会话维度。

## 10. 指标、SLO 与告警

指标必须同时反映服务可靠性、模型使用和 Agent 质量。所有指标都必须明确分母、
时间窗口和是否包含采样数据。

### 10.1 核心指标

核心指标按流量、可靠性、性能、成本、质量和采集器健康分组。

| 类别 | 指标 |
| --- | --- |
| 流量 | 请求数、并发运行数、活跃会话、每会话轮数 |
| 可靠性 | 成功率、错误率、超时率、取消率、重试率 |
| 性能 | 总延迟 P50/P95/P99、TTFT、LLM/工具/检索耗时占比 |
| 模型 | input/output/cached/reasoning Token、每请求 Token |
| 成本 | 每请求、每会话、每用户、每模型和每日成本 |
| 工具 | 调用量、成功率、重试率、平均耗时和错误类型 |
| RAG | 命中率、空结果率、分数分布、引用覆盖率 |
| 质量 | 用户好评率、自动评分、人工评分和回归率 |
| 采集器 | 队列深度、丢 span 数、写库失败率和数据延迟 |

### 10.2 默认 SLO

默认规则用于开发环境验证，生产值允许按租户覆盖：

- 15 分钟窗口 Trace 错误率超过 5%；
- 15 分钟窗口 P95 总延迟超过 15 秒；
- 15 分钟窗口 P95 TTFT 超过 3 秒；
- 1 小时成本超过配置预算的 80%；
- 工具错误率超过 10% 且调用数不少于 20；
- 自动质量评分相对 7 日基线下降 15%；
- 采集队列使用率超过 80%；
- 连续 5 分钟没有新数据但请求指标仍在增长。

告警状态使用 `pending → firing → acknowledged → resolved`。规则必须支持静默、
恢复通知和最小触发样本，避免低流量噪音。

## 11. 评测与反馈

可观测体系必须把“运行是否成功”和“回答是否有用”分开。HTTP 200 或 Trace `ok`
不能代表回答质量合格。

### 11.1 评分模型

`trace_scores` 必须支持：

- 用户反馈：点赞、点踩和可选原因；
- 人工标注：正确性、相关性、安全性和引用质量；
- 规则评测：JSON 格式、引用存在、禁止词和工具使用；
- LLM-as-a-judge：正确性、完整性、忠实度和任务完成度；
- RAG 评测：context relevance、faithfulness 和 citation correctness。

每个评分必须保存名称、值、来源、评测器版本、说明、创建者和时间。自动评测
调用自身也必须产生 Trace 和成本。

### 11.2 数据集和实验

质量页面必须支持从生产 Trace 添加脱敏样本到数据集，并对以下版本做实验对比：

- 模型版本；
- Prompt 版本；
- 工具 schema 版本；
- RAG 参数和 Embedding 模型；
- 记忆开关和注入策略。

实验结果必须同时比较质量、延迟、Token 和成本，不得只展示单一综合分。

## 12. API 规格

所有 API 必须经过 `require_auth`，由后端强制使用当前租户。平台管理员的跨租户
查询必须使用独立权限，不接受普通客户端直接传入任意 `tenant_id`。

### 12.1 总览与指标

总览接口返回页面首屏和多维趋势所需的数据。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/observability/overview` | 健康摘要、趋势、异常和采集状态 |
| GET | `/observability/metrics` | 多指标时间序列 |
| GET | `/observability/breakdowns` | 按模型、工具、用户、版本聚合 |
| GET | `/observability/health` | 队列、写入器、聚合任务健康 |

`/metrics` 必须支持：

- `metric`：可重复传入多个指标；
- `from`、`to` 和 `granularity`；
- `group_by=model|tool|user|prompt_version|status`；
- `filters`；
- 返回数据时标明 `sampled` 和 `estimated`。

### 12.2 Trace

Trace 接口支持从运行列表下钻到时间线、日志和评分。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/observability/traces` | 游标分页列表和聚合字段 |
| GET | `/observability/traces/{trace_id}` | Trace 摘要和 span 树 |
| GET | `/observability/traces/{trace_id}/timeline` | Waterfall 数据 |
| GET | `/observability/traces/{trace_id}/logs` | 关联日志 |
| GET | `/observability/traces/{trace_id}/scores` | 关联评分 |
| POST | `/observability/traces/{trace_id}/scores` | 新增人工评分 |
| POST | `/observability/traces/export` | 按权限导出 |

Trace 列表筛选必须支持：

- 时间、状态、Trace ID、Request ID；
- 会话、用户和租户；
- Agent、模型、工具和知识库；
- Prompt 名称和版本；
- Token、成本、总耗时、TTFT 范围；
- 是否有错误、是否有反馈和评分范围；
- tags、metadata 和全文关键字。

### 12.3 调用、日志、成本和质量

以下接口覆盖 Span 级调用分析和运营类观测能力。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/observability/calls` | LLM、工具、检索、Embedding 调用列表 |
| GET | `/observability/calls/{span_id}` | 单次调用详情 |
| GET | `/observability/logs` | 日志游标分页和筛选 |
| GET | `/observability/costs` | 成本趋势和拆分 |
| GET/POST | `/observability/model-prices` | 模型价格版本 |
| GET | `/observability/quality` | 评分趋势和回归 |
| GET/POST | `/observability/evaluations` | 评测任务 |
| GET/POST | `/observability/alerts` | 告警规则 |
| GET | `/observability/incidents` | 告警事件 |

### 12.4 分页、错误和实时更新

所有大列表和运行中页面必须遵循统一的分页、错误和实时更新约定。

- 大列表必须使用 cursor 分页，默认 50 条，最大 200 条；
- API 必须返回稳定 `next_cursor`，不得用变化中的总量破坏翻页；
- 请求参数错误返回标准 4xx，不得在 HTTP 200 中只放业务 `code=404`；
- Trace 创建后，SSE 必须发送 `trace_start` 事件；
- 如果框架允许，响应头同时返回 `X-Trace-Id` 和 `X-Request-Id`；
- 运行中的 Trace 更新使用 SSE 或 2 秒轮询，完成后停止；
- 时间统一使用 UTC ISO 8601，前端按用户时区显示。

## 13. 前端设计原则

前端必须参考 Beautiful UI 的交互细节，但整体定位是工程观测后台，不是 AI
演示页面。设计评审优先判断信息是否清楚、状态是否稳定、操作是否可预测。

### 13.1 Beautiful UI 参考映射

只选择与真实产品行为匹配的最小组件集：

| Beautiful UI 参考 | 使用位置 | 借鉴内容 | 禁止照搬内容 |
| --- | --- | --- | --- |
| `records-table` | Trace、调用、日志列表 | 稠密表格、列配置、横向滚动 | 演示数据和不相关计算 |
| `filter-table` | 状态筛选 | 状态 chip、筛选后平滑收拢 | 大数据列表逐行长动画 |
| `thinking-state` | Trace span 树 | 展开层级、引导线、状态收敛 | “思考过程”营销文案 |
| `task-rows` | 工具、评测、告警状态 | running/failed/retry/ok 状态语法 | 模拟定时状态 |
| `insight-cards` | 趋势图和异常卡 | 指针 tooltip、系列切换、暗色适配 | 自动轮播和虚构洞察 |
| `loading-state` | 长查询和导出 | 明确动作、已耗时、运行状态 | `Surfer` 视频和娱乐化加载 |

如果后续复制 Beautiful UI 的源代码或实质性样式，必须保留其 MIT License。
当前项目不使用 Tailwind，优先把语义 token 和必要 keyframe 映射到现有
`global.css`，不得直接引入完整上游 CSS reset。

### 13.2 去除不专业和强 AI 模板感

观测台必须遵循以下视觉约束：

- 使用中性灰表面和单一品牌强调色，状态色只表达状态；
- 观测工作区圆角以 8px、10px、12px 为主，不延续全页面 24px、32px 大圆角；
- 表格和详情面板以 1px 分隔线和轻阴影建立层级；
- 不对每个卡片使用毛玻璃、渐变、发光和悬浮上移；
- 不使用大面积彩色渐变背景、霓虹描边、粒子、光斑和装饰性网格；
- 不堆叠胶囊标签。状态、筛选和少量关键维度才使用 pill；
- 不使用机器人头像、星光、魔法棒、emoji 或“AI 正在思考”类文案；
- 不对历史数据使用打字机效果、循环 shimmer 或持续脉冲；
- 不使用自动轮播图；
- 不为零数据生成虚假趋势或模拟运行过程；
- 数字使用 tabular figures，ID、模型名和时间使用等宽字体；
- 页面标题控制在 20px 左右，不使用营销型 hero 区域；
- 单页只保留一个主操作，其余进入二级按钮或更多菜单。

### 13.3 设计 token

观测工作区新增语义 token，映射现有品牌色：

```css
--obs-canvas: #f5f7fa;
--obs-surface: #ffffff;
--obs-surface-subtle: #f8fafc;
--obs-text: #191c1e;
--obs-text-secondary: #535867;
--obs-text-muted: #727687;
--obs-line: #e6e8eb;
--obs-line-strong: #c2c6d8;
--obs-accent: #0050cb;
--obs-success: #008a7e;
--obs-warning: #b26a00;
--obs-danger: #ba1a1a;
--obs-radius-control: 8px;
--obs-radius-card: 10px;
--obs-shadow-card: 0 1px 2px rgba(24, 39, 62, 0.06);
--obs-shadow-overlay: 0 12px 32px rgba(24, 39, 62, 0.14);
```

深色模式必须提供对应语义 token，不能简单对整个页面使用 CSS filter 反色。

## 14. 动效规格

动效必须解释状态变化、空间关系和操作反馈。任何无法明确说明用途的动画都不得
加入。数据刷新时应保持布局稳定，避免卡片和表格反复跳动。

### 14.1 动效 token

所有观测页面必须复用以下时长和缓动，禁止组件自行创造不一致节奏。

| Token | 时长 | 用途 |
| --- | --- | --- |
| `--motion-instant` | 100ms | hover、focus、颜色变化 |
| `--motion-fast` | 160ms | 按钮按压、tooltip、chip |
| `--motion-base` | 240ms | 行展开、侧栏选择、Tab |
| `--motion-slow` | 360ms | 抽屉、面板首次进入 |
| `--ease-out-strong` | `cubic-bezier(0.23,1,0.32,1)` | 展开和高度变化 |
| `--ease-link` | `cubic-bezier(0.16,1,0.3,1)` | 浮层和页面局部进入 |

普通交互动画不得超过 400ms。只有运行中的 spinner 和 shimmer 可以无限循环，
状态结束后必须停止。

### 14.2 页面和导航

页面级动效只提示路由和选择变化，不改变信息布局。

- 路由切换只对内容区执行 160ms opacity，不做整页左右滑动；
- 首次加载使用固定尺寸 skeleton，避免布局位移；
- 返回列表时保留滚动位置、筛选、列宽和选中行；
- 侧栏当前项使用 160ms 背景和文字颜色过渡，不使用发光轨迹；
- 面包屑和标题不做逐字或逐项进入动画。

### 14.3 表格和筛选

表格动效必须保持高信息密度和大数据性能。

- 行 hover 只改变背景色，不执行 `translateY`；
- 行选中使用 100ms 背景和左侧 2px accent 标记；
- 筛选 chip 使用 160ms 颜色、边框和轻微按压反馈；
- 小于 50 行的本地筛选可以使用 240ms grid-row 收拢；
- 服务端分页或虚拟列表不得对每一行 stagger 动画；
- 排序后只更新行位置，不增加弹跳、翻牌或旋转；
- 新到达的运行只做一次 600ms 淡色背景提示，不持续闪烁。

### 14.4 Trace 树

Trace 树参考 `thinking-state` 的展开语法：

- chevron 旋转 180°，时长 240ms；
- 子树使用 `grid-template-rows` 和 opacity，时长 240ms；
- 引导线高度与内容同步，时长不超过 300ms；
- running span 使用低对比 spinner 或 1.4 秒 shimmer 文本；
- span 进入终态后使用 200ms fade 切换为固定状态图标；
- error 状态只出现一次 240ms `pop-in`，随后保持静态；
- 默认只展开关键路径和错误路径，避免首次进入时全树动画；
- “推理”文案改为“执行步骤”或具体节点名，不暗示展示隐藏思维链。

### 14.5 Waterfall 时间线

时间线动效用于表达持续时间、实时进度和当前选择。

- 首次加载时横条可以从起点绘制到实际宽度，最多 300ms；
- 后续实时更新只平滑调整运行中 span 的右边界；
- hover 显示十字线和 tooltip，100ms 淡入；
- 点击 span 后树、时间线和详情面板使用同一选中状态；
- 缩放和平移必须跟随指针，不使用惯性过冲；
- 错误 span 使用静态红色，不做持续呼吸或闪烁。

### 14.6 图表

图表动效必须便于读数，刷新时不得重复制造视觉噪音。

- 首次出现允许 300ms line/bar reveal；
- 数据刷新不得从零重新播放整张图；
- tooltip 跟随指针，离开后 100ms 消失；
- 系列开关使用 160ms opacity，不改变容器高度；
- 不使用自动轮播、自动切换指标和循环脉冲点；
- 异常点可以一次性 240ms `pop-in`，随后保持静态；
- 图表必须提供表格或文本摘要，不能只依赖颜色。

### 14.7 长任务和加载

超过 800ms 的查询、导出和评测显示动作名称及已耗时，参考 `loading-state`。
加载文案必须具体，例如“正在查询 30 天 Trace”，不得只显示“AI 分析中”。

- 800ms 内不显示全局 loader，避免闪烁；
- 800ms 后显示紧凑状态和 elapsed timer；
- 10 秒后显示取消按钮和进度说明；
- 结束后使用 160ms fade 切换内容；
- 不使用视频、娱乐化加载和无意义 shimmer 大面积覆盖。

### 14.8 减少动态效果

在 `prefers-reduced-motion: reduce` 下必须：

- 关闭页面淡入、图表绘制、行收拢、pop-in、shimmer 和旋转 chevron 过渡；
- 保留静态 running 图标和实时文字状态；
- 保留 elapsed timer，但数字变化不做动画；
- 确保被关闭动画的内容立即可见；
- 不以动画作为状态变化的唯一提示。

## 15. 页面详细规格

每个页面都必须提供 loading、empty、error、partial、permission denied 和
stale data 状态。空状态只解释原因和下一步，不使用插画占据主要空间。

### 15.1 总览

总览采用 12 列栅格，默认时间范围为最近 24 小时。顶部保留一个紧凑筛选栏，
包含时间、环境、模型和自动刷新，不使用大型页头。

首屏从上到下包含：

1. 6 个指标卡：请求、成功率、P95、TTFT、Token 和成本；
2. 请求与错误趋势；
3. 延迟分位和成本趋势；
4. 模型和工具拆分；
5. 活跃告警；
6. 最近异常 Trace；
7. 采集健康状态。

指标卡必须显示当前值、对比窗口、变化量和数据口径。没有上期数据时显示
“无对比数据”，不能显示 0% 变化。

### 15.2 运行记录

运行记录使用 `records-table` 风格的高密度表格，不再使用大卡片列表。

默认列顺序为：

```text
时间 | 状态 | 会话/输入摘要 | 模型 | 耗时 | TTFT | Token | 成本 |
LLM | 工具 | 评分 | 环境 | Trace ID
```

必须支持：

- 固定表头、列宽调整、列显示配置和横向滚动；
- 服务端筛选、排序、cursor 分页和保存视图；
- 点击行打开详情；
- `Cmd/Ctrl + Click` 新标签打开；
- 键盘上下选择、Enter 打开和 Escape 清除选择；
- 批量导出必须经过权限和二次确认；
- 运行中、错误、有评分和内容已脱敏状态必须清晰可见。

### 15.3 Trace Explorer

桌面端使用三栏可调布局：

```text
┌──────────────────┬────────────────────────────┬───────────────────┐
│ Span tree        │ Waterfall / conversation   │ Span detail       │
│ 280–420px        │ flexible                   │ 360–520px         │
└──────────────────┴────────────────────────────┴───────────────────┘
```

顶部摘要栏展示状态、时间、用户、会话、模型集合、Token、成本、耗时、评分和
Trace ID。摘要栏固定在内容顶部，但不得遮挡键盘焦点。

左栏必须支持：

- 真实父子树、关键路径、错误路径和搜索；
- 按类型显隐；
- 一键展开错误路径和折叠成功节点；
- running、interrupted、retry 和 error 状态；
- 虚拟化或分段渲染超大 Trace。

中栏提供以下 Tab：

- **Waterfall**：时间、并行和关键路径；
- **Conversation**：脱敏后的消息、LLM 和工具交替记录；
- **Logs**：当前 Trace 的关联日志；
- **Scores**：用户、人工和自动评分。

右栏按 span 类型展示结构化信息：

- Overview：状态、耗时、父节点和 metadata；
- Input：Prompt、工具参数、检索查询；
- Output：模型响应、工具结果、命中文档；
- Model：模型、参数、Token、cost、finish reason；
- Errors：异常、短堆栈、重试和关联日志；
- Raw：有权限时查看脱敏 JSON。

### 15.4 调用分析

调用分析默认展示所有 span 类型，并提供 `LLM`、`Tool`、`Retriever`、
`Embedding` 和 `Memory` 快速筛选。

- LLM 表：模型、Prompt 版本、TTFT、Token、成本、finish reason；
- Tool 表：工具、参数摘要、状态、重试、耗时、副作用；
- Retriever 表：知识库、top_k、命中、分数、耗时、引用；
- Embedding 表：模型、批量大小、维度、Token、耗时；
- Memory 表：操作、候选、注入、接受、冲突和耗时。

展开行只显示短摘要，完整内容进入右侧详情抽屉，避免表格行高度失控。

### 15.5 日志

日志页面采用时间倒序的虚拟化表格，默认过滤 INFO 以上级别。

默认列为：时间、级别、服务、模块、消息、Trace、Span、实例。页面必须支持：

- severity、服务、模块、实例、Trace 和时间筛选；
- 关键字和结构化属性查询；
- 自动换行开关；
- 实时 tail 开关，用户滚动后自动暂停；
- 展开异常堆栈；
- 从 Trace/Span ID 跳转；
- 内容复制和按权限导出；
- 相同错误指纹聚合。

### 15.6 成本

成本页面必须同时显示金额和 Token，避免只看金额而无法解释原因。

- 总成本、每请求成本和预算使用率；
- 按模型、Prompt 版本、用户、租户和环境拆分；
- input/output/cache/reasoning 成本堆叠；
- 未配置价格的调用数量和影响范围；
- 价格版本变更历史；
- 点击任何聚合项下钻到对应 Trace；
- 预算阈值和告警入口。

### 15.7 质量

质量页面展示用户反馈、人工评分、自动评测和实验结果。

- 评分趋势和样本量；
- 质量、延迟和成本的多目标对比；
- 按模型和 Prompt 版本分组；
- 低分 Trace 队列；
- 标注任务和评测任务状态；
- 数据集样本、运行版本和回归详情；
- 一键进入 Trace Explorer 定位低分原因。

### 15.8 告警与规则

告警页面分为 **事件**、**规则**、**采样**、**保留期** 和 **数据健康**。

- 规则创建使用明确表单，不使用对话式生成作为唯一入口；
- 危险修改需要确认；
- 事件展示 firing、acknowledged 和 resolved；
- 每个告警必须展示触发指标、阈值、窗口、当前值和相关 Trace；
- 采样策略必须显示预计保留比例；
- 数据健康显示队列、写入延迟、丢弃数和最近聚合时间。

## 16. 响应式与可访问性

观测台优先面向桌面工程场景，但必须在较窄窗口下保持可用。

### 16.1 响应式

布局按可用宽度逐步折叠，同时保留核心查询和下钻能力。

- `≥1440px`：Trace Explorer 三栏完整显示；
- `1200–1439px`：右侧详情可折叠为抽屉；
- `960–1199px`：树和时间线使用 Tab，表格保持横向滚动；
- `<960px`：提供只读单栏降级，不强制保持当前 `body min-width: 960px`；
- 所有面板必须设置合理最小宽度，不能将字段压缩成不可读碎片；
- 用户调整的列宽和面板宽度保存在本地偏好中。

### 16.2 可访问性

所有页面和动效必须满足键盘、屏幕阅读器和低动态偏好要求。

- 所有交互元素必须可通过键盘访问；
- 使用 `aria-expanded`、`aria-selected`、`aria-live` 和语义表格；
- focus ring 对比度必须满足 WCAG AA；
- 状态不能只靠颜色，必须同时提供文字或图标；
- 图表必须有文本摘要或表格替代；
- Tooltip 内容必须能通过键盘触发；
- 抽屉打开后管理焦点，关闭后焦点返回触发元素；
- 颜色对比度、200% 缩放和减少动态效果必须纳入验收。

## 17. 安全、权限与隐私

观测数据可能包含用户输入、模型输出、工具参数和检索内容，必须按敏感数据处理。

### 17.1 权限

定义以下角色：

| 角色 | 权限 |
| --- | --- |
| `observability_viewer` | 查看摘要、指标和脱敏内容 |
| `observability_debugger` | 查看允许的详细输入输出和日志 |
| `observability_admin` | 管理采样、保留、价格和告警 |
| `platform_admin` | 经审计的跨租户访问 |

后端必须逐接口和逐字段鉴权。隐藏按钮不是权限控制。

### 17.2 脱敏

脱敏管线按以下顺序执行：

1. 删除明确密钥字段；
2. 对结构化 JSON Path 应用规则；
3. 检测常见 PII；
4. 按模式遮罩、hash 或删除；
5. 执行长度限制；
6. 写入 `redaction_version`；
7. 在 UI 标记字段是否被截断或脱敏。

错误堆栈必须移除环境变量值、连接串和请求凭据。完整内容模式必须加密存储，
查看行为必须审计。

## 18. 性能与容量要求

观测体系不得显著拖慢 Agent 主链路，也不得因自身故障阻塞业务响应。

- 采集入队同步开销 P95 小于 2ms；
- 默认批量模式下主请求额外 P95 延迟小于 5ms；
- Trace 列表 API P95 小于 500ms；
- Trace 详情在 500 span 内 P95 小于 800ms；
- 日志列表 P95 小于 800ms；
- Dashboard 首屏可交互时间在本地网络下小于 2 秒；
- 单 Trace 支持至少 5,000 span，超过阈值采用分段加载；
- 10 万行以上日志必须使用服务端分页和前端虚拟化；
- 图表默认最多返回 1,000 个点，后端按像素级别降采样；
- 观测数据库故障时，业务请求继续执行并增加采集失败指标。

## 19. 测试策略

测试必须验证真实执行链路，不得只通过手工构造数据库记录证明实现正确。

### 19.1 单元测试

单元测试覆盖采集器内部状态、数据标准化和安全边界。

- TraceContext 传播和根级采样；
- `run_id`、`parent_ids` 到 span 树映射；
- 状态机、幂等结束和异常收尾；
- 多次 LLM Token 汇总和去重；
- 模型、参数、finish reason 和价格版本提取；
- 结构化参数脱敏和 PII 规则；
- 批量、重试、队列溢出和 shutdown flush；
- 多租户聚合隔离；
- 权限和字段级隐藏。

### 19.2 集成测试

必须使用可控的 fake LLM 和 fake tool 运行真实 LangGraph：

1. 单次 LLM 直接回答；
2. LLM → Tool → LLM；
3. 连续两个 Tool；
4. Tool error → retry → success；
5. LLM error；
6. timeout 和客户端断连；
7. HITL interrupt → resume；
8. 非流式 `ainvoke`；
9. 记忆提炼后台 Trace link；
10. RAG 检索、Embedding 和引用。

每个场景必须断言真实父子结构、时间顺序、状态、Token、成本、日志和租户归属。

### 19.3 前端测试

前端测试必须验证真实交互状态、性能和可访问性，而不只验证组件渲染。

- 列表筛选使用最新状态，不允许出现 React stale state；
- 返回列表保留筛选和滚动位置；
- Trace 树、Waterfall 和详情选择同步；
- 运行中 Trace 增量更新后布局稳定；
- 大 Trace 和大日志列表性能；
- loading、empty、error、partial 和权限状态；
- 键盘操作、focus、屏幕阅读器标签；
- light、dark 和 `prefers-reduced-motion`；
- 视觉回归覆盖 1440px、1200px、960px 和窄屏降级。

### 19.4 数据准确性测试

构造已知 Token、价格和耗时的运行样本，必须满足：

- Trace Token 等于所有 LLM span Token 之和；
- Trace 成本等于所有 LLM span 成本之和；
- 根 span 汇总字段不参与二次汇总；
- P50/P95/P99 使用统一算法；
- SSE 和非流式相同业务路径产生相同观测结构；
- 采样关闭时仍保留错误计数但不产生孤儿 span；
- 多租户数据和聚合完全隔离。

## 20. 迁移计划

迁移按数据正确性优先执行。每个阶段必须独立可验收，不能依赖后续 UI 掩盖缺口。

### 20.1 M0：基线冻结

本阶段冻结现状并建立能够复现当前缺陷的测试基线。

- 为当前行为补充失败用例；
- 记录现有数据模型和 API 响应；
- 为旧路由和字段建立兼容清单；
- 将页面“OpenLLMetry”文案改为实际采集方式。

### 20.2 M1：采集正确性

本阶段先让全链路数据可靠，再扩展数据表和页面。

- 引入 TraceContext 和统一状态机；
- 覆盖 SSE、REST、resume 和后台任务；
- 修复父子链路、Token、模型、错误、采样和批量写入；
- 新增真实 LangGraph 集成测试；
- 完成多租户聚合隔离。

### 20.3 M2：数据与 API

本阶段建立支持专业查询、成本、日志和评分的数据底座。

- 新增 `agent_traces`、日志、价格、评分和小时聚合表；
- 提供 cursor 分页、复杂筛选和关联查询；
- 完成内容采集模式、脱敏版本和权限；
- 迁移现有 span，并标记历史数据可信度。

### 20.4 M3：专业 Trace Explorer

本阶段使用可信数据完成运行列表和 Trace 详情的专业化重构。

- 重构运行记录为高密度表格；
- 实现三栏 Trace Explorer、Waterfall、Conversation 和关联日志；
- 按 Beautiful UI 参考实现状态动效；
- 完成 dark、reduced motion、键盘和视觉回归验证。

### 20.5 M4：日志与成本

本阶段打通运行诊断、模型用量和预算管理。

- 上线结构化日志和 Trace 关联；
- 上线模型价格版本和成本页面；
- 增加采集健康和数据新鲜度；
- 完成预算和成本告警。

### 20.6 M5：质量与运营

本阶段补齐质量评估、SLO 和持续运营闭环。

- 上线反馈、评分、数据集和实验；
- 上线 SLO、告警规则和事件；
- 增加失败聚类和低分 Trace 队列；
- 完成容量、故障降级和合规验收。

## 21. 验收标准

以下条件全部满足后，v2 才能标记完成。

### 21.1 数据与链路

数据与链路验收必须基于真实运行结果和可重复断言。

- [ ] SSE、REST、HITL、RAG、记忆和后台任务都有 Trace；
- [ ] LLM → Tool → LLM 显示真实父子链和时间关系；
- [ ] 每次 LLM 显示真实模型、参数、Token、TTFT 和成本；
- [ ] 工具显示脱敏结构化参数、结果、重试和错误；
- [ ] Trace Token 和成本与子 span 汇总完全一致；
- [ ] timeout、cancel、error 和 interrupt 状态准确；
- [ ] 日志可通过 trace_id 和 span_id 双向跳转；
- [ ] 多租户查询、聚合和导出完全隔离；
- [ ] 队列溢出和写入失败可观测。

### 21.2 前端与动效

前端验收必须同时检查信息密度、交互状态、动效节奏和可访问性。

- [ ] 运行记录使用专业高密度表格，不再使用大卡片列表；
- [ ] Trace Explorer 完成树、Waterfall、详情和日志联动；
- [ ] 页面没有自动轮播、娱乐化加载、持续发光或无意义漂浮；
- [ ] hover 不造成布局位移；
- [ ] 所有动效符合 100/160/240/360ms token；
- [ ] `prefers-reduced-motion` 下内容立即可见且功能完整；
- [ ] light、dark、键盘和屏幕阅读器路径通过；
- [ ] 所有空、错、部分成功和权限状态有明确说明；
- [ ] 1440px、1200px、960px 和窄屏降级通过视觉回归。

### 21.3 质量与运营

质量与运营验收必须证明从指标发现问题到 Trace 定位的闭环可用。

- [ ] 用户反馈、人工评分和自动评分可以关联 Trace；
- [ ] 实验同时比较质量、延迟、Token 和成本；
- [ ] 默认 SLO 可以触发、确认和恢复；
- [ ] 任一告警可以下钻到相关 Trace；
- [ ] 内容查看、导出和规则修改都有审计记录；
- [ ] 保留期清理不会产生孤儿数据。

## 22. Definition of Done

任何观测功能只有同时满足以下条件才能合并：

- 代码实现与本规格字段和状态一致；
- 单元、真实链路集成、前端和权限测试通过；
- 数据准确性有确定输入和期望值验证；
- 失败、超时、取消和降级路径经过验证；
- 页面使用真实后端数据，不含 mock timer 和演示状态；
- 动效通过正常模式和减少动态效果验证；
- 新增字段有迁移、索引、保留和脱敏策略；
- API 文档、类型和前端调用同步更新；
- 不将外部平台能力描述为本项目已实现能力；
- 评审者可以从 Dashboard 下钻到 Trace、Span 和关联日志完成一次故障定位。

## 23. 参考依据

本规格结合当前代码审查、行业观测产品能力和开放标准形成。实现时优先遵循标准
语义和本项目真实数据契约，不直接复制外部平台的产品结构。

- [OpenTelemetry GenAI semantic conventions](https://github.com/open-telemetry/semantic-conventions-genai)
  定义 GenAI spans、metrics 和 events 的标准方向；
- [OpenTelemetry logs data model](https://opentelemetry.io/docs/specs/otel/logs/data-model/)
  定义日志时间、级别、TraceId、SpanId、资源和属性；
- [Langfuse observability](https://langfuse.com/docs/observability/overview)
  参考 Trace、成本、评分、Dashboard 和告警能力边界；
- [LangSmith observability](https://docs.langchain.com/langsmith/observability)
  参考 Trace 调查、反馈、自动化和生产监控闭环；
- [Phoenix tracing](https://arize.com/docs/phoenix/tracing/llm-traces)
  参考检索、Embedding、Prompt、工具和函数调用观测字段；
- 本机 `beautiful-ui` skill 的组件目录、集成说明和上游 MIT 资源快照，
  用于前端交互、动效、暗色和减少动态效果规范。

## 24. 后续步骤

规格评审通过后，实施团队按 M0 到 M5 拆分任务。第一批任务只处理采集正确性、
真实 LangGraph 集成测试和多租户隔离，不与大规模前端重构混合提交。完成 M1 并
验证数据可信后，再开始 Trace Explorer 和其他页面实现。
