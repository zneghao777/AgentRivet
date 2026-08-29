# 多 Agent 协同编排 · 实施规格书（M2 交付）

> 版本：v1.0.0（草案）
> 日期：2026-08-30
> 状态：待评审
> 里程碑代号：MULTI-AGENT-ORCH
> 上游方案：[MULTI_AGENT_ORCHESTRATION_SPEC.md](./MULTI_AGENT_ORCHESTRATION_SPEC.md)
> 关联文档：[AGENT_HUB_SPEC.md](./AGENT_HUB_SPEC.md)、[OBSERVABILITY_SPEC.md](./OBSERVABILITY_SPEC.md)、[design-qa.md](../design-qa.md)

本文是 M2 多 Agent 编排的**可实施规格**，覆盖数据模型、运行时节点、SSE 协议、
API、配置、可观测性、高可用、**前端交互与视觉设计**、测试验收与迁移。
实现以本文为准；上游方案文档只保留架构决策与调研结论。

---

## 0. 决策摘要

1. ★ 编排沿用 LangGraph，Supervisor 图内新增 5 类节点：
   `router → planner → worker*（并行）→ verifier → synthesizer`。
2. ★ Agent 间交接唯一协议为 `TaskCard`（Pydantic v2 模型），落库表 `agent_tasks`。
3. ★ 意图识别 = LLM 结构化输出路由，候选集来自 AgentHub（`scope in (user, internal)`
   且 `status=active`），置信度 < 0.5 触发澄清反问。
4. ★ 并行用 Send API fan-out，`agent_outputs` 用 `operator.add` reducer 合并，
   并发闸门 `MAX_PARALLEL_WORKERS=5`。
5. ★ 交接四道防线：入口 schema 校验 → 上下文打包检查 → Verifier 校验+重试 1 次
   → task_id 全链路 trace + 降级输出。
6. ★ SSE 新增 6 个编排事件（§4），前端新增 `OrchestrationPanel` 等 4 个组件（§9）。
7. ★ 前端视觉严格沿用蓝白磨砂玻璃体系（`global.css` token），新增编排语义色与
   Worker 身份色板（§9.2），不引入新风格。
8. `MULTI_AGENT_ENABLED=false` 时全部新逻辑不激活，行为与现状逐字节一致。
9. Supervisor 不绑定任何业务工具；`SUPERVISOR_MODEL` 独立配置（默认跟随全局）。
10. Worker 间禁止直接对话（星型拓扑），一切经 Supervisor 中转。

---

## 1. 范围与里程碑

| 里程碑 | 交付内容 | 预估 |
|--------|---------|------|
| **M2a** | Router 节点 + 子 Agent 真实执行 + 澄清反问 + AgentHub internal 管理与路由模拟器 | 1~1.5 周 |
| **M2b** | Planner + Send API 并行 + 两级聚合 + 四道防线 + `agent_tasks` 表 + OrchestrationPanel | 2~3 周 |
| **M2c**（按需） | Hierarchical / DAG / A2A 导出 / Planner 后 HITL / 路由缓存 | 排期另定 |

M2a 与 M2b 可独立上线：M2a 只有单选路由（无并行），M2b 增加编排。
前端组件按里程碑分两批交付（§9.3 标注）。

---

## 2. 数据模型

### 2.1 核心模型（`harness/models.py` 新增）

```python
from pydantic import BaseModel, Field
from typing import Literal
import uuid


class TaskCard(BaseModel):
    """Agent 间交接的唯一协议。"""
    task_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    parent_task_id: str | None = None
    target_agent: str
    goal: str = Field(min_length=1, description="子任务目标，一句话")
    context_brief: str = Field(default="", max_length=2000,
                               description="必要上下文摘要，假设 Worker 看不到对话历史")
    constraints: list[str] = []
    expected_output: str | None = Field(
        default=None, description="期望输出描述（自然语言即可，不做 JSON schema 强约束）")
    artifacts: list[dict] = []          # 产物引用：{"type": "kb_doc"|"file", "id": ...}
    status: Literal["pending", "running", "done", "failed"] = "pending"
    result: str | None = None
    error: str | None = None
    started_at: float | None = None
    finished_at: float | None = None


class RouterDecision(BaseModel):
    """路由决策：LLM 结构化输出，禁止自由文本。"""
    mode: Literal["direct", "orchestrate"]
    target_agent: str = "main"          # direct 时的目标 Agent
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, description="必须引用候选 Agent 的具体能力描述")
    needs_clarification: bool = False
    clarification_question: str | None = None
    clarification_options: list[str] = []   # 最多 3 个快捷选项


class Plan(BaseModel):
    """Planner 产出：编排模式下的任务分解。"""
    tasks: list[TaskCard] = Field(min_length=1, max_length=5)
    execution: Literal["parallel", "sequential"] = "parallel"
    summary: str = Field(description="面向用户的一句话计划说明")
```

约束：
- `Plan.tasks` 上限 5（M2b 不支持更深分解，防止成本失控）；
- `TaskCard.context_brief` 上限 2000 字符（Planner prompt 同步声明）；
- 所有模型 `model_config = ConfigDict(extra="forbid")`，LLM 输出多余字段即校验失败。

### 2.2 `agent_tasks` 表（`storage/models.py` 新增）

```python
class AgentTask(Base):
    __tablename__ = "agent_tasks"

    id = Column(UUID, primary_key=True, default=uuid.uuid4)
    task_id = Column(String(32), index=True, nullable=False)      # TaskCard.task_id
    parent_task_id = Column(String(32), index=True, nullable=True)
    session_id = Column(UUID, ForeignKey("sessions.id"), index=True)
    message_id = Column(UUID, nullable=True)                      # 触发消息
    target_agent_id = Column(UUID, ForeignKey("agents.id"), nullable=True)
    target_agent_name = Column(String(64), nullable=False)
    goal = Column(Text, nullable=False)
    context_brief = Column(Text)
    status = Column(String(16), nullable=False, default="pending")  # pending/running/done/failed
    result = Column(Text)
    error = Column(Text)
    duration_ms = Column(Integer)
    trace_id = Column(String(64), index=True)                     # 关联 observability trace
    created_at = Column(DateTime, server_default=func.now())
```

落库粒度（上游方案开放问题 5 的决策）：**每个 TaskCard 一行，状态终态时更新
result/error/duration**；重试中间记录只进 trace，不落库。

### 2.3 State 扩展（`harness/state.py`）

```python
import operator

class SupervisorState(HarnessState):
    route: str = "main"
    router_decision: dict = {}                                   # RouterDecision.model_dump()
    plan: list[dict] = []                                        # Plan.tasks（TaskCard dump）
    agent_outputs: Annotated[list[dict], operator.add] = []      # 并行结果合并
    aggregated: dict = {}                                        # 机械聚合产物
    clarification: dict = {}                                     # 澄清反问（needs_clarification 时）
```

---

## 3. 运行时架构

### 3.1 图结构（改造 `harness/supervisor.py`）

```
START → router ─┬─ needs_clarification → END（SSE 返回澄清卡）
                ├─ direct → sub_{target} 或 main → verifier → END
                └─ orchestrate → planner ─┬─ parallel → Send fan-out → worker* → aggregate
                                          └─ sequential → 链式 worker → aggregate
aggregate → verifier → synthesizer → END
```

- `MULTI_AGENT_ENABLED=false`：返回现有 `build_main_graph`（保留 `supervisor.py:31-33`）；
- Worker 节点动态注册：启动时从 AgentHub 读取 `scope=internal` + `status=active`
  的 Agent，每个注册为 `sub_{name}` 节点；`main` 始终注册；
- 节点注册在 `harness_app.build()` 时完成（进程启动期），运行期不增删节点；
  AgentHub 增删 Agent 后需重启生效（M2a 简化，M2c 可做热加载）。

### 3.2 Router 节点（`harness/router.py` 新增）

```python
ROUTER_SYSTEM_PROMPT = """你是任务路由器。根据用户消息与候选 Agent 列表，决定处理方式。

# 候选 Agent
{agent_cards}

# 规则
1. 只能路由到列出的 Agent；均不合适 → target_agent="main"。
2. 任务需要 ≥2 个 Agent 的能力、或可拆分为独立并行子任务 → mode="orchestrate"。
3. confidence < {threshold} → needs_clarification=true，给出澄清问题与
   至多 3 个候选项；禁止猜测路由。
4. reason 必须引用候选 Agent description 中的具体能力，不得空泛。
5. 单 Agent 能完成的简单任务一律 direct，禁止过度编排。"""

ROUTER_OUTPUT_SCHEMA = RouterDecision.model_json_schema()
```

- **Agent Card 渲染**：`name + description + 能力摘要`。能力摘要由 resolver
  输出拼装：`Skills: a, b | MCP: x, y | KB: z`（各取前 3 个，超出加 `…`）；
- **候选过滤**：SQL 层过滤租户 + `status=active`；候选 > 15 时按 description
  与消息的嵌入相似度取 top-15（M2a 先不做粗筛，直接全量注入，超限再启用）；
- **校验与重试**：`RouterDecision.model_validate_json` 失败 → 重试 1 次
  （错误信息附入 prompt）→ 仍失败 → 回退 `direct/main` 并记 trace 警告；
- **置信度阈值**：`ROUTER_CONFIDENCE_THRESHOLD=0.5`（配置项）；
- Router LLM：`SUPERVISOR_MODEL`（默认跟随 `LLM_MODEL`），`temperature=0`。

### 3.3 Planner 节点（`harness/planner.py` 新增，M2b）

```python
PLANNER_SYSTEM_PROMPT = """你是任务规划器。将用户请求分解为至多 5 个可并行或
顺序执行的子任务，每个子任务指派给一个候选 Agent。

# 硬性要求
1. 每个 TaskCard.context_brief 必须自包含：假设执行者看不到对话历史，
   用户消息中的数字、日期、名称、文件名等关键信息必须原样复制进 brief；
2. 子任务之间不得有隐式依赖；确需顺序执行时 execution="sequential"；
3. goal 用动词开头的一句话；constraints 写明格式/字数/禁止事项；
4. 不得创建没有对应候选 Agent 的子任务。"""
```

- 输出 `Plan` 模型，校验失败重试 1 次，仍失败 → 降级为
  `direct/main`（记 trace 警告）；
- `plan.summary` 面向用户展示（"我将并行执行 3 个子任务：…"）。

### 3.4 Worker 节点（改造 `supervisor.py:_sub_node`）

```python
def _sub_node(entry: AgentEntry):
    def _n(state: dict) -> dict:
        task = TaskCard(**state["task"])              # Send 携带的独立副本
        # 1. 入口校验（防线1）
        if not task.goal.strip():
            return {"agent_outputs": [{"task_id": task.task_id, "status": "failed",
                                       "error": "invalid_task_card"}]}
        # 2. 能力注入：resolver 按 entry.agent_id 解析（复用现有链路）
        # 3. system prompt = AgentHub 提示词 + TaskCard(goal/constraints) + 职责边界段
        # 4. 复用 agent_node 的 LLM 调用 + 工具循环（bind_tools）
        # 5. 输出写 agent_outputs（reducer 合并），TaskCard 状态落库
    return _n
```

- Worker 的 system prompt 追加职责边界段（固定模板）：

```
# 职责边界
你只负责：{description}。超出此范围时，输出「CANNOT_HANDLE: 原因」，
不要尝试硬做。
```

- Worker 输出以 `CANNOT_HANDLE:` 开头 → 直接标记 `failed`
  （error="agent_declined"），不重试（职责边界问题重试无意义）；
- Worker 级超时：`WORKER_TIMEOUT_SECONDS`（默认 = `AGENT_TIMEOUT_SECONDS`）；
- Worker 失败重试 1 次（`agent_declined` 除外），重试时把上次 error 附入 prompt。

### 3.5 Verifier 节点（`harness/verifier.py` 新增，M2b）

- **代码级校验**（全部结果）：`result` 非空、非纯拒答模板、长度 ≥ 20 字符；
- **LLM Critic**（可选，`VERIFIER_LLM_ENABLED=false` 默认关）：判断 result
  是否回答了 goal，输出 `{pass: bool, reason: str}`；
- 校验失败 → 重派该 Worker 1 次（携带失败原因）→ 再失败 → 保持 failed，
  交给聚合层降级。

### 3.6 Synthesizer 节点（`harness/synthesizer.py` 新增，M2b）

```python
SYNTHESIZER_PROMPT = """你是结果综合器。以下多个 Agent 分别完成了子任务，
请综合为面向用户的最终回答。

# 要求
1. 忠实于各分支结果，不得编造；
2. 分支结论冲突时显式标注（"关于 X，A 认为…，B 认为…"），不得擅自裁决；
3. 存在失败分支：说明缺失部分及原因，基于成功部分给出可用回答；
4. 关键结论标注来源 Agent。
"""
```

- 输入：`aggregated.results`（按 task_id 稳定排序）+ `aggregated.failures`；
- 输出写入 `final_answer`，走现有 SSE 文本流通道（`StreamingText` 渲染）。

### 3.7 Agent-as-Tool 委派（M2b）

- 主 Agent（`main` 节点）动态注册工具 `delegate_to_{agent_name}`，
  参数 schema：`{"goal": str, "context_brief": str}`；
- 工具执行 = 同步调用对应 Worker 逻辑（复用 `_sub_node` 内核），
  返回 `result` 文本；
- 仅 `scope=internal` 的 Agent 暴露为委派工具；`user` scope 的 Agent
  不注册（用户可直接切换会话）。

---

## 4. SSE 事件协议（前后端契约）

在现有 SSE 帧基础上新增 `orchestration.*` 事件（`graph_runtime.py` 转换层）。
帧格式沿用现有 `{type, payload, ts}`。

| 事件 | payload | 时机 |
|------|---------|------|
| `orchestration.router_decision` | `{mode, target_agent, confidence, reason, needs_clarification}` | Router 节点完成后 |
| `orchestration.clarification` | `{question, options[]}` | needs_clarification=true 时（随后 run 正常结束） |
| `orchestration.plan` | `{summary, execution, tasks: [{task_id, target_agent, goal}]}` | Planner 完成后 |
| `orchestration.worker_started` | `{task_id, agent_name}` | 每个 Worker 开始 |
| `orchestration.worker_finished` | `{task_id, status, duration_ms, result_preview(≤120字)}` | 每个 Worker 结束 |
| `orchestration.synthesis` | `{phase: "started"}` | Synthesizer 开始（其后文本走现有流式通道） |

约束：
- `result_preview` 截断 120 字符，完整结果不进 SSE（防泄漏与带宽）；
- 事件顺序保证：`router_decision → (clarification | plan → worker_* → synthesis)`；
- 前端对未知 `orchestration.*` 事件必须忽略（向前兼容）。

---

## 5. API 变更

| 方法 | 路径 | 说明 | 里程碑 |
|------|------|------|--------|
| POST | `/api/admin/agents/{id}/route-dry-run` | 路由模拟器：body `{message}` → 返回 `RouterDecision`（不执行任务，只跑 Router） | M2a |
| GET | `/api/sessions/{id}/tasks` | 会话的 TaskCard 列表（回放用，按 created_at 升序） | M2b |
| GET | `/api/observability/router-stats` | 路由命中率统计：按 target_agent 聚合 `{total, by_agent: {name: {count, avg_confidence}}}` | M2a |

- `route-dry-run` 权限：管理员（复用 `router_admin.py` 的鉴权依赖）；
- 现有 `/api/chat` 请求体不变（编排由 Router 自动决策，前端无需传编排开关）。

---

## 6. 配置项（`config.py` 新增）

```python
# --- Multi-Agent Orchestration（MULTI_AGENT_ORCH）---
ROUTER_CONFIDENCE_THRESHOLD: float = 0.5
SUPERVISOR_MODEL: str | None = None          # None → 跟随 LLM_MODEL
MAX_PARALLEL_WORKERS: int = 5
PLAN_MAX_TASKS: int = 5
WORKER_TIMEOUT_SECONDS: float = 120.0        # 默认 = AGENT_TIMEOUT_SECONDS
WORKER_RETRY: int = 1
VERIFIER_LLM_ENABLED: bool = False
AGENT_CIRCUIT_BREAKER_THRESHOLD: int = 3     # 连续失败 N 次熔断
AGENT_CIRCUIT_BREAKER_COOLDOWN: int = 600    # 熔断冷却（秒）
```

---

## 7. 可观测性

1. **trace**：Router/Planner/Worker/Verifier/Synthesizer 各自产生 span，
   `parent_ids` 按 LangGraph 事件树传递（`graph_runtime.py:190-230` 既有机制）；
   Worker span 携带 `task_id` 属性，与 `agent_tasks.trace_id` 互查；
   **span 落库自动复用既有 MQ 管道**：`tracing.py` 已将 `obs.span` 发布至
   RabbitMQ（`obs.exchange → obs.spans.q`），由 `obs_worker` 异步落库，
   编排侧零新增代码；`MQ_ENABLED=false` 时走进程内直写降级路径；
2. **指标**（`observability/metrics.py` 新增）：
   - `orch_router_decisions_total{mode, target_agent}`（路由分布）
   - `orch_router_confidence_bucket`（置信度直方图）
   - `orch_worker_duration_ms{agent_name, status}`（Worker 耗时）
   - `orch_plan_tasks_total`（每次编排的子任务数分布）
   - `orch_clarification_total`（澄清反问次数）
3. **审计**：`RouterDecision` 全量写入 trace span attributes（含 reason），
   支持误路由归因。

---

## 8. 高可用

| 故障 | 机制 | 落点 |
|------|------|------|
| Router 输出不合法 | 重试 1 次 → 回退 direct/main | §3.2 |
| Worker 超时 | `WORKER_TIMEOUT_SECONDS` + 重试 1 次 | §3.4 |
| Worker 持续失败 | 熔断：连续 `AGENT_CIRCUIT_BREAKER_THRESHOLD` 次失败后，冷却期内 Router 候选集剔除该 Agent | 内存态 `agent_circuit_breaker.py` |
| 部分分支失败 | 聚合层降级：Synthesizer 基于成功分支回答 + 标注缺失 | §3.6 |
| 全部分支失败 | 返回错误说明 + 建议重试（不静默吞错） | §3.6 |
| Planner 分解失败 | 降级 direct/main | §3.3 |
| 进程崩溃 | Postgres Checkpointer 恢复；`agent_tasks` 中 status=running 的行由启动时清扫为 failed | `main.py` 启动钩子 |
| LLM 配额 | `MAX_PARALLEL_WORKERS` 闸门 | §3.1 |
| 高危操作 | `GRAPH_INTERRUPT_TOOLS` HITL（既有，不变） | config |
| 编排整体异常 | `MULTI_AGENT_ENABLED=false` 一键回退 | 既有 |
| 旁路发布失败（span/审计/TaskCard 审计事件） | 复用 `publish_nowait` 的 fallback 模式：MQ 不可用时降级进程内执行，不阻塞编排主链路 | `mq/publisher.py:43-56` 既有 |

> 编排关键路径（Router→Planner→Worker→Synthesizer）**不引入 RabbitMQ**，
> 决策理由与未来演进边界见 §13。

---

## 9. 前端设计规格

### 9.1 设计原则与现有体系衔接

1. **不引入新风格**。严格沿用蓝白磨砂玻璃体系（`frontend/src/styles/global.css`
   的 token 与 `design-qa.md` 验收基准）：半透明白表面 + `backdrop-filter: blur`
   + 14~26px 圆角 + 蓝色主行动色。
2. **信息密度优先**。编排进度是数据面板不是插画：紧凑行高、状态点、
   等宽数字（tabular numerals），无装饰性动效。
3. **渐进披露**。默认只展示「阶段条 + Worker 状态行」；结果摘要、
   路由理由、任务卡详情折叠，点击展开。
4. **技术栈不变**：React + antd + 自定义 CSS（BEM 风格类名）+
   Material Symbols 图标 + 现有 SSE 消费链路（`stores/` + `ChatWindow`）。

### 9.2 色彩系统

#### 9.2.1 沿用的现有 token（`global.css`，不改动）

| Token | 值 | 用途 |
|-------|-----|------|
| `--brand-500` | `#2f7fe6` | 主行动色、选中态、进行中状态 |
| `--brand-600` | `#1d62c3` | 主按钮 hover 加深 |
| `--success` | `#169b91` | 成功、done 状态 |
| `--warning` | `#d98b27` | 降级、部分失败、等待确认 |
| `--danger` | `#d9505d` | 失败、熔断、删除 |
| `--bg-card` | `rgba(255,255,255,.68)` | 玻璃卡片表面 |
| `--border-soft` | `1px solid rgba(151,190,226,.52)` | 卡片描边 |
| `--gray-400~600` | — | 次级文字、禁用态 |
| `--r-md ~ --r-3xl` | 14~26px | 组件圆角 |
| `--dur-fast/base/slow` | 160/240/400ms | 动效时长 |

#### 9.2.2 新增 token（追加到 `global.css :root`）

```css
/* 编排语义色 */
--orch-router: #7c5cd6;        /* Router/Planner 阶段（紫，区别于执行态蓝） */
--orch-synth: #0e8f8f;         /* Synthesizer 阶段（深青） */
--orch-queued: var(--gray-400);/* 排队中 */
--orch-running: var(--brand-500);
--orch-done: var(--success);
--orch-failed: var(--danger);
--orch-degraded: var(--warning);
--orch-track: rgba(151, 190, 226, .28);  /* 阶段条轨道 */

/* Worker 身份色板：按 agent_name 稳定哈希取色，用于头像/描边/进度 */
--agent-c1: #2f7fe6;  /* 蓝 */
--agent-c2: #7c5cd6;  /* 紫 */
--agent-c3: #0e9f8f;  /* 青 */
--agent-c4: #d9822b;  /* 橙 */
--agent-c5: #c2569b;  /* 品红 */
--agent-c6: #3f9d4f;  /* 绿 */
```

取色规则：`hash(agent_name) % 6 → --agent-cN`，同一 Agent 在任务卡、
进度行、trace 树中颜色一致；与语义状态色正交（状态用状态点表达，
身份用头像底色/描边表达，不混用）。

对比度要求：所有新增色在 `--bg-card` 玻璃表面上的文字对比度 ≥ 4.5:1；
身份色只用于图形元素（头像、描边、进度条），不直接作正文色。

### 9.3 信息架构与页面变更

| 页面/区域 | 变更 | 里程碑 |
|----------|------|--------|
| `ChatPage` 消息流 | 助手消息上方内嵌 `OrchestrationPanel`；澄清时插入 `ClarificationCard` | M2a(路由行) / M2b(完整面板) |
| `AgentHubPage` | ① 表单增加 scope 编辑与 description 四要素引导；② 新增「路由模拟器」区块 | M2a |
| `observability/Overview` | 新增「路由分布」指标卡（调 `/api/observability/router-stats`） | M2a |
| `observability/Traces` | 编排 trace 按 parent_ids 渲染树状缩进，Worker span 带 `task_id` 徽标 | M2b |
| `SessionsPage` | 会话详情增加「任务回放」入口（调 `/api/sessions/{id}/tasks`） | M2b（可选） |

### 9.4 组件规格

#### 9.4.1 `OrchestrationPanel`（`components/chat/OrchestrationPanel.tsx`，M2b 核心）

消息气泡上方的玻璃卡片，展示一次编排的全过程。扩展而非替代
`RunProcessPanel`：单 Agent 直派时仍走 RunProcessPanel 原样渲染。

```
┌──────────────────────────────────────────────────────────┐
│ ◈ 编排执行                                    置信度 92%  │  ← 面板头（eyebrow 风格）
│ ──●路由──●计划──●执行(3)──○校验──○综合──                 │  ← 阶段条
│                                                          │
│ 计划：并行执行 3 个子任务                                  │  ← plan.summary
│ ┌────────────┐ ┌────────────┐ ┌────────────┐             │
│ │ ◉ 数据分析  │ │ ◉ 报告撰写  │ │ ○ 图表生成  │             │  ← Worker 卡片网格
│ │ 运行中 12s  │ │ 排队       │ │ 排队       │             │
│ └────────────┘ └────────────┘ └────────────┘             │
│ ▸ 查看路由理由                                            │  ← 折叠详情
└──────────────────────────────────────────────────────────┘
```

结构（类名前缀 `orch-`）：

- `.orch-panel`：`--bg-card` 玻璃表面 + `--border-soft` + `--r-xl` 圆角 +
  `--shadow-sm`；内边距 16px；最大宽度与消息气泡一致；
- `.orch-stages`（阶段条）：横向 5 段，每段 = 状态点(10px 圆) + 标签(12px)；
  轨道 `--orch-track` 2px；完成段填充对应语义色，进行段脉冲动画；
- `.orch-worker-grid`：`display: grid; grid-template-columns:
  repeat(auto-fill, minmax(180px, 1fr)); gap: 10px`；
- `.orch-worker-card`：
  - 头像（28px 圆角方块，身份色 12% 透明底 + 身份色图标）+ Agent 名(13px/600)；
  - 状态行：状态点(8px) + 状态文案(12px `--gray-500`) + 耗时；
  - 状态映射：`pending→排队(gray-400)`、`running→运行中(brand-500 脉冲)`、
    `done→完成(success)`、`failed→失败(danger)`；
  - `done/failed` 后整卡可点击 → 展开 `result_preview`（12px，`--gray-600`，
    最多 3 行截断）；
- `.orch-degraded-banner`（部分失败横幅）：`--warning` 8% 透明底 +
  `--warning` 文字 + `warning` 图标：「2/5 子任务失败，已基于成功结果回答」；
- `.orch-reason`（折叠详情）：路由 reason + 各任务卡 goal/constraints 列表，
  12px `--gray-600`。

数据消费：组件内维护 `Map<task_id, WorkerState>`，按 §4 事件增量更新；
SSE 断线时保留已收状态并显示「连接中断」灰条（复用现有断线处理）。

#### 9.4.2 `TaskChip`（M2a）

轻量路由指示，M2a 阶段 OrchestrationPanel 未就绪时的过渡形态，
M2b 后保留用于 `direct` 模式：

```
◉ 已路由 → 数据分析 Agent · 置信度 92%
```

- 行内 chip：`--r-full` 圆角、`--brand-500` 10% 透明底、12px 文字；
- 点击展开 reason（Popover，玻璃底）。

#### 9.4.3 `ClarificationCard`（M2a）

`needs_clarification=true` 时插入消息流（用户侧渲染为系统卡片，非 AI 气泡）：

```
┌────────────────────────────────────────────┐
│ ❓ 需要确认                                  │
│ 您想分析销售数据，还是生成销售报告？            │
│ [ 分析销售数据 ]  [ 生成销售报告 ]  [ 其他 ]   │  ← 快捷选项
└────────────────────────────────────────────┘
```

- 卡片：`--bg-card` 玻璃 + `--orch-router` 8% 透明底 + `--r-lg`；
- 快捷选项按钮：白底玻璃、`--border-soft` 描边、hover 变 `--brand-500`
  10% 底；点击 = 以该选项文本作为用户消息发送（复用现有发送链路）；
- 「其他」= 聚焦底部输入框（不预填）；
- 发送后卡片变为已选态（选中项 `--brand-500` 实底白字），不可重复点击。

#### 9.4.4 AgentHubPage 变更（M2a）

1. **表单（Drawer）新增**：
   - `scope` 字段：`Select`（用户可选 / 内部编排），`internal` 选项旁
     灰字提示「仅供编排调用，不出现在会话选择器」；
   - description 四要素引导：placeholder 示例
     `负责【XX 类任务】。能：…。不能：…。输入：…；输出：…`；
     保存时前端软校验：缺少「负责」与「不能」关键字 → `Form.Item` 下方
     `--warning` 色提示（不阻断保存）；
2. **路由模拟器**（页面顶部 `MetricStrip` 下方新区块）：
   - 输入框 + 「模拟路由」按钮 → 调 `route-dry-run`；
   - 结果卡：目标 Agent（身份色头像）+ 置信度进度条（`--brand-500`，
     < 阈值段显示 `--warning`）+ reason 折叠文本；
   - `mode=orchestrate` 时额外列出 Plan 预览（dry-run 同时返回 Plan）；
   - 空态/加载/错误态复用 antd `Empty/Skeleton/Alert`，玻璃底。

#### 9.4.5 Observability 变更

- **Overview**：新增「路由分布」卡——横向条形图（纯 CSS div 实现，
  与现有图表风格一致），条色 = 各 Agent 身份色，右侧标注次数与平均置信度；
- **Traces**：编排 trace 树按 `parent_ids` 缩进渲染（既有数据结构支持）；
  Worker span 行尾追加 `task_id` 前 8 位等宽徽标（`--gray-500` 底白字）。

### 9.5 交互流程（状态机）

```
用户发送消息
  │ SSE: router_decision
  ├─ direct ──► TaskChip 显示路由 ──► 既有 RunProcessPanel 流程（不变）
  ├─ orchestrate
  │    SSE: plan ──► OrchestrationPanel 渲染（Worker 卡骨架 → 填充）
  │    SSE: worker_started/finished ──► 卡片状态流转 + 阶段条推进
  │    SSE: synthesis ──► 阶段条到「综合」──► 既有流式文本渲染 final_answer
  └─ needs_clarification
       SSE: clarification ──► ClarificationCard 插入 ──► 用户点选/输入
       ──► 作为新消息重新走路由（面板中上一轮决策保留为历史）
```

规则：
- 编排事件到达前面板先渲染骨架（阶段条 + N 个灰卡，N 未知时渲染 3 个）；
- `worker_finished` 的 `result_preview` 点击展开完整预览（仅预览，
  完整结果以 final_answer 为准）；
- 用户在编排进行中发送新消息 → 前端禁止（输入框禁用，与现有 run 互斥逻辑一致）；
- 停止按钮中断 SSE → 面板冻结当前状态 + 「已停止」灰条（复用 `canceled` 文案）。

### 9.6 动效规格

| 元素 | 动效 | 参数 |
|------|------|------|
| 状态点（running） | 脉冲缩放 + 光晕 | `1.6s infinite`，`--ease-out`，光晕 `0 0 8px` 同色 65% 透明（对齐 `.session-active-dot`） |
| Worker 卡进入 | fade + 上移 6px | `--dur-fast` 160ms |
| 阶段条推进 | 轨道填充宽度过渡 | `--dur-base` 240ms |
| 面板展开/折叠（reason、result_preview） | 高度过渡 | `--dur-base` 240ms |
| ClarificationCard 插入 | fade + 上移 | `--dur-fast` |

- 全部动效仅用 transform/opacity（不触发重排）；
- `prefers-reduced-motion: reduce` 时关闭脉冲与位移动画，仅保留颜色过渡；
- 不做画布、不做粒子、不做装饰性循环动画（对齐 AGENT_HUB_SPEC 决策 14）。

### 9.7 响应式与可访问性

- **1198px**（基准）：OrchestrationPanel 单列内嵌消息流，Worker 网格
  auto-fill；
- **1024px**：Worker 网格降为 2 列；路由模拟器结果卡移至输入框下方
  （对齐 design-qa.md 的 1024 断点行为）；
- 触达目标 ≥ 40px（快捷选项按钮高 40px）；
- 状态不只用颜色表达：状态点 + 文案 + 图标三重编码（色弱可用）；
- `OrchestrationPanel` 根元素 `role="status"` + `aria-live="polite"`，
  Worker 状态变化对读屏软件可感知；
- 折叠详情用原生 `button + aria-expanded`，不用 div 点击。

---

## 10. 测试与验收标准

### 10.1 后端

| 编号 | 场景 | 预期 |
|------|------|------|
| T1 | 单 Agent 能力问题（"帮我查知识库里的报销制度"） | direct 路由到对应 Agent，真实执行并返回；trace 含 RouterDecision |
| T2 | 复合请求（"分析这份销售数据并生成报告"） | orchestrate → Plan ≤5 任务 → 并行执行 → 聚合回答覆盖两个子目标 |
| T3 | 模糊请求（"帮我处理一下"） | needs_clarification=true，SSE 收到 clarification 事件 |
| T4 | Router 输出非法 JSON | 重试 1 次后回退 main，trace 有警告 |
| T5 | Worker 返回 CANNOT_HANDLE | 任务 failed（agent_declined），不重试，聚合层降级 |
| T6 | 2/5 Worker 失败 | final_answer 基于成功分支 + 显式标注缺失；`agent_tasks` 状态正确 |
| T7 | 全部 Worker 失败 | 返回错误说明，HTTP 200 + SSE 错误帧（不 5xx） |
| T8 | Agent 连续失败 3 次 | 熔断生效，后续路由候选剔除，冷却后恢复 |
| T9 | `MULTI_AGENT_ENABLED=false` | 行为与现状一致（回归：现有全部测试通过） |
| T10 | 并发 5 Worker + 配额闸门 | `MAX_PARALLEL_WORKERS` 生效，无未捕获异常 |
| T11 | 进程重启 | Checkpointer 恢复；running 态 `agent_tasks` 清扫为 failed |

### 10.2 前端

| 编号 | 场景 | 预期 |
|------|------|------|
| F1 | 视觉走查 | OrchestrationPanel/ClarificationCard 与玻璃体系一致：`--bg-card` 表面、`--border-soft` 描边、14~26px 圆角；对照 design-qa.md 基准无风格漂移 |
| F2 | 编排全流程 | 阶段条 5 段依次点亮；Worker 卡状态流转正确；final_answer 流式渲染 |
| F3 | 部分失败 | degraded 横幅出现，文案含失败数；成功卡仍可展开预览 |
| F4 | 澄清交互 | 快捷选项点击即发送；卡片变已选态；「其他」聚焦输入框 |
| F5 | 未知事件兼容 | 注入未定义的 `orchestration.*` 事件，UI 不报错不渲染 |
| F6 | SSE 断线 | 面板冻结 + 「连接中断」灰条；重连后不重复渲染 |
| F7 | 响应式 | 1198/1024 两档无横向溢出；1024 下 Worker 网格 2 列 |
| F8 | 可访问性 | 状态三重编码；`aria-live` 生效；键盘可操作折叠区 |
| F9 | 动效降级 | `prefers-reduced-motion` 下无脉冲/位移 |
| F10 | Chrome 控制台 | 全流程无 error（对齐 design-qa.md 验收口径） |

### 10.3 性能预算

- Router 首事件延迟：≤ 2.5s（P90，小模型 + temperature=0）；
- OrchestrationPanel 渲染：事件到达到 DOM 更新 ≤ 100ms；
- 面板内存：单次编排 ≤ 50 个 Worker 状态对象（Plan 上限 5，天然满足）。

---

## 11. 迁移与兼容

1. **数据库**：`agent_tasks` 为纯新增表，Alembic 迁移无破坏性变更；
2. **API**：`/api/chat` 请求/响应结构不变，仅 SSE 新增事件类型；
   旧前端忽略未知事件（F5 保证）；
3. **AgentHub**：`scope` 字段已存在，本次仅开放编辑；存量 Agent 默认
   `user`，不受影响；
4. **回退**：`MULTI_AGENT_ENABLED=false` 时前端不渲染编排组件
   （SSE 无编排事件，组件天然不挂载），无需前端开关；
5. **清理**：`supervisor.py` 的 `demo_subagent` 硬编码路由与固定文案
   `_sub_node` 在 M2a 合入时删除，`agents/registry.py` 的内存注册中心
   保留但改为由 AgentHub 数据驱动填充。

---

## 12. 开放问题

1. **Router 模型选型**：`SUPERVISOR_MODEL` 默认跟随全局还是强制小模型？
   建议 M2a 上线后按路由延迟与准确率数据决定；
2. **Plan 确认 HITL**：分解结果回显确认会增加一轮交互，M2b 先做成
   `PLAN_CONFIRM_ENABLED=false` 的预留开关；
3. **Worker 身份色板扩展**：单租户 Agent > 6 个时色板循环复用，
   是否需要按 Agent 自定义色（AgentHub 加可选 `color` 字段）——M2c 评估；
4. **路由模拟器是否开放给普通用户**：当前定为管理员-only，
   若用于 Agent 调试可放宽（配合租户配额）。

---

## 13. RabbitMQ 的边界与衔接

> 结论先行：**编排关键路径不引入 RabbitMQ；旁路任务全部复用既有 MQ 管道**。
> 项目已有成熟 MQ 基础设施（`app/mq/` + `app/workers/`），本节明确
> 哪些环节与它衔接、哪些明确不用、未来何时扩展。

### 13.1 既有 MQ 基础设施盘点（本规格直接复用，不重复建设）

| 设施 | 现状 | 编排侧衔接点 |
|------|------|-------------|
| obs 管道 | `obs.exchange`(topic) → `obs.spans.q` / `obs.audit.q` → `obs_worker` 落库 | 编排 span、TaskCard 审计事件 |
| session 管道 | `session.exchange`(direct) → `session.postprocess.q` → `session_worker` 记忆提炼 | 编排结束后的记忆提炼（自动衔接，零开发） |
| 可靠性设施 | quorum 队列、publisher confirm、手动 ack + prefetch、幂等去重（`MQProcessedMessage`）、分级重试（TTL 5s/30s/120s）+ DLX | 直接继承 |
| trace 透传 | `MessageEnvelope.trace` 跨进程传播 trace_id/tenant_id | Worker/审计 span 与编排 trace 关联 |
| 发布降级 | `publish_nowait` + fallback：MQ 不可用时进程内执行 | 所有编排旁路发布的兜底模式 |
| 开关 | `MQ_ENABLED`（默认 false）+ `MQ_OBS_ENABLED` / `MQ_SESSION_ENABLED` 分管道开关 | 新增消息类型纳入 `MQ_OBS_ENABLED` 管控 |

### 13.2 为什么编排关键路径不走 MQ（决策记录）

Router → Planner → Worker → Synthesizer 是**同步交互链路**，用户正在
等待流式回答：

1. **延迟**：SSE 首 token 延迟预算 ≤ 2.5s（§10.3）；MQ 引入序列化、
   排队、投递延迟，且每跳任务都要过队列，链路越长放大越明显；
2. **流式回传困难**：Worker 若在独立消费进程执行，LLM token 流需要
   额外的回传通道（再建一条 MQ/Redis pub/sub 回 API 进程再转 SSE），
   复杂度翻倍且引入新的故障点；
3. **双状态源**：LangGraph checkpoint 在 Postgres，任务执行位置在 MQ，
   恢复/重放时两套状态需人工对齐，违背 §4.4「单一事实来源」的防线设计；
4. **规模不匹配**：`MAX_PARALLEL_WORKERS=5` 的并发规模，进程内
   asyncio（Send API）完全够用；MQ 的价值在跨进程/跨机分发，当前无此需求；
5. **业界对照**：LangGraph/CrewAI/AutoGen 的编排执行均为进程内图执行；
   消息队列在这些架构中的角色同样是旁路（观测、后处理、跨系统），
   与本项目现状一致。

### 13.3 编排与 MQ 的衔接点（按里程碑）

**M2a/M2b 自动衔接（零开发）**：
- 编排 span 落库：`tracing.py` 既有 `obs.span` 发布，编排节点 span 自动进入管道；
- 记忆提炼：编排会话结束后 `router_chat.py` 既有 `session.postprocess`
  发布，`session_worker` 正常消费——编排不改变该链路任何代码。

**M2b 新增（低成本）**：
- **TaskCard 审计事件**：TaskCard 状态终态（done/failed）时发布
  `obs.audit` 消息（payload 含 task_id/target_agent/status/duration_ms），
  `agent_tasks` 落库保持同步直写（事实来源），审计流走旁路；
  复用 `publish_nowait` + fallback 模式；
- **编排进度事件广播预留**：`orchestration.*` SSE 事件当前由图执行进程
  直推。接口设计上把「事件出口」抽象为单点（`graph_runtime.py` 的
  SSE 转换层），M2c 多实例部署时在该单点切换为「发布到 MQ 广播 +
  各实例 SSE 网关订阅」，前端协议（§4）不变。

**M2c+ 演进触发条件（满足任一再评估，当前不做）**：

| 触发信号 | MQ 方案 |
|---------|---------|
| Worker 任务变重（分钟级：长文档分析、批量工具调用） | 新增 `orch.worker.q`，TaskCard 序列化投递，独立 worker 进程池消费；代价是失去进程内流式回传，需配合任务级轮询/回调 |
| API 多实例水平扩展 | `orchestration.*` 事件经 MQ 广播（§13.3 预留点生效） |
| 跨系统 A2A 委派 | MQ 作为跨进程传输层之一（与 HTTP 并列），TaskCard 即消息体 |
| 突发流量削峰 | 编排入口排队 + `MQ_PREFETCH` 控制消费速率 |

### 13.4 明确不做的事

1. 不把 Router/Planner/Synthesizer 的 LLM 调用改为 MQ 任务——它们是
   编排决策链，必须与图状态同进程；
2. 不用 MQ 替代 Worker 的进程内重试——交互式场景重试需要秒级反馈，
   MQ 分级重试（5s/30s/120s）适合后台任务，不适合用户在等的 Worker；
3. 不为编排新建 exchange/队列（M2b 范围内）——只复用 `obs.exchange`
   的既有 routing key，拓扑零变更。
