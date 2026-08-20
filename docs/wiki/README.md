# Agent Harness Code Wiki

企业级 Agent 开发底座（agent-harness）的代码知识库。本文档基于对仓库源码的实际分析生成，覆盖整体架构、模块职责、关键类与函数、数据模型、API、依赖关系与运行方式。

## 项目简介

本项目是一个**不绑定具体业务的 Agent 开发底座**，核心能力包括：

- **对话与编排**：基于 LangGraph 的 ReAct 图编排，支持流式 SSE 输出、多 Agent Supervisor 路由、HITL（人工介入）中断与恢复
- **会话管理**：会话持久化（Postgres + LangGraph Checkpointer）、历史恢复、跨会话长期记忆
- **工具体系**：内置工具（calculator / echo / get_time）+ 知识检索工具 + 记忆检索工具 + MCP 适配（预留）
- **Skill 体系**：基于 `SKILL.md` 声明式加载，支持脚本工具注册与启停
- **RAG 知识库**：文档上传 → 解析（GLM 异步解析 / 本地解析）→ 分块 → 向量化（BGE-M3, 1024 维）→ pgvector 余弦检索，以 RAG-as-Tool 形式接入 Agent
- **长期记忆**：对话后自动提炼记忆（semantic / episodic）、去重、冲突调和、TTL 过期、时间加权检索
- **可观测性**：本地 OpenLLMetry 风格的 Trace/Span 采集、审计日志、指标聚合（小时/日）、成本计量、SLO 告警
- **多租户与安全**：API Key 鉴权（SHA256 哈希 + 常量时间比较）、租户数据隔离、内容脱敏
- **前端工作台**：React 18 对话界面 + 知识库管理 + 记忆管理 + Skill 管理 + 观测台

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | Python 3.11+, FastAPI, LangGraph 1.x, langchain-openai, SQLAlchemy 2.x (async), pydantic-settings |
| 数据库 | PostgreSQL 16 + pgvector（向量检索，HNSW 索引），Redis（可选） |
| 前端 | React 18, TypeScript 5, Vite 5, Ant Design 5, Zustand, axios, react-markdown, recharts |
| 部署 | Docker Compose（pgvector/pgvector:pg16 + redis:7 + backend + frontend/nginx） |

## 文档目录

| 文档 | 内容 |
|---|---|
| [01-architecture.md](01-architecture.md) | 整体架构、分层设计、核心数据流、启动生命周期 |
| [02-backend-modules.md](02-backend-modules.md) | 后端各模块（agents / harness / api / knowledge / memory / observability / security / skills / storage / tools）的职责 |
| [03-backend-key-classes.md](03-backend-key-classes.md) | 后端关键类与函数说明（签名、功能、协作关系） |
| [04-frontend.md](04-frontend.md) | 前端架构、路由、状态管理、API/SSE 层、页面与组件 |
| [05-data-model.md](05-data-model.md) | 数据库表模型（21 张表）与存储层设计 |
| [06-api-reference.md](06-api-reference.md) | REST API 全量路由一览（/api/v1） |
| [07-dependencies.md](07-dependencies.md) | 第三方依赖、模块间依赖关系、外部服务依赖 |
| [08-getting-started.md](08-getting-started.md) | 本地开发、一键启动、Docker 部署、测试、配置项说明 |

## 目录结构速览

```
MyAgentDemo1/
├── backend/           # Python 后端（FastAPI + LangGraph）
│   ├── app/
│   │   ├── agents/    # Agent 定义与注册中心
│   │   ├── api/       # REST 路由与请求/响应模型
│   │   ├── harness/   # 图编排运行时（核心）
│   │   ├── knowledge/ # RAG 知识库（解析/分块/向量化/检索）
│   │   ├── memory/    # 长期记忆（提取/检索/卫生/清理）
│   │   ├── observability/ # 观测（tracing/audit/metrics/aggregation）
│   │   ├── security/  # API Key 鉴权与租户解析
│   │   ├── skills/    # Skill 加载与管理
│   │   ├── storage/   # ORM 模型、数据库、仓储、Checkpointer
│   │   ├── tools/     # 工具注册中心与内置/RAG/记忆工具
│   │   ├── config.py  # 全局配置中心（pydantic-settings）
│   │   └── main.py    # FastAPI 入口（lifespan 启动钩子）
│   └── tests/         # pytest 测试与评测脚本
├── frontend/          # React 前端工作台
│   └── src/
│       ├── api/       # axios 封装 + SSE 流式客户端
│       ├── components/# chat / knowledge / layout / skills 组件
│       ├── pages/     # 页面（Chat/Knowledge/Memory/Skills/Settings/Observability）
│       ├── stores/    # Zustand 状态（session/settings）
│       └── types/     # TS 类型定义
├── skills/            # 外部企业 Skill 仓库（SKILL.md）
├── deploy/            # docker-compose.yml + postgres/init.sql
├── docs/              # 规格文档（SPEC / RAG_SPEC / MEMORY_SPEC / OBSERVABILITY_SPEC 等）
├── data/uploads/      # 知识库上传文件落盘目录
└── start.sh           # 本地一键启动脚本
```

## 关键设计决策

1. **RAG-as-Tool**：知识检索不直接拼接进 prompt，而是注册为 `knowledge_search` 工具，由 Agent 在 ReAct 循环中自主决定何时检索、检索哪个知识库。
2. **租户强制隔离**：所有业务表带 `tenant_id` 列，检索 SQL 强制租户过滤，工具内部再次校验防越权；租户从 API Key 前缀或 `X-Tenant-Id` 头解析。
3. **可观测内建**：以 `astream_events(version="v2")` 的 `run_id`/`parent_ids` 作为链路映射唯一事实来源，run_id → span_id 映射修复父子关系；Token 每次 LLM 调用独立记录、根 span 只存汇总。
4. **配置集中**：所有配置项集中于 [config.py](../../backend/app/config.py) 的 `Settings`，禁止业务代码散落读取环境变量。
5. **观测与记忆均有降级路径**：Checkpointer 初始化失败时降级为无持久化模式；GLM 解析 Key 未配置时回退本地解析；LLM Key 未配置时允许启动、接口返回错误事件。

## 相关规格文档

更详细的 设计规格 见 `docs/` 目录：

- `docs/SPEC.md` — 底座总体规格
- `docs/RAG_SPEC.md` / `RAG_INTEGRATION.md` / `RAG_RESEARCH.md` — RAG 设计
- `docs/KB_MANAGEMENT_SPEC.md` — 知识库管理
- `docs/MEMORY_SPEC.md` / `memory-system-research.md` — 长期记忆设计
- `docs/OBSERVABILITY_SPEC.md` — 可观测性设计
- `docs/SKILL_GUIDE.md` — Skill 编写指南
