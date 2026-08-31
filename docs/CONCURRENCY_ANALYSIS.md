# MyAgentDemo1 并发能力评估与高并发升级方案

> 评估日期：2026-08-30 · 基于当前 main 分支源码静态分析

---

## 一、结论速览

| 问题 | 结论 |
| --- | --- |
| 能允许多少用户同时使用 | **同时在线浏览：数百用户**；**同时进行流式对话：约 10~30 路**（单实例，受 LLM 上游约束） |
| 并发量 | REST 短请求约数百 QPS 量级；SSE 长连接是主要瓶颈点 |
| 有无限流 | **无 API 级限流**（无 429、无按 Key/租户/IP 限速）。仅有后台任务的进程内 Semaphore |
| 有无熔断 | **有，但覆盖不全**：Agent 级熔断器仅用于多 Agent 分支（默认关闭），主对话链路和 LLM 上游均无熔断 |
| 总体定位 | 开发/演示级底座，具备异步化骨架（MQ + Worker），但缺少面向高并发的准入、限流、熔断、缓存与水平扩展设计 |

---

## 二、当前架构的并发模型

### 2.1 部署与运行形态

| 组件 | 现状 | 代码位置 |
| --- | --- | --- |
| 后端进程 | **单进程 uvicorn**，开发模式带 `--reload`，无 `--workers` | `start.sh:332-334` |
| 容器部署 | Compose 单副本 `backend`，无副本数/资源限制 | `deploy/docker-compose.yml:46-68` |
| 数据库 | 单 PostgreSQL 实例，连接池 `pool_size=20 + max_overflow=10` = **单进程最多 30 连接** | `backend/app/storage/db.py:14-20` |
| Redis | 仅用于 MQ 消费幂等（SET NX），**不承担缓存/限流** | `backend/app/mq/dedupe.py` |
| RabbitMQ | obs-worker（观测落库）+ session-worker（会话收尾）异步化，prefetch=200 | `backend/app/config.py:45`、`start.sh:356-378` |
| Nginx | 前端容器反代，**未配置 limit_req/limit_conn** | `deploy/` |

### 2.2 一次流式对话的资源占用

```
SSE 请求进入
  ├─ 前置阶段（占 1 个 PG 连接）：会话加载、消息写入、记忆检索、MCP 工具解析
  ├─ 流式阶段（最长 120s，AGENT_TIMEOUT_SECONDS）：1 条到 LLM 的流式连接
  │    └─ 期间 PG 连接已释放，仅工具调用时短暂借用
  └─ 收尾阶段（异步化到 session-worker）：消息保存、记忆提炼、观测落库
```

**关键结论：每个活跃 SSE 流的核心占用是 1 条 LLM 上游连接（持续 10~120 秒）**，数据库压力反而被异步化设计缓解了。

### 2.3 并发瓶颈排序（单实例）

1. **LLM 上游并发**（第一瓶颈）：所有对话共享一个模型服务配额，无本地并发上限，高并发时要么打爆上游、要么被上游 429 拒绝且无重试/降级
2. **PG 连接池 30**：突发 REST 请求（会话列表、历史加载）会排队等待连接
3. **单进程事件循环**：附件解析、文档解析等 CPU 密集操作会阻塞所有并发流
4. **SSE 连接数**：asyncio 可维持数千空闲连接，不是首要瓶颈

---

## 三、已有保护机制盘点

### 3.1 已实现（值得保留）

| 机制 | 位置 | 说明 | 局限 |
| --- | --- | --- | --- |
| Agent 熔断器 | `backend/app/harness/agent_circuit_breaker.py` | 按 (tenant, agent) 连续失败 3 次 → 熔断 600s | ① 仅多 Agent 分支使用（`MULTI_AGENT_ENABLED=false` 默认关闭）② **进程内状态，多副本不共享** ③ 主对话链路未接入 |
| 附件提取限流 | `router_chat.py:214` `_extract_semaphore` | 有界并发防高峰 | 进程内 |
| 图谱构建限流 | `graph/builder.py:39` Semaphore(4) | 后台抽取并发 | 进程内 |
| 文档解析限流 | `knowledge/service.py:47` Semaphore(3) | 后台解析并发 | 进程内 |
| session-worker 限流 | `workers/session_worker.py:214` Semaphore(3) | MQ 消费并发 | 单 worker 进程 |
| 全链路超时 | `AGENT_TIMEOUT_SECONDS=120`、MCP 三级超时、httpx 各处超时 | 防止请求悬挂 | 已覆盖 |
| MQ 可靠性 | 分级重试 TTL（5s/30s/120s）、死信告警、Redis+PG 双层幂等 | 异步任务不丢不重 | 已覆盖 |
| 观测背压 | `OBS_QUEUE_CAPACITY=10000`，满则丢弃并计数 | 采集不阻塞主链路 | 已覆盖 |

### 3.2 缺失清单（高并发关键缺口）

| # | 缺口 | 后果 |
| --- | --- | --- |
| 1 | **无 API 级限流**：无 429、无按 API-Key/租户/IP 限速 | 单个 Key 可无限打请求，拖垮所有租户 |
| 2 | **无 LLM 上游并发保护**：主链路对 LLM 并发调用无上限、无熔断、无降级 | 高并发直接打爆上游或被上游限流，错误直接透传给用户 |
| 3 | **无 SSE 准入控制**：最大并发流数无限制，超出时不排队也不拒绝 | 全部涌入，雪崩式劣化 |
| 4 | **熔断器进程内**：多副本状态不共享；主链路未接入 | 扩容后熔断失效；主链路裸奔 |
| 5 | **无缓存层**：相同问题、相同检索每次全量执行 | 重复流量 100% 打到 LLM 和 PG |
| 6 | **单实例**：无水平扩展设计（好在状态全在 PG，扩展阻力小） | 无法通过加机器提升对话并发 |
| 7 | **PG 单点**：无 PgBouncer、无读写分离 | 多实例后连接数超 PG 上限（默认 100） |
| 8 | **无压测基线**：无 locust/k6 脚本，拐点未知 | 容量规划无依据 |

---

## 四、并发量估算依据

| 指标 | 估算值 | 推导 |
| --- | --- | --- |
| 同时进行中的流式对话 | **10~30 路** | 受 LLM 上游并发配额约束（DeepSeek 等商用 API 通常几十路）；本地 `--reload` 模式更低 |
| REST 短请求 QPS | 数百量级 | 单进程 asyncio + PG 30 连接，简单 CRUD 可达 500~1000 QPS，含检索的接口显著更低 |
| 同时在线浏览用户 | 数百 | 浏览 UI 主要是静态资源 + 低频 REST |
| 多实例扩展后对话并发 | ≈ N × 20 路 | 状态在 PG 可水平扩展，但需先补齐缺口 #1/#2/#4 |

> 注意：以上为静态分析推算，**建议用阶段一第 5 项的压测脚本实测校准**。

---

## 五、升级方案（分四阶段，按投入产出排序）

### 阶段一：单实例加固（1~2 周，收益最大）

#### 1. API 级限流（缺口 #1）

**方案 A：slowapi（推荐起步）**

```python
# pip install slowapi
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(
    key_func=lambda request: request.headers.get("X-API-Key", get_remote_address(request)),
    storage_uri=settings.REDIS_URL,  # 起步可用 memory://，多副本时切 Redis
)
app.state.limiter = limiter

@router.post("/{session_id}/messages/stream")
@limiter.limit("10/minute")          # 按 API-Key 限流
async def stream_message(...): ...
```

- 优点：FastAPI 生态标准选择，几行代码落地，支持 `memory://` 和 `redis://` 两种存储
- 局限：SSE 长连接场景下"次数限流"不够，需配合第 3 项并发流控制

**方案 B：Redis 令牌桶（多副本就绪）**

```python
# Lua 脚本实现令牌桶，按 (tenant_id, api_key) 双维度
# 建议维度：租户级 100 req/min + Key 级 30 req/min + IP 级 60 req/min
```

- 优点：天然支持多副本共享计数，维度灵活
- 建议：REST 用 slowapi 起步，压测后如需多副本再迁 Redis 令牌桶

#### 2. LLM 上游保护（缺口 #2，**优先级最高**）

```python
# harness/llm_guard.py（新建）
class LLMGuard:
    """LLM 上游并发闸门 + 端点级熔断。"""
    _semaphore: asyncio.Semaphore          # 全局并发上限，如 20
    _circuit: EndpointCircuitBreaker       # 复用 agent_circuit_breaker 思路推广到 LLM 端点

    async def acquire(self):               # 熔断开启 → 快速失败或降级备用模型
        ...
```

- **全局并发 Semaphore**：限制同时在途的 LLM 调用数（如 20），超出排队或快速返回"系统繁忙"
- **端点级熔断**：把现有 `agent_circuit_breaker.py` 的思路推广到 LLM 端点——连续失败/超时率达阈值 → 熔断 N 秒 → 期间请求走降级链
- **重试配置**：`langchain-openai` 的 `ChatOpenAI(max_retries=2)` 自带指数退避，当前未显式配置
- **降级链**：主模型熔断 → 备用模型（配置 `LLM_FALLBACK_MODELS`）→ 友好错误

#### 3. SSE 准入控制（缺口 #3）

```python
# 全局最大并发流 + 满载快速拒绝
_STREAM_SEMAPHORE = asyncio.Semaphore(settings.MAX_CONCURRENT_STREAMS)  # 如 50

async def event_stream():
    if _STREAM_SEMAPHORE.locked():      # 非阻塞尝试获取
        yield 'event: error\ndata: {"code":42900,"message":"系统繁忙，请稍后重试"}\n\n'
        return
    async with _STREAM_SEMAPHORE:
        ...  # 现有流式逻辑
```

- 超出上限返回 429 语义的 SSE error 事件，前端提示"稍后重试"
- 多副本时升级为 Redis 分布式信号量（SET NX + TTL + 心跳续期）

#### 4. 多 worker 进程

```bash
# start.sh / Dockerfile CMD
python -m uvicorn app.main:app --workers 4 --host 0.0.0.0 --port 8000
```

- 注意联动调整：PG 连接池 = 30 × 4 = 120（需 PgBouncer 或调低 `pool_size`）；进程内 Semaphore/熔断器按 worker 分裂（阶段二统一迁 Redis）

#### 5. 压测基线（缺口 #8）

```bash
# k6 示例：模拟 50 并发 SSE 对话
k6 run --vus 50 --duration 5m sse-load-test.js
# 观测指标：TTFT P95（已有 SLO_P95_TTFT_MS=3000）、错误率、LLM 上游 429 率
```

- 项目已有可观测台（P50/P95/错误率），压测数据可直接在观测台验证

### 阶段二：多副本水平扩展（2~4 周）

| 事项 | 方案 |
| --- | --- |
| 无状态化确认 | 会话/消息/Checkpointer 均在 PG ✓；需迁移的进程内状态：熔断器、Semaphore、metrics 计数 |
| 分布式限流 | Redis 令牌桶（Lua），按 tenant/api-key/ip 三维度 |
| 分布式信号量 | Redis SET NX + TTL 做 SSE 并发流控制，心跳续期防死锁 |
| Nginx 兜底 | `limit_req_zone` + `limit_conn_zone`；SSE 路由必须 `proxy_buffering off` + 调大 `proxy_read_timeout` |
| K8s HPA | 按 CPU + 活跃连接数双指标扩缩容（Compose → K8s 迁移时） |

### 阶段三：数据层与任务层（伴随阶段二）

| 事项 | 方案 | 解决 |
| --- | --- | --- |
| PgBouncer | transaction pooling 模式，PG 侧 `max_connections` 保护 | 多实例连接数爆炸 |
| 读写分离 | 观测查询、RAG 检索走只读副本 | 检索拖慢写入 |
| 缓存层 | Redis 缓存 Embedding 结果（相同文本 hash → 向量）+ RAG 检索结果（TTL 5~15min）；进阶可做语义缓存（相似问题命中） | 重复流量 |
| 任务队列统一 | 索引/图谱/记忆任务全部走 RabbitMQ（部分仍是进程内 asyncio task），worker 可独立扩容 | 后台任务与 API 争抢资源 |
| PG 调优 | 观测表按时间分区（`TRACE_RETENTION_DAYS=30` 的清理会变慢）、HNSW 参数调优 | 大数据量查询劣化 |

### 阶段四：韧性架构（按业务需要选做）

- **多模型降级链**：主模型 → 备用模型 → 排队 → 友好降级文案
- **语义缓存**：Embedding 相似度命中缓存直接返回（GPTCache 思路），对 FAQ 类场景命中率可观
- **租户优先级**：付费租户的请求进高优先级队列
- **混沌演练**：定期演练 LLM 上游宕机、PG 主从切换、Redis 不可用（现有代码已有降级路径，如 `dedupe.py` Redis 失败降级 PG，值得扩展到全链路）

---

## 六、技术选型对比

| 需求 | 候选 | 推荐 | 理由 |
| --- | --- | --- | --- |
| API 限流 | slowapi / 自研 Redis 令牌桶 / API 网关（Kong、APISIX） | **slowapi 起步 → Redis 令牌桶** | 快速落地；网关方案留到有运维团队时 |
| 熔断 | 现有进程内熔断器推广 / pybreaker / 自研 Redis 熔断 | **推广现有熔断器到 LLM 端点，状态迁 Redis** | 复用已有代码与配置项风格（`AGENT_CIRCUIT_BREAKER_*`） |
| 连接池 | PgBouncer / 调低应用池 | **PgBouncer（transaction 模式）** | 多实例标准解法 |
| 压测 | k6 / locust / wrk | **k6** | SSE 支持好、JS 脚本易写、指标输出可接观测台 |
| 缓存 | Redis 手写 / GPTCache 类语义缓存 | **Redis 手写起步** | 语义缓存引入新依赖与误命中风险，后置 |

---

## 七、落地优先级建议（最小改动路径）

```
第 1 周：LLM 并发 Semaphore + 端点熔断（缺口 #2）     ← 投入最小、防雪崩收益最大
第 1 周：SSE 最大并发流 + 429 快速拒绝（缺口 #3）
第 2 周：slowapi 按 API-Key 限流（缺口 #1）+ 压测脚本（缺口 #8）
第 3 周：uvicorn --workers + 连接池联动调整
之后：  按压测结果决定是否进入阶段二多副本
```

所有新配置建议沿用现有 `config.py` 的集中管理模式，例如：

```python
# config.py 新增
LLM_MAX_CONCURRENCY: int = 20            # LLM 全局并发上限
MAX_CONCURRENT_STREAMS: int = 50         # SSE 最大并发流
RATE_LIMIT_ENABLED: bool = True
RATE_LIMIT_PER_KEY: str = "30/minute"
LLM_FALLBACK_MODELS: str = ""            # 逗号分隔的降级模型
```

---

## 附：关键代码索引

| 文件 | 关注点 |
| --- | --- |
| `backend/app/storage/db.py:14-20` | 连接池 pool_size=20 / max_overflow=10 |
| `backend/app/harness/agent_circuit_breaker.py` | 现有熔断器（进程内，仅多 Agent 分支） |
| `backend/app/api/router_chat.py:479-690` | SSE 端点（无准入控制） |
| `backend/app/config.py:55-59` | AGENT_TIMEOUT_SECONDS=120、GRAPH_RECURSION_LIMIT=25 |
| `backend/app/config.py:127-128` | 熔断阈值/冷却配置 |
| `backend/app/mq/dedupe.py` | Redis 幂等 + PG 降级（可复用的降级模式） |
| `start.sh:332-334` | 单进程 uvicorn 启动 |
| `deploy/docker-compose.yml` | 单副本部署形态 |
