# 消息队列（RabbitMQ）接入技术规格书

版本：v1.0.0
日期：2026-08-28
状态：待评审
关联文档：`docs/SPEC.md`、`docs/OBSERVABILITY_SPEC.md`、`docs/MEMORY_SPEC.md`、
`docs/MESSAGE_QUEUE_HA_ANALYSIS.md`

本规格定义 RabbitMQ 在本项目中的接入范围、拓扑设计、消息模型、可靠性语义、
降级策略、配置、测试与验收标准。**本期只覆盖两个场景**：

1. **观测体系**：Trace/Span、结构化日志、审计日志的旁路落库；
2. **会话进行中的任务**：对话结束后的收尾工作（首期为长期记忆提炼）。

多 Agent 编排、知识库索引、定时任务调度**不在本期范围内**（见 §2.2 非目标）。

---

## 1. 背景与问题定义

### 1.1 当前实现基线

| 链路 | 当前实现 | 位置 |
| --- | --- | --- |
| Span/日志落库 | 进程内 `asyncio.Queue`（容量 `OBS_QUEUE_CAPACITY=10000`）+ 批量写（100 条/200ms）+ 指数退避重试 3 次 | `observability/tracing.py` `_write_loop` / `_enqueue_nowait` |
| 审计落库 | 进程内 `asyncio.Queue`（容量 1000）+ 单条写；队列满降级同步写 | `observability/audit.py` `_write_loop` / `audit` |
| 记忆提炼 | 对话结束后 `asyncio.create_task` + `Semaphore(3)`，**无持久化任务记录**，失败仅记日志 | `api/router_chat.py` `_schedule_memory_extract` / `_run_memory_extract` |

### 1.2 已确认的高可用缺陷

| 编号 | 缺陷 | 影响 |
| --- | --- | --- |
| Q-01 | 采集/审计队列在进程内存，**进程崩溃即丢缓冲数据** | 审计数据不可抵赖性被破坏；错误 span 丢失导致问题无法定位 |
| Q-02 | 批量写 PG 与 API 共享事件循环与连接池，高峰期**拖慢 SSE 对话** | 在线主链路延迟抖动 |
| Q-03 | 记忆提炼为 fire-and-forget，**进程重启即丢本轮提炼** | 记忆覆盖率静默下降，无告警无恢复 |
| Q-04 | API 无法水平扩容后协调后台任务（多副本各自 `create_task`） | 重复执行或任务分散无序 |
| Q-05 | 失败无重试通道：提炼失败仅 `logger.warning`，审计写失败仅告警日志 | 静默失败 |

### 1.3 引入 RabbitMQ 后必须回答的问题

1. API 进程崩溃后，缓冲中的 span/审计/提炼任务是否一条不丢？
2. RabbitMQ 整体不可用时，在线对话是否仍然可用（降级路径）？
3. 同一条消息被重复投递时，落库/提炼是否幂等？
4. 消费失败的消息去了哪里，多久重试，重试几次，最终失败如何告警？
5. 后台任务的 span 是否仍能关联到触发它的那次对话 Trace？

---

## 2. 目标、非目标与设计原则

### 2.1 目标

- 观测（span/log/audit）与在线 API 解耦：API 只做发布，落库由独立 Worker 承担；
- 消息持久化 + publisher confirm + 消费 manual ack，实现**至少一次**投递；
- 会话收尾任务（记忆提炼）可靠化：崩溃可恢复、失败可重试、超限进死信并告警；
- 全链路 TraceContext 透传：Worker 消费产生的 span 挂到原对话 Trace 下；
- RabbitMQ 不可用时自动降级回现有进程内队列，**在线链路永不因 MQ 阻塞**；
- 消费端幂等，重复投递不产生重复数据。

### 2.2 非目标

- 不做多 Agent 子任务分发与编排（后续版本）；
- 不迁移知识库解析/图谱构建任务（保留现有 DB 任务表 + 进程内调度）；
- 不引入 Kafka / Redis Stream（观测吞吐未达瓶颈前不引入）；
- 不做精确延迟调度（成本结算、质量评分等轮询任务本期不动）；
- 不用 MQ 传递 SSE 流式帧（在线主链路保持同步）。

### 2.3 设计原则

1. **在线主链路优先**。发布消息必须非阻塞、有超时；MQ 故障只降级、不报错、不拖慢对话。
2. **DB 仍是事实源**。MQ 只做投递通道；span/审计/记忆的最终状态以 PG 为准。
3. **至少一次 + 消费幂等**。所有消费者必须能安全处理重复消息。
4. **生产侧脱敏不变**。进入 MQ 的消息已是脱敏快照（复用 `sanitize_attrs`/`redact_text` 管线），MQ 中不出现新增敏感数据。
5. **拓扑声明幂等**。exchange/queue/binding 由代码声明（durable），启动时自动创建，不依赖手工配置。
6. **可观测闭环**。MQ 自身的发布失败、队列堆积、死信数量纳入现有指标与告警体系。

---

## 3. 总体架构

```
┌─────────────── API 进程（FastAPI）───────────────┐
│  SSE 对话（在线同步，不经过 MQ）                    │
│    │ 对话完成                                      │
│    ├─► publisher: session.postprocess ─┐          │
│    ├─► publisher: obs.span / obs.log ──┤          │
│    └─► publisher: obs.audit ───────────┤          │
│         （confirm 超时/失败 → 降级内存队列）          │
└────────────────────────────────┬────────────────┘
                                 │ AMQP（持久化消息）
                    ┌────────────▼─────────────┐
                    │  RabbitMQ 3.13（quorum）  │
                    │  exchange: obs / session │
                    │  + 死信/延迟重试拓扑       │
                    └─┬──────────────┬─────────┘
        竞争消费        │              │
┌───────────────────────▼──┐   ┌───────▼──────────────────┐
│  obs-worker（可 xN 扩容） │   │ session-worker（可 xN）   │
│  批量落 PG（100条/200ms） │   │ 记忆提炼（幂等+重试）      │
│  bulk_upsert_spans 等    │   │ extract_from_conversation│
└──────────────────────────┘   └──────────────────────────┘
```

- **API 进程**：保留全部采集/脱敏/采样逻辑，仅把"入内存队列"替换为"发布到 MQ"；发布失败降级回内存队列（现有 `_write_loop` 逻辑保留为 fallback）。
- **obs-worker**：独立进程，竞争消费 `obs.*` 队列，复用现有 `bulk_upsert_spans` / `bulk_add_observability_logs` / `add_audit` 批量写。
- **session-worker**：独立进程，消费 `session.postprocess`，执行 `memory_service.extract_from_conversation`。

---

## 4. RabbitMQ 拓扑设计

### 4.1 交换机与队列

| 对象 | 类型 | 声明参数 | 说明 |
| --- | --- | --- | --- |
| `obs.exchange` | topic | durable | 观测数据入口 |
| `session.exchange` | direct | durable | 会话收尾任务入口 |
| `obs.spans.q` | quorum | durable, DLX=`obs.dlx` | span + 结构化日志（`record_type` 区分） |
| `obs.audit.q` | quorum | durable, DLX=`obs.dlx` | 审计（强合规，独立队列便于单独控保留） |
| `session.postprocess.q` | quorum | durable, DLX=`session.dlx` | 会话收尾任务 |
| `obs.dlx` / `session.dlx` | direct | durable | 死信交换机 |
| `obs.deadletter.q` / `session.deadletter.q` | classic | durable | 死信队列（人工排查 + 告警数据源） |
| `session.retry.q` | classic | durable, `x-message-ttl`（分级）, DLX=`session.exchange` | 延迟重试队列（TTL 到期后回投原交换机） |

Binding：

```
obs.exchange  --obs.span.#-->  obs.spans.q      （routing key: obs.span / obs.log）
obs.exchange  --obs.audit-->   obs.audit.q
session.exchange --postprocess--> session.postprocess.q
session.retry.q --(TTL 到期)--> session.exchange（回投 postprocess）
```

### 4.2 消息信封（统一格式）

所有消息使用统一信封，`payload` 内为各场景数据：

```json
{
  "message_id": "uuid（幂等键）",
  "type": "obs.span | obs.log | obs.audit | session.postprocess",
  "version": 1,
  "created_at": "2026-08-28T12:00:00.000Z",
  "retry_count": 0,
  "trace": {
    "trace_id": "…32hex",
    "span_id": "…16hex",
    "request_id": "req-…",
    "tenant_id": "default",
    "session_id": "uuid | null"
  },
  "payload": { }
}
```

- `message_id`：生产端生成，消费端幂等判重键；
- `trace`：从当前 `TraceContext` 提取，Worker 消费时用 `set_trace_context` 恢复，使落库/提炼产生的 span 挂回原 Trace；
- `payload` 序列化：`datetime` → ISO8601 字符串，`uuid` → str，消费端还原。

### 4.3 各场景 payload

**obs.span / obs.log**：`SpanHandle.snapshot()` / 日志快照原样（已脱敏），消费端按 `record_type` 分流到 `bulk_upsert_spans` / `bulk_add_observability_logs`。

**obs.audit**：`audit()` 的 item 字段（tenant_id/action/user_id/session_id/detail/ip/trace_id/request_id/resource_id），消费端调 `add_audit`。

**session.postprocess**：

```json
{
  "payload": {
    "tenant_id": "default",
    "user_id": "u-123",
    "session_id": "uuid",
    "session_title": "…",
    "user_msg": "…（已按 MEMORY_SPEC 脱敏）",
    "assistant_msg": "…"
  }
}
```

幂等键：`message_id` + `payload.session_id`。消费端以 `message_id` 做判重（Redis `SET NX EX 86400`，Redis 不可用时降级查 `agent_memories` 近期提炼记录）。

---

## 5. 可靠性语义

### 5.1 发布侧（API 进程）

| 机制 | 要求 |
| --- | --- |
| Publisher confirm | 开启；单条确认超时 `MQ_CONFIRM_TIMEOUT_S`（默认 2s） |
| 发布超时 | 整个发布动作（含 confirm）不超过 2s；超时视为失败 |
| 失败降级 | confirm 失败/连接断开 → 写入本地内存队列（现有 `_write_loop` 兜底），并计数 `mq_publish_fallback_total` |
| 连接管理 | 单例长连接 + 自动重连（指数退避，上限 30s）；发布通道池化 |
| 阻塞保护 | 发布在 `asyncio.to_thread` 之外的纯异步路径；绝不阻塞事件循环超过超时阈值 |

### 5.2 消费侧（Worker 进程）

| 机制 | 要求 |
| --- | --- |
| prefetch | `MQ_PREFETCH`（默认 200）；obs-worker 内部攒批（100 条/200ms）后一次 `ack` 批内全部 delivery_tag |
| ack 语义 | 落库成功才 ack；失败 nack + requeue（首次） |
| 重试 | `session.postprocess` 失败 → 发往 `session.retry.q`（TTL 分级 5s/30s/120s，按 `retry_count` 递增），到期回投原交换机；`retry_count >= 3` → 直接进死信 |
| 死信 | 重试耗尽 / 消息格式非法 / 幂等冲突不可恢复 → 死信队列，并触发告警事件（复用 `alerts` 体系，新增 `mq.deadletter` 规则） |
| 幂等 | 见 §4.3；重复消息直接 ack 丢弃（记 `mq_duplicate_total`） |
| 优雅退出 | SIGTERM → 停止拉取 → 处理完在途批 → ack → 关闭连接（超时 10s 强制退出，未 ack 消息自动 requeue） |

### 5.3 高可用部署

- RabbitMQ 以 **quorum queue** 提供队列级 HA（3 节点集群为生产目标态；开发环境单节点 + 持久化即可）；
- 消息 `delivery_mode=2`（持久化），队列 durable；
- Worker 无状态，可任意扩容（竞争消费）；同一队列消息按 prefetch 分摊，不重复消费。

---

## 6. 代码改造点

### 6.1 新增模块

```
backend/app/mq/
  __init__.py
  connection.py    # 连接/通道管理、自动重连、拓扑声明（幂等）
  publisher.py     # publish_span / publish_log / publish_audit / publish_session_postprocess
  envelopes.py     # 消息信封构造、序列化/反序列化、TraceContext 提取与恢复
backend/app/workers/
  obs_worker.py        # 消费 obs.*，批量落 PG（复用 repositories）
  session_worker.py    # 消费 session.postprocess，执行记忆提炼
```

依赖：`aio-pika>=9`（asyncio 原生，加入 `requirements.txt`）。

### 6.2 既有代码修改

| 文件 | 修改 |
| --- | --- |
| `observability/tracing.py` | `_enqueue_nowait` 改为：MQ 可用 → `publish_span/publish_log`（非阻塞，带超时）；失败/不可用 → 走现有内存队列。`_write_loop` 保留为降级通道 |
| `observability/audit.py` | `audit()` 同上：MQ 优先，内存队列 + 同步写降级保留 |
| `api/router_chat.py` | `_schedule_memory_extract` 改为 `publish_session_postprocess(...)`；`_run_memory_extract` 及其 Semaphore 迁移至 `session_worker`（API 侧保留降级直调路径） |
| `main.py` | 启动时声明 MQ 拓扑（`MQ_ENABLED=true` 时）；关闭时优雅断开 |
| `config.py` | 新增 §7 配置项 |
| `deploy/docker-compose.yml` | 新增 `rabbitmq`（3.13-management，数据卷）、`obs-worker`、`session-worker` 服务 |

### 6.3 Worker 进程入口

```
python -m app.workers.obs_worker      # 或 uvicorn 独立进程脚本
python -m app.workers.session_worker
```

Worker 启动流程：初始化 DB 工厂 → `init_tracing`（Worker 自身 span 也走 MQ 或直写）→ 声明拓扑 → 启动消费。Worker 内部产生的 span（如 `memory.extract` 子 span）直接批量写 PG（避免自我回环）。

---

## 7. 配置项（`config.py` 新增）

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `MQ_ENABLED` | `false` | 总开关；false 时行为与现状完全一致 |
| `RABBITMQ_URL` | `"amqp://harness:harness@localhost:5672/"` | 连接串 |
| `MQ_CONFIRM_TIMEOUT_S` | `2.0` | publisher confirm 超时 |
| `MQ_PREFETCH` | `200` | 消费者 prefetch |
| `MQ_OBS_ENABLED` | `true` | 观测链路开关（细粒度） |
| `MQ_SESSION_ENABLED` | `true` | 会话收尾开关（细粒度） |
| `MQ_SESSION_MAX_RETRIES` | `3` | 提炼任务最大重试次数 |
| `MQ_RETRY_TTLS_MS` | `[5000, 30000, 120000]` | 分级重试 TTL |
| `MQ_DEDUPE_TTL_S` | `86400` | 幂等键保留时长 |
| `MQ_DEADLETTER_ALERT_ENABLED` | `true` | 死信触发告警 |

---

## 8. 监控与告警

新增指标（纳入现有 `metrics` 体系）：

| 指标 | 类型 | 说明 |
| --- | --- | --- |
| `mq_publish_total{type,result}` | counter | 发布成功/降级/失败 |
| `mq_publish_fallback_total` | counter | 降级内存队列次数（>0 持续即 MQ 异常） |
| `mq_consume_total{queue,result}` | counter | 消费成功/重复/失败 |
| `mq_deadletter_total{queue}` | counter | 死信条数 |
| `mq_queue_depth{queue}` | gauge | 队列堆积（采集自 RabbitMQ management API，60s 周期） |

新增 SLO/告警规则（复用 `alerts.py` 评估器）：

- `mq.deadletter`：死信队列 5min 内新增 > 0 → warning；
- `mq.queue_depth`：`session.postprocess.q` 堆积 > 1000 持续 10min → warning；
- `mq.publish_fallback`：降级比例 > 5% 持续 5min → critical（MQ 不可用）。

---

## 9. 测试与验收标准

### 9.1 单元测试

- 信封序列化/反序列化（datetime/uuid/None 边界）；
- 发布降级：模拟 confirm 超时 → 消息进入内存队列；
- 消费幂等：同一 `message_id` 重复投递 → 只落库一次；
- 重试分级：`retry_count` 0→1→2→3 的 TTL 与死信路径。

### 9.2 集成测试（docker-compose 起 RabbitMQ）

- 端到端：发送一条对话 → span/log/audit 出现在 PG，`session.postprocess` 产生记忆记录；
- Trace 关联：Worker 产生的 `memory.extract` span 的 `trace_id` 与对话根 Trace 一致；
- 扩容：双 obs-worker 实例下无重复落库（唯一键 `(trace_id, span_id)` 兜底）。

### 9.3 故障演练（验收硬门槛）

| 演练 | 预期 |
| --- | --- |
| API 进程 `kill -9`（缓冲中有未发布消息） | 已 confirm 的消息不丢；降级内存队列中的消息丢失量 ≤ 内存队列容量，且 `dropped` 指标可见 |
| RabbitMQ 容器停止 5min | API 正常响应，SSE P95 无明显劣化；发布全部降级并计数；恢复后自动重连 |
| session-worker `kill -9`（在途消息未 ack） | 消息 requeue，另一实例或重启后继续消费，提炼最终完成 |
| 注入非法消息（格式损坏） | 进死信 + 告警，不阻塞队列 |

### 9.4 性能验收

- 发布路径新增开销 P99 < 1ms（不含 confirm 等待，异步确认）；
- 开启 MQ 后，SSE 首 Token 延迟（TTFT）与基线差异 < 5%；
- obs-worker 单实例吞吐 ≥ 现有进程内 writer（100 条/批）。

---

## 10. 分阶段实施计划

| 阶段 | 内容 | 交付物 |
| --- | --- | --- |
| M0 | 基础设施：`mq/` 模块、拓扑声明、compose 增加 rabbitmq、配置项 | 连接/发布/消费可用，开关默认关闭 |
| M1 | 观测链路切换：span/log/audit 发布 + obs-worker + 降级路径 | §9.3 前两项演练通过 |
| M2 | 会话收尾切换：postprocess 发布 + session-worker + 幂等/重试/死信 | §9.3 全部演练通过 |
| M3 | 监控告警接入 + 性能验收 + 文档更新（README/SPEC 关联） | §8 指标上线，§9.4 达标 |

每阶段独立可回滚：`MQ_ENABLED=false` 即回到现状。

---

## 11. 开放问题

1. 审计消息是否需要比 quorum queue 更强的保证（如单独镜像到冷存储）——待合规评审；
2. `user_msg`/`assistant_msg` 全文进入 MQ 的合规边界（当前 MEMORY_SPEC 允许提炼使用原文，但 MQ 落盘时长 > 内存队列）——建议 M2 前确认保留策略；
3. obs-worker 批量写与 API 直写共用连接池的容量规划——待压测数据。
