// 通用格式化工具

/** 将 ISO 时间格式化为 "MM-DD HH:mm"；今天则显示 "今天 HH:mm" */
export function formatTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const now = new Date();
  const sameDay =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate();
  if (sameDay) return `今天 ${hm}`;
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${hm}`;
}

/** 取消息文本摘要作为会话标题 */
export function summarize(text: string, max = 20): string {
  const t = text.replace(/\s+/g, " ").trim();
  return t.length > max ? `${t.slice(0, max)}…` : t;
}

/** 将 YYYY-MM-DD 相对化为 今天 / 昨天 / M月D日（记忆时间线分组标签） */
export function formatDayLabel(dateKey: string): string {
  if (!dateKey) return "";
  const d = new Date(`${dateKey}T00:00:00`);
  if (Number.isNaN(d.getTime())) return dateKey;
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  const todayKey = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
  if (dateKey === todayKey) return "今天";
  const yest = new Date(now);
  yest.setDate(now.getDate() - 1);
  const yestKey = `${yest.getFullYear()}-${pad(yest.getMonth() + 1)}-${pad(yest.getDate())}`;
  if (dateKey === yestKey) return "昨天";
  return `${d.getMonth() + 1}月${d.getDate()}日`;
}
