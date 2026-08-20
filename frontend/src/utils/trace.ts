// 溯源工具（KB_MANAGEMENT_SPEC §4.6 / §4.8）：
// - 解析 knowledge_search 工具结果 markdown 中的 `引用: KB-{kb_id}/{chunk_id}` 锚点
// - 构建溯源深链 URL

export interface ReferenceAnchor {
  kbId: string;
  chunkId: number;
  /** 来源文件（无则从锚点行解析出） */
  sourceFile?: string;
  /** 相似度分数 */
  score?: number;
}

const ANCHOR_RE = /KB-([0-9a-fA-F-]{36})\/(\d+)/g;
const SCORE_RE = /相关度\s*([0-9.]+)/;
const SOURCE_RE = /来源[:：]\s*([^\s|]+)/;

/** 从 markdown 文本中解析全部引用锚点（含来源/分数，若锚点行可解析）。 */
export function parseReferenceAnchors(markdown: string): ReferenceAnchor[] {
  const anchors: ReferenceAnchor[] = [];
  const re = new RegExp(ANCHOR_RE.source, "g");
  let match: RegExpExecArray | null;
  while ((match = re.exec(markdown)) !== null) {
    const kbId = match[1];
    const chunkId = Number.parseInt(match[2], 10);
    if (Number.isNaN(chunkId)) continue;
    // 定位锚点所在行，尝试解析来源与分数
    const lineStart = markdown.lastIndexOf("\n", match.index) + 1;
    const lineEnd = markdown.indexOf("\n", match.index);
    const line = markdown.slice(lineStart, lineEnd === -1 ? undefined : lineEnd);
    const source = line.match(SOURCE_RE)?.[1];
    const scoreRaw = line.match(SCORE_RE)?.[1];
    anchors.push({
      kbId,
      chunkId,
      sourceFile: source,
      score: scoreRaw ? Number.parseFloat(scoreRaw) : undefined,
    });
  }
  return anchors;
}

/** 构建溯源深链：跳转知识库分块页并定位高亮。 */
export function buildTraceUrl(
  kbId: string,
  chunkId: number,
  docId?: number | null
): string {
  const params = new URLSearchParams({ tab: "chunks", chunk: String(chunkId) });
  if (docId) params.set("doc", String(docId));
  return `/knowledge/${kbId}?${params.toString()}`;
}
