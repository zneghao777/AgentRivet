# MyAgentDemo1 流量快速增长应对评估报告

> 评估时间：2026-09-03 · 基于 main 分支源码 + `deploy/` 部署配置静态分析

---

## 一、结论速览（先给答案）

**问题：面对流量快速增长怎么办？基础设施是否足够，仅需扩展算力就行吗？**

**答：不行，不能只"扩展算力"。当前是"单机 + 单进程 + 进程内并发状态"架构，直接加机器/加核不仅提不上量，还会因为进程内闸门分裂而打爆 LLM 上游。必须先做一次"可水平扩展改造"，之后才能谈"加算力 = 加副本"。**

**核心卡点（一句话）**：Dockerfile 里注释已白纸黑字写明 —— *"并发防护当前为进程内状态。保持单 worker，避免每个 worker 各自放行一整套 LLM_MAX_CONCURRENCY；多副本部署前必须先把闸门/熔断/降级迁移到 Redis。"*

---

### 当前能扛多少（估算）

| 规模 | 估算值 | 依据 |
|---|---|---|
| 同时浏览用户 | 数百 ~ 千 | 静态资源 + 低频 REST，asyncio 无压力 |
| 同时进行中的流式对话 | **约 30~45 路**（当前单副本在线池上限 `LLM_MAX_CONCURRENCY=45`） | prod compose 注入 `LLM_MAX_CONCURRENCY: 45` |
| REST 短请求 QPS | 数百量级 | 单进程 + PG 池 20 |
| 多副本扩展后对话并发 | 受 LLM 上游配额约束，需先做水平化改造 | 见下文 |

> 估算为静态推导，投产前务必用压测校准（已有可观测台可看 P50/P95/错误率/429 率）。

---

## 二、基础设施现状盘点（已核实）

### 2.1 部署形态：单机 Docker Compose，非集群

| 服务 | 副本 | 说明 |
|---|---|---|
| `backend` | 1（uvicorn **--workers 1**） | Dockerfile 强制单 worker |
| `chat-worker` | 1 | durable 模式下跑图 |
| `obs-worker` | 1 | 观测落库 |
| `session-worker` | 1 | 会话收尾 |
| `postgres` / `redis` / `rabbitmq` | 单点 | 无 PgBouncer、无主从、无集群 |

- 编排工具 = **Docker Compose**（非 Swarm replicas、非 K8s），仓库内无任何 `.yaml` deployment/service 清单。
- 更新靠 Watchtower 轮询 GHCR `latest` 自动滚更（单机自更新）。

### 2.2 应用层保护：已经很完善（但全在单进程内）

已落地一套分级并发防护体系（`config.py` + `llm_guard.py` + `stream_gate.py`）：

| 层级 | 机制 | 状态存储 |
|---|---|---|
| L1 入口限流 | API-Key + IP 限流，**Redis 分布式**优先、内存降级 | ✅ Redis |
| L2 会话单流锁 | `SessionStreamLock`，每用户单流 SET NX | ✅ Redis |
| L3 LLM 并发闸门 | `LLMPool`（online/background 两级 semaphore） | ❌ **进程内** |
| L3 SSE 排队 | `StreamGate` 准入 + 队列 | ❌ **进程内** |
| L4 端点熔断 + AIMD + 降级模型 | `EndpointCircuitBreaker` | ❌ **进程内** |
| L5 过载降级 | `DegradeController` 高低水位 | ❌ **进程内** |
| 语义缓存 | `SEMANTIC_CACHE_*`（默认关） | Redis（需开启） |
| ChatRun 租约领取 | PG `FOR UPDATE SKIP LOCKED` | ✅ 跨副本安全 |
| MQ 幂等 | Redis SET NX 降级 PG | ✅ 跨副本安全 |

> **只有 L1/L2 + MQ/ChatRun 是分布式的；L3/L4/L5 全部是进程内内存。**这就是不能直接加副本的根因。

---

## 三、为什么"只加算力"不够？三个核心障碍

### 障碍① LLM 并发闸门是进程内的（最致命）
每加一个 backend 副本，就多放行一整套 `LLM_MAX_CONCURRENCY`（prod=45）。副本一多，同时在途 LLM 调用数 = `副本数 × 45`，很快打爆模型服务配额 → 上游 429 → 因为熔断/降级状态也不共享，无法统一降级 → 请求雪崩透传。**加机器反而更危险。**

### 障碍② SSE 唤醒跨副本失效
`run_events.py` 的唤醒用的是进程内 `asyncio.Condition`，只在同进程内缩短轮询。跨副本时订阅端退回 `CHAT_RUN_EVENT_POLL_INTERVAL_MS=1000` 的 DB 轮询兜底——能工作但不事件驱动、延迟变高。（`CHAT_RUN_REDIS_NOTIFY_ENABLED` 配置已存在，需确认 `run_events.py` 是否真正接入 Redis notify。）

### 障碍③ 状态底座单点
即便计算层水平扩展，PostgreSQL（事件事实/租约/checkpoint/限流）、Redis（限流计数/锁/幂等）、RabbitMQ 仍是单点。数据库是**一切请求的汇聚点**，也是向量检索、RAG、记忆的所在。单机 PG 无法通过"加后端算力"缓解。

---

## 四、应对策略（分阶段，按投入产出排序）

### 阶段 A：先量化，确认拐点在哪（1~3 天）
- 用 k6/locust 对 SSE 对话做压测（50~100 并发 VU），落到观测台看：TTFT P95、错误率、LLM 上游 429 率、PG 连接池占用。
- **先测再改**：确认瓶颈究竟是"LLM 上游配额""PG 连接""还是进程内 CPU"。

### 阶段 B：横向扩展改造（约 2~4 周，前提必做）
这是"扩展算力之前"必须完成的代码改造：

| 事项 | 方案 | 解决 |
|---|---|---|
| LLM 闸门迁 Redis | Redis Lua 原子计数/令牌，替换进程内 `BoundedSemaphore` | 障碍① |
| 熔断/降级迁 Redis | 状态集中到 Redis，统一阈值 | 障碍① |
| SSE 唤醒接 Redis notify | 跑通 `CHAT_RUN_REDIS_NOTIFY_ENABLED`，跨副本事件推送 | 障碍② |
| 开启 durable 模式 | `CHAT_RUN_DURABLE_ENABLED=true`，让图执行走独立 chat-worker（PG 租约领取本身已跨副本安全） | 障碍②③ |
| 引入 PgBouncer | transaction pooling，保护单 PG `max_connections` | 多副本连接数爆炸 |
| 引入编排 | Docker Swarm 起步（`deploy.replicas`）→ 平滑过渡 K8s；或直接 K8s + HPA（CPU + 活跃连接双指标） | 单机→多机 |
| 前端/网关 | Nginx `limit_req_zone`/`limit_conn` 兜底；确认 `proxy_buffering off` 已用于 SSE | 入口 |

### 阶段 C：真正"扩展算力"（阶段 B 完成后再做）
- 副本数按压测 + LLM 配额估算：对话并发 ≈ `N 副本 × 在线池`，受 LLM 上游配额约束。
- 前端静态资源可 CDN；backend/chat-worker 可分别扩副本。
- 数据库：优先上托管 PostgreSQL（自带高可用与扩容）；超量后再评估读写分离、观测表分区、pgvector HNSW 调优。

### 阶段 D：韧性增强（按需）
- 语义缓存开启（FAQ 场景命中率高，显著减 LLM 压力）。
- 多模型降级链（`LLM_FALLBACK_MODELS`）+ 友好降级文案。
- 租户优先级队列、混沌演练。

---

## 五、马上能做 / 短期止血（不改架构）

即使不进入完整阶段 B，有几件事**今天就能做**，能立刻提升单机吞吐与稳定性：

1. **压测 + 观测台校准**（必须先做，一切决策的数据基础）。
2. 确认 backend 生产配置补齐 `CHAT_RUN_DURABLE_ENABLED=true` + `MQ_ENABLED=true`——**把图执行从 backend 单 worker 挪到独立 chat-worker**，这是成本最低的"释放主进程 CPU/连接"动作。
3. 开启语义缓存 `SEMANTIC_CACHE_ENABLED=true`（针对重复检索/FAQ）。
4. 调低 `TRACE_SAMPLE_RATE`（如 0.1）减轻观测写库压力；高并发下 `metadata_only` 已够。
5. Nginx 加 `limit_req_zone`/`limit_conn` 作为第一道闸（不依赖应用代码）。
6. 数据库索引/连接池核对：确认没有启动期自动删建向量列这类开发期行为残留到 prod。

---

## 六、一句话总结

> **"基础设施"当前只够演示/小规模内测（几十路并发对话）。要迎接快速增长，真正缺的不是"算力"，而是"把进程内并发状态（闸门/熔断/降级）迁到 Redis + 引入编排实现多副本 + 数据库去单点"这一轮架构改造。完成这轮改造后，加算力（副本）才有效，届时对话并发可随副本与 LLM 配额线性增长。**

---

## 附：关键代码/配置索引

| 位置 | 关注点 |
|---|---|
| `backend/Dockerfile:61-63` | **进程内并发状态，单 worker，多副本前需迁 Redis**（自证限制） |
| `backend/app/config.py:40-110` | L1~L5 分级并发防护配置 |
| `backend/app/harness/llm_guard.py` | LLM 闸门/熔断/降级（进程内，`_active_tickets`/`_states`） |
| `backend/app/harness/agent_circuit_breaker.py` | Agent 熔断器（进程内，注释自明） |
| `backend/app/api/stream_gate.py` | StreamGate（进程内）/ SessionStreamLock（Redis） |
| `backend/app/api/rate_limit.py` | 限流中间件（Redis 优先 + 内存降级） |
| `backend/app/storage/db.py:14-20` | PG 池 pool_size=20 / max_overflow=10 |
| `backend/app/harness/run_coordinator.py` | `local_execution_enabled` 判定 |
| `backend/app/workers/chat_worker.py` | durable 跑图 worker（PG 租约 + MQ 幂等） |
| `deploy/docker-compose.prod.yml` | 单副本、无 replicas、Watchtower 自更新 |
| `docs/CONCURRENCY_ANALYSIS.md` | 早期并发评估与升级路线参考 |
