-- 初始化脚本：建库 / 权限（镜像已在 POSTGRES_* 建好 harness 库）
-- 预留：为 LangGraph checkpoint 建独立 schema
CREATE SCHEMA IF NOT EXISTS checkpoints AUTHORIZATION harness;

-- RAG：启用 pgvector 扩展（知识库向量检索）
CREATE EXTENSION IF NOT EXISTS vector;
