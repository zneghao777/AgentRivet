"""LangSmith 集成：通过环境变量启用（无需代码埋点）。"""

from __future__ import annotations

import logging

from ..config import settings

logger = logging.getLogger(__name__)


def init_tracing() -> None:
    """启动时检测 LangSmith 配置。

    MUST：LANGSMITH_TRACING=true 但缺 Key 时输出 warning（允许启动）。
    配置通过环境变量自动生效（langsmith 自动读取）。
    """
    if not settings.LANGSMITH_TRACING:
        logger.info("LangSmith tracing 已禁用")
        return

    if not settings.LANGSMITH_API_KEY:
        logger.warning(
            "LANGSMITH_TRACING=true 但未配置 LANGSMITH_API_KEY，trace 不会上报（允许启动）"
        )
    else:
        logger.info(
            "LangSmith tracing 已启用，project=%s endpoint=%s",
            settings.langsmith_project,
            settings.LANGSMITH_ENDPOINT or "cloud",
        )


def trace_tags(tenant_id: str, session_id: str) -> list[str]:
    """为每次请求注入自定义 tags，便于按租户筛选 trace。"""
    return [tenant_id, session_id]
