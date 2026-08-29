# 消息队列在 MyAgentDemo1 中的高可用应用方案调研（聚焦版）

> 目标：聚焦评估消息队列（MQ）在三个最契合本项目高可用目标的场景中的作用：
> **① 观测体系** · **② 会话进行中的任务** · **③ 后续接入多 Agent 编排**。
> 其余场景（知识库索引、定时 cron 等）本文档不做展开，仅在与上述场景相关时提及。
>
> 结论先行：本项目最值得用 MQ 的三个地方，恰好是"数据流"最明显的三处——
> 观测是**高吞吐旁路写入**，会话是**在线主链路 + 收尾异步化**，多 Agent 是**跨进程任务分发与结果回收**。
> 三者可以共用同一套队列基础设施，但队列类型、投递语义、可靠性要求各不相同，下文逐一分析。

---

## 1. 三个场景的现状盘点

### 场景 A：观测体系（Observability）

| 子项 | 当前实现 | 关键风险 |
| --- | --- | --- |
| Trace/Span 落库 | `observability/tracing.py`：进程内 `asyncio.Queue`，批量 100 条/200ms 写 PG；溢出按优先级丢；指数退避重试；shutdown flush 5s | **队列在内存，进程崩溃即丢缓冲中数据**；批量写与 API 争用同一事件循环和 DB 连接池 |
| 审计日志 | `observability/audit.py`：进程内 `asyncio.Queue`，溢出降级同步写 | 审计属强合规数据，降级同步写会**拖慢在线请求**，崩溃同样丢数据 |
| 结构化日志 | `observability/log_export.py`：WARN+ 经 trace 队列写 `observability_logs` | 同 Trace |
| 指标聚合/清理 | `observability/aggregation.py`：常驻 asyncio 轮询（5min） | 轮询式，无调度；多副本会重复执行 |
| 告警评估 | `observability/alerts.py`：常驻 asyncio 轮询（60s） | 同上 |

**一句话**：观测是典型的"生产端高吞吐、消费端可容忍延迟"的数据流，但当前生产端队列放在进程内存里，既不可靠又与在线链路抢资源。

### 场景 B：会话进行中的任务（In-flight Conversation Tasks）

| 子项 | 当前实现 | 关键风险 |
| --- | --- | --- |
| SSE 流式对话 | `harness/graph_runtime.py` `astream`：LangGraph 事件 → SSE 帧，**在线同步**执行 LLM/工具 | 在线主链路，**不能**被 MQ 异步化（用户必须等响应）；但旁路工作会挤占它 |
| 会话收尾：记忆提炼 | `api/router_chat.py` `_schedule_memory_extract`：对话完成后 `asyncio.create_task`，**无持久化任务记录** | **进程崩溃即丢本轮提炼**；限并发 3，高峰时延后 |
| 会话收尾：观测/审计/日志 | 同场景 A | 同上 |
| 会话收尾：成本结算/质量评分 | 由 `aggregation._run_cost_backfill` 等**轮询任务**补算（5min 级延迟） | 非实时，会话完成后不能即时结算 |
| 中断/超时恢复 | `graph_runtime.py` `resume`：Postgres Checkpointer + `AGENT_TIMEOUT_SECONDS` | 超时后人工/接口触发恢复，缺少自动重试通道 |

**一句话**：会话主链路（LLM 生成、工具执行）必须在线同步；但**会话结束后产生的所有收尾工作**（记忆提炼、成本结算、质量评分、观测上报）当前都是进程内 fire-and-forget，是"会话进行中任务"里最应该异步化、可靠化的部分。此外会话过程中工具调用的失败重试目前是同步阻塞的，也可借助队列做"旁路重试"。

### 场景 C：多 Agent 编排（MULTI_AGENT_ENABLED，默认关闭）

| 子项 | 当前实现 | 关键风险 |
| --- | --- | --- |
| Supervisor 路由 | `harness/supervisor.py`：`build_supervisor_graph` 在**单个进程内同步**路由到主 Agent / 子 Agent | 子 Agent 与主 Agent 同进程同步执行，**无法独立扩容**；子 Agent 崩溃即整个请求失败 |
| 子 Agent | `agents/demo_subagent.py`：纯演示，不调用 LLM；注册到 `agent_registry` | 骨架阶段，没有真实子任务 |
| 任务间通信 | 无（LangGraph state 单进程传递） | 无跨进程通信能力 |
| 编排模式 | 仅固定路由 `demo_subagent`，无并行/汇聚/条件路由 | 无 |

**一句话**：多 Agent 是未来最需要 MQ 的地方。一旦子 Agent 是真实 LLM 任务（如并行检索、子任务分解执行），Supervisor 必须能把子任务**投递出去**、**独立执行**、**回收结果**，这正是消息队列的经典场景。

---

## 2. 三个场景的 MQ 价值分析

### 场景 A：观测体系 —— 用"持久化旁路队列"替换"内存采集队列"

**价值**：
1. **崩溃不丢观测/审计数据**：内存 `asyncio.Queue` → Redis Stream / MQ，消息持久化，进程重启后消费端继续处理，满足审计"不可抵赖"。
2. **观测与在线链路彻底解耦**：API 进程只做"入队"（快、非阻塞），批量落 PG 由**独立观测 Worker** 承担，高吞吐时不拖慢 SSE 对话。
3. **可独立扩容/限流**：观测 Worker 按需横向扩展；消费端可控制批大小、频率，避免打爆 DB。

**方案设计**：

```
[API 进程]                          [观测 Worker 进程 xN]
  tracing.flush_span ──► enqueue ─┐
  audit.audit ─────────► enqueue ─┼─► MQ(obs.spans / obs.audit / obs.logs) ─► 批量落 PG
  log_export ──────────► enqueue ─┘   (消费者组/竞争消费，至少一次)
```

- **生产者侧**：保持现有脱敏管线（`redact_text`/`sanitize_attrs`）、采样决策、TraceContext，只把 `_enqueue_nowait` 的内存队列替换为投递 MQ。
- **消费者侧**：观测 Worker 用现有 `bulk_upsert_spans` / `bulk_add_observability_logs` 批量写，保留"消费失败重试 → 死信 + 指标计数"。
- **保留兜底**：若 MQ 不可用，降级回内存队列或直接同步写（与现有 `audit` 溢出降级一致），保证 API 永不因观测阻塞。

**选型倾向**：Redis Stream（消费者组 + ack + 高吞吐，复用现有 Redis）或 arq；数据量到 GB 级再考虑 Kafka 日志重放。

### 场景 B：会话进行中的任务 —— 在线主链路不动，收尾与旁路工作异步可靠化

**价值**：
1. **对话收尾任务可靠化**：记忆提炼、成本结算、质量评分从"`create_task` fire-and-forget"升级为"投递队列 + 至少一次 + 重试"，进程崩溃不丢，结果即时（不再等 5min 轮询）。
2. **不挤占 SSE 事件循环**：收尾任务（含一次 LLM 提炼调用）移到独立 Worker，对话响应更快更稳。
3. **中断/超时恢复有了自动通道**：超时的运行可投递"重试"消息，由 Worker 自动 `resume`，而非只靠接口手动触发。

**方案设计**：

```
[API 进程]                                    [Worker 进程 xN]
  SSE 对话（在线，不动）┐
    │ 对话完成 ──► enqueue session.postprocess ──► MQ ──► 记忆提炼 / 成本结算 / 质量评分
    │ 观测/审计 ──► enqueue（同场景 A）──────────► MQ ──► 观测 Worker
    │ 超时/失败 ──► enqueue agent.retry ─────────► MQ ──► resume 恢复
```

- **关键边界**：**LLM 生成、工具执行、SSE 帧推送必须保持在线同步**，绝不放入队列（否则用户体验从"流式"退化为"轮询"）。MQ 只处理"会话结束后/旁路"的工作。
- **会话进行中的旁路重试**：工具调用失败目前是图内同步重试；可把"失败的工具调用"作为事件投递，由 Worker 执行降级路径（如换模型重试、改用备选工具），不阻塞主链路。
- **幂等与上下文**：消息中携带 `session_id` / `thread_id` / `tenant_id` / TraceContext（`set_trace_context` 已支持），消费端幂等写入（记忆判重、成本结算按 session 去重）。

### 场景 C：多 Agent 编排 —— 跨进程子任务分发、结果回收、事件驱动（重点）

**价值**（这是 MQ 在本项目中最有想象力的地方）：
1. **子 Agent 独立成进程**：Supervisor 把子任务投递到 `agent.<name>` 队列，子 Agent Worker 独立消费执行，**可独立扩容、可独立发布**，子 Agent 崩溃只重试该子任务，不拖垮整个会话。
2. **并行编排成为可能**：主管 Agent 分解任务 → **fan-out** 到多个子 Agent 队列并行执行 → 结果经结果队列/回调汇聚 → 主管综合裁决。这是当前单进程同步 Supervisor 完全做不到的。
3. **高可用**：at-least-once + 消费幂等（子任务按 `task_id` 去重），子 Agent 失败自动重试，超过阈值进死信并触发告警（复用场景 A 的告警体系）。
4. **可观测性闭环**：每个子任务继承父 Trace（`trace_id` 透传 + `links`），子任务 Span 成为父 Trace 的子 span，会话级可观测天然覆盖多 Agent。

**方案设计（结合 LangGraph 现状）**：

```
[API 进程 / 主 Agent 图]                       [子 Agent Worker 进程 xN]
  Supervisor 节点 ──► enqueue agent.<name> ──► MQ ──► 子 Agent 执行 ──► 结果队列/回调
       │  ▲
       │  └──── 结果回收：等待该 task_id 完成事件 ────────────────┘
       └── 汇总裁决 → 最终回答
```

- **集成方式**：不重写 LangGraph 图。Supervisor 的"投递-等待-回收"可封装为一个**异步工具**（如 `agent_dispatch(task, agent_name)`），在主图内被 LLM/路由节点调用；结果队列用 `task_id` 关联。
- **编排模式演进**：
  - 起步：**简单路由**（现有 demo_subagent 的队列版）——投递即执行。
  - 进阶：**并行 fan-out + 汇聚**（MapReduce 式，如"拆解任务→并行检索/分析→综合"）。
  - 进阶：**条件路由 / 优先级队列**（紧急任务高优先级队列）。
  - 远期：**事件总线**（`agent.completed` / `task.failed` 事件广播，非依赖子 Agent 的其他模块可订阅响应）。
- **队列语义**：子任务需要**结果回传**（request/reply），比纯 fire-and-forget 更复杂：
  - 简单：arq + 结果写回 PG/Redis（任务结果表），主管轮询或等待回调。
  - 复杂：RabbitMQ 的 `reply_to` / `correlation_id`（RPC 模式），或 topic exchange 做事件总线。

---

## 3. 三个场景的落点汇总

| 场景 | MQ 做什么 | 队列/语义 | 可靠性要求 | 优先级 |
| --- | --- | --- | --- | --- |
| **观测体系** | Trace/Span、审计、结构化日志持久化旁路落库 | Redis Stream / arq，消费者组，至少一次 | 高（审计不可丢） | **P0** |
| **会话收尾任务** | 记忆提炼、成本结算、质量评分、超时恢复 | arq 任务队列，至少一次 + 幂等 | 中高 | **P0** |
| **多 Agent 编排** | 子任务分发、结果回收、事件驱动编排 | arq 起步 → RabbitMQ（RPC/topic） | 高（任务可重试，结果需回收） | **P1（MULTI_AGENT_ENABLED 启用时）** |

---

## 4. 技术选型建议

| 需求 | 推荐 | 理由 |
| --- | --- | --- |
| 观测旁路采集（高吞吐、持久化、崩溃不丢） | **Redis Stream + 消费者组**（复用现有 Redis） | 天然支持 ack、消费者组水平扩展、持久化（需开 AOF）；吞吐足够且无新增组件 |
| 会话收尾任务（简单、可靠、重试） | **arq**（Redis asyncio 任务队列） | 与项目 asyncio 风格完全一致，函数即任务，内置重试/超时/幂等，零额外中间件 |
| 多 Agent 子任务分发 + 结果回收 | **arq 起步**；编排复杂后升级 **RabbitMQ + aio-pika** | arq 覆盖简单分发；RabbitMQ 的 `correlation_id`/`reply_to`（RPC）、topic exchange（事件总线）、优先级队列支撑高级编排 |
| 日志重放 / 超大观测量 | Kafka（远期，当前不需要） | 当前规模用 Stream/arq 足够 |

**统一原则**：P0 阶段（观测 + 会话收尾）全部走 **Redis（Stream + arq）**，一套基础设施、改造量最小；多 Agent 进入真实编排时再评估引入 RabbitMQ。

---

## 5. 演进路径（聚焦三场景，分三步落地）

### 第一步（P0）：观测采集队列化 + 独立观测 Worker
- 新增 `backend/app/queue.py`（Stream/arq 入队封装）、`backend/app/workers/obs_worker.py`。
- 替换 `tracing._enqueue_nowait` / `audit.audit` / `log_export` 的进程内队列为 MQ 投递，保留脱敏/采样/溢出降级兜底。
- `docker-compose.yml` 增加独立 `obs-worker` 服务。

### 第二步（P0）：会话收尾任务队列化
- 新增 `backend/app/workers/session_worker.py`，消费 `session.postprocess`。
- 替换 `router_chat._schedule_memory_extract` 为投递队列；成本结算/质量评分改为会话完成后即时投递（替代 5min 轮询）。
- 消息携带 TraceContext，收尾任务纳入观测闭环。

### 第三步（P1）：多 Agent 子任务分发
- 启用 `MULTI_AGENT_ENABLED`，Supervisor 改为"投递-等待-回收"异步工具。
- 新增 `agent.<name>` 队列 + `agent.result` 结果队列；子 Agent Worker 消费执行并回传结果。
- 并行 fan-out 编排 + 事件总线（`agent.completed` 等），全链路 trace 透传。
- 评估升级 RabbitMQ 支撑复杂路由/RPC/优先级。

---

## 6. 高可用设计要点（三场景通用）

1. **在线主链路绝不入队**：LLM 生成、工具执行、SSE 推送必须同步在线；MQ 只承载旁路/收尾/子任务。
2. **至少一次 + 消费幂等**：所有消费者按 `job_id` / `task_id` / `session_id` 幂等去重，防重复提炼/重复结算/重复执行子任务。
3. **DB 仍是事实源**：任务状态、结果、记忆、成本都落 PG；MQ 是投递与调度通道，两者双保险（如 MQ 丢消息可从 DB 恢复）。
4. **TraceContext 透传**：生产者把 `trace_id`/`span_id` 写入消息头，消费者 `set_trace_context`，子任务/收尾任务成为父 Trace 子 span，观测贯穿全链路。
5. **背压与限流**：观测 Worker 控制批大小/频率；子 Agent Worker 沿用现有 `Semaphore` 思想限制并发，防打爆模型服务。
6. **死信与告警**：重试耗尽进死信队列，联动现有 `alerts` 体系产生 incident，替代"日志里躺着的静默失败"。
7. **Redis 高可用**：Redis 是队列底座时必须开启持久化（AOF）+ 主从/哨兵，避免 Redis 单点成为新的故障源。

---

## 7. 结论

| 场景 | 是否值得用 MQ | 一句话建议 |
| --- | --- | --- |
| **观测体系** | 值得，P0 | 内存采集队列 → Redis Stream 持久化旁路 + 独立观测 Worker，崩溃不丢、不拖慢对话 |
| **会话进行中的任务** | 值得，P0 | 主链路保持同步；会话收尾（记忆提炼/成本结算/质量评分）与失败重试走 arq 队列可靠化 |
| **多 Agent 编排** | **最值得，是主战场** | 子任务跨进程投递/回收/并行编排，Supervisor 从"进程内同步路由"升级为"队列驱动的异步编排"，天然获得独立扩容、任务重试、全链路观测 |

**执行顺序**：先做第一步（观测）与第二步（会话收尾，共享 Redis 队列设施），为第三步多 Agent 铺好队列基座；多 Agent 启用时再按需引入 RabbitMQ 支撑复杂编排。
