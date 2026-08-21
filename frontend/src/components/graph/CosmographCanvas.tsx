import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Cosmograph, usePreparedCosmographData } from "@cosmograph/react";
import type { CosmographDataPrepConfig, CosmographRef } from "@cosmograph/react";
import type { CSSProperties, MouseEvent as ReactMouseEvent } from "react";

export interface ForceGraphNode {
  id: string;
  label: string;
  kind: string;
  color: string;
  size: number;
  detail?: string;
}

export interface ForceGraphLink {
  id: string;
  source: string;
  target: string;
  kind: string;
  color: string;
  width: number;
  strength: number;
}

export interface ForceGraphLegendItem {
  label: string;
  color: string;
  variant?: "node" | "line" | "dashed";
}

interface CosmographCanvasProps {
  nodes: ForceGraphNode[];
  links: ForceGraphLink[];
  legend: ForceGraphLegendItem[];
  selectedId?: string;
  ariaLabel: string;
  note: string;
  onSelect?: (node: ForceGraphNode) => void;
}

const ZOOM_STEP = 1.28;
const DATA_CONFIG = {
  points: {
    pointIdBy: "id",
    pointLabelBy: "label",
    pointLabelWeightBy: "size",
    pointColorBy: "color",
    pointSizeBy: "size",
    pointIncludeColumns: ["*"],
  },
  links: {
    linkSourceBy: "source",
    linkTargetsBy: ["target"],
    linkColorBy: "color",
    linkWidthBy: "width",
    linkStrengthBy: "strength",
    linkIncludeColumns: ["*"],
  },
} satisfies CosmographDataPrepConfig;
const POINTS_ONLY_DATA_CONFIG = { points: DATA_CONFIG.points } satisfies CosmographDataPrepConfig;

export default function CosmographCanvas({
  nodes,
  links,
  legend,
  selectedId,
  ariaLabel,
  note,
  onSelect,
}: CosmographCanvasProps) {
  const graphRef = useRef<CosmographRef>();
  const [paused, setPaused] = useState(false);
  const [hoveredIndex, setHoveredIndex] = useState<number>();
  const [ready, setReady] = useState(false);

  const prepared = useMemo(() => {
    const pointIndexById = new Map<string, number>();
    const points = nodes.map((node, index) => {
      pointIndexById.set(node.id, index);
      return { ...node, index };
    });
    const preparedLinks = links.flatMap((link) => {
      const sourceIndex = pointIndexById.get(link.source);
      const targetIndex = pointIndexById.get(link.target);
      if (sourceIndex === undefined || targetIndex === undefined) return [];
      return [{ ...link, sourceIndex, targetIndex }];
    });
    return { points, links: preparedLinks, pointIndexById };
  }, [links, nodes]);
  const hasLinks = prepared.links.length > 0;
  const { config: preparedConfig, isLoading, error } = usePreparedCosmographData(
    hasLinks ? DATA_CONFIG : POINTS_ONLY_DATA_CONFIG,
    prepared.points,
    hasLinks ? prepared.links : undefined,
  );

  const syncSelection = useCallback(() => {
    const graph = graphRef.current;
    if (!graph) return;
    const index = selectedId ? prepared.pointIndexById.get(selectedId) : undefined;
    graph.unselectAllPoints();
    graph.setFocusedPoint(index);
  }, [prepared.pointIndexById, selectedId]);

  useEffect(() => {
    if (!ready) return;
    syncSelection();
  }, [ready, syncSelection]);

  useEffect(() => {
    setReady(false);
  }, [prepared.points, prepared.links]);

  const handlePointClick = useCallback((index: number) => {
    const node = nodes[index];
    if (node) onSelect?.(node);
  }, [nodes, onSelect]);

  const fitView = () => graphRef.current?.fitView(360, 0.16);
  const changeZoom = (direction: 1 | -1) => {
    const graph = graphRef.current;
    const current = graph?.getZoomLevel() ?? 1;
    graph?.setZoomLevel(direction === 1 ? current * ZOOM_STEP : current / ZOOM_STEP, 180);
  };
  const restart = () => {
    setPaused(false);
    graphRef.current?.start(0.72);
    window.setTimeout(fitView, 520);
  };
  const togglePaused = () => {
    const next = !paused;
    setPaused(next);
    if (next) graphRef.current?.pause();
    else graphRef.current?.unpause();
  };

  const hoveredNode = hoveredIndex === undefined ? undefined : nodes[hoveredIndex];
  const keepAttributionInWorkbench = (event: ReactMouseEvent<HTMLDivElement>) => {
    const target = event.target;
    const element = target instanceof Element ? target : null;
    const anchor = element?.closest<HTMLAnchorElement>("a[href]");
    const attribution = element?.closest<HTMLElement>("[class*='_attribution__']");
    if (!attribution && (!anchor || !/^https?:\/\//i.test(anchor.href))) return;
    event.preventDefault();
    event.stopPropagation();
  };

  return (
    <section className="force-graph-shell" aria-label={ariaLabel}>
      <div className="force-graph-toolbar">
        <div className="force-graph-engine" aria-label="GPU 力导向布局">
          <i aria-hidden="true" />
          <span><b>GPU FORCE</b><small>{nodes.length} NODES / {prepared.links.length} EDGES</small></span>
        </div>
        <div className="force-graph-legend" aria-label="图例">
          {legend.map((item) => (
            <span key={`${item.label}-${item.variant ?? "node"}`}>
              <i className={item.variant ?? "node"} style={{ "--legend-color": item.color } as CSSProperties} />
              {item.label}
            </span>
          ))}
        </div>
        <div className="force-graph-controls">
          <button type="button" onClick={() => changeZoom(-1)} aria-label="缩小图谱" title="缩小">
            <span className="material-symbols-rounded">remove</span>
          </button>
          <button type="button" onClick={() => changeZoom(1)} aria-label="放大图谱" title="放大">
            <span className="material-symbols-rounded">add</span>
          </button>
          <button type="button" onClick={fitView} aria-label="适配全部节点" title="适配视图">
            <span className="material-symbols-rounded">fit_screen</span>
          </button>
          <button type="button" onClick={restart} aria-label="重新运行力导向布局" title="重新布局">
            <span className="material-symbols-rounded">motion_mode</span>
          </button>
          <button type="button" className={paused ? "active" : ""} onClick={togglePaused} aria-label={paused ? "继续布局" : "暂停布局"} title={paused ? "继续布局" : "暂停布局"}>
            <span className="material-symbols-rounded">{paused ? "play_arrow" : "pause"}</span>
          </button>
        </div>
      </div>

      <div className="force-graph-stage" role="group" aria-label={`${ariaLabel}，支持拖拽节点、滚轮缩放和点击聚焦`} onClickCapture={keepAttributionInWorkbench}>
        {isLoading ? (
          <div className="force-graph-inline-state"><span className="material-symbols-rounded spin">progress_activity</span><p>正在准备图谱数据…</p></div>
        ) : error ? (
          <div className="force-graph-inline-state error"><span className="material-symbols-rounded">error</span><p>图谱数据准备失败，请刷新后重试。</p></div>
        ) : <Cosmograph
          {...preparedConfig}
          ref={graphRef}
          className="force-graph-cosmograph"
          pointLabelBy="label"
          pointLabelWeightBy="size"
          pointColorBy="color"
          pointColorStrategy="direct"
          pointSizeBy="size"
          pointSizeStrategy="direct"
          linkColorBy="color"
          linkColorStrategy="direct"
          linkWidthBy="width"
          linkWidthStrategy="direct"
          linkStrengthBy="strength"
          enableSimulation
          enableDrag
          enableZoom
          enableRightClickRepulsion
          selectPointOnClick={false}
          focusPointOnClick
          resetSelectionOnEmptyCanvasClick
          preservePointPositionsOnDataUpdate
          backgroundColor="#f7faff"
          pointOpacity={0.96}
          pointGreyoutOpacity={0.5}
          linkOpacity={0.72}
          linkGreyoutOpacity={0.08}
          linkDefaultArrows
          linkArrowsSizeScale={0.72}
          curvedLinks
          curvedLinkWeight={0.48}
          curvedLinkControlPointDistance={0.18}
          scalePointsOnZoom={false}
          scaleLinksOnZoom={false}
          renderHoveredPointRing
          hoveredPointRingColor="#e2a128"
          focusedPointRingColor="#245fa9"
          hoveredPointCursor="pointer"
          showLabels
          showDynamicLabels
          showDynamicLabelsLimit={28}
          showTopLabels
          showTopLabelsLimit={24}
          showSelectedLabels
          showUnselectedPointLabels
          pointLabelColor="#29445f"
          pointLabelFontSize={11}
          pointLabelClassName="force-graph-point-label"
          hoveredPointLabelClassName="force-graph-point-label hovered"
          pointLabelPosition="below"
          labelMargin={7}
          labelPadding={[5, 3, 5, 3]}
          simulationDecay={3800}
          simulationGravity={0.18}
          simulationCenter={0.06}
          simulationRepulsion={1.16}
          simulationRepulsionTheta={1.28}
          simulationLinkSpring={1.2}
          simulationLinkDistance={9}
          simulationFriction={0.88}
          simulationRepulsionFromMouse={1.4}
          fitViewOnInit
          fitViewDelay={320}
          fitViewDuration={420}
          fitViewPadding={0.16}
          randomSeed="my-agent-graph"
          pixelRatio={Math.min(window.devicePixelRatio || 1, 2)}
          spaceSize={2048}
          disableLogging
          onMount={(graph) => {
            graphRef.current = graph;
            setReady(true);
          }}
          onGraphRebuilt={() => {
            setReady(true);
            window.setTimeout(syncSelection, 80);
            window.setTimeout(() => graphRef.current?.fitView(360, 0.16), 620);
          }}
          onPointClick={handlePointClick}
          onPointMouseOver={(index) => setHoveredIndex(index)}
          onPointMouseOut={() => setHoveredIndex(undefined)}
          onBackgroundClick={() => setHoveredIndex(undefined)}
        />}

        <div className={`force-graph-hovercard${hoveredNode ? " visible" : ""}`} aria-hidden={!hoveredNode}>
          {hoveredNode ? (
            <>
              <i style={{ background: hoveredNode.color }} />
              <span><small>{hoveredNode.kind}</small><b>{hoveredNode.label}</b>{hoveredNode.detail ? <em>{hoveredNode.detail}</em> : null}</span>
            </>
          ) : null}
        </div>

        <div className="force-graph-gesture-hint" aria-hidden="true">
          <span className="material-symbols-rounded">drag_pan</span>
          拖拽探索 · 滚轮缩放 · 右键推开
        </div>

        <ul className="force-graph-a11y-list" aria-label="图谱节点列表">
          {nodes.map((node) => (
            <li key={node.id}><button type="button" onClick={() => onSelect?.(node)}>{node.kind}：{node.label}</button></li>
          ))}
        </ul>
      </div>
      <p className="force-graph-note"><span className="material-symbols-rounded">flare</span>{note}</p>
    </section>
  );
}
