import { lazy, Suspense, useMemo } from "react";
import { Empty } from "antd";
import type { ForceGraphLink, ForceGraphNode } from "@/components/graph/CosmographCanvas";
import type { MemoryGraphData, MemoryItem } from "@/types/api";

const CosmographCanvas = lazy(() => import("@/components/graph/CosmographCanvas"));

interface MemoryGraphProps {
  data: MemoryGraphData;
  selectedId?: number;
  onSelect: (memory: MemoryItem) => void;
}

function shortLabel(content: string) {
  return content.length > 18 ? `${content.slice(0, 18)}…` : content;
}

export function MemoryGraph({ data, selectedId, onSelect }: MemoryGraphProps) {
  const nodeById = useMemo(() => new Map(data.nodes.map((node) => [String(node.memory_id), node])), [data.nodes]);
  const nodes = useMemo<ForceGraphNode[]>(() => data.nodes.map((node) => ({
    id: String(node.memory_id),
    label: shortLabel(node.content),
    kind: node.memory_type === "semantic" ? "语义记忆" : "情景记忆",
    color: node.memory_type === "semantic" ? "#5f8cff" : "#22c8b5",
    size: Math.min(18, 9 + Math.log2(node.access_count + 1) * 2.4),
    detail: `置信度 ${Math.round(node.confidence * 100)}% · 命中 ${node.access_count} 次`,
  })), [data.nodes]);
  const links = useMemo<ForceGraphLink[]>(() => data.edges.map((edge, index) => ({
    id: `${edge.source}-${edge.target}-${edge.relation}-${index}`,
    source: String(edge.source),
    target: String(edge.target),
    kind: edge.relation === "same_source" ? "同一来源" : "语义相近",
    color: edge.relation === "same_source" ? "#e9a23b" : "#7898f5",
    width: 0.8 + edge.strength * 1.55,
    strength: 0.55 + edge.strength * 0.65,
  })), [data.edges]);

  if (data.nodes.length === 0) {
    return <Empty description="暂无可绘制的记忆关系" style={{ padding: 56 }} />;
  }

  return <Suspense fallback={<div className="force-graph-loading"><span className="material-symbols-rounded spin">progress_activity</span><p>正在启动 GPU 图谱引擎…</p></div>}>
    <CosmographCanvas
      nodes={nodes}
      links={links}
      selectedId={selectedId === undefined ? undefined : String(selectedId)}
      ariaLabel="记忆关系图谱"
      legend={[
        { label: "语义记忆", color: "#5f8cff" },
        { label: "情景记忆", color: "#22c8b5" },
        { label: "语义相近", color: "#7898f5", variant: "line" },
        { label: "同一来源", color: "#e9a23b", variant: "dashed" },
      ]}
      note={`节点大小表示命中强度；点击节点可聚焦关系并查看详情。语义关系阈值为 ${Math.round(data.similarity_threshold * 100)}%。`}
      onSelect={(node) => {
        const memory = nodeById.get(node.id);
        if (memory) onSelect(memory);
      }}
    />
  </Suspense>;
}
