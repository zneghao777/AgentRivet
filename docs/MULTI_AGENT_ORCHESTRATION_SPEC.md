# 多 Agent 协同编排 · 技术方案

> 版本：v1.0.0（草案）
> 日期：2026-08-30
> 状态：待评审
> 里程碑代号：MULTI-AGENT-ORCH
> 关联文档：[AGENT_HUB_SPEC.md](./AGENT_HUB_SPEC.md)、[OBSERVABILITY_SPEC.md](./OBSERVABILITY_SPEC.md)、[MCP_SKILL_ECOSYSTEM_SPEC.md](./MCP_SKILL_ECOSYSTEM_SPEC.md)

本文回答多 Agent 协作的五个核心问题：**Agent 间如何通信、Supervisor 意图识别如何做、
并行结果如何聚合、交接准确性如何保障、责任如何划分**，并结合当前项目进度给出
分阶段落地方案。

---

## 0. 决策摘要

1. ★ **编排框架沿用 LangGraph，不自研**。主链路已是 LangGraph StateGraph
   （`harness/graph_builder.py`），Supervisor 骨架已存在（`harness/supervisor.py`），
   业内主流模式均可在此之上实现。
2. ★ **通信采用「共享状态（黑板）+ 结构化任务卡」混合模式**。同进程编排内
   通过 LangGraph 共享 State 传递数据；跨 Agent 委派一律使用结构化 `TaskCard`
   （Pydantic 模型），禁止自由文本交接。
3. ★ **意图识别采用「LLM 结构化输出路由」**，路由依据是 AgentHub 中每个 Agent
   的 `name + description + capability 摘要`（Agent Card 思想），输出带置信度
   与兜底策略（低置信 → 澄清反问或回退主 Agent）。
4. ★ **并行执行用 LangGraph Send API（Map-Reduce）**，聚合分两级：
   机械聚合（reducer 合并结构化结果）+ LLM 综合（Synthesizer 节点）。
5. ★ **交接准确性靠四道防线**：结构化契约（schema 校验）→ 上下文打包
   （任务卡携带目标/约束/产物引用）→ 执行后校验（Verifier/Critic）→
   失败重试与降级。
6. ★ **责任划分复用 AgentHub 既有模型**：`scope=user/internal` 区分面向用户
   与仅供编排的 Agent；工具级绑定限定能力边界；每个 Agent 单一职责。
7. 高可用：超时 → 重试 → 降级 → 熔断 → Checkpoint 恢复 → HITL 审批，
   全部映射到既有机制并补齐缺口。
8. 落地分三个里程碑：**M2a 单选路由 → M2b 分解 + 并行聚合 → M2c 层级编排 +
   A2A 预留**，与 AGENT_HUB_SPEC 的 M2 对齐。

---

## 1. 现状盘点：项目对多 Agent 的支撑程度

### 1.1 已具备的基础（可直接复用）

| 能力 | 位置 | 状态 |
|------|------|------|
| LangGraph 主图（ReAct 循环） | `harness/graph_builder.py:57-143` | ✅ 生产可用 |
| Supervisor 图骨架 + 条件路由边 | `harness/supervisor.py:22-72` | ⚠️ 骨架，路由硬编码 |
| `SupervisorState(route, agent_outputs)` | `harness/supervisor.py:75-79` | ⚠️ 字段已声明，从未读写 |
| Agent 注册中心 `AgentEntry` | `agents/registry.py` | ⚠️ 内存态，仅 demo |
| AgentHub（定义/能力绑定/resolver） | `agents/resolver.py` + `storage/models.py` | ✅ 已交付 |
| `scope=user/internal` 字段（编排预留） | AGENT_HUB_SPEC 决策 4 | ✅ 已交付 |
| 请求级 `ToolContext`（不进 checkpoint） | `harness/context.py:19-41` | ✅ 生产可用 |
| 全链路 tracing（run_id/parent_ids） | `harness/graph_runtime.py:190-230` | ✅ 生产可用 |
| 会话↔thread_id + 消息级 agent_id 审计 | `harness/session_manager.py` | ✅ 生产可用 |
| Postgres Checkpointer（断点恢复） | `main.py:125` | ✅ 生产可用 |
| 超时 `AGENT_TIMEOUT_SECONDS=120` | `config.py:58` | ✅ 生产可用 |
| HITL 预留 `GRAPH_INTERRUPT_TOOLS` | `config.py:59` | ⚠️ 默认关闭 |

### 1.2 明确缺失的部分（本方案要补齐）

1. **意图识别**：`supervisor.py:37-43` 路由硬编码（有 `demo_subagent` 就路由过去），
   无 LLM 参与、无置信度。
2. **子 Agent 真实执行**：`supervisor.py:98-102` 的 `_sub_node` 返回固定文案，
   不调用 LLM、没有自己的工具循环。
3. **任务分解**：无 Planner，复杂请求不会被拆成子任务。
4. **并行执行**：图结构是串行单路由，无 fan-out/fan-in。
5. **结果聚合**：`agent_outputs` 无人写入，无 Synthesizer。
6. **交接契约**：Agent 间无结构化输入输出约定。
7. **重试/降级**：只有超时，无失败重试与路由降级。

### 1.3 关键架构事实（决定方案形态）

- **「一个图 + 请求级人格切换」而非「多图实例」**：编译后的图是全局单例
  （`app_runtime.py:23-28`），AgentHub 的 Agent 是数据库行，每次请求由 resolver
  解析成能力集注入。因此子 Agent 编排走 **Agent-as-Node** 路线（每个子 Agent
  是图上一个节点，共享同一 State），不为每个 Agent 编译独立图。
- **`ToolContext` 不进 checkpoint**：跨 Agent 的持久信息必须走 State，
  请求级敏感信息（api_key 等）继续留在 ContextVar。
- **observability 已按 run_id/parent_ids 建模**：天然支持多 Agent 树状链路，
  编排层只需正确传递 parent_ids。

---

## 2. 业内方案调研

### 2.1 主流框架编排模式对比

| 框架 | 编排模式 | 通信方式 | 对本项目的启示 |
|------|---------|---------|--------------|
| **LangGraph** | Supervisor / Network / Hierarchical（图状态机） | 共享 State（黑板）+ Command | ✅ 直接沿用，官方 Supervisor 模式与现有骨架同构 |
| **AutoGen** | GroupChat 自由对话 / 顺序流水线 | 消息总线 | 消息总线可用「消息追加到共享 State」模拟，不必引入 |
| **CrewAI** | 角色流水线（role/goal/backstory） | 任务链顺序传递 | role/goal 思想吸收进 AgentHub 的 description 规范 |
| **OpenAI Swarm** | Handoffs（agent 即工具，控制权移交） | handoff 函数 + 上下文变量 | handoff 思想用于会话级 Agent 切换（已由 AgentHub 实现） |
| **Anthropic orchestrator-workers** | 总控分解 → 并行工人 → 综合 | 任务描述 + 产物回传 | ✅ M2b 的 Map-Reduce 蓝本 |
| **A2A 协议（Google）** | 跨系统 Agent 互操作 | Agent Card + Task 生命周期 + Artifact | M2c 预留：AgentHub 定义可导出为 Agent Card |

### 2.2 业内对五个核心问题的共识

**① Agent 间通信**：三种流派——共享状态/黑板（LangGraph）、消息传递（AutoGen）、
Agent-as-Tool（Swarm/Anthropic）。共识：**同进程优先共享状态 + 结构化消息；
跨进程/跨系统才需要 A2A 协议**。纯自由文本对话式协作在生产中被普遍认为不可控。

**② 意图识别**：三代演进——静态规则 → 小模型分类 → **LLM 结构化输出路由**
（当前主流）。关键点：路由依据是 Agent 能力声明而非硬编码；structured output
强制输出 `{target_agent, confidence, reason}`；必须有低置信兜底。

**③ 并行与聚合**：LangGraph **Send API** 动态 fan-out，结果经 **Annotated
reducer** 合并；聚合两级：机械聚合（确定性代码）+ LLM 综合；失败分支不阻塞整体。

**④ 交接准确性**：四道防线成为事实标准——结构化契约（schema 校验）、
上下文打包（任务卡：目标/约束/产物引用，A2A Task/Artifact 即此思想）、
执行后校验（Critic）、全链路可追溯。

**⑤ 责任划分**：能力声明驱动（capability-based）、单一职责、权限最小化
（工具级绑定）、职责边界写入提示词。

---

## 3. 总体架构设计

```
用户消息 ──► START
              │
              ▼
        ┌──────────┐   LLM 结构化输出路由（依据 AgentHub 卡片）
        │ 1.Router │──► RouterDecision{mode, target, confidence}
        └────┬─────┘
             ▼
        ┌──────────┐   orchestrate 时分解为 TaskCard[]
        │ 2.Planner│──► execution: parallel | sequential
        └────┬─────┘
             │ Send API 动态 fan-out
        ┌────┼────┐
        ▼    ▼    ▼
   [worker][worker][worker]   并行子 Agent（Agent-as-Node）
        │    │    │
        └────┼────┘  reducer 合并 agent_outputs
             ▼
        ┌──────────┐   校验/Critic/重试
        │ 3.Verifier│
        └────┬─────┘
             ▼
        ┌───────────┐  LLM 综合最终答案（冲突显式标注）
        │4.Synthesizer│
        └────┬──────┘
             ▼
            END ──► SSE 流式输出（复用 graph_runtime）
```

**核心设计原则**：
1. **Agent-as-Node**：子 Agent 是 Supervisor 图上的节点，共享 `SupervisorState`，
   复用 `agent_node` 的 LLM 调用与 resolver 能力注入。
2. **决策与执行分离**：Router/Planner 只做决策产出 TaskCard；Worker 只执行；
   Verifier 只校验；Synthesizer 只综合。
3. **一切交接走 TaskCard**：结构化、可校验、可追溯。
4. **渐进增强**：`MULTI_AGENT_ENABLED=false` 时行为与现在完全一致
   （`supervisor.py:31-33` 回退逻辑保留）。

---

## 4. 五大核心问题方案

### 4.1 Agent 之间如何通信

**结论：共享状态（黑板）为主 + 结构化 TaskCard 为协议，Agent-as-Tool 为补充。**

#### 4.1.1 扩展 SupervisorState

```python
# harness/state.py 新增
class TaskCard(BaseModel):
    """Agent 间交接的唯一协议。"""
    task_id: str                        # uuid，全链路追踪
    parent_task_id: str | None          # 支持层级分解
    target_agent: str                   # 目标 Agent 名（AgentHub name）
    goal: str                           # 子任务目标（一句话）
    context_brief: str                  # 必要上下文摘要（不是全量历史！）
    constraints: list[str] = []         # 约束（格式/字数/禁止事项）
    expected_output_schema: str | None  # 期望输出描述
    artifacts: list[dict] = []          # 产物引用（KB 文档 id、文件路径）
    status: Literal["pending", "running", "done", "failed"] = "pending"
    result: str | None = None
    error: str | None = None


class SupervisorState(HarnessState):
    route: str = "main"
    plan: list[TaskCard] = []                                  # Planner 产出
    agent_outputs: Annotated[list[dict], operator.add] = []    # 并行结果合并
    router_decision: dict = {}                                 # 路由决策（含置信度）
```

要点：
- `agent_outputs` 用 `operator.add` reducer，并行分支各自 append 自动合并——
  这正是 `supervisor.py:79` 已预留但未实现的字段。
- **通信传「任务卡 + 上下文摘要」，不传全量消息历史**。这是控制 token 成本
  与防止上下文污染的关键（Anthropic 与 LangGraph 官方均建议如此）。

#### 4.1.2 三种通信路径按场景选用

| 场景 | 机制 | 说明 |
|------|------|------|
| Supervisor → Worker 委派 | TaskCard 写入 State，Worker 节点读取 | 主路径 |
| Worker → Supervisor 回报 | `agent_outputs.append({task_id, result, status})` | reducer 自动合并 |
| 主 Agent 运行中临时求助 | **Agent-as-Tool**：子 Agent 包装成 `delegate_to_{name}` 工具 | 复用 bind_tools，主 Agent 自主决定何时调用 |
| 会话级切换 Agent | AgentHub 既有会话绑定（handoff 思想） | 已实现，不变 |

Agent-as-Tool 的价值：简单请求（如「查下库存再总结」）不需要 Planner 分解，
主 Agent 通过工具调用直接委派，延迟最低。两种模式并存，Router 决定走哪条。

#### 4.1.3 跨进程通信（M2c 预留）

需要跨服务/跨框架协作时引入 **A2A 协议**：AgentHub 的 Agent 定义可导出为
A2A **Agent Card**；TaskCard 与 A2A 的 Task/Message/Artifact 模型天然对齐，
序列化即可互通。本期不做，但 TaskCard 字段设计已预留兼容空间。

### 4.2 总控 Agent 的意图识别怎么做

**结论：LLM 结构化输出路由，以 AgentHub 能力声明为路由依据，带置信度与兜底。**

#### 4.2.1 Router 节点设计

```python
# harness/router.py（新增）
class RouterDecision(BaseModel):
    """路由决策：结构化输出，禁止自由文本。"""
    mode: Literal["direct", "orchestrate"]   # 单任务直派 / 需要编排
    target_agent: str                        # direct 时的目标 Agent
    confidence: float                        # 0~1
    reason: str                              # 路由理由（写入 trace 供审计）
    needs_clarification: bool = False        # 置信度不足时反问用户


ROUTER_SYSTEM_PROMPT = """你是任务路由器。根据用户消息与候选 Agent 列表，
决定由哪个 Agent 处理，或是否需要多 Agent 编排。

# 候选 Agent（来自 AgentHub，name + description + 能力摘要）
{agent_cards}

# 规则
1. 只能路由到列出的 Agent；没有合适的 → target_agent="main"。
2. 任务需要多个 Agent 的能力或可并行分解 → mode="orchestrate"。
3. confidence < {CONFIDENCE_THRESHOLD} → needs_clarification=true，
   生成澄清问题，禁止猜测路由。
4. reason 必须引用 Agent 的具体能力描述，不得空泛。
"""
```

#### 4.2.2 关键机制

1. **Agent Card 动态生成**：从 AgentHub 读取可路由的 Agent（`user` + `internal`），
   拼装 `name + description + 绑定的 Skill/MCP 能力摘要` 注入 router prompt。
   **新建 Agent 无需改代码即可被路由**——这是与硬编码路由的本质区别。
2. **两级过滤**：先按租户/启用状态过滤候选集（SQL 层），再交给 LLM 选择。
   候选 > 15 个时先用嵌入检索粗筛 top-K，避免路由退化。
3. **置信度兜底**：
   - `>= 0.8`：直接路由；
   - `0.5 ~ 0.8`：路由但记录 reason 供事后审计与阈值调优；
   - `< 0.5`：`needs_clarification=true`，生成澄清问题返回用户（复用现有 SSE）。
4. **路由可观测**：RouterDecision 全量写入 trace（复用 `graph_runtime.py`
   的 span 机制），支持统计各 Agent 的路由命中率与误路由率。

#### 4.2.3 Planner（编排模式下的任务分解）

Router 判定 `orchestrate` 后，Planner 节点做分解：

```python
class Plan(BaseModel):
    tasks: list[TaskCard]
    execution: Literal["parallel", "sequential", "dag"]
```

- 分解依据同样是 Agent Card（每个子任务必须指派给存在的 Agent）；
- `parallel`：无依赖，Send API fan-out；`sequential`：链式执行；
  `dag`：M2c 再支持；
- **分解结果可回显给用户确认**（HITL：在 Planner 后加 interrupt 点，
  复用 `GRAPH_INTERRUPT_TOOLS` 机制，M2b 可选开启）。

### 4.3 并行执行后结果怎么聚合

**结论：Send API 动态 fan-out + reducer 机械聚合 + Synthesizer LLM 综合。**

#### 4.3.1 并行执行（Map）

```python
# harness/orchestrator.py（新增）
def fan_out(state: SupervisorState):
    """Planner 后的动态 fan-out：每个 TaskCard 一个 Send。"""
    return [Send(f"sub_{t.target_agent}", {"task": t, **state})
            for t in state["plan"]]

g.add_conditional_edges("planner", fan_out, sub_node_names)
```

- 每个 Worker 收到独立 State 副本（含自己的 TaskCard），执行逻辑复用
  `agent_node`（`base_agent.py:101-128`），system prompt 额外注入
  TaskCard 的 goal/constraints；
- Worker 完成后 `agent_outputs.append(...)`，reducer 自动合并；
- **并发上限**：`asyncio.Semaphore(settings.MAX_PARALLEL_WORKERS)`（默认 5），
  防止 LLM 并发打满配额。

#### 4.3.2 两级聚合（Reduce）

**第一级：机械聚合（确定性，代码完成）**——按 task_id 稳定排序合并成功结果、
收集失败分支、计算 `success_rate`。

**第二级：LLM 综合（Synthesizer 节点）**，prompt 核心要求：

```
1. 忠实于各分支结果，不得编造未提供的信息；
2. 冲突信息显式指出（"A Agent 认为…，B Agent 认为…"），不得擅自裁决；
3. 有失败分支时：说明哪些部分缺失及原因，基于成功部分给出可用回答；
4. 标注信息来源（哪个 Agent 产出）。
```

- **失败不阻塞**：全部失败 → 返回错误说明 + 建议重试；部分失败 →
  降级回答 + 标注缺失；
- **冲突检测**：强制显式标注冲突，不擅自合并矛盾结论。

#### 4.3.3 流式体验

并行执行期间通过现有 SSE 通道推送每个 Worker 的进度事件
（`worker_started / worker_finished`），前端可展示「任务面板」式并行进度——
observability 的 run_id/parent_ids 树状结构天然支持此展示。

### 4.4 哪些机制保证交接的准确性

**结论：四道防线，全部落在 TaskCard 生命周期上。**

```
防线1 结构化契约 → 防线2 上下文打包 → 防线3 执行后校验 → 防线4 可追溯+重试
  (schema 校验)     (任务卡完整)       (Verifier)        (trace+降级)
```

**防线 1：结构化契约（入口校验）**
- Worker 收到任务先校验 TaskCard：`target_agent` 存在、`goal` 非空、
  `expected_output_schema` 可解析；
- 校验失败 → 直接标记 `failed`（error="invalid_task_card"），不进 LLM 调用
  （省 token 且快速失败）。

**防线 2：上下文打包（防信息丢失）**
- 交接三要素缺一不可：**目标**（goal）、**约束**（constraints）、
  **上下文摘要**（context_brief）；
- Planner 生成 TaskCard 时硬性要求：「假设 Worker 看不到对话历史」，
  context_brief 必须包含完成子任务所需的关键信息（数字、日期、文件名等）；
- 产物引用（artifacts）传引用不传内容（如 KB 文档 id），Worker 通过工具
  自行拉取——避免大段内容在 State 中反复复制。

**防线 3：执行后校验（Verifier）**
- 轻量校验（代码级）：输出非空、符合 expected_output_schema 描述、
  未包含明显拒答模板；
- 关键任务（按 Agent 配置）追加 LLM Critic：判断结果是否回答了 goal；
- 校验失败 → 自动重试一次（携带失败原因让 Worker 修正），再失败 →
  标记 failed 交给聚合层降级处理。

**防线 4：可追溯 + 重试降级**
- `task_id / parent_task_id` 贯穿 trace（复用 run_id/parent_ids 机制），
  交接失败可定位到具体一跳；
- 重试策略：Worker 级 1 次自动重试 → 聚合级降级输出 → 用户可见的错误说明；
- 全链路审计：消息级 `agent_id`（session_manager 已有）+ TaskCard 落库
  （新增 `agent_tasks` 表），支持事后回放与归因。

### 4.5 Agent 之间的责任怎么划分

**结论：能力声明驱动 + 单一职责 + 权限最小化，全部复用 AgentHub 既有模型。**

#### 4.5.1 划分维度

| 维度 | 机制 | 项目落点 |
|------|------|---------|
| 能否被用户直接选 | `scope=user/internal` | AgentHub 已实现；`internal` 仅供编排调用 |
| 能做什么 | description + 绑定的 Skill/MCP | AgentHub 已实现；description 同时是路由依据 |
| 不能用什么 | 工具级绑定白名单 | `agent_mcp_bindings` 已实现；Worker 只拿 TaskCard 相关工具 |
| 对谁负责 | Supervisor 统一对外 | Worker 结果只回给聚合层，不直接面向用户输出 |
| 边界外怎么办 | 提示词显式声明 | system prompt 加「职责边界」段：超出范围返回 `cannot_handle` 而非硬做 |

#### 4.5.2 Agent 定义规范（写入 AgentHub 使用指南）

每个编排参与 Agent 的 description 必须包含四要素（CrewAI role/goal 思想的落地）：

```
name: data_analyst
description: 负责【数据查询与统计分析】类任务。能：调用 KB 检索与 SQL 工具，
产出结构化数据结论。不能：生成营销文案、执行写操作。输入：数据需求描述；
输出：结论 + 数据引用。
```

- **单一职责**：一个 Agent 一类任务。宁可拆 `data_analyst` + `report_writer`
  两个 Agent，不做「数据分析报告全包」Agent；
- **正交性检查**：AgentHub 保存时校验 description 相似度，过高的两个 Agent
  提示职责重叠（路由会不稳定）；
- **internal Agent 治理**：管理员创建维护，不开放普通用户；单租户建议 ≤ 10 个
  （路由候选过多会退化）。

#### 4.5.3 Supervisor 自身的责任

Supervisor **只做决策与调度，不做具体工作**：
- Router/Planner/Synthesizer 是 Supervisor 的三个内部节点，各自单一职责；
- Supervisor 不绑定业务工具（无 MCP 绑定），防止「总控亲自下场」导致
  职责混乱与权限放大；
- Supervisor 的 LLM 用独立小模型配置（`SUPERVISOR_MODEL`，可选）：
  路由/分解不需要最强模型，降本提速。

---

## 5. 高可用设计

| 故障场景 | 机制 | 项目落点 |
|---------|------|---------|
| Worker LLM 超时 | 节点级超时（继承 `AGENT_TIMEOUT_SECONDS`）+ 重试 1 次 | `graph_runtime.py:305` 已有超时，补重试 |
| Worker 持续失败 | 标记 failed，聚合层降级输出部分结果 | §4.3.2 |
| Router 输出不合法 | schema 校验失败 → 重试 → 仍失败回退主 Agent | §4.2 |
| 单 Agent 故障频发 | 熔断：连续 N 次失败后 10 分钟内路由跳过该 Agent | 新增 `agent_circuit_breaker`（内存态即可） |
| 进程崩溃 | Postgres Checkpointer 恢复最近 checkpoint，未完成任务重新派发 | 已有 |
| 高危操作 | HITL 审批：interrupt_before（工具/派发前） | `GRAPH_INTERRUPT_TOOLS` 已预留 |
| 编排整体异常 | `MULTI_AGENT_ENABLED=false` 一键回退单 Agent 主链路 | `supervisor.py:31-33` 已有 |
| LLM 配额打满 | `MAX_PARALLEL_WORKERS` 并发闸门 + 请求排队 | 新增配置 |

---

## 6. 落地路线图

### M2a：单选路由（1~1.5 周）—— 让路由真实可用

- [ ] 新增 `harness/router.py`：RouterDecision 模型 + Router 节点
      （LLM 结构化输出、Agent Card 注入、置信度兜底）
- [ ] 改造 `supervisor.py`：删除硬编码路由，接入 Router；保留回退逻辑
- [ ] 实现 `_sub_node` 真实执行：复用 `agent_node` + resolver 能力注入
- [ ] AgentHub 增加 `internal` scope 管理界面与路由候选过滤
- [ ] Router 决策写入 trace；新增路由命中率指标
- **验收**：消息按 AgentHub 描述正确路由并真实执行；低置信度触发澄清反问。

### M2b：任务分解 + 并行聚合（2~3 周）—— 编排核心

- [ ] 新增 `harness/planner.py`：Plan/TaskCard 模型 + Planner 节点
- [ ] 新增 `harness/orchestrator.py`：Send API fan-out + reducer 聚合
      + `MAX_PARALLEL_WORKERS` 闸门
- [ ] 新增 Synthesizer 节点（两级聚合）
- [ ] TaskCard 四道防线落地：入口校验、上下文打包检查、Verifier、重试降级
- [ ] 新增 `agent_tasks` 表（TaskCard 落库，支持回放）
- [ ] SSE 推送 worker 进度事件；前端任务面板（可选）
- [ ] Agent-as-Tool：`delegate_to_{name}` 动态工具注册
- **验收**：复杂请求分解为 ≤5 个并行子任务，聚合回答完整、冲突显式标注；
  部分失败时降级输出可用。

### M2c：层级编排 + 生态预留（按需）

- [ ] Hierarchical：Worker 结果作为子 Supervisor 输入（LangGraph 原生支持）
- [ ] DAG 执行策略（依赖声明）
- [ ] Agent Card 导出为 A2A 格式；跨进程 Agent 委派（A2A client/server）
- [ ] Planner 后 HITL 确认（interrupt 点）
- [ ] 路由决策缓存与嵌入粗筛

---

## 7. 风险与开放问题

1. **路由准确率依赖 description 质量**：需配套 AgentHub 的 description 撰写
   规范与示例（M2a 交付文档），否则「垃圾进垃圾出」。
2. **token 成本放大**：编排链路（Router + Planner + N 个 Worker + Synthesizer）
   单次请求 LLM 调用数倍增。缓解：Supervisor 用小模型、context_brief 控制在
   500 token 内、路由缓存。
3. **延迟**：并行 Worker 受最慢分支拖累。缓解：分支级超时独立设置、
   先到先聚合（可选，M2c）。
4. **开放问题——Worker 间能否直接对话**：本方案定为「不能」（星型拓扑，
   全部经 Supervisor 中转），理由：可控、可审计、避免对话发散。若未来出现
   强 P2P 协作需求（如辩论式任务），再评估 Network 模式。
5. **开放问题——TaskCard 落库粒度**：全量落库便于回放但存储增长快，
   建议 M2b 先落「计划 + 最终结果」，中间重试记录只进 trace。

---

## 附：与 AGENT_HUB_SPEC 的关系

- 本方案是 AGENT_HUB_SPEC 中「M2：Supervisor 真实路由」的实现规格；
- AgentHub 是唯一的 Agent 事实来源：路由候选、能力注入、权限边界全部消费
  resolver 输出，编排层不单独维护 Agent 元数据；
- `scope=internal` 从「预留」转为「启用」：M2a 起仅供编排调用的 Agent
  以 internal scope 注册。
