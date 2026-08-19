# 企业级 Agent Harness 底座系统

> 严格依据 `AGENT_HARNESS_SPEC.md` v1.0.0 实现的企业级 Agent 开发底座。

## 项目定位

不绑定业务的 Agent 开发底座，提供：会话管理、图编排、工具注册、Skill 体系、RAG 知识库、多 Agent 骨架、人工介入（HITL）、多租户隔离、鉴权、审计日志、成本计量、可观测性（LangSmith）以及 React 对话工作台。

## 技术栈

- **后端**: Python 3.11+, FastAPI, LangGraph 1.x, langchain-openai, SQLAlchemy 2.x, Postgres（pgvector）, Redis
- **前端**: React 18, Vite 5, TypeScript 5, Ant Design 5, Zustand, axios
- **部署**: Docker Compose（Postgres + Redis + Backend + Frontend）

## 目录结构

```
agent-harness/
├── backend/        # Python 后端（FastAPI + LangGraph）
├── frontend/       # React 前端
├── skills/         # 企业 Skill 仓库
├── deploy/         # Docker Compose / 初始化脚本
└── docs/           # 文档（SPEC / SKILL_GUIDE）
```

## 快速开始

### 方式一：Docker Compose 一键启动

```bash
cp .env.example .env
docker compose -f deploy/docker-compose.yml up --build
```

启动后：

- 前端工作台: http://localhost
- 后端 API: http://localhost:8000
- API 健康检查: http://localhost:8000/api/v1/health

### 方式二：本地开发

**后端**

```bash
cd backend
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --port 8000
```

**前端**

```bash
cd frontend
npm install
npm run dev
```

## API 鉴权

所有请求需携带 `X-API-Key` 请求头（`/health` 除外）。默认开发 Key 见 `backend/.env.example` 的 `API_KEYS`。

## 测试

```bash
cd backend
pytest -v
```

## RAG 知识库能力

底座内置 RAG 知识库能力（`docs/RAG_SPEC.md` v1.0.0），采用**检索工具（RAG-as-Tool）**形式接入：

- **能力底座**：知识库 CRUD、文档上传（`.md/.txt/.pdf/.docx`）、标题层级分块 + 固定长度重叠、OpenAI 兼容 Embedding、pgvector 余弦检索（HNSW 索引）。
- **Agent 集成**：`knowledge_search` 检索工具注册进工具中心，Agent 在 ReAct 循环中自主决定何时检索、检索哪个知识库；回答自动标注来源。
- **多租户隔离**：知识库数据按 `tenant_id` 严格隔离，检索 SQL 强制租户过滤，工具内部再次校验防越权。
- **前端闭环**：知识库管理页（创建/上传/删除）、侧栏入口、ContextPanel 知识库卡片、发送栏知识库多选、消息气泡检索来源引用。
- **可观测**：`knowledge.upload / delete / search` 审计 + 检索指标。

本地开发需确保 Postgres 启用 pgvector 扩展（`CREATE EXTENSION IF NOT EXISTS vector`），Docker 使用 `pgvector/pgvector:pg16` 镜像自动启用。

## 里程碑状态

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M1 | 项目骨架 + 基础对话（图 + REST + SSE + 前端工作台） | ✅ |
| M2 | 会话持久化 + 历史恢复 | ✅ |
| M3 | 工具体系 + Skill 体系 | ✅ |
| M4 | 多 Agent + HITL + 租户隔离 | ✅ |
| M5 | 治理（审计/指标/成本）+ 测试 + 文档 | ✅ |
| R1 | RAG 基础设施：pgvector + 配置 + 服务层（embedding/chunker/retriever/ingest） | ✅ |
| R2 | 检索工具接入 ReAct + API 路由 + 审计 | ✅ |
| R3 | 前端：知识库页 + 侧栏 + ContextPanel + 发送栏多选 + 工具卡片增强 | ✅ |
| R4 | 测试 + 文档 + Docker 联调 | ✅ |

详细技术规格请见 `docs/SPEC.md` 与 `docs/RAG_SPEC.md`。
