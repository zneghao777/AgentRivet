# AgentHub（Agent 管理）需求调研报告

> 版本：v0.1（调研阶段，非规格）
> 日期：2026-08-28
> 状态：调研 → 待评审
> 关联文档：
> [MCP_SKILL_ECOSYSTEM_SPEC.md](./MCP_SKILL_ECOSYSTEM_SPEC.md)、
> [MCP_SKILL_ECOSYSTEM_RESEARCH.md](./MCP_SKILL_ECOSYSTEM_RESEARCH.md)、
> [agent-loop-design-report.md](./agent-loop-design-report.md)、
> [SKILL_GUIDE.md](./SKILL_GUIDE.md)、[SPEC.md](./SPEC.md)

本报告回答三个问题：**现状是什么、业界怎么做的、本项目要建什么**。
它不是最终规格，而是把"多 Agent 模式"落地前必须理清的需求边界，作为后续
设计评审的输入。

---

## 1. 结论摘要（TL;DR）

1. **现状是"单 Agent + 全局能力"，多 Agent 只是一个硬编码演示骨架**。
   `MULTI_AGENT_ENABLED` 默认关闭；开启后也只会无脑路由到 `demo_subagent`，
   子 Agent 节点不调用 LLM、不携带独立 system prompt / Skill / 工具集。
2. **当前所有对话把"全部已启用 Skill"无条件注入**，没有按 Agent 隔离能力；
   MCP 工具虽然有"审批 + 启停"，但也是租户级全局集合，不与某个 Agent 绑定。
3. **业界成熟做法是把 Agent 当成"一等的可配置实体"**（Claude Code subagents /
   OpenAI Assistants / Dify 应用 / Coze Bots）：每个 Agent = 系统提示词 + 模型 +
   能力白名单（Skill/工具/MCP）+ 记忆开关 + 可见性。多 Agent 编排再决定谁出场。
4. **因此 AgentHub 应优先解决"定义与治理 Agent"，而非"立即实现复杂编排"**。
   先把 Agent 做成数据模型 + CRUD + 运行时按 Agent 解析能力集；Supervisor 路由
   作为消费方接入，而不是反过来先搭路由框架再补定义。

---

## 2. 现状盘点（基于真实代码）

### 2.1 运行链路现状

一条对话的完整链路（`router_chat.py` → `harness.app_runtime` → LangGraph）：

```text
POST /chat/{session_id}/messages/stream
  → skill_names = body.skills（前端把"全部已启用 skill"传来）
  → knowledge_bases / capabilities（MCP 工具 id 白名单）
  → set_tool_context(ToolContext(...))      // 请求级，不进 checkpoint
  → harness_app.runtime.astream(thread_id, message, tenant, user, skill_names, ...)
      → supervisor.graph（MULTI_AGENT_ENABLED=false 时回退主 Agent 图）
          → agent_node: build_system_prompt(skill_names) + bind_tools(_tools)
```

### 2.2 与 Agent 直接相关的模块

| 模块 | 文件 | 现状 | 问题 |
|---|---|---|---|
| 主 Agent 提示词 | `agents/base_agent.py` | 固定 `SYSTEM_PROMPT_TEMPLATE` + Skill 描述/正文 + 记忆 | 提示词写死，无法按 Agent 定制 |
| Agent 注册中心 | `agents/registry.py` | `AgentEntry(name, description, graph)`，内存 dict | 仅 3 个字段，无系统提示词/能力/模型/状态 |
| 示例子 Agent | `agents/demo_subagent.py` | 硬编码，不调 LLM | 无法复用主 Agent 机制，无法配置 |
| Supervisor | `harness/supervisor.py` | 硬编码 `route → demo_subagent` | 无真实路由逻辑，无按 Agent 的 tool/skill 隔离 |
| 主图构建 | `harness/graph_builder.py` | ReAct 循环 + HITL | 工具集 = 全局注册表 + 请求态 MCP，与 Agent 无关 |
| 状态 | `harness/state.py` | `skill_loaded` 全局字段 | 无 `agent_id`，无法在 checkpoint 里追踪哪个 Agent 在跑 |
| Skill 管理 | `skills/manager.py` + `skills/models.py` | 全局发现 + 全局 enabled | **没有"某 Skill 归某 Agent 用"的绑定** |
| MCP 管理 | `mcp/manager.py` | 租户级 Server + 能力审批 | **能力集合是租户级，未绑定 Agent** |
| 工具注册 | `tools/registry.py` | 进程级全局单例 | 与 Agent 无关 |
| 前端对话 | `ChatWindow.tsx` | `skills.filter(enabled)` 全量发送 | 无"选择 Agent"入口 |
| 前端能力中心 | `SkillsPage.tsx` | Skills + MCP 两个 Tab | 无 Agent Tab |

### 2.3 关键缺口（为什么需要 AgentHub）

1. **没有 Agent 定义实体**：DB 里只有 `sessions/messages/skills/mcp_servers/...`，
   没有 `agents` 表，无法持久化"某 Agent 是什么"。
2. **系统提示词无法定制**：`build_system_prompt` 只拼 Skill 内容，不读 Agent 配置。
3. **能力无归属**：Skill/MCP/内置工具全部是"租户全局可用"，没法说"这个 Agent
   才能用 knowledge_search，那个 Agent 不能用"。
4. **模型无法按 Agent 指定**：模型走请求体 `model` 字段，没有 Agent 级默认模型。
5. **无 Agent 级记忆/知识库绑定**：记忆是用户级、知识库白名单在会话级，
   都未下沉到 Agent。
6. **Supervisor 无真实路由**：子 Agent 不能真正干活，无法扩展成工作流。

---

## 3. 业界对标（为什么这么设计）

### 3.1 Claude Code 的 Subagents（最贴近本项目"Agent=提示词+能力"）

- 每个 subagent 是一个 `.md` 文件，frontmatter 声明 `name`、`description`、
  `tools`（允许/禁止的工具），正文即系统提示词。
- 主 agent 通过 `description` 决定何时 `Task` 调用子 agent；子 agent 有自己独立的
  system prompt 和 tool 白名单，与主 agent 上下文隔离。
- **启示**：Agent 的核心属性 = `名称 + 一句话描述（路由依据）+ 系统提示词 +
  工具白名单`。这与本项目 Skill 的 frontmatter 模式天然一致，可以复用 `loader`
  思路。

### 3.2 OpenAI Assistants API / GPTs

- Assistant = `instructions`（系统提示词）+ `model` + `tools`
  （code_interpreter / retrieval / function）+ `tool_resources`。
- **启示**：Agent 是"定义即服务"，每个 Agent 持久化，会话挂到某个 Assistant 上
  就复用其配置。这对应本项目"会话绑定 Agent"的模型。

### 3.3 Dify / Coze / 扣子（企业级 Agent 平台）

- 应用/Bot 中心化管理：列表、创建向导、编排画布、发布、版本。
- 每个应用可绑定：提示词、模型、知识库（RAG）、工具/插件、记忆、变量。
- **启示**：需要一个"Agent 管理页"，但首期不必做可视化画布。可先做表单式
  创建/编辑（系统提示词 + 能力勾选 + 模型选择），画布编排放后续里程碑。

### 3.4 LangGraph / CrewAI（编排框架）

- Supervisor 路由：LLM 判断派给哪个 worker。
- CrewAI：`Agent(role, goal, backstory)` + `Task` + `Process`。
- **启示**：编排形态有多种（Supervisor / 顺序 / 层级 / handoff），
  但**所有形态都建立在"每个 Agent 有明确定义"之上**。先定义后编排。

### 3.5 业界共性结论

> **Agent = 一段可复用的身份定义（system prompt） + 能力边界（工具/Skill/MCP）
> + 运行参数（模型/记忆/知识库） + 可见性（谁能用/能否被路由）。**
> 多 Agent 编排是"消费这些定义"的一层，不应与定义层耦合。

---

## 4. 需求定义

### 4.1 核心用户故事

- 作为用户，我可以在"Agent 管理"页**创建/编辑/停用/删除**一个 Agent。
- 我可以为每个 Agent 设置**系统提示词**（决定它的角色和任务边界）。
- 我可以为每个 Agent **勾选可用的 Skill**，只让该 Agent 使用选中的技能。
- 我可以为每个 Agent **勾选可用的 MCP 工具**（复用现有能力审批结果）。
- 我可以为每个 Agent 指定**模型**（默认模型，可被请求体覆盖）。
- 我可以选择该 Agent **是否启用长期记忆 / 绑定哪些知识库**。
- 在对话中我可以**选择用哪个 Agent** 来回复，会话绑定该 Agent。
- 多 Agent 开启时，Supervisor 能根据 Agent 的定义**正确路由并注入对应能力**。

### 4.2 功能范围（首期 MVP）

| 能力 | 首期 | 说明 |
|---|---|---|
| Agent CRUD（增删改查/启停） | ✅ | 表单式，不做画布 |
| 系统提示词编辑 | ✅ | 覆盖默认模板 |
| Skill 白名单绑定 | ✅ | 复用现有 Skill 数据 |
| MCP 工具白名单绑定 | ✅ | 复用现有能力审批 |
| 内置工具白名单 | ✅ | 内置工具分组可选 |
| 模型默认值 | ✅ | Agent 级默认 + 请求覆盖 |
| 知识库绑定 | 🟡 可后置 | 建议首期做，复用 RAG 白名单 |
| 记忆开关 | ✅ | 复用 `MEMORY_ENABLED` 体系 |
| 会话绑定 Agent | ✅ | Session 增加 `agent_id` |
| 运行时按 Agent 解析能力 | ✅ | CapabilityResolver 升级 |
| Supervisor 真实路由 | 🟡 后置 | 首期单 Agent 即可用，路由 M2 |
| 可视化编排画布 | ❌ | 后续里程碑 |
| Agent 版本/发布/分享 | ❌ | 后续里程碑 |
| 权限/角色（谁能改某 Agent） | 🟡 | 先按租户隔离，细粒度后置 |

### 4.3 非目标（首期不做）

- 不做可视化工作流/画布编排。
- 不做 Agent 之间的 message passing / 多轮协作协议（先单 Agent + Supervisor 路由）。
- 不做跨租户 Agent 共享/市场。
- 不做 Agent 的 A/B 测试 / 版本发布流水线。
- 不为 Agent 脚本引入独立沙箱（Skill 脚本沙箱问题沿用现有边界）。

---

## 5. 建议的数据模型（草案）

新增 `agents` 表（租户级），可选项：

```text
agents
├── id            UUID PK
├── tenant_id     VARCHAR(64)      # 租户隔离
├── name          VARCHAR(128)     # (tenant_id, name) 唯一
├── description   TEXT             # 一句话描述，供路由/选择依据
├── system_prompt TEXT             # 用户自定义系统提示词（可空=用默认模板）
├── model         VARCHAR(128)     # 默认模型，可空=用全局 LLM_MODEL
├── model_base_url VARCHAR(2048)   # 可选
├── memory_enabled BOOLEAN default true
├── status        VARCHAR(16) default 'active'   # active/disabled/archived
├── created_by    VARCHAR(64)
├── created_at / updated_at
```

能力绑定（一个 Agent 可绑定多个，多对多）：

```text
agent_skill_bindings    (agent_id, skill_id, activation_mode)
agent_mcp_bindings      (agent_id, mcp_capability_id, approval_policy)
agent_tool_bindings     (agent_id, tool_name, allowed bool)   # 内置工具白名单
agent_kb_bindings       (agent_id, kb_id)                      # 知识库（可选）
```

> 复用原则：**Skill/MCP 的表结构保持现状**，Agent 只新增"绑定表"，不改造 Skill /
> MCP 本身。这样 AgentHub 是"聚合 + 授权层"，不侵入现有能力中心。

`sessions` 表增加 `agent_id`（可空），旧会话无 agent_id 时回退当前"全部已启用
Skill + 租户能力"的默认行为，保证向后兼容。

---

## 6. 建议的架构方案

### 6.1 分层：定义层 / 绑定层 / 运行时解析层

```text
┌────────────────────── AgentHub 控制面 ──────────────────────┐
│  Agent CRUD API/UI     绑定管理    模型默认    可见性/状态   │
└───────────┬─────────────────────────────────────────────────┘
            ▼
   AgentProvider（agents/manager.py）
        │ 读取 agents + 三张绑定表
        ▼
   CapabilityResolver（升级）
        │ 输入：tenant_id + user_id + session_id + agent_id + requested
        ▼
   ResolvedCapabilitySet
        ├─ system_prompt（Agent 自定 or 默认模板 + 绑定 Skill）
        ├─ tools（内置白名单 + 绑定 Skill 脚本工具 + 绑定 MCP 工具）
        ├─ model（Agent 默认模型 or 请求覆盖）
        └─ memory / kb 开关
        │
        ▼
   Supervisor / 主 Agent（消费解析结果）
```

### 6.2 运行时如何接（关键决策）

1. **Agent 在请求进入时解析**：`router_chat.py` 从 `body.agent_id`（或 session 绑定）
   读取 agent 定义，调用 Resolver 得到 `ResolvedCapabilitySet`，写入请求级
   `ToolContext`（**不进 checkpoint**，延续 `context.py` 的教训）。
2. **system prompt 生成下沉到 Agent**：`base_agent.build_system_prompt` 增加
   `agent_system_prompt` 参数；有自定义则优先，否则默认模板 + 绑定 Skill。
3. **能力解析复用现成组件**：
   - Skill 脚本工具：`skill_manager` 已能把 `scripts/*` 注册为 `skill_{name}__*` 工具，
     Resolver 只需按 Agent 绑定的 skill 过滤工具名。
   - MCP 工具：`mcp_manager.resolve_tools` 已有 `requested_capability_ids` 参数，
     Resolver 传入 Agent 绑定的 capability ids 即可（天然兼容）。
   - 内置工具：按 `agent_tool_bindings` 过滤 `tool_registry`。
4. **会话绑定**：Session 增 `agent_id`，创建/切换会话时设置；对话时若请求体未传
   `agent_id` 则读 session。这是 Dify/OpenAI 的标准做法。
5. **Supervisor 消费**：M2 再把 Supervisor 的 `_supervisor` 从"硬编码 demo"改为
   "根据各 Agent 的 description + 用户意图路由"，把 `route` 目标 Agent 的定义注入
   该节点。**首期即使单 Agent，AgentHub 也应可用**。

### 6.3 需要改造的文件（预估）

```text
backend/app/agents/manager.py       新增：Agent 定义 CRUD + 绑定读写
backend/app/agents/models.py        新增：AgentModel（内存/DTO）
backend/app/storage/models.py       新增：Agent + 三张绑定表 + sessions.agent_id
backend/app/storage/repositories.py 新增：agent repository
backend/app/api/router_agents.py    新增：/agents CRUD + 绑定管理 API
backend/app/api/router_chat.py      改：读 agent_id → 解析能力 → 注入
backend/app/api/schemas.py          改：Agent 请求/响应 + MessageRequest.agent_id
backend/app/capabilities/resolver.py 改：输入 agent_id，输出 ResolvedCapabilitySet
backend/app/agents/base_agent.py    改：build_system_prompt 支持 Agent 自定义
backend/app/harness/supervisor.py   改（M2）：真实路由

frontend/src/pages/AgentHubPage.tsx 新增：Agent 列表/创建/编辑/详情
frontend/src/components/agents/*.tsx 新增：AgentCard / AgentEditor / 能力绑定
frontend/src/types/api.ts           改：Agent 类型
frontend/src/App.tsx / Sidebar.tsx  改：导航入口
frontend/src/components/chat/ChatWindow.tsx 改：Agent 选择器
```

---

## 7. 待确认的关键决策点（需要产品/架构拍板）

以下问题会在正式规格前需要明确，先列出来供讨论：

1. **Agent 与 Session 的关系**：Session 绑定一个 Agent 后，中途能否换 Agent？
   换 Agent 是否视为新上下文（建议：换 Agent = 清空会话或新建会话，避免能力错乱）。
2. **"全部已启用 Skill" 的兼容**：旧对话无 agent_id 时，是维持"全量 Skill"还是
   收敛到默认 Agent？建议默认 Agent = "平台默认配置"，保持兼容。
3. **Agent 系统提示词与 Skill 的关系**：Agent 自定义提示词是**完全覆盖**默认模板，
   还是**叠加**（Agent 身份 + Skill 指令）？建议叠加——Skill 指令必须始终保留，
   否则 Agent 会"看不见"已绑定 Skill 的能力边界。
4. **内置工具的粒度**：内置工具（knowledge_search / memory_search / 计算器…）是否
   全部可勾选？是否允许"某 Agent 禁用 knowledge_search"？建议首期只按分组开关。
5. **多 Agent 路由的默认模式**：首期要不要暴露给用户手选 Agent？还是只靠
   Supervisor 自动路由？建议首期"会话手动绑定 Agent"，自动路由放 M2。
6. **MCP 工具绑定粒度**：是绑定到"整个 Server"还是"单个 Capability"？现有
   `mcp_capabilities` 已到工具级，建议首期按工具级绑定，但 UI 可按 Server 批量勾选。
7. **Agent 是否可被 Supervisor 路由**：需要 `routable` 字段区分"用户可选"和
   "仅供子 Agent 内部调用"？建议加。

---

## 8. 风险与对策

| 风险 | 影响 | 对策 |
|---|---|---|
| 把 Agent 做成又一套全局配置，与 Skill 能力中心割裂 | 用户混乱 | AgentHub 只做"绑定/聚合"，复用现有 Skill/MCP 数据 |
| system prompt 被自定义覆盖导致 Agent 失去能力边界 | Agent 乱调工具 | 叠加注入：Agent 提示词 + 必选 Skill 指令始终保留 |
| 会话中途换 Agent 造成上下文/能力错乱 | 回答错误 | 换 Agent 视为新上下文或限制为不可切换 |
| 与现有"全量 Skill"默认行为冲突 | 回归 | 默认 Agent 保持兼容，无 agent_id 走旧逻辑 |
| 能力解析重复实现 | 维护成本 | 全部走统一 CapabilityResolver，禁止分散拼接 |
| Supervisor 先行、定义后补 | 骨架空心化 | 严格先 AgentHub 后编排 |

---

## 9. 建议的交付顺序

| 里程碑 | 范围 | 退出条件 |
|---|---|---|
| H0 基线冻结 | 确认第 7 节决策点、Schema 评审 | 决策记录一致 |
| H1 Agent 定义 | `agents` 表 + CRUD + 绑定表 + API | Agent 可创建/编辑/启停 |
| H2 运行时解析 | Resolver 按 agent_id 解析能力集 + 会话绑定 | 单 Agent 对话能按 Agent 注入 Skill/工具/提示词 |
| H3 AgentHub UI | Agent 列表/创建向导/编辑/能力绑定 | 前端可完整管理 Agent 并选择 |
| H4 观测与审计 | Agent 维度 Trace/指标/审计 | 可看到"哪个 Agent 用了什么能力" |
| M2 编排 | Supervisor 真实路由 | 多 Agent 自动分工可用 |

> H0–H4 构成首个可用的 AgentHub；M2 编排复用现有 Supervisor 骨架，独立交付。

---

## 10. 参考依据

- 本项目真实代码：`agents/*`、`harness/supervisor.py`、`skills/manager.py`、
  `mcp/manager.py`、`tools/registry.py`、`api/router_chat.py`。
- [MCP_SKILL_ECOSYSTEM_SPEC.md](./MCP_SKILL_ECOSYSTEM_SPEC.md)：统一能力中心的
  租户隔离、能力绑定、审批策略、Resolver/Dispatcher 设计（AgentHub 直接复用其
  `CapabilityContext.agent_id` 与绑定语义）。
- [agent-loop-design-report.md](./agent-loop-design-report.md)：自由度⑥多 Agent
  编排形态（Supervisor / 子 Agent 即工具）与"先定义后编排"结论。
- 业界：Claude Code Subagents、OpenAI Assistants/GPTs、Dify/Coze 应用模型、
  LangGraph Supervisor、CrewAI Agent 定义。
