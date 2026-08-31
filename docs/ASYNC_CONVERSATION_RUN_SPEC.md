# ASYNC_CONVERSATION_RUN_SPEC — 异步并发会话与可恢复运行规范

本文定义 MyAgentDemo1 的异步并发会话能力。目标是让同一用户在不同会话窗口
中并发发起任务，并在切换会话、切换浏览器窗口、刷新页面或暂时断开连接后，
继续查看原任务的状态和结果。本文同时定义数据模型、运行状态机、API、SSE
重连协议、前端状态管理、并发治理、可靠性要求和验收标准。

> 版本：v1.1.0<br>
> 日期：2026-08-31<br>
> 状态：已实现；自动化与真实浏览器验收通过<br>
> 关联文档：
> [`CONCURRENCY_GUARD_SPEC.md`](./CONCURRENCY_GUARD_SPEC.md)、
> [`MQ_INTEGRATION_SPEC.md`](./MQ_INTEGRATION_SPEC.md)、
> [`OBSERVABILITY_SPEC.md`](./OBSERVABILITY_SPEC.md)、
> [`MULTI_AGENT_ORCH_DELIVERY_SPEC.md`](./MULTI_AGENT_ORCH_DELIVERY_SPEC.md)

本版本已经完成 M1、M2 和 M3 的代码交付。生产 RabbitMQ 集群上的 `kill -9`、
五分钟断连和容量压测仍属于部署验收门槛，不由本地自动化结果替代。

## 1. 决策摘要

本规范采用“会话、运行任务、事件订阅”三层分离的设计。浏览器连接只负责
订阅事件，不再拥有任务生命周期。

1. `Session` 是对话容器，`ChatRun` 是一次用户请求的独立运行任务，
   `ChatRunEvent` 是可重放的运行事件。
2. 同一用户可以在不同会话中并发运行任务；同一会话同一时刻最多有一个
   活动任务，以保护 LangGraph `thread_id` 和检查点顺序。
3. 用户切换会话或浏览器窗口时，前端只切换展示对象，不取消原任务。
4. SSE 断连只取消订阅，不取消 `ChatRun`。只有显式调用取消 API 才终止任务。
5. 第一阶段通过会话级流锁和前端按会话运行管理满足跨窗口并发；第二阶段
   增加持久化 `ChatRun` 和事件重放；第三阶段增加独立 Worker、可靠投递和
   服务重启恢复。
6. 现有全局 SSE 闸门、LLM 调用闸门、入口限流、熔断和降级机制继续生效。
7. 现有 `AgentTask` 继续表示多 Agent 编排中的子任务，不承担顶层对话运行
   的职责。

## 2. 规范优先级

本规范修改现有并发防护设计中的会话约束。出现冲突时，以下规则以本文为准，
其他并发防护规则继续以 `CONCURRENCY_GUARD_SPEC.md` 为准。

- 本规范替代“同一用户同时仅允许一个 SSE 对话流”的规则。
- 本规范替代“SSE 断连即取消 LangGraph 运行”的规则。
- 本规范不替代全局流准入、LLM 槽位、排队、入口限流、熔断和降级规则。
- 本规范扩展 `MQ_INTEGRATION_SPEC.md` 的范围，但只有 M3 才允许 RabbitMQ
  承担 `ChatRun` 命令分发。RabbitMQ 不传输 SSE Token 帧。

本文使用以下约束词表达要求。`MUST` 表示实现和验收的硬性要求，`MUST NOT`
表示禁止行为，`建议` 表示推荐方案。

## 3. 实施前基线

当前实现将网络流、前端组件状态和 Agent 运行绑定在同一个生命周期中。这使
单会话流式体验成立，但阻止同一用户使用多个会话并发工作。

### 3.1 后端现状

后端通过用户级租约控制 SSE 流。锁键为
`stream_lock:{tenant_id}:{user_id}`，因此同一用户或同一 API Key 的第二条流
会收到 `42901`。

- `backend/app/harness/stream_gate.py` 提供 `UserStreamLock`。
- `backend/app/api/router_chat.py` 在持久化消息和执行 GraphRuntime 前获取租约。
- 客户端断连后，`router_chat.py` 调用 `mark_cancelled()`，释放流槽和 LLM
  请求，并关闭运行时生成器。
- `STREAM_MAX_CONCURRENCY` 和 `LLM_MAX_CONCURRENCY` 控制系统级容量，能够
  继续用于跨用户和跨会话的全局治理。

### 3.2 前端现状

前端只有一个全局 `messages` 和一个全局 `streaming` 状态。`ChatWindow`
持有唯一的 `AbortController`，会话变化时会中断该控制器。

- `frontend/src/stores/sessionStore.ts` 只保存当前会话的消息和流状态。
- `frontend/src/components/chat/ChatWindow.tsx` 在 `currentSessionId` 变化时
  调用 `abort()`。
- `frontend/src/components/layout/Sidebar.tsx` 在 `streaming=true` 时禁止新建
  会话。
- `frontend/src/pages/ChatPage.tsx` 在会话切换后重新加载历史，并覆盖全局消息
  列表。

### 3.3 持久化现状

当前 `sessions` 和 `messages` 表能够恢复对话历史，Trace 能够回放已落库的
执行步骤，但系统没有表示一次顶层对话运行的持久化实体。

- `Session` 表示一个长期对话，不能表示每次请求的排队、运行或取消状态。
- `Message` 保存最终消息和 Trace 关联，不保存完整的增量事件序列。
- `AgentTask` 保存多 Agent 子任务快照，不覆盖主 Agent、排队、订阅和恢复。
- 当前 SSE 增量没有稳定的事件序号，刷新后不能从断点补播。

## 4. 目标和非目标

本节定义本期必须交付的用户体验，以及明确不在当前范围内的能力。

### 4.1 目标

实现必须满足以下用户和系统目标。

- 同一用户可以在不同会话中并发运行任务。
- 用户在单页应用内切换会话时，原任务继续运行。
- 用户在不同浏览器窗口或标签页中工作时，各会话互不取消。
- 用户返回运行中的会话时，能够看到当前状态、已生成内容和后续增量。
- M2 完成后，刷新页面、关闭浏览器或临时断网不取消任务，并能从事件游标
  恢复订阅，但 API 进程故障恢复不在 M2 承诺内。
- M3 完成后，API 或 Worker 进程重启不丢失已接受任务。
- 显式取消、HITL 审批、错误、超时和完成状态都有一致的 API 与 UI 表达。
- 并发放开后仍满足租户隔离、用户公平性、全局容量保护和可观测要求。

### 4.2 非目标

本规范不改变以下产品和运行边界。

- 不允许同一会话内的多个用户消息无序并发执行。
- 不改变 LangGraph 的主 Agent、工具循环或多 Agent 编排拓扑。
- 不使用 RabbitMQ 传输逐 Token SSE 帧。
- 不保证已发送到不支持取消的上游模型调用能够立即停止。
- 不在本期实现跨地域任务迁移、跨集群事件复制或无限期事件保留。
- 不把浏览器本地状态作为任务运行状态的事实源。

## 5. 核心术语和不变量

实现必须围绕以下术语组织代码和接口。任何优化都不能破坏本节的不变量。

| 术语 | 定义 |
| --- | --- |
| `Session` | 一个有序的多轮对话容器，对应唯一 LangGraph `thread_id` |
| `ChatRun` | 一条用户消息触发的一次顶层 Agent 运行 |
| `ChatRunEvent` | 某个 `ChatRun` 产生的有序、可重放事件 |
| Subscriber | 通过 SSE 查看 `ChatRunEvent` 的浏览器连接 |
| Active run | 状态为 `queued`、`running` 或 `pending_approval` 的运行 |
| Terminal run | 状态为 `succeeded`、`failed` 或 `canceled` 的运行 |

系统必须维持以下不变量。

1. 一个 `Session` 同一时刻最多有一个 Active run。
2. 一个 `ChatRun` 可以有零个、一个或多个 Subscriber。
3. Subscriber 断开不会改变 `ChatRun.status`。
4. 同一 `ChatRun` 在至少一次任务投递下仍只能产生一个逻辑执行结果。
5. `ChatRunEvent.sequence` 在单个 `run_id` 内严格递增且不重复。
6. Assistant 最终消息最多落库一次，并与 `run_id`、`request_id` 和
   `trace_id` 关联。
7. 同一会话的后续运行只能在前一运行进入 Terminal run 后开始。
8. HITL 中断属于 `pending_approval`，不是失败，也不占用运行 Worker。

## 6. 目标架构

目标架构把命令提交、Agent 执行、事件持久化和浏览器订阅拆开。浏览器连接的
变化只影响最后一段订阅链路。

```text
Browser A ─┐
           ├─ POST /sessions/{session_id}/runs ─► ChatRun store
Browser B ─┘                                      │
                                                  ▼
                                      Run coordinator / Worker
                                                  │
                                      GraphRuntime + Checkpointer
                                                  │
                                                  ▼
                                         ChatRunEvent store
                                            │           │
                           SSE subscriber A ◄┘           └► SSE subscriber B
```

组件职责必须按以下方式划分。

- API 负责鉴权、参数校验、幂等受理、状态查询和事件订阅。
- Run coordinator 或 Worker 负责领取 `ChatRun`、执行 GraphRuntime、持久化
  事件和更新终态。
- PostgreSQL 是 `ChatRun`、最终消息和可重放事件的事实源。
- Redis 可以用于会话锁、用户活动运行计数和事件通知；Redis 故障不能导致
  已持久化任务丢失。
- RabbitMQ 在 M3 负责可靠命令投递，不负责存储或转发逐 Token SSE 帧。
- 前端只根据 API 和事件流更新状态，不根据组件是否挂载推断任务是否运行。

## 7. M1：跨窗口并发和无中断切换

M1 以最小运行时改造满足当前明确提出的体验。M1 不承诺刷新、关闭页面或 API
进程重启后的任务延续，这些能力由 M2 和 M3 提供。

### 7.1 会话级流锁

后端必须把用户级单流锁改为会话级单流锁。锁必须同时包含租户、用户和会话
标识，防止不同用户或租户互相影响。

```text
session_stream_lock:{tenant_id}:{user_id}:{session_id}
```

实现必须满足以下要求。

- `UserStreamLock` 重命名为表达准确语义的 `SessionStreamLock`，或新增等价实现。
- `acquire()`、`renew()` 和 `release()` 必须接收 `session_id`。
- Redis `SET NX EX`、所有权校验续期和 compare-and-delete 语义保持不变。
- Redis 不可用时，进程内降级键也必须包含 `session_id`。
- 同一会话获取失败时返回 `40901`，消息为“当前会话已有任务正在进行中”。
- 原 `42901` 在一个兼容版本内保留前端解析，但不再用于不同会话并发限制。

### 7.2 用户并发运行限制

放开不同会话后，系统仍必须限制单个用户同时占用的活动运行数量。这里的
“限制”表示实时数值上限，不替代系统级容量治理。

- 新增 `CHAT_RUN_MAX_ACTIVE_PER_USER`，开发环境默认值为 `3`。
- 用户达到限制后，新运行进入 `queued`，而不是取消已有运行。
- 如果用户排队数量达到 `CHAT_RUN_MAX_QUEUED_PER_USER`，系统返回 `42902`。
- 用户限制按 `tenant_id + user_id` 计算；登录前按认证 API Key 标识计算。
- 管理员可以通过配置调整数值，但不能设置为小于 `1`。
- 全局 `STREAM_MAX_CONCURRENCY` 和 `LLM_MAX_CONCURRENCY` 继续作为系统限制。

### 7.3 前端按会话存储

前端必须把全局消息和流状态改为按会话存储。切换当前会话只改变可见状态，
不能销毁其他会话的数据。

```ts
interface SessionState {
  currentSessionId: string | null;
  messagesBySession: Record<string, ChatMessage[]>;
  runsBySession: Record<string, ChatRunViewState>;
  loadingBySession: Record<string, boolean>;
  errorsBySession: Record<string, string | null>;
}
```

状态更新方法必须显式接收 `sessionId`。禁止通过“最后一条 Assistant 消息”
隐式更新当前会话以外的数据。

### 7.4 前端运行管理器

前端必须新增脱离 `ChatWindow` 生命周期的 `ChatRunManager`。建议把它实现为
模块级服务，并通过 Zustand action 发布状态。

- 每个会话分别保存网络控制器、事件游标和当前运行状态。
- `ChatWindow` 卸载或 `currentSessionId` 变化时不得调用运行控制器的
  `abort()`。
- 只有用户点击 **停止生成** 时才触发取消逻辑。
- **新建会话** 和会话切换不得被其他会话的 `streaming` 状态禁用。
- 活动流完成后，管理器更新对应会话，而不是当前可见会话。
- 同一页面内重复订阅同一 `run_id` 时必须复用已有订阅。
- 页面卸载行为在 M1 可以断开订阅并取消旧式 SSE 运行，但 UI 必须明确 M1
  不提供关闭页面后继续执行的承诺。

### 7.5 会话侧边栏

侧边栏必须显示每个会话的运行状态，使用户在不进入会话时也能判断任务进度。

- `queued` 显示“排队中”。
- `running` 显示动态运行标识。
- `pending_approval` 显示需要处理的审批标识。
- `succeeded` 显示一次性完成提示，用户查看后清除。
- `failed` 显示失败提示和重试入口。
- `canceled` 显示已停止状态，不自动重试。

### 7.6 M1 验收边界

M1 只有在真实浏览器流程通过后才能宣称支持跨窗口并发。

- 窗口 A 的会话 A 和窗口 B 的会话 B 可以同时运行。
- 两个延迟任务的总耗时接近较慢任务，而不是两者耗时之和。
- 在单页应用中从会话 A 切到会话 B 后，会话 A 的后端请求不被取消。
- 返回会话 A 后，已生成内容和后续 Token 继续显示。
- 停止会话 A 不影响会话 B。
- 同一会话在第二个窗口重复提交时收到 `40901`。
- 会话 A 等待工具审批时，会话 B 仍可以正常运行。

## 8. M2：持久化 ChatRun 和可重连事件流

M2 把任务生命周期从浏览器页面中完全解耦。M2 完成后，刷新页面、关闭浏览器、
网络切换或关闭 Subscriber 都不会取消已经受理的任务。M2 的后台执行仍可以
位于 API 进程内，因此不承诺 API 进程故障后的自动恢复。

### 8.1 `chat_runs` 表

`chat_runs` 是顶层对话运行的事实源。实现必须通过正式数据库迁移创建表和
索引，不能只依赖开发环境的 `create_all()`。

| 字段 | 类型 | 约束和用途 |
| --- | --- | --- |
| `id` | UUID | 主键，对外 `run_id` |
| `tenant_id` | String(64) | 非空，租户隔离索引 |
| `user_id` | String(64) | 非空，用户限制和审计索引 |
| `session_id` | UUID | 非空，外键到 `sessions.id` |
| `request_id` | String(128) | 非空，全局请求关联 |
| `client_request_id` | String(64) | 非空，客户端幂等键 |
| `user_message_id` | BigInteger | 触发运行的用户消息 |
| `assistant_message_id` | BigInteger | 终态 Assistant 消息，可空 |
| `status` | String(24) | 运行状态机当前状态 |
| `request_payload` | JSONB | 模型、Agent、Skill、附件和审批模式快照 |
| `trace_id` | String(64) | 可观测 Trace，可空 |
| `last_event_sequence` | BigInteger | 已提交的最大事件序号，默认 `0` |
| `attempt` | Integer | Worker 执行尝试次数，默认 `0` |
| `lease_owner` | String(128) | 当前 Worker 标识，可空 |
| `lease_expires_at` | DateTime | Worker 租约到期时间，可空 |
| `cancel_requested_at` | DateTime | 显式取消请求时间，可空 |
| `error_code` | String(64) | 稳定错误标识，可空 |
| `error_message` | Text | 脱敏错误说明，可空 |
| `created_at` | DateTime | 受理时间 |
| `started_at` | DateTime | 首次开始执行时间，可空 |
| `finished_at` | DateTime | 进入终态时间，可空 |
| `updated_at` | DateTime | 最近状态更新时间 |

数据库必须建立以下唯一性和查询约束。

- 唯一索引：`(tenant_id, user_id, client_request_id)`。
- 查询索引：`(session_id, created_at DESC)`。
- 查询索引：`(tenant_id, user_id, status, created_at)`。
- 部分唯一索引：同一 `session_id` 在 `queued`、`running`、
  `pending_approval` 状态下最多一行。
- Check constraint：`status` 只能使用 §8.3 定义的值。

### 8.2 `chat_run_events` 表

`chat_run_events` 保存客户端需要重放的运行事件。它不替代 Trace Span，也不
保存内部调试对象。

| 字段 | 类型 | 约束和用途 |
| --- | --- | --- |
| `id` | BigInteger | 主键 |
| `run_id` | UUID | 外键到 `chat_runs.id` |
| `sequence` | BigInteger | 单次运行内严格递增 |
| `event_type` | String(64) | SSE 事件名 |
| `payload` | JSONB | 已脱敏的客户端事件数据 |
| `created_at` | DateTime | 事件提交时间 |

实现必须建立唯一索引 `(run_id, sequence)` 和查询索引
`(run_id, sequence ASC)`。事件插入和 `last_event_sequence` 更新必须在同一
数据库事务中提交。

逐 Token 事件可能产生较多记录。实现可以在不改变客户端展示文本的前提下，
把 50 毫秒内或 512 字符以内的连续 `message_delta` 合并为一个事件。

### 8.3 状态机

`ChatRun.status` 必须遵循以下状态机。实现不得直接跳过状态校验更新数据库。

```text
queued ─────────► running ─────────► succeeded
  ▲                 │  └───────────► failed
  │                 │
  │                 └──────────────► pending_approval
  │                                      │
  └──────────────── resume ──────────────┘

queued / running / pending_approval ─────► canceled
```

状态语义必须保持一致。

- `queued` 表示已持久化但尚未由执行器领取。
- `running` 表示执行器持有有效租约并正在推进 GraphRuntime。
- `pending_approval` 表示 LangGraph 已持久化 interrupt，等待用户决定。
- `succeeded` 表示最终 Assistant 消息和终态事件已经提交。
- `failed` 表示运行不可继续，错误已经持久化。
- `canceled` 表示用户显式取消或系统按明确策略取消。

Worker 失去租约不能直接把运行标记为 `canceled`。系统必须根据恢复策略重新
排队，或在尝试次数耗尽后标记为 `failed`。

### 8.4 运行事件协议

SSE 必须使用 `id` 字段承载事件序号。客户端通过 `Last-Event-ID` 或
`after_sequence` 恢复订阅。

```text
id: 121
event: message_delta
data: {"run_id":"...","session_id":"...","sequence":121,
       "content":"继续生成的文本"}
```

所有现有面向 Chat UI 的事件可以继续使用，包括 `trace_start`、
`run_step_start`、`message_delta`、工具事件、多 Agent 编排事件、
`interrupt_requested`、`message_end`、`run_end` 和 `error`。

协议必须新增以下事件。

- `run_snapshot`：订阅建立后的当前完整状态和已聚合文本。
- `run_status`：`queued`、`running`、`pending_approval` 或终态变化。
- `run_reconnected`：客户端完成断点补播并进入实时订阅。

### 8.5 事件通知和补播

PostgreSQL 是事件事实源。实时通知只能提示订阅端“有新事件”，不能成为唯一
事件存储。

- 单实例 M2 可以使用进程内 `asyncio.Condition` 唤醒本地 Subscriber。
- 多实例建议使用 Redis Pub/Sub 发布 `run_id:last_sequence` 通知。
- Redis 不可用时，Subscriber 以可配置间隔轮询 PostgreSQL。
- Subscriber 每次被唤醒后都必须从 PostgreSQL 查询
  `sequence > last_delivered_sequence`，不能直接信任通知载荷。
- 多个 Subscriber 可以同时读取同一运行，不得触发重复执行。

### 8.6 事件保留

事件保留必须可配置，并区分最终消息与临时增量事件。

- `CHAT_RUN_EVENT_RETENTION_DAYS` 默认 `7` 天。
- 终态 Assistant 消息按现有消息保留策略保存，不随运行事件清理。
- 事件清理只能删除 Terminal run 的过期事件。
- `pending_approval` 和 Active run 的事件不得清理。
- 清理任务必须记录删除数量、运行耗时和失败指标。

## 9. API 规范

新 API 使用 `ChatRun` 作为一等资源。现有响应信封
`{"code":0,"message":"ok","data":...}` 保持不变。

### 9.1 创建运行

客户端通过以下端点提交一条新消息并创建运行。

```http
POST /api/v1/sessions/{session_id}/runs
Idempotency-Key: 7df5bba6-5cc7-49e8-b407-d551803e26aa
Content-Type: application/json
```

```json
{
  "message": "分析这份数据并给出结论",
  "approval_mode": "manual",
  "model": "deepseek-chat",
  "agent_id": null,
  "skills": ["data-analysis"],
  "knowledge_bases": ["运营知识库"],
  "attachment_ids": []
}
```

服务必须在一个数据库事务中完成以下操作。

1. 校验租户、用户、会话和能力范围。
2. 检查同一会话是否存在 Active run。
3. 使用 `Idempotency-Key` 查找或创建 `ChatRun`。
4. 持久化用户消息和不可变 `request_payload` 快照。
5. 提交 `queued` 状态和初始事件。
6. 返回 HTTP `202 Accepted`。

响应数据必须至少包含以下字段。

```json
{
  "run_id": "0b0d31e5-73a2-4f0a-96d0-f4c3ac77fe80",
  "session_id": "a7d8fca1-0bbf-4ec8-bf06-c9dc1788aa5e",
  "status": "queued",
  "created_at": "2026-08-31T12:00:00Z",
  "events_url": "/api/v1/runs/0b0d31e5-73a2-4f0a-96d0-f4c3ac77fe80/events"
}
```

### 9.2 查询运行

客户端通过以下端点查询当前状态。服务必须校验运行所属租户和用户。

```http
GET /api/v1/runs/{run_id}
GET /api/v1/sessions/{session_id}/active-run
GET /api/v1/sessions/{session_id}/runs?page=1&page_size=20
```

`GET /runs/{run_id}` 必须返回状态、已聚合文本、Trace、错误、Token 用量、
时间戳和最后事件序号。`active-run` 在没有 Active run 时返回 `data=null`。

### 9.3 订阅事件

客户端通过以下端点订阅运行事件。断开该端点不能触发运行取消。

```http
GET /api/v1/runs/{run_id}/events?after_sequence=120
Accept: text/event-stream
Last-Event-ID: 120
```

如果查询参数和 `Last-Event-ID` 同时存在，服务使用较大的有效序号。服务必须
先补播缺失事件，再进入实时等待，并继续发送 15 秒 SSE 心跳。

### 9.4 显式取消

客户端通过以下端点请求取消。关闭 SSE 或浏览器窗口不能替代该调用。

```http
POST /api/v1/runs/{run_id}/cancel
```

取消必须满足以下语义。

- Terminal run 重复取消返回当前状态，保持幂等。
- `queued` 运行可以直接进入 `canceled`。
- `running` 运行设置 `cancel_requested_at`，并通知 Worker 停止。
- Worker 必须调用现有 `cancel_request(request_id)` 释放 LLM 槽位。
- Worker 只有完成清理和终态落库后才能发布 `run_end(canceled)`。
- 取消一个运行不能释放或修改其他会话的锁和槽位。

### 9.5 HITL 恢复

客户端通过以下端点恢复 `pending_approval` 运行。

```http
POST /api/v1/runs/{run_id}/resume
Content-Type: application/json
```

```json
{
  "decision": "approve",
  "input": "批准执行"
}
```

恢复请求必须持久化为同一个 `run_id` 的后续尝试，并继续使用原会话
`thread_id`。恢复不能创建第二个 Active run，也不能丢失原 Trace 关联。

### 9.6 错误码

新端点必须使用稳定错误码，并保留可操作的错误说明。

| HTTP | code | 语义 |
| --- | --- | --- |
| 409 | `40901` | 当前会话已有 Active run |
| 409 | `40902` | 运行状态不允许该操作 |
| 409 | `40903` | 幂等键与原请求内容冲突 |
| 429 | `42902` | 用户活动或排队运行达到限制 |
| 404 | `40410` | 运行不存在或不属于当前租户 |
| 503 | `50310` | 运行调度器暂不可用 |

现有 `42900`、`50300`、`50400`、`50401` 和 `50402` 继续用于全局防护和
模型链路错误。

## 10. 后端执行模型

后端必须独立消费 GraphRuntime 事件，再把事件写入 `ChatRunEvent`。不得通过
保持一个客户端 `yield` 调用来维持任务执行。

### 10.1 Run coordinator

M2 新增 `RunCoordinator`，负责领取、执行、暂停、取消和完成运行。它可以先在
API 进程内运行，但生命周期必须独立于请求对象。

```python
class RunCoordinator:
    async def enqueue(self, run_id: UUID) -> None: ...
    async def execute(self, run_id: UUID) -> None: ...
    async def request_cancel(self, run_id: UUID) -> None: ...
    async def resume(self, run_id: UUID, command: dict) -> None: ...
```

`execute()` 必须消费 `GraphRuntime.astream()` 的所有帧，并把可见事件交给统一
`ChatRunEventSink`。Subscriber 数量为零时，消费过程仍必须继续。

### 10.2 数据库会话边界

后台运行不能复用 FastAPI 请求依赖提供的 `AsyncSession`。每个后台事务必须
通过 `AsyncSessionLocal` 创建短生命周期会话。

- 领取任务、更新租约和状态使用独立短事务。
- 插入一批事件和推进 `last_event_sequence` 使用同一事务。
- 最终 Assistant 消息、运行终态和 `run_end` 事件使用同一逻辑提交单元。
- 长时间 LLM 或工具调用期间不得持有数据库事务。
- 重试事务时必须保持事件序号和 Assistant 消息幂等。

### 10.3 运行上下文快照

`request_payload` 必须保存运行开始所需的不可变配置，避免用户在任务运行中
修改 Agent 或设置后改变已受理任务的行为。

快照至少包含以下内容。

- 用户消息和附件引用。
- 最终解析到的 `agent_id`。
- 模型标识和自定义 Provider 引用。
- Skill、MCP 和知识库能力列表。
- `approval_mode`。
- 创建时的降级档位和必要 TraceContext。

API Key、Provider 密钥和附件原始内容不得写入 `request_payload`。Worker 必须
通过受控配置或加密引用在执行时解析敏感值。

### 10.4 Subscriber 断开

事件订阅端点检测到断连后，只能完成以下清理。

- 取消该 Subscriber 的等待任务。
- 释放订阅连接和本地缓冲区。
- 记录订阅断开指标。
- 保留 `ChatRun`、会话锁、Worker 租约和 LLM 运行。

订阅端点 MUST NOT 调用 `mark_cancelled()`、`cancel_request()` 或
`runtime_stream.aclose()`。

### 10.5 完成和失败

Worker 进入终态时必须保证状态、消息和事件一致。

- `succeeded` 必须已经写入非空 Assistant 最终消息。
- `failed` 必须写入稳定 `error_code` 和脱敏 `error_message`。
- `canceled` 不写入伪造 Assistant 答案，可以保留已生成的部分文本供审计。
- 所有终态都必须写入唯一的 `run_end` 事件。
- 终态提交后必须释放会话锁和用户活动运行计数。
- 终态提交失败时，Worker 不能确认任务完成。

## 11. M3：独立 Worker 和故障恢复

M3 提供 API 或 Worker 进程重启恢复、多副本执行和可靠任务投递能力。M3 是
生产级异步运行的必要条件。

### 11.1 可靠命令投递

M3 可以扩展现有 RabbitMQ 基础设施，新增独立交换机和队列。该扩展必须与
观测和会话收尾队列隔离。

```text
chat.exchange
  └─ routing key: run.requested ─► chat.run.q
  └─ routing key: run.resume    ─► chat.resume.q
```

命令只包含 `run_id`、命令类型、幂等 `message_id` 和 TraceContext。完整请求
载荷必须从 PostgreSQL 读取。

### 11.2 Transactional outbox

创建 `ChatRun` 和发布 RabbitMQ 命令之间必须使用 Transactional Outbox，避免
数据库提交成功但命令未发布。

```text
chat_run_outbox
- id
- run_id
- command_type
- payload
- created_at
- published_at
- attempt
- last_error
```

API 在同一数据库事务中写入 `chat_runs` 和 `chat_run_outbox`。Outbox dispatcher
使用 `FOR UPDATE SKIP LOCKED` 领取记录，收到 publisher confirm 后更新
`published_at`。

### 11.3 Worker 领取和租约

Worker 必须通过数据库条件更新领取任务，RabbitMQ 的至少一次投递不能造成
重复执行。

- Worker 只领取 `queued` 或符合恢复条件的运行。
- 领取时原子写入 `lease_owner`、`lease_expires_at` 和 `attempt`。
- Worker 定期续期；失去租约后必须停止推进运行。
- 重复命令发现运行已被有效 Worker 领取时直接确认消息。
- Worker 崩溃后，租约扫描器把可恢复运行重新置为 `queued`。
- `attempt` 达到 `CHAT_RUN_MAX_ATTEMPTS` 后标记 `failed` 并告警。

### 11.4 Checkpointer 和恢复边界

LangGraph Postgres Checkpointer 继续承担图状态恢复。`ChatRun` 负责业务运行状态，
两者必须通过 `session.thread_id` 和 `run_id` 关联。

- `pending_approval` 运行在 Worker 退出后仍可恢复。
- Worker 重启后必须先检查当前图快照，再决定从检查点继续或失败。
- 已提交外部副作用的工具必须使用幂等键，不能假设 Checkpointer 能回滚工具。
- 无法证明安全重试的工具运行必须进入 `failed`，并提示人工确认。
- API 启动时不得像普通进程内任务一样把所有 `running` 运行直接标记失败。

### 11.5 M3 降级

RabbitMQ 故障时，已受理任务必须保留在 PostgreSQL 的 `queued` 状态。系统不得
降级为不可恢复的 fire-and-forget 执行。

- Outbox dispatcher 持续重试发布，并记录发布失败指标。
- API 可以继续受理到可配置的 PostgreSQL 排队限制。
- 达到排队限制后返回 `50310`，不能无限积压。
- RabbitMQ 恢复后自动发布未处理 Outbox 记录。
- 管理员必须能查询排队数量、最老任务年龄和最后发布错误。

## 12. 前端交互规范

前端必须让用户理解任务是在后台继续运行，而不是把切换会话表现为离开或
取消任务。

### 12.1 发送和切换

发送后，当前会话立即显示用户消息、运行卡片和 `queued` 或 `running` 状态。

- 用户可以立即新建或选择其他会话。
- 切换会话不弹出“是否停止生成”确认框。
- 运行中的会话在侧边栏保持状态标识。
- 返回会话时先加载 `run_snapshot`，再补播缺失事件。
- 当前会话输入框只受当前会话 Active run 影响，不能被其他会话禁用。

### 12.2 多窗口同步

多个浏览器窗口可以同时查看和操作不同会话。前端建议使用
`BroadcastChannel("agent-harness-runs")` 同步轻量状态通知。

- BroadcastChannel 只发送 `run_id`、`session_id`、状态和最后事件序号。
- 任何窗口收到通知后都必须从后端读取事实状态。
- BroadcastChannel 不承担任务执行、事件持久化或权限校验。
- 两个窗口使用同一 `Idempotency-Key` 重复提交时，后端返回同一个 `run_id`。
- 两个窗口同时订阅同一 `run_id` 时，允许建立两个 Subscriber，但只执行一次。

### 12.3 停止任务

**停止生成** 必须变为服务端显式取消操作，而不是只调用本地
`AbortController.abort()`。

- 点击后按钮显示“正在停止”。
- 收到 `run_status=canceled` 后显示“已停止”。
- 本地订阅断开但取消请求失败时，UI 必须重新查询状态，不能假装已停止。
- 用户取消一个会话后可以继续使用其他会话。

### 12.4 HITL 审批

审批卡与 `run_id` 绑定，并在任意窗口恢复。审批状态不能只保存在当前
`ChatWindow` 组件中。

- `pending_approval` 状态必须显示在侧边栏。
- 刷新后通过 `active-run` 和运行状态恢复审批卡。
- 批准或拒绝后立即从当前窗口移除旧审批卡。
- 重复审批返回幂等结果或 `40902`，不能执行两次工具。
- 会话 A 的审批不能阻塞会话 B 的发送或运行。

### 12.5 完成通知

任务在非当前会话完成时，前端必须提供低干扰通知。

- 侧边栏显示未查看完成标识。
- 浏览器通知必须遵循用户授权和设置，默认不开启系统通知。
- 用户进入会话后清除未查看标识。
- 通知内容不得包含敏感对话全文。

## 13. 并发治理

异步运行会增加单个用户可以同时占用的模型和工具资源。系统必须分层控制
会话、用户、租户和全局容量。

### 13.1 分层限制

系统采用以下实时限制。默认值必须通过压测后才能用于生产承诺。

| 层级 | 默认规则 | 达到限制后的行为 |
| --- | --- | --- |
| 会话 | 1 个 Active run | 返回 `40901` |
| 用户 | 3 个 Active run | 新运行保持 `queued` |
| 用户队列 | 10 个 queued run | 返回 `42902` |
| 租户 | 可配置 Active run 限制 | 排队或返回租户级错误 |
| 系统 SSE | 复用 `STREAM_MAX_CONCURRENCY` | 复用 queue 事件 |
| 系统 LLM | 复用 `LLM_MAX_CONCURRENCY` | 等待、熔断或降级 |

### 13.2 公平排队

调度器必须防止单个用户或租户长期占用所有运行槽。

- 同一用户按创建时间 FIFO。
- 全局调度建议按租户和用户轮转，而不是只按全局创建时间领取。
- `pending_approval` 不占用 Worker 和 LLM 槽，但计入会话 Active run。
- 工具执行期间按现有设计释放不需要的 LLM 槽。
- 多 Agent 并行仍受 `MAX_PARALLEL_WORKERS` 和 LLM 闸门控制。

### 13.3 超时

网络订阅超时和任务执行超时必须分开配置。

- SSE Subscriber 可以无限重连，不决定任务终态。
- 排队超时决定运行是继续排队还是失败，由产品配置明确。
- 单次 LLM 调用继续使用 `LLM_CALL_TIMEOUT_SECONDS`。
- 整个 `ChatRun` 新增 `CHAT_RUN_TIMEOUT_SECONDS`，默认值建议为 `900`。
- `pending_approval` 不计入运行执行超时，但受独立审批过期策略控制。

## 14. 可观测性和审计

每个 `ChatRun` 必须能够从会话、请求、Trace、子任务和 Worker 日志双向定位。

### 14.1 Trace 关联

TraceContext 必须包含 `run_id`。现有 `request_id`、`session_id`、`thread_id` 和
多 Agent `task_id` 继续保留。

- 根 Trace 属性新增 `chat.run_id`。
- `chat_runs.trace_id` 在 Trace 创建后立即更新。
- Worker 重试继续使用同一业务 `run_id`，但每次尝试可以创建独立 attempt span。
- `AgentTask` 通过 `session_id + trace_id` 关联到顶层运行。

### 14.2 指标

实现必须新增以下指标，并沿用现有 Metrics 和告警体系。

| 指标 | 类型 | 说明 |
| --- | --- | --- |
| `chat_run_active{status}` | gauge | 各活动状态运行数 |
| `chat_run_queued` | gauge | 当前排队运行数 |
| `chat_run_queue_wait_ms` | histogram | 受理到开始执行的等待时间 |
| `chat_run_duration_ms` | histogram | 开始到终态的运行时间 |
| `chat_run_subscribers` | gauge | 当前 SSE Subscriber 数 |
| `chat_run_reconnect_total` | counter | 事件重连次数 |
| `chat_run_replayed_events_total` | counter | 补播事件数量 |
| `chat_run_cancel_total{result}` | counter | 取消请求结果 |
| `chat_run_recovery_total{result}` | counter | Worker 恢复结果 |
| `chat_run_event_lag` | gauge | Subscriber 与最新事件序号差值 |

### 14.3 审计

以下操作必须写入审计日志。审计失败策略沿用现有观测和 MQ 规范。

- `chat.run.create`
- `chat.run.start`
- `chat.run.cancel_request`
- `chat.run.canceled`
- `chat.run.resume`
- `chat.run.succeeded`
- `chat.run.failed`
- `chat.run.recovered`

审计 detail 必须包含 `run_id`、`session_id`、状态、错误码和执行尝试次数，但不
重复写入完整用户消息或模型输出。

## 15. 安全和数据治理

异步运行会延长请求数据在服务端存在的时间。实现必须保持现有租户隔离和
内容采集策略。

- 所有 Run API 必须按 `tenant_id` 和 `user_id` 校验资源所有权。
- `run_id` 使用不可预测 UUID，不能替代权限校验。
- `request_payload` 不保存明文 API Key、Provider 密钥或未授权文件路径。
- `ChatRunEvent.payload` 遵循现有 `CONTENT_CAPTURE_MODE` 和脱敏规则。
- 附件只保存受控引用，Worker 执行时再次校验租户和会话归属。
- 事件清理、运行清理和消息保留必须分别配置并记录审计。
- BroadcastChannel 和浏览器通知不得发送敏感全文。

## 16. 配置规范

新增配置必须集中放在 `backend/app/config.py`，并在 `.env.example`、
`backend/.env.example` 和生产部署示例中保持一致。

```python
# --- 异步并发会话 ---
CHAT_RUN_ENABLED: bool = True
CHAT_RUN_DURABLE_ENABLED: bool = False

# 用户和会话限制
CHAT_RUN_MAX_ACTIVE_PER_USER: int = 3
CHAT_RUN_MAX_QUEUED_PER_USER: int = 10
CHAT_RUN_MAX_QUEUED_GLOBAL: int = 10_000
CHAT_RUN_MAX_ACTIVE_PER_TENANT: int = 100

# 执行和恢复
CHAT_RUN_TIMEOUT_SECONDS: int = 900
CHAT_RUN_QUEUE_TIMEOUT_SECONDS: int = 0
CHAT_RUN_MAX_ATTEMPTS: int = 3
CHAT_RUN_WORKER_LEASE_SECONDS: int = 60
CHAT_RUN_WORKER_HEARTBEAT_SECONDS: int = 20
CHAT_RUN_APPROVAL_TIMEOUT_SECONDS: int = 86_400

# 事件
CHAT_RUN_EVENT_RETENTION_DAYS: int = 7
CHAT_RUN_EVENT_BATCH_MS: int = 50
CHAT_RUN_EVENT_BATCH_CHARS: int = 512
CHAT_RUN_EVENT_POLL_INTERVAL_MS: int = 1000
CHAT_RUN_REDIS_NOTIFY_ENABLED: bool = True
```

配置校验必须保证以下关系成立。

- `CHAT_RUN_MAX_ACTIVE_PER_USER >= 1`。
- `CHAT_RUN_MAX_QUEUED_PER_USER >= 0`。
- `CHAT_RUN_WORKER_HEARTBEAT_SECONDS < CHAT_RUN_WORKER_LEASE_SECONDS / 2`。
- `CHAT_RUN_EVENT_BATCH_MS` 在 `10` 到 `500` 毫秒之间。
- `CHAT_RUN_EVENT_RETENTION_DAYS >= 1`。
- 启用 Durable run 时必须配置 PostgreSQL；启用 M3 时必须完成 Worker 健康检查。

## 17. 兼容和迁移

迁移必须保持现有聊天接口可用，并允许按阶段回滚。新旧路径不能同时执行同一
条用户消息。

### 17.1 M1 兼容

M1 保留 `POST /sessions/{session_id}/messages/stream`，只修改锁粒度和前端状态
管理。新版前端默认启用 Run API；`CHAT_RUN_ENABLED=false` 仅用于显式回滚旧
接口，且不得破坏现有数据库。

### 17.2 M2 兼容适配器

M2 把旧流式端点实现为“创建 ChatRun 后立即订阅”的兼容适配器。

```text
POST /messages/stream
  ├─ create ChatRun
  └─ subscribe ChatRun events on the same HTTP response
```

兼容适配器断连后不得取消 `ChatRun`。新前端迁移完成后，旧端点标记为
deprecated，但至少保留一个版本。

### 17.3 数据迁移

历史消息不需要反向生成 `ChatRun`。只有功能启用后的新请求创建运行记录。

- `messages.run_id` 可以新增为 nullable 外键，历史行保持 null。
- 已有 `request_id` 和 `trace_id` 继续用于历史兼容。
- `AgentTask` 不迁移、不重命名。
- 回滚时保留 `chat_runs` 和事件表，关闭功能开关后旧聊天路径继续可读历史。

## 18. 实施里程碑

实施按体验、持久化和高可用三阶段推进。每个阶段必须独立通过对应验收，不能
用后续阶段承诺替代当前阶段缺失。

| 里程碑 | 状态 | 交付内容 | 对外能力 |
| --- | --- | --- | --- |
| M1 | 已完成 | 会话级锁、用户并发限制、按会话状态、RunManager、侧栏状态 | 多窗口和单页切换并发 |
| M2 | 已完成 | `ChatRun`、事件表、Run API、重放、取消、HITL 恢复 | 刷新和断网恢复，订阅断开不取消 |
| M3 | 已完成 | Outbox、RabbitMQ 命令、独立 Worker、租约和恢复 | API/Worker 重启恢复与多副本领取 |

里程碑依赖为 M1 → M2 → M3。M1 可以先发布，但 UI 和文档必须明确其页面
刷新边界。只有 M3 验收通过后才能宣称支持生产级后台异步任务。

## 19. 测试和验收

测试必须区分源代码评审、定向自动化测试、完整测试套件、真实 HTTP 流程和
真实浏览器流程。任何一类通过都不能替代其他类别。

本次实现的验证记录如下。

- 后端完整测试：`486 passed`。
- 异步运行相关 Ruff 静态检查：通过。
- 前端 TypeScript 与 Vite 生产构建：通过。
- Alembic 离线升级 SQL 生成：通过。
- 真实浏览器：两个会话同时运行，Mock LLM 观测到
  `peak_inflight=2`；独立取消、刷新恢复和双标签页恢复通过。
- 浏览器控制台：验收页面无错误和警告。

### 19.1 单元测试

单元测试覆盖状态机、锁、事件和幂等逻辑。

- 会话锁键包含租户、用户和会话。
- 不同会话可以同时获取锁，同一会话不能重复获取。
- `ChatRun` 状态转换拒绝非法路径。
- 同一 `client_request_id` 返回同一 `run_id`。
- 同一 `run_id + sequence` 不能重复插入。
- Delta 合并不改变最终文本。
- 取消、完成和失败终态保持幂等。

### 19.2 后端集成测试

后端集成测试使用可控延迟的 Fake LLM、真实 FastAPI 路由和测试数据库。

- 同一用户的会话 A、B 同时运行并分别落库。
- 同一会话并发提交返回 `40901`。
- Subscriber 断开后，运行继续并写入最终消息。
- 使用 `after_sequence` 能完整补播且不重复事件。
- 取消 A 后 B 继续运行，所有槽位计数正确。
- `pending_approval` 不占 Worker，恢复后继续同一运行。
- Worker 重复领取命令不产生第二条 Assistant 消息。
- Worker 租约过期后安全恢复或明确失败。

### 19.3 前端测试

前端组件和 Store 测试覆盖按会话隔离的状态更新。

- 会话 A 的事件不能更新会话 B 的消息。
- 切换 `currentSessionId` 不调用运行控制器 `abort()`。
- 其他会话运行时可以新建会话和发送消息。
- 侧边栏按会话显示排队、运行、审批和终态。
- 显式停止调用服务端取消 API。
- 重连从本地最后事件序号继续。

### 19.4 真实浏览器验收

真实浏览器验收必须打开至少两个独立窗口或标签页，并在每次切换后重新读取
页面状态，不能复用失效 DOM 引用。

| 场景 | 通过标准 |
| --- | --- |
| 双窗口并发 | A、B 同时生成，完成时间接近较慢任务 |
| 单页切换 | 离开 A 后 A 继续运行，返回后内容连续 |
| 独立取消 | 停止 A 后 B 不受影响 |
| 双订阅 | 两窗口查看同一运行，只产生一条最终消息 |
| HITL | A 待审批时 B 可运行，刷新 A 后审批卡恢复 |
| 刷新恢复 | M2：刷新后补播缺失事件并继续实时更新 |
| 关闭窗口 | M2：关闭所有 Subscriber 后任务仍完成 |
| 服务重启 | M3：API 重启不丢任务，Worker 按租约恢复 |

### 19.5 负载和故障验收

并发能力必须使用 Mock LLM 建立确定性基线，再进行真实模型验证。

- 单用户 3 个不同会话并发时全部进入运行，第 4 个进入排队。
- 多用户负载下没有单个用户长期占满全局运行槽。
- Redis 通知不可用时事件订阅降级轮询，最终文本不丢失。
- RabbitMQ 不可用时 M3 任务保留在 PostgreSQL，不进入不可恢复执行。
- Worker `kill -9` 后未确认任务被重新领取，幂等约束保持成立。
- 数据库短暂不可用时不确认终态，恢复后继续提交或明确失败。

## 20. 风险和开放问题

以下问题必须在进入对应里程碑前确认。没有结论时采用表中的默认决策。

| 编号 | 问题或风险 | 默认决策 |
| --- | --- | --- |
| R-01 | M1 页面卸载仍可能取消旧式流 | UI 明确边界，M2 解决 |
| R-02 | 同一会话消息是否排队 | 首期返回 `40901`，不做会话内排队 |
| R-03 | Token 事件表增长过快 | Delta 合并，Terminal run 按 7 天清理 |
| R-04 | Redis 通知丢失 | PostgreSQL 游标补播，通知只做唤醒 |
| R-05 | Worker 重试重复外部工具副作用 | 工具幂等；无法证明安全时失败并人工处理 |
| R-06 | 自定义 Provider 密钥快照 | 只存加密引用，不存明文 |
| R-07 | 多 Agent 单运行占多个 LLM 槽 | 保留全局 LLM 闸门和启动容量告警 |
| R-08 | `pending_approval` 长期占用会话 | 增加独立审批过期策略，默认不自动批准 |
| R-09 | 多窗口重复系统通知 | 使用 `run_id` 去重，通知状态后端或跨窗口同步 |
| R-10 | RabbitMQ 扩展与原 MQ 范围冲突 | 已更新 MQ Spec，ChatRun 使用独立拓扑 |

## 21. 代码改造索引

以下文件是当前预计的主要改造点。实现阶段必须先检查工作区已有修改，并按
真实依赖调整，不能机械覆盖。

| 文件 | 预计改造 |
| --- | --- |
| `backend/app/storage/models.py` | 新增 `ChatRun`、`ChatRunEvent` 和可选 Outbox |
| `backend/app/storage/chat_runs.py` | 运行、事件、租约、幂等和 Outbox 仓储 |
| `backend/app/api/router_chat.py` | 旧端点兼容适配，移除 Subscriber 断连取消语义 |
| `backend/app/api/router_runs.py` | 新增运行创建、查询、订阅、取消和恢复 API |
| `backend/app/harness/stream_gate.py` | 用户锁改为会话锁，增加用户活动运行限制 |
| `backend/app/harness/run_coordinator.py` | 新增后台执行和取消协调器 |
| `backend/app/harness/run_events.py` | 新增事件聚合、序号和持久化 Sink |
| `backend/app/config.py` | 新增 §16 配置和校验 |
| `backend/app/main.py` | 启停 Run coordinator、通知器和恢复扫描器 |
| `backend/app/mq/` | M3 新增 ChatRun 命令和 Outbox 发布支持 |
| `backend/app/workers/chat_worker.py` | M3 新增独立 ChatRun Worker |
| `frontend/src/stores/sessionStore.ts` | 改为按会话保存消息和运行状态 |
| `frontend/src/api/runs.ts` | 新增 Run API 和可重连 SSE 客户端 |
| `frontend/src/services/chatRunManager.ts` | 新增跨会话运行订阅管理器 |
| `frontend/src/components/chat/ChatWindow.tsx` | 移除会话切换取消，接入当前会话 Run |
| `frontend/src/components/layout/Sidebar.tsx` | 允许并发新建并显示各会话状态 |
| `frontend/src/pages/ChatPage.tsx` | 加载按会话历史、Active run 和事件游标 |

## 22. 实施结果和上线门槛

代码层面的 M1、M2 和 M3 已完成。正式 Alembic 迁移、配置示例、兼容端点、
独立 Worker、生产 Compose、关联规范和自动化测试均已落地。

生产发布前还必须在目标部署环境执行以下环境验收。它们用于验证 RabbitMQ
集群、数据库和上游模型的实际容量，不是待补代码项。

1. 在生产同构 RabbitMQ 上执行 Worker `kill -9` 和未确认消息重投演练。
2. 执行 RabbitMQ 五分钟不可用、恢复后 Outbox 自动补发演练。
3. 使用目标模型完成租户公平性、队列上限和 TTFT 容量压测。
4. 确认数据库备份、事件保留和审批过期参数符合生产数据治理要求。
