"""请求/响应 Pydantic 模型（与前端 types 一一对应）。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, SecretStr

# ---------- 通用响应包装 ----------


class ApiResponse(BaseModel):
    code: int = 0
    message: str = "ok"
    data: Any | None = None


class ApiError(BaseModel):
    code: int
    message: str
    data: Any | None = None


# ---------- 会话 ----------


class SessionCreateRequest(BaseModel):
    title: str | None = None
    agent_id: str | None = None


class SessionUpdateRequest(BaseModel):
    title: str | None = Field(default=None, max_length=128)
    agent_id: str | None = None


class AgentSummaryResponse(BaseModel):
    id: str
    name: str
    status: str


class SessionResponse(BaseModel):
    session_id: str
    thread_id: str
    title: str | None = None
    status: str
    agent_id: str | None = None
    agent: AgentSummaryResponse | None = None
    created_at: str | None = None
    updated_at: str | None = None


class SessionListData(BaseModel):
    items: list[SessionResponse]
    total: int
    page: int
    page_size: int


# ---------- 消息 ----------


class CapabilityRefRequest(BaseModel):
    kind: Literal["skill", "mcp_tool"]
    id: str


class ModelRuntimeConfigRequest(BaseModel):
    """仅用于本次模型调用；供应商密钥不持久化、不进入观测字段。"""

    base_url: str = Field(min_length=1, max_length=2048)
    api_key: SecretStr | None = Field(default=None, max_length=4096)


class MessageSendRequest(BaseModel):
    message: str = Field(min_length=1)
    skills: list[str] | None = None  # 本次会话使用的 Skill 白名单
    knowledge_bases: list[str] | None = None  # 本次会话绑定的知识库名称白名单（默认不启用 RAG）
    capabilities: list[CapabilityRefRequest] | None = None
    model: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:/-]+$",
    )
    provider_config: ModelRuntimeConfigRequest | None = None
    attachment_ids: list[str] | None = None
    agent_id: str | None = None


class MessageStreamRequest(BaseModel):
    message: str = Field(min_length=1)
    skills: list[str] | None = None
    knowledge_bases: list[str] | None = None
    capabilities: list[CapabilityRefRequest] | None = None
    model: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:/-]+$",
    )
    provider_config: ModelRuntimeConfigRequest | None = None
    attachment_ids: list[str] | None = None
    agent_id: str | None = None


class MessageResponse(BaseModel):
    session_id: str
    reply: str
    token_usage: dict[str, int] = Field(default_factory=dict)
    tool_calls: int = 0
    duration_ms: int = 0


class ModelConnectionTestRequest(BaseModel):
    model: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:/-]+$",
    )
    base_url: str | None = Field(default=None, max_length=2048)
    api_key: SecretStr | None = Field(default=None, max_length=4096)


class HistoryMessage(BaseModel):
    id: int
    role: str
    content: str
    tool_name: str | None = None
    token_usage: dict[str, int] | None = None
    trace_id: str | None = None
    request_id: str | None = None
    step_index: int | None = None
    agent_id: str | None = None
    agent_name: str | None = None
    created_at: str | None = None


# ---------- AgentHub ----------


class AgentCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=2000)
    system_prompt: str | None = Field(default=None, max_length=20000)
    model: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9._:/-]+$", max_length=128
    )
    model_base_url: str | None = Field(default=None, max_length=2048)
    memory_enabled: bool = True
    scope: Literal["user", "internal"] = "user"


class AgentUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, min_length=1, max_length=2000)
    system_prompt: str | None = Field(default=None, max_length=20000)
    model: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9._:/-]+$", max_length=128
    )
    model_base_url: str | None = Field(default=None, max_length=2048)
    memory_enabled: bool | None = None
    scope: Literal["user", "internal"] | None = None
    status: Literal["active", "disabled"] | None = None


class AgentBindingIdsRequest(BaseModel):
    skill_ids: list[int] | None = None
    tool_names: list[str] | None = None
    capability_ids: list[str] | None = None
    kb_ids: list[str] | None = None


class RouteDryRunRequest(BaseModel):
    message: str = Field(min_length=1, max_length=10000)


class ResumeRequest(BaseModel):
    input: str
    decision: str | None = None


# ---------- Skill ----------


class SkillResponse(BaseModel):
    name: str
    description: str
    version: str
    author: str
    tags: list[str]
    enabled: bool
    path: str
    source: str = "external"
    file_count: int = 1
    has_scripts: bool = False


# ---------- Tool ----------


class ToolResponse(BaseModel):
    name: str
    description: str
    source: str


# ---------- MCP / Capability hub ----------


class McpAuthRequest(BaseModel):
    type: Literal["none", "bearer", "headers"] = "none"
    secret_value: str | None = None
    headers: dict[str, str] | None = None


class McpServerCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    transport: Literal["streamable_http"] = "streamable_http"
    endpoint_url: str = Field(min_length=1, max_length=2048)
    auth: McpAuthRequest = Field(default_factory=McpAuthRequest)


class McpServerUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = None
    endpoint_url: str | None = Field(default=None, min_length=1, max_length=2048)
    auth: McpAuthRequest | None = None


class McpCapabilityUpdateRequest(BaseModel):
    enabled: bool | None = None
    approval_policy: Literal["never", "write", "always"] | None = None


class McpServerResponse(BaseModel):
    id: str
    name: str
    description: str | None = None
    transport: str
    endpoint_url: str
    auth_type: str
    credential_configured: bool
    enabled: bool
    health_status: str
    protocol_version: str | None = None
    capability_revision: int
    tool_count: int = 0
    enabled_tool_count: int = 0
    pending_tool_count: int = 0
    last_checked_at: str | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class McpCapabilityResponse(BaseModel):
    id: str
    server_id: str
    kind: str
    remote_name: str
    effective_name: str
    title: str | None = None
    description: str
    input_schema: dict
    annotations: dict
    enabled: bool
    risk_level: str
    approval_policy: str
    review_status: str
    removed_at: str | None = None


# ---------- Audit ----------


class AuditResponse(BaseModel):
    id: int
    tenant_id: str
    session_id: str | None = None
    user_id: str | None = None
    action: str
    detail: dict | None = None
    ip: str | None = None
    created_at: str | None = None


# ---------- Knowledge（RAG）----------


class KnowledgeBaseCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = None
    index_mode: Literal["standard", "graph_enhanced"] = "standard"
    graph_build_policy: Literal["auto", "manual"] = "manual"


class KnowledgeBaseStatusRequest(BaseModel):
    enabled: bool


class KnowledgeBaseResponse(BaseModel):
    kb_id: str
    name: str
    description: str | None = None
    doc_count: int = 0
    chunk_count: int = 0
    index_mode: str = "standard"
    graph_build_policy: str = "manual"
    index_status: str = "empty"
    graph_status: str = "disabled"
    graph_version: int = 1
    graph_meta: dict = Field(default_factory=dict)
    settings: dict = Field(default_factory=dict)
    entity_count: int = 0
    active_job_count: int = 0
    status: str
    created_at: str | None = None
    updated_at: str | None = None


class KnowledgeDocResponse(BaseModel):
    doc_id: int = 0
    kb_id: str | None = None
    source_file: str
    file_size: int = 0
    mime_type: str | None = None
    parse_status: str = "pending"
    parse_method: Literal["text", "ocr"] = "text"
    parse_source: str | None = None
    graph_override: str = "inherit"
    vector_status: str = "pending"
    graph_status: str = "excluded"
    vector_error: str | None = None
    graph_error: str | None = None
    vector_indexed_at: str | None = None
    graph_indexed_at: str | None = None
    graph_version: int | None = None
    content_hash: str | None = None
    chunk_count: int = 0
    error: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class KnowledgeSearchRequest(BaseModel):
    query: str = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=10)


class KnowledgeHit(BaseModel):
    text: str
    source_file: str
    chunk_index: int
    score: float
    kb_id: str | None = None
    chunk_id: int | None = None
    # 混合检索 / Rerank 扩展（AGENTIC_RAG_UPGRADE_SPEC §3.8）
    strategy: str = "vector"  # vector | keyword | hybrid | rerank
    rerank_score: float | None = None


class KnowledgeUploadResponse(BaseModel):
    kb_id: str
    doc_id: int
    source_file: str
    parse_status: str = "pending"
    parse_method: Literal["text", "ocr"]
    vector_status: str = "pending"
    graph_status: str = "excluded"
    chunk_count: int = 0
    job_ids: list[str] = Field(default_factory=list)


class KnowledgeIndexSettingsRequest(BaseModel):
    graph_build_policy: Literal["auto", "manual"] | None = None
    settings: dict | None = None


class KnowledgeIndexModeTransitionRequest(BaseModel):
    target_mode: Literal["standard", "graph_enhanced"]
    graph_build_policy: Literal["auto", "manual"] = "manual"
    build_existing_documents: bool = True


class DocumentGraphOverrideRequest(BaseModel):
    graph_override: Literal["inherit", "include", "exclude"]


# ---------- 分块治理（KB_MANAGEMENT_SPEC §3.5） ----------


class KnowledgeChunkItem(BaseModel):
    chunk_id: int
    kb_id: str
    doc_id: int | None = None
    source_file: str
    heading: str | None = None
    text: str
    char_count: int
    enabled: bool = True
    chunk_index: int = 0
    has_embedding: bool = False
    metadata: dict = Field(default_factory=dict)
    created_at: str | None = None
    updated_at: str | None = None


class ChunkListResponse(BaseModel):
    items: list[KnowledgeChunkItem]
    total: int
    page: int
    page_size: int


class ChunkPreviewRequest(BaseModel):
    text: str = Field(min_length=1)
    chunk_size: int | None = Field(default=None, ge=64, le=4096)


class ChunkPreviewItem(BaseModel):
    index: int
    heading: str | None = None
    text: str
    char_count: int


class ChunkUpdateRequest(BaseModel):
    text: str | None = None
    enabled: bool | None = None


class ChunkEnabledRequest(BaseModel):
    enabled: bool


class TestSearchRequest(BaseModel):
    query: str = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=10)
    # 混合检索 / Rerank / Query 改写（AGENTIC_RAG_UPGRADE_SPEC §3.8；默认值兼容旧前端）
    strategy: str = Field(default="auto")  # auto | vector | keyword | hybrid
    mode: Literal["auto", "text", "graph", "fused"] | None = None
    rerank: bool = True
    expand_query: bool = False
    graph_hop: int = Field(default=2, ge=1, le=3)


# ---------- 检索评测（AGENTIC_RAG_UPGRADE_SPEC §3.8） ----------


class EvaluateRequest(BaseModel):
    name: str = Field(min_length=1)
    # dataset: [{query, expected_chunk_ids?, expected_doc_ids?, reference_answer?}]
    dataset: list[dict] = Field(min_length=1)
    variants: list[str] | None = None  # 缺省 ["baseline", "hybrid", "hybrid+rerank"]
    judge: bool = False  # 是否追加 LLM-as-judge 端到端评分


# ---------- 长期记忆（MEMORY_SPEC §7.3） ----------


class MemoryItem(BaseModel):
    memory_id: int
    memory_type: str            # semantic / episodic
    content: str
    status: str
    confidence: float
    source_session_id: str | None = None
    source_summary: str | None = None
    access_count: int = 0
    last_accessed_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class MemoryListResponse(BaseModel):
    items: list[MemoryItem]
    total: int
    page: int
    page_size: int


class MemoryTimelineGroup(BaseModel):
    date: str                   # YYYY-MM-DD
    items: list[MemoryItem]
    count: int


class MemoryTimelineResponse(BaseModel):
    groups: list[MemoryTimelineGroup]


class MemoryGraphEdge(BaseModel):
    source: int
    target: int
    relation: Literal["semantic", "same_source"]
    strength: float


class MemoryGraphResponse(BaseModel):
    nodes: list[MemoryItem]
    edges: list[MemoryGraphEdge]
    similarity_threshold: float


class MemoryOverview(BaseModel):
    total: int
    semantic_count: int
    episodic_count: int
    active_count: int
    pending_count: int
    disabled_count: int
    added_7d: int
    top_accessed: list[MemoryItem]


class MemorySettings(BaseModel):
    long_term_enabled: bool = True
    auto_extract: bool = True
    ttl_days: int = 90


class MemoryUpdateRequest(BaseModel):
    content: str | None = None
    status: Literal["active", "disabled", "pending"] | None = None


class MemorySettingsUpdateRequest(BaseModel):
    long_term_enabled: bool | None = None
    auto_extract: bool | None = None
    ttl_days: int | None = Field(default=None, ge=7, le=365)


# ---------- 可观测性（OBSERVABILITY_SPEC §2.5） ----------


class ObservabilityOverview(BaseModel):
    """总览 Dashboard 数据。"""

    today_requests: int = 0
    today_success_rate: float = 1.0
    today_errors: int = 0
    today_tokens: dict[str, int] = Field(default_factory=lambda: {"prompt": 0, "completion": 0, "total": 0})
    today_latency_p50_ms: int = 0
    today_latency_p95_ms: int = 0
    active_sessions: int = 0
    # 近 24h 折线序列（每个点 {ts, requests, errors, tokens_prompt, tokens_completion, latency_p50_ms, latency_p95_ms}）
    trend: list[dict] = Field(default_factory=list)
    # 工具调用 Top（{tool_name, calls, success, avg_duration_ms}）
    tool_stats: list[dict] = Field(default_factory=list)
    # 最近异常 Trace（TraceListItem 子集）
    recent_errors: list[dict] = Field(default_factory=list)


class TraceListItem(BaseModel):
    trace_id: str
    span_id: str
    session_id: str | None = None
    span_name: str
    status: str
    started_at: str | None = None
    ended_at: str | None = None
    duration_ms: int = 0
    model: str | None = None
    token_usage: dict | None = None
    input_summary: str | None = None
    output_summary: str | None = None
    error: str | None = None
    span_count: int = 0
    tool_count: int = 0


class SpanNode(BaseModel):
    span_id: str
    parent_span_id: str | None = None
    span_name: str
    span_type: str
    status: str
    model: str | None = None
    token_usage: dict | None = None
    input_summary: str | None = None
    output_summary: str | None = None
    duration_ms: int = 0
    started_at: str | None = None
    ended_at: str | None = None
    error: str | None = None
    children: list[SpanNode] = Field(default_factory=list)


class TraceDetail(BaseModel):
    trace_id: str
    root: SpanNode | None = None
    token_usage: dict | None = None
    duration_ms: int = 0
    status: str = "ok"
    started_at: str | None = None
    ended_at: str | None = None
    error: str | None = None


class TraceListResponse(BaseModel):
    items: list[TraceListItem]
    total: int
    page: int
    page_size: int


class ToolLogItem(BaseModel):
    span_id: str
    trace_id: str
    session_id: str | None = None
    tool_name: str
    status: str
    input_summary: str | None = None
    output_summary: str | None = None
    duration_ms: int = 0
    started_at: str | None = None
    error: str | None = None


class ToolLogsResponse(BaseModel):
    items: list[ToolLogItem]
    total: int
    page: int
    page_size: int


class TrendPoint(BaseModel):
    ts: str
    requests: int = 0
    errors: int = 0
    tokens_prompt: int = 0
    tokens_completion: int = 0
    latency_p50_ms: int = 0
    latency_p95_ms: int = 0


# ---------- 可观测性 v2（OBSERVABILITY_SPEC §12）----------


class ScoreCreateRequest(BaseModel):
    score_name: str = Field(min_length=1)
    value: float
    source: str = "human"
    reason: str | None = None


class TraceExportRequest(BaseModel):
    trace_ids: list[str] = Field(default_factory=list)
    include_logs: bool = True
    include_scores: bool = True


class AlertRuleCreateRequest(BaseModel):
    name: str = Field(min_length=1)
    metric: str = Field(min_length=1)
    threshold: float
    comparison: str = ">"
    window_minutes: int = 15
    min_samples: int = 1
    enabled: bool = True


class AlertIncidentUpdateRequest(BaseModel):
    status: Literal["acknowledged", "resolved"] | None = None
    acknowledged_by: str | None = None


class PriceCreateRequest(BaseModel):
    model_provider: str | None = None
    model_name: str = Field(min_length=1)
    input_price_per_million: float = 0
    cached_input_price_per_million: float = 0
    output_price_per_million: float = 0
    currency: str = "USD"
    version: str | None = None


class EvaluationRunCreateRequest(BaseModel):
    name: str = Field(min_length=1)
    dataset: dict | None = None
    variants: list | None = None
