import { useMemo, useState } from "react";
import { Button, Empty, Tag } from "antd";
import type { MemoryGraphData, MemoryGraphEdge, MemoryItem } from "@/types/api";

const WIDTH = 960;
const HEIGHT = 560;
const PADDING = 54;

interface PositionedNode extends MemoryItem {
  x: number;
  y: number;
}

interface MemoryGraphProps {
  data: MemoryGraphData;
  selectedId?: number;
  onSelect: (memory: MemoryItem) => void;
}

function seededPosition(id: number, index: number, count: number) {
  const angle = (index / Math.max(count, 1)) * Math.PI * 2 + (id % 17) * 0.035;
  const radius = Math.min(WIDTH, HEIGHT) * (0.28 + ((id * 13) % 9) / 90);
  return {
    x: WIDTH / 2 + Math.cos(angle) * radius,
    y: HEIGHT / 2 + Math.sin(angle) * radius,
  };
}

function buildLayout(nodes: MemoryItem[], edges: MemoryGraphEdge[]): PositionedNode[] {
  if (nodes.length === 0) return [];
  const points = nodes.map((node, index) => ({
    ...node,
    ...seededPosition(node.memory_id, index, nodes.length),
    vx: 0,
    vy: 0,
  }));
  const indexById = new Map(points.map((node, index) => [node.memory_id, index]));

  for (let step = 0; step < 110; step += 1) {
    const cooling = 1 - step / 120;
    for (let left = 0; left < points.length; left += 1) {
      for (let right = left + 1; right < points.length; right += 1) {
        const dx = points[right].x - points[left].x || 0.01;
        const dy = points[right].y - points[left].y || 0.01;
        const distanceSquared = Math.max(dx * dx + dy * dy, 100);
        const force = 1500 / distanceSquared;
        points[left].vx -= dx * force;
        points[left].vy -= dy * force;
        points[right].vx += dx * force;
        points[right].vy += dy * force;
      }
    }

    edges.forEach((edge) => {
      const sourceIndex = indexById.get(edge.source);
      const targetIndex = indexById.get(edge.target);
      if (sourceIndex === undefined || targetIndex === undefined) return;
      const source = points[sourceIndex];
      const target = points[targetIndex];
      const dx = target.x - source.x;
      const dy = target.y - source.y;
      const distance = Math.max(Math.sqrt(dx * dx + dy * dy), 1);
      const desired = edge.relation === "same_source" ? 92 : 128;
      const force = (distance - desired) * 0.012 * edge.strength;
      const fx = (dx / distance) * force;
      const fy = (dy / distance) * force;
      source.vx += fx;
      source.vy += fy;
      target.vx -= fx;
      target.vy -= fy;
    });

    points.forEach((point) => {
      point.vx += (WIDTH / 2 - point.x) * 0.0018;
      point.vy += (HEIGHT / 2 - point.y) * 0.0018;
      point.x = Math.min(WIDTH - PADDING, Math.max(PADDING, point.x + point.vx * cooling));
      point.y = Math.min(HEIGHT - PADDING, Math.max(PADDING, point.y + point.vy * cooling));
      point.vx *= 0.72;
      point.vy *= 0.72;
    });
  }

  return points.map(({ vx: _vx, vy: _vy, ...point }) => point);
}

function shortLabel(content: string) {
  return content.length > 12 ? `${content.slice(0, 12)}…` : content;
}

export function MemoryGraph({ data, selectedId, onSelect }: MemoryGraphProps) {
  const [zoom, setZoom] = useState(1);
  const nodes = useMemo(() => buildLayout(data.nodes, data.edges), [data]);
  const nodeById = useMemo(
    () => new Map(nodes.map((node) => [node.memory_id, node])),
    [nodes]
  );
  const connectedIds = useMemo(
    () => new Set(data.edges.flatMap((edge) => [edge.source, edge.target])),
    [data.edges]
  );

  if (data.nodes.length === 0) {
    return <Empty description="暂无可绘制的记忆关系" style={{ padding: 56 }} />;
  }

  return (
    <section className="memory-graph" aria-label="记忆关系图谱">
      <div className="memory-graph-toolbar">
        <div className="memory-graph-legend" aria-label="图例">
          <span><i className="memory-graph-dot semantic" />语义记忆</span>
          <span><i className="memory-graph-dot episodic" />情景记忆</span>
          <span><i className="memory-graph-line semantic" />语义相近</span>
          <span><i className="memory-graph-line source" />同一来源</span>
        </div>
        <div className="memory-graph-tools">
          <Tag bordered={false}>{data.nodes.length} 节点 · {data.edges.length} 关系</Tag>
          <Button size="small" aria-label="缩小图谱" disabled={zoom <= 0.75} onClick={() => setZoom((value) => Math.max(0.75, value - 0.25))}>−</Button>
          <Button size="small" onClick={() => setZoom(1)}>{Math.round(zoom * 100)}%</Button>
          <Button size="small" aria-label="放大图谱" disabled={zoom >= 1.75} onClick={() => setZoom((value) => Math.min(1.75, value + 0.25))}>＋</Button>
        </div>
      </div>

      <div className="memory-graph-canvas">
        <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="img" aria-labelledby="memory-graph-title memory-graph-desc">
          <title id="memory-graph-title">记忆关系图谱</title>
          <desc id="memory-graph-desc">节点代表记忆，实线表示语义相近，虚线表示来自同一会话。可选择节点查看详情。</desc>
          <g transform={`translate(${WIDTH * (1 - zoom) / 2} ${HEIGHT * (1 - zoom) / 2}) scale(${zoom})`}>
            {data.edges.map((edge) => {
              const source = nodeById.get(edge.source);
              const target = nodeById.get(edge.target);
              if (!source || !target) return null;
              const highlighted = selectedId === edge.source || selectedId === edge.target;
              return (
                <line
                  className={`memory-graph-edge ${edge.relation}${highlighted ? " highlighted" : ""}`}
                  key={`${edge.source}-${edge.target}-${edge.relation}`}
                  x1={source.x}
                  y1={source.y}
                  x2={target.x}
                  y2={target.y}
                  strokeWidth={0.8 + edge.strength * 1.8}
                >
                  <title>{edge.relation === "same_source" ? "同一来源会话" : `语义相似度 ${Math.round(edge.strength * 100)}%`}</title>
                </line>
              );
            })}
            {nodes.map((node) => {
              const selected = node.memory_id === selectedId;
              const radius = Math.min(17, 8 + Math.log2(node.access_count + 1) * 2.2);
              return (
                <g
                  className={`memory-graph-node ${node.memory_type}${selected ? " selected" : ""}${connectedIds.has(node.memory_id) ? "" : " isolated"}`}
                  key={node.memory_id}
                  role="button"
                  tabIndex={0}
                  aria-label={`${node.memory_type === "semantic" ? "语义" : "情景"}记忆：${node.content}`}
                  onClick={() => onSelect(node)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      onSelect(node);
                    }
                  }}
                  transform={`translate(${node.x} ${node.y})`}
                >
                  <circle r={radius + (selected ? 5 : 3)} className="memory-graph-node-halo" />
                  <circle r={radius} className="memory-graph-node-core" />
                  <text y={radius + 17} textAnchor="middle">{shortLabel(node.content)}</text>
                  <title>{node.content}\n置信度 {Math.round(node.confidence * 100)}% · 命中 {node.access_count} 次</title>
                </g>
              );
            })}
          </g>
        </svg>
      </div>
      <p className="memory-graph-note">节点大小表示命中次数；孤立节点表示当前阈值下没有发现关系。语义关系阈值为 {Math.round(data.similarity_threshold * 100)}%。</p>
    </section>
  );
}
