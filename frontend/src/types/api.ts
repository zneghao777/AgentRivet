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
  source?: SkillCategory;
}

export type SkillCategory = "builtin" | "external";

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
  source_file: string;
  chunk_count: number;
  created_at: string | null;
}

export interface KnowledgeBaseDetail {
  kb: KnowledgeBaseItem;
  documents: KnowledgeDocItem[];
}

export interface KnowledgeUploadResponse {
  kb_id: string;
  doc_id: number;
  source_file: string;
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
}
