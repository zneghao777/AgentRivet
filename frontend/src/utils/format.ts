// 通用格式化工具

/** 将 ISO 时间格式化为 "YYYY-MM-DD HH:mm"（始终包含日期，避免只有时间看不出是哪天） */
export function formatTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const pad = (n: number) => String(n).padStart(2, "0");
  const ymd = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  return `${ymd} ${hm}`;
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
