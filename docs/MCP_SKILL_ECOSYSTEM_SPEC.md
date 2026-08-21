# Skill 与 MCP 统一能力中心 · 技术规格书

> 版本：v1.0.0（草案）
> 日期：2026-08-22
> 状态：待评审
> 里程碑代号：CAP-HUB
> 关联文档：
> [MCP_SKILL_ECOSYSTEM_RESEARCH.md](./MCP_SKILL_ECOSYSTEM_RESEARCH.md)、
> [SKILL_GUIDE.md](./SKILL_GUIDE.md)、
> [OBSERVABILITY_SPEC.md](./OBSERVABILITY_SPEC.md)、
> [KNOWLEDGE_BASE_INDEXING_UX_SPEC.md](./KNOWLEDGE_BASE_INDEXING_UX_SPEC.md)

本文定义现有 Skill 技能库升级为 Skill 与 MCP 共存的统一能力中心所需的
产品模型、后端架构、数据模型、运行时解析、API、安全治理、可观测性、前端
界面、迁移、测试和验收标准。首个可交付版本聚焦远程 Streamable HTTP MCP
Tools；MCP Resources、Prompts、OAuth 和本地 `stdio` 按后续里程碑扩展。

> **注意：** 当前仓库只有可运行的 Skill 生命周期和一个不执行连接的 MCP
> 适配器占位。本文描述的是目标实现，不代表当前 MCP 功能已经可用。

---

## 0. 决策摘要

本节固定实施期间的核心决策。实现不得在页面、API 和运行时分别创造不同的
Skill 或 MCP 语义。

1. 产品入口统一为 **能力中心**，首期继续复用 `/skills` 路由，避免破坏现有
   导航和深链。
2. Skill 与 MCP Server 是两类独立的一等实体，不把 MCP 配置嵌入
   `SKILL.md`，也不把 MCP Server 当作 Skill 子类型。
3. Skill 负责指令、流程、资源和受控本地工具；MCP Server 负责通过协议发现
   并调用外部 Tools，后续再支持 Resources 与 Prompts。
4. 页面统一，运行时分离。Skill 和 MCP 共用搜索、筛选、启停、审计和状态
   设计，但使用不同的 Provider 和生命周期。
5. MCP 工具不得全部永久注册到进程级全局 ToolRegistry。Agent 每次运行前
   必须根据租户、会话、Agent 和策略解析本次允许的能力集。
6. MCP 工具名必须带 Server 前缀，避免多个 Server 暴露同名工具。
7. 新导入的外部 Skill 和新添加的 MCP Server 默认停用。包含脚本、写操作或
   破坏性工具时，启用前必须展示风险和确认步骤。
8. MCP M1 只支持远程 Streamable HTTP 和 Tools。`stdio`、旧 SSE、OAuth、
   Resources、Prompts 和状态型长连接不进入首个生产里程碑。
9. MCP 配置必须按租户隔离；密钥只保存引用或密文，不得通过列表、日志、
   Trace 和错误响应返回。
10. 前端采用明亮、专业、数据优先的工作台风格。Skill 和 MCP 主列表使用紧凑
    小卡片，不使用大面积渐变、卡片套卡片或装饰性 AI 动效。
11. 前端可复用 Beautiful UI 的交互状态机，但必须映射到项目现有 React、
    Ant Design、Material Symbols 和 CSS token，不引入完整上游 CSS reset。
12. 任一 MCP Server 连接失败不得阻断其他 Server、内置工具或 Skill 工具。

---

## 1. 背景与现状

当前项目已经具备完整度较高的 Skill 管理闭环，但工具注册、租户隔离和运行时
能力选择仍以单实例为中心。MCP 进入系统后会放大这些边界，因此不能只补全
`mcp_adapter.py` 的连接代码。

### 1.1 已有 Skill 能力

当前实现已经提供以下能力，改造必须保持兼容。

- 从内置目录和外部目录递归发现 `SKILL.md`。
- 解析 YAML frontmatter、正文、版本、作者和标签。
- 从 ZIP、TAR、TGZ、`.skill`、浏览器文件夹和单 Markdown 导入。
- 拒绝路径穿越、符号链接、危险路径、超大包和过多文件。
- 对外部 Skill 执行启用、停用、重新扫描和删除。
- 将已启用 Skill 的描述和正文注入 Agent system prompt。
- 通过 `builtin_read_skill_file` 读取已启用 Skill 内的文本资源。
- 将 `scripts/*.py` 的 `run()` 暴露为 LangChain 工具。
- 在 Skill 变化后重建编译态 LangGraph ToolNode。
- 在 `/skills` 页面提供列表、详情、搜索、导入、启停和删除。

### 1.2 已确认的 MCP 边界

当前 MCP 只存在接口占位，不能视为已实现能力。

- `backend/app/tools/mcp_adapter.py` 的 `register_mcp_tools()` 返回 `0`。
- 后端依赖未包含 `langchain-mcp-adapters` 和 MCP Python SDK。
- 数据库没有 MCP Server、能力快照、凭据引用或健康状态。
- API 没有添加、测试、发现、启停和删除 MCP Server 的入口。
- FastAPI lifespan 不会恢复或连接已启用的 MCP Server。
- 前端没有 MCP Server 管理页面和连接流程。
- 测试没有启动真实 MCP Server，也没有验证 Agent 工具调用。

### 1.3 当前结构性问题

以下问题在加入 MCP 前必须被纳入设计，不得通过局部补丁长期保留。

- ToolRegistry 是进程级全局单例，只记录名称、描述和来源。
- LangGraph 编译时固定全部工具，能力启停需要重建全局 Graph。
- `skills` 表和 SkillManager 没有完整租户隔离。
- 对话请求的 Skill 白名单只影响 Prompt，不限制全局工具可见性。
- 前端默认把所有已启用 Skill 发送给每一次对话。
- Skill Python 脚本通过 `exec()` 在后端进程内执行，没有沙箱。
- 工具审批只有全局开关，没有只读、写入和破坏性操作的分级策略。

---

## 2. 目标与非目标

本期目标是交付可管理、可调用、可隔离和可观察的统一能力中心，同时保留现有
Skill 使用方式。

### 2.1 产品目标

能力中心必须让用户完成以下任务。

- 在一个页面查看和管理 Skills 与 MCP Servers。
- 快速识别能力类型、启用状态、健康状态和需要处理的问题。
- 继续导入外部 Skill，并通过向导添加远程 MCP Server。
- 测试 MCP 连接、发现工具、审查工具风险并选择允许的工具。
- 在会话或 Agent 范围内选择允许使用的能力。
- 查看 MCP Server 的最近状态、工具数量、调用表现和错误摘要。
- 安全停用或删除能力，不影响其他健康能力。

### 2.2 工程目标

后端必须建立以下基础能力。

- 建立 SkillProvider、McpProvider 和统一 CapabilityResolver。
- 建立按租户隔离的 MCP 持久化模型。
- 建立动态工具解析和执行路径，消除 MCP 启停对全局 Graph 重建的依赖。
- 建立工具命名、能力版本、风险、审批和白名单策略。
- 建立健康检查、超时、重试、熔断和单 Server 故障隔离。
- 复用现有鉴权、审计、Trace 和 SSE 事件体系。
- 保持旧 `/skills` API 和 `skills` 请求字段的兼容性。

### 2.3 首期非目标

以下能力不进入 M1，不得通过隐式扩展拖慢首个可用版本。

- 不实现 MCP Registry 或公开能力市场。
- 不实现 MCP Resources 和 Prompts 的 Agent 注入。
- 不实现 OAuth 2.1 浏览器授权流程。
- 不启动用户提交的任意 `stdio` command。
- 不兼容已废弃 HTTP+SSE Server，除非明确开启兼容开关。
- 不实现跨租户共享私有 MCP 凭据。
- 不为 Skill Python 脚本交付完整容器沙箱。
- 不提供用户自定义 Graph、工作流或任意 ToolNode 编排器。
- 不把全部可用 MCP 工具无条件绑定给每一次模型调用。

---

## 3. 术语与产品模型

本节统一产品文案、API 命名和代码命名。页面不得混用“插件”“连接器”“工具包”
和“技能”等含义不稳定的名称。

### 3.1 能力中心

能力中心是统一管理入口，不是一种新的运行时协议。它聚合 Skill 和 MCP Server
的元数据、状态、策略和审计入口。

内部代码使用 `CapabilityHub` 或 `CapabilityResolver`；页面首期标题使用
“能力中心”，侧边栏可以暂时保留“技能库”文案以降低导航迁移成本。

### 3.2 Skill

Skill 是由 `SKILL.md` 定义的指令与资源包，可包含 references、assets 和本地
Python 脚本工具。

Skill 的主要控制面包括：

- Manifest 与资源校验。
- 导入、版本、来源和文件统计。
- 租户可用性与启停状态。
- Prompt 注入和资源读取。
- 本地脚本工具注册与风险提示。

### 3.3 MCP Server

MCP Server 是通过 MCP 协议暴露能力的远程或本地服务。首期只接入远程
Streamable HTTP Server。

MCP Server 的主要控制面包括：

- Endpoint、transport 和认证配置。
- 连接测试和协议初始化。
- Tools、Resources 和 Prompts 能力发现。
- 工具白名单、健康状态和调用策略。
- 凭据、超时、重试和熔断。

### 3.4 MCP Capability

MCP Capability 是一次能力发现得到的 Server 原语快照，类型为 `tool`、
`resource` 或 `prompt`。M1 只允许 `tool` 进入运行时。

能力快照用于比较工具新增、删除和 Schema 变化，不等同于持久连接。

### 3.5 能力绑定

能力绑定描述某个 Skill 或 MCP Tool 在租户、Agent 或会话范围内是否可用。

首期支持以下激活模式：

| 枚举 | 产品文案 | 行为 |
|---|---|---|
| `always` | 默认可用 | 每次对话默认进入候选能力集 |
| `explicit` | 按需选择 | 只有请求或 Agent 配置显式选择时可用 |
| `disabled` | 已停用 | 不进入 Prompt 和工具集合 |

迁移后的内置 Skill 默认使用 `always`；新 MCP Tool 默认使用 `explicit`。

---

## 4. 总体架构

目标架构分为控制面和运行面。控制面负责管理与治理，运行面负责为单次 Agent
执行解析最小能力集合。

### 4.1 组件关系

```text
┌──────────────────────── Capability hub control plane ────────────────────────┐
│ Skills API/UI      MCP API/UI       Policy      Secrets      Health/Audit   │
└──────────┬──────────────┬──────────────┬────────────┬──────────────┬──────────┘
           │              │              │            │              │
      SkillProvider   McpProvider   CapabilityResolver         Observability
           │              │              │
           │         MCP adapter          │
           │              │              ▼
           └──────────────┴──── ResolvedCapabilitySet
                                          │
                              ┌───────────┴───────────┐
                              ▼                       ▼
                       Prompt composer       Dynamic tool dispatcher
                              │                       │
                              └────────── Agent/Graph ┘
```

### 4.2 模块职责

模块必须遵循以下职责边界。

| 模块 | 职责 | 不负责 |
|---|---|---|
| `SkillProvider` | Skill 发现、Prompt、资源和脚本工具 | MCP 连接与认证 |
| `McpProvider` | Server 配置、发现、调用和健康 | Skill Manifest 解析 |
| `CapabilityResolver` | 按请求生成 Prompt 和工具 ID 集合 | 直接执行工具 |
| `ToolCatalog` | 保存稳定工具元数据与 Provider 来源 | 保存请求态凭据 |
| `DynamicToolDispatcher` | 校验白名单、审批并调用 Provider | 全局重建 Graph |
| `SecretStore` | 保存或读取密钥引用 | 向 API 返回密钥值 |
| `CapabilityPolicy` | 风险、审批、租户和作用域判断 | 根据模型输出绕过策略 |

### 4.3 Provider 合同

所有能力 Provider 必须实现稳定合同，避免 Graph 和 API 依赖具体实现。

```python
class CapabilityProvider(Protocol):
    async def list_capabilities(self, context: CapabilityContext) -> list[Capability]: ...
    async def resolve_tools(self, refs: list[CapabilityRef], context: CapabilityContext) -> list[BaseTool]: ...
    async def invoke_tool(self, ref: CapabilityRef, arguments: dict, context: CapabilityContext) -> ToolResult: ...
```

Provider 返回的错误必须转换为统一错误码，不能把底层 SDK 堆栈直接返回给用户。

---

## 5. 数据模型

数据模型必须区分定义、租户绑定、远程能力快照和运行状态。密钥值不得保存在
普通 JSON 字段。

### 5.1 `skills` 表扩展

保留现有表和整数主键，增加以下字段。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `owner_tenant_id` | `VARCHAR(64) NULL` | `null` | 内置为 `null`，外部 Skill 归属租户 |
| `source` | `VARCHAR(16)` | `external` | `builtin` 或 `external` |
| `manifest_hash` | `VARCHAR(64) NULL` | `null` | Manifest 和受管文件版本摘要 |
| `file_count` | `INTEGER` | `1` | 文件数量 |
| `has_scripts` | `BOOLEAN` | `false` | 是否包含可执行脚本 |
| `risk_level` | `VARCHAR(16)` | `low` | `low`、`medium`、`high` |
| `validation_status` | `VARCHAR(16)` | `valid` | `valid`、`warning`、`invalid` |

现有全局 `name UNIQUE` 必须迁移为：

- 内置 Skill 名称全局唯一。
- 外部 Skill 在 `(owner_tenant_id, name)` 范围内唯一。
- 外部托管目录使用租户不可逆标识分层，不能继续全部写到同一名称目录。

### 5.2 `skill_bindings` 新表

Skill 定义与租户启用状态必须分离，以支持同一个内置 Skill 在不同租户下使用
不同策略。

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | `BIGINT PK` | 绑定 ID |
| `tenant_id` | `VARCHAR(64)` | 租户 |
| `skill_id` | `BIGINT FK` | Skill ID |
| `activation_mode` | `VARCHAR(16)` | `always`、`explicit`、`disabled` |
| `allowed_tools` | `JSONB` | 允许的脚本工具名；空数组表示不允许脚本工具 |
| `approval_policy` | `VARCHAR(16)` | `never`、`write`、`always` |
| `created_at` | `TIMESTAMPTZ` | 创建时间 |
| `updated_at` | `TIMESTAMPTZ` | 更新时间 |

`(tenant_id, skill_id)` 必须唯一。旧 `skills.enabled` 在兼容期只作为默认值读取，
最终由 binding 取代。

### 5.3 `mcp_servers` 新表

MCP Server 是租户拥有的连接配置和健康实体。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `id` | `UUID PK` | 自动生成 | Server ID |
| `tenant_id` | `VARCHAR(64)` | 无 | 租户隔离 |
| `name` | `VARCHAR(128)` | 无 | 租户内唯一名称 |
| `description` | `TEXT NULL` | `null` | 用户维护的说明 |
| `transport` | `VARCHAR(24)` | `streamable_http` | 传输方式 |
| `endpoint_url` | `TEXT` | 无 | MCP Endpoint |
| `auth_type` | `VARCHAR(24)` | `none` | `none`、`bearer`、`headers`、`oauth` |
| `secret_ref` | `TEXT NULL` | `null` | SecretStore 引用，不存密钥 |
| `activation_mode` | `VARCHAR(16)` | `disabled` | Server 激活模式 |
| `health_status` | `VARCHAR(16)` | `unknown` | 健康状态 |
| `protocol_version` | `VARCHAR(32) NULL` | `null` | 最近协商协议版本 |
| `server_info` | `JSONB` | `{}` | 脱敏后的 Server 信息 |
| `capability_revision` | `INTEGER` | `0` | 能力快照版本 |
| `last_checked_at` | `TIMESTAMPTZ NULL` | `null` | 最近检查时间 |
| `last_error_code` | `VARCHAR(64) NULL` | `null` | 稳定错误码 |
| `last_error_message` | `TEXT NULL` | `null` | 脱敏错误摘要 |
| `created_by` | `VARCHAR(64)` | 无 | 创建用户 |
| `created_at` | `TIMESTAMPTZ` | 当前时间 | 创建时间 |
| `updated_at` | `TIMESTAMPTZ` | 当前时间 | 更新时间 |

`(tenant_id, name)` 必须唯一。`endpoint_url` 不返回解析后的认证信息和敏感查询
参数。

### 5.4 `mcp_capabilities` 新表

能力表保存最近一次成功发现的 Server 能力快照。

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `id` | `UUID PK` | 自动生成 | 能力 ID |
| `server_id` | `UUID FK` | 无 | 所属 Server |
| `kind` | `VARCHAR(16)` | `tool` | `tool`、`resource`、`prompt` |
| `remote_name` | `VARCHAR(255)` | 无 | Server 返回的名称 |
| `effective_name` | `VARCHAR(255)` | 无 | 发送给模型的唯一名称 |
| `title` | `VARCHAR(255) NULL` | `null` | 展示标题 |
| `description` | `TEXT` | 空串 | 描述 |
| `input_schema` | `JSONB` | `{}` | 输入 JSON Schema |
| `output_schema` | `JSONB NULL` | `null` | 输出 Schema |
| `annotations` | `JSONB` | `{}` | Server 声明的注解 |
| `enabled` | `BOOLEAN` | `false` | 是否进入候选工具集 |
| `risk_level` | `VARCHAR(16)` | `medium` | 平台判定风险，不直接信任 Server |
| `approval_policy` | `VARCHAR(16)` | `write` | 审批策略 |
| `schema_hash` | `VARCHAR(64)` | 无 | Schema 变更检测 |
| `review_status` | `VARCHAR(16)` | `pending` | `pending`、`approved`、`changed` |
| `last_seen_at` | `TIMESTAMPTZ` | 当前时间 | 最近发现时间 |
| `removed_at` | `TIMESTAMPTZ NULL` | `null` | Server 已移除该能力的时间 |

`(server_id, kind, remote_name)` 必须唯一。新发现工具默认 `enabled=false` 和
`review_status=pending`。

### 5.5 有效工具名

MCP 工具必须使用稳定、可读且可追踪的名称。

```text
mcp_{server_slug}__{remote_tool_name}
```

生成规则必须：

- 将 Server 名称和工具名规范化为模型支持的字符集。
- 保留 `remote_name`，不得只保存转换后的名称。
- 在冲突时附加稳定短哈希，不得依赖加载顺序。
- 限制最终长度，并在截断时保留哈希。
- 在 Trace 中同时记录 `effective_name` 和 `remote_name`。

---

## 6. MCP 生命周期与状态机

MCP 管理必须区分配置生命周期、健康状态和能力审查状态。一个停用 Server 不
等于连接失败，一个连接成功 Server 也不等于所有工具都已获准。

### 6.1 Server 配置生命周期

```text
draft
  └─ test ─→ discovered ─→ disabled ─→ enabled
       │          │            ↑          │
       └─ error ←─┴────────────┴──────────┘
                                      delete → deleted
```

生命周期规则如下：

- 添加配置后先保存为 `draft`，不得自动对 Agent 开放。
- **测试连接** 成功后执行协议初始化和能力发现。
- 首次发现成功后保存能力快照，Server 仍保持停用。
- 用户审查并选择工具后才能启用。
- 修改 endpoint、transport 或认证后必须重新测试和发现。
- 删除时先停用工具，再删除 Secret 引用和 Server 记录。

### 6.2 健康状态

健康状态只描述最近一次连接和调用表现。

| 枚举 | 产品文案 | 触发条件 |
|---|---|---|
| `unknown` | 未检查 | 从未成功检查或配置已变化 |
| `checking` | 检查中 | 正在初始化或发现能力 |
| `ready` | 正常 | 最近检查成功，关键能力可用 |
| `degraded` | 部分可用 | 连接成功但部分工具失败或延迟异常 |
| `error` | 连接异常 | 初始化、发现或连续调用失败 |
| `disabled` | 已停用 | 用户停用，不执行后台调用 |

状态必须包含文字和图标，不得只依靠颜色。

### 6.3 能力刷新规则

能力发现必须比较新旧 Schema，并执行以下规则。

- 新增能力写入 `pending`，默认停用。
- 名称和 Schema 未变化的能力保留启用和审批状态。
- Schema 或风险注解变化的能力标记为 `changed`，暂停该工具的新调用。
- Server 删除的能力立即从 Resolver 移除，并记录 `removed_at`。
- 单个工具解析失败不丢弃该 Server 其他有效工具。
- 刷新失败保留最近一次成功快照，但 Server 标记为 `degraded` 或 `error`。

### 6.4 会话模式

M1 默认使用无状态调用：每次工具调用创建并清理 MCP Session。只有 Server
明确要求连续上下文时，后续版本才允许状态型 Session。

状态型 Session 必须有独立连接上限、空闲超时、租户绑定和关闭流程，不能
复用无状态 Provider 的生命周期假设。

---

## 7. 运行时能力解析

Agent 运行时必须只看到本次请求允许使用的 Prompt 和 Tools。全局启用不等于
无条件注入。

### 7.1 解析输入

CapabilityResolver 接收以下上下文：

```python
CapabilityContext(
    tenant_id=tenant_id,
    user_id=user_id,
    session_id=session_id,
    agent_id=agent_id,
    requested_capabilities=requested_capabilities,
)
```

解析顺序必须为：

1. 加载租户和用户上下文。
2. 加载 `always` 能力和请求显式选择的 `explicit` 能力。
3. 排除停用、无权、健康不可用和待审查能力。
4. 应用 Agent、会话和工具级白名单。
5. 计算 Skill Prompt、资源权限和工具 ID。
6. 生成带 revision 的 `ResolvedCapabilitySet`。

### 7.2 解析输出

解析结果只保存稳定引用，不把凭据或 SDK Session 写入 LangGraph State。

```python
ResolvedCapabilitySet(
    revision="capset-...",
    skill_names=[...],
    prompt_fragments=[...],
    tool_refs=[...],
    approval_policies={...},
)
```

可持久化 State 只保存 `revision`、Skill 名称和 tool refs。Provider 在调用时根据
租户上下文重新解析运行对象。

### 7.3 动态工具执行

现有编译态 ToolNode 必须逐步替换为动态分发节点。

动态分发必须：

- 校验模型返回的工具名存在于当前 `ResolvedCapabilitySet`。
- 拒绝调用已停用、已变更或不属于当前租户的工具。
- 在调用前执行审批策略。
- 为每次调用设置超时和最大输出限制。
- 将工具错误作为结构化 ToolMessage 返回，不泄露敏感信息。
- 将 transport、Server、工具和结果状态写入 Trace。

### 7.4 工具数量控制

单次发送给模型的工具数量必须可配置。超过限制时优先使用显式选择、关键词
匹配和最近使用排序，不得简单截断数据库顺序。

首期默认值：

```text
CAPABILITY_MAX_TOOLS_PER_RUN = 32
```

当候选工具超过上限时，Resolver 必须记录裁剪数量，并在调试信息中提供原因。

---

## 8. API 规格

API 继续使用现有 `/api/v1` 前缀、`ApiResponse` 包装、鉴权依赖和审计模式。
所有 MCP 查询和写入必须包含租户条件。

### 8.1 能力中心摘要

```http
GET /api/v1/capabilities/summary
```

响应示例：

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "skills": {"total": 8, "enabled": 6, "with_scripts": 2},
    "mcp_servers": {"total": 3, "ready": 2, "degraded": 1},
    "mcp_tools": {"discovered": 24, "enabled": 9, "pending_review": 3},
    "needs_attention": 4
  }
}
```

### 8.2 Skill API 兼容

现有 Skill API 路径保持可用：

```text
GET    /api/v1/skills
POST   /api/v1/skills/import
GET    /api/v1/skills/{name}
POST   /api/v1/skills/{name}/enable
POST   /api/v1/skills/{name}/disable
POST   /api/v1/skills/reload
DELETE /api/v1/skills/{name}
```

列表和详情响应增加 `id`、`risk_level`、`validation_status` 和
`activation_mode`，旧字段继续返回。

### 8.3 MCP Server API

```text
GET    /api/v1/mcp/servers
POST   /api/v1/mcp/servers
GET    /api/v1/mcp/servers/{server_id}
PATCH  /api/v1/mcp/servers/{server_id}
POST   /api/v1/mcp/servers/{server_id}/test
POST   /api/v1/mcp/servers/{server_id}/discover
POST   /api/v1/mcp/servers/{server_id}/enable
POST   /api/v1/mcp/servers/{server_id}/disable
DELETE /api/v1/mcp/servers/{server_id}
```

创建请求示例：

```json
{
  "name": "internal-github",
  "description": "研发仓库只读检索",
  "transport": "streamable_http",
  "endpoint_url": "https://mcp.example.com/mcp",
  "auth": {
    "type": "bearer",
    "secret_value": "submitted-once"
  }
}
```

响应不得返回 `secret_value`。只返回：

```json
{
  "auth_type": "bearer",
  "credential_configured": true
}
```

### 8.4 MCP Capability API

```text
GET   /api/v1/mcp/servers/{server_id}/capabilities
PATCH /api/v1/mcp/servers/{server_id}/capabilities/{capability_id}
POST  /api/v1/mcp/servers/{server_id}/capabilities/bulk-review
```

批量审查请求示例：

```json
{
  "items": [
    {
      "capability_id": "4bbfd3ee-...",
      "enabled": true,
      "approval_policy": "never"
    },
    {
      "capability_id": "7e234820-...",
      "enabled": true,
      "approval_policy": "always"
    }
  ]
}
```

### 8.5 对话请求兼容

现有 `skills` 字段继续兼容，同时新增统一能力引用。

```json
{
  "message": "查询今天失败的流水线",
  "skills": ["release-analysis"],
  "capabilities": [
    {"kind": "skill", "id": "release-analysis"},
    {"kind": "mcp_tool", "id": "4bbfd3ee-..."}
  ]
}
```

兼容规则如下：

- 只有 `skills` 时按旧逻辑映射为 Skill refs。
- 同时提供时合并去重。
- 请求引用无权或停用能力时返回稳定错误，不静默放宽权限。
- MCP 工具未显式请求时，只加入 activation mode 为 `always` 的已批准工具。

### 8.6 错误码

API 必须使用稳定错误码，以支持前端显示可恢复操作。

| 错误码 | HTTP | 含义 | 前端动作 |
|---|---:|---|---|
| `MCP_CONFIG_INVALID` | 422 | 配置格式无效 | 定位字段 |
| `MCP_ENDPOINT_BLOCKED` | 422 | Endpoint 不符合网络策略 | 修改地址或联系管理员 |
| `MCP_AUTH_FAILED` | 401/422 | 远程认证失败 | 更新凭据 |
| `MCP_CONNECT_TIMEOUT` | 504 | 连接超时 | 重试或检查网络 |
| `MCP_PROTOCOL_ERROR` | 502 | 初始化或协议不兼容 | 查看协议版本 |
| `MCP_DISCOVERY_FAILED` | 502 | 能力发现失败 | 保留旧快照并重试 |
| `MCP_TOOL_SCHEMA_INVALID` | 422 | 工具 Schema 无法适配 | 停用问题工具 |
| `MCP_TOOL_REVIEW_REQUIRED` | 409 | 工具待审查 | 进入工具审查 |
| `MCP_TOOL_NOT_ALLOWED` | 403 | 当前请求无权调用 | 检查绑定策略 |
| `MCP_SERVER_NOT_READY` | 409 | Server 未就绪 | 测试连接或启用 |
| `CAPABILITY_LIMIT_EXCEEDED` | 422 | 本次工具数超过限制 | 缩小能力范围 |

---

## 9. 安全与治理

MCP Server 和 Skill 脚本都属于可执行能力面。实现必须把来源、凭据、网络、
工具风险和输出内容视为不可信输入。

### 9.1 租户隔离

所有 MCP repository 方法必须显式接收 `tenant_id`。禁止先按 ID 查询，再在
Python 层判断租户。

- Server、能力、凭据引用和审计必须按租户隔离。
- 外部 Skill 文件目录必须按租户分层。
- ToolCatalog 可以缓存元数据，但缓存 key 必须包含 tenant 和 revision。
- 当前信任 `X-Tenant-Id` 的开发期模式不得作为生产 MCP 授权边界。

### 9.2 凭据处理

凭据必须遵循以下规则。

- API 只允许一次性提交密钥值。
- 数据库只保存 `secret_ref` 或应用级加密密文。
- 列表、详情、日志、审计和 Trace 只返回“已配置”状态。
- 修改非认证字段不得要求前端回传已有密钥。
- 删除 Server 必须删除或吊销对应 Secret。
- Header 名称需要白名单，禁止覆盖 `Host`、`Origin` 和转发相关 Header。

### 9.3 网络与 SSRF

远程 Endpoint 必须经过出站网络策略。

- 生产环境默认只允许 `https`。
- 拒绝包含用户名密码的 URL。
- 解析 DNS 后检查目标地址，并在重定向后重新检查。
- 禁止自动跟随到 metadata、loopback 和未授权内网地址。
- 企业内网 MCP 通过显式 allowlist 或 egress proxy 开放。
- 限制重定向次数、连接超时、读取超时和最大响应体积。

### 9.4 工具风险与审批

平台不能直接信任 MCP Server 自报的只读或破坏性注解。Server 注解只作为风险
判定输入之一。

| 风险 | 示例 | 默认策略 |
|---|---|---|
| `low` | 只读搜索、获取公开状态 | 可配置为无需审批 |
| `medium` | 读取私有内容、创建草稿 | 首次或写操作审批 |
| `high` | 发送消息、修改记录、部署、删除 | 每次审批 |

审批必须显示 Server、工具、关键参数、影响对象和可撤销性。不得只显示“是否
允许执行工具”。

### 9.5 工具输出

工具输出可能包含 Prompt Injection、敏感信息和大体积内容。

- 保留结构化内容类型，不把所有输出无条件拼接为 Prompt。
- 对文本、图片和二进制内容分别设置大小限制。
- 对日志和 Trace 执行脱敏与截断。
- 不自动执行工具输出中的命令、链接或二次工具指令。
- 将远程内容标记为外部工具结果，不提升为 system 指令。

### 9.6 `stdio` 后续要求

`stdio` 不进入 M1。后续开启时必须同时满足：

- command 使用管理员维护的绝对路径 allowlist。
- 不通过 shell 执行，不接受命令字符串拼接。
- args 使用结构化数组并执行长度限制。
- 环境变量使用 allowlist 和 Secret 引用。
- 子进程运行在独立用户或容器中。
- 设置 CPU、内存、文件系统、网络和执行时限。
- 进程退出、stderr 和重启次数进入健康与审计记录。

---

## 10. 前端设计系统

能力中心必须延续观测台和知识库的专业数据工作台方向，同时使用更紧凑的小
卡片承载 Skill 与 MCP Server。Beautiful UI 提供交互参考，不重新定义整个
项目视觉系统。

### 10.1 视觉方向

页面必须遵循以下约束。

- 使用项目现有浅色画布、白色表面、品牌蓝和 Material Symbols。
- 复用 `WorkspaceHeader`、`MetricStrip`、`StatusDot` 和
  `ResponsiveInspector` 的结构和语义。
- 参考观测台水平 Tab 的键盘交互和 URL 状态。
- 参考知识库总览的指标带、筛选工具栏、卡片网格、空态和响应式规则。
- 卡片圆角使用 10 至 12 像素，边框 1 像素，阴影保持轻量。
- 卡片 hover 只调整边框、背景和轻阴影，不执行明显上浮或缩放。
- 每张卡片只保留一层表面，不嵌套第二层统计卡片。
- 不使用大面积渐变、霓虹、光斑、巨型阴影和装饰性 AI 图标。
- 不为静态历史状态使用循环 shimmer、脉冲或打字机效果。
- 页面只保留一个随当前 Tab 变化的主操作。

### 10.2 Beautiful UI 参考映射

只采用与真实产品行为匹配的最小组件集合。

| Beautiful UI 参考 | 使用位置 | 借鉴内容 | 禁止照搬内容 |
|---|---|---|---|
| `search` | Skill/MCP 搜索 | 实时过滤、键盘清除、空态 | 独立全屏命令面板 |
| `filter-table` | 状态与类型筛选 | 状态 chip、筛选反馈 | 卡片逐个长动画 |
| `tool-chips` | MCP 卡片与检视器 | 紧凑工具摘要、展开调用信息 | 文件编辑演示数据 |
| `task-rows` | 测试连接与发现步骤 | running、failed、retry、ok 语法 | 模拟定时器和虚假进度 |
| `loading-state` | 超过 800ms 的测试与发现 | 动作名称、elapsed time、取消 | `Surfer` 视频和娱乐化加载 |
| `approval-card` | 高风险 Tool/Skill 启用 | 风险问题、选项和确认状态机 | 未连接后端的演示确认 |
| `records-table` | MCP 工具完整清单 | 稠密列、排序和选择 | 替换主卡片列表 |

如果复制 Beautiful UI 源代码或实质性样式，必须保留其 MIT License。当前项目
不使用 Tailwind，因此必须把需要的语义 token、组件规则和 keyframe 映射到
现有 CSS，禁止导入完整上游 base/reset。

### 10.3 颜色与状态

状态色必须与文字和图标一起使用。

| 语义 | 颜色 | 使用位置 |
|---|---|---|
| 品牌/主要操作 | 项目品牌蓝 | 主按钮、选中 Tab、焦点 |
| 正常/已启用 | 绿色或青绿 | 已启用 Skill、Ready MCP |
| 处理中 | 蓝色 | 测试连接、能力发现 |
| 待审查/部分可用 | 琥珀色 | Pending、Changed、Degraded |
| 失败/危险 | 红色 | Error、删除、破坏性工具 |
| 停用/未知 | 中性灰 | Disabled、Unknown |

普通文本对背景的对比度必须达到 4.5:1。颜色不能成为状态的唯一信号。

### 10.4 动效

动效只解释状态和空间关系。

- hover、focus 和 chip 使用 100 至 160 毫秒。
- Tab、工具展开和检视器内容切换使用 160 至 240 毫秒。
- Drawer 使用不超过 360 毫秒的进入和退出。
- 卡片筛选不使用 stagger、弹跳和翻牌。
- 运行中的测试允许 spinner 或低干扰 shimmer，结束后立即停止。
- `prefers-reduced-motion: reduce` 下关闭淡入、shimmer 和位移动画。

---

## 11. 信息架构与路由

能力中心首期复用 `/skills`，通过 URL 查询参数保存 Tab、搜索、筛选和当前
检视对象，确保刷新、返回和深链行为稳定。

### 11.1 路由

```text
/skills?tab=skills
/skills?tab=mcp
/skills?tab=skills&q=report&status=enabled&type=external
/skills?tab=mcp&q=github&health=degraded&transport=streamable_http
/skills?tab=skills&item=agent-mbti
/skills?tab=mcp&server={server_id}
```

未提供 `tab` 时默认进入 `skills`，保持现有用户预期。

### 11.2 页面骨架

页面按以下顺序组织：

```text
WorkspaceHeader
MetricStrip
Horizontal tabs: Skills | MCP Servers
Scoped discovery toolbar
Compact card grid
Grid footer / pagination
Responsive inspector or drawer
Import Skill modal / Add MCP modal
```

Tab 必须使用 `role=tablist`、`role=tab` 和 `role=tabpanel`，支持左右方向键、
Home 和 End。

### 11.3 页面头部

页面头部使用以下文案：

- 标题：**能力中心**。
- 说明：**统一管理 Skill 与 MCP Server，为 Agent 配置可信、可观察的能力。**
- Skills Tab 主操作：**导入 Skill**。
- MCP Servers Tab 主操作：**添加 MCP Server**。

次要刷新操作放在工具栏或更多菜单，不与主操作竞争。

### 11.4 指标带

指标带显示四项，参考观测台 `MetricStrip` 和知识库 summary band。

| 指标 | 主值 | 辅助信息 | 点击行为 |
|---|---:|---|---|
| Skills | Skill 总数 | 已启用数量 | 切换 Skills Tab |
| MCP Servers | Server 总数 | Ready 数量 | 切换 MCP Tab |
| 可用工具 | 已启用工具数 | Skill/MCP 拆分 | 打开工具筛选 |
| 需要处理 | Error、Degraded、Pending 总数 | 问题摘要 | 应用问题筛选 |

没有数据时显示 `0`，不得生成虚假趋势。

---

## 12. 卡片列表规格

Skill 和 MCP 主列表都使用紧凑小卡片，但卡片字段必须针对实体类型设计，不能
用一套字段强行统一。

### 12.1 卡片网格

推荐 CSS 结构如下：

```css
.capability-card-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 14px;
  align-items: stretch;
}
```

桌面宽屏最多显示四列；普通桌面显示三列；平板显示两列；窄屏显示一列。单卡
建议高度为 180 至 220 像素，内容变化时允许自然增高，不固定截断操作区。

### 12.2 通用卡片结构

所有卡片使用以下单层结构：

1. 顶部：类型图标、名称、主状态和更多菜单。
2. 中部：两行描述，超出使用省略并提供 Tooltip。
3. 元数据：最多三项关键指标。
4. 标签：最多三个，超出显示 `+N`。
5. 底部：最近更新或检查时间、详情入口和启停 Switch。

卡片本身使用 `article`。当内部包含 Switch 和菜单时，不得把整个卡片设置为
嵌套 `button`。名称或明确的 **查看详情** 控件负责打开检视器。

### 12.3 Skill 卡片

Skill 卡片必须展示：

- 名称和版本。
- 内置或外部来源。
- 已启用、已停用、警告或校验失败状态。
- 描述，两行截断。
- 文件数、资源数和脚本工具数。
- 最多三个标签。
- 包含脚本时显示静态安全标记。
- 启停 Switch 和更多菜单。

外部 Skill 的更多菜单包含 **查看文件**、**重新校验** 和 **删除**；内置 Skill
不显示删除。

### 12.4 MCP Server 卡片

MCP Server 卡片必须展示：

- Server 名称和 transport。
- Ready、Degraded、Error、Disabled 或 Unknown 状态。
- 描述或脱敏 hostname。
- 已发现工具数、已启用工具数和待审查数量。
- 最近检查耗时或最近一次成功时间。
- 认证是否已配置，不展示认证值。
- 启停 Switch、**测试连接** 和更多菜单。

Error 和 Degraded 卡片必须显示可执行下一步，例如 **重新测试** 或
**查看问题**，不能只显示红色状态。

### 12.5 加载、空态和错误

列表必须覆盖以下状态：

- 首次加载显示固定尺寸的卡片 Skeleton，避免网格跳动。
- Skills 为空时显示导入方式说明和 **导入 Skill**。
- MCP 为空时说明 Streamable HTTP 和安全边界，并显示 **添加 MCP Server**。
- 筛选无结果时显示 **清除筛选**，不显示创建引导。
- 请求失败时在网格区域显示错误摘要、request ID 和 **重新加载**。
- 部分卡片刷新失败时保留其他卡片，不把整个页面替换为错误页。

---

## 13. Skills Tab 与检视器

Skills Tab 保留现有功能，但从主从数据列表升级为紧凑卡片和响应式检视器。

### 13.1 筛选工具栏

Skills 工具栏包含：

- 搜索：名称、描述、作者和标签，输入使用 250 毫秒防抖。
- 来源：全部、内置、外部。
- 状态：全部、已启用、已停用、需要处理。
- 风险：全部、含脚本、高风险。
- 排序：最近更新、名称、版本和问题优先。

筛选状态写入 URL 查询参数。

### 13.2 Skill 检视器

桌面大于 1180 像素时使用右侧检视器；较窄窗口使用 Drawer。检视器按以下
顺序展示：

1. 名称、版本、来源、启用和校验状态。
2. 描述、作者、标签和托管路径。
3. 文件、references、assets 和 scripts 统计。
4. Manifest 摘要和最近加载时间。
5. 脚本工具清单与风险提示。
6. **启用/停用**、**重新校验** 和外部 Skill **删除**。

路径只显示紧凑形式；完整路径通过 Tooltip 提供。页面不得暴露租户目录散列
之外的敏感主机目录信息。

### 13.3 Skill 导入流程

现有导入 Modal 继续支持 Skill 包、文件夹和单 Markdown，并增加以下体验：

1. 选择导入模式和文件。
2. 显示文件数、总大小、Manifest、references 和 scripts 预检摘要。
3. 展示校验错误、警告和名称冲突。
4. 对覆盖和包含脚本分别确认。
5. 导入成功后保持默认停用，并聚焦新卡片。

包含脚本时可参考 Beautiful UI `approval-card` 的问题与确认状态机，但确认
必须调用真实导入 API。

---

## 14. MCP Servers Tab 与添加流程

MCP Tab 必须让用户从连接配置走到能力审查，而不是只保存一条 URL。

### 14.1 筛选工具栏

MCP 工具栏包含：

- 搜索：名称、描述、hostname 和工具名。
- 健康：全部、正常、部分可用、异常、未检查。
- 状态：全部、已启用、已停用、待审查。
- Transport：首期固定 Streamable HTTP，仍保留筛选结构。
- 排序：最近检查、名称、工具数和问题优先。

### 14.2 添加 MCP Server

添加流程使用宽 720 至 800 像素的分步 Modal。

#### 第一步：基本信息

本步骤收集名称、描述和 transport。M1 transport 固定为 Streamable HTTP，
页面说明 `stdio` 尚未开放，不显示不可用的复杂字段。

#### 第二步：Endpoint 与认证

本步骤收集 Endpoint、认证类型和一次性密钥。

- Endpoint 输入显示协议和网络策略提示。
- Bearer 和自定义 Header 密钥使用密码输入框。
- 编辑现有 Server 时显示“凭据已配置”，不回填密钥。
- 用户只有修改凭据时才输入新值。

#### 第三步：测试与发现

提交后按真实后端事件展示以下任务，参考 Beautiful UI `task-rows`：

```text
校验 Endpoint
建立安全连接
初始化 MCP 协议
读取 Server 信息
发现 Tools
保存能力快照
```

超过 800 毫秒后参考 `loading-state` 显示“正在测试 MCP 连接”和已耗时。不得用
前端 timer 模拟步骤完成。

#### 第四步：审查与保存

成功发现后展示：

- Server 名称、协议版本和认证状态。
- Tools 总数、新增、变化和不兼容数量。
- 工具名、描述、输入字段、风险和默认审批策略。
- 工具批量选择和逐项调整。

工具摘要可使用 `tool-chips`；完整清单使用 `records-table` 风格的紧凑表格。
新工具默认不选中。保存后 Server 默认停用，用户可以在摘要页显式启用。

### 14.3 MCP 检视器

检视器按以下顺序展示：

1. 名称、状态、transport 和启停 Switch。
2. 脱敏 Endpoint、认证状态和协议版本。
3. 已发现、已启用、待审查和变化工具计数。
4. 最近检查、延迟、连续失败和错误摘要。
5. 工具列表与审批策略。
6. 最近调用摘要和观测台深链。
7. **测试连接**、**刷新能力**、**编辑配置** 和 **删除**。

测试和刷新期间使用任务行展示真实步骤。用户关闭 Drawer 后任务继续运行，
页面卡片状态必须同步更新。

### 14.4 工具审查

工具审查必须支持单项和批量操作。

- 默认按待审查、风险和名称排序。
- 展示 remote name 与 effective name。
- 展示描述、输入 Schema 摘要和 Server 注解。
- 允许配置启用状态和审批策略。
- 高风险工具使用 Beautiful UI `approval-card` 交互确认。
- Schema 变化时展示字段 diff，不允许一键无提示恢复启用。

---

## 15. 对话与 Agent 接入

能力中心的启停必须对真实对话生效。前端列表显示成功不等同于 Agent 已经可以
调用工具。

### 15.1 默认能力

对话默认包含：

- 租户内 activation mode 为 `always` 的已启用 Skill。
- activation mode 为 `always` 且已批准的 MCP Tools。
- 内置基础工具。

状态为 Error、Changed、Pending 或 Disabled 的 MCP Tool 不得进入默认集合。

### 15.2 会话能力选择器

后续在对话 ContextPanel 增加 **能力** 区域，展示当前会话选中的 Skills 和 MCP
Tools。首期可以只显示只读摘要，M2 完成选择和持久化。

选择器必须：

- 按 Skill 和 MCP 分组。
- 显示能力来源和状态。
- 禁止选择无权、待审查和异常能力。
- 展示本次已选工具数与上限。
- 将选择结果发送为 `capabilities`，不只发送 Skill 名称。

### 15.3 Prompt 与工具边界

Skill 正文只在 Skill 被解析进入本次能力集时注入。MCP Tool 的描述通过工具
Schema 提供，不自动转为 system prompt。

MCP Prompts 后续必须由用户显式选择，不得因为 Server 已启用就自动注入。

---

## 16. 可观测性与审计

MCP 的连接、发现和调用必须接入现有本地观测体系，使用户能够从能力卡片跳转
到真实 Trace，而不是只看到最后一个错误字符串。

### 16.1 Trace 字段

每个 MCP Tool span 增加以下属性：

```text
capability.kind = mcp_tool
mcp.server_id
mcp.server_name
mcp.transport
mcp.tool.remote_name
mcp.tool.effective_name
mcp.protocol_version
mcp.session_mode
mcp.attempt
mcp.result_content_types
```

禁止记录认证 Header、token、Secret 引用的值和未脱敏工具输入。

### 16.2 指标

至少采集以下指标：

- MCP Server Ready、Degraded 和 Error 数量。
- 连接测试成功率和 P95 延迟。
- 能力发现成功率和发现工具数。
- 每个 Server/Tool 的调用量、错误率和 P95 延迟。
- 超时、熔断、审批拒绝和策略拒绝次数。
- 工具输出截断次数和字节数。

### 16.3 审计事件

能力中心必须记录以下事件：

```text
skill.import
skill.enable
skill.disable
skill.delete
mcp.server.create
mcp.server.update
mcp.server.test
mcp.server.discover
mcp.server.enable
mcp.server.disable
mcp.server.delete
mcp.tool.review
mcp.tool.enable
mcp.tool.disable
mcp.tool.approval_allow
mcp.tool.approval_deny
```

审计 detail 可以记录字段名和策略变化，不能记录 Secret 值。

---

## 17. 可靠性与性能

MCP 属于外部依赖，运行时必须允许部分失败和稳定降级。

### 17.1 超时与重试

首期默认值如下：

```text
MCP_CONNECT_TIMEOUT_SECONDS = 10
MCP_DISCOVERY_TIMEOUT_SECONDS = 20
MCP_TOOL_TIMEOUT_SECONDS = 60
MCP_MAX_RETRIES = 1
MCP_MAX_OUTPUT_BYTES = 1048576
```

写入或破坏性工具默认不自动重试，除非工具明确支持幂等且平台策略允许。

### 17.2 故障隔离

实现必须逐 Server 发现和解析能力。

- 一个 Server 失败不取消其他 Server 的发现。
- 一个工具 Schema 无效不丢弃同 Server 的其他工具。
- MCP 失败不影响内置工具和 Skill Prompt。
- 熔断只作用于 Server 或工具，不关闭全局 MCP 功能。
- 停用 Server 后新请求立即不可见；进行中的调用按取消策略收敛。

### 17.3 缓存

ToolCatalog 可以按 `(tenant_id, capability_revision)` 缓存元数据。Secret、SDK
Session 和请求态审批结果不得进入共享缓存。

缓存失效必须发生在：

- Server 启停或配置变化。
- 能力发现 revision 变化。
- 工具审查或审批策略变化。
- Skill 导入、启停、覆盖或删除。

---

## 18. 配置项

配置必须集中在 `backend/app/config.py`，不得在 Provider 中散落读取环境变量。

```python
# Capability hub
CAPABILITY_HUB_ENABLED: bool = True
CAPABILITY_MAX_TOOLS_PER_RUN: int = 32

# MCP
MCP_ENABLED: bool = False
MCP_REMOTE_HTTP_ENABLED: bool = True
MCP_STDIO_ENABLED: bool = False
MCP_LEGACY_SSE_ENABLED: bool = False
MCP_AUTO_HEALTHCHECK_ENABLED: bool = True
MCP_HEALTHCHECK_INTERVAL_SECONDS: int = 60
MCP_CONNECT_TIMEOUT_SECONDS: float = 10.0
MCP_DISCOVERY_TIMEOUT_SECONDS: float = 20.0
MCP_TOOL_TIMEOUT_SECONDS: float = 60.0
MCP_MAX_RETRIES: int = 1
MCP_MAX_OUTPUT_BYTES: int = 1024 * 1024
MCP_ALLOWED_HOSTS: str = ""
MCP_ALLOWED_STDIO_COMMANDS: str = ""
```

生产环境开启 `MCP_ENABLED` 时必须校验 SecretStore、出站网络策略和租户鉴权
已经配置。

---

## 19. 数据迁移与兼容

迁移必须保留现有 Skill、API 和会话行为，同时逐步引入租户 binding 和动态
工具解析。

### 19.1 Skill 数据回填

迁移按以下顺序执行：

1. 为 `skills` 增加新字段，不删除 `enabled`。
2. 将当前内置 Skill 标记为 `source=builtin`、`owner_tenant_id=null`。
3. 将当前外部 Skill 归属 `DEFAULT_TENANT`。
4. 为 `DEFAULT_TENANT` 创建 `skill_bindings`。
5. 使用旧 `enabled` 回填 `activation_mode`。
6. 切换读路径为 binding 优先、旧字段兜底。
7. 稳定一个版本后停止写入旧 `enabled`。

### 19.2 文件迁移

外部 Skill 最终目录调整为租户分层结构。迁移必须先复制并校验 hash，再原子
切换数据库路径；不得直接移动后删除源目录。

```text
skills/_tenants/{tenant_key}/{skill_name}/
```

内置 Skill 目录保持不变。

### 19.3 API 兼容

旧前端在迁移期间必须继续工作。

- `/skills` 路径和现有字段继续返回。
- `skills` 对话请求字段继续支持。
- `source=builtin|external` 语义保持不变。
- MCP API 由 `MCP_ENABLED` 控制；关闭时返回明确能力不可用响应。
- 新前端遇到旧后端时隐藏 MCP Tab，并保留 Skills 页面。

### 19.4 Graph 迁移

动态 Tool Dispatcher 在功能开关下灰度启用。

- 第一阶段保留现有 ToolNode 作为回退。
- 第二阶段让 MCP Tools 只走动态 Dispatcher。
- 第三阶段让 Skill 脚本工具也走统一 Dispatcher。
- 验证稳定后移除 Skill 启停触发的全局 Graph 重建。

---

## 20. 实现文件清单

本节给出预期文件边界。实现可以在不改变职责的前提下调整文件拆分。

### 20.1 后端新增

```text
backend/app/capabilities/__init__.py
backend/app/capabilities/contracts.py
backend/app/capabilities/catalog.py
backend/app/capabilities/resolver.py
backend/app/capabilities/policy.py
backend/app/capabilities/dispatcher.py

backend/app/mcp/__init__.py
backend/app/mcp/models.py
backend/app/mcp/adapter.py
backend/app/mcp/manager.py
backend/app/mcp/health.py
backend/app/mcp/secrets.py

backend/app/api/router_capabilities.py
backend/app/api/router_mcp.py
```

### 20.2 后端修改

```text
backend/requirements.txt
backend/.env.example
backend/app/config.py
backend/app/main.py
backend/app/api/schemas.py
backend/app/api/router_chat.py
backend/app/api/router_skills.py
backend/app/storage/models.py
backend/app/storage/repositories.py
backend/app/storage/db.py
backend/app/tools/registry.py
backend/app/harness/context.py
backend/app/harness/graph_builder.py
backend/app/agents/base_agent.py
```

原 `backend/app/tools/mcp_adapter.py` 在迁移期转发到新 adapter，稳定后删除，
避免同时维护两套实现。

### 20.3 前端新增

```text
frontend/src/components/capabilities/CapabilityMetricStrip.tsx
frontend/src/components/capabilities/CapabilityTabs.tsx
frontend/src/components/capabilities/CapabilityCardGrid.tsx
frontend/src/components/capabilities/SkillCard.tsx
frontend/src/components/capabilities/McpServerCard.tsx
frontend/src/components/capabilities/SkillInspector.tsx
frontend/src/components/capabilities/McpServerInspector.tsx
frontend/src/components/capabilities/McpServerCreateModal.tsx
frontend/src/components/capabilities/McpConnectionTasks.tsx
frontend/src/components/capabilities/McpToolReview.tsx
frontend/src/components/capabilities/CapabilityApproval.tsx
frontend/src/api/capabilities.ts
```

从 Beautiful UI 复制实质性代码时，还必须加入对应 LICENSE 文件或项目第三方
许可清单。

### 20.4 前端修改

```text
frontend/src/pages/SkillsPage.tsx
frontend/src/types/api.ts
frontend/src/App.tsx
frontend/src/components/layout/Sidebar.tsx
frontend/src/components/layout/ContextPanel.tsx
frontend/src/styles/global.css
```

样式优先扩展现有语义 token，不新增完整 CSS framework。

### 20.5 测试新增

```text
backend/tests/test_capability_resolver.py
backend/tests/test_capability_tenant_isolation.py
backend/tests/test_mcp_adapter.py
backend/tests/test_mcp_manager.py
backend/tests/test_mcp_api.py
backend/tests/test_mcp_security.py
backend/tests/test_mcp_graph_tool_loop.py
backend/tests/fixtures/mcp_test_server.py

frontend/src/components/capabilities/*.test.tsx
frontend/e2e/capability-hub.spec.ts
```

---

## 21. 测试计划

测试必须覆盖定义管理、真实协议调用、Agent 运行、租户隔离和前端交互。只 Mock
repository 不能证明 MCP 工具已经进入真实 LangGraph 事件链路。

### 21.1 单元测试

单元测试至少覆盖：

- MCP 配置转换和 transport 校验。
- 工具名前缀、截断、冲突和稳定 hash。
- 工具 Schema 适配与不兼容错误。
- 能力新增、删除和 Schema 变化 diff。
- Resolver 的 always、explicit、disabled 和上限规则。
- 风险与审批策略。
- Secret 响应脱敏。
- Skill binding 兼容读取。

### 21.2 数据库与 API 测试

数据库和 API 测试至少覆盖：

- 两个租户可以创建同名 Server，但不能互相查询或修改。
- 新 Server 和新工具默认停用。
- 连接失败不覆盖最近成功能力快照。
- 更新 Endpoint 后状态回到 Unknown，并要求重新发现。
- 删除 Server 同步移除绑定和 Secret 引用。
- 所有响应不包含 token、Header value 和密钥。
- 旧 `/skills` API 行为保持兼容。

### 21.3 MCP 集成测试

测试必须启动可控的真实 MCP Server，覆盖：

- Streamable HTTP 初始化。
- Tools list 和 Tool call。
- 文本和结构化结果转换。
- 工具返回业务错误。
- transport 断开、超时和重连。
- 单 Server 故障不影响另一个健康 Server。
- 工具新增、删除和 Schema 变化。

### 21.4 LangGraph 回归测试

至少提供一条真实事件集成测试：

1. 启动测试 MCP Server。
2. 创建租户 MCP 配置并发现工具。
3. 审批并启用测试工具。
4. 构建真实 Graph Runtime。
5. 让测试 LLM 发起 MCP tool call。
6. 验证 ToolMessage、最终回答和 Trace span。
7. 停用工具并验证下一次请求拒绝调用。

### 21.5 前端组件测试

前端测试至少覆盖：

- Tab 键盘导航和 URL 同步。
- Skill/MCP 卡片不同字段与状态。
- 搜索、防抖、筛选和清除筛选。
- Switch 成功、失败和回滚。
- 创建向导字段校验和一次性 Secret 行为。
- 连接任务的 running、failed、retry 和 success。
- 工具批量审查和高风险确认。
- Drawer 焦点管理和窄屏布局。
- `prefers-reduced-motion` 下内容立即可见。

### 21.6 浏览器端到端测试

浏览器 E2E 必须使用真实前后端和测试 MCP Server，覆盖：

- 添加 Server、测试、发现、选择工具、保存和启用。
- 在能力中心看到 Ready 状态和工具数。
- 在对话中真实调用 MCP Tool。
- 从卡片跳转到对应观测 Trace。
- 模拟 Server 离线并看到 Degraded/Error 与恢复动作。
- 停用和删除后工具立即不可用。

DOM 检查或 Mock API 页面截图不能单独标记为 MCP E2E 通过。

---

## 22. 里程碑与交付顺序

里程碑按可验证的纵向切片组织，每个阶段都必须有 API、测试和运行证据。

| 里程碑 | 范围 | 退出条件 |
|---|---|---|
| M0 基线冻结 | 当前 Skill 回归、Feature Flag、Schema 评审 | Skill 测试和旧 API 通过 |
| M1 MCP 控制面 | 表、Secret 引用、单 Server test/discover API | 真实 MCP Server 发现成功 |
| M2 动态运行时 | Resolver、Dispatcher、Tool call、租户隔离 | 真实 LangGraph MCP 调用通过 |
| M3 能力中心 UI | 卡片页、Tab、添加向导、检视器、工具审查 | 前端 build 和主要交互通过 |
| M4 治理与观测 | 审批、健康、熔断、Trace、指标和审计 | 故障隔离和安全测试通过 |
| M5 完整 MCP | OAuth、Resources、Prompts、可选 stdio | 单独 Spec 和安全评审通过 |

M1 到 M4 共同构成首个生产可用版本。只完成 M1 不能宣称“已支持 MCP 技能库”。

---

## 23. 验收标准

以下条件全部满足后，CAP-HUB 首期才能标记完成。

### 23.1 产品语义

- 页面明确区分 Skill 和 MCP Server。
- 用户能理解启用 Server、启用 Tool 和审批 Tool 的差异。
- 新外部 Skill、新 MCP Server 和新发现 Tool 默认停用。
- Error、Degraded 和 Pending 都提供下一步操作。
- 页面不把 MCP Tools、Resources 和 Prompts 混成同一种能力。

### 23.2 后端与运行时

- 真实 Streamable HTTP MCP Server 可以添加、发现和调用。
- Agent 只看到当前租户和请求允许的工具。
- MCP 启停不要求重建全局 Graph。
- 同名工具生成稳定且不冲突的 effective name。
- 一个 MCP Server 故障不影响其他 Server、Skill 和内置工具。
- 服务重启后能恢复 Server 配置、能力快照和启用状态。

### 23.3 安全

- 跨租户 Server、能力和 Secret 访问被拒绝。
- API、日志、审计和 Trace 不包含认证值。
- Endpoint 通过 SSRF 和出站网络策略校验。
- 高风险工具在调用前执行明确审批。
- 工具输出执行大小限制、脱敏和外部内容标记。
- M1 不能通过配置启动任意 `stdio` command。

### 23.4 UI 与交互

- `/skills` 呈现统一能力中心和 Skills/MCP 水平 Tab。
- Skill 和 MCP 都使用紧凑小卡片网格。
- 页面视觉与观测台、知识库保持同一工作台语言。
- Beautiful UI 只用于真实搜索、任务、工具和审批状态。
- 卡片、筛选、Drawer 和 Modal 在桌面、平板和窄屏均可用。
- 键盘、焦点、状态播报、对比度和减少动态效果满足本规格。
- 加载、空态、筛选无结果、局部失败和全局失败均有明确界面。

### 23.5 工程质量

- 后端定向测试、完整测试和前端 build 通过。
- 真实 MCP 集成测试和真实 LangGraph 工具调用测试通过。
- 浏览器 E2E 覆盖添加、启用、调用、故障和停用流程。
- Beautiful UI 代码和样式引用保留 MIT License。
- 文档、API Schema 和前端 TypeScript 类型保持一致。

---

## 24. 风险与对策

本节记录最可能导致返工或安全问题的风险。

| 风险 | 影响 | 对策 |
|---|---|---|
| 全局注册全部 MCP 工具 | 跨租户暴露、工具膨胀 | 按请求 Resolver 与动态 Dispatcher |
| 直接复用 SkillManager 管 MCP | 生命周期和领域模型混乱 | 独立 McpProvider，共用控制面合同 |
| 明文保存 Header/token | 凭据泄露 | Secret 引用、响应脱敏、日志过滤 |
| 开放任意 stdio command | 后端 RCE | M1 禁用，后续 allowlist 与沙箱 |
| 批量发现任一失败全量失败 | 健康 Server 不可用 | 逐 Server 发现和错误隔离 |
| 相信 Server 风险注解 | 高风险工具误放行 | 平台风险判定和人工审查 |
| 每次启停重建 Graph | 并发竞态和请求抖动 | 动态工具执行与 revision 缓存 |
| 前端只有配置表单 | 无法理解发现和审查 | 四步向导、任务状态和工具审查 |
| 卡片视觉过度装饰 | 信息密度下降 | 单层小卡片、轻边框和克制动效 |
| 复制完整 Beautiful UI CSS | 全局样式冲突 | 只映射必要 token、规则和 keyframe |

---

## 25. 参考依据

本规格基于当前项目源码、现有 Skill 导入与资源读取行为、LangChain MCP 适配器
和 MCP 协议的公开语义制定。

- [LangChain MCP integration](https://docs.langchain.com/oss/python/langchain/mcp)
- [LangChain MCP adapters](https://github.com/langchain-ai/langchain-mcp-adapters)
- [MCP Server primitives](https://modelcontextprotocol.io/specification/2025-11-25/server/index)
- [MCP transports](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)
- [MCP authorization](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)

---

## 26. 后续步骤

规格评审通过后，按以下顺序进入实施。

1. 冻结 M1 范围和 MCP Streamable HTTP 测试 Server。
2. 先实现数据模型、Secret 引用、单 Server test/discover 和集成测试。
3. 实现 CapabilityResolver 与动态工具调用，验证真实 LangGraph 事件。
4. 按本规格实现能力中心卡片页、MCP 添加向导和工具审查。
5. 接入审批、健康、观测和审计，并完成浏览器端到端验证。
6. 将 OAuth、Resources、Prompts 和 `stdio` 另行拆分为 M5 规格。
