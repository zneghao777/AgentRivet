// 历史会话页
import { useEffect, useState } from "react";
import { Button, Empty, Pagination, Space, Tag, message } from "antd";
import { deleteData, getData } from "@/api/http";
import type { SessionItem, SessionListData } from "@/types/api";

export default function SessionsPage() {
  const [data, setData] = useState<SessionListData | null>(null);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);

  const load = async (p: number) => {
    setLoading(true);
    try {
      const d = await getData<SessionListData>(
        `/sessions?page=${p}&page_size=20`
      );
      setData(d);
    } catch (e) {
      message.error(e instanceof Error ? e.message : "加载会话失败");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load(page);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page]);

  const items = data?.items ?? [];

  return (
    <div
      style={{
        flex: 1,
        minWidth: 0,
        height: "100%",
        overflowY: "auto",
        padding: "28px 32px 60px",
      }}
    >
      <header
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: 24,
        }}
      >
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 14,
            padding: "14px 22px",
            background:
              "linear-gradient(135deg, rgba(0,80,203,0.06), rgba(0,184,169,0.04))",
            borderRadius: "var(--r-2xl)",
            border: "1px solid var(--brand-100)",
            flex: 1,
            marginRight: 16,
          }}
        >
          <div
            style={{
              width: 44,
              height: 44,
              borderRadius: 12,
              background:
                "linear-gradient(135deg, var(--brand-500), var(--brand-400))",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              boxShadow: "var(--shadow-brand)",
            }}
          >
            <span
              className="material-symbols-rounded icon-filled"
              style={{ color: "#fff", fontSize: 22 }}
            >
              history
            </span>
          </div>
          <div>
            <div
              style={{
                fontFamily: "var(--font-display)",
                fontSize: 20,
                fontWeight: 700,
                color: "var(--gray-900)",
              }}
            >
              历史会话
            </div>
            <div
              style={{
                fontSize: 12,
                color: "var(--gray-500)",
                marginTop: 4,
              }}
            >
              共 {data?.total ?? 0} 条会话记录
            </div>
          </div>
        </div>
      </header>

      {loading && (
        <div
          style={{
            textAlign: "center",
            padding: 60,
            color: "var(--gray-400)",
          }}
        >
          加载中…
        </div>
      )}
      {!loading && items.length === 0 && <Empty description="暂无历史会话" />}

      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fill, minmax(260px, 1fr))",
          gap: 12,
        }}
      >
        {items.map((s) => (
          <SessionCard key={s.session_id} item={s} onDelete={async () => {
            try {
              await deleteData(`/sessions/${s.session_id}`);
              message.success("已删除");
              load(page);
            } catch (e) {
              message.error(e instanceof Error ? e.message : "删除失败");
            }
          }} />
        ))}
      </div>

      {data && data.total > data.page_size && (
        <div style={{ display: "flex", justifyContent: "center", marginTop: 20 }}>
          <Pagination
            current={page}
            total={data.total}
            pageSize={data.page_size}
            onChange={setPage}
          />
        </div>
      )}
    </div>
  );
}

function SessionCard({
  item,
  onDelete,
}: {
  item: SessionItem;
  onDelete: () => void;
}) {
  return (
    <div
      style={{
        background: "var(--bg-card)",
        borderRadius: "var(--r-xl)",
        border: "var(--border-soft)",
        padding: 16,
        boxShadow: "var(--shadow-xs)",
        transition:
          "transform var(--dur-base) var(--ease-out), box-shadow var(--dur-base)",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          marginBottom: 8,
        }}
      >
        <span
          className="material-symbols-rounded"
          style={{
            color: "var(--brand-500)",
            fontSize: 16,
          }}
        >
          chat_bubble
        </span>
        <span
          style={{
            fontSize: 14,
            fontWeight: 600,
            color: "var(--gray-900)",
            flex: 1,
            whiteSpace: "nowrap",
            overflow: "hidden",
            textOverflow: "ellipsis",
          }}
        >
          {item.title || "新会话"}
        </span>
        <Tag color={item.status === "active" ? "success" : "default"}>
          {item.status}
        </Tag>
      </div>
      <div
        style={{
          fontSize: 12,
          color: "var(--gray-500)",
          marginBottom: 12,
        }}
      >
        {item.created_at ? new Date(item.created_at).toLocaleString() : "-"}
      </div>
      <Space>
        <Button
          size="small"
          type="primary"
          onClick={() => (window.location.href = "/")}
        >
          继续对话
        </Button>
        <Button size="small" danger onClick={onDelete}>
          删除
        </Button>
      </Space>
    </div>
  );
}
