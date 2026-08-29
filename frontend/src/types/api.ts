// 与后端 schemas.py 一一对应的 TS 类型

export interface ApiResponse<T = unknown> {
  code: number;
  message: string;
  data: T | null;
}

export interface SessionItem {
  session_id: string;
  thread_id: string;
  title: string | null;
  status: string;
  agent_id: string | null;
  agent?: { id: string; name: string; status: AgentStatus } | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface SessionListData {
  items: SessionItem[];
  total: number;
  page: number;
  page_size: number;
}

export interface MessageSendRequest {
  message: string;
  skills?: string[];
  knowledge_bases?: string[];
}

export interface MessageSendResponse {
  session_id: string;
  reply: string;
  token_usage: Record<string, number>;
  tool_calls: number;
  duration_ms: number;
}

export interface HistoryMessage {
  id: number;
  role: "user" | "assistant" | "tool";
  content: string;
  tool_name: string | null;
  token_usage: Record<string, number> | null;
  trace_id: string | null;
  request_id: string | null;
  step_index: number | null;
  agent_id: string | null;
  agent_name: string | null;
  created_at: string | null;
}

export interface ResumeRequest {
  input: string;
  decision?: string;
}

export interface SkillItem {
  name: string;
  description: string;
  version: string;
  author: string;
  tags: string[];
  enabled: boolean;
  path: string;
  source: SkillCategory;
  file_count: number;
  has_scripts: boolean;
}

export type SkillCategory = "builtin" | "external";

export interface SkillImportResponse {
  skills: SkillItem[];
  replaced: string[];
  warnings: string[];
}

export interface ToolItem {
  name: string;
  description: string;
  source: string;
}

export type McpHealthStatus = "unknown" | "checking" | "ready" | "degraded" | "error" | "disabled";

export interface McpServerItem {
  id: string;
  name: string;
  description: string | null;
  transport: "streamable_http";
  endpoint_url: string;
  auth_type: "none" | "bearer" | "headers";
  credential_configured: boolean;
  enabled: boolean;
  health_status: McpHealthStatus;
  protocol_version: string | null;
  capability_revision: number;
  tool_count: number;
  enabled_tool_count: number;
  pending_tool_count: number;
  last_checked_at: string | null;
  last_error_code: string | null;
  last_error_message: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface McpCapabilityItem {
  id: string;
  server_id: string;
  kind: "tool" | "resource" | "prompt";
  remote_name: string;
  effective_name: string;
  title: string | null;
  description: string;
  input_schema: Record<string, unknown>;
  annotations: Record<string, unknown>;
  enabled: boolean;
  risk_level: "low" | "medium" | "high";
  approval_policy: "never" | "write" | "always";
  review_status: "pending" | "approved" | "changed";
  removed_at: string | null;
}

export interface CapabilitySummary {
  skills: { total: number; enabled: number; with_scripts: number };
  mcp_servers: { total: number; ready: number; degraded: number };
  needs_attention: number;
}

export interface AuditItem {
  id: number;
  tenant_id: string;
  session_id: string | null;
  user_id: string | null;
  action: string;
  detail: Record<string, unknown> | null;
  ip: string | null;
  created_at: string | null;
}

export type IndexMode = "standard" | "graph_enhanced";
export type GraphBuildPolicy = "auto" | "manual";
export type IndexStatus = "empty" | "indexing" | "ready" | "partial" | "failed" | "stale";
export type GraphStatusValue = "disabled" | "none" | "pending" | "building" | "ready" | "partial" | "failed" | "stale";
export type VectorStatus = "pending" | "building" | "ready" | "failed" | "stale";
export type DocumentGraphStatus = "excluded" | "pending" | "building" | "ready" | "failed" | "stale";

export interface KnowledgeBaseItem {
  kb_id: string;
  name: string;
  description: string | null;
  doc_count: number;
  chunk_count: number;
  index_mode: IndexMode;
  graph_build_policy: GraphBuildPolicy;
  index_status: IndexStatus;
  graph_status: GraphStatusValue;
  graph_version: number;
  graph_meta: Record<string, number | string | null | undefined>;
  settings: Record<string, unknown>;
  entity_count: number;
  active_job_count: number;
  status: string;
  created_at: string | null;
  updated_at: string | null;
}

export interface KnowledgeDocItem {
  doc_id: number;
  kb_id?: string | null;
  source_file: string;
  file_size: number;
  mime_type: string | null;
  parse_status: "pending" | "parsing" | "parsed" | "failed" | "reparsing";
  parse_method: "text" | "ocr";
  parse_source: "glm" | "local" | null;
  graph_override: "inherit" | "include" | "exclude";
  vector_status: VectorStatus;
  graph_status: DocumentGraphStatus;
  vector_error: string | null;
  graph_error: string | null;
  vector_indexed_at: string | null;
  graph_indexed_at: string | null;
  graph_version: number | null;
  content_hash: string | null;
  chunk_count: number;
  error: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface KnowledgeBaseDetail {
  kb: KnowledgeBaseItem;
  documents: KnowledgeDocItem[];
}

export interface KnowledgeUploadResponse {
  kb_id: string;
  doc_id: number;
  source_file: string;
  parse_status: string;
  parse_method: "text" | "ocr";
  vector_status: VectorStatus;
  graph_status: DocumentGraphStatus;
  chunk_count: number;
  job_ids: string[];
}

export interface KnowledgeIndexJob {
  job_id: string;
  kb_id: string;
  doc_id: number | null;
  job_type: "parse" | "vectorize" | "graph_build" | "graph_cleanup";
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled";
  stage: string;
  progress: number;
  attempt: number;
  max_attempts: number;
  error_code: string | null;
  error_message: string | null;
  meta: Record<string, unknown>;
  started_at: string | null;
  finished_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface KnowledgeSearchRequest {
  query: string;
  top_k?: number;
}

export interface KnowledgeHit {
  text: string;
  source_file: string;
  chunk_index: number;
  score: number;
  kb_id?: string | null;
  chunk_id?: number | null;
}

// ---------- 分块治理（KB_MANAGEMENT_SPEC §4.8） ----------

export interface KnowledgeChunkItem {
  chunk_id: number;
  kb_id: string;
  doc_id: number | null;
  source_file: string;
  heading: string | null;
  text: string;
  char_count: number;
  enabled: boolean;
  chunk_index: number;
  has_embedding: boolean;
  metadata: Record<string, unknown>;
  created_at: string | null;
  updated_at: string | null;
}

export interface ChunkListData {
  items: KnowledgeChunkItem[];
  total: number;
  page: number;
  page_size: number;
}

export interface ChunkPreviewItem {
  index: number;
  heading: string | null;
  text: string;
  char_count: number;
}

export interface RetrieveHit {
  chunk_id: number;
  kb_id: string;
  source_file: string;
  text: string;
  score: number;
  chunk_index: number;
  strategy: string; // vector | keyword | hybrid | rerank
  rerank_score: number | null;
}

// ---------- Graph RAG 知识图谱（GRAPH_RAG_SPEC §6） ----------

export interface GraphStatus {
  kb_id: string;
  graph_status: GraphStatusValue;
  platform_enabled?: boolean;
  graph_meta?: {
    entities?: number;
    relations?: number;
    events?: number;
    communities?: number;
    chunks_processed?: number;
    error?: string;
  } | null;
}

export interface GraphEntityItem {
  id: string;
  kb_id: string;
  doc_id: number | null;
  entity_type: string;
  name: string;
  canonical_name: string;
  description: string | null;
  meta?: Record<string, unknown>;
  chunk_ids?: number[];
  created_at?: string | null;
  updated_at?: string | null;
}

export interface GraphEntityListData {
  items: GraphEntityItem[];
  total: number;
  page: number;
  page_size: number;
}

export interface GraphRelation {
  relation_id?: string;
  source_entity_id: string;
  source_name: string;
  target_entity_id: string;
  target_name: string;
  relation_type: string;
  description?: string | null;
  weight: number;
  source_chunk_id?: number | null;
  created_at?: string | null;
}

export interface GraphEventItem {
  event_id: string;
  subject_entity_id?: string;
  subject_name?: string;
  order_no: number | null;
  predicate: string | null;
  object: string | null;
  summary: string | null;
  event_type: string | null;
  source_chunk_id?: number | null;
  created_at?: string | null;
}

export interface GraphRelationListData {
  items: GraphRelation[];
  total: number;
  page: number;
  page_size: number;
}

export interface GraphEventListData {
  items: GraphEventItem[];
  total: number;
  page: number;
  page_size: number;
}

export interface GraphSearchResult {
  entities?: GraphEntityItem[];
  relations: GraphRelation[];
  communities?: { summary: string; keywords?: string[]; level?: number }[];
  events?: GraphEventItem[];
  evidence_chunk_ids: number[];
  strategy: string;
}

export interface GraphSubgraph {
  nodes: Array<{ id: string; name: string; entity_type: string; description?: string | null }>;
  edges: Array<{ id: string; source: string; target: string; relation_type: string; source_chunk_id?: number | null }>;
}

// ---------- 混合检索 / 评测（AGENTIC_RAG_UPGRADE_SPEC §4） ----------

export type RetrievalStrategy = "auto" | "vector" | "keyword" | "hybrid";

export interface TestSearchRequest {
  query: string;
  top_k: number;
  strategy: string;
  rerank: boolean;
  expand_query?: boolean;
}

export interface UnifiedRetrieveResult {
  query: string;
  requested_mode: "auto" | "text" | "graph" | "fused";
  resolved_mode: "text" | "graph" | "fused";
  hits: Array<RetrieveHit & { doc_id?: number | null; channels: Array<"text" | "graph"> }>;
  text_hits: RetrieveHit[];
  graph_context: {
    entities: unknown[];
    relations: GraphRelation[];
    communities: Array<{ summary: string; keywords?: string[]; level?: number }>;
    events: GraphEventItem[];
  };
  evidence_chunks: Array<{ chunk_id: number; kb_id: string; doc_id: number | null; source_file: string; text: string }>;
  citations: Array<Record<string, unknown>>;
  degradations: string[];
  timings: { text_ms: number; graph_ms: number; total_ms: number };
}

export interface EvaluationCaseResult {
  query: string;
  hit: boolean;
  rank: number; // 首个命中排名（0 = 未命中）
  top_chunk_ids: number[];
}

export interface EvaluationVariantResult {
  variant: string;
  hit_rate: number;
  mrr: number;
  cases: EvaluationCaseResult[];
}

export interface EvaluationJudgeCase {
  query: string;
  faithfulness: number;
  relevancy: number;
}

export interface EvaluationJudgeSummary {
  avg_faithfulness: number;
  avg_relevancy: number;
  cases: EvaluationJudgeCase[];
}

export interface EvaluationResults {
  variants: EvaluationVariantResult[];
  judge: EvaluationJudgeSummary | null;
  stats: { total: number; valid: number; skipped: number };
}

export interface EvaluationRunItem {
  evaluation_id: number;
  name: string;
  status: string;
  variants: string[] | null;
  results: EvaluationResults | null;
  created_at: string | null;
}

// ---------- 长期记忆（MEMORY_SPEC §8.5） ----------

export type MemoryType = "semantic" | "episodic";
export type MemoryStatus = "active" | "pending" | "superseded" | "disabled" | "expired";

export interface MemoryItem {
  memory_id: number;
  memory_type: MemoryType;
  content: string;
  status: MemoryStatus;
  confidence: number;
  source_session_id: string | null;
  source_summary: string | null;
  access_count: number;
  last_accessed_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface MemoryListData {
  items: MemoryItem[];
  total: number;
  page: number;
  page_size: number;
}

export interface MemoryTimelineGroup {
  date: string;
  items: MemoryItem[];
  count: number;
}

export interface MemoryTimelineData {
  groups: MemoryTimelineGroup[];
}

export interface MemoryGraphEdge {
  source: number;
  target: number;
  relation: "semantic" | "same_source";
  strength: number;
}

export interface MemoryGraphData {
  nodes: MemoryItem[];
  edges: MemoryGraphEdge[];
  similarity_threshold: number;
}

export interface MemoryOverview {
  total: number;
  semantic_count: number;
  episodic_count: number;
  active_count: number;
  pending_count: number;
  disabled_count: number;
  added_7d: number;
  top_accessed: MemoryItem[];
}

export interface MemorySettings {
  long_term_enabled: boolean;
  auto_extract: boolean;
  ttl_days: number;
}

// ---------- AgentHub ----------

export type AgentStatus = "active" | "disabled" | "archived";
export type AgentScope = "user" | "internal";

export interface AgentBindingCounts {
  skills: number;
  tools: number;
  mcp: number;
  kbs: number;
}

export interface AgentBindings {
  skills: Array<{ id: number; name: string; enabled: boolean; invalid: boolean }>;
  tools: string[];
  mcp_capabilities: Array<{ id: string; server_id: string; server_name: string; name: string; effective_name: string; enabled: boolean; invalid: boolean }>;
  knowledge_bases: Array<{ id: string; name: string; enabled: boolean; invalid: boolean }>;
}

export interface AgentItem {
  id: string;
  name: string;
  description: string;
  system_prompt?: string | null;
  model: string | null;
  model_base_url: string | null;
  memory_enabled: boolean;
  scope: AgentScope;
  status: AgentStatus;
  bindings: AgentBindings;
  binding_counts: AgentBindingCounts;
  session_count: number;
  last_used_at: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface AgentListData {
  items: AgentItem[];
  total: number;
  page: number;
  page_size: number;
}

export interface AgentOptions {
  skills: Array<{ id: number; name: string; description: string; enabled: boolean }>;
  tools: ToolItem[];
  mcp_servers: Array<{
    server_id: string;
    server_name: string;
    enabled: boolean;
    health_status: McpHealthStatus;
    capabilities: Array<{ id: string; name: string; description: string; enabled: boolean }>;
  }>;
  knowledge_bases: Array<{ id: string; name: string; description: string | null }>;
}

export interface RouteDryRunResult {
  mode: "direct" | "orchestrate";
  target_agent: string;
  confidence: number;
  reason: string;
  needs_clarification: boolean;
  clarification_question?: string | null;
  clarification_options: string[];
  plan?: {
    summary: string;
    execution: "parallel" | "sequential";
    tasks: Array<{
      task_id: string;
      target_agent: string;
      goal: string;
      constraints: string[];
    }>;
  } | null;
}
