"""向后兼容别名：`embedder.py` 的 re-export。

既有代码 / 测试引用 `app.knowledge.embedding` 下的符号；
新实现见 `embedder.py`（BGE-M3 / 1024 维 / 维度校验 / 纯 httpx 调用）。
"""

from .embedder import (  # noqa: F401
    EmbeddingError,
    _reset,
    embed_query,
    embed_texts,
)
