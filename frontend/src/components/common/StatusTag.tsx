// 状态徽标
import { Tag } from "antd";

const STATUS_MAP: Record<string, { color: string; label: string }> = {
  active: { color: "green", label: "进行中" },
  completed: { color: "blue", label: "已完成" },
  interrupted: { color: "orange", label: "待人工" },
  deleted: { color: "red", label: "已删除" },
};

export default function StatusTag({ status }: { status: string }) {
  const cfg = STATUS_MAP[status] ?? { color: "default", label: status };
  return <Tag color={cfg.color}>{cfg.label}</Tag>;
}
