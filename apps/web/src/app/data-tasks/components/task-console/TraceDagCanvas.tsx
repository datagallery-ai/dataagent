import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type {
  KeyboardEvent as ReactKeyboardEvent,
  PointerEvent as ReactPointerEvent,
  WheelEvent as ReactWheelEvent,
} from "react";
import type {
  TraceDagDto,
  TraceDagEdgeDto,
  TraceDagNodeDto,
  TraceDagNodeKind,
  TraceDagSectionDto,
} from "../../../../lib/config-api";

const CANVAS_WIDTH = 920;
const CANVAS_HEIGHT = 620;
const DOT_RADIUS = 8;
const HIT_RADIUS = 22;
const LEVEL_GAP = 82;
const SIBLING_GAP = 132;
const RUN_GROUP_GAP = 112;
const LAYOUT_MARGIN = 72;
const MAX_TOOL_NODES_PER_ROW = 5;
const MIN_SCALE = 0.38;
const MAX_SCALE = 2.2;

type TraceDagCanvasProps = {
  dag: TraceDagDto | null;
  error: string | null;
  isLoading: boolean;
  mode?: "embedded" | "fullscreen";
  sections?: TraceDagSectionDto[];
  collapsedSectionIds?: ReadonlySet<string>;
  selectedNodeId: string | null;
  selectedSectionId?: string | null;
  onSelectNode: (nodeId: string) => void;
  onSelectSection?: (sectionId: string) => void;
  onToggleSection?: (sectionId: string) => void;
};

type ViewTransform = {
  x: number;
  y: number;
  scale: number;
};

type DragState = {
  pointerId: number;
  startX: number;
  startY: number;
  view: ViewTransform;
};

type CanvasNode = TraceDagNodeDto & {
  section?: TraceDagSectionDto;
};

type CanvasDag = Omit<TraceDagDto, "nodes"> & {
  nodes: CanvasNode[];
};

type LayoutNode = CanvasNode & {
  x: number;
  y: number;
};

type LayoutEdge = TraceDagEdgeDto & {
  path: string;
  sourceNode: LayoutNode;
  targetNode: LayoutNode;
};

type DagLayout = {
  edges: LayoutEdge[];
  height: number;
  nodeById: Map<string, LayoutNode>;
  nodes: LayoutNode[];
  width: number;
};

type NodeVisual = {
  color: string;
  halo: string;
  stroke: string;
};

export function TraceDagCanvas({
  dag,
  error,
  isLoading,
  mode = "fullscreen",
  sections = [],
  collapsedSectionIds = new Set<string>(),
  selectedNodeId,
  selectedSectionId,
  onSelectNode,
  onSelectSection,
  onToggleSection,
}: TraceDagCanvasProps) {
  const svgRef = useRef<SVGSVGElement | null>(null);
  const dragRef = useRef<DragState | null>(null);
  const previousLayoutKey = useRef<string | null>(null);
  const displayDag = useMemo(
    () => collapseTraceSections(dag, sections, collapsedSectionIds),
    [collapsedSectionIds, dag, sections],
  );
  const layout = useMemo(() => buildDagLayout(displayDag), [displayDag]);
  const [view, setView] = useState<ViewTransform>(() => fitLayout(layout));
  const layoutKey = layout?.nodes.map((node) => node.id).join("|") ?? "";

  const resetView = useCallback(() => {
    setView(fitLayout(layout));
  }, [layout]);

  useEffect(() => {
    if (previousLayoutKey.current === layoutKey) return;
    previousLayoutKey.current = layoutKey;
    setView(fitLayout(layout));
  }, [layout, layoutKey]);

  const zoomBy = useCallback((factor: number) => {
    setView((current) => zoomAt(current, factor, CANVAS_WIDTH / 2, CANVAS_HEIGHT / 2));
  }, []);

  const handleWheel = (event: ReactWheelEvent<SVGSVGElement>) => {
    event.preventDefault();
    const rect = svgRef.current?.getBoundingClientRect();
    if (!rect) return;
    const pointX = ((event.clientX - rect.left) / rect.width) * CANVAS_WIDTH;
    const pointY = ((event.clientY - rect.top) / rect.height) * CANVAS_HEIGHT;
    setView((current) => zoomAt(current, event.deltaY > 0 ? 0.9 : 1.1, pointX, pointY));
  };

  const handlePointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    if ((event.target as Element).closest?.("[data-trace-dag-node]")) return;
    dragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      view,
    };
    event.currentTarget.setPointerCapture(event.pointerId);
  };

  const handlePointerMove = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    setView({
      ...drag.view,
      x: drag.view.x + event.clientX - drag.startX,
      y: drag.view.y + event.clientY - drag.startY,
    });
  };

  const handlePointerUp = (event: ReactPointerEvent<HTMLDivElement>) => {
    const drag = dragRef.current;
    if (!drag || drag.pointerId !== event.pointerId) return;
    dragRef.current = null;
    event.currentTarget.releasePointerCapture(event.pointerId);
  };

  if (isLoading) {
    return (
      <TraceDagCanvasState
        tone="loading"
        title="Loading trace graph"
        subtitle="Preparing checkpoint tree."
      />
    );
  }

  if (error) {
    return <TraceDagCanvasState tone="error" title="Trace graph failed" subtitle={error} />;
  }

  if (!layout || layout.nodes.length === 0) {
    return (
      <TraceDagCanvasState
        tone="empty"
        title="No trace graph nodes yet"
        subtitle="Run a task to generate checkpoints."
      />
    );
  }

  const selectedNode = selectedNodeId ? layout.nodeById.get(selectedNodeId) : undefined;

  return (
    <section className="min-w-0 rounded-lg border border-border bg-surface p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h3 className="text-sm font-semibold text-foreground">Trace DAG</h3>
          <div className="mt-0.5 text-[11px] text-muted-light">
            {layout.nodes.length} visible nodes · {sections.length} sections
          </div>
        </div>
        <div className="flex items-center gap-1">
          <TraceCanvasToolButton label="-" onClick={() => zoomBy(0.85)} />
          <TraceCanvasToolButton label="+" onClick={() => zoomBy(1.18)} />
          <TraceCanvasToolButton label="Fit" onClick={resetView} />
        </div>
      </div>

      {sections.length > 0 ? (
        <TraceSectionStrip
          collapsedSectionIds={collapsedSectionIds}
          onSelectSection={onSelectSection}
          onToggleSection={onToggleSection}
          sections={sections}
          selectedSectionId={selectedSectionId}
        />
      ) : null}

      <div
        className={[
          "relative overflow-hidden rounded-lg border border-border",
          "bg-[linear-gradient(180deg,var(--surface-subtle),var(--surface))]",
          "touch-none",
          mode === "embedded" ? "h-[360px] md:h-[420px]" : "h-[520px] md:h-[620px]",
        ].join(" ")}
        onPointerCancel={handlePointerUp}
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={handlePointerUp}
      >
        <svg
          ref={svgRef}
          aria-label="Trace DAG vertical tree"
          className="h-full w-full"
          role="img"
          viewBox={`0 0 ${CANVAS_WIDTH} ${CANVAS_HEIGHT}`}
          onWheel={handleWheel}
        >
          <defs>
            <pattern id="trace-dag-dot-grid" width="24" height="24" patternUnits="userSpaceOnUse">
              <circle cx="1" cy="1" r="1" fill="var(--border)" opacity="0.55" />
            </pattern>
            <marker id="trace-dag-arrow" markerHeight="6" markerWidth="7" orient="auto" refX="7" refY="3">
              <path d="M 0 0 L 7 3 L 0 6 z" fill="var(--muted-light)" />
            </marker>
            <marker id="trace-dag-arrow-active" markerHeight="6" markerWidth="7" orient="auto" refX="7" refY="3">
              <path d="M 0 0 L 7 3 L 0 6 z" fill="var(--primary)" />
            </marker>
            <style>
              {`
                @keyframes trace-dag-flow {
                  to { stroke-dashoffset: -18; }
                }
                .trace-dag-flow {
                  animation: trace-dag-flow 1.4s linear infinite;
                }
              `}
            </style>
          </defs>
          <rect width={CANVAS_WIDTH} height={CANVAS_HEIGHT} fill="url(#trace-dag-dot-grid)" />
          <g transform={`translate(${view.x} ${view.y}) scale(${view.scale})`}>
            {layout.edges.map((edge) => (
              <TraceCanvasEdge
                key={edge.id}
                edge={edge}
                selected={edge.source === selectedNodeId || edge.target === selectedNodeId}
              />
            ))}
            {layout.nodes.map((node) => (
              <TraceCanvasNode
                key={node.id}
                node={node}
                selected={node.id === selectedNodeId || node.section?.id === selectedSectionId}
                onSelect={() => node.section && onSelectSection
                  ? onSelectSection(node.section.id)
                  : onSelectNode(node.id)}
              />
            ))}
          </g>
        </svg>

        <TraceMiniMap layout={layout} selectedNode={selectedNode} view={view} />
        <div className="pointer-events-none absolute bottom-3 left-3 flex flex-wrap gap-2">
          <TraceLegendPill tone="user" label="User" />
          <TraceLegendPill tone="tool" label="Tool" />
          <TraceLegendPill tone="checkpoint" label="Checkpoint" />
          <TraceLegendPill tone="branch" label="Branch" />
        </div>
      </div>
    </section>
  );
}

function TraceCanvasNode({
  node,
  selected,
  onSelect,
}: {
  node: LayoutNode;
  selected: boolean;
  onSelect: () => void;
}) {
  const visual = traceNodeVisual(node);
  const label = node.section
    ? shortLabel(node.section.title, 28)
    : node.kind === "tool"
      ? shortLabel(toolDisplayName(node), 18)
      : undefined;
  const radius = node.prominent ? DOT_RADIUS + 2 : DOT_RADIUS;
  const checkpointRadius = radius + 7;

  const onKeyDown = (event: ReactKeyboardEvent<SVGGElement>) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      onSelect();
    }
  };

  return (
    <g
      data-trace-dag-node
      role="button"
      tabIndex={0}
      transform={`translate(${node.x} ${node.y})`}
      className="cursor-pointer outline-none"
      onClick={onSelect}
      onKeyDown={onKeyDown}
      onPointerDown={(event) => event.stopPropagation()}
    >
      <title>{node.label}</title>
      <circle r={HIT_RADIUS} fill="transparent" />
      {node.section ? (
        <>
          <rect
            x="-58"
            y="-15"
            width="116"
            height="30"
            rx="5"
            fill="var(--surface)"
            stroke={selected ? "var(--foreground)" : "var(--primary)"}
            strokeWidth={selected ? 2.5 : 1.5}
          />
          <circle cx="-45" r="5" fill="var(--primary)" />
          <text x="-34" y="4" className="pointer-events-none fill-foreground text-[10px] font-semibold">
            {label}
          </text>
          <text x="48" y="4" className="pointer-events-none fill-muted text-[9px]" textAnchor="end">
            {node.section.nodeIds.length}
          </text>
        </>
      ) : null}
      <circle
        r={selected ? checkpointRadius + 5 : checkpointRadius}
        fill={visual.halo}
        opacity={selected ? 0.32 : 0.16}
      />
      {node.checkpointId ? (
        <circle
          r={checkpointRadius}
          fill="none"
          stroke="var(--step-success)"
          strokeDasharray="3 3"
          strokeOpacity="0.9"
          strokeWidth={selected ? 2.4 : 1.8}
        />
      ) : null}
      {!node.section ? (
        <>
          <circle
            r={radius}
            fill={visual.color}
            stroke={selected ? "var(--foreground)" : visual.stroke}
            strokeWidth={selected ? 3 : 2}
          />
          <circle r={radius / 2.6} fill="var(--surface)" opacity={node.rollbackable ? 0.86 : 0.42} />
        </>
      ) : null}
      {label && !node.section ? (
        <text
          y={radius + 24}
          className="pointer-events-none fill-muted text-[11px] font-semibold"
          textAnchor="middle"
        >
          {label}
        </text>
      ) : null}
    </g>
  );
}

function TraceCanvasEdge({ edge, selected }: { edge: LayoutEdge; selected: boolean }) {
  const branch = edge.kind === "branches_from";
  return (
    <path
      d={edge.path}
      fill="none"
      stroke={selected ? "var(--primary)" : branch ? "var(--accent)" : "var(--muted-light)"}
      strokeDasharray={branch ? "5 7" : selected ? "10 8" : undefined}
      strokeLinecap="round"
      strokeOpacity={selected ? 0.92 : 0.5}
      strokeWidth={selected ? 2.7 : 1.7}
      markerEnd={selected ? "url(#trace-dag-arrow-active)" : "url(#trace-dag-arrow)"}
      className={selected ? "trace-dag-flow" : undefined}
    />
  );
}

function TraceMiniMap({
  layout,
  selectedNode,
  view,
}: {
  layout: DagLayout;
  selectedNode?: LayoutNode;
  view: ViewTransform;
}) {
  const visibleX = -view.x / view.scale;
  const visibleY = -view.y / view.scale;
  const visibleWidth = CANVAS_WIDTH / view.scale;
  const visibleHeight = CANVAS_HEIGHT / view.scale;
  return (
    <div
      className={[
        "pointer-events-none absolute right-3 top-3 hidden rounded-lg border border-border",
        "bg-surface/90 p-1.5 shadow-sm md:block",
      ].join(" ")}
    >
      <svg width="118" height="96" viewBox={`0 0 ${layout.width} ${layout.height}`}>
        {layout.edges.map((edge) => (
          <path
            key={edge.id}
            d={edge.path}
            fill="none"
            stroke="var(--muted-light)"
            strokeOpacity="0.32"
            strokeWidth="8"
            vectorEffect="non-scaling-stroke"
          />
        ))}
        {layout.nodes.map((node) => (
          <circle
            key={node.id}
            cx={node.x}
            cy={node.y}
            r={DOT_RADIUS * 2}
            fill={traceNodeVisual(node).color}
            opacity={node.id === selectedNode?.id ? 0.95 : 0.42}
          />
        ))}
        <rect
          x={visibleX}
          y={visibleY}
          width={visibleWidth}
          height={visibleHeight}
          fill="none"
          stroke="var(--primary)"
          strokeWidth="10"
          vectorEffect="non-scaling-stroke"
        />
      </svg>
    </div>
  );
}

function TraceLegendPill({ label, tone }: { label: string; tone: "branch" | "checkpoint" | "tool" | "user" }) {
  const className = {
    branch: "border-accent/30 bg-accent/10 text-muted",
    checkpoint: "border-step-success/30 bg-step-success/10 text-step-success",
    tool: "border-step-success/25 bg-step-success/10 text-step-success",
    user: "border-primary/25 bg-primary-light/10 text-foreground",
  }[tone];
  return (
    <span className={`rounded border px-2 py-1 text-[10px] font-semibold shadow-sm backdrop-blur ${className}`}>
      {label}
    </span>
  );
}

function TraceCanvasToolButton({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={[
        "h-7 min-w-7 rounded border border-border px-2 text-xs font-semibold text-muted",
        "transition hover:bg-surface-subtle hover:text-foreground",
      ].join(" ")}
    >
      {label}
    </button>
  );
}

function TraceDagCanvasState({
  subtitle,
  title,
  tone,
}: {
  subtitle: string;
  title: string;
  tone: "empty" | "error" | "loading";
}) {
  const className = tone === "error"
    ? "border-step-error/30 bg-step-error/10 text-step-error"
    : "border-border bg-surface-subtle text-muted";
  return (
    <div className={`rounded-lg border p-5 text-sm ${className}`}>
      <div className="font-semibold">{title}</div>
      <div className="mt-1 text-xs opacity-80">{subtitle}</div>
    </div>
  );
}

function TraceSectionStrip({
  collapsedSectionIds,
  onSelectSection,
  onToggleSection,
  sections,
  selectedSectionId,
}: {
  collapsedSectionIds: ReadonlySet<string>;
  onSelectSection?: (sectionId: string) => void;
  onToggleSection?: (sectionId: string) => void;
  sections: TraceDagSectionDto[];
  selectedSectionId?: string | null;
}) {
  return (
    <div className="mb-3 grid gap-2">
      {sections.map((section) => {
        const collapsed = collapsedSectionIds.has(section.id);
        const selected = section.id === selectedSectionId;
        return (
          <div
            key={section.id}
            className={[
              "grid grid-cols-[minmax(0,1fr)_auto] gap-2 rounded border px-3 py-2",
              selected ? "border-primary bg-primary-light/10" : "border-border bg-surface-subtle",
            ].join(" ")}
          >
            <button
              type="button"
              onClick={() => onSelectSection?.(section.id)}
              className="min-w-0 text-left"
            >
              <div className="flex items-center gap-2">
                <span className={[
                  "h-2 w-2 shrink-0 rounded-full",
                  section.status === "completed" ? "bg-step-success" : "bg-primary",
                ].join(" ")} />
                <span className="truncate text-xs font-semibold text-foreground">{section.title}</span>
                <span className="shrink-0 text-[10px] text-muted-light">{section.nodeIds.length} nodes</span>
              </div>
              <p className="mt-1 line-clamp-2 text-[11px] leading-4 text-muted">{section.summary}</p>
            </button>
            <button
              type="button"
              aria-expanded={!collapsed}
              onClick={() => onToggleSection?.(section.id)}
              className="self-start rounded border border-border px-2 py-1 text-[10px] font-semibold text-muted hover:bg-surface"
            >
              {collapsed ? "Expand" : "Collapse"}
            </button>
          </div>
        );
      })}
    </div>
  );
}

function collapseTraceSections(
  dag: TraceDagDto | null,
  sections: TraceDagSectionDto[],
  collapsedSectionIds: ReadonlySet<string>,
): CanvasDag | null {
  if (!dag) {
    return null;
  }
  const collapsedSections = sections.filter((section) =>
    collapsedSectionIds.has(section.id) && section.nodeIds.length > 0
  );
  if (collapsedSections.length === 0) {
    return dag;
  }
  const hiddenNodeIds = new Set(collapsedSections.flatMap((section) => section.nodeIds));
  const sectionNodes = collapsedSections.flatMap((section) => {
    const firstNode = section.nodeIds
      .map((nodeId) => dag.nodes.find((node) => node.id === nodeId))
      .find((node): node is TraceDagNodeDto => Boolean(node));
    if (!firstNode) {
      return [];
    }
    return [{
      ...firstNode,
      id: `section:${section.id}`,
      kind: "context" as const,
      label: section.title,
      prominent: true,
      summary: section.summary,
      status: section.status,
      checkpointId: undefined,
      rollbackable: false,
      section,
    }];
  });
  return {
    ...dag,
    nodes: [...dag.nodes.filter((node) => !hiddenNodeIds.has(node.id)), ...sectionNodes]
  };
}

function buildDagLayout(dag: CanvasDag | null): DagLayout | null {
  if (!dag || dag.nodes.length === 0) return null;
  const sourceNodes = dag.nodes;
  const sourceOrder = new Map(sourceNodes.map((node, index) => [node.id, index]));
  const nodeById = new Map(sourceNodes.map((node) => [node.id, node]));
  const visualEdges = buildVisualTreeEdges(dag, nodeById, sourceOrder);
  const rowGroups = buildRowGroups(sourceNodes, sourceOrder);
  const maxRowSize = Math.max(1, ...rowGroups.flatMap((group) => group.rows.map((row) => row.length)));
  const width = LAYOUT_MARGIN * 2 + Math.max(0, maxRowSize - 1) * SIBLING_GAP + 220;
  const centerX = width / 2;
  const layoutNodes: LayoutNode[] = [];
  let currentY = LAYOUT_MARGIN;

  for (const group of rowGroups) {
    for (const row of group.rows) {
      const sorted = [...row].sort((left, right) => compareNodes(left, right, sourceOrder));
      const startX = centerX - ((sorted.length - 1) * SIBLING_GAP) / 2;
      sorted.forEach((node, index) => {
        layoutNodes.push({
          ...node,
          x: startX + index * SIBLING_GAP,
          y: currentY,
        });
      });
      currentY += LEVEL_GAP;
    }
    currentY += RUN_GROUP_GAP;
  }

  const layoutNodeById = new Map(layoutNodes.map((node) => [node.id, node]));
  const layoutEdges = visualEdges.flatMap((edge) => {
    const sourceNode = layoutNodeById.get(edge.source);
    const targetNode = layoutNodeById.get(edge.target);
    if (!sourceNode || !targetNode) return [];
    return [{ ...edge, path: edgePath(sourceNode, targetNode), sourceNode, targetNode }];
  });

  return {
    edges: layoutEdges,
    height: Math.max(CANVAS_HEIGHT, currentY - RUN_GROUP_GAP + LAYOUT_MARGIN),
    nodeById: layoutNodeById,
    nodes: layoutNodes,
    width,
  };
}

function buildVisualTreeEdges(
  dag: CanvasDag,
  nodeById: Map<string, CanvasNode>,
  sourceOrder: Map<string, number>,
): TraceDagEdgeDto[] {
  const edges: TraceDagEdgeDto[] = [];
  const edgeIds = new Set<string>();
  const runIds = uniqueStrings(dag.nodes.map((node) => node.runId).filter(Boolean));

  const add = (source: string | undefined, target: string | undefined, kind: TraceDagEdgeDto["kind"]) => {
    if (!source || !target || source === target || !nodeById.has(source) || !nodeById.has(target)) return;
    const id = `visual:${source}->${target}:${kind}`;
    if (edgeIds.has(id)) return;
    edgeIds.add(id);
    edges.push({ id, source, target, kind });
  };

  for (const runId of runIds) {
    const runNodes = dag.nodes
      .filter((node) => node.runId === runId)
      .sort((left, right) => compareNodes(left, right, sourceOrder));
    const user = runNodes.find((node) => node.kind === "user-turn");
    const runStart = runNodes.find((node) => node.kind === "run-start");
    const contexts = runNodes.filter((node) => node.kind === "context");
    const tools = runNodes.filter((node) => node.kind === "tool");
    const terminals = runNodes.filter((node) => node.kind === "run-terminal");
    const artifacts = runNodes.filter((node) => node.kind === "artifact");
    const branches = runNodes.filter((node) => node.kind === "branch");
    const firstContext = contexts[0];

    add(user?.id, runStart?.id, "starts_run");
    add(runStart?.id, firstContext?.id, "emits");
    contexts.slice(1).forEach((context, index) => add(contexts[index]?.id, context.id, "emits"));
    tools.forEach((tool) => add(parentForEvent(tool, contexts, runStart)?.id, tool.id, "emits"));

    terminals.forEach((terminal) => add(parentForEvent(terminal, contexts, runStart)?.id, terminal.id, "emits"));

    const artifactParent = terminals.at(-1)?.id ?? tools.at(-1)?.id ?? contexts.at(-1)?.id ?? runStart?.id;
    artifacts.forEach((artifact) => add(artifactParent, artifact.id, "produces_artifact"));
    branches.forEach((branch) => add(findBranchSource(dag, branch.id), branch.id, "branches_from"));
  }

  for (const edge of dag.edges) {
    if (edge.kind === "branches_from") {
      add(edge.source, edge.target, edge.kind);
    }
  }

  return edges;
}

function buildRowGroups(
  nodes: CanvasNode[],
  sourceOrder: Map<string, number>,
): Array<{ runId: string; rows: TraceDagNodeDto[][] }> {
  const groups: Array<{ runId: string; rows: TraceDagNodeDto[][] }> = [];
  const assigned = new Set<string>();
  const handledRuns = new Set<string>();
  const userTurns = nodes
    .filter((node) => node.kind === "user-turn")
    .sort((left, right) => compareNodes(left, right, sourceOrder));

  for (const userTurn of userTurns) {
    const runId = userTurn.runId;
    const relatedNodes = runId && !handledRuns.has(runId)
      ? nodes.filter((node) => node.runId === runId)
      : [userTurn];
    const rows = rowsForTurn(userTurn, relatedNodes, sourceOrder);
    groups.push({ runId: runId ?? userTurn.id, rows });
    rows.flat().forEach((node) => assigned.add(node.id));
    if (runId) handledRuns.add(runId);
  }

  const remainingRunIds = uniqueStrings(
    nodes
      .filter((node) => node.runId && !assigned.has(node.id))
      .map((node) => node.runId),
  );
  for (const runId of remainingRunIds) {
    const rows = rowsForRun(nodes.filter((node) => node.runId === runId && !assigned.has(node.id)), sourceOrder);
    groups.push({ runId, rows });
    rows.flat().forEach((node) => assigned.add(node.id));
  }

  const unassigned = nodes.filter((node) => !assigned.has(node.id));
  for (const userTurn of unassigned.filter((node) => node.kind === "user-turn")) {
    groups.push({ runId: userTurn.id, rows: [[userTurn]] });
    assigned.add(userTurn.id);
  }

  const remaining = nodes.filter((node) => !assigned.has(node.id));
  if (remaining.length > 0) {
    groups.push({ runId: "unassigned", rows: rowsForRun(remaining, sourceOrder) });
  }
  return groups.filter((group) => group.rows.length > 0);
}

function rowsForTurn(
  userTurn: CanvasNode,
  nodes: CanvasNode[],
  sourceOrder: Map<string, number>,
): TraceDagNodeDto[][] {
  return [[userTurn], ...rowsForRun(nodes.filter((node) => node.id !== userTurn.id), sourceOrder)];
}

function rowsForRun(nodes: CanvasNode[], sourceOrder: Map<string, number>): CanvasNode[][] {
  const byKind = (kind: TraceDagNodeKind) =>
    nodes.filter((node) => node.kind === kind).sort((left, right) => compareNodes(left, right, sourceOrder));
  const runStarts = byKind("run-start");
  const contexts = byKind("context");
  const tools = byKind("tool");
  const terminals = byKind("run-terminal");
  const finalNodes = [...byKind("artifact"), ...byKind("branch")];

  if (contexts.length === 0) {
    return [
      byKind("user-turn"),
      runStarts,
      ...chunkNodes(tools, MAX_TOOL_NODES_PER_ROW),
      terminals,
      finalNodes,
    ].filter((row) => row.length > 0);
  }

  const rows: TraceDagNodeDto[][] = [byKind("user-turn"), runStarts].filter((row) => row.length > 0);
  contexts.forEach((context, index) => {
    rows.push([context]);
    const nextContext = contexts[index + 1];
    const toolsForContext = tools.filter((tool) =>
      eventSeqInRange(tool, context.eventSeq, nextContext?.eventSeq)
    );
    rows.push(...chunkNodes(toolsForContext, MAX_TOOL_NODES_PER_ROW));
  });
  const toolsBeforeFirstContext = tools.filter((tool) =>
    tool.eventSeq !== undefined && contexts[0]?.eventSeq !== undefined && tool.eventSeq < contexts[0].eventSeq!
  );
  if (toolsBeforeFirstContext.length > 0) {
    rows.splice(runStarts.length > 0 ? 2 : 1, 0, ...chunkNodes(toolsBeforeFirstContext, MAX_TOOL_NODES_PER_ROW));
  }
  return [
    ...rows,
    terminals,
    finalNodes,
  ].filter((row) => row.length > 0);
}

function chunkNodes(nodes: CanvasNode[], size: number): CanvasNode[][] {
  const chunks: CanvasNode[][] = [];
  for (let index = 0; index < nodes.length; index += size) {
    chunks.push(nodes.slice(index, index + size));
  }
  return chunks;
}

function edgePath(source: LayoutNode, target: LayoutNode): string {
  const sourceY = source.y + DOT_RADIUS + 2;
  const targetY = target.y - DOT_RADIUS - 2;
  const midY = sourceY + (targetY - sourceY) / 2;
  return [
    `M ${source.x} ${sourceY}`,
    `C ${source.x} ${midY}, ${target.x} ${midY}, ${target.x} ${targetY}`,
  ].join(" ");
}

function fitLayout(layout: DagLayout | null): ViewTransform {
  if (!layout) return { x: 0, y: 0, scale: 1 };
  const scale = Math.min(
    1.12,
    Math.max(MIN_SCALE, Math.min((CANVAS_WIDTH - 80) / layout.width, (CANVAS_HEIGHT - 80) / layout.height)),
  );
  return {
    scale,
    x: (CANVAS_WIDTH - layout.width * scale) / 2,
    y: 42,
  };
}

function zoomAt(view: ViewTransform, factor: number, pointX: number, pointY: number): ViewTransform {
  const nextScale = clamp(view.scale * factor, MIN_SCALE, MAX_SCALE);
  const graphX = (pointX - view.x) / view.scale;
  const graphY = (pointY - view.y) / view.scale;
  return {
    scale: nextScale,
    x: pointX - graphX * nextScale,
    y: pointY - graphY * nextScale,
  };
}

function compareNodes(
  left: TraceDagNodeDto,
  right: TraceDagNodeDto,
  sourceOrder: Map<string, number>,
): number {
  const leftSeq = left.eventSeq ?? Number.MAX_SAFE_INTEGER;
  const rightSeq = right.eventSeq ?? Number.MAX_SAFE_INTEGER;
  if (leftSeq !== rightSeq) return leftSeq - rightSeq;
  return (sourceOrder.get(left.id) ?? 0) - (sourceOrder.get(right.id) ?? 0);
}

function findBranchSource(dag: CanvasDag, branchNodeId: string): string | undefined {
  return dag.edges.find((edge) => edge.kind === "branches_from" && edge.target === branchNodeId)?.source;
}

function parentForEvent(
  node: CanvasNode,
  contexts: CanvasNode[],
  runStart: TraceDagNodeDto | undefined,
): TraceDagNodeDto | undefined {
  if (contexts.length === 0) return runStart;
  const eventSeq = node.eventSeq;
  if (eventSeq === undefined) return contexts.at(-1) ?? runStart;
  return [...contexts]
    .reverse()
    .find((context) => context.eventSeq !== undefined && context.eventSeq <= eventSeq) ?? runStart;
}

function eventSeqInRange(node: CanvasNode, start?: number, end?: number): boolean {
  if (node.eventSeq === undefined) return end === undefined;
  if (start !== undefined && node.eventSeq < start) return false;
  return end === undefined || node.eventSeq < end;
}

function uniqueStrings(values: Array<string | undefined>): string[] {
  return [...new Set(values.filter((value): value is string => Boolean(value)))];
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function shortLabel(value: string, maxLength: number): string {
  return value.length > maxLength ? `${value.slice(0, maxLength - 1)}...` : value;
}

function toolDisplayName(node: TraceDagNodeDto): string {
  const label = node.label.replace(/^Tool:\s*/u, "").trim();
  if (label && label.toLowerCase() !== "tool call") {
    return label;
  }
  const fromSummary = node.summary?.match(/\b([a-z][a-z0-9_]{2,})\s*\(/iu)?.[1];
  if (fromSummary) {
    return fromSummary;
  }
  const fromToolCallId = readableToolCallId(node.toolCallId);
  return fromToolCallId ?? "tool";
}

function readableToolCallId(toolCallId?: string): string | undefined {
  if (!toolCallId) return undefined;
  const parts = toolCallId.split(/[:/._-]+/u).filter(Boolean);
  for (let index = parts.length - 1; index >= 0; index -= 1) {
    const part = parts[index];
    if (!/^(call|tool|id|[a-f0-9]{8,})$/iu.test(part)) {
      return shortLabel(part, 24);
    }
  }
  return shortLabel(toolCallId, 24);
}

export function traceDagNodeKindLabel(node: { kind: TraceDagNodeKind }): string {
  switch (node.kind) {
    case "user-turn":
      return "User";
    case "run-start":
      return "Run";
    case "context":
      return "Context";
    case "tool":
      return "Tool";
    case "run-terminal":
      return "Result";
    case "artifact":
      return "Output";
    case "branch":
      return "Branch";
  }
}

function traceNodeVisual(node: TraceDagNodeDto): NodeVisual {
  switch (node.kind) {
    case "user-turn":
      return {
        color: "var(--primary)",
        halo: "color-mix(in srgb, var(--primary-light) 26%, transparent)",
        stroke: "color-mix(in srgb, var(--primary) 45%, var(--surface))",
      };
    case "context":
      return {
        color: "var(--step-query)",
        halo: "color-mix(in srgb, var(--step-query) 25%, transparent)",
        stroke: "color-mix(in srgb, var(--step-query) 48%, var(--surface))",
      };
    case "tool":
      return node.status === "failed"
        ? {
            color: "var(--step-error)",
            halo: "color-mix(in srgb, var(--step-error) 28%, transparent)",
            stroke: "color-mix(in srgb, var(--step-error) 52%, var(--surface))",
          }
        : {
            color: "var(--step-success)",
            halo: "color-mix(in srgb, var(--step-success) 25%, transparent)",
            stroke: "color-mix(in srgb, var(--step-success) 48%, var(--surface))",
          };
    case "artifact":
      return {
        color: "var(--accent)",
        halo: "color-mix(in srgb, var(--accent) 25%, transparent)",
        stroke: "color-mix(in srgb, var(--accent) 48%, var(--surface))",
      };
    case "branch":
      return {
        color: "var(--step-warning)",
        halo: "color-mix(in srgb, var(--step-warning) 24%, transparent)",
        stroke: "color-mix(in srgb, var(--step-warning) 48%, var(--surface))",
      };
    case "run-terminal":
      return {
        color: node.status === "failed" ? "var(--step-error)" : "var(--foreground)",
        halo: "color-mix(in srgb, var(--muted-light) 22%, transparent)",
        stroke: "color-mix(in srgb, var(--muted-light) 42%, var(--surface))",
      };
    case "run-start":
      return {
        color: "var(--muted)",
        halo: "color-mix(in srgb, var(--muted-light) 18%, transparent)",
        stroke: "color-mix(in srgb, var(--muted-light) 36%, var(--surface))",
      };
  }
}
