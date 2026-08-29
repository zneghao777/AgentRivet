# AgentHub（Agent 管理）· 技术规格书

> 版本：v1.0.0（草案）
> 日期：2026-08-28
> 状态：待评审
> 里程碑代号：AGENT-HUB
> 关联文档：
> [AGENT_HUB_RESEARCH.md](./AGENT_HUB_RESEARCH.md)、
> [MCP_SKILL_ECOSYSTEM_SPEC.md](./MCP_SKILL_ECOSYSTEM_SPEC.md)、
> [MEMORY_SPEC.md](./MEMORY_SPEC.md)、
> [OBSERVABILITY_SPEC.md](./OBSERVABILITY_SPEC.md)、
> [agent-loop-design-report.md](./agent-loop-design-report.md)

本文定义 AgentHub（Agent 定义、能力绑定、运行时解析与会话绑定）所需的产品模型、
数据模型、运行时架构、API、安全治理、可观测性、前端界面、迁移、测试和验收标准。
首个可交付版本聚焦**单 Agent 的定义与按 Agent 解析能力集**；Supervisor 真实路由
（多 Agent 自动编排）作为 M2 独立交付，不阻塞本规格。

> **注意：** 当前仓库的多 Agent 只有 `demo_subagent` 硬编码演示骨架
> （`MULTI_AGENT_ENABLED` 默认关闭），数据库没有 `agents` 表，系统提示词写死在
> `base_agent.py`。本文描述的是目标实现，不代表当前多 Agent 功能已经可用。

---

## 0. 决策摘要

本节固定实施期间的核心决策。实现不得在页面、API 和运行时分别创造不同的
Agent 语义。带 ★ 的决策已由产品确认。

1. ★ **会话可中途切换 Agent**。切换从下一条消息生效，历史消息保留；每条消息
   按当前 Agent 重新解析能力集。`messages` 表新增可空 `agent_id`，记录每条
   助手回复由哪个 Agent 产生，用于审计与观测。
2. ★ **系统提示词叠加注入，不覆盖**。Agent 自定义提示词作为「身份块」注入在
   默认模板之前；`SYSTEM_PROMPT_TEMPLATE` 的能力边界与已绑定 Skill 指令
   **始终保留**，防止 Agent 失去能力边界。
3. ★ **MCP 按工具级（capability 级）绑定**。`agent_mcp_bindings` 直接引用
   `mcp_capabilities.id`；前端按 Server 分组批量勾选。Server 停用或能力快照
   失效时，解析阶段静默跳过失效绑定，不报错、不阻断。
4. ★ **Agent 默认用户可选**。新增 `scope` 字段区分 `user`（用户可在会话中
   选择）与 `internal`（仅供 Supervisor 编排内部调用，不出现在选择器）。
   首期新建 Agent 一律默认 `user`；`internal` 为 M2 编排预留。
5. AgentHub 是 Skill/MCP 能力中心之上的**聚合与授权层**。不改造 Skill 与
   MCP 实体本身，只新增绑定表；能力数据仍以能力中心为唯一事实来源。
6. 能力解析必须有**统一入口** `agents/resolver.py`。路由层、图构建层、
   Supervisor 一律消费其输出，禁止各自拼接能力集。
7. Agent 定义与绑定解析结果只驻留请求级 `ToolContext`（ContextVar），
   **不进入 LangGraph checkpoint**（延续 `harness/context.py` 的既有约束）。
8. 兼容规则：请求与会话均无 `agent_id` 时，完全走现有行为（全部已启用 Skill
   + 请求级能力白名单）。任何旧客户端不得因本规格变更而破坏。
9. 优先级：请求体 `model` > Agent 默认模型 > 全局 `LLM_MODEL`；
   请求体 `agent_id` > 会话绑定 `agent_id` > 无 Agent（旧逻辑）。
10. Agent 级记忆开关 `memory_enabled=False` 时，该 Agent 不注入记忆上下文、
    不注册 `memory_search` 工具；全局 `MEMORY_ENABLED` 仍是熔断总开关。
11. Agent 按租户隔离；名称在租户内唯一；每租户 Agent 数量有上限。
12. 新建 Agent 默认 `active`；删除默认软删（`archived`），存在会话引用时
    禁止物理删除。
13. AgentHub 不依赖 `MULTI_AGENT_ENABLED`。单 Agent 模式下 AgentHub 完整可用；
    `MULTI_AGENT_ENABLED` 只控制 Supervisor 编排路径（M2）。
14. 前端沿用能力中心的明亮、数据优先工作台风格：紧凑卡片、表单式编辑器，
    不做画布、不做装饰性动效。

---

## 1. 背景与现状

详细现状盘点见 [AGENT_HUB_RESEARCH.md](./AGENT_HUB_RESEARCH.md) §2，此处只列
与本规格直接相关的约束：

- **无 Agent 实体**：`storage/models.py` 没有 `agents` 表；Agent 注册中心
  `agents/registry.py` 只有内存态 `AgentEntry(name, description, graph)`。
- **提示词写死**：`base_agent.build_system_prompt(skill_names, memory_context)`
  只拼默认模板与 Skill 内容，无 Agent 身份注入点。
- **能力全局化**：前端把全部已启用 Skill 无条件发送；MCP 工具白名单是请求级
  `capabilities`，均不与 Agent 绑定。
- **可复用的既有机制**：
  - Skill 脚本工具命名 `skill_{name}__{script}`（`skills/manager.py`），
    按前缀即可过滤；
  - `mcp_manager.resolve_tools(..., requested_capability_ids=...)` 已支持
    能力级白名单入参；
  - `ToolContext`（`harness/context.py`）已是请求级上下文载体；
  - `db.py` 采用 `create_all` + 幂等 DDL 补丁的迁移方式（无 Alembic）。

---

## 2. 目标与非目标

### 2.1 产品目标

用户必须能完成以下任务：

- 在「Agent 管理」页创建、编辑、启停、删除 Agent。
- 为 Agent 设置系统提示词（角色与任务边界）、默认模型、记忆开关。
- 为 Agent 勾选可用 Skill、内置工具、MCP 工具（按 Server 分组批量勾选）、
  知识库。
- 在对话中为会话选择 Agent，并可中途切换，切换后下一条消息立即生效。
- 查看 Agent 的绑定概览与最近使用情况。

### 2.2 工程目标

- 建立 `agents` 主表与四张绑定表，会话与消息记录 Agent 归属。
- 建立统一能力解析器，输出一次运行的完整能力集（提示词、工具、模型、记忆）。
- 对话链路按 Agent 解析并注入能力集，无 Agent 时保持旧行为。
- 审计、Trace、指标增加 Agent 维度。

### 2.3 非目标（本期不做）

- 可视化编排画布、Agent 间消息传递协议。
- Supervisor 真实路由（M2，复用 `scope=internal`）。
- Agent 版本管理、发布流水线、跨租户共享市场。
- Agent 级细粒度 RBAC（沿用租户隔离）。
- Agent 独立沙箱（Skill 脚本沙箱边界沿用现状）。

---

## 3. 产品模型

### 3.1 Agent 定义

一个 Agent 是租户内的具名实体：

```text
Agent
├── 身份：name / description / scope / status
├── 行为：system_prompt（可空）/ model（可空）/ memory_enabled
└── 能力：skills[] / builtin_tools[] / mcp_capabilities[] / knowledge_bases[]
```

- `description` 是一句话职责描述，同时作为 M2 Supervisor 路由依据与前端
  选择器副标题，必填。
- `system_prompt` 可空。为空时 Agent 等价于「默认助手 + 绑定能力」。
- `model` 可空。为空时回退全局默认模型。

### 3.2 生命周期

```text
创建(active) ──► 停用(disabled) ──► 启用(active)
   │                  │
   └──────────────────┴──► 归档(archived，软删) ──► 物理删除(仅无会话引用)
```

- `disabled` 的 Agent 不出现在会话选择器；已绑定它的会话继续可见历史，
  新消息按「无 Agent 旧逻辑」执行并在响应中提示 Agent 已停用。
- `archived` 的 Agent 从所有列表消失；`sessions.agent_id` 保留原值供审计。

### 3.3 会话绑定与切换语义（决策 ★1）

- 会话创建时可指定 `agent_id`；会话详情返回当前 Agent 摘要。
- 切换 = 更新 `sessions.agent_id`，**从下一条消息生效**，历史不重置。
- 每条消息进入时按「请求体 > 会话绑定」解析 Agent 并重新解析能力集，
  因此切换天然生效，无需清理 checkpoint。
- 助手回复落库时写入 `messages.agent_id`，前端可在消息级展示来源 Agent。
- 切换到已停用/已归档的 Agent：接口返回 409，前端提示并回退当前选择。

### 3.4 提示词叠加语义（决策 ★2）

`build_system_prompt` 最终结构：

```text
[Agent 身份块]                      ← agent.system_prompt，可空；非空时注入
[默认 SYSTEM_PROMPT_TEMPLATE]       ← 能力边界 + Skill descriptions/bodies，始终保留
[记忆块]                            ← memory_context，按 Agent 开关与全局开关
```

约束：

- 身份块注入前必须做长度截断（`AGENT_SYSTEM_PROMPT_MAX_CHARS`）与控制字符清理。
- 身份块与默认模板之间以明确分隔标记拼接，便于 Trace 与调试还原完整提示词。
- Skill 指令永远来自绑定 Skill 的实际内容，不允许在身份块中「声明」能力。

### 3.5 能力绑定语义（决策 ★3）

| 绑定 | 粒度 | 空绑定语义 |
|---|---|---|
| Skill | skill 级 | 空 = 该 Agent 不注入任何 Skill（提示词与脚本工具均无） |
| 内置工具 | 工具名级 | 空 = 不限制（全部内置工具可用），降低首期配置成本 |
| MCP | capability（工具）级 | 空 = 该 Agent 不解析任何 MCP 工具 |
| 知识库 | KB 级 | 空 = 跟随请求（维持现状：请求体 `knowledge_bases` 决定） |

- 内置工具空绑定不限制，是首期刻意取舍：避免老用户建一个 Agent 后所有
  内置工具消失。MCP 与 Skill 无此兼容包袱，空即禁用。
- 绑定引用的目标被删除/停用时，解析阶段跳过并在 Agent 详情中标记失效项。

---

## 4. 数据模型

全部新增表遵循现有约定：SQLAlchemy 2.0 `Mapped/mapped_column`、UUID 主键、
`uq_/ix_/ck_` 命名、租户字段 `String(64)`。

### 4.1 `agents` 主表

```text
agents
├── id             UUID PK
├── tenant_id      VARCHAR(64)  NOT NULL
├── name           VARCHAR(128) NOT NULL
├── description    TEXT         NOT NULL
├── system_prompt  TEXT         NULL
├── model          VARCHAR(128) NULL          # 校验同 MessageSendRequest.model
├── model_base_url VARCHAR(2048) NULL
├── memory_enabled BOOLEAN      NOT NULL DEFAULT TRUE
├── scope          VARCHAR(16)  NOT NULL DEFAULT 'user'   # user | internal
├── status         VARCHAR(16)  NOT NULL DEFAULT 'active' # active | disabled | archived
├── created_by     VARCHAR(64)  NULL
├── created_at     TIMESTAMP    NOT NULL
├── updated_at     TIMESTAMP    NOT NULL
├── uq_agent_tenant_name (tenant_id, name)
├── ix_agent_tenant_status (tenant_id, status)
├── ck_agent_scope  scope IN ('user', 'internal')
└── ck_agent_status status IN ('active', 'disabled', 'archived')
```

### 4.2 绑定表

```text
agent_skill_bindings   (agent_id FK→agents.id ON DELETE CASCADE,
                        skill_id  BIGINT FK→skills.id,
                        uq (agent_id, skill_id))

agent_tool_bindings    (agent_id FK→agents.id ON DELETE CASCADE,
                        tool_name VARCHAR(128),
                        uq (agent_id, tool_name))

agent_mcp_bindings     (agent_id       FK→agents.id ON DELETE CASCADE,
                        capability_id  FK→mcp_capabilities.id,
                        uq (agent_id, capability_id))

agent_kb_bindings      (agent_id FK→agents.id ON DELETE CASCADE,
                        kb_id    FK→knowledge_bases.id,
                        uq (agent_id, kb_id))
```

- 绑定表均带 `ix_agent_binding_agent (agent_id)` 索引。
- `skills` 表当前无租户字段（全局），Skill 绑定沿用其全局 id；MCP/KB 绑定
  所引用的实体本身已租户隔离，解析时必须再校验 `tenant_id` 匹配。

### 4.3 既有表扩展

```text
sessions  ADD COLUMN agent_id UUID NULL        -- 幂等 ALTER，不加 FK，建索引
messages  ADD COLUMN agent_id UUID NULL        -- 幂等 ALTER，不加 FK，建索引
```

- 两列通过 `db.py` 现有幂等 DDL 补丁机制追加
  （参照 `mq_message_id` 的 `ADD COLUMN IF NOT EXISTS` 模式），不引入 Alembic。
- 不加 FK 的原因：`ALTER` 补列路径无法保证对旧数据回填约束，且 Agent 归档后
  历史引用必须可读。

### 4.4 仓储层

`storage/repositories.py` 新增 `AgentRepository`：

- `create / get / get_by_name / list(query, status, scope, page) /
  update / archive / hard_delete / count_by_tenant`
- `replace_bindings(agent_id, kind, ids)`：四类绑定统一的全量替换语义，
  单事务内先删后插。
- `list_sessions_referencing(agent_id)`：物理删除前的引用检查。

---

## 5. 运行时架构

### 5.1 模块划分

```text
backend/app/agents/
├── base_agent.py     改：build_system_prompt 增加 agent_system_prompt 参数
├── registry.py       不变（LangGraph 图注册，与 AgentHub 定义层解耦）
├── manager.py        新：AgentManager（定义 CRUD、校验、缓存失效）
├── models.py         新：AgentDefinition / ResolvedAgentCapabilities DTO
└── resolver.py       新：统一能力解析器（唯一入口）
```

### 5.2 解析器契约

```python
@dataclass
class ResolvedAgentCapabilities:
    agent_id: uuid.UUID | None
    agent_name: str | None
    system_prompt_block: str | None   # 叠加用身份块；None = 无 Agent/未配置
    allowed_skill_names: list[str] | None  # None = 不限制（旧逻辑）
    allowed_tool_names: set[str] | None    # None = 内置工具不限制
    allowed_capability_ids: set[uuid.UUID] | None  # MCP 工具级白名单
    model: str | None                 # Agent 默认模型；None = 回退请求/全局
    memory_enabled: bool
```

```python
async def resolve_agent_capabilities(
    db: AsyncSession, *, tenant_id: str, agent_id: uuid.UUID | None,
) -> ResolvedAgentCapabilities
```

解析规则：

1. `agent_id` 为空 → 返回全 `None` 的旧逻辑结果，调用方走现状路径。
2. Agent 不存在 / `archived` → 视为无 Agent（记 warning 日志），不抛异常阻断对话。
3. Agent `disabled` → 同上，但路由层需在响应元数据中提示。
4. Skill：绑定 ∩ 已启用 Skill；脚本工具名由 `skill_{name}__` 前缀推导。
5. 内置工具：绑定集非空时输出为绑定集；空绑定输出 `None`（不限制）。
6. MCP：绑定 capability ids ∩ 该租户当前健康 Server 的能力快照；失效项跳过。
7. 模型：`agent.model`；`model_base_url` 仅在 Agent 显式配置时生效。

### 5.3 ToolContext 扩展

`harness/context.py` 的 `ToolContext` 新增字段（全部请求级，不进 checkpoint）：

```python
agent_id: uuid.UUID | None = None
agent_system_prompt: str | None = None
allowed_skill_names: list[str] | None = None
allowed_tool_names: set[str] | None = None
allowed_capability_ids: set[uuid.UUID] | None = None
```

### 5.4 对话链路接入

`router_chat.py` 在现有 `set_tool_context` 之前完成解析：

```text
agent_id = body.agent_id or session.agent_id or None
resolved = await resolve_agent_capabilities(db, tenant_id=..., agent_id=agent_id)
ToolContext(..., agent_id, agent_system_prompt, allowed_*, ...)
```

- `base_agent.agent_node`：
  - 构建 prompt 时传入 `agent_system_prompt`（叠加语义，§3.4）；
  - 绑定工具时对全局注册表按 `allowed_tool_names` 过滤，对
    `skill_{name}__*` 按 `allowed_skill_names` 过滤；
  - MCP 工具经 `mcp_manager.resolve_tools(requested_capability_ids=
    ctx.allowed_capability_ids)` 解析（既有参数，天然兼容）。
- 模型选择：`body.model or resolved.model or settings.LLM_MODEL`。
- 记忆：`resolved.memory_enabled is False` 时跳过记忆检索与
  `memory_search` 注册；全局 `MEMORY_ENABLED=False` 仍优先熔断。
- `messages` 落库时写入 `agent_id`。

### 5.5 会话接口

- `POST /sessions` 请求体新增可选 `agent_id`（校验：存在、active、scope=user）。
- 会话更新接口新增 `agent_id` 变更（切换语义 §3.3）；若现有路由无更新端点
  则新增 `PATCH /sessions/{session_id}`。
- 会话详情响应携带 `agent: {id, name, status} | null`。

---

## 6. API 设计

新路由 `backend/app/api/router_agents.py`，挂载 `prefix=API`（`/api/agents`）。
所有端点要求租户头（沿用现有机制）并写审计日志。

### 6.1 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/agents` | 分页列表；`query/status/scope/page/page_size` |
| POST | `/agents` | 创建；超上限返回 409 |
| GET | `/agents/{id}` | 详情，含四类绑定与失效项标记 |
| PATCH | `/agents/{id}` | 部分更新（含 scope/status/model 等） |
| DELETE | `/agents/{id}` | 软删（archived）；`?hard=true` 且无会话引用时物理删除，否则 409 |
| POST | `/agents/{id}/enable` | 启用（archived 不可直接启用，409） |
| POST | `/agents/{id}/disable` | 停用 |
| PUT | `/agents/{id}/bindings/skills` | `{skill_ids: int[]}` 全量替换 |
| PUT | `/agents/{id}/bindings/tools` | `{tool_names: string[]}` 全量替换 |
| PUT | `/agents/{id}/bindings/mcp` | `{capability_ids: uuid[]}` 全量替换 |
| PUT | `/agents/{id}/bindings/kbs` | `{kb_ids: uuid[]}` 全量替换 |
| GET | `/agents/{id}/usage` | H4：消息数/工具调用/Token 消耗（按天） |

### 6.2 Schema（`api/schemas.py` 新增）

```python
class AgentCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=2000)
    system_prompt: str | None = Field(default=None, max_length=20000)
    model: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9._:/-]+$", max_length=128
    )
    model_base_url: str | None = Field(default=None, max_length=2048)
    memory_enabled: bool = True
    scope: Literal["user", "internal"] = "user"

class AgentUpdateRequest(BaseModel):
    # 全字段可选（partial update）；status 仅允许 active/disabled，归档走 DELETE
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, min_length=1, max_length=2000)
    system_prompt: str | None = Field(default=None, max_length=20000)
    model: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9._:/-]+$", max_length=128
    )
    model_base_url: str | None = Field(default=None, max_length=2048)
    memory_enabled: bool | None = None
    scope: Literal["user", "internal"] | None = None
    status: Literal["active", "disabled"] | None = None

class AgentBindingResponse(BaseModel):
    skills: list[SkillRef]
    tools: list[str]
    mcp_capabilities: list[McpCapabilityRef]   # 含 server 信息与失效标记
    knowledge_bases: list[KbRef]

class AgentDetailResponse(BaseModel):
    id: str
    name: str
    description: str
    system_prompt: str | None
    model: str | None
    memory_enabled: bool
    scope: str
    status: str
    bindings: AgentBindingResponse
    session_count: int
    created_at: str
    updated_at: str
```

`MessageSendRequest` / `MessageStreamRequest` 新增：

```python
agent_id: str | None = None   # 显式覆盖会话绑定
```

### 6.3 错误约定

| 场景 | 状态码 |
|---|---|
| 名称重复 / 超租户上限 / 物理删除存在引用 / 切换到非 active Agent | 409 |
| Agent 或绑定目标不存在 | 404 |
| 字段校验失败（含 scope/status 非法值） | 422 |
| `AGENT_HUB_ENABLED=false` 时访问全部端点 | 404（功能关闭视为不存在） |

---

## 7. 安全与治理

- **租户隔离**：所有查询强制 `tenant_id` 过滤；跨租户 id 一律 404。
- **提示词治理**：`system_prompt` 上限 `AGENT_SYSTEM_PROMPT_MAX_CHARS=20000`；
  入库前清理控制字符；完整提示词仅管理员可见（沿用现有 Trace 权限）。
- **能力收敛**：Agent 绑定只能收窄能力，不能放大——MCP 实际解析集 =
  「Agent 绑定 ∩ 请求 `capabilities` ∩ 租户健康能力」；审批策略仍按能力中心
  既有规则执行，AgentHub 不绕过审批。
- **审计**：`audit_logs` 记录 `agent.created/updated/enabled/disabled/
  archived/deleted/binding.changed`，payload 含变更摘要（不含完整提示词）。
- **上限**：`AGENT_MAX_PER_TENANT=50`；单次运行工具总数仍受
  `CAPABILITY_MAX_TOOLS_PER_RUN` 约束。

### 配置项（`config.py` 新增）

```python
# --- AgentHub（AGENT_HUB_SPEC）---
AGENT_HUB_ENABLED: bool = True
AGENT_MAX_PER_TENANT: int = 50
AGENT_SYSTEM_PROMPT_MAX_CHARS: int = 20000
```

---

## 8. 可观测性

- **Trace**：`agent_traces` 与 span 属性增加 `agent_id` / `agent_name`；
  无 Agent 时为空，不改变现有语义。
- **指标**：`agent_metrics_daily/hourly` 增加 agent 维度标签（消息数、
  工具调用数、Token、错误率）；聚合查询按需下钻。
- **消息审计**：`messages.agent_id` 支持按 Agent 检索历史（管理端）。
- **失效绑定观测**：Agent 详情返回失效项；解析器跳过失效绑定时记
  warning 日志（含 agent_id 与失效 id），便于治理。

---

## 9. 前端界面

### 9.1 新增页面 `AgentHubPage`（路由 `/agents`）

- 侧边栏新增「Agent 管理」入口（能力中心旁）。
- 列表：紧凑卡片，展示名称、描述、状态徽标、模型、绑定计数、最近使用；
  支持搜索与状态筛选；风格对齐能力中心（无渐变、无卡片套卡片）。
- 创建/编辑：表单式抽屉或独立视图：
  - 基础：名称、描述、系统提示词（多行，带字数统计）、模型（下拉 +
    自定义）、记忆开关、scope（首期固定 user，internal 仅展示为只读标签）。
  - 绑定：四个分区——Skill（复用能力中心卡片选择）、内置工具（按分组
    复选）、MCP 工具（按 Server 分组批量勾选，展开到工具级）、知识库。
  - 失效绑定以警示样式标记并支持一键移除。

### 9.2 会话内 Agent 选择

- `ChatWindow` 会话头部新增 Agent 选择器：仅列出 `scope=user` 且 `active`
  的 Agent；支持「不使用 Agent（默认）」选项。
- 切换即调用会话更新接口；失败（409）时提示并回退。
- 消息级展示来源 Agent（数据来自 `messages.agent_id`），样式轻量（如
  头像徽标），不引入大面积装饰。

### 9.3 类型与 API 层

`frontend/src/types/api.ts` 新增 `Agent` / `AgentDetail` / `AgentBinding`
类型；`api` 层新增对应请求封装；`sse.ts` 请求体透传 `agent_id`。

---

## 10. 迁移与兼容

1. `db.py` 启动初始化：`create_all` 建五张新表；幂等 DDL 为 `sessions` /
   `messages` 补 `agent_id` 列与索引（`ADD COLUMN IF NOT EXISTS` +
   `CREATE INDEX IF NOT EXISTS`）。
2. 旧行为兼容：无 `agent_id` 的请求/会话走现有路径；`skills`、
   `knowledge_bases` 等请求参数语义不变。
3. 旧会话绑定已归档 Agent：历史可读；新消息按旧逻辑执行并提示。
4. `AGENT_HUB_ENABLED=false`：全部新端点 404，对话链路跳过解析器，
   行为与现状完全一致。
5. 前端旧版本：不发送 `agent_id`，后端按会话绑定/旧逻辑执行，无破坏。

---

## 11. 测试与验收

### 11.1 后端测试

| 文件 | 覆盖 |
|---|---|
| `tests/test_agents_api.py` | CRUD、启停、归档/物理删除、重名 409、租户上限、绑定全量替换、跨租户 404、`AGENT_HUB_ENABLED=false` 404 |
| `tests/test_agent_resolution.py` | 解析器：无 Agent 旧逻辑、Skill 过滤、内置工具空绑定不限制、MCP 失效绑定跳过、模型回退链、提示词叠加结构、记忆开关 |
| `tests/test_chat_agent_binding.py` | 会话绑定 Agent 的对话流（fake LLM）：prompt 注入、工具白名单生效、中途切换下一条消息生效、`messages.agent_id` 落库、请求体覆盖会话绑定 |

### 11.2 前端测试

- `AgentHubPage` 渲染、创建/编辑表单校验、绑定交互、失效项提示。
- `ChatWindow` Agent 选择器：列表过滤（仅 user+active）、切换调用、409 回退。

### 11.3 验收标准

1. 创建 Agent 并绑定 1 个 Skill + 1 个 MCP 工具后，会话选择该 Agent：
   提示词含身份块与该 Skill 指令，工具列表仅含白名单项。
2. 中途切换 Agent，下一条消息按新 Agent 能力集执行，历史完整保留。
3. 删除该 Agent 所绑定的 MCP Server 后对话不报错，工具静默缺席，
   详情页显示失效绑定。
4. 不选择 Agent 的会话行为与上线前完全一致（回归通过）。
5. 审计日志与 Trace 可按 Agent 维度检索。

---

## 12. 里程碑与交付顺序

| 里程碑 | 范围 | 退出条件 |
|---|---|---|
| H1 定义层 | 五张新表 + `AgentRepository` + `AgentManager` + `/agents` CRUD/绑定 API + `db.py` 补列 + 配置项 | API 测试全绿，可创建/编辑/启停/绑定 |
| H2 运行时 | `resolver.py` + `ToolContext` 扩展 + `base_agent`/`router_chat`/会话绑定接入 | 对话测试全绿，按 Agent 注入能力集，旧逻辑回归通过 |
| H3 前端 | `AgentHubPage` + 会话 Agent 选择器 + 类型/API 封装 | 前端可完整管理 Agent 并在会话中选择/切换 |
| H4 观测 | 审计事件、Trace/指标 agent 维度、`/usage` API | 可按 Agent 检索用量与审计 |
| M2 编排 | Supervisor 真实路由（消费 `description` + `scope=internal`） | 多 Agent 自动分工可用，独立评审 |

依赖关系：H1 → H2 → H3 相互串行；H4 依赖 H2；M2 依赖 H1/H2，可与 H3/H4 并行。

---

## 13. 参考依据

- 本项目真实代码：`agents/base_agent.py`、`agents/registry.py`、
  `harness/context.py`、`harness/supervisor.py`、`skills/manager.py`、
  `mcp/manager.py`、`storage/models.py`、`storage/db.py`、
  `api/router_chat.py`、`api/schemas.py`、`config.py`。
- [AGENT_HUB_RESEARCH.md](./AGENT_HUB_RESEARCH.md)：现状盘点、业界对标
  （Claude Code Subagents / OpenAI Assistants / Dify / LangGraph）与决策背景。
- [MCP_SKILL_ECOSYSTEM_SPEC.md](./MCP_SKILL_ECOSYSTEM_SPEC.md)：能力中心
  租户隔离、能力审批与 `resolve_tools` 白名单语义（本规格直接复用）。
