import { useEffect, useState } from "react";
import { Popconfirm, message } from "antd";
import { useLocation, useNavigate } from "react-router-dom";
import { deleteData, getData, postData } from "@/api/http";
import type { SessionItem, SessionListData } from "@/types/api";
import { useSessionStore } from "@/stores/sessionStore";
import { formatTime, summarize } from "@/utils/format";

const FEATURES = [
  { label: "观测台", icon: "monitoring", path: "/observability" },
  { label: "技能库", icon: "bolt", path: "/skills" },
  { label: "知识库", icon: "database", path: "/knowledge" },
  { label: "记忆", icon: "memory", path: "/memory" },
  { label: "设置", icon: "settings", path: "/settings" },
];

export default function Sidebar() {
  const navigate = useNavigate();
  const location = useLocation();
  const { currentSessionId, setSessions, setCurrentSession, sessions, streaming } = useSessionStore();
  const [creating, setCreating] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  useEffect(() => {
    getData<SessionListData>("/sessions?page=1&page_size=10")
      .then((data) => setSessions(data.items))
      .catch(() => undefined);
  }, [setSessions]);

  const createNew = async () => {
    if (streaming) return;
    setCreating(true);
    try {
      const session = await postData<SessionItem>("/sessions", { title: "新对话" });
      setSessions([session, ...sessions.filter((item) => item.session_id !== session.session_id)]);
      setCurrentSession(session.session_id);
      navigate("/");
    } finally {
      setCreating(false);
    }
  };

  const removeSession = async (sessionId: string) => {
    if (streaming) return;
    setDeletingId(sessionId);
    try {
      await deleteData(`/sessions/${sessionId}`);
      const remaining = sessions.filter((item) => item.session_id !== sessionId);
      setSessions(remaining);
      if (currentSessionId === sessionId) {
        setCurrentSession(remaining[0]?.session_id ?? null);
        navigate("/");
      }
      message.success("会话已删除");
    } catch (error) {
      message.error(error instanceof Error ? error.message : "删除会话失败");
    } finally {
      setDeletingId(null);
    }
  };

  return (
    <aside className="side-panel">
      <div className="brand-lockup">
        <div className="brand-mark"><span className="material-symbols-rounded">explore</span></div>
        <strong>Harness</strong>
      </div>
      <button className="new-chat-button" onClick={createNew} disabled={creating || streaming}>
        <span className="material-symbols-rounded">add</span>{creating ? "创建中..." : "新建会话"}
      </button>
      <section className="recent-section">
        <div className="eyebrow">最近会话</div>
        <div className="recent-list">
          {sessions.length === 0 ? <div className="empty-recent">暂无会话</div> : null}
          {sessions.map((session) => {
            const active = currentSessionId === session.session_id;
            return (
              <div className={`session-row${active ? " active" : ""}`} key={session.session_id}>
                <button
                  className="session-select"
                  onClick={() => { setCurrentSession(session.session_id); navigate("/"); }}
                  disabled={streaming && !active}
                  aria-current={active ? "page" : undefined}
                >
                  <span className="material-symbols-rounded">chat_bubble</span>
                  <span className="session-copy">
                    <strong>{summarize(session.title || "新对话", 16)}</strong>
                    <small>{formatTime(session.updated_at || session.created_at)}</small>
                  </span>
                </button>
                <span className="session-action-slot">
                  {active ? <i className="session-active-dot" aria-label="当前会话" /> : null}
                  <Popconfirm
                    title="删除这条会话？"
                    description="删除后将无法恢复。"
                    okText="删除"
                    cancelText="取消"
                    okButtonProps={{ danger: true }}
                    onConfirm={() => void removeSession(session.session_id)}
                  >
                    <button
                      className="session-delete"
                      title="删除会话"
                      aria-label={`删除会话：${session.title || "新对话"}`}
                      disabled={streaming || deletingId === session.session_id}
                    >
                      <span className="material-symbols-rounded">
                        {deletingId === session.session_id ? "progress_activity" : "delete"}
                      </span>
                    </button>
                  </Popconfirm>
                </span>
              </div>
            );
          })}
        </div>
      </section>
      <section className="feature-section">
        <div className="eyebrow">功能区</div>
        {FEATURES.map((item) => {
          const active =
            location.pathname === item.path ||
            (item.path === "/observability" && location.pathname.startsWith("/observability")) ||
            (item.path !== "/observability" && location.pathname.startsWith(`${item.path}/`));
          return (
            <button key={item.path} className={`feature-row${active ? " active" : ""}`} onClick={() => navigate(item.path)}>
              <span className="material-symbols-rounded">{item.icon}</span>{item.label}
            </button>
          );
        })}
      </section>
      <div className="profile-row">
        <div className="profile-avatar"><span className="material-symbols-rounded">person</span></div>
        <div><strong>Admin User</strong><small>admin@example.com</small></div>
        <span className="material-symbols-rounded profile-more">more_vert</span>
      </div>
    </aside>
  );
}
