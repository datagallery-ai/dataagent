"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from "react";
import { useT } from "../../../i18n/locale-context";
import type {
  DatalinkEdgeDto,
  DatalinkGraphDto,
  DatalinkNodeDto,
  DatalinkServerDto,
} from "../../../lib/config-api";
import { configApi } from "../../../lib/config-api";
import { btnSecondaryClass, panelTitleClass, sectionLabelClass } from "../ui-tokens";

type DataLinkPanelProps = {
  onBack: () => void;
  onOpenMcpSettings: () => void;
};

type GraphNodeType = "table" | "column" | "concept" | "entity";
type EntryMode = "table" | "entity";
type SelectedItem = { id: string; kind: "edge" | "node" } | null;

type EdgeWithId = DatalinkEdgeDto & {
  id: string;
  sourceId: string;
  targetId: string;
};

type GraphIndexes = {
  edgeById: Map<string, EdgeWithId>;
  edgesByNode: Map<string, EdgeWithId[]>;
  edgeTypeCounts: Array<{ count: number; type: string }>;
  nodeById: Map<string, DatalinkNodeDto>;
};

type ExplorerNode = {
  depth: number;
  expanded: boolean;
  id: string;
  name: string;
  raw: DatalinkNodeDto;
  root: boolean;
  type: GraphNodeType | "other";
  val: number;
};

type ExplorerLink = {
  id: string;
  raw: EdgeWithId;
  source: string;
  target: string;
  type: string;
};

type ExplorerGraphData = {
  links: ExplorerLink[];
  nodes: ExplorerNode[];
};

type CanvasPoint = {
  x: number;
  y: number;
};

type PositionedNode = ExplorerNode & CanvasPoint & {
  radius: number;
};

type PositionedLink = ExplorerLink & {
  sourceNode: PositionedNode;
  targetNode: PositionedNode;
};

type CanvasLayout = {
  height: number;
  links: PositionedLink[];
  nodes: PositionedNode[];
  width: number;
};

type VisibleGraph = {
  activeLinkIds: Set<string>;
  activeNodeIds: Set<string>;
  data: ExplorerGraphData;
  hasFocus: boolean;
};

type GraphStats = Record<GraphNodeType | "edge", number>;

const GRAPH_NODE_TYPES: GraphNodeType[] = ["table", "column", "concept", "entity"];
const DEFAULT_CANVAS_HEIGHT = 620;

export function DataLinkPanel({ onBack, onOpenMcpSettings }: DataLinkPanelProps) {
  const t = useT();
  const [servers, setServers] = useState<DatalinkServerDto[]>([]);
  const [serverId, setServerId] = useState("");
  const [graph, setGraph] = useState<DatalinkGraphDto | null>(null);
  const [entryMode, setEntryMode] = useState<EntryMode>("table");
  const [rootNodeId, setRootNodeId] = useState("");
  const [activeNodeId, setActiveNodeId] = useState("");
  const [enabledEdgeTypes, setEnabledEdgeTypes] = useState<Set<string>>(() => new Set());
  const [selectedItem, setSelectedItem] = useState<SelectedItem>(null);
  const [hoverNodeId, setHoverNodeId] = useState<string | null>(null);
  const [hoverLinkId, setHoverLinkId] = useState<string | null>(null);
  const [loadingServers, setLoadingServers] = useState(true);
  const [loadingGraph, setLoadingGraph] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [entrySearch, setEntrySearch] = useState("");
  const [exploreQuery, setExploreQuery] = useState("");
  const [exploreFocus, setExploreFocus] = useState("");
  const [exploreResult, setExploreResult] = useState("");
  const [source, setSource] = useState("");
  const [sourceType, setSourceType] = useState("csv");
  const [table, setTable] = useState("");
  const [schemaName, setSchemaName] = useState("");
  const [actionResult, setActionResult] = useState("");
  const [busyAction, setBusyAction] = useState<string | null>(null);

  const selectedServer = servers.find((server) => server.id === serverId) ?? null;
  const indexes = useMemo(() => buildGraphIndexes(graph), [graph]);
  const graphStats = useMemo(() => summarizeGraph(graph), [graph]);
  const entryNodes = useMemo(
    () => rootEntryNodes(graph, entryMode, entrySearch),
    [entryMode, entrySearch, graph],
  );
  const entryNodeIdsKey = useMemo(() => entryNodes.map((node) => node.id).join("|"), [entryNodes]);
  const edgeTypeKey = useMemo(
    () => indexes.edgeTypeCounts.map((edgeType) => edgeType.type).join("|"),
    [indexes.edgeTypeCounts],
  );
  const visibleGraph = useMemo(
    () => buildVisibleGraph({
      enabledEdgeTypes,
      activeNodeId,
      hoverLinkId,
      hoverNodeId,
      indexes,
      rootNodeId,
      selectedItem,
    }),
    [activeNodeId, enabledEdgeTypes, hoverLinkId, hoverNodeId, indexes, rootNodeId, selectedItem],
  );
  const visibleEdgeTypeCounts = useMemo(
    () => countVisibleEdgeTypes(visibleGraph.data.links),
    [visibleGraph.data.links],
  );
  const selectedNode = selectedItem?.kind === "node"
    ? indexes.nodeById.get(selectedItem.id) ?? null
    : null;
  const selectedEdge = selectedItem?.kind === "edge"
    ? indexes.edgeById.get(selectedItem.id) ?? null
    : null;
  const tableNodes = useMemo(
    () => (graph?.nodes ?? []).filter((node) => node.type === "table"),
    [graph],
  );

  const loadServers = useCallback(async () => {
    setLoadingServers(true);
    setError(null);
    // Avoid flashing stale servers/graph from a previous open while refetching.
    setServers([]);
    setServerId("");
    setGraph(null);
    try {
      const response = await configApi.listDatalinkServers();
      setServers(response.servers);
      setServerId(response.servers[0]?.id || "");
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : t("dataLink.loadServersFailed"));
      setServers([]);
      setServerId("");
      setGraph(null);
    } finally {
      setLoadingServers(false);
    }
  }, [t]);

  const loadGraph = useCallback(async (targetServerId: string) => {
    if (!targetServerId) {
      setGraph(null);
      setLoadingGraph(false);
      return;
    }
    setLoadingGraph(true);
    setError(null);
    setGraph(null);
    try {
      const response = await configApi.getDatalinkGraph(targetServerId);
      setGraph(response.graph);
    } catch (loadError) {
      setGraph(null);
      setError(loadError instanceof Error ? loadError.message : t("dataLink.loadGraphFailed"));
    } finally {
      setLoadingGraph(false);
    }
  }, [t]);

  const selectRootNode = useCallback((nodeId: string) => {
    setRootNodeId(nodeId);
    setActiveNodeId(nodeId);
    setSelectedItem({ id: nodeId, kind: "node" });
    setHoverLinkId(null);
    setHoverNodeId(null);
  }, []);

  const runAction = async (label: string, action: () => Promise<string>, refresh = true, showResult = true) => {
    if (!serverId) return;
    setBusyAction(label);
    setError(null);
    setActionResult("");
    try {
      const result = await action();
      if (showResult) {
        setActionResult(result);
      }
      if (refresh) {
        await loadGraph(serverId);
      }
    } catch (actionError) {
      setError(actionError instanceof Error ? actionError.message : `${label} failed`);
    } finally {
      setBusyAction(null);
    }
  };

  useEffect(() => {
    void loadServers();
  }, [loadServers]);

  useEffect(() => {
    void loadGraph(serverId);
  }, [loadGraph, serverId]);

  useEffect(() => {
    setEnabledEdgeTypes(new Set(indexes.edgeTypeCounts.map((edgeType) => edgeType.type)));
  }, [edgeTypeKey, indexes.edgeTypeCounts]);

  useEffect(() => {
    if (entryNodes.length === 0) {
      setRootNodeId("");
      setActiveNodeId("");
      setSelectedItem(null);
      return;
    }
    if (!rootNodeId || !entryNodes.some((node) => node.id === rootNodeId)) {
      selectRootNode(entryNodes[0]?.id ?? "");
    }
  }, [entryNodeIdsKey, entryNodes, rootNodeId, selectRootNode]);

  const handleNodeClick = useCallback((nodeId: string) => {
    setSelectedItem({ id: nodeId, kind: "node" });
    setActiveNodeId(nodeId);
  }, []);

  const handleLinkClick = useCallback((linkId: string) => {
    setSelectedItem({ id: linkId, kind: "edge" });
  }, []);

  const resetExplorer = useCallback(() => {
    if (rootNodeId) {
      setActiveNodeId(rootNodeId);
      setSelectedItem({ id: rootNodeId, kind: "node" });
    }
    setHoverLinkId(null);
    setHoverNodeId(null);
  }, [rootNodeId]);

  const collapseSelectedNode = useCallback(() => {
    if (selectedItem?.kind !== "node" || selectedItem.id === rootNodeId) return;
    setActiveNodeId(rootNodeId);
    setSelectedItem({ id: rootNodeId, kind: "node" });
  }, [rootNodeId, selectedItem]);

  return (
    <div className="flex min-h-0 min-w-0 flex-col overflow-hidden bg-surface">
      <PanelHeader
        graph={graph}
        loadingGraph={loadingGraph}
        onBack={onBack}
        onOpenMcpSettings={onOpenMcpSettings}
        onRefresh={() => void loadGraph(serverId)}
      />

      <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
        <div className="flex flex-col gap-4">
          {error ? <ErrorBanner message={error} /> : null}

          {loadingServers ? (
            <LoadingServersState />
          ) : (
            <>
              <ServerToolbar
                loadingServers={loadingServers}
                serverId={serverId}
                servers={servers}
                onServerChange={setServerId}
              />

              {servers.length === 0 ? (
                <EmptyGraphState onOpenMcpSettings={onOpenMcpSettings} />
              ) : (
                <>
                  <StatsStrip graphStats={graphStats} visibleGraph={visibleGraph.data} />
                  <section className={explorerShellClass}>
                    <RootEntryPanel
                      entryMode={entryMode}
                      entryNodes={entryNodes}
                      entrySearch={entrySearch}
                      rootNodeId={rootNodeId}
                      onEntryModeChange={setEntryMode}
                      onEntrySearchChange={setEntrySearch}
                      onSelectRoot={selectRootNode}
                    />
                    <div className="min-w-0 space-y-3">
                      <EdgeTypeToolbar
                        edgeTypeCounts={indexes.edgeTypeCounts}
                        enabledEdgeTypes={enabledEdgeTypes}
                        visibleEdgeTypeCounts={visibleEdgeTypeCounts}
                        onReset={resetExplorer}
                        onToggleEdgeType={(type) =>
                          setEnabledEdgeTypes((current) => toggleSetValue(current, type))
                        }
                      />
                      <DataLinkCanvas
                        activeLinkIds={visibleGraph.activeLinkIds}
                        activeNodeIds={visibleGraph.activeNodeIds}
                        graphData={visibleGraph.data}
                        hasFocus={visibleGraph.hasFocus}
                        isLoading={loadingGraph}
                        selectedItem={selectedItem}
                        onLinkClick={handleLinkClick}
                        onLinkHover={setHoverLinkId}
                        onNodeClick={handleNodeClick}
                        onNodeHover={setHoverNodeId}
                      />
                    </div>
                    <InspectorPanel
                      activeNodeId={activeNodeId}
                      edge={selectedEdge}
                      node={selectedNode}
                      rootNodeId={rootNodeId}
                      server={selectedServer}
                      onCollapseNode={collapseSelectedNode}
                      onExpandNode={setActiveNodeId}
                    />
                  </section>

                  <section className="grid gap-4 xl:grid-cols-3">
                    <ExplorePanel
                      busy={busyAction === "explore"}
                      focus={exploreFocus}
                      query={exploreQuery}
                      result={exploreResult}
                      onFocusChange={setExploreFocus}
                      onQueryChange={setExploreQuery}
                      onRun={() =>
                        runAction(
                          "explore",
                          async () => {
                            const response = await configApi.exploreDatalink(serverId, {
                              query: exploreQuery,
                              ...(exploreFocus ? { focus: exploreFocus } : {}),
                              maskCredential: true,
                              maxNodes: 12,
                            });
                            setExploreResult(response.result);
                            return response.result;
                          },
                          false,
                          false,
                        )
                      }
                    />
                    <AddTablePanel
                      busy={busyAction === "add_table"}
                      schemaName={schemaName}
                      source={source}
                      sourceType={sourceType}
                      table={table}
                      onRun={() =>
                        runAction("add_table", async () => {
                          const response = await configApi.addDatalinkTable(serverId, {
                            source,
                            sourceType,
                            ...(schemaName.trim() ? { schemaName } : {}),
                            ...(table.trim() ? { table } : {}),
                          });
                          return response.result;
                        })
                      }
                      onSchemaNameChange={setSchemaName}
                      onSourceChange={setSource}
                      onSourceTypeChange={setSourceType}
                      onTableChange={setTable}
                    />
                    <GraphMaintenancePanel
                      actionResult={actionResult}
                      busyAction={busyAction}
                      selectedNode={selectedNode}
                      tableNodes={tableNodes}
                      onRebuild={() =>
                        runAction("rebuild", async () => {
                          const response = await configApi.rebuildDatalink(serverId);
                          return response.result;
                        })
                      }
                      onRemoveTable={(tableId) =>
                        runAction("remove_table", async () => {
                          const response = await configApi.removeDatalinkTable(serverId, {
                            cleanupOrphans: true,
                            tableId,
                          });
                          return response.result;
                        })
                      }
                    />
                  </section>
                </>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function PanelHeader({
  graph,
  loadingGraph,
  onBack,
  onOpenMcpSettings,
  onRefresh,
}: {
  graph: DatalinkGraphDto | null;
  loadingGraph: boolean;
  onBack: () => void;
  onOpenMcpSettings: () => void;
  onRefresh: () => void;
}) {
  const t = useT();
  return (
    <div className="flex h-16 items-center gap-3 border-b border-border px-4">
      <button
        type="button"
        onClick={onBack}
        className={backButtonClass}
        aria-label={t("common.backToWorkspace")}
        title={t("common.backToWorkspace")}
      >
        <BackIcon />
      </button>
      <div className="min-w-0 flex-1">
        <h2 className={panelTitleClass}>{t("dataLink.title")}</h2>
        <p className="text-xs text-muted-light">
          {graph ? t("dataLink.nodesEdges", { nodes: graph.nodes.length, edges: graph.edges.length }) : t("dataLink.subtitle")}
        </p>
      </div>
      <button type="button" onClick={onRefresh} className={btnSecondaryClass}>
        {loadingGraph ? t("common.refreshing") : t("common.refresh")}
      </button>
      <button type="button" onClick={onOpenMcpSettings} className={btnSecondaryClass}>
        {t("dataLink.mcpSettings")}
      </button>
    </div>
  );
}

function ServerToolbar({
  loadingServers,
  serverId,
  servers,
  onServerChange,
}: {
  loadingServers: boolean;
  serverId: string;
  servers: DatalinkServerDto[];
  onServerChange: (value: string) => void;
}) {
  const t = useT();
  return (
    <section className="flex flex-wrap items-end gap-3 border-b border-border pb-4">
      <label className="min-w-[260px] flex-1">
        <span className={sectionLabelClass}>{t("dataLink.server")}</span>
        <select
          value={serverId}
          disabled={loadingServers || servers.length === 0}
          onChange={(event) => onServerChange(event.target.value)}
          className={`${fieldClass} mt-1 w-full`}
        >
          {servers.length === 0 ? <option value="">{t("dataLink.noServer")}</option> : null}
          {servers.map((server) => (
            <option key={server.id} value={server.id}>
              {server.name}
            </option>
          ))}
        </select>
      </label>
    </section>
  );
}

function StatsStrip({ graphStats, visibleGraph }: { graphStats: GraphStats; visibleGraph: ExplorerGraphData }) {
  const t = useT();
  return (
    <section className="grid gap-3 md:grid-cols-5">
      <StatPill label={t("dataLink.tables")} value={graphStats.table} />
      <StatPill label={t("dataLink.entities")} value={graphStats.entity} />
      <StatPill label={t("dataLink.columns")} value={graphStats.column} />
      <StatPill label={t("dataLink.edges")} value={graphStats.edge} />
      <StatPill label={t("dataLink.visible")} value={`${visibleGraph.nodes.length}/${visibleGraph.links.length}`} />
    </section>
  );
}

function RootEntryPanel({
  entryMode,
  entryNodes,
  entrySearch,
  rootNodeId,
  onEntryModeChange,
  onEntrySearchChange,
  onSelectRoot,
}: {
  entryMode: EntryMode;
  entryNodes: DatalinkNodeDto[];
  entrySearch: string;
  rootNodeId: string;
  onEntryModeChange: (value: EntryMode) => void;
  onEntrySearchChange: (value: string) => void;
  onSelectRoot: (nodeId: string) => void;
}) {
  const t = useT();
  return (
    <aside className={sidePanelClass}>
      <div className={sectionLabelClass}>{t("dataLink.entry")}</div>
      <div className="mt-3 grid grid-cols-2 gap-1 rounded-lg border border-border bg-surface-subtle p-1">
        {(["table", "entity"] as EntryMode[]).map((mode) => (
          <button
            key={mode}
            type="button"
            onClick={() => onEntryModeChange(mode)}
            className={[
              "h-8 rounded-md text-xs font-semibold capitalize transition",
              entryMode === mode
                ? "bg-white text-slate-900 shadow-[var(--shadow-card)]"
                : "text-muted-light hover:text-foreground",
            ].join(" ")}
          >
            {mode === "table" ? t("dataLink.tables") : t("dataLink.entities")}
          </button>
        ))}
      </div>
      <input
        value={entrySearch}
        onChange={(event) => onEntrySearchChange(event.target.value)}
        placeholder={t("dataLink.searchEntry", { mode: entryMode === "table" ? t("dataLink.tables") : t("dataLink.entities") })}
        className={`${fieldClass} mt-3 w-full`}
      />
      <div className="mt-3 max-h-[516px] space-y-1 overflow-y-auto pr-1">
        {entryNodes.map((node) => (
          <button
            key={node.id}
            type="button"
            onClick={() => onSelectRoot(node.id)}
            className={[
              "w-full rounded-lg border px-3 py-2 text-left transition",
              rootNodeId === node.id
                ? "border-cyan-200 bg-cyan-50 text-cyan-950"
                : "border-transparent bg-white text-slate-700 hover:border-border hover:bg-surface-subtle",
            ].join(" ")}
          >
            <span className="block truncate text-sm font-semibold">{nodeLabel(node)}</span>
            <span className="mt-0.5 block truncate text-[11px] text-muted-light">{node.id}</span>
          </button>
        ))}
        {entryNodes.length === 0 ? (
          <div className="rounded-lg border border-dashed border-border bg-surface-subtle px-3 py-5 text-center">
            <span className="text-xs text-muted-light">{t("dataLink.noEntries")}</span>
          </div>
        ) : null}
      </div>
    </aside>
  );
}

function EdgeTypeToolbar({
  edgeTypeCounts,
  enabledEdgeTypes,
  visibleEdgeTypeCounts,
  onReset,
  onToggleEdgeType,
}: {
  edgeTypeCounts: Array<{ count: number; type: string }>;
  enabledEdgeTypes: Set<string>;
  visibleEdgeTypeCounts: Map<string, number>;
  onReset: () => void;
  onToggleEdgeType: (type: string) => void;
}) {
  const t = useT();
  return (
    <div className="rounded-lg border border-border bg-white px-3 py-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className={sectionLabelClass}>{t("dataLink.edges")}</span>
        <div className="flex min-w-0 flex-1 flex-wrap gap-1.5">
          {edgeTypeCounts.map((edgeType) => (
            <button
              key={edgeType.type}
              type="button"
              onClick={() => onToggleEdgeType(edgeType.type)}
              className={[
                "h-7 rounded-full border px-2.5 text-[11px] font-semibold transition",
                enabledEdgeTypes.has(edgeType.type)
                  ? "border-cyan-200 bg-cyan-50 text-cyan-900"
                  : "border-slate-200 bg-slate-50 text-slate-400",
              ].join(" ")}
            >
              {edgeType.type}
              <span className="ml-1 text-[10px] tabular-nums opacity-70">
                {visibleEdgeTypeCounts.get(edgeType.type) ?? 0}/{edgeType.count}
              </span>
            </button>
          ))}
        </div>
        <button type="button" onClick={onReset} className={btnSecondaryClass}>
          {t("common.reset")}
        </button>
      </div>
    </div>
  );
}

function DataLinkCanvas({
  activeLinkIds,
  activeNodeIds,
  graphData,
  hasFocus,
  isLoading,
  selectedItem,
  onLinkClick,
  onLinkHover,
  onNodeClick,
  onNodeHover,
}: {
  activeLinkIds: Set<string>;
  activeNodeIds: Set<string>;
  graphData: ExplorerGraphData;
  hasFocus: boolean;
  isLoading: boolean;
  selectedItem: SelectedItem;
  onLinkClick: (linkId: string) => void;
  onLinkHover: (linkId: string | null) => void;
  onNodeClick: (nodeId: string) => void;
  onNodeHover: (nodeId: string | null) => void;
}) {
  const t = useT();
  const containerRef = useRef<HTMLDivElement | null>(null);
  const svgRef = useRef<SVGSVGElement | null>(null);
  const [dragNodeId, setDragNodeId] = useState<string | null>(null);
  const [pinnedPositions, setPinnedPositions] = useState<Record<string, CanvasPoint>>({});
  const [size, setSize] = useState({ height: DEFAULT_CANVAS_HEIGHT, width: 900 });

  useEffect(() => {
    const element = containerRef.current;
    if (!element) return;
    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (!entry) return;
      setSize({
        height: DEFAULT_CANVAS_HEIGHT,
        width: Math.max(320, Math.floor(entry.contentRect.width)),
      });
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    setPinnedPositions((current) => {
      const visibleNodeIds = new Set(graphData.nodes.map((node) => node.id));
      const next: Record<string, CanvasPoint> = {};
      for (const [nodeId, position] of Object.entries(current)) {
        if (visibleNodeIds.has(nodeId)) {
          next[nodeId] = position;
        }
      }
      return Object.keys(next).length === Object.keys(current).length ? current : next;
    });
  }, [graphData]);

  const layout = useMemo(
    () => buildCanvasLayout(graphData, size.width, size.height, pinnedPositions),
    [graphData, pinnedPositions, size.height, size.width],
  );

  const pointerPoint = useCallback((event: ReactPointerEvent<SVGSVGElement>): CanvasPoint => {
    const svg = svgRef.current;
    if (!svg) return { x: 0, y: 0 };
    const rect = svg.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) return { x: 0, y: 0 };
    return {
      x: ((event.clientX - rect.left) / rect.width) * layout.width,
      y: ((event.clientY - rect.top) / rect.height) * layout.height,
    };
  }, [layout.height, layout.width]);

  return (
    <div ref={containerRef} className={canvasShellClass}>
      <svg
        ref={svgRef}
        role="img"
        aria-label="Datalink nodes and relationships"
        data-testid="datalink-canvas-svg"
        className="h-full w-full touch-none"
        viewBox={`0 0 ${layout.width} ${layout.height}`}
        onPointerLeave={() => {
          setDragNodeId(null);
          onLinkHover(null);
          onNodeHover(null);
        }}
        onPointerMove={(event) => {
          if (!dragNodeId) return;
          const point = pointerPoint(event);
          setPinnedPositions((current) => ({
            ...current,
            [dragNodeId]: {
              x: clamp(point.x, 34, layout.width - 34),
              y: clamp(point.y, 38, layout.height - 38),
            },
          }));
        }}
        onPointerUp={() => setDragNodeId(null)}
      >
        <defs>
          <pattern id="datalink-dot-grid" width="28" height="28" patternUnits="userSpaceOnUse">
            <circle cx="1" cy="1" r="1" fill="#cbd5e1" opacity="0.58" />
          </pattern>
          <filter id="datalink-node-shadow" x="-30%" y="-30%" width="160%" height="160%">
            <feDropShadow dx="0" dy="7" floodColor="#0f172a" floodOpacity="0.14" stdDeviation="8" />
          </filter>
        </defs>
        <rect width={layout.width} height={layout.height} fill="#f8fafc" />
        <rect width={layout.width} height={layout.height} fill="url(#datalink-dot-grid)" />
        <g>
          {layout.links.map((link) => (
            <CanvasLink
              key={link.id}
              activeLinkIds={activeLinkIds}
              hasFocus={hasFocus}
              link={link}
              selected={selectedItem?.kind === "edge" && selectedItem.id === link.id}
              onClick={() => onLinkClick(link.id)}
              onHover={onLinkHover}
            />
          ))}
        </g>
        <g>
          {layout.nodes.map((node) => (
            <CanvasNode
              key={node.id}
              activeNodeIds={activeNodeIds}
              hasFocus={hasFocus}
              node={node}
              selected={selectedItem?.kind === "node" && selectedItem.id === node.id}
              onHover={onNodeHover}
              onPointerDown={(event) => {
                event.preventDefault();
                event.currentTarget.setPointerCapture(event.pointerId);
                setDragNodeId(node.id);
                onNodeClick(node.id);
              }}
            />
          ))}
        </g>
      </svg>
      {isLoading ? <CanvasOverlay title={t("dataLink.loading")} /> : null}
      {!isLoading && graphData.nodes.length === 0 ? <CanvasOverlay title={t("dataLink.noVisibleNodes")} /> : null}
    </div>
  );
}

function CanvasOverlay({ title }: { title: string }) {
  return (
    <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
      <div className="rounded-lg border border-border bg-white/85 px-4 py-2 text-sm font-medium text-muted">
        {title}
      </div>
    </div>
  );
}

function InspectorPanel({
  activeNodeId,
  edge,
  node,
  rootNodeId,
  server,
  onCollapseNode,
  onExpandNode,
}: {
  activeNodeId: string;
  edge: EdgeWithId | null;
  node: DatalinkNodeDto | null;
  rootNodeId: string;
  server: DatalinkServerDto | null;
  onCollapseNode: () => void;
  onExpandNode: (nodeId: string) => void;
}) {
  const t = useT();
  const properties = node?.properties ?? {};
  const propertyEntries = Object.entries(properties).slice(0, 12);
  const edgeProperties = edge?.properties ?? {};
  const edgePropertyEntries = Object.entries(edgeProperties).slice(0, 12);

  return (
    <aside className={sidePanelClass}>
      <div className={sectionLabelClass}>{t("dataLink.inspector")}</div>
      {node ? (
        <div className="mt-3 space-y-3">
          <div>
            <h3 className="break-words text-sm font-semibold text-slate-950">{nodeLabel(node)}</h3>
            <p className="mt-1 text-xs text-slate-500">{node.type}</p>
          </div>
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              onClick={() => onExpandNode(node.id)}
              className={smallActionClass}
            >
              {t("dataLink.focus")}
            </button>
            <button
              type="button"
              disabled={node.id === rootNodeId || activeNodeId !== node.id}
              onClick={onCollapseNode}
              className={smallActionClass}
            >
              {t("dataLink.backToRoot")}
            </button>
          </div>
          <DetailRow label="ID" value={node.id} />
          {propertyEntries.map(([key, value]) => (
            <DetailRow key={key} label={key} value={formatPropertyValue(value)} />
          ))}
        </div>
      ) : edge ? (
        <div className="mt-3 space-y-3">
          <div>
            <h3 className="break-words text-sm font-semibold text-slate-950">{edge.type}</h3>
            <p className="mt-1 break-all text-xs leading-5 text-slate-500">
              {edge.sourceId}
              {" -> "}
              {edge.targetId}
            </p>
          </div>
          <DetailRow label="ID" value={edge.id} />
          {edge.confidence !== undefined ? <DetailRow label={t("dataLink.confidence")} value={String(edge.confidence)} /> : null}
          {edgePropertyEntries.map(([key, value]) => (
            <DetailRow key={key} label={key} value={formatPropertyValue(value)} />
          ))}
        </div>
      ) : (
        <div className="mt-3 space-y-3 text-sm text-slate-500">
          <p>{server ? server.name : t("dataLink.noServerSelected")}</p>
          {server?.serverUrl ? <DetailRow label={t("dataLink.endpoint")} value={server.serverUrl} /> : null}
        </div>
      )}
    </aside>
  );
}

function LoadingServersState() {
  const t = useT();
  return (
    <section className="rounded-lg border border-dashed border-slate-300 bg-slate-50 px-5 py-8 text-center">
      <p className="text-sm font-semibold text-slate-800">{t("dataLink.loading")}</p>
    </section>
  );
}

function EmptyGraphState({ onOpenMcpSettings }: { onOpenMcpSettings: () => void }) {
  const t = useT();
  return (
    <section className="rounded-lg border border-dashed border-slate-300 bg-slate-50 px-5 py-8 text-center">
      <p className="text-sm font-semibold text-slate-800">{t("dataLink.emptyTitle")}</p>
      <button type="button" onClick={onOpenMcpSettings} className={`mt-4 ${btnSecondaryClass}`}>
        {t("dataLink.openMcpSettings")}
      </button>
    </section>
  );
}

function ErrorBanner({ message }: { message: string }) {
  return (
    <div className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-800">
      {message}
    </div>
  );
}

function StatPill({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="rounded-lg border border-border bg-white px-4 py-3">
      <div className="text-[10px] font-semibold uppercase tracking-[0.06em] text-slate-400">{label}</div>
      <div className="mt-1 text-lg font-semibold text-slate-950">{value}</div>
    </div>
  );
}

function DetailRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-slate-100 bg-slate-50 px-3 py-2">
      <div className="text-[10px] font-semibold uppercase tracking-[0.06em] text-slate-400">{label}</div>
      <div className="mt-1 max-h-24 overflow-y-auto break-words text-xs text-slate-700">{value}</div>
    </div>
  );
}

function ExplorePanel({
  busy,
  focus,
  query,
  result,
  onFocusChange,
  onQueryChange,
  onRun,
}: {
  busy: boolean;
  focus: string;
  query: string;
  result: string;
  onFocusChange: (value: string) => void;
  onQueryChange: (value: string) => void;
  onRun: () => void;
}) {
  const t = useT();
  return (
    <section className={toolPanelClass}>
      <div className={sectionLabelClass}>{t("dataLink.explore")}</div>
      <input
        value={query}
        onChange={(event) => onQueryChange(event.target.value)}
        placeholder="satscores cds"
        className={`${fieldClass} w-full`}
      />
      <select
        value={focus}
        onChange={(event) => onFocusChange(event.target.value)}
        className={`${fieldClass} w-full`}
      >
        <option value="">{t("dataLink.balanced")}</option>
        <option value="join_paths">{t("dataLink.joinPaths")}</option>
        <option value="schema">{t("dataLink.schemaFocus")}</option>
        <option value="data_profile">{t("dataLink.dataProfile")}</option>
      </select>
      <button type="button" disabled={busy || !query.trim()} onClick={onRun} className={primaryButtonClass}>
        {busy ? t("dataLink.exploring") : t("dataLink.explore")}
      </button>
      {result ? <ResultBlock value={result} /> : null}
    </section>
  );
}

function AddTablePanel({
  busy,
  schemaName,
  source,
  sourceType,
  table,
  onRun,
  onSchemaNameChange,
  onSourceChange,
  onSourceTypeChange,
  onTableChange,
}: {
  busy: boolean;
  schemaName: string;
  source: string;
  sourceType: string;
  table: string;
  onRun: () => void;
  onSchemaNameChange: (value: string) => void;
  onSourceChange: (value: string) => void;
  onSourceTypeChange: (value: string) => void;
  onTableChange: (value: string) => void;
}) {
  const t = useT();
  return (
    <section className={toolPanelClass}>
      <div className={sectionLabelClass}>{t("dataLink.addTable")}</div>
      <input
        value={source}
        onChange={(event) => onSourceChange(event.target.value)}
        placeholder={t("dataLink.sourcePlaceholder")}
        className={`${fieldClass} w-full`}
      />
      <div className="grid gap-2 sm:grid-cols-2">
        <select
          value={sourceType}
          onChange={(event) => onSourceTypeChange(event.target.value)}
          className={fieldClass}
        >
          <option value="csv">CSV</option>
          <option value="parquet">Parquet</option>
          <option value="database">Database</option>
        </select>
        <input
          value={table}
          onChange={(event) => onTableChange(event.target.value)}
          placeholder={t("dataLink.tablePlaceholder")}
          className={fieldClass}
        />
      </div>
      <input
        value={schemaName}
        onChange={(event) => onSchemaNameChange(event.target.value)}
        placeholder={t("dataLink.schemaPlaceholder")}
        className={`${fieldClass} w-full`}
      />
      <button type="button" disabled={busy || !source.trim()} onClick={onRun} className={primaryButtonClass}>
        {busy ? t("dataLink.adding") : t("dataLink.addTableAction")}
      </button>
    </section>
  );
}

function GraphMaintenancePanel({
  actionResult,
  busyAction,
  selectedNode,
  tableNodes,
  onRebuild,
  onRemoveTable,
}: {
  actionResult: string;
  busyAction: string | null;
  selectedNode: DatalinkNodeDto | null;
  tableNodes: DatalinkNodeDto[];
  onRebuild: () => void;
  onRemoveTable: (tableId: string) => void;
}) {
  const t = useT();
  const [tableId, setTableId] = useState("");
  useEffect(() => {
    if (selectedNode?.type === "table") {
      setTableId(selectedNode.id);
    }
  }, [selectedNode]);

  return (
    <section className={toolPanelClass}>
      <div className={sectionLabelClass}>{t("dataLink.maintain")}</div>
      <select
        value={tableId}
        onChange={(event) => setTableId(event.target.value)}
        className={`${fieldClass} w-full`}
      >
        <option value="">{t("dataLink.selectTable")}</option>
        {tableNodes.map((node) => (
          <option key={node.id} value={node.id}>
            {node.name || node.id}
          </option>
        ))}
      </select>
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          disabled={busyAction === "remove_table" || !tableId}
          onClick={() => {
            if (window.confirm(t("dataLink.confirmRemoveTable"))) {
              onRemoveTable(tableId);
            }
          }}
          className={dangerButtonClass}
        >
          {busyAction === "remove_table" ? t("dataLink.removing") : t("dataLink.removeTable")}
        </button>
        <button
          type="button"
          disabled={busyAction === "rebuild"}
          onClick={() => {
            if (window.confirm(t("dataLink.confirmRebuild"))) {
              onRebuild();
            }
          }}
          className={btnSecondaryClass}
        >
          {busyAction === "rebuild" ? t("dataLink.rebuilding") : t("dataLink.rebuild")}
        </button>
      </div>
      {actionResult ? <ResultBlock value={actionResult} /> : null}
    </section>
  );
}

function ResultBlock({ value }: { value: string }) {
  return (
    <pre className={resultBlockClass}>
      {value}
    </pre>
  );
}

function BackIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      className="h-4 w-4"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="m15 18-6-6 6-6" />
    </svg>
  );
}

function buildGraphIndexes(graph: DatalinkGraphDto | null): GraphIndexes {
  const nodeById = new Map((graph?.nodes ?? []).map((node) => [node.id, node]));
  const edgeById = new Map<string, EdgeWithId>();
  const edgesByNode = new Map<string, EdgeWithId[]>();
  const edgeTypeCounter = new Map<string, number>();

  (graph?.edges ?? []).forEach((edge, index) => {
    const sourceId = edgeSource(edge);
    const targetId = edgeTarget(edge);
    if (!sourceId || !targetId) return;
    const id = edge.id || `${sourceId}:${targetId}:${edge.type}:${index}`;
    const normalized = { ...edge, id, sourceId, targetId };
    edgeById.set(id, normalized);
    edgeTypeCounter.set(edge.type, (edgeTypeCounter.get(edge.type) ?? 0) + 1);
    for (const nodeId of [sourceId, targetId]) {
      const current = edgesByNode.get(nodeId) ?? [];
      current.push(normalized);
      edgesByNode.set(nodeId, current);
    }
  });

  return {
    edgeById,
    edgesByNode,
    edgeTypeCounts: Array.from(edgeTypeCounter.entries())
      .map(([type, count]) => ({ count, type }))
      .sort((left, right) => left.type.localeCompare(right.type)),
    nodeById,
  };
}

function buildVisibleGraph({
  activeNodeId,
  enabledEdgeTypes,
  hoverLinkId,
  hoverNodeId,
  indexes,
  rootNodeId,
  selectedItem,
}: {
  activeNodeId: string;
  enabledEdgeTypes: Set<string>;
  hoverLinkId: string | null;
  hoverNodeId: string | null;
  indexes: GraphIndexes;
  rootNodeId: string;
  selectedItem: SelectedItem;
}): VisibleGraph {
  if (!rootNodeId || !indexes.nodeById.has(rootNodeId)) {
    return emptyVisibleGraph();
  }

  const focusNodeId = activeNodeId && indexes.nodeById.has(activeNodeId) ? activeNodeId : rootNodeId;
  const visibleNodeIds = new Set([rootNodeId]);
  visibleNodeIds.add(focusNodeId);
  for (const edge of indexes.edgesByNode.get(focusNodeId) ?? []) {
    if (!enabledEdgeTypes.has(edge.type)) continue;
    visibleNodeIds.add(edge.sourceId);
    visibleNodeIds.add(edge.targetId);
  }

  const visibleEdges = Array.from(indexes.edgeById.values()).filter((edge) =>
    enabledEdgeTypes.has(edge.type) && visibleNodeIds.has(edge.sourceId) && visibleNodeIds.has(edge.targetId));
  const depthByNode = computeDepths(rootNodeId, visibleEdges);
  const nodes = Array.from(visibleNodeIds).flatMap((nodeId) => {
    const node = indexes.nodeById.get(nodeId);
    if (!node) return [];
    return [{
      depth: nodeId === rootNodeId ? 0 : depthByNode.get(nodeId) ?? 1,
      expanded: nodeId === focusNodeId,
      id: node.id,
      name: nodeLabel(node),
      raw: node,
      root: node.id === rootNodeId,
      type: graphNodeType(node),
      val: node.id === rootNodeId ? 8 : node.type === "table" || node.type === "entity" ? 5.5 : 3.5,
    }];
  });
  const nodeIdSet = new Set(nodes.map((node) => node.id));
  const links = visibleEdges.filter((edge) => nodeIdSet.has(edge.sourceId) && nodeIdSet.has(edge.targetId))
    .map((edge) => ({
      id: edge.id,
      raw: edge,
      source: edge.sourceId,
      target: edge.targetId,
      type: edge.type,
    }));
  const focus = focusIds({ hoverLinkId, hoverNodeId, links, selectedItem });
  return {
    activeLinkIds: focus.activeLinkIds,
    activeNodeIds: focus.activeNodeIds,
    data: { links, nodes },
    hasFocus: focus.hasFocus,
  };
}

function emptyVisibleGraph(): VisibleGraph {
  return {
    activeLinkIds: new Set(),
    activeNodeIds: new Set(),
    data: { links: [], nodes: [] },
    hasFocus: false,
  };
}

function focusIds({
  hoverLinkId,
  hoverNodeId,
  links,
  selectedItem,
}: {
  hoverLinkId: string | null;
  hoverNodeId: string | null;
  links: ExplorerLink[];
  selectedItem: SelectedItem;
}): { activeLinkIds: Set<string>; activeNodeIds: Set<string>; hasFocus: boolean } {
  const activeNodeIds = new Set<string>();
  const activeLinkIds = new Set<string>();
  const selectedNodeId = selectedItem?.kind === "node" ? selectedItem.id : null;
  const selectedLinkId = selectedItem?.kind === "edge" ? selectedItem.id : null;
  const focusNodeId = hoverNodeId ?? selectedNodeId;
  const focusLinkId = hoverLinkId ?? selectedLinkId;

  if (focusLinkId) {
    const link = links.find((item) => item.id === focusLinkId);
    if (link) {
      activeLinkIds.add(link.id);
      activeNodeIds.add(linkEndpointId(link.source));
      activeNodeIds.add(linkEndpointId(link.target));
      return { activeLinkIds, activeNodeIds, hasFocus: true };
    }
  }
  if (focusNodeId) {
    activeNodeIds.add(focusNodeId);
    for (const link of links) {
      const sourceId = linkEndpointId(link.source);
      const targetId = linkEndpointId(link.target);
      if (sourceId === focusNodeId || targetId === focusNodeId) {
        activeLinkIds.add(link.id);
        activeNodeIds.add(sourceId);
        activeNodeIds.add(targetId);
      }
    }
    return { activeLinkIds, activeNodeIds, hasFocus: true };
  }
  return { activeLinkIds, activeNodeIds, hasFocus: false };
}

function computeDepths(rootNodeId: string, edges: EdgeWithId[]): Map<string, number> {
  const adjacency = new Map<string, string[]>();
  for (const edge of edges) {
    adjacency.set(edge.sourceId, [...adjacency.get(edge.sourceId) ?? [], edge.targetId]);
    adjacency.set(edge.targetId, [...adjacency.get(edge.targetId) ?? [], edge.sourceId]);
  }
  const depthByNode = new Map([[rootNodeId, 0]]);
  const queue = [rootNodeId];
  while (queue.length > 0) {
    const nodeId = queue.shift() ?? "";
    const depth = depthByNode.get(nodeId) ?? 0;
    for (const nextId of adjacency.get(nodeId) ?? []) {
      if (depthByNode.has(nextId)) continue;
      depthByNode.set(nextId, depth + 1);
      queue.push(nextId);
    }
  }
  return depthByNode;
}

function rootEntryNodes(
  graph: DatalinkGraphDto | null,
  entryMode: EntryMode,
  search: string,
): DatalinkNodeDto[] {
  const normalizedSearch = search.trim().toLowerCase();
  return (graph?.nodes ?? [])
    .filter((node) => node.type === entryMode)
    .filter((node) => !normalizedSearch || nodeSearchText(node).includes(normalizedSearch))
    .sort((left, right) => nodeLabel(left).localeCompare(nodeLabel(right)));
}

function summarizeGraph(graph: DatalinkGraphDto | null): GraphStats {
  const initial = { column: 0, concept: 0, edge: graph?.edges.length ?? 0, entity: 0, table: 0 };
  for (const node of graph?.nodes ?? []) {
    const type = graphNodeType(node);
    if (type !== "other") {
      initial[type] += 1;
    }
  }
  return initial;
}

function countVisibleEdgeTypes(links: ExplorerLink[]): Map<string, number> {
  const counts = new Map<string, number>();
  for (const link of links) {
    counts.set(link.type, (counts.get(link.type) ?? 0) + 1);
  }
  return counts;
}

function CanvasLink({
  activeLinkIds,
  hasFocus,
  link,
  selected,
  onClick,
  onHover,
}: {
  activeLinkIds: Set<string>;
  hasFocus: boolean;
  link: PositionedLink;
  selected: boolean;
  onClick: () => void;
  onHover: (linkId: string | null) => void;
}) {
  const active = !hasFocus || activeLinkIds.has(link.id) || selected;
  const path = curvedPath(link.sourceNode, link.targetNode);
  const labelPosition = curveLabelPoint(link.sourceNode, link.targetNode);

  return (
    <g
      className="cursor-pointer"
      data-link-id={link.id}
      opacity={active ? 1 : 0.2}
      onClick={onClick}
      onMouseEnter={() => onHover(link.id)}
      onMouseLeave={() => onHover(null)}
    >
      <path d={path} fill="none" stroke="transparent" strokeWidth="16" />
      <path
        d={path}
        fill="none"
        stroke={selected ? "#0f766e" : edgeColor(link.type, active)}
        strokeLinecap="round"
        strokeWidth={selected ? 3.2 : active ? 2.1 : 1.2}
      />
      {selected ? (
        <g pointerEvents="none" transform={`translate(${labelPosition.x} ${labelPosition.y})`}>
          <rect x="-48" y="-11" width="96" height="22" rx="7" fill="#ffffff" stroke="#cbd5e1" />
          <text
            fill="#334155"
            fontSize="11"
            fontWeight="700"
            textAnchor="middle"
            dominantBaseline="middle"
          >
            {shortLabel(link.type, 14)}
          </text>
        </g>
      ) : null}
    </g>
  );
}

function CanvasNode({
  activeNodeIds,
  hasFocus,
  node,
  selected,
  onHover,
  onPointerDown,
}: {
  activeNodeIds: Set<string>;
  hasFocus: boolean;
  node: PositionedNode;
  selected: boolean;
  onHover: (nodeId: string | null) => void;
  onPointerDown: (event: ReactPointerEvent<SVGGElement>) => void;
}) {
  const active = !hasFocus || activeNodeIds.has(node.id) || selected;
  const palette = nodePalette(node.type);
  const label = shortLabel(node.name, node.root ? 22 : 18);
  const labelWidth = Math.min(150, Math.max(44, label.length * 7 + 18));

  return (
    <g
      className="cursor-grab active:cursor-grabbing"
      data-node-id={node.id}
      filter={active ? "url(#datalink-node-shadow)" : undefined}
      opacity={active ? 1 : 0.32}
      transform={`translate(${node.x} ${node.y})`}
      onMouseEnter={() => onHover(node.id)}
      onMouseLeave={() => onHover(null)}
      onPointerDown={onPointerDown}
    >
      <circle
        r={node.radius + (selected ? 8 : node.root ? 6 : 4)}
        fill={selected ? palette.ring : palette.soft}
        opacity={selected ? 0.5 : 0.72}
      />
      <circle
        r={node.radius}
        fill={active ? palette.fill : "#cbd5e1"}
        stroke={selected ? "#0f766e" : palette.stroke}
        strokeWidth={selected ? 2.8 : node.root ? 2.2 : 1.6}
      />
      {node.expanded ? <circle r="3" cx={node.radius - 3} cy={-node.radius + 3} fill="#ffffff" /> : null}
      <g pointerEvents="none" transform={`translate(0 ${node.radius + 20})`}>
        <rect
          x={-labelWidth / 2}
          y="-11"
          width={labelWidth}
          height="22"
          rx="7"
          fill="#ffffff"
          stroke="#dbe4ee"
          opacity={node.depth <= 1 || selected || node.root ? 0.96 : 0.82}
        />
        <text
          fill={active ? palette.text : "#64748b"}
          fontSize="11"
          fontWeight={node.root || selected ? "800" : "650"}
          textAnchor="middle"
          dominantBaseline="middle"
        >
          {label}
        </text>
      </g>
    </g>
  );
}

function buildCanvasLayout(
  graphData: ExplorerGraphData,
  width: number,
  height: number,
  pinnedPositions: Record<string, CanvasPoint>,
): CanvasLayout {
  const center = { x: width / 2, y: height / 2 };
  const depthGroups = new Map<number, ExplorerNode[]>();

  for (const node of graphData.nodes) {
    depthGroups.set(node.depth, [...depthGroups.get(node.depth) ?? [], node]);
  }

  const positionedNodes = graphData.nodes.map((node) => {
    const pinned = pinnedPositions[node.id];
    const radius = nodeRadius(node);
    if (pinned) {
      return {
        ...node,
        radius,
        x: clamp(pinned.x, 34, width - 34),
        y: clamp(pinned.y, 38, height - 38),
      };
    }
    if (node.root || node.depth === 0) {
      return { ...node, radius, x: center.x, y: center.y };
    }

    const siblings = depthGroups.get(node.depth) ?? [node];
    const index = Math.max(0, siblings.findIndex((item) => item.id === node.id));
    const ringRadius = Math.min(Math.min(width, height) * 0.43, 135 + node.depth * 92);
    const angle = Math.PI * 2 * index / Math.max(1, siblings.length) - Math.PI / 2;
    const stagger = siblings.length > 7 ? index % 2 * 26 : 0;

    return {
      ...node,
      radius,
      x: clamp(center.x + Math.cos(angle) * (ringRadius + stagger), 34, width - 34),
      y: clamp(center.y + Math.sin(angle) * (ringRadius + stagger), 38, height - 38),
    };
  });
  const nodeById = new Map(positionedNodes.map((node) => [node.id, node]));
  const links = graphData.links.flatMap((link) => {
    const sourceNode = nodeById.get(link.source);
    const targetNode = nodeById.get(link.target);
    return sourceNode && targetNode ? [{ ...link, sourceNode, targetNode }] : [];
  });

  return { height, links, nodes: positionedNodes, width };
}

function nodeRadius(node: ExplorerNode): number {
  if (node.root) return 18;
  if (node.type === "table" || node.type === "entity") return 14;
  if (node.type === "column" || node.type === "concept") return 11;
  return 10;
}

function curvedPath(source: PositionedNode, target: PositionedNode): string {
  const dx = target.x - source.x;
  const dy = target.y - source.y;
  const distance = Math.hypot(dx, dy) || 1;
  const curve = Math.min(76, Math.max(24, distance * 0.16));
  const control = {
    x: (source.x + target.x) / 2 + -dy / distance * curve,
    y: (source.y + target.y) / 2 + dx / distance * curve,
  };
  return `M ${source.x} ${source.y} Q ${control.x} ${control.y} ${target.x} ${target.y}`;
}

function curveLabelPoint(source: PositionedNode, target: PositionedNode): CanvasPoint {
  const dx = target.x - source.x;
  const dy = target.y - source.y;
  const distance = Math.hypot(dx, dy) || 1;
  const curve = Math.min(76, Math.max(24, distance * 0.16));
  return {
    x: (source.x + target.x) / 2 + -dy / distance * curve * 0.5,
    y: (source.y + target.y) / 2 + dx / distance * curve * 0.5,
  };
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function nodePalette(
  type: GraphNodeType | "other",
): { fill: string; ring: string; soft: string; stroke: string; text: string } {
  switch (type) {
    case "table":
      return { fill: "#0891b2", ring: "#67e8f9", soft: "#cffafe", stroke: "#0e7490", text: "#155e75" };
    case "column":
      return { fill: "#16a34a", ring: "#86efac", soft: "#dcfce7", stroke: "#15803d", text: "#166534" };
    case "concept":
      return { fill: "#d97706", ring: "#fcd34d", soft: "#fef3c7", stroke: "#b45309", text: "#92400e" };
    case "entity":
      return { fill: "#e11d48", ring: "#fda4af", soft: "#ffe4e6", stroke: "#be123c", text: "#9f1239" };
    default:
      return { fill: "#64748b", ring: "#cbd5e1", soft: "#f1f5f9", stroke: "#475569", text: "#334155" };
  }
}

function edgeColor(type: string, active: boolean): string {
  const alpha = active ? 0.84 : 0.42;
  if (type === "foreign_key" || type === "joinable") return `rgba(15, 118, 110, ${alpha})`;
  if (type === "contains") return `rgba(71, 85, 105, ${alpha})`;
  if (type === "semantic_synonym" || type === "has_concept") return `rgba(180, 83, 9, ${alpha})`;
  return `rgba(51, 65, 85, ${alpha})`;
}

function toggleSetValue(current: Set<string>, value: string): Set<string> {
  const next = new Set(current);
  if (next.has(value)) {
    next.delete(value);
  } else {
    next.add(value);
  }
  return next;
}

function graphNodeType(node: DatalinkNodeDto): GraphNodeType | "other" {
  return GRAPH_NODE_TYPES.includes(node.type as GraphNodeType) ? node.type as GraphNodeType : "other";
}

function edgeSource(edge: DatalinkEdgeDto): string {
  return String(edge.source_id ?? edge.source ?? "");
}

function edgeTarget(edge: DatalinkEdgeDto): string {
  return String(edge.target_id ?? edge.target ?? "");
}

function linkEndpointId(endpoint: ExplorerNode | string): string {
  return typeof endpoint === "string" ? endpoint : endpoint.id;
}

function nodeLabel(node: DatalinkNodeDto): string {
  return node.name || node.id;
}

function nodeSearchText(node: DatalinkNodeDto): string {
  return `${node.id} ${node.type} ${node.name ?? ""} ${JSON.stringify(node.properties ?? {})}`.toLowerCase();
}

function shortLabel(value: string, maxLength: number): string {
  return value.length > maxLength ? `${value.slice(0, maxLength - 1)}...` : value;
}

function formatPropertyValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value);
}

const explorerShellClass =
  "grid min-h-[680px] gap-4 xl:grid-cols-[260px_minmax(0,1fr)_320px]";
const sidePanelClass =
  "min-h-0 rounded-lg border border-border bg-white px-4 py-3 shadow-[var(--shadow-card)]";
const toolPanelClass =
  "space-y-3 rounded-lg border border-border bg-white px-4 py-3 shadow-[var(--shadow-card)]";
const canvasShellClass =
  "relative h-[620px] min-h-[620px] overflow-hidden rounded-lg border border-border bg-slate-50 " +
  "shadow-[inset_0_1px_0_rgba(255,255,255,0.8)]";
const backButtonClass =
  "flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-muted-light transition " +
  "hover:bg-surface-subtle hover:text-foreground";
const fieldClass =
  "h-9 rounded-lg border border-border bg-white px-3 text-sm text-slate-900 outline-none transition " +
  "focus:border-slate-400";
const primaryButtonClass =
  "h-9 rounded-lg bg-teal-700 px-4 text-sm font-semibold text-white transition hover:bg-teal-800 " +
  "disabled:cursor-not-allowed disabled:opacity-40";
const dangerButtonClass =
  "h-9 rounded-lg border border-rose-200 bg-rose-50 px-4 text-sm font-semibold text-rose-800 transition " +
  "hover:bg-rose-100 disabled:cursor-not-allowed disabled:opacity-40";
const smallActionClass =
  "h-8 rounded-lg border border-border bg-white px-3 text-xs font-semibold text-slate-700 transition " +
  "hover:bg-surface-subtle disabled:cursor-not-allowed disabled:opacity-40";
const resultBlockClass =
  "max-h-56 overflow-auto whitespace-pre-wrap rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 " +
  "text-[11px] leading-5 text-slate-700";
