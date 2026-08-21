import { Tag } from "antd";
import type { GraphStatusValue, IndexStatus } from "@/types/api";

const META: Record<string, { label: string; color?: string }> = {
  empty: { label: "空库" },
  indexing: { label: "索引中", color: "processing" },
  ready: { label: "已就绪", color: "success" },
  partial: { label: "部分可用", color: "warning" },
  failed: { label: "失败", color: "error" },
  stale: { label: "已过期", color: "warning" },
  disabled: { label: "不启用图谱" },
  none: { label: "暂无图谱" },
  pending: { label: "待构建", color: "warning" },
  building: { label: "构建中", color: "processing" },
  excluded: { label: "不参与" },
};

export default function IndexHealthBadge({ status, prefix }: { status: IndexStatus | GraphStatusValue | string; prefix?: string }) {
  const meta = META[status] ?? { label: status };
  return <Tag className="kb-health-badge" color={meta.color}>{prefix ? `${prefix} · ` : ""}{meta.label}</Tag>;
}

export { META as INDEX_STATUS_META };
