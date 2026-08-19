import { useEffect, useState } from "react";
import ChatWindow from "@/components/chat/ChatWindow";
import ContextPanel from "@/components/layout/ContextPanel";
import { getData } from "@/api/http";
import { useSessionStore } from "@/stores/sessionStore";
import type { SkillItem } from "@/types/api";

export default function ChatPage() {
  const { sessions, currentSessionId, setCurrentSession } = useSessionStore();
  const [skills, setSkills] = useState<SkillItem[]>([]);
  const [messagesLoading, setMessagesLoading] = useState(false);

  useEffect(() => {
    getData<SkillItem[]>("/skills").then(setSkills).catch(() => undefined);
  }, []);

  useEffect(() => {
    if (!currentSessionId) { useSessionStore.getState().setMessages([]); return; }
    const sessionId = currentSessionId;
    const controller = new AbortController();
    setMessagesLoading(true);
    getData<{ role: string; content: string; created_at: string | null }[]>(`/sessions/${sessionId}/messages`, { signal: controller.signal })
      .then((messages) => {
        if (useSessionStore.getState().currentSessionId !== sessionId) return;
        useSessionStore.getState().setMessages(messages.map((message) => ({
          role: message.role as "user" | "assistant" | "system", content: message.content, createdAt: message.created_at ?? undefined,
        })));
      })
      .catch((error) => {
        if (!controller.signal.aborted) useSessionStore.getState().setError(error instanceof Error ? error.message : "加载消息失败");
      })
      .finally(() => { if (!controller.signal.aborted) setMessagesLoading(false); });
    return () => controller.abort();
  }, [currentSessionId]);

  useEffect(() => {
    if (!currentSessionId && sessions.length > 0) setCurrentSession(sessions[0].session_id);
  }, [currentSessionId, sessions, setCurrentSession]);

  return (
    <div className="chat-layout">
      <section className="chat-panel">
        {messagesLoading ? <div className="history-loading"><span className="material-symbols-rounded">progress_activity</span>正在加载会话…</div> : <ChatWindow skills={skills} />}
      </section>
      <ContextPanel />
    </div>
  );
}
