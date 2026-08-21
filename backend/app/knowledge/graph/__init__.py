"""Graph RAG 知识图谱模块（GRAPH_RAG_SPEC）。

图谱能力包：实体/关系/社区/事件抽取 + 构建流水线 + 图查询检索 + Agent 工具。
"""

from .graph_retriever import GraphRetriever

__all__ = ["GraphRetriever"]
