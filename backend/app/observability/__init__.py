"""可观测性（OBSERVABILITY_SPEC v2）：本地采集、指标、聚合、日志、成本与告警。

- tracing.py: Trace/Span 本地采集与落库（agent_spans / agent_traces）+ 脱敏 + 采样 + 批量队列
- log_export.py: 结构化日志 → observability_logs（自动注入 TraceContext，§8.1）
- costing.py: 按价格版本的 LLM 成本计算（§6.4）
- alerts.py: SLO 规则评估与告警事件状态机（§10.2）
- metrics.py: 内存实时指标（低延迟）+ 采集健康
- aggregation.py: 日/小时聚合、Trace 汇总、成本校准与数据保留清理
- audit.py: 审计日志（异步批量，关联 trace_id/request_id，§8.2）

采集链路：HTTP/SSE/后台任务 → TraceContext → LangGraph/LLM/Tool 插桩
→ 本地批量队列 → agent_spans/agent_traces/observability_logs
→ 聚合/评测/告警 → Observability API → Web Console。
"""
