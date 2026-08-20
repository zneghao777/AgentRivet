// 分块列表（KB_MANAGEMENT_SPEC §4.3-B）：records-table 质感，可编辑/启停/重算/溯源定位高亮
import { useEffect, useRef, useState } from "react";
import { Button, Input, Select, Switch, Tag, message } from "antd";
import { getData, patchData, postData } from "@/api/http";
import type {
  ChunkListData,
  KnowledgeChunkItem,
  KnowledgeDocItem,
} from "@/types/api";
import ChunkEditModal from "./ChunkEditModal";

export default function ChunkRecordsTable({
  kbId,
  documents,
  focusChunkId,
  focusDocId,
  onFocusHandled,
}: {
  kbId: string;
  documents: KnowledgeDocItem[];
  focusChunkId?: number | null;
  focusDocId?: number | null;
  onFocusHandled?: () => void;
}) {
  const [data, setData] = useState<ChunkListData>({ items: [], total: 0, page: 1, page_size: 20 });
  const [loading, setLoading] = useState(false);
  const [docFilter, setDocFilter] = useState<number | undefined>(focusDocId ?? undefined);
  const [keyword, setKeyword] = useState("");
  const [enabledFilter, setEnabledFilter] = useState<boolean | undefined>(undefined);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [editChunk, setEditChunk] = useState<KnowledgeChunkItem | null>(null);
  const rowRefs = useRef<Map<number, HTMLDivElement>>(new Map());
  const focusedRef = useRef<number | null>(null);

  const load = async (page = 1, append = false) => {
    setLoading(true);
    try {
      const params = new URLSearchParams({ page: String(page), page_size: "50" });
      if (docFilter !== undefined) params.set("doc_id", String(docFilter));
      if (keyword.trim()) params.set("q", keyword.trim());
      if (enabledFilter !== undefined) params.set("enabled", String(enabledFilter));
      const result = await getData<ChunkListData>(`/knowledge-bases/${kbId}/chunks?${params.toString()}`);
      setData((prev) =>
        append
          ? { ...result, items: [...prev.items, ...result.items] }
          : result
      );
    } catch (e) {
      message.error(e instanceof Error ? e.message : "加载分块失败");
    } finally {
      setLoading(false);
    }
  };

  // 文档过滤变化时重置分页
  useEffect(() => {
    load(1);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [docFilter, keyword, enabledFilter]);

  // 溯源定位：加载后滚动高亮目标分块
  useEffect(() => {
    if (!focusChunkId) return;
    if (data.items.length === 0) return;
    if (focusedRef.current === focusChunkId) return;
    const el = rowRefs.current.get(focusChunkId);
    if (el) {
      focusedRef.current = focusChunkId;
      el.scrollIntoView({ behavior: "smooth", block: "center" });
      el.classList.add("chunk-focus");
      window.setTimeout(() => el.classList.remove("chunk-focus"), 2600);
      onFocusHandled?.();
    }
  }, [focusChunkId, data.items, onFocusHandled]);

  const toggleEnabled = async (chunk: KnowledgeChunkItem, enabled: boolean) => {
    // 乐观更新
    setData((prev) => ({
      ...prev,
      items: prev.items.map((c) => (c.chunk_id === chunk.chunk_id ? { ...c, enabled } : c)),
    }));
    try {
      const updated = await patchData<KnowledgeChunkItem>(
        `/knowledge-bases/${kbId}/chunks/${chunk.chunk_id}/enabled`,
        { enabled }
      );
      setData((prev) => ({
        ...prev,
        items: prev.items.map((c) => (c.chunk_id === chunk.chunk_id ? updated : c)),
      }));
    } catch (e) {
      // 失败回滚
      setData((prev) => ({
        ...prev,
        items: prev.items.map((c) => (c.chunk_id === chunk.chunk_id ? { ...c, enabled: !enabled } : c)),
      }));
      message.error(e instanceof Error ? e.message : "更新失败");
    }
  };

  const reembed = async (chunk: KnowledgeChunkItem) => {
    setBusyId(chunk.chunk_id);
    try {
      await postData(`/knowledge-bases/${kbId}/chunks/${chunk.chunk_id}/re-embed`);
      message.success(`分块 #${chunk.chunk_id} 向量已重新生成`);
    } catch (e) {
      message.error(e instanceof Error ? e.message : "重新向量化失败");
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="kb-chunk-panel">
      <div className="kb-panel-toolbar">
        <div className="kb-panel-title">
          <span className="material-symbols-rounded">grid_view</span>
          <b>分块管理</b>
          <Tag bordered={false}>{data.total} 个分块</Tag>
        </div>
        <div className="kb-chunk-filters">
          <Select
            size="small"
            allowClear
            placeholder="全部文档"
            style={{ minWidth: 150 }}
            value={docFilter}
            onChange={setDocFilter}
            options={documents.map((doc) => ({
              value: doc.doc_id,
              label: doc.source_file,
            }))}
          />
          <Select
            size="small"
            allowClear
            placeholder="全部状态"
            style={{ minWidth: 110 }}
            value={enabledFilter}
            onChange={setEnabledFilter}
            options={[
              { value: true, label: "已启用" },
              { value: false, label: "已停用" },
            ]}
          />
          <Input
            size="small"
            allowClear
            prefix={<span className="material-symbols-rounded" style={{ fontSize: 14 }}>search</span>}
            placeholder="关键词过滤…"
            style={{ width: 180 }}
            value={keyword}
            onChange={(event) => setKeyword(event.target.value)}
          />
          <Button size="small" icon={<span className="material-symbols-rounded" style={{ fontSize: 14 }}>refresh</span>} onClick={() => load(1)}>
            刷新
          </Button>
        </div>
      </div>

      {loading && data.items.length === 0 ? (
        <div className="kb-skeleton">
          {Array.from({ length: 5 }).map((_, index) => (
            <div className="kb-skeleton-row" key={index} />
          ))}
        </div>
      ) : data.items.length === 0 ? (
        <div className="kb-doc-empty">
          <span className="material-symbols-rounded">grid_view</span>
          <p>暂无分块。上传文档并完成解析后即可查看与管理分块。</p>
        </div>
      ) : (
        <div className="kb-chunk-list">
          {data.items.map((chunk) => (
            <div
              className={`kb-chunk-row${chunk.enabled ? "" : " disabled"}`}
              key={chunk.chunk_id}
              ref={(el) => {
                if (el) rowRefs.current.set(chunk.chunk_id, el);
                else rowRefs.current.delete(chunk.chunk_id);
              }}
              id={`chunk-${chunk.chunk_id}`}
            >
              <div className="kb-chunk-head">
                <span className="kb-chunk-index">#{chunk.chunk_index + 1}</span>
                {chunk.heading ? <Tag color="purple" bordered={false}>{chunk.heading}</Tag> : null}
                {!chunk.enabled ? <Tag color="default" bordered={false}>已停用</Tag> : null}
                <span className="kb-chunk-meta">
                  {chunk.char_count} 字符 · {chunk.source_file}
                </span>
              </div>
              <div className="kb-chunk-text">{chunk.text}</div>
              <div className="kb-chunk-actions">
                <Switch
                  size="small"
                  checked={chunk.enabled}
                  onChange={(checked) => void toggleEnabled(chunk, checked)}
                  checkedChildren="启用"
                  unCheckedChildren="停用"
                  aria-label={`${chunk.enabled ? "停用" : "启用"}分块 #${chunk.chunk_id}`}
                />
                <Button size="small" type="text" onClick={() => setEditChunk(chunk)} title="编辑分块">
                  <span className="material-symbols-rounded" style={{ fontSize: 15 }}>edit_note</span>
                </Button>
                <Button
                  size="small"
                  type="text"
                  loading={busyId === chunk.chunk_id}
                  onClick={() => void reembed(chunk)}
                  title="重新向量化"
                >
                  <span className="material-symbols-rounded" style={{ fontSize: 15 }}>cached</span>
                </Button>
              </div>
            </div>
          ))}
          {data.total > data.items.length ? (
            <Button block type="link" onClick={() => void load(data.page + 1, true)}>
              加载更多（{data.items.length}/{data.total}）
            </Button>
          ) : null}
        </div>
      )}

      <ChunkEditModal
        kbId={kbId}
        chunk={editChunk}
        open={editChunk !== null}
        onClose={() => setEditChunk(null)}
        onSaved={(updated) => {
          setData((prev) => ({
            ...prev,
            items: prev.items.map((c) => (c.chunk_id === updated.chunk_id ? updated : c)),
          }));
        }}
      />
    </div>
  );
}
