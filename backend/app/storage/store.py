"""记忆 Store（预留）：LangGraph BaseStore 接口。

第一版返回 InMemoryStore；后续接入 Postgres/Redis 语义记忆（SHOULD）。
"""

from __future__ import annotations

from langgraph.store.memory import InMemoryStore

_store: InMemoryStore | None = None


def get_store() -> InMemoryStore:
    """获取记忆 Store 单例。"""
    global _store
    if _store is None:
        _store = InMemoryStore()
    return _store
