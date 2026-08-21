# 技能库体系演进调研：Skill + MCP 统一能力仓库

> 调研人：CodeBuddy
> 日期：2026-08-21
> 状态：调研分析（未实施）

## 1. 当前技能库体系现状

### 1.1 整体架构

当前项目已具备一套**完整的 Skill 生命周期管理**体系，前端有一个集中的「技能库」页面：

```
skills/                      # 外部 Skill 仓库（业务团队托管扩展）
├── agent-mbti/              # SKILL.md + references/
├── skill_greeting/          # 简单 Skill
backend/app/skills/          # Skill 运行时管理
├── manager.py               # 发现/加载/启停/重载/卸载 + 脚本工具注册
├── loader.py                # SKILL.md frontmatter 解析（Anthropic 开放规范）
├── importer.py              # ZIP/TAR/文件夹/Markdown 安全导入
└── models.py                # SkillModel 数据模型
backend/app/tools/registry.py # 统一工具注册中心（唯一入口）
frontend/src/pages/SkillsPage.tsx  # 技能库管理 UI
```

### 1.2 Skill 的核心机制

| 环节 | 实现 | 说明 |
|---|---|---|
| **定义** | `SKILL.md` + YAML frontmatter | `name`/`description`/`tags` 等，正文注入系统提示词 |
| **发现** | `skill_manager.discover()` | 递归扫描内置 + 外部目录，启动钩子 + `/reload` 接口 |
| **持久化** | DB `skills` 表 | `upsert_skill` / `set_skill_enabled`，启停状态持久化 |
| **工具化** | `_register_script_tools()` | `scripts/*.py` 的 `run()` 注册为 `skill_{name}__*` 工具 |
| **注入 Agent** | `tool_registry` → `graph_builder`/`supervisor` | ToolNode 在编译时固定工具集 |
| **API** | `router_skills.py` | list/get/import/enable/disable/reload/delete |
| **前端** | `SkillsPage.tsx` | 列表、详情、导入、启停、删除、重扫 |
| **安全** | `importer.py` | 路径穿越/符号链接/体积/重复名校验 |

**关键约束**：`graph_builder.build_main_graph()` 和 `supervisor.py` 在**编译时**通过 `tool_reg.all()` 固定工具集，因此 Skill/MCP 的生命周期变化后必须调用 `harness_app.build(...)` 重建图（`router_skills.py` 中的 `_rebuild_harness()`）。

### 1.3 当前短板

- **只有 Skill，没有 MCP**：`backend/app/tools/mcp_adapter.py` 仅是一个**占位实现**（`register_mcp_tools` 直接 `return 0`），无实际连接逻辑。
- Skill 的能力边界是「提示词 + 本地脚本工具」，**无法对接外部 MCP Server**（如第三方服务、远程工具）。
- 前端「技能库」只展示 Skill，没有 MCP Server 的管理入口。

---

## 2. MCP 能力如何加入

### 2.1 MCP 与 Skill 的定位差异

| 维度 | Skill | MCP Server |
|---|---|---|
| 本质 | 提示词 + 本地脚本/资源 | 外部工具服务（可远程/本地进程） |
| 触发方式 | `description` 驱动模型选择 | 工具 schema 驱动模型调用 |
| 生命周期 | 目录扫描 + DB 状态 | 连接管理 + 工具发现 |
| 承载 | SKILL.md + references/assets | 工具集 + prompts + resources |
| 治理点 | 启停开关 | 连接配置 + 启停开关 |

**结论**：二者是**互补**关系。MCP 提供的是**可执行工具**，Skill 提供的是**指令/流程**。把它们放进同一个「能力仓库」统一管理（启停、导入、审计、注入 Agent），即用户想要的「MCP + Skill 技能库」。

### 2.2 技术选型（推荐）

当前后端使用 **LangChain/LangGraph 生态**，`tool_registry` 统一持有 `langchain_core.tools.StructuredTool`。因此最平滑的方案是官方 **`langchain-mcp-adapters`**：

```bash
pip install langchain-mcp-adapters
```

核心能力：
- `MultiServerMCPClient`：管理多个 MCP Server 连接，`get_tools()` 返回 LangChain 兼容工具。
- 支持 transport：`stdio`（本地子进程）与 `sse` / `streamable-http`（远程 HTTP）。
- 返回的工具可直接 `registry.register(tool, source="mcp")`，**与现有工具注册中心无缝对接**。

替代方案（不推荐首选）：
- **直接基于 `mcp` Python SDK** 手写 client：灵活但工作量大，且工具需自行封装成 StructuredTool。
- **LangGraph 的 `mcp` 集成**：适合 graph 内临时建连，但不符合本项目「工具必须经 tool_registry 统一登记」的约束。

### 2.3 集成路径（从现状到 MCP）

```
MCP Server 配置（DB/文件）
   │  register_mcp_tools()  ← 实现 mcp_adapter.py
   ▼
tool_registry.register(tool, source="mcp:<server>")
   │
   ▼
harness_app.build()  ← 重建图（沿用现有 _rebuild_harness 机制）
   │
   ▼
agent 可调用 MCP 工具（与 Skill 脚本工具并列）
```

关键改动点：

1. **配置存储**：新增 `mcp_servers` 表（name / command+args(stdio) 或 url(远程) / enabled / env），或复用 `settings` 表。
2. **实现 `mcp_adapter.py`**：用 `MultiServerMCPClient` 建立连接 → `get_tools()` → 批量 `registry.register`，返回注册数量。
3. **生命周期管理**：新增 `MCPManager`（类比 `SkillManager`），启动时 discover、`/reload`、enable/disable 时重连/断开并 `_rebuild_harness`。
4. **API 路由**：新增 `router_mcp.py`（list / add / enable / disable / delete / reload / test-connection），沿用 `router_skills.py` 的鉴权 + 审计模式。
5. **前端**：在「技能库」页面增加「MCP Server」分区或独立 Tab，支持新增连接、启停、查看已暴露工具数、连接测试。
6. **注入 Agent**：MCP 工具注册进 `tool_registry` 后，`graph_builder`/`supervisor` 无需改动即可自动注入。

### 2.4 配置项建议（config.py 扩展）

```python
# --- MCP（MCP_SKILL_ECOSYSTEM_RESEARCH §2.4）---
MCP_ENABLED: bool = False          # 总开关
MCP_AUTO_CONNECT: bool = True      # 启动时自动连接已启用 server
MCP_CONNECT_TIMEOUT_SECONDS: float = 10.0
```

### 2.5 安全与治理（对标 Skill 的既有边界）

- MCP Server 会执行工具，属**不可信代码执行面**，启用前必须确认来源可信（类比 Skill `scripts/*.py` 的警告）。
- stdio transport 本质是启动本地子进程，需白名单 command，防 RCE。
- 远程 transport（HTTP/SSE）需校验 URL 协议与鉴权头，防 SSRF。
- 环境变量/secrets 不应写入 MCP 配置明文，应走密钥管理。

---

## 3. 落地工作分解（建议顺序）

| 阶段 | 内容 | 涉及文件 |
|---|---|---|
| **P0 依赖与配置** | 加 `langchain-mcp-adapters` 依赖；`config.py` 增 MCP 开关 | `requirements.txt`、`config.py` |
| **P1 存储模型** | 新增 `mcp_servers` 表 + repository + ORM model | `storage/models.py`、`storage/repositories.py` |
| **P2 适配器实现** | 实现 `mcp_adapter.py` 真实连接逻辑（stdio + 远程） | `tools/mcp_adapter.py` |
| **P3 生命周期** | 新增 `MCPManager`，启动 discover、启停、重连、重载 | `skills/mcp_manager.py`（或 `tools/mcp/`） |
| **P4 API** | `router_mcp.py`（list/add/enable/disable/delete/reload/test） | `api/router_mcp.py`、`api/schemas.py` |
| **P5 前端** | 「技能库」页增加 MCP Server 管理 Tab | `pages/SkillsPage.tsx`、`types/api.ts`、新增组件 |
| **P6 启动钩子** | `main.py` lifespan 中连接已启用 MCP server | `main.py` |
| **P7 测试** | 覆盖 adapter/manager/API；mock MCP server 起停 | `tests/test_mcp_*.py` |

---

## 4. 结论

- 当前体系是**成熟的 Skill 生命周期管理**，但 MCP 完全缺失（`mcp_adapter.py` 仅占位）。
- 项目基于 LangChain/LangGraph，**推荐用 `langchain-mcp-adapters` 将 MCP 工具适配进现有 `tool_registry`**，复用已有的图重建、鉴权、审计、前端治理机制。
- 改造的核心是「让 MCP Server 像 Skill 一样可管理」，而非重构现有体系。二者统一到「技能库/能力仓库」概念下。
- 工作量主要集中在新增 MCPManager + mcp_servers 存储 + 前端 Tab，**对现有 Skill 逻辑零侵入**。
