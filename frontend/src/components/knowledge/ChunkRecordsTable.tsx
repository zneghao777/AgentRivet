import { useCallback, useDeferredValue, useEffect, useRef, useState } from "react";
import { Button, Input, Pagination, Select, Switch, Tag, message } from "antd";
import { getData, patchData, postData } from "@/api/http";
import type { ChunkListData, KnowledgeChunkItem, KnowledgeDocItem } from "@/types/api";
import { formatTime } from "@/utils/format";
import ChunkEditModal from "./ChunkEditModal";

function metadataValue(value: unknown) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

export default function ChunkRecordsTable({ kbId, documents, focusChunkId, focusDocId, onFocusHandled }: { kbId: string; documents: KnowledgeDocItem[]; focusChunkId?: number | null; focusDocId?: number | null; onFocusHandled?: () => void }) {
  const [data, setData] = useState<ChunkListData>({ items: [], total: 0, page: 1, page_size: 12 });
  const [loading, setLoading] = useState(false);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(12);
  const [docFilter, setDocFilter] = useState<number | undefined>(focusDocId ?? undefined);
  const [keyword, setKeyword] = useState("");
  const deferredKeyword = useDeferredValue(keyword.trim());
  const [enabledFilter, setEnabledFilter] = useState<boolean | undefined>(undefined);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [editChunk, setEditChunk] = useState<KnowledgeChunkItem | null>(null);
  const [expandedIds, setExpandedIds] = useState<Set<number>>(() => new Set());
  const rowRefs = useRef<Map<number, HTMLDivElement>>(new Map());
  const focusedRef = useRef<number | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
      if (docFilter !== undefined) params.set("doc_id", String(docFilter));
      if (deferredKeyword) params.set("q", deferredKeyword);
      if (enabledFilter !== undefined) params.set("enabled", String(enabledFilter));
      setData(await getData<ChunkListData>(`/knowledge-bases/${kbId}/chunks?${params.toString()}`));
    } catch (error) {
      message.error(error instanceof Error ? error.message : "加载分块失败");
    } finally {
      setLoading(false);
    }
  }, [deferredKeyword, docFilter, enabledFilter, kbId, page, pageSize]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    if (focusDocId === null || focusDocId === undefined) return;
    setDocFilter(focusDocId);
    setPage(1);
  }, [focusDocId]);

  useEffect(() => {
    if (!focusChunkId || data.items.length === 0 || focusedRef.current === focusChunkId) return;
    const element = rowRefs.current.get(focusChunkId);
    if (!element) return;
    focusedRef.current = focusChunkId;
    element.scrollIntoView({ behavior: "smooth", block: "center" });
    element.classList.add("chunk-focus");
    window.setTimeout(() => element.classList.remove("chunk-focus"), 2600);
    onFocusHandled?.();
  }, [data.items, focusChunkId, onFocusHandled]);

  const selectedDocument = documents.find((doc) => doc.doc_id === docFilter) ?? null;
  const pageStart = data.total === 0 ? 0 : (page - 1) * pageSize + 1;
  const pageEnd = Math.min(page * pageSize, data.total);

  const toggleEnabled = async (chunk: KnowledgeChunkItem, enabled: boolean) => {
    setData((current) => ({ ...current, items: current.items.map((item) => item.chunk_id === chunk.chunk_id ? { ...item, enabled } : item) }));
    try {
      const updated = await patchData<KnowledgeChunkItem>(`/knowledge-bases/${kbId}/chunks/${chunk.chunk_id}/enabled`, { enabled });
      setData((current) => ({ ...current, items: current.items.map((item) => item.chunk_id === chunk.chunk_id ? updated : item) }));
    } catch (error) {
      setData((current) => ({ ...current, items: current.items.map((item) => item.chunk_id === chunk.chunk_id ? { ...item, enabled: !enabled } : item) }));
      message.error(error instanceof Error ? error.message : "更新失败");
    }
  };

  const reembed = async (chunk: KnowledgeChunkItem) => {
    setBusyId(chunk.chunk_id);
    try {
      await postData(`/knowledge-bases/${kbId}/chunks/${chunk.chunk_id}/re-embed`);
      message.success(`分块 #${chunk.chunk_index + 1} 向量已重新生成`);
      await load();
    } catch (error) {
      message.error(error instanceof Error ? error.message : "重新向量化失败");
    } finally {
      setBusyId(null);
    }
  };

  const toggleExpanded = (chunkId: number) => {
    setExpandedIds((current) => {
      const next = new Set(current);
      if (next.has(chunkId)) next.delete(chunkId); else next.add(chunkId);
      return next;
    });
  };

  return (
    <div className="kb-chunk-panel kb-chunk-workbench">
      <header className="kb-chunk-workbench-header">
        <div><span className="eyebrow">CHUNK RECORDS</span><h2>分块管理</h2><p>按文档和块序号升序展示；编辑内容后将自动重新生成向量。</p></div>
        <div className="kb-chunk-range"><strong>{data.total}</strong><span>个分块</span><small>当前 {pageStart}–{pageEnd}</small></div>
      </header>

      {selectedDocument ? <div className="kb-chunk-document-context"><span className="material-symbols-rounded">description</span><div><small>正在查看文档</small><b>{selectedDocument.source_file}</b><span>{selectedDocument.chunk_count} 个分块 · 文档 ID {selectedDocument.doc_id}</span></div><Button onClick={() => { setDocFilter(undefined); setPage(1); }}>查看全部文档</Button></div> : null}

      <div className="kb-chunk-toolbar">
        <Select allowClear placeholder="全部文档" value={docFilter} onChange={(value) => { setDocFilter(value); setPage(1); }} options={documents.map((doc) => ({ value: doc.doc_id, label: `${doc.source_file} · ${doc.chunk_count}` }))} />
        <Select allowClear placeholder="全部状态" value={enabledFilter} onChange={(value) => { setEnabledFilter(value); setPage(1); }} options={[{ value: true, label: "已启用" }, { value: false, label: "已停用" }]} />
        <Input allowClear prefix={<span className="material-symbols-rounded">search</span>} placeholder="搜索分块正文" value={keyword} onChange={(event) => { setKeyword(event.target.value); setPage(1); }} />
        <Button icon={<span className="material-symbols-rounded">refresh</span>} onClick={() => void load()} loading={loading}>刷新</Button>
      </div>

      {loading && data.items.length === 0 ? <div className="kb-skeleton">{Array.from({ length: 5 }, (_, index) => <div className="kb-skeleton-row" key={index} />)}</div> : data.items.length === 0 ? <div className="kb-doc-empty"><span className="material-symbols-rounded">grid_view</span><p>没有符合当前筛选条件的分块。</p>{docFilter || keyword || enabledFilter !== undefined ? <Button onClick={() => { setDocFilter(undefined); setKeyword(""); setEnabledFilter(undefined); setPage(1); }}>清除筛选</Button> : null}</div> : (
        <div className={`kb-chunk-list${loading ? " is-loading" : ""}`} aria-live="polite">
          {data.items.map((chunk) => {
            const metadata = Object.entries(chunk.metadata ?? {}).filter(([key]) => !["heading", "source_file", "chunk_index"].includes(key)).slice(0, 5);
            const expanded = expandedIds.has(chunk.chunk_id);
            return (
              <div className={`kb-chunk-row kb-context-chunk${chunk.enabled ? "" : " disabled"}`} key={chunk.chunk_id} ref={(element) => { if (element) rowRefs.current.set(chunk.chunk_id, element); else rowRefs.current.delete(chunk.chunk_id); }} id={`chunk-${chunk.chunk_id}`}>
                <div className="kb-context-chunk-index"><small>BLOCK</small><strong>{String(chunk.chunk_index + 1).padStart(2, "0")}</strong></div>
                <div className="kb-context-chunk-main">
                  <header>
                    <div><span className="material-symbols-rounded">description</span><b>{chunk.source_file}</b>{chunk.heading ? <Tag color="blue" bordered={false}>{chunk.heading}</Tag> : null}</div>
                    <div>{chunk.has_embedding ? <Tag color="success" bordered={false}>向量就绪</Tag> : <Tag color="warning" bordered={false}>无向量</Tag>}{!chunk.enabled ? <Tag bordered={false}>已停用</Tag> : null}</div>
                  </header>
                  <div className={`kb-context-chunk-text${expanded ? " expanded" : ""}`}>{chunk.text}</div>
                  {chunk.text.length > 260 ? <button type="button" className="kb-chunk-expand" onClick={() => toggleExpanded(chunk.chunk_id)}>{expanded ? "收起正文" : "展开完整正文"}<span className="material-symbols-rounded">{expanded ? "expand_less" : "expand_more"}</span></button> : null}
                  <div className="kb-context-metadata">
                    <span><small>块 ID</small><b>{chunk.chunk_id}</b></span>
                    <span><small>文档 ID</small><b>{chunk.doc_id ?? "—"}</b></span>
                    <span><small>字符数</small><b>{chunk.char_count.toLocaleString()}</b></span>
                    <span><small>创建时间</small><b>{formatTime(chunk.created_at)}</b></span>
                    <span><small>更新时间</small><b>{formatTime(chunk.updated_at)}</b></span>
                  </div>
                  {metadata.length ? <div className="kb-context-meta-chips">{metadata.map(([key, value]) => <span key={key}><b>{key}</b>{metadataValue(value)}</span>)}</div> : null}
                  <footer>
                    <label><Switch size="small" checked={chunk.enabled} onChange={(checked) => void toggleEnabled(chunk, checked)} /><span>{chunk.enabled ? "参与检索" : "不参与检索"}</span></label>
                    <div><Button onClick={() => setEditChunk(chunk)} icon={<span className="material-symbols-rounded">edit_note</span>}>编辑</Button><Button loading={busyId === chunk.chunk_id} onClick={() => void reembed(chunk)} icon={<span className="material-symbols-rounded">cached</span>}>重算向量</Button></div>
                  </footer>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {data.total > 0 ? <footer className="kb-chunk-pagination"><span>共 {data.total} 个分块，按块序号升序</span><Pagination current={page} pageSize={pageSize} total={data.total} showSizeChanger pageSizeOptions={["12", "24", "48"]} showQuickJumper onChange={(nextPage, nextSize) => { setPage(nextPage); if (nextSize !== pageSize) { setPageSize(nextSize); setPage(1); } }} /></footer> : null}

      <ChunkEditModal kbId={kbId} chunk={editChunk} open={editChunk !== null} onClose={() => setEditChunk(null)} onSaved={(updated) => setData((current) => ({ ...current, items: current.items.map((item) => item.chunk_id === updated.chunk_id ? updated : item) }))} />
    </div>
  );
}
