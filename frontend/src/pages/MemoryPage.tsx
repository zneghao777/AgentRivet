// 长期记忆管理页（MEMORY_SPEC §8）：列表/时间线、开关、清理、编辑、停用/启用
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Button,
  Empty,
  Input,
  Modal,
  Popconfirm,
  Select,
  Skeleton,
  Switch,
  Tag,
  message,
} from "antd";
import { deleteData, getData, patchData } from "@/api/http";
import type {
  MemoryItem,
  MemoryListData,
  MemoryOverview,
  MemorySettings,
  MemoryTimelineData,
  MemoryType,
} from "@/types/api";
import { formatDayLabel, formatTime } from "@/utils/format";

const STATUS_LABELS: Record<string, string> = {
  active: "已启用",
  pending: "待确认",
  superseded: "已被取代",
  disabled: "已停用",
  expired: "已过期",
};

function statusColor(status: string): string {
  switch (status) {
    case "active":
      return "success";
    case "pending":
      return "warning";
    case "expired":
      return "error";
    case "superseded":
      return "purple";
    default:
      return "default";
  }
}

type ViewMode = "timeline" | "list";

export default function MemoryPage() {
  const [overview, setOverview] = useState<MemoryOverview | null>(null);
  const [settings, setSettings] = useState<MemorySettings>({
    long_term_enabled: true,
    auto_extract: true,
    ttl_days: 90,
  });
  const [listData, setListData] = useState<MemoryListData>({
    items: [],
    total: 0,
    page: 1,
    page_size: 20,
  });
  const [timeline, setTimeline] = useState<MemoryTimelineData>({ groups: [] });
  const [loading, setLoading] = useState(false);
  const [view, setView] = useState<ViewMode>("timeline");
  const [typeFilter, setTypeFilter] = useState<MemoryType | "all">("all");
  const [statusFilter, setStatusFilter] = useState<string>("active");
  const [keyword, setKeyword] = useState("");
  const [showNonActive, setShowNonActive] = useState(false);
  const [editItem, setEditItem] = useState<MemoryItem | null>(null);
  const [editing, setEditing] = useState(false);
  const [editText, setEditText] = useState("");
  const [togglingId, setTogglingId] = useState<number | null>(null);
  const [clearing, setClearing] = useState(false);

  const loadOverview = useCallback(async () => {
    try {
      const data = await getData<MemoryOverview>("/memory/overview");
      setOverview(data);
    } catch (e) {
      message.error(e instanceof Error ? e.message : "加载记忆统计失败");
    }
  }, []);

  const loadSettings = useCallback(async () => {
    try {
      const data = await getData<MemorySettings>("/memory/settings");
      setSettings(data);
    } catch (e) {
      message.error(e instanceof Error ? e.message : "加载记忆设置失败");
    }
  }, []);

  const load = useCallback(
    async (append = false) => {
      setLoading(true);
      try {
        if (view === "timeline") {
          const data = await getData<MemoryTimelineData>("/memory/timeline?days=30");
          setTimeline(data);
        } else {
          const params = new URLSearchParams({
            page: append ? String(listData.page + 1) : "1",
            page_size: "20",
          });
          if (typeFilter !== "all") params.set("memory_type", typeFilter);
          if (statusFilter) params.set("status", statusFilter);
          if (keyword.trim()) params.set("q", keyword.trim());
          const data = await getData<MemoryListData>(`/memory?${params.toString()}`);
          setListData((prev) =>
            append ? { ...data, items: [...prev.items, ...data.items] } : data
          );
        }
      } catch (e) {
        message.error(e instanceof Error ? e.message : "加载记忆失败");
      } finally {
        setLoading(false);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [view, typeFilter, statusFilter, keyword]
  );

  useEffect(() => {
    void loadOverview();
    void loadSettings();
  }, [loadOverview, loadSettings]);

  useEffect(() => {
    void load();
  }, [load]);

  // 时间线视图只展示 active（默认）；showNonActive 开启后展示全部状态
  const timelineGroups = useMemo(() => {
    if (view !== "timeline") return [];
    let groups = timeline.groups;
    if (!showNonActive) {
      groups = groups
        .map((g) => ({
          ...g,
          items: g.items.filter((m) => m.status === "active"),
        }))
        .filter((g) => g.items.length > 0);
    }
    if (typeFilter !== "all") {
      groups = groups
        .map((g) => ({
          ...g,
          items: g.items.filter((m) => m.memory_type === typeFilter),
        }))
        .filter((g) => g.items.length > 0);
    }
    if (keyword.trim()) {
      groups = groups
        .map((g) => ({
          ...g,
          items: g.items.filter((m) => m.content.includes(keyword.trim())),
        }))
        .filter((g) => g.items.length > 0);
    }
    return groups;
  }, [timeline, view, showNonActive, typeFilter, keyword]);

  const toggleLongTerm = async (checked: boolean) => {
    try {
      const data = await patchData<MemorySettings>("/memory/settings", {
        long_term_enabled: checked,
      });
      setSettings(data);
      message.success(checked ? "长期记忆已开启" : "长期记忆已关闭");
    } catch (e) {
      message.error(e instanceof Error ? e.message : "更新设置失败");
    }
  };

  const toggleItemStatus = async (item: MemoryItem) => {
    setTogglingId(item.memory_id);
    const nextStatus = item.status === "disabled" ? "active" : "disabled";
    try {
      const updated = await patchData<MemoryItem>(`/memory/${item.memory_id}`, {
        status: nextStatus,
      });
      await Promise.all([loadOverview(), load()]);
      void updated;
      message.success(nextStatus === "disabled" ? "已停用该记忆" : "已启用该记忆");
    } catch (e) {
      message.error(e instanceof Error ? e.message : "更新失败");
    } finally {
      setTogglingId(null);
    }
  };

  const confirmPending = async (item: MemoryItem) => {
    try {
      await patchData<MemoryItem>(`/memory/${item.memory_id}`, { status: "active" });
      message.success("记忆已确认启用");
      await Promise.all([loadOverview(), load()]);
    } catch (e) {
      message.error(e instanceof Error ? e.message : "确认失败");
    }
  };

  const removeItem = async (item: MemoryItem) => {
    try {
      await deleteData(`/memory/${item.memory_id}`);
      message.success("记忆已删除");
      await Promise.all([loadOverview(), load()]);
    } catch (e) {
      message.error(e instanceof Error ? e.message : "删除失败");
    }
  };

  const clearAll = async () => {
    setClearing(true);
    try {
      const data = await deleteData<{ deleted: number }>("/memory");
      message.success(`已清空 ${data.deleted} 条记忆`);
      await Promise.all([loadOverview(), load()]);
    } catch (e) {
      message.error(e instanceof Error ? e.message : "清空失败");
    } finally {
      setClearing(false);
    }
  };

  const openEdit = (item: MemoryItem) => {
    setEditItem(item);
    setEditText(item.content);
  };

  const saveEdit = async () => {
    if (!editItem) return;
    setEditing(true);
    try {
      await patchData<MemoryItem>(`/memory/${editItem.memory_id}`, { content: editText });
      message.success("记忆已保存，向量已重新生成");
      setEditItem(null);
      await Promise.all([loadOverview(), load()]);
    } catch (e) {
      message.error(e instanceof Error ? e.message : "保存失败");
    } finally {
      setEditing(false);
    }
  };

  const stats = overview ?? {
    total: 0,
    semantic_count: 0,
    episodic_count: 0,
    active_count: 0,
    pending_count: 0,
    disabled_count: 0,
    added_7d: 0,
    top_accessed: [],
  };

  const renderCard = (item: MemoryItem) => (
    <article
      className={`kb-chunk-row${item.status !== "active" ? " disabled" : ""}`}
      key={item.memory_id}
    >
      <div className="kb-chunk-head">
        <Tag
          color={item.memory_type === "semantic" ? "processing" : "geekblue"}
          bordered={false}
        >
          {item.memory_type === "semantic" ? "语义" : "情景"}
        </Tag>
        <Tag color={statusColor(item.status)} bordered={false}>
          {STATUS_LABELS[item.status] ?? item.status}
        </Tag>
        {item.status === "pending" ? (
          <span style={{ fontSize: 11, color: "#d48806" }}>低置信度，未参与检索</span>
        ) : null}
        <span className="kb-chunk-meta">
          置信度 {Math.round(item.confidence * 100)}% · 命中 {item.access_count} 次 · 更新 {formatTime(item.updated_at ?? item.created_at)}
        </span>
      </div>
      <div className="kb-chunk-text">{item.content}</div>
      <div className="kb-chunk-foot">
        {item.source_summary ? (
          <span className="mem-source">
            <span className="material-symbols-rounded">link</span>
            {item.source_summary}
          </span>
        ) : null}
        <div className="kb-chunk-actions">
          <Button size="small" type="text" onClick={() => openEdit(item)} title="编辑记忆">
            <span className="material-symbols-rounded" style={{ fontSize: 15 }}>edit_note</span>
          </Button>
          {item.status === "pending" ? (
            <Button size="small" type="primary" ghost onClick={() => void confirmPending(item)}>
              确认
            </Button>
          ) : null}
          {item.status === "active" || item.status === "disabled" ? (
            <Button
              size="small"
              type="text"
              loading={togglingId === item.memory_id}
              onClick={() => void toggleItemStatus(item)}
              title={item.status === "disabled" ? "启用" : "停用"}
            >
              <span className="material-symbols-rounded" style={{ fontSize: 15 }}>
                {item.status === "disabled" ? "play_circle" : "pause_circle"}
              </span>
            </Button>
          ) : null}
          <Popconfirm
            title="删除这条记忆？"
            description="删除后该记忆立即不再参与检索。"
            okText="删除"
            cancelText="取消"
            okButtonProps={{ danger: true }}
            onConfirm={() => void removeItem(item)}
          >
            <Button size="small" type="text" danger title="删除记忆">
              <span className="material-symbols-rounded" style={{ fontSize: 15 }}>delete</span>
            </Button>
          </Popconfirm>
        </div>
      </div>
    </article>
  );

  return (
    <div className="page-scroll">
      <header className="page-header">
        <div className="page-header-icon">
          <span className="material-symbols-rounded">memory</span>
        </div>
        <div className="page-header-copy">
          <h1>记忆</h1>
          <p>跨会话长期记忆：Agent 从对话中学习的事实与偏好（用户级）</p>
        </div>
        <Switch
          checked={settings.long_term_enabled}
          onChange={(checked) => void toggleLongTerm(checked)}
          checkedChildren="长期记忆开"
          unCheckedChildren="长期记忆关"
        />
        <Popconfirm
          title="清空全部记忆？"
          description="将物理删除当前用户全部记忆，此操作不可恢复。"
          okText="全部清除"
          cancelText="取消"
          okButtonProps={{ danger: true }}
          onConfirm={() => void clearAll()}
          disabled={stats.total === 0}
        >
          <Button danger disabled={stats.total === 0} loading={clearing}>
            <span className="material-symbols-rounded" style={{ fontSize: 15 }}>delete_sweep</span>
            全部清除
          </Button>
        </Popconfirm>
        <Button icon={<span className="material-symbols-rounded" style={{ fontSize: 16 }}>refresh</span>} onClick={() => void load()}>
          刷新
        </Button>
      </header>

      <div className="kb-grid" style={{ marginBottom: 20 }}>
        {[
          { label: "记忆总数", value: stats.total, icon: "memory" },
          { label: "语义记忆", value: stats.semantic_count, icon: "lightbulb" },
          { label: "情景记忆", value: stats.episodic_count, icon: "event_note" },
          { label: "近7日新增", value: stats.added_7d, icon: "trending_up" },
        ].map((card) => (
          <article className="kb-card mem-stat-card" key={card.label}>
            <div className="kb-card-head">
              <div className="kb-card-icon">
                <span className="material-symbols-rounded">{card.icon}</span>
              </div>
              <div className="kb-card-title">
                <div className="kb-card-name">{card.label}</div>
              </div>
            </div>
            <div className="mem-stat-value">{card.value}</div>
          </article>
        ))}
      </div>

      <div className="kb-chunk-panel">
        <div className="kb-panel-toolbar">
          <div className="kb-panel-title">
            <span className="material-symbols-rounded">psychology</span>
            <b>记忆管理</b>
            <Tag bordered={false}>{view === "list" ? listData.total : stats.active_count} 条启用</Tag>
          </div>
          <div className="kb-chunk-filters">
            <Select
              size="small"
              style={{ minWidth: 110 }}
              value={typeFilter}
              onChange={setTypeFilter}
              options={[
                { value: "all", label: "全部类型" },
                { value: "semantic", label: "语义记忆" },
                { value: "episodic", label: "情景记忆" },
              ]}
            />
            {view === "list" ? (
              <Select
                size="small"
                style={{ minWidth: 120 }}
                value={statusFilter}
                onChange={setStatusFilter}
                options={[
                  { value: "", label: "全部状态" },
                  { value: "active", label: "已启用" },
                  { value: "pending", label: "待确认" },
                  { value: "disabled", label: "已停用" },
                  { value: "expired", label: "已过期" },
                ]}
              />
            ) : (
              <Button
                size="small"
                type={showNonActive ? "primary" : "default"}
                onClick={() => setShowNonActive((v) => !v)}
              >
                查看非启用
              </Button>
            )}
            <Input
              size="small"
              allowClear
              prefix={<span className="material-symbols-rounded" style={{ fontSize: 14 }}>search</span>}
              placeholder="关键词搜索…"
              style={{ width: 180 }}
              value={keyword}
              onChange={(event) => setKeyword(event.target.value)}
            />
            <Select
              size="small"
              style={{ minWidth: 100 }}
              value={view}
              onChange={setView}
              options={[
                { value: "timeline", label: "时间线视图" },
                { value: "list", label: "列表视图" },
              ]}
            />
          </div>
        </div>

        {loading && listData.items.length === 0 ? (
          <div className="kb-skeleton">
            {Array.from({ length: 5 }).map((_, index) => (
              <div className="kb-skeleton-row" key={index} />
            ))}
          </div>
        ) : view === "timeline" ? (
          timelineGroups.length === 0 ? (
            <Empty description="暂无记忆。与 Agent 对话后，它将自动提炼值得长期记住的事实与偏好。" style={{ padding: 40 }} />
          ) : (
            <div className="mem-timeline">
              {timelineGroups.map((group) => (
                <section className="mem-day-group" key={group.date}>
                  <div className="mem-day-head">
                    <span className="mem-day-label">{formatDayLabel(group.date)}</span>
                    <span className="mem-day-date">{group.date}</span>
                    <span className="mem-day-count">{group.count} 条</span>
                  </div>
                  <div className="kb-chunk-list">
                    {group.items.map((item) => renderCard(item))}
                  </div>
                </section>
              ))}
            </div>
          )
        ) : listData.items.length === 0 ? (
          <Empty description="暂无匹配的记忆" style={{ padding: 40 }} />
        ) : (
          <div className="kb-chunk-list">
            {listData.items.map((item) => renderCard(item))}
            {listData.total > listData.items.length ? (
              <Button block type="link" onClick={() => void load(true)}>
                加载更多（{listData.items.length}/{listData.total}）
              </Button>
            ) : null}
          </div>
        )}
      </div>

      <Modal
        title={
          <div className="kb-modal-title">
            <span className="material-symbols-rounded">edit_note</span>
            编辑记忆 #{editItem?.memory_id}
          </div>
        }
        open={editItem !== null}
        onOk={() => void saveEdit()}
        onCancel={() => setEditItem(null)}
        okText="保存并重新向量化"
        cancelText="取消"
        confirmLoading={editing}
        width="min(760px, 92vw)"
        destroyOnHidden
      >
        {editItem ? (
          <>
            <div className="chunk-edit-meta">
              <Tag color={editItem.memory_type === "semantic" ? "processing" : "geekblue"} bordered={false}>
                {editItem.memory_type === "semantic" ? "语义记忆" : "情景记忆"}
              </Tag>
              <Tag color={statusColor(editItem.status)} bordered={false}>
                {STATUS_LABELS[editItem.status] ?? editItem.status}
              </Tag>
              <Tag bordered={false}>置信度 {Math.round(editItem.confidence * 100)}%</Tag>
              <Tag bordered={false}>命中 {editItem.access_count} 次</Tag>
            </div>
            <textarea
              className="chunk-edit-area"
              value={editText}
              onChange={(event) => setEditText(event.target.value)}
              rows={8}
              placeholder="记忆内容…"
            />
            <div className="chunk-edit-hint">
              <span className="material-symbols-rounded">info</span>
              保存后将重新生成该记忆的向量，Agent 检索将指向最新内容。
            </div>
          </>
        ) : (
          <Skeleton active />
        )}
      </Modal>
    </div>
  );
}
