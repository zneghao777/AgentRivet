"""知识库索引模式与文档图谱参与策略。"""

from __future__ import annotations

from typing import Literal

IndexMode = Literal["standard", "graph_enhanced"]
GraphBuildPolicy = Literal["auto", "manual"]
GraphOverride = Literal["inherit", "include", "exclude"]
GraphAction = Literal["excluded", "pending", "enqueue"]

INDEX_MODES = {"standard", "graph_enhanced"}
GRAPH_BUILD_POLICIES = {"auto", "manual"}
GRAPH_OVERRIDES = {"inherit", "include", "exclude"}


class IndexPolicyError(ValueError):
    """稳定的索引策略错误，可由 API 映射为业务错误码。"""

    def __init__(self, message: str, *, code: str = "INDEX_MODE_CONFLICT") -> None:
        super().__init__(message)
        self.code = code


def validate_index_settings(index_mode: str, graph_build_policy: str) -> None:
    if index_mode not in INDEX_MODES:
        raise IndexPolicyError("无效的知识库索引模式")
    if graph_build_policy not in GRAPH_BUILD_POLICIES:
        raise IndexPolicyError("无效的图谱构建策略")
    if index_mode == "standard" and graph_build_policy != "manual":
        raise IndexPolicyError("标准知识库不能启用自动图谱构建")


def resolve_graph_action(
    *,
    platform_enabled: bool,
    index_mode: str,
    build_policy: str,
    graph_override: str,
) -> GraphAction:
    """解析文本索引完成后的图谱动作，不读取环境或数据库。"""

    validate_index_settings(index_mode, build_policy)
    if graph_override not in GRAPH_OVERRIDES:
        raise IndexPolicyError("无效的文档图谱参与方式")
    if index_mode == "standard":
        if graph_override == "include":
            raise IndexPolicyError("标准知识库中的文档不能强制参与图谱构建")
        return "excluded"
    if graph_override == "exclude":
        return "excluded"
    if not platform_enabled:
        return "pending"
    if graph_override == "include" and build_policy == "auto":
        return "enqueue"
    return "enqueue" if build_policy == "auto" else "pending"
