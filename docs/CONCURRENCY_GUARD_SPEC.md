# CONCURRENCY_GUARD_SPEC — 并发防护与体验保障规范

> 版本：v1.1（2026-08-30） · 状态：待评审
> 关联文档：`CONCURRENCY_ANALYSIS.md`（现状分析）、`OBSERVABILITY_SPEC`（指标落点）、`MQ_INTEGRATION_SPEC`（Redis 降级模式）
> 上游约束：生产环境本地部署模型 **100 路并发硬上限**；开发环境 DeepSeek V4 Flash 官方 API（**2500 并发/账号**）

> L2 会话约束和 SSE 断连语义已由
> [`ASYNC_CONVERSATION_RUN_SPEC.md`](./ASYNC_CONVERSATION_RUN_SPEC.md)
> 替代。L1、L3、L4 和 L5 仍以本文为准。

---

## §1 背景与目标

### 1.1 问题

当前系统为开发/演示级底座：单进程 uvicorn、无 API 限流、无 LLM 上游并发保护、无 SSE 准入控制、熔断器仅覆盖多 Agent 分支（默认关闭）。生产环境模型本地部署后仅有 100 路并发，若不做防护，突发流量将雪崩式劣化所有用户的体验（详见 `CONCURRENCY_ANALYSIS.md` §3.2 缺口清单）。

### 1.2 目标

在 **LLM 并发 ≤100 路** 的硬约束下，通过"限流 → 准入 → 排队 → 熔断 → 降级"五层防护栈，实现：

| 在线规模 | 体验等级 | 用户感受 |
| --- | --- | --- |
| ≤300 人 | **P0 无感** | 零排队，TTFT P95 < 3s |
| 300~500 人 | P1 可接受 | 高峰排队 10~30s，有明确预期反馈 |
| 500~800 人 | P2 降级可用 | 自动关思考/裁剪上下文，回答略简短但正确 |
| >800 人 | P3 保底 | 排队转异步/友好拒绝，**人人有回应，无人被黑盒悬挂** |

### 1.3 设计原则

1. **Provider 无关**：所有策略实现在应用层，通过 `LLM_BASE_URL/LLM_MODEL` 切换 DeepSeek API 与本地 vLLM，策略行为一致。
2. **排队优于拒绝，快速失败优于悬挂**：超载时先排队（带预期反馈），队列满才拒绝；上游故障时熔断快速失败，绝不无限等待。
3. **在线优先**：在线对话与后台批处理（图谱/记忆/索引）**分池**，后台任务永不挤占对话并发。
4. **可观测**：每个防护动作（限流/排队/熔断/降级）都有指标与事件，可向甲方汇报 SLA 达成率。
5. **渐进增强**：阶段一进程内实现（单实例即可运行），阶段二迁 Redis 支持多副本，接口不变。

---

## §2 现状与差距（摘要）

| 缺口（编号沿用 CONCURRENCY_ANALYSIS §3.2） | 本 Spec 对应章节 |
| --- | --- |
| #1 无 API 级限流 | §4 |
| #2 无 LLM 上游并发保护 | §6、§7 |
| #3 无 SSE 准入控制 | §5、§6 |
| #4 熔断器进程内且主链路未接入 | §7 |
| #5 无缓存层 | §8.4（阶段四） |
| #8 无压测基线 | §14 |

已有可复用资产：`agent_circuit_breaker.py`（熔断状态机模式）、`mq/dedupe.py`（Redis 不可用降级模式）、`metrics.record_request`（指标埋点）、`SLO_P95_TTFT_MS=3000`（SLO 配置）。

---

## §3 总体架构：五层防护栈

```
用户请求
  │
  ▼
[L1 入口限流]  slowapi + Redis：按 Key/IP 限速，超速 429 + Retry-After
  │
  ▼
[L2 会话级约束] 同一会话 1 个 Active run；不同会话受用户和租户额度治理
  │
  ▼
[L3 准入 + 排队] SSE 流闸门（asyncio）+ LLM 调用闸门（线程信号量）
  │              超出 → 排队，推送 queue 事件（位置 + 预计等待）
  ▼
[L4 熔断 + 快速失败] 端点级熔断器 + 429 自适应收敛（AIMD）+ 降级模型链
  │
  ▼
[L5 降级阶梯] 利用率水位触发：关思考 → 裁剪上下文 → 缩短 max_tokens → 语义缓存兜底
  │
  ▼
LLM 上游（dev: DeepSeek V4 Flash API / prod: 本地 vLLM）
```

**两层闸门的关系**（MUST 理解）：

- **SSE 流闸门**（async 层）：限制"进行中的对话流"数量，排队与预期反馈在此层实现（可异步等待并推送事件）。
- **LLM 调用闸门**（同步层）：限制"实际在途的 LLM 调用"数量，是真正的上游资源闸门。一个对话流仅在 LLM 调用期间占 1 个槽，工具执行期间释放；多 Agent 并行（`MAX_PARALLEL_WORKERS=5`）时一个流可占多个 LLM 槽，因此 **`STREAM_MAX_CONCURRENCY` × 平均并行度 ≤ `LLM_MAX_CONCURRENCY`** 必须在配置校验中保证。

---

## §4 L1 入口限流

### 4.1 方案

slowapi + Redis storage（`REDIS_URL` 已有；Redis 未配置时降级 `memory://`，仅单实例有效）。

```python
# main.py 初始化
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(
    key_func=lambda request: getattr(request.state, "api_key_id", None) or get_remote_address(request),
    storage_uri=settings.REDIS_URL or "memory://",
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)
```

### 4.2 限流维度与默认值

| 维度 | 限制 | 说明 |
| --- | --- | --- |
| API Key（SSE 端点） | `RATE_LIMIT_STREAM_PER_KEY = 10/minute` | 对话发起频率 |
| API Key（REST 端点） | `RATE_LIMIT_REST_PER_KEY = 60/minute` | 会话/历史等短请求 |
| IP（兜底） | `RATE_LIMIT_PER_IP = 120/minute` | 防未认证刷量 |

- MUST：key_func 在 `require_auth` 依赖之后取认证态 Key 标识（通过 middleware 先注入 `request.state.api_key_id`）。
- MUST：429 响应体统一为 `{"code": 42900, "message": "请求过于频繁，请稍后重试"}`，并携带 `Retry-After` 头。
- SHOULD：仅对写操作与 SSE 端点限流，只读列表接口放宽。

---

## §5 L2 会话级约束

### 5.1 会话级单流锁

同一会话同时只允许一个 Active run，不同会话允许并行。锁键为
`session_stream_lock:{tenant_id}:{user_id}:{session_id}`。同一会话冲突返回
`40901`；用户达到活动上限后新运行保持排队，队列满返回 `42902`。详细状态机、
配额和兼容行为以 `ASYNC_CONVERSATION_RUN_SPEC.md` 为准。

### 5.2 SSE 断连只释放订阅

启用 `CHAT_RUN_ENABLED` 后，SSE 连接只是 Subscriber。断连只能释放订阅等待、
连接和本地缓冲区，不得取消 `ChatRun`、Worker 租约或 LLM 运行。只有显式调用
`POST /runs/{run_id}/cancel` 才取消运行。功能关闭时，旧式流端点保留原兼容行为。

---

## §6 L3 准入控制与排队

### 6.1 SSE 流闸门（async 层）

位置：`router_chat.py` `event_stream()` 开头（前置 DB 操作之后、`astream` 之前）。

```python
# harness/stream_gate.py（新建）
class StreamGate:
    """SSE 流准入：非阻塞尝试 → 排队（推送位置）→ 超时拒绝。"""

    def __init__(self, max_streams: int, queue_capacity: int): ...
    async def acquire(self, on_position_change) -> GuardTicket: ...
    def release(self, ticket: GuardTicket) -> None: ...
```

行为序列：

1. `try_acquire`（非阻塞）成功 → 直接进入流式。
2. 失败且队列未满 → 入队等待，通过 `on_position_change` 回调推送 `queue` 事件（见 §9）；位置每变化或每 `QUEUE_POSITION_PUSH_INTERVAL_S` 秒推送一次。
3. 等待期间被唤醒 → 进入流式。
4. 排队超时（`QUEUE_TIMEOUT_SECONDS`）→ SSE error `{"code": 50400, "message": "当前使用人数较多，请稍后重试"}`。
5. 队列满（`QUEUE_CAPACITY`）→ 立即 SSE error `{"code": 42900, ...}`。

### 6.2 LLM 调用闸门（同步层）

位置：`agents/base_agent.py` `create_llm()` 返回前统一包装——**该函数是全部 LLM 调用的必经单点**（主 Agent、子 Agent、HITL 均经此创建），包装后所有链路自动纳管。

```python
# harness/llm_guard.py（新建）
class GuardedChatModel(BaseChatModel):
    """包装真实 ChatOpenAI：调用前获取槽位，带超时与熔断检查。"""

    inner: BaseChatModel
    pool: LLMPool  # online / background

    def _generate(self, ...):      # 同步路径：threading.BoundedSemaphore（带超时）
        ...
    async def _agenerate(self, ...):  # 异步路径：asyncio.Semaphore
        ...
```

- 获取槽位前 MUST 先检查熔断状态（§7），熔断开启 → 直接抛 `LLMGuardOpenError`（映射为降级链或友好错误）。
- 等待槽位超时（`LLM_SLOT_WAIT_TIMEOUT_SECONDS=90`）→ 抛 `LLMBusyError` → 上层转 50400 语义。
- `max_retries`：`ChatOpenAI(max_retries=2)` 显式配置（当前未配置，429/5xx 指数退避）。

### 6.3 在线/后台分池（MUST）

| 池 | 配置 | 覆盖调用 | 说明 |
| --- | --- | --- | --- |
| online | `LLM_MAX_CONCURRENCY` | 主对话链路 | 生产默认 **90**（100 路留 10% 余量） |
| background | `LLM_BG_MAX_CONCURRENCY` | 图谱抽取、记忆提炼、批量索引 | 默认 **8**，独立信号量，与在线互不挤占 |

判定方式：`ToolContext` 增加 `call_channel: Literal["online", "background"]` 字段，后台任务创建 LLM 时显式传入；未标记默认 online。

### 6.4 环境默认值

| 参数 | dev（DeepSeek API） | prod（本地 100 路） |
| --- | --- | --- |
| `LLM_MAX_CONCURRENCY` | 200（上游上限 2500，留突发余量） | **90** |
| `STREAM_MAX_CONCURRENCY` | 240 | 120 |
| `QUEUE_CAPACITY` | 50 | 100 |
| `QUEUE_TIMEOUT_SECONDS` | 30 | 60 |

---

## §7 L4 熔断与快速失败

### 7.1 端点级熔断器

新建 `harness/llm_guard.py::EndpointCircuitBreaker`，复用 `agent_circuit_breaker.py` 的状态机模式，key=`(base_url, model)`：

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `CB_WINDOW_SECONDS` | 60 | 滑动窗口 |
| `CB_MIN_SAMPLES` | 10 | 最小样本数 |
| `CB_ERROR_RATE_THRESHOLD` | 0.5 | 错误率阈值（超时/5xx/连接错误计入；**429 不计入**，走 §7.2） |
| `CB_COOLDOWN_SECONDS` | 30 | 熔断冷却 |
| 半开探测 | 冷却后放行 1 个请求 | 成功 → 关闭；失败 → 重新熔断 |

- 熔断开启期间：`GuardedChatModel` 直接抛 `LLMGuardOpenError`，**不排队不等待**。
- 降级链：主模型熔断 → `LLM_FALLBACK_MODELS`（逗号分隔，依次尝试）→ 全部熔断 → SSE error `{"code": 50300, "message": "模型服务暂时不可用，请稍后重试", "retry_after_ms": 30000}`。
- 阶段一进程内实现（单实例）；阶段二迁 Redis（状态 key：`cb:{base_url}:{model}`，Lua 原子更新）。

### 7.2 429 自适应收敛（AIMD）

上游 429 说明达到上游并发限制（DeepSeek 按账号计并发；本地 vLLM 受 `--max-num-seqs` 约束），应主动收敛而非重试硬打：

- 收到 429 → 有效并发上限临时下调 20%（`effective_max = max(effective_max * 0.8, LLM_MAX_CONCURRENCY // 4)`）。
- 连续 30s 无 429 → 上调 10%，直至恢复 `LLM_MAX_CONCURRENCY`。
- 开关：`CB_429_ADAPTIVE_ENABLED`（默认 true）。
- MUST：429 重试遵循 `Retry-After` 头（DeepSeek 返回时），无该头则指数退避（1s/2s/4s，最多 2 次）。

### 7.3 超时治理（对齐现有配置）

| 项 | 值 | 说明 |
| --- | --- | --- |
| 单次 LLM 调用超时 | `LLM_CALL_TIMEOUT_SECONDS=120` | ≤ DeepSeek 10 分钟断连线；对齐 `AGENT_TIMEOUT_SECONDS` |
| SSE 心跳 | 15s | 复用现有 keep-alive 机制（如有）则对齐 |
| 槽位等待超时 | `LLM_SLOT_WAIT_TIMEOUT_SECONDS=90` | §6.2 |

---

## §8 L5 降级阶梯

### 8.1 水位触发

`llm_guard.py::DegradeController`（进程内单例，阶段二迁 Redis 共享档位）：

| 档位 | 触发 | 动作 | 退出 |
| --- | --- | --- | --- |
| D0 正常 | — | 思考 low + 完整 RAG + `LLM_MAX_TOKENS` | — |
| D1 | online 池利用率 >`DEGRADE_HIGH_WATERMARK`(0.8) 持续 `DEGRADE_ENTER_HOLD_S`(60s) | 关思考模式 + `max_tokens` 减半 | 利用率 <`DEGRADE_LOW_WATERMARK`(0.7) 持续 `DEGRADE_EXIT_HOLD_S`(120s) |
| D2 | D1 下利用率仍 >0.9 持续 60s | 再裁剪 RAG 上下文（`RAG_TOP_K` 5→3、rerank 后截断） | 同上 |

- 档位变化推送 SSE `degrade` 事件 + 指标 `degrade_level`（gauge）。
- MUST：降级仅影响**新请求**，进行中的流不中断。

### 8.2 Provider 适配：思考模式

| Provider | D1 动作 |
| --- | --- |
| 本地 vLLM（Qwen3.8 等） | 透传 `thinking_mode: none`（OpenAI 兼容 extra_body） |
| DeepSeek API | flash 档本身为快速模型，D1 仅收紧 `max_tokens`；如后续官方提供思考开关参数则接入映射表 |

MUST：思考开关参数集中在 `llm_guard.py::PROVIDER_THINKING_PARAM` 映射表，禁止散落在业务代码。

### 8.3 上下文裁剪

D2 档位下，`build_system_prompt` 与消息组装前按档位裁剪（`ToolContext` 读取当前档位）。裁剪 MUST 保序：先截断历史中间消息，保留 system + 最近消息 + 附件。

### 8.4 语义缓存（阶段四，选做）

- 位置：LLMGuard 之前挂 `cache_lookup` hook；命中直接返回并标记 `cache_hit=true`。
- Key：用户消息 Embedding（复用 bge-m3）相似度 ≥0.95 的历史问答对，TTL 24h。
- 风险控制：仅对**无工具调用意图**的纯问答请求启用，默认 `SEMANTIC_CACHE_ENABLED=false`。

---

## §9 SSE 事件协议扩展

在现有事件（trace_start / message_delta / run_end / error 等）基础上新增：

```
event: queue
data: {"position": 3, "queue_size": 12, "estimated_wait_ms": 18000}

event: degrade
data: {"level": 1, "reason": "high_utilization", "effects": ["thinking_disabled", "max_tokens_reduced"]}

event: error
data: {"code": 42900, "message": "系统繁忙，请稍后重试", "retry_after_ms": 30000}
```

### 首个有效反馈

`GUARD_OUTCOME_DEADLINE_SECONDS=60` 约束从请求进入服务到首个有效反馈发出的绝对时长。以下任一前端可见 SSE 事件均视为首个有效反馈：

- `message_delta`
- `error`
- `orchestration.router_decision`
- `orchestration.clarification`
- `orchestration.plan`
- `orchestration.worker_started`
- `orchestration.worker_finished`
- `orchestration.synthesis`

首个有效反馈发出后停止该 60 秒计时，后续模型调用和图执行仍受各自响应超时约束。`queue` 事件仅用于展示排队位置，不解除 `QUEUE_TIMEOUT_SECONDS` 排队截止约束；排队超时仍按 50400 处理。

### 错误码规范

| code | 语义 | 触发层 |
| --- | --- | --- |
| 42900 | 限流/队列满 | L1 / L3 |
| 40901 | 当前会话已有 Active run | L2 |
| 42902 | 用户活动或排队运行达到限制 | L2 |
| 50300 | 模型服务熔断 | L4 |
| 50400 | 排队/槽位等待超时 | L3 |
| 50401 | 60 秒内未产生首个有效反馈 | L3 / 编排链路 |
| 50402 | 模型调用/图执行响应超时 | LLM / Graph Runtime |

前端 MUST：对 `queue` 事件渲染"排队第 N 位，预计 X 秒"；对 42901/42900/50300/50400/50401/50402 渲染友好提示并提供重试按钮（读取 `retry_after_ms`）。

---

## §10 配置项清单（config.py 新增）

```python
# --- 并发防护（CONCURRENCY_GUARD_SPEC）---
GUARD_ENABLED: bool = True

# L3 闸门与排队
LLM_MAX_CONCURRENCY: int = 200           # online 池（prod 覆写 90）
LLM_BG_MAX_CONCURRENCY: int = 8          # background 池
STREAM_MAX_CONCURRENCY: int = 240        # SSE 流闸门（prod 覆写 120）
LLM_SLOT_WAIT_TIMEOUT_SECONDS: float = 90.0
QUEUE_ENABLED: bool = True
QUEUE_CAPACITY: int = 50                 # prod 覆写 100
QUEUE_TIMEOUT_SECONDS: float = 30.0      # prod 覆写 60
QUEUE_POSITION_PUSH_INTERVAL_S: float = 5.0
GUARD_OUTCOME_DEADLINE_SECONDS: float = 60.0

# L2 会话级
USER_STREAM_LOCK_ENABLED: bool = True
USER_STREAM_LOCK_TTL_S: int = 180

# L1 限流
RATE_LIMIT_ENABLED: bool = True
RATE_LIMIT_STREAM_PER_KEY: str = "10/minute"
RATE_LIMIT_REST_PER_KEY: str = "60/minute"
RATE_LIMIT_PER_IP: str = "120/minute"

# L4 熔断
CB_ENABLED: bool = True
CB_WINDOW_SECONDS: int = 60
CB_MIN_SAMPLES: int = 10
CB_ERROR_RATE_THRESHOLD: float = 0.5
CB_COOLDOWN_SECONDS: int = 30
CB_429_ADAPTIVE_ENABLED: bool = True
LLM_FALLBACK_MODELS: str = ""            # 逗号分隔降级模型
LLM_CALL_TIMEOUT_SECONDS: float = 120.0

# L5 降级
DEGRADE_ENABLED: bool = True
DEGRADE_HIGH_WATERMARK: float = 0.8
DEGRADE_LOW_WATERMARK: float = 0.7
DEGRADE_ENTER_HOLD_S: int = 60
DEGRADE_EXIT_HOLD_S: int = 120

# 阶段四
SEMANTIC_CACHE_ENABLED: bool = False
```

MUST：启动时校验 `STREAM_MAX_CONCURRENCY * MAX_PARALLEL_WORKERS <= LLM_MAX_CONCURRENCY` 不成立时打 WARNING（多 Agent 默认关闭，单流并行度 1，通常满足）。

---

## §11 Provider 适配（dev: DeepSeek API / prod: 本地 vLLM）

### 11.1 开发环境（DeepSeek V4 Flash）

```bash
# .env（dev）
LLM_MODEL=deepseek-v4-flash
LLM_BASE_URL=https://api.deepseek.com
LLM_API_KEY=sk-***
LLM_MAX_CONCURRENCY=200
```

上游特性与对策（来源：DeepSeek 官方「限速与隔离」文档）：

| 特性 | 对策 |
| --- | --- |
| 并发限制 **2500/账号**（按账号计，与 Key 数量无关） | dev 闸门 200 远低于上限；策略照常生效，为 prod 无缝切换做准备 |
| 超限返回 HTTP 429 | §7.2 AIMD 自适应收敛 + `max_retries=2` 退避 |
| 流式响应持续发送 `: keep-alive` 注释行 | SSE 解析按 `event:`/`data:` 前缀匹配，注释行自然忽略（现有 `_extract_stream_delta` 兼容，验收时确认） |
| 10 分钟未开始推理则断连 | `LLM_CALL_TIMEOUT_SECONDS=120` 远低于该线 |
| 提升配额后按 `user_id` 隔离限速 | SHOULD：`create_llm` 透传 `model_kwargs={"user": user_id}`（取 ToolContext），为扩容后公平限速预留 |

### 11.2 生产环境（本地 vLLM）

```bash
# .env（prod）
LLM_MODEL=Qwen3.8-27B-W8A8            # 或甲方部署的模型
LLM_BASE_URL=http://<vllm-host>:8000/v1
LLM_MAX_CONCURRENCY=90
STREAM_MAX_CONCURRENCY=120
QUEUE_CAPACITY=100
QUEUE_TIMEOUT_SECONDS=60
```

- vLLM 侧 MUST 配置 `--max-num-seqs 100` 与应用层闸门形成双保险。
- 切换 provider 仅改环境变量，策略代码零改动（§1.3 原则 1）。

---

## §12 观测指标与告警

### 12.1 新增指标（接入现有 metrics 体系）

| 指标 | 类型 | 说明 |
| --- | --- | --- |
| `guard_llm_inflight{pool}` | gauge | 各池在途 LLM 调用数 |
| `guard_stream_inflight` | gauge | 进行中 SSE 流数 |
| `guard_queue_length` | gauge | 排队长度 |
| `guard_queue_wait_ms` | histogram | 排队等待时长（P95 告警） |
| `guard_rejected_total{reason}` | counter | 拒绝数（reason: rate_limit / user_lock / queue_full / queue_timeout / llm_slot_timeout / first_feedback_timeout / model_response_timeout / cb_open） |
| `guard_upstream_429_total` | counter | 上游 429 次数 |
| `cb_state{endpoint}` | gauge | 0 closed / 1 half_open / 2 open |
| `degrade_level` | gauge | 当前降级档位 0/1/2 |
| `stream_cancelled_total` | counter | 断连取消数 |
| `llm_orphan_calls_total` | counter | 断连后残留完成的 LLM 调用数 |

### 12.2 告警阈值（复用 SLO 机制）

| 条件 | 级别 |
| --- | --- |
| `guard_queue_wait_ms` P95 > 30s 持续 15min | WARNING（容量接近上限，考虑扩容/降级） |
| `guard_rejected_total{reason=queue_full}` 增速 > 10/min 持续 5min | CRITICAL |
| `cb_state` = open 持续 > 5min | CRITICAL（上游故障） |
| `degrade_level` ≥ 1 持续 > 30min | WARNING（长期超载） |
| `guard_upstream_429_total` 增速异常 | WARNING（AIMD 已收敛仍频繁 429 → 上游配额不足） |

---

## §13 实施计划

| 里程碑 | 内容 | 涉及文件 | 验收 |
| --- | --- | --- | --- |
| **M1（第 1 周）核心防雪崩** | `llm_guard.py`（GuardedChatModel + 双池信号量 + EndpointCircuitBreaker + AIMD）；`create_llm()` 接入；SSE error 码落地 | `harness/llm_guard.py`（新）、`agents/base_agent.py`、`harness/context.py`、`config.py` | §14 场景 3/4 |
| **M2（第 1~2 周）准入与排队** | `stream_gate.py`（流闸门 + 排队 + queue 事件）；每用户单流锁；断连取消 | `harness/stream_gate.py`（新）、`api/router_chat.py`、前端 SSE 处理 | §14 场景 1/2/5 |
| **M3（第 2~3 周）限流与降级** | slowapi 接入；DegradeController + 思考开关/上下文裁剪；§12 指标全量 | `main.py`、`llm_guard.py`、`agents/base_agent.py`、观测台 | §14 场景 6 + 指标可见 |
| **M4（按需）** | 语义缓存；Redis 分布式化（熔断/闸门/档位，多副本前置条件） | `llm_guard.py`、`stream_gate.py` | 多副本压测 |

依赖关系：M1 → M2 → M3 → M4；M1/M2 完成前禁止宣称支持生产并发。

---

## §14 测试与验收标准

### 14.1 k6 场景（脚本放 `backend/tests/load/`）

| # | 场景 | 配置 | 通过标准 |
| --- | --- | --- | --- |
| 1 | P0 稳态 | 30 VU SSE 持续 10min | 零 429/503/504；TTFT P95 < 3s（对齐 `SLO_P95_TTFT_MS`） |
| 2 | 突刺 | 30→150 VU 阶跃 | 无 5xx；超载请求收到 queue 事件且位置单调递减；排队 P95 < 60s |
| 3 | 上游故障注入 | mock LLM 返回 500 | 熔断 30s 内开启；期间请求 ≤1s 内收到 50300；无请求悬挂 |
| 4 | 上游 429 注入 | mock LLM 返回 429 | AIMD 生效：inflight 收敛，429 率 30s 内下降 |
| 5 | 单用户滥用 | 同 Key 5 并发流 | 仅 1 路放行，其余 42901 |
| 6 | 降级触发 | 压至利用率 >80% 持续 60s | degrade 事件推送；max_tokens 实际减半；利用率回落后 120s 退出 D1 |
| 7 | 断连回收 | 建流后 5s 断开 | `guard_llm_inflight` 5s 内回落；`stream_cancelled_total` +1 |

### 14.2 体验承诺（向甲方汇报口径）

- P0（≤300 人在线）：可用性 99.5%，TTFT P95 < 3s，零排队。
- P1（300~500 人）：排队 P95 < 30s，拒绝率 < 1%。
- P2（500~800 人）：降级自动生效，拒绝率 < 5%，无黑盒悬挂。
- 任何情况下：请求在 60s 内开始生成、展示前端可见的编排进度，或收到明确的错误码与重试指引。

---

## §15 风险与开放问题

| # | 风险/问题 | 处置 |
| --- | --- | --- |
| 1 | `agent_node` 同步 invoke 在 executor 线程，断连后线程内调用不可中断 | §5.2 已声明边界；观察 `llm_orphan_calls_total`，若显著再评估迁移 async invoke |
| 2 | 多 Agent 开启后单流占多槽，闸门配置失衡 | 启动校验 WARNING（§10）；开启 `MULTI_AGENT_ENABLED` 时建议 `STREAM_MAX_CONCURRENCY` 下调 |
| 3 | DeepSeek 官方并发政策变化（当前 2500/账号） | 策略层不依赖具体数值，AIMD 自适应兜底；扩容工单免费可提 |
| 4 | 排队事件增加 SSE 帧量 | 位置变化节流（§6.1），单流额外帧 < 0.2/s |
| 5 | 语义缓存误命中（相似但答案不同的问题） | 默认关闭；仅纯问答启用；命中标注 `cache_hit` 便于审计回滚 |
| 6 | 进程内状态（闸门/熔断/档位）在多 worker 下分裂 | 阶段一按 worker 均分额度（`LLM_MAX_CONCURRENCY // workers`）；阶段二 Redis 统一 |

---

## 附：关键代码落点索引

| 文件 | 改动 |
| --- | --- |
| `backend/app/harness/llm_guard.py` | 新建：GuardedChatModel、LLMPool、EndpointCircuitBreaker、AIMD、DegradeController |
| `backend/app/harness/stream_gate.py` | 新建：SSE 流闸门 + 排队 |
| `backend/app/agents/base_agent.py:36-57` | `create_llm()` 返回前包装 guard；`max_retries=2` |
| `backend/app/api/router_chat.py:554` | `event_stream()` 开头接入准入/排队；finally 释放 |
| `backend/app/harness/context.py` | ToolContext 增加 `call_channel`、降级档位读取 |
| `backend/app/config.py` | §10 配置段 |
| `backend/app/main.py` | slowapi 初始化 + 429 handler |
| `backend/app/harness/agent_circuit_breaker.py` | 模式参考（不改动） |
| `backend/tests/load/` | 新建：k6 场景脚本 |
