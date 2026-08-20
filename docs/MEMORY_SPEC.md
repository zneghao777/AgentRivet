# 记忆体系规格说明书（MEMORY_SPEC）

> 版本：v1.0.0
> 状态：草案，待评审
> 关联文档：`SPEC.md`（底座总规格）、`KB_MANAGEMENT_SPEC.md`（知识库/RAG 规格）、`RAG_INTEGRATION.md`（RAG 接入方案）
> 范围：本规格定义 Harness 底座的**分层记忆体系**（短期/长期记忆）、**记忆卫生策略**、**记忆管理 API** 与 **前端记忆管理页面**，并复用现有 RAG（pgvector + BGE-M3 embedding）基础设施。

---

## 1. 背景与目标

### 1.1 背景

- 底座已具备 **短期记忆**（LangGraph MessagesState + Postgres Checkpointer，会话内多轮恢复）与 **程序记忆**（Skill 体系静态注入），以及 **RAG 文档语义检索**（`knowledge_search` 工具）。
- 底座**缺少跨会话的长期记忆**：Agent 无法记住用户在对话中透露的事实、偏好、历史决策。`storage/store.py` 中的 `InMemoryStore` 仅为占位，未启用。
- 底座**缺少记忆卫生**：去重、冲突解析、TTL 遗忘、时间加权检索均为空白。
- **当前系统无用户概念**：`api/deps.py#require_auth` 将 `user_id` 硬编码为 `"api-user"`（注释"可后续扩展"），`Session` / `HarnessState` 默认 `"unknown"`，无 `users` 表，鉴权仅基于 API Key + 租户。**后续将接入登录体系，长期记忆必须按用户隔离**。

### 1.2 目标

1. 建立**跨会话长期记忆**：Agent 从对话中自主提炼事实/偏好，跨会话检索注入，越用越懂用户。
2. 建立**记忆管理能力**：前端页面支持查看记忆内容、时间线、长期记忆开关、单条/批量清理。
3. **复用 RAG**：长期记忆的语义存储与检索复用 pgvector + 现有 embedding 服务，不引入新基础设施。
4. **规避行业已知坑**：从第一天内置记忆卫生（去重/冲突/过期/时间加权）与租户隔离。
5. **用户级记忆（本版补充）**：记忆归属按 `tenant_id + user_id` 双维度建模，**为后续登录体系预留用户级隔离**；登录未接入前以统一占位用户运行，接入后零迁移升级为真实用户级记忆。
6. **样式一致**：前端页面复用 `knowledge` 体系既有 class（`page-scroll` / `page-header` / `kb-card` 等），保持全站审美统一。

### 1.3 非目标（本期不做）

- 不做 Agent 自主「程序记忆自我改写」（Skill 自更新）——列为 P1。
- 不做跨用户/跨租户记忆共享。
- **不做登录系统本身**（本期仍用 API Key 鉴权）：但记忆的数据模型与 API 必须按用户级设计，登录接入仅需在鉴权层替换 `user_id` 来源（见 §3.2 演进路径）。
- 不做多模态记忆。

---

## 2. 现状与差距分析

| 记忆类型 | 现状载体 | 状态 | 差距 |
|---|---|---|---|
| 短期记忆（工作记忆） | MessagesState + Postgres Checkpointer | ✅ 已有 | 无 |
| 语义记忆（文档级） | `knowledge_chunks` + pgvector | ✅ 已有 | 属"人为导入知识"，非对话提炼 |
| 程序记忆（指令） | Skill / SKILL.md | ✅ 已有 | 静态，非自学习 |
| **跨会话长期记忆（对话提炼）** | `store.py` InMemoryStore 占位 | ⚠️ 缺失 | **本 SPEC 核心** |
| 记忆卫生 | 无 | ❌ 缺失 | **本 SPEC 核心** |
| 记忆可视化/管理 | 无 | ❌ 缺失 | **本 SPEC 核心** |

**结论**：短期记忆不动；长期记忆在 `memory/` 新包中实现，完全复用 RAG 基础设施。

---

## 3. 总体架构

```
┌─────────────────────────────────────────────────────────────┐
│ 前端 MemoryPage (/memory)                                    │
│   · 记忆列表 + 时间线 · 长期记忆开关 · 清理 · 编辑/停用/删除   │
└──────────────┬──────────────────────────────────────────────┘
               │ HTTP (ApiResponse / require_auth / audit)
┌──────────────▼──────────────────────────────────────────────┐
│ API 层  router_memory.py  (/memory)                          │
│   list / timeline / overview / patch / delete / clear /      │
│   toggle (long-term switch)                                  │
└──────────────┬──────────────────────────────────────────────┘
┌──────────────▼──────────────────────────────────────────────┐
│ 服务层  memory/service.py · memory/extractor.py · hygiene.py │
│                                                              │
│  写入：extract_from_conversation()                           │
│        → extractor（LLM 提炼候选）                           │
│        → 调和（检索相似 → ADD/UPDATE/NOOP）                   │
│        → 向量化入库（pgvector）                              │
│  读取：retrieve_relevant()                                   │
│        → embed(query) + pgvector 余弦（tenant/user 过滤）    │
│        → 时间加权 → top-k                                    │
│  卫生：去重/冲突/TTL/时间加权（§6.3）                          │
└──────────────┬──────────────────────────────────────────────┘
┌──────────────▼──────────────────────────────────────────────┐
│ 存储  storage/models.py 新增 MemoryEntry / MemorySettings     │
│       embedding 复用 app/knowledge/embedding.py              │
└──────────────┬──────────────────────────────────────────────┘
┌──────────────▼──────────────────────────────────────────────┐
│ 接入点                                                        │
│  写入：router_chat（对话结束后后台调度）                        │
│  读取：base_agent.build_system_prompt（对话前检索注入）         │
│       或 memory_search 工具（Agent 自主检索）                  │
└─────────────────────────────────────────────────────────────┘
```

### 3.1 分层记忆映射

| 层级 | 内容 | 存储 | 生命周期 |
|---|---|---|---|
| L1 工作记忆 | 当前会话消息 | MessagesState + Checkpointer | 会话内 |
| L2 长期记忆-语义 | 用户事实/偏好/决策 | `memory_entries`（pgvector） | 跨会话，可编辑/删除 |
| L3 长期记忆-情景 | 重要事件快照 | `memory_entries`（type=episodic） | 跨会话，TTL 衰减 |
| L4 程序记忆 | Skill 指令 | SKILL.md + system prompt | 静态 |

### 3.2 用户模型与记忆归属（用户级记忆设计）

> **需求背景（用户补充）**：当前系统无用户概念；后续系统将接入登录，长期记忆必须为**用户级**。本条将用户级隔离作为记忆体系的**第一公民约束**，从数据模型、写入/读取、API 三层同时落地，确保登录接入时零迁移。

**归属维度**：每条记忆的唯一归属为 `(tenant_id, user_id)` 二元组。

```
租户 A ─┬─ 用户 A-1 ── 记忆集（memory_entries WHERE tenant=A AND user=A-1）
        ├─ 用户 A-2 ── 记忆集（memory_entries WHERE tenant=A AND user=A-2）
        └─ 未登录占位用户 ── 记忆集（user_id = "__anonymous__"）
租户 B ── 用户 B-1 ── 记忆集（与租户 A 完全隔离）
```

**当前阶段（无登录）**：
- `require_auth` 提供的 `user_id` 为统一占位值 `"api-user"`（沿用现有 `deps.py`）。
- 记忆按 `(tenant_id, "api-user")` 隔离——即**租户级隔离在占位用户下生效**，行为与现状一致。
- `HarnessState.user_id` 与 `ToolContext.user_id` 继续沿用到记忆写入/检索链路。

**登录接入后的演进路径（零迁移）**：
1. 新增 `users` 表（见 §5.4）与登录鉴权（JWT/Session），`require_auth` 改为从登录凭证解析真实 `user_id`。
2. `AuthContext.user_id` 从 `"api-user"` 变为真实用户 ID，其余代码**无需改动**——因为记忆表、服务、工具、API 从第一天就按 `(tenant_id, user_id)` 建模。
3. 历史占位用户（`"api-user"`）的记忆保留为匿名记忆集；如需迁移到真实用户，提供一次性后台迁移脚本（见 §6.5），不在本 SPEC 强制范围。

**设计约束（MUST）**：
- `memory_entries` 的所有读写 SQL 必须同时带 `tenant_id` 与 `user_id` 过滤。
- `memory_search` 工具 / 自动注入 / 管理 API 均只能访问当前 `AuthContext` 对应用户的记忆。
- `memory_settings` 同样按 `(tenant_id, user_id)` 主键隔离，长期记忆开关是**用户级**开关。
- 登录接入后用户注销：级联物理删除该用户全部记忆（对齐 GDPR 类"forget me"，见 §6.5）。

---

## 4. 记忆类型与生命周期

### 4.1 类型

- **semantic（语义记忆）**：跨会话稳定的用户事实与偏好，如"用户的生产数据库是 Postgres 16"、"用户偏好简洁回复"。
- **episodic（情景记忆）**：带时间戳的重要事件/已解决事项，如"用户上周解决了 XX 工单，方案是 YY"。

### 4.2 状态机

```
extracted(新增) ──▶ active(启用，参与检索)
     │                    │
     ├─(低置信度)──▶ pending(不参与检索，可人工确认) ──▶ active / delete
     │                    │
     └─(与旧记忆冲突)──▶ superseded(被更新取代) ──▶ 软删
active ──▶ disabled(人工停用) ──▶ active（重新启用）
active ──▶ (TTL 到期/置信度衰减) ──▶ expired ──▶ 软删
```

- 检索仅命中 `status == "active"`。
- `pending` / `superseded` / `expired` 在管理页可见但默认过滤（可切换查看）。

### 4.3 字段说明

每条记忆固定携带：来源会话（`source_session_id`）、创建/更新时间、置信度（0-1）、命中次数（`access_count`）、最近命中时间（`last_accessed_at`）——这四个字段是记忆卫生与时间加权的基础。

---

## 5. 数据模型

在 `backend/app/storage/models.py` 新增两张表：

### 5.1 `memory_entries`

| 列 | 类型 | 说明 |
|---|---|---|
| id | BigInteger PK | 记忆稳定主键（定位/编辑/删除） |
| tenant_id | String(64), index | 租户隔离（安全红线） |
| user_id | String(64), index | 用户级记忆 |
| memory_type | String(16) | `semantic` / `episodic` |
| content | Text | 记忆文本内容 |
| status | String(16) | `active/pending/superseded/disabled/expired` |
| confidence | Float | 提取置信度 0-1 |
| embedding | Vector(EMBEDDING_DIM) | BGE-M3 向量（复用维度配置） |
| source_session_id | UUID FK sessions.id | 来源会话（溯源） |
| source_summary | Text | 来源会话一句话摘要（溯源可读化） |
| access_count | Integer, default 0 | 命中次数（强化信号） |
| last_accessed_at | DateTime | 最近命中时间 |
| meta | JSON | 扩展：关键词、矛盾旧记忆 id 等 |
| created_at / updated_at | DateTime | 时间线 & TTL 依据 |

索引：
- `ix_mem_tenant_user`（tenant_id, user_id）
- `ix_mem_tenant_type`（tenant_id, memory_type）
- `ix_mem_tenant_created`（tenant_id, created_at）——时间线查询

### 5.2 `memory_settings`

> 用户级开关：`(tenant_id, user_id)` 复合主键。当前阶段 `user_id` 为占位值 `"api-user"`；登录接入后自动变为真实用户 ID，同一行数据即该用户独立的开关配置。

| 列 | 类型 | 说明 |
|---|---|---|
| tenant_id | String(64) PK | 租户 |
| user_id | String(64) PK | 用户（复合主键，用户级隔离） |
| long_term_enabled | Bool, default true | 长期记忆总开关（用户级） |
| auto_extract | Bool, default true | 对话后自动提炼 |
| ttl_days | Int, default 90 | 未强化记忆 TTL |
| updated_at | DateTime | 最近修改 |

### 5.3 DDL 说明

- embedding 列使用 `pgvector.sqlalchemy.Vector`，维度取 `settings.EMBEDDING_DIM`（当前 1024）。
- 建 HNSW 索引（与 `knowledge_chunks` 一致的做法）：`CREATE INDEX ... USING hnsw (embedding vector_cosine_ops)`，由迁移脚本或 `init.sql` 提供。
- 用户级过滤需同时建 `ix_mem_tenant_user`（tenant_id, user_id）组合索引，检索/管理/统计均依赖它。

### 5.4 `users` 表（登录体系预留，本期不建）

> 说明：**本期不创建、不依赖此表**。列为登录体系接入时预留的设计基线，确保 `memory_entries.user_id` 语义与未来 `users` 表一致。创建时机：登录需求立项时，随认证系统一并落地。

| 列 | 类型 | 说明 |
|---|---|---|
| id | UUID PK | 用户稳定主键（即记忆的 `user_id`） |
| tenant_id | String(64), index | 归属租户 |
| username | String(128), unique | 登录名 |
| display_name | String(128) | 显示名 |
| password_hash | String(255) | 口令哈希（或接 OAuth 免密） |
| status | String(16) | active / disabled / deleted |
| created_at / updated_at | DateTime | — |

**预留约束**：
- `memory_entries.user_id` / `memory_settings.user_id` 的取值必须与 `users.id` 对齐（登录后由 `require_auth` 注入）。
- 用户注销时，级联清理该用户全部 `memory_entries`、`memory_settings`（物理删除，对齐 GDPR "forget me"）。

---

## 6. 后端核心流程

### 6.1 写入流程：提取-调和（extract-consolidate）

**触发**：`router_chat` 中一轮对话（assistant 回复落库）后，`asyncio.create_task(memory_service.extract_from_conversation(...))` 后台调度，不阻塞响应。

```
1. 对话结束（user + assistant 消息均落库）
   │
2. extractor.extract(对话)           【LLM 调用 #1】
   │   prompt：从对话中提炼"值得长期记住"的事实/偏好/事件
   │   输出：JSON 列表 [{content, memory_type, confidence}]
   │   防幻觉：confidence < 0.6 → status=pending，不入检索
   │
3. 逐条调和 consolidate(memory)
   │   embed(content) → pgvector 检索同 tenant+user 的 top-k 相似记忆
   │   若 max_similarity >= 0.92：
   │     → 判定为重复 → NOOP（不新增；更新原记忆 access_count）
   │   若 0.70 <= max_similarity < 0.92：
   │     → 交由 LLM 裁决【LLM 调用 #2，仅此场景触发】：
   │         ADD（新事实）/ UPDATE（覆盖旧记忆，旧记忆 → superseded）/
   │         MERGE（合并文本，保留两者来源）
   │   否则 → ADD 新增
   │
4. 向量化入库（embed(content) 与步骤 3 复用，缓存一次结果）
```

**设计取舍**：
- 采用「提取-调和」而非「提取-追加」：多一次 LLM 调用换来自纠错，避免矛盾事实共存（行业最典型坑）。
- 调和仅在相似度落在 0.70–0.92 区间才触发，降低常规延迟；0.92 以上直接判重，0.70 以下直接新增。
- 提取为后台异步任务，热路径（对话响应）零额外延迟。

### 6.2 读取流程：检索注入

两阶段设计，均可由配置开关：

**方式 A：自动注入（默认开启，`MEMORY_AUTO_INJECT`）**
```
对话开始（agent_node 构建 system prompt 前）
  │
  embed(最近用户消息 + 会话标题)
  │
  pgvector 检索同 tenant+user 的 active 记忆 top-k
  │   排序：final_score = similarity * recency_weight（时间加权）
  │   recency_weight = 1 / (1 + α * days_since_last_accessed)
  │
  注入 system prompt：
    「以下是与该用户相关的长期记忆（来源已标注，若与当前对话矛盾请以当前对话为准）：
     - [语义] 用户生产库是 Postgres 16（来源：会话 X，2026-08-10）
     ...」
  │
  命中记忆 access_count += 1, last_accessed_at = now（异步）
```

**方式 B：Agent 自主工具（`memory_search`）**
- 新增 `tools/memory_search.py`，`StructuredTool.from_function` 写法与 `knowledge_search` 完全一致。
- 供 Agent 在需要回忆用户背景时主动检索（RAG-as-Tool 模式复用）。
- 同样带 tenant/user 过滤与审计。

### 6.3 记忆卫生（规避行业坑）

| 坑 | 规避方案 | 落点 |
|---|---|---|
| 矛盾事实并存 | 提取-调和：0.70–0.92 触发 LLM UPDATE/MERGE；被覆盖者 → superseded | §6.1 |
| 重复记忆 | 相似度 >= 0.92 判重 NOOP，并累加原记忆 access_count | §6.1 |
| 过期事实误导 | TTL：`created_at` 超 `ttl_days` 且 `access_count < 强化阈值(3)` → 置信度衰减 → expired → 软删；被持续命中的记忆自动"续期"（刷新 last_accessed_at） | §6.3.1 |
| 检索不看时间 | 时间加权排序（recency_weight） | §6.2 |
| 提取幻觉 | confidence < 0.6 → pending 不参与检索；管理页可见 | §6.1 |
| 隐私/越权 | 全链路 tenant_id + user_id 过滤；删除即物理删除行与向量；`memory_settings` 可整体关闭 | §10 |
| 注入噪声 | top-k 上限（默认 5，配置可调）+ 相似度阈值 | §6.2 |
| 嵌入模型漂移 | 与知识库同方案：换模型需重索引（运维文档备注） | §13 |

**6.3.1 TTL 与强化机制**
- 后台定时任务（每 24h）扫描：`status=active AND created_at < now()-ttl_days AND access_count < 3` → `expired`（软删）。
- 语义记忆若在对话中被持续检索命中（access_count 增长），视为"被强化"，不因 TTL 过期。
- 管理页提供"查看已过期/已停用"开关，人工可恢复。

### 6.4 RAG 复用点

| RAG 设施 | 复用方式 |
|---|---|
| `app/knowledge/embedding.py`（BGE-M3，1024 维） | 记忆向量化与查询，直接复用 |
| `app/knowledge/retriever.py` 检索 SQL 模式 | 记忆检索 SQL 采用同款 `tenant_id + status=active` 过滤写法 |
| `config.settings.EMBEDDING_*` | 维度/端点/Key 完全复用，不新增配置 |
| `knowledge_search` 工具模式 | `memory_search` 工具按同款写法实现 |

> 注意：`memory_entries` 与 `knowledge_chunks` 是**两张表**。知识库面向"组织文档知识"（kb 维度），长期记忆面向"用户个人事实"（user 维度），语义不同、不得混存。

### 6.5 用户级数据迁移与注销处理

**背景**：登录体系接入前，所有记忆归属 `user_id = "api-user"`（占位）；接入后归属真实用户 ID。本小节定义两条演进动作（**本期仅预埋设计，不实现**）：

1. **占位用户迁移脚本（一次性，可选）**：登录上线时，将 `tenant_id + "api-user"` 的记忆批量迁移到指定真实用户（`UPDATE memory_entries SET user_id = :new_user WHERE tenant_id = :t AND user_id = 'api-user'`），随后删除占位用户。是否迁移由业务决定（匿名记忆默认不随登录自动归属）。
2. **用户注销级联删除（MUST）**：用户注销/删除时，物理删除该用户全部 `memory_entries`（含向量行）与 `memory_settings`，保证"forget me"即时生效。同时清理该用户关联会话（复用现有 `soft_delete_session` 或升级为物理删除，取决于业务合规要求）。

**占位用户常量（约定）**：
- 未登录阶段的统一 `user_id` 建议定义为 `settings.ANONYMOUS_USER_ID = "__anonymous__"`，替换现有 `deps.py` 硬编码的 `"api-user"`（避免与未来真实用户 ID 混淆）。此改动为 MUST，属于本 SPEC 的 M1 范围。

---

## 7. API 设计

统一遵循现有约定：`ApiResponse` 包装、`require_auth` 鉴权、`audit.audit` 审计、越权返回 404。

**用户级范围**：所有 `/memory` 接口的操作对象均为「当前 `AuthContext` 对应用户」的记忆——`tenant_id` 与 `user_id` 均取自鉴权上下文，不接受客户端传参覆盖（防越权）。

`APIRouter(prefix="/memory", tags=["memory"])`

### 7.1 记忆条目

| 方法 | 路径 | 请求 | 返回 | 说明 |
|---|---|---|---|---|
| GET | `/memory` | query: `page`, `page_size`, `memory_type`, `status`, `q` | `MemoryListResponse` | 列表/过滤/分页（当前用户） |
| GET | `/memory/timeline` | query: `days=30` | `MemoryTimelineResponse` | 按天分组时间线（当前用户） |
| GET | `/memory/overview` | — | `MemoryOverview` | 统计：总数/类型/状态/近7日新增/命中top（当前用户） |
| PATCH | `/memory/{id}` | `{content?, status?}` | `MemoryItem` | 编辑内容（自动重新向量化）/ 改状态（当前用户） |
| DELETE | `/memory/{id}` | — | `{id}` | 单条删除（物理，当前用户） |
| DELETE | `/memory` | — | `{deleted}` | 清空当前用户全部记忆（二次确认由前端） |

### 7.2 记忆设置

| 方法 | 路径 | 请求 | 返回 | 说明 |
|---|---|---|---|---|
| GET | `/memory/settings` | — | `MemorySettings` | 读取当前用户开关/TTL |
| PATCH | `/memory/settings` | `{long_term_enabled?, auto_extract?, ttl_days?}` | `MemorySettings` | 更新当前用户设置 |

### 7.3 响应 Schema（`schemas.py` 新增）

```python
class MemoryItem(BaseModel):
    memory_id: int
    memory_type: str            # semantic / episodic
    content: str
    status: str
    confidence: float
    source_session_id: str | None
    source_summary: str | None
    access_count: int
    last_accessed_at: str | None
    created_at: str | None
    updated_at: str | None

class MemoryListResponse(BaseModel):
    items: list[MemoryItem]
    total: int
    page: int
    page_size: int

class MemoryTimelineGroup(BaseModel):
    date: str                   # YYYY-MM-DD
    items: list[MemoryItem]
    count: int

class MemoryTimelineResponse(BaseModel):
    groups: list[MemoryTimelineGroup]

class MemoryOverview(BaseModel):
    total: int
    semantic_count: int
    episodic_count: int
    active_count: int
    pending_count: int
    disabled_count: int
    added_7d: int
    top_accessed: list[MemoryItem]   # 按 access_count 排序 top 5

class MemorySettings(BaseModel):
    long_term_enabled: bool = True
    auto_extract: bool = True
    ttl_days: int = 90

class MemoryUpdateRequest(BaseModel):
    content: str | None = None
    status: Literal["active", "disabled", "pending"] | None = None

class MemorySettingsUpdateRequest(BaseModel):
    long_term_enabled: bool | None = None
    auto_extract: bool | None = None
    ttl_days: int | None = Field(default=None, ge=7, le=365)
```

---

## 8. 前端设计

### 8.1 入口与路由

- 侧边栏 `Sidebar.tsx` 的 `FEATURES` 增加：`{ label: "记忆", icon: "memory", path: "/memory" }`（置于"知识库"之后）。
- `App.tsx` 增加路由 `<Route path="/memory" element={<MemoryPage />} />`。
- 新建 `frontend/src/pages/MemoryPage.tsx`。

### 8.2 页面结构（复用 knowledge 体系样式）

```
┌──────────────────────────────────────────────────────────────┐
│ page-header                                                  │
│  [memory 图标]  记忆                              [刷新]     │
│  跨会话长期记忆：Agent 从对话中学习的事实与偏好（用户级）      │
│  ── 长期记忆开关 Switch ── 全部清除(危险,Popconfirm) ──      │
├──────────────────────────────────────────────────────────────┤
│ 统计卡行（kb-card 风格 x4）                                    │
│  记忆总数 | 语义记忆 | 情景记忆 | 近7日新增                    │
├──────────────────────────────────────────────────────────────┤
│ 工具栏                                                       │
│  [类型: 全部/语义/情景] [状态: 启用/待确认/已停用/已过期]       │
│  [关键词搜索 Input]  [视图: 时间线/列表]                      │
├──────────────────────────────────────────────────────────────┤
│ 内容区                                                       │
│  视图A 时间线：按日期分组（今天/昨天/MM-DD），组内记忆卡片      │
│  视图B 列表：kb-chunk-list 风格                            │
│  ── 记忆卡片 ──                                              │
│   [类型 Tag]  内容文本                                         │
│   来源:会话摘要 · 置信度 xx% · 命中 N 次 · 更新时间             │
│   [编辑] [停用/启用] [删除(Popconfirm)]                        │
└──────────────────────────────────────────────────────────────┘
```

### 8.3 交互细节

| 交互 | 行为 |
|---|---|
| 长期记忆开关 | 调 `PATCH /memory/settings`；关闭后 Agent 不再注入/写入记忆，页面数据保留 |
| 全部清除 | `Popconfirm` 二次确认 → `DELETE /memory` → 空态 |
| 单条删除 | `Popconfirm` → `DELETE /memory/{id}` |
| 停用/启用 | `PATCH /memory/{id}` status 切换 |
| 编辑内容 | Modal（复用 `kb-chunk-edit-area` 风格）→ `PATCH /memory/{id}`，后端自动重新向量化 |
| 时间线 | 后端按天分组返回，前端 `今天 / 昨天 / M月D日` 相对化展示（复用 `formatTime` utils） |
| 待确认记忆 | status=pending 卡片带橙色 Tag + 提示文案，可一键确认（→active）或删除 |

### 8.4 样式一致性（复用清单）

| 前端元素 | 复用 class / 组件 |
|---|---|
| 页面骨架 | `.page-scroll`、`.page-header`、`.page-header-icon`、`.page-header-copy` |
| 统计卡片 | `.kb-card` 系列（卡片、hover、渐变描边） |
| 记忆卡片列表 | `.kb-chunk-list` / `.kb-chunk-row`（文本、hover、禁用态） |
| 过滤器 | `.kb-chunk-filters`（antd Select / Input.Search） |
| 编辑弹窗 | 复用 `kb-modal-title` / `.chunk-edit-area` / `.chunk-edit-hint` |
| 标签 | antd `Tag`（`semantic`→processing，`episodic`→geekblue，`pending`→warning，`disabled`→default） |
| 图标 | `material-symbols-rounded`（memory / timeline / delete / edit / refresh） |
| 空态/加载 | antd `Empty` / `Skeleton`（与知识库页一致） |

### 8.5 前端类型（`types/api.ts` 新增）

```ts
export type MemoryType = "semantic" | "episodic";
export type MemoryStatus = "active" | "pending" | "superseded" | "disabled" | "expired";

export interface MemoryItem {
  memory_id: number;
  memory_type: MemoryType;
  content: string;
  status: MemoryStatus;
  confidence: number;
  source_session_id: string | null;
  source_summary: string | null;
  access_count: number;
  last_accessed_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface MemoryListData { items: MemoryItem[]; total: number; page: number; page_size: number; }
export interface MemoryTimelineGroup { date: string; items: MemoryItem[]; count: number; }
export interface MemoryTimelineData { groups: MemoryTimelineGroup[]; }
export interface MemoryOverview { total: number; semantic_count: number; episodic_count: number; active_count: number; pending_count: number; disabled_count: number; added_7d: number; top_accessed: MemoryItem[]; }
export interface MemorySettings { long_term_enabled: boolean; auto_extract: boolean; ttl_days: number; }
```

---

## 9. 配置项（`config.py` 新增）

```python
# --- 长期记忆（MEMORY_SPEC §6/§7） ---
MEMORY_ENABLED: bool = True            # 全局总开关（熔断用）
MEMORY_AUTO_INJECT: bool = True        # 对话前自动注入记忆
MEMORY_SEARCH_TOOL: bool = True        # 注册 memory_search 工具
MEMORY_TOP_K: int = 5                  # 单次注入/检索上限
MEMORY_SIM_DUP: float = 0.92           # 判重相似度阈值
MEMORY_SIM_CONFLICT: float = 0.70      # 冲突调和阈值下限
MEMORY_CONFIDENCE_PENDING: float = 0.6 # 低于此值进 pending
MEMORY_DEFAULT_TTL_DAYS: int = 90      # 默认 TTL（settings 表可覆盖）
MEMORY_EXTRACT_MAX_ITEMS: int = 10     # 单次对话最多提炼条数

# --- 用户级记忆（MEMORY_SPEC §3.2 / §6.5） ---
ANONYMOUS_USER_ID: str = "__anonymous__"  # 未登录阶段的统一占位用户 ID（替换 deps.py 硬编码 "api-user"）
```

> 登录体系接入时，`require_auth` 将 `user_id` 替换为真实用户 ID，`ANONYMOUS_USER_ID` 仅用于未登录阶段的占位记忆集。

---

## 10. 错误处理与安全

- **鉴权**：所有 `/memory` 接口走 `require_auth`；查询一律带 `tenant_id + user_id`，越权返回 404（不泄露存在性，沿用知识库红线）。
- **越权防护（用户级）**：`get_memory_entry` 必须 `WHERE id=? AND tenant_id=? AND user_id=?`——`user_id` 取自 `AuthContext`，客户端不可指定他人用户。
- **审计**：`memory.extract`（后台）、`memory.update`、`memory.delete`、`memory.clear`、`memory.settings`、`memory.search` 均写 `audit_logs`（审计中的 `user_id` 与记忆归属一致，登录后即真实用户）。
- **记忆卫生任务**：TTL 扫描在独立后台任务中执行，失败只记录不抛出。
- **删除语义**：单条/清空为**物理删除**（行 + 向量 + 相关 embedding），保证"forget"语义即时生效。
- **用户注销**：级联物理删除该用户全部记忆与设置（§6.5），作为登录体系接入时的高优先项。
- **异常兜底**：提取/调和 LLM 调用失败 → 记录日志 + 指标，本轮不产生记忆，不影响对话主链路。

---

## 11. 里程碑

| 里程碑 | 内容 |
|---|---|
| M1 | 数据模型（`memory_entries` / `memory_settings`）+ `memory/` 服务骨架（extract-consolidate 写入、检索注入、TTL）+ `ANONYMOUS_USER_ID` 替换 `deps.py` 硬编码 |
| M2 | `router_memory.py` 全部 API + `schemas.py` + 配置项 + 审计/指标 |
| M3 | 对话接入点（`router_chat` 后台提取 + `base_agent` 自动注入 + `memory_search` 工具），全程注入 `AuthContext.user_id` |
| M4 | 前端 `MemoryPage`（列表/时间线/开关/清理/编辑）+ 侧边栏入口 + 类型 |
| M5 | 测试（单元 + 集成）+ 记忆卫生验证（去重/冲突/TTL）+ 用户级隔离验证 + 文档 |

---

## 12. 验收标准

- [ ] 对话结束后，后台自动提炼出用户事实/偏好，`/memory` 可见且带来源与置信度
- [ ] 新会话提问与旧记忆相关时，Agent 回复中体现该记忆（自动注入生效）
- [ ] 同一事实重复出现不产生重复记忆（>=0.92 判重 NOOP）
- [ ] 用户改口后旧记忆被 UPDATE/superseded，不再同时参与检索
- [ ] 超过 TTL 且未强化的记忆自动过期，管理页可见、可恢复
- [ ] 关闭长期记忆开关后，Agent 不再注入/写入记忆；重新打开数据仍在
- [ ] 单条删除与全部清空后，检索立即不再命中
- [ ] 所有 `/memory` 接口鉴权生效，跨租户/跨用户访问返回 404
- [ ] **用户级隔离**：不同 `user_id`（模拟登录前后）下记忆互不可见，`user_id` 不可由客户端指定
- [ ] `deps.py` 不再硬编码 `"api-user"`，统一使用 `settings.ANONYMOUS_USER_ID`
- [ ] 前端记忆页与知识库页视觉一致（复用样式，无风格违和）
- [ ] 全链路审计日志存在（extract/update/delete/clear/settings/search）

---

## 13. 风险与注意事项

1. **提取质量不稳定**：LLM 提炼可能漏提或错提 → 通过 confidence→pending 兜底 + 管理页人工编辑/删除形成闭环。
2. **embedding 模型漂移**：更换 embedding 模型后 `memory_entries` 需全量重索引（与知识库同生命周期管理）。
3. **记忆体积膨胀**：TTL + 判重 + access_count 强化机制控制规模；管理页可随时清理。
4. **注入上下文预算**：`MEMORY_TOP_K` 默认 5 条，每条限制长度（如 120 字内），防止挤占窗口。
5. **与知识库语义边界**：长期记忆是"用户个人事实"，知识库是"组织文档知识"，检索工具各自独立、勿混用。
6. **后台任务资源**：提取任务用 `asyncio.create_task` + 有界 Semaphore（对齐 `PARSE_CONCURRENCY=3` 的做法），防止并发高峰。
7. **superseded/expired 数据**：软删保留审计可追溯，管理页默认隐藏，避免噪音。
8. **占位用户与真实用户混用**：登录上线前必须完成 `ANONYMOUS_USER_ID` 替换（M1），避免 `"api-user"` 与未来真实用户 ID 撞名导致记忆串号。
9. **匿名记忆不自动归属**：占位用户的记忆默认不随登录自动归属到真实用户（隐私优先），是否迁移由业务显式决策（§6.5）。
10. **用户维度查询性能**：所有记忆 SQL 必须命中 `ix_mem_tenant_user` 组合索引，避免用户量增长后全表扫描。

---

## 14. 参考

- 本报告：《记忆体系调研报告》（`docs/memory-system-research.md`）——行业分层记忆架构、四类记忆、记忆卫生坑位。
- 行业范式：MemGPT/Letta OS 式分层记忆、LangMem（语义/情景/程序三 API）、Mem0 提取-调和写入。
- 本项目：`SPEC.md`、`KB_MANAGEMENT_SPEC.md`、`RAG_INTEGRATION.md`。
