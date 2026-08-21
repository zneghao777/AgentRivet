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

export interface KnowledgeBaseItem {
  kb_id: string;
  name: string;
  description: string | null;
  doc_count: number;
  chunk_count: number;
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
  parse_source: "glm" | "local" | null;
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
  chunk_count: number;
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

// ---------- 混合检索 / 评测（AGENTIC_RAG_UPGRADE_SPEC §4） ----------

export type RetrievalStrategy = "auto" | "vector" | "keyword" | "hybrid";

export interface TestSearchRequest {
  query: string;
  top_k: number;
  strategy: string;
  rerank: boolean;
  expand_query?: boolean;
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
