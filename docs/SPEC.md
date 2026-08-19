# 企业级 Agent Harness 底座系统 · 技术规格说明书

> 本文件为正式技术规格（SPEC）v1.0.0。原始来源：`AGENT_HARNESS_SPEC.md`。
> 开发团队 / AI 开发 Agent 依据此规格实现，不得偏离技术选型与接口约定。

详细内容请查阅根目录 `AGENT_HARNESS_SPEC.md`（或上级 `workbuddyProject/AGENT_HARNESS_SPEC.md`）。

## 核心决策摘要

- **后端**：Python 3.11+ · FastAPI · LangGraph 1.x · SQLAlchemy 2.x · Postgres 16 · Redis 7
- **前端**：React 18 · Vite 5 · TypeScript 5 · Ant Design 5 · Zustand
- **图编排**：主 Agent ReAct 循环 + 可选 Supervisor 多 Agent 骨架 + HITL interrupt
- **持久化**：Session→thread_id 一对一，LangGraph Postgres Checkpointer
- **Skill 体系**：遵循 Anthropic SKILL.md 开放规范，可发现/加载/启停/注册为工具
- **治理**：多租户隔离、API Key 鉴权、审计日志、成本计量、LangSmith 可观测

## 里程碑

| 里程碑 | 内容 |
|---|---|
| M1 | 项目骨架 + 基础对话（图 + REST + SSE + 前端工作台） |
| M2 | 会话持久化 + 历史恢复 |
| M3 | 工具体系 + Skill 体系 |
| M4 | 多 Agent + HITL + 租户隔离 |
| M5 | 治理（审计/指标/成本）+ 测试 + 文档 |

## 验收标准（MVP）

- [x] Chat API 完成带工具调用的流式多轮对话
- [x] 会话持久化，服务重启后可恢复
- [x] 动态加载/停用 Skill，能力生效
- [x] Agent 执行轨迹 LangSmith 可见（需配置 Key）
- [x] 请求写审计日志
- [x] 前端完成「新建会话 → 流式对话 → 查看/加载 Skill」闭环
- [x] `docker compose up` 一键启动
