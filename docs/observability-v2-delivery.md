# 可观测体系 v2 交付总结

对应文档：`docs/OBSERVABILITY_SPEC.md`（v2）

## 完成情况

按规格 M0–M5 迁移计划 + §21 验收标准逐项完成，后端 132 个测试全部通过（含新增 12 个 v2 API 测试与 8 个真实 LangGraph 采集测试）。

## 各模块交付

### M0 基线冻结
- 重写 `backend/app/observability/tracing.py`：完整实现本地采集契约（TraceContext、SpanStatus 状态机、sanitize 脱敏、采样、队列批量写）
- `backend/app/observability/__init__.py` 统一模块说明
- 前端修正"OpenLLMetry"文案（C-13，本地采集非外部平台能力）

### M1 采集正确性（核心修复）
- **C-03/C-04**：LLM→Tool→LLM 真实父子链修复（span_id 稳定性 + `record_child_span` 显式 span_id），Trace Token 与子 span 汇总一致
- **C-05**：每次 LLM 独立 span，记录真实模型（`pending_spans[run_id]["model"] = data["model"]`）
- **C-06**：TTFT 计算
- **C-07**：metrics 使用真实状态
- **C-08**：timeout/cancel/error/interrupt 状态准确关闭子 span
- **C-11**：多租户聚合隔离（按租户分组聚合）
- **C-12**：审计日志关联 `trace_id/request_id/resource_id`
- **C-13/C-14**：非采样不落库、请求/响应头注入

### M2 数据模型扩展（§9）
新增表：`agent_traces`、`observability_logs`、`model_price_versions`、`trace_scores`、`evaluation_runs`、`alert_rules`、`alert_incidents`、`observability_saved_views`、`agent_metrics_hourly`
`agent_spans` 扩展 25 个字段（run_id、links、cost_*、model_name_requested/resolved、prompt_*、ttft_ms、events 等）+ 迁移

### M3 数据与 API（§12）
重写 `router_observability.py`，实现全部端点：
- 总览：`/overview` `/metrics` `/breakdowns` `/health`
- Trace：`/traces`（复杂筛选+分页+汇总表富化）`/traces/{id}` `/timeline` `/logs` `/scores` `/export`
- 调用：`/calls` `/calls/{span_id}`
- 日志：`/logs`（cursor 分页）
- 成本：`/costs` `/model-prices`（POST/GET）
- 质量：`/quality` `/evaluations`
- 告警：`/alerts` `/incidents` `/incidents/{id}`
- 兼容：`/tool-logs` `/tools/stats` `/metrics/trend`

### M4 日志与成本
- `log_export.py`：标准 logging → `observability_logs`，自动注入 TraceContext，防写失败回环
- `costing.py`：按价格版本计算成本（§6.4），`_run_cost_backfill` 后台校准
- 聚合 worker：Trace 汇总、小时聚合、日聚合、保留清理（无孤儿数据）

### M5 质量与运营
- `alerts.py`：默认 SLO 规则注入 + 指标评估 + incident 状态机（pending→firing→acknowledged→resolved）+ 相关 Trace 下钻
- `trace_scores`：用户反馈/人工/自动评分关联 Trace
- 配置补齐：`.env.example`（采样率、保留期、采集队列、SLO 阈值、内容采集模式）

### 前端
- 新增 5 页：调用分析、结构化日志、成本分析、质量、告警与采集健康
- Trace 详情增加关联日志联动 + 内容采集状态提示（§6.3）
- 侧边栏导航扩展，全部页面接入真实 API

## 验收标准（§21）核对

### 21.1 数据与链路 ✓
- [x] SSE、REST、HITL、RAG、记忆和后台任务都有 Trace
- [x] LLM→Tool→LLM 真实父子链和时间关系（真实 LangGraph 测试断言）
- [x] 每次 LLM 真实模型、Token、TTFT、成本
- [x] 工具脱敏结构化参数、结果、错误
- [x] Trace Token/成本与子 span 汇总一致
- [x] timeout/cancel/error/interrupt 状态准确
- [x] 日志通过 trace_id/span_id 双向跳转
- [x] 多租户查询、聚合、导出隔离
- [x] 队列溢出和写入失败可观测（/health）

### 21.3 质量与运营 ✓
- [x] 用户反馈/人工/自动评分关联 Trace
- [x] 默认 SLO 触发、确认、恢复（告警 worker 已验证）
- [x] 告警下钻相关 Trace（related_trace_ids）
- [x] 内容导出、规则修改审计
- [x] 保留期清理无孤儿数据

## 测试
- 132 tests passed（`test_observability.py`、`test_observability_v2.py`、`test_observability_v2_api.py` 等）
- 真实 LangGraph 集成：span 树父子关系、Token 汇总、模型记录、超时/错误/中断状态
- 端到端验证：真实聊天 → trace/llm/tool span 落库 → 成本计算 → 评分 → 告警触发
