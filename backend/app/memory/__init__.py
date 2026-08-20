"""长期记忆子系统（MEMORY_SPEC）。

- extractor.py：LLM 从对话中提炼候选记忆
- retriever.py：embed(query) + pgvector 检索（tenant/user/status 过滤 + 时间加权）
- hygiene.py：去重 / 冲突调和 / TTL 过期 / 强化
- service.py：MemoryService 门面（写入 extract-consolidate、读取检索注入、管理）
"""
