import { useState } from "react";
import { App as AntdApp } from "antd";
import { postData } from "@/api/http";

type Feedback = "up" | "down" | null;

export default function MessageActions({ content, traceId }: { content: string; traceId?: string }) {
  const { message: toast } = AntdApp.useApp();
  const [copied, setCopied] = useState(false);
  const [feedback, setFeedback] = useState<Feedback>(null);
  const [saving, setSaving] = useState(false);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(content);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      toast.error("复制失败，请检查浏览器剪贴板权限");
    }
  };

  const rate = async (next: Exclude<Feedback, null>) => {
    if (saving || feedback === next) return;
    if (!traceId) {
      toast.info("这条历史消息暂无可关联的运行记录");
      return;
    }
    setSaving(true);
    try {
      await postData(`/observability/traces/${traceId}/scores`, {
        score_name: "user_feedback",
        value: next === "up" ? 1 : 0,
        source: "human",
        reason: next === "up" ? "chat_thumbs_up" : "chat_thumbs_down",
      });
      setFeedback(next);
      toast.success("感谢你的反馈");
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "反馈提交失败");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="message-actions" aria-label="回复操作">
      <button type="button" className={copied ? "active" : ""} aria-label={copied ? "已复制" : "复制回复"} title={copied ? "已复制" : "复制"} onClick={() => void copy()}>
        <span className="material-symbols-rounded">{copied ? "check" : "content_copy"}</span>
      </button>
      <button type="button" className={feedback === "up" ? "active" : ""} aria-label="赞" aria-pressed={feedback === "up"} title="赞" disabled={saving} onClick={() => void rate("up")}>
        <span className="material-symbols-rounded">thumb_up</span>
      </button>
      <button type="button" className={feedback === "down" ? "active" : ""} aria-label="踩" aria-pressed={feedback === "down"} title="踩" disabled={saving} onClick={() => void rate("down")}>
        <span className="material-symbols-rounded">thumb_down</span>
      </button>
    </div>
  );
}
