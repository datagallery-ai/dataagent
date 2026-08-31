import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import {
  configApi,
  type TraceDagDto,
  type TraceDagEdgeDto,
  type TraceDagNodeDto,
} from "../../../../lib/config-api";
import type { DataArtifact } from "../../data-task-state";
import type { LiveRun } from "../../live-run-state";
import { buildTraceTimeline, traceTimelineStats } from "../../trace-timeline";
import {
  formatPayload,
  ToolFormattedParams,
  ToolFormattedResult,
  ToolRawFallback,
} from "../../tool-result-format";
import { overlayBackdropClass, overlayPanelClass, statusTone } from "../../ui-tokens";
import { ArtifactMarkdownPreview, isMarkdownFilePath } from "./ArtifactMarkdownPreview";
import { TraceDagCanvas, traceDagNodeKindLabel } from "./TraceDagCanvas";
import { TraceList } from "./TraceList";

type TraceOverlayProps = {
  artifacts: DataArtifact[];
  liveRun: LiveRun;
  isOpen: boolean;
  onClose: () => void;
  onCreateCheckpointBranch?: (checkpointId: string) => Promise<void> | void;
  onSelectArtifact: (artifactId: string) => void;
  onSelectEvent: (eventId: string) => void;
  onOpenFullscreen?: () => void;
  presentation?: "embedded" | "overlay";
  sessionId?: string;
};

const TRACE_REFRESH_INTERVAL_MS = 2_500;
const TRACE_FINALIZATION_TIMEOUT_MS = 180_000;

function runStatusLabel(status: LiveRun["runStatus"]): string {
  switch (status) {
    case "running":
      return "Running";
    case "completed":
      return "Completed";
    case "failed":
      return "Failed";
    case "canceled":
      return "Canceled";
    default:
      return "Idle";
  }
}

function isTerminalRunStatus(status: LiveRun["runStatus"]): boolean {
  return status === "completed" || status === "failed" || status === "canceled";
}

function traceSectionsFinalized(dag: TraceDagDto | null): boolean {
  if (!dag || dag.sections.length === 0 || dag.sections.some((section) => section.status !== "completed")) {
    return false;
  }
  const terminalNode = dag.nodes.find((node) => node.kind === "run-terminal");
  return terminalNode !== undefined
    && dag.sections.some((section) => section.nodeIds.includes(terminalNode.id));
}

function formatDuration(ms?: number): string {
  if (ms === undefined) return "—";
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

type TraceContextDetail = Extract<NonNullable<TraceDagNodeDto["detail"]>, { type: "context" }>;
type TraceToolDetail = Extract<NonNullable<TraceDagNodeDto["detail"]>, { type: "tool" }>;
type TraceArtifactDetail = Extract<NonNullable<TraceDagNodeDto["detail"]>, { type: "artifact" }>;
type TraceTerminalDetail = Extract<NonNullable<TraceDagNodeDto["detail"]>, { type: "terminal" }>;

export function TraceOverlay({
  artifacts,
  liveRun,
  isOpen,
  onClose,
  onCreateCheckpointBranch,
  onSelectArtifact,
  onSelectEvent,
  onOpenFullscreen,
  presentation = "overlay",
  sessionId,
}: TraceOverlayProps) {
  const [branchingCheckpointId, setBranchingCheckpointId] = useState<string | null>(null);
  const [dagError, setDagError] = useState<string | null>(null);
  const [isDagLoading, setIsDagLoading] = useState(false);
  const [refreshTick, setRefreshTick] = useState(0);
  const [collapsedSectionIds, setCollapsedSectionIds] = useState<Set<string>>(() => new Set());
  const [expandedEmbeddedSectionIds, setExpandedEmbeddedSectionIds] = useState<Set<string>>(() => new Set());
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [selectedSectionId, setSelectedSectionId] = useState<string | null>(null);
  const [traceDag, setTraceDag] = useState<TraceDagDto | null>(null);
  const hasLoadedTraceDag = useRef(false);
  const initializedSectionIds = useRef(new Set<string>());
  const loadedTraceSessionId = useRef<string | null>(null);
  const traceFinalizationDeadline = useRef<number | null>(null);
  const stats = useMemo(
    () => traceTimelineStats(liveRun, buildTraceTimeline(liveRun)),
    [liveRun],
  );
  const selectedNode = useMemo(
    () => traceDag?.nodes.find((node) => node.id === selectedNodeId) ?? null,
    [selectedNodeId, traceDag],
  );
  const selectedSection = useMemo(
    () => traceDag?.sections.find((section) => section.id === selectedSectionId) ?? null,
    [selectedSectionId, traceDag],
  );

  useEffect(() => {
    if (!isOpen || presentation !== "overlay") return;
    // Capture so Esc closes only the trace overlay, not an underlying console drawer.
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      event.stopImmediatePropagation();
      onClose();
    };
    window.addEventListener("keydown", onKeyDown, true);
    return () => window.removeEventListener("keydown", onKeyDown, true);
  }, [isOpen, onClose, presentation]);

  useEffect(() => {
    if (!isOpen || !sessionId) {
      traceFinalizationDeadline.current = null;
      return;
    }
    if (liveRun.runStatus === "running") {
      traceFinalizationDeadline.current = null;
      return;
    }
    if (isTerminalRunStatus(liveRun.runStatus)) {
      traceFinalizationDeadline.current = Date.now() + TRACE_FINALIZATION_TIMEOUT_MS;
      setRefreshTick((current) => current + 1);
    }
  }, [isOpen, liveRun.runStatus, sessionId]);

  useEffect(() => {
    if (!isOpen || !sessionId) return;
    const isRunning = liveRun.runStatus === "running";
    const deadline = traceFinalizationDeadline.current;
    const isFinalizing = deadline !== null && Date.now() < deadline && !traceSectionsFinalized(traceDag);
    if (!isRunning && !isFinalizing) return;
    const interval = window.setInterval(
      () => setRefreshTick((current) => current + 1),
      TRACE_REFRESH_INTERVAL_MS,
    );
    return () => window.clearInterval(interval);
  }, [isOpen, liveRun.runStatus, sessionId, traceDag]);

  useEffect(() => {
    if (!isOpen || !sessionId) {
      setTraceDag(null);
      setDagError(null);
      setSelectedNodeId(null);
      setSelectedSectionId(null);
      setCollapsedSectionIds(new Set());
      setExpandedEmbeddedSectionIds(new Set());
      hasLoadedTraceDag.current = false;
      initializedSectionIds.current.clear();
      loadedTraceSessionId.current = null;
      return;
    }
    if (loadedTraceSessionId.current !== sessionId) {
      loadedTraceSessionId.current = sessionId;
      hasLoadedTraceDag.current = false;
      initializedSectionIds.current.clear();
      setTraceDag(null);
      setSelectedNodeId(null);
      setSelectedSectionId(null);
      setCollapsedSectionIds(new Set());
      setExpandedEmbeddedSectionIds(new Set());
    }
    let canceled = false;
    if (!hasLoadedTraceDag.current) {
      setIsDagLoading(true);
    }
    setDagError(null);
    configApi.getSessionTraceDag(sessionId)
      .then((dag) => {
        if (canceled) return;
        hasLoadedTraceDag.current = true;
        setTraceDag(dag);
        setCollapsedSectionIds((current) => {
          const next = new Set(current);
          for (const section of dag.sections) {
            if (section.status === "completed" && !initializedSectionIds.current.has(section.id)) {
              next.add(section.id);
              initializedSectionIds.current.add(section.id);
            }
          }
          return next;
        });
        setSelectedNodeId((current) =>
          current && dag.nodes.some((node) => node.id === current)
            ? current
            : dag.nodes.find((node) => node.prominent)?.id ?? dag.nodes[0]?.id ?? null,
        );
      })
      .catch((error) => {
        if (canceled) return;
        setDagError(error instanceof Error ? error.message : "Failed to load trace graph");
      })
      .finally(() => {
        if (!canceled) setIsDagLoading(false);
      });
    return () => {
      canceled = true;
    };
  }, [isOpen, refreshTick, sessionId]);

  const handleCreateCheckpointBranch = async (checkpointId: string) => {
    if (!onCreateCheckpointBranch) return;
    setBranchingCheckpointId(checkpointId);
    try {
      await onCreateCheckpointBranch(checkpointId);
      onClose();
    } finally {
      setBranchingCheckpointId(null);
    }
  };

  if (!isOpen) return null;

  const workspace = (
    <TraceDagWorkspace
      branchingCheckpointId={branchingCheckpointId}
      collapsedSectionIds={collapsedSectionIds}
      dag={traceDag}
      dagError={dagError}
      isDagLoading={isDagLoading}
      mode={presentation === "embedded" ? "embedded" : "fullscreen"}
      node={selectedNode}
      onCreateCheckpointBranch={handleCreateCheckpointBranch}
      onSelectNode={(nodeId) => {
        setSelectedSectionId(null);
        setSelectedNodeId(nodeId);
      }}
      onSelectSection={(sectionId) => {
        setSelectedNodeId(null);
        setSelectedSectionId(sectionId);
      }}
      onToggleSection={(sectionId) => {
        setCollapsedSectionIds((current) => {
          const next = new Set(current);
          if (next.has(sectionId)) {
            next.delete(sectionId);
          } else {
            next.add(sectionId);
          }
          return next;
        });
      }}
      section={selectedSection}
    />
  );

  if (presentation === "embedded") {
    return (
      <section className="grid gap-3 rounded-lg border border-border bg-surface p-3">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h3 className="text-sm font-semibold text-foreground">Trace DAG</h3>
            <p className="mt-0.5 text-[11px] text-muted-light">Semantic task sections and checkpoint lineage.</p>
          </div>
          {onOpenFullscreen ? (
            <button
              type="button"
              onClick={onOpenFullscreen}
              className={[
                "shrink-0 rounded border border-border px-2.5 py-1.5 text-[11px] font-semibold text-muted",
                "hover:bg-surface-subtle",
              ].join(" ")}
            >
              Open full screen
            </button>
          ) : null}
        </div>
        <TraceSectionProgressList
          dag={traceDag}
          error={dagError}
          expandedSectionIds={expandedEmbeddedSectionIds}
          isLoading={isDagLoading}
          onToggleSection={(sectionId) => {
            setExpandedEmbeddedSectionIds((current) => {
              const next = new Set(current);
              if (next.has(sectionId)) {
                next.delete(sectionId);
              } else {
                next.add(sectionId);
              }
              return next;
            });
          }}
        />
      </section>
    );
  }

  return (
    <div className={`${overlayBackdropClass} p-4`}>
      <div className={`mx-auto h-full max-w-7xl ${overlayPanelClass}`}>
        <header className="border-b border-border px-5 py-4">
          <div className="flex items-start justify-between gap-3">
            <div>
              <h2 className="text-base font-semibold text-foreground">
                Full Task Trace
              </h2>
              <p className="mt-1 text-xs text-muted-light">
                Review data operations, SQL, raw results, and artifact lineage by time.
              </p>
            </div>
            <button
              type="button"
              onClick={onClose}
              className={[
                "shrink-0 rounded-lg border border-border px-3 py-1.5 text-xs font-semibold text-muted",
                "transition hover:bg-surface-subtle",
              ].join(" ")}
            >
              Close
            </button>
          </div>

          {stats.runStatus !== "idle" ? (
            <div className="mt-3 flex flex-wrap items-center gap-2 text-xs">
              <span
                className={`rounded-full px-2.5 py-1 font-semibold ${
                  stats.runStatus === "completed"
                    ? statusTone("success")
                    : stats.runStatus === "failed"
                      ? statusTone("error")
                      : stats.runStatus === "running"
                        ? statusTone("info")
                        : statusTone("muted")
                }`}
              >
                {runStatusLabel(stats.runStatus)}
              </span>
              <span className="text-muted-light">
                Run duration {formatDuration(stats.durationMs)}
              </span>
              {liveRun.errorMessage ? (
                <span className="text-step-error">{liveRun.errorMessage}</span>
              ) : null}
            </div>
          ) : null}
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto p-5">
          {workspace}

          <div className="mt-5">
            <TraceList
              artifacts={artifacts}
              liveRun={liveRun}
              onSelectArtifact={onSelectArtifact}
              onSelectEvent={onSelectEvent}
            />
          </div>
        </div>

        {stats.entryCount > 0 ? (
          <footer className="border-t border-border px-5 py-3 text-[11px] text-muted-light">
            {stats.entryCount} records · {stats.toolCount} data operations ·{" "}
            {stats.artifactCount} artifacts
          </footer>
        ) : null}
      </div>
    </div>
  );
}

export function EmbeddedTraceDag(
  props: Omit<TraceOverlayProps, "isOpen" | "onClose" | "presentation">
) {
  return <TraceOverlay {...props} isOpen presentation="embedded" onClose={() => undefined} />;
}

function TraceSectionProgressList({
  dag,
  error,
  expandedSectionIds,
  isLoading,
  onToggleSection,
}: {
  dag: TraceDagDto | null;
  error: string | null;
  expandedSectionIds: ReadonlySet<string>;
  isLoading: boolean;
  onToggleSection: (sectionId: string) => void;
}) {
  if (isLoading) {
    return (
      <TraceProgressState
        label="Collecting task phases"
        detail="The first phase appears after enough context steps."
      />
    );
  }
  if (error) {
    return <TraceProgressState label="Trace phases unavailable" detail={error} tone="error" />;
  }
  if (!dag || dag.nodes.length === 0) {
    return <TraceProgressState label="No task progress yet" detail="Start a task to build its trace phases." />;
  }
  if (dag.sections.length === 0) {
    return <TraceProgressState
      label="Building the first phase"
      detail="The trace will be grouped after several context steps or when the task finishes."
    />;
  }
  const nodesById = new Map(dag.nodes.map((node) => [node.id, node]));
  return (
    <ol className="grid gap-1.5">
      {dag.sections.map((section, index) => {
        const expanded = expandedSectionIds.has(section.id);
        const phaseNodes = section.nodeIds.flatMap((nodeId) => {
          const node = nodesById.get(nodeId);
          return node ? [node] : [];
        });
        return (
          <li key={section.id} className="relative pl-7">
            {index < dag.sections.length - 1 ? (
              <span className="absolute left-[9px] top-5 h-[calc(100%+3px)] w-px bg-border" />
            ) : null}
            <span className={[
              "absolute left-0 top-2.5 flex h-5 w-5 items-center justify-center rounded-full border-2 bg-surface",
              section.status === "completed" ? "border-step-success" : "border-primary",
            ].join(" ")}>
              <span className={[
                "h-1.5 w-1.5 rounded-full",
                section.status === "completed" ? "bg-step-success" : "bg-primary",
              ].join(" ")} />
            </span>
            <div className="rounded border border-border bg-surface-subtle px-3 py-2.5">
              <div className="flex items-start justify-between gap-3">
                <button type="button" onClick={() => onToggleSection(section.id)} className="min-w-0 text-left">
                  <div className="truncate text-xs font-semibold text-foreground">{section.title}</div>
                  <p className="mt-1 text-[11px] leading-4 text-muted">{section.summary}</p>
                </button>
                <button
                  type="button"
                  aria-expanded={expanded}
                  onClick={() => onToggleSection(section.id)}
                  className={[
                    "shrink-0 rounded border border-border px-1.5 py-1 text-[10px] font-semibold text-muted",
                    "hover:bg-surface",
                  ].join(" ")}
                >
                  {expanded ? "Hide steps" : `${phaseNodes.length} steps`}
                </button>
              </div>
              {expanded ? (
                <ol className="mt-2 grid gap-1 border-t border-border pt-2">
                  {phaseNodes.map((node) => (
                    <li key={node.id} className="flex min-w-0 items-center gap-2 text-[11px] text-muted">
                      <span className={[
                        "h-1.5 w-1.5 shrink-0 rounded-full",
                        node.kind === "tool" ? "bg-step-success" : "bg-muted-light",
                      ].join(" ")} />
                      <span className="shrink-0 text-muted-light">{traceDagNodeKindLabel(node)}</span>
                      <span className="truncate text-foreground">{node.label}</span>
                    </li>
                  ))}
                </ol>
              ) : null}
            </div>
          </li>
        );
      })}
    </ol>
  );
}

function TraceProgressState({
  detail,
  label,
  tone = "muted",
}: {
  detail: string;
  label: string;
  tone?: "error" | "muted";
}) {
  return (
    <div className={[
      "rounded border px-3 py-3 text-xs",
      tone === "error"
        ? "border-step-error/30 bg-step-error/10 text-step-error"
        : "border-border bg-surface-subtle text-muted",
    ].join(" ")}>
      <div className="font-semibold">{label}</div>
      <div className="mt-1 text-[11px] leading-4 opacity-80">{detail}</div>
    </div>
  );
}

function TraceDagWorkspace({
  branchingCheckpointId,
  collapsedSectionIds,
  dag,
  dagError,
  isDagLoading,
  mode,
  node,
  onCreateCheckpointBranch,
  onSelectNode,
  onSelectSection,
  onToggleSection,
  section,
}: {
  branchingCheckpointId: string | null;
  collapsedSectionIds: ReadonlySet<string>;
  dag: TraceDagDto | null;
  dagError: string | null;
  isDagLoading: boolean;
  mode: "embedded" | "fullscreen";
  node: TraceDagNodeDto | null;
  onCreateCheckpointBranch: (checkpointId: string) => Promise<void>;
  onSelectNode: (nodeId: string) => void;
  onSelectSection: (sectionId: string) => void;
  onToggleSection: (sectionId: string) => void;
  section: NonNullable<TraceDagDto["sections"]>[number] | null;
}) {
  const layoutClass = mode === "embedded"
    ? "grid gap-3"
    : "grid gap-4 xl:grid-cols-[minmax(0,1.55fr)_minmax(340px,0.75fr)]";
  return (
    <div className={layoutClass}>
      <TraceDagCanvas
        collapsedSectionIds={collapsedSectionIds}
        dag={dag}
        error={dagError}
        isLoading={isDagLoading}
        mode={mode}
        onSelectNode={onSelectNode}
        onSelectSection={onSelectSection}
        onToggleSection={onToggleSection}
        sections={dag?.sections}
        selectedNodeId={node?.id ?? null}
        selectedSectionId={section?.id ?? null}
      />
      <TraceDagNodeDetails
        branchingCheckpointId={branchingCheckpointId}
        dag={dag}
        node={node}
        onCreateCheckpointBranch={onCreateCheckpointBranch}
        section={section}
      />
    </div>
  );
}

function TraceDagNodeDetails({
  branchingCheckpointId,
  dag,
  node,
  onCreateCheckpointBranch,
  section,
}: {
  branchingCheckpointId: string | null;
  dag: TraceDagDto | null;
  node: TraceDagNodeDto | null;
  onCreateCheckpointBranch: (checkpointId: string) => Promise<void>;
  section: NonNullable<TraceDagDto["sections"]>[number] | null;
}) {
  if (section) {
    return (
      <aside className="min-w-0 rounded-lg border border-border bg-surface p-4">
        <div className="text-[11px] font-semibold uppercase tracking-wide text-muted-light">Task section</div>
        <h3 className="mt-1 text-base font-semibold text-foreground">{section.title}</h3>
        <p className="mt-3 text-xs leading-5 text-muted">{section.summary}</p>
        <div className="mt-4 grid gap-2 border-t border-border pt-4 text-xs">
          <TraceField label="Steps" value={`${section.startEventSeq}-${section.endEventSeq}`} />
          <TraceField label="Nodes" value={String(section.nodeIds.length)} />
          <TraceField label="State" value={section.status} />
        </div>
      </aside>
    );
  }
  if (!node) {
    return (
      <aside className="rounded-xl border border-border bg-surface p-4 text-sm text-muted">
        Select a trace node
      </aside>
    );
  }
  const incoming = dag?.edges.filter((edge) => edge.target === node.id) ?? [];
  const outgoing = dag?.edges.filter((edge) => edge.source === node.id) ?? [];
  const canBranch = Boolean(node.checkpointId && node.rollbackable);
  return (
    <aside className="min-w-0 rounded-xl border border-border bg-surface p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-[11px] font-semibold uppercase tracking-wide text-muted-light">
            {traceDagNodeKindLabel(node)}
          </div>
          <h3 className="mt-1 truncate text-base font-semibold text-foreground">{node.label}</h3>
        </div>
        {node.status ? (
          <span className="shrink-0 rounded bg-surface-subtle px-2 py-1 text-[11px] font-semibold text-muted">
            {node.status}
          </span>
        ) : null}
      </div>

      <TraceNodeReadableDetail node={node} />

      {node.checkpointId ? (
        <div className="mt-4 rounded-lg border border-border bg-surface-subtle p-3">
          <div className="text-[11px] font-semibold text-muted-light">Checkpoint</div>
          <div className="mt-1 break-all font-mono text-xs text-foreground">{node.checkpointId}</div>
          <div className="mt-2 flex flex-wrap gap-2">
            <button
              type="button"
              onClick={() => void navigator.clipboard?.writeText(node.checkpointId!)}
              className="rounded border border-border px-2.5 py-1 text-[11px] font-semibold text-muted hover:bg-surface"
            >
              Copy id
            </button>
            <button
              type="button"
              disabled={!canBranch || branchingCheckpointId === node.checkpointId}
              onClick={() => node.checkpointId && onCreateCheckpointBranch(node.checkpointId)}
              className={[
                "rounded px-2.5 py-1 text-[11px] font-semibold transition",
                canBranch
                  ? "bg-primary text-white hover:bg-primary-light"
                  : "cursor-not-allowed bg-surface text-muted-light",
              ].join(" ")}
            >
              {branchingCheckpointId === node.checkpointId ? "Creating..." : "Continue from here"}
            </button>
          </div>
        </div>
      ) : null}

      <div className="mt-4 grid gap-2 border-t border-border pt-4 text-xs">
        <TraceField label="Run" value={node.runId} />
        <TraceField label="Event" value={node.eventSeq !== undefined ? String(node.eventSeq) : undefined} />
        <TraceField label="Tool" value={node.toolCallId} />
      </div>

      <TraceEdgeList title="Incoming" edges={incoming} />
      <TraceEdgeList title="Outgoing" edges={outgoing} />
    </aside>
  );
}

function TraceNodeReadableDetail({ node }: { node: TraceDagNodeDto }) {
  const detail = node.detail;
  if (detail?.type === "context") {
    return <TraceContextReadableDetail detail={detail} summary={node.summary} />;
  }
  if (detail?.type === "tool") {
    return <TraceToolReadableDetail detail={detail} node={node} />;
  }
  if (detail?.type === "artifact") {
    return <TraceArtifactReadableDetail detail={detail} node={node} />;
  }
  if (detail?.type === "terminal") {
    return <TraceTerminalReadableDetail detail={detail} summary={node.summary} />;
  }
  return node.summary ? <p className="mt-3 text-xs leading-5 text-muted">{node.summary}</p> : null;
}

function TraceContextReadableDetail({
  detail,
  summary,
}: {
  detail: TraceContextDetail;
  summary?: string;
}) {
  const metrics = [
    detail.model ? { label: "Model", value: detail.model } : null,
    detail.modelProfileId ? { label: "Profile", value: detail.modelProfileId } : null,
    detail.stepNumber !== undefined ? { label: "Step", value: String(detail.stepNumber) } : null,
    detail.promptTokens !== undefined ? { label: "Prompt", value: formatCount(detail.promptTokens) } : null,
    detail.remainingTokens !== undefined ? { label: "Remaining", value: formatCount(detail.remainingTokens) } : null,
    detail.packageRevision !== undefined ? { label: "Package rev", value: String(detail.packageRevision) } : null,
  ].filter(Boolean) as Array<{ label: string; value: string }>;

  return (
    <div className="mt-4 grid gap-4">
      {summary ? <p className="text-xs leading-5 text-muted">{summary}</p> : null}
      <TraceMetricGrid metrics={metrics} />
      <TraceTextPreview
        emptyText="No reasoning chunks were recorded for this step."
        text={detail.reasoning}
        title="Model reasoning"
      />
      <TraceTextPreview title="Assistant output" markdown text={detail.assistantOutput} />
      <TraceChipSection title="Selected context groups" values={detail.selectedGroupIds} />
      <TraceContextPackageDetail detail={detail} />
    </div>
  );
}

function TraceToolReadableDetail({
  detail,
  node,
}: {
  detail: TraceToolDetail;
  node: TraceDagNodeDto;
}) {
  const labelToolName = node.label.replace(/^Tool:\s*/u, "").trim();
  const toolName = detail.toolName ?? (labelToolName || "tool");
  const parameters = detail.arguments ?? detail.argumentsText;
  const result = detail.result ?? detail.resultText;

  return (
    <div className="mt-4 grid gap-4">
      {node.summary ? <TraceTextPreview title="Latest event" text={node.summary} /> : null}
      <TraceDetailSection title="Tool parameters">
        {parameters !== undefined ? (
          <ToolFormattedParams parameters={parameters} />
        ) : (
          <TraceEmptyLine text="No parameters were recorded for this tool call." />
        )}
      </TraceDetailSection>
      <TraceDetailSection title="Tool result">
        {result !== undefined ? (
          <ToolFormattedResult toolName={toolName} result={result} variant="console" />
        ) : (
          <TraceEmptyLine text="No result has been recorded yet." />
        )}
      </TraceDetailSection>
    </div>
  );
}

function TraceContextPackageDetail({ detail }: { detail: TraceContextDetail }) {
  const sourceMetrics = [
    detail.selectedSources ? { label: "Selected sources", value: String(detail.selectedSources.length) } : null,
    detail.omittedSources ? { label: "Omitted sources", value: String(detail.omittedSources.length) } : null,
    detail.omittedGroupIds ? { label: "Omitted groups", value: String(detail.omittedGroupIds.length) } : null,
  ].filter(Boolean) as Array<{ label: string; value: string }>;

  return (
    <TraceDetailSection title="Context package">
      <div className="grid gap-2 text-xs">
        <TraceField label="Package" value={detail.packageId} />
        <TraceField label="Plan" value={detail.planId} />
      </div>
      <TraceMetricGrid metrics={sourceMetrics} />
      <TraceTokenReport value={detail.tokenReport} />
      <TraceDecisionList decisions={detail.decisions} />
    </TraceDetailSection>
  );
}

function TraceMetricGrid({ metrics }: { metrics: Array<{ label: string; value: string }> }) {
  if (metrics.length === 0) return null;
  return (
    <div className="grid grid-cols-2 gap-2">
      {metrics.map((metric) => (
        <div key={metric.label} className="rounded-lg border border-border bg-surface-subtle p-2">
          <div className="text-[10px] font-semibold uppercase tracking-wide text-muted-light">
            {metric.label}
          </div>
          <div className="mt-1 truncate text-xs font-semibold text-foreground">{metric.value}</div>
        </div>
      ))}
    </div>
  );
}

function TraceDetailSection({
  children,
  title,
}: {
  children: ReactNode;
  title: string;
}) {
  return (
    <section className="grid gap-2">
      <div className="text-[11px] font-semibold uppercase tracking-wide text-muted-light">{title}</div>
      {children}
    </section>
  );
}

function TraceTextPreview({
  emptyText,
  markdown = false,
  text,
  title,
}: {
  emptyText?: string;
  markdown?: boolean;
  text?: string;
  title: string;
}) {
  const trimmed = text?.trim();
  if (!trimmed) {
    return emptyText ? (
      <TraceDetailSection title={title}>
        <TraceEmptyLine text={emptyText} />
      </TraceDetailSection>
    ) : null;
  }
  return (
    <TraceDetailSection title={title}>
      {markdown ? (
        <ArtifactMarkdownPreview content={trimmed} />
      ) : (
        <pre className="max-h-72 overflow-auto whitespace-pre-wrap rounded-lg bg-surface-subtle p-3 text-xs">
          {trimmed}
        </pre>
      )}
    </TraceDetailSection>
  );
}

function TraceChipSection({
  title,
  values,
}: {
  title: string;
  values?: string[];
}) {
  if (!values || values.length === 0) return null;
  return (
    <TraceDetailSection title={title}>
      <div className="flex flex-wrap gap-1.5">
        {values.slice(0, 8).map((value) => (
          <span
            key={value}
            className="max-w-full truncate rounded border border-border bg-surface-subtle px-2 py-1 text-[11px]"
          >
            {value}
          </span>
        ))}
        {values.length > 8 ? (
          <span className="rounded bg-surface-subtle px-2 py-1 text-[11px] text-muted-light">
            +{values.length - 8}
          </span>
        ) : null}
      </div>
    </TraceDetailSection>
  );
}

function TraceTokenReport({ value }: { value: unknown }) {
  const record = recordValue(value);
  if (!record) return null;
  const entries = Object.entries(record)
    .flatMap(([key, entryValue]) => {
      if (typeof entryValue === "number") {
        return [{ label: formatKeyLabel(key), value: formatCount(entryValue) }];
      }
      if (typeof entryValue === "string") {
        return [{ label: formatKeyLabel(key), value: entryValue }];
      }
      return [];
    })
    .slice(0, 6);
  if (entries.length === 0) return null;
  return <TraceMetricGrid metrics={entries} />;
}

function TraceDecisionList({ decisions }: { decisions?: unknown[] }) {
  if (!decisions || decisions.length === 0) return null;
  return (
    <div className="grid gap-2">
      <div className="text-[11px] font-semibold text-muted-light">Selection decisions</div>
      {decisions.slice(0, 5).map((decision, index) => {
        const record = recordValue(decision);
        const label = stringValue(record?.decision) ?? stringValue(record?.kind) ?? `Decision ${index + 1}`;
        const reason = decisionReasonText(record) ?? truncateText(formatPayload(decision), 180);
        return (
          <div key={index} className="rounded-lg border border-border bg-surface-subtle p-2 text-xs">
            <div className="font-semibold text-foreground">{label}</div>
            {reason ? <div className="mt-1 leading-5 text-muted">{reason}</div> : null}
          </div>
        );
      })}
    </div>
  );
}

function TracePreviewValue({
  artifactName,
  value,
}: {
  artifactName: string;
  value: unknown;
}) {
  const record = recordValue(value);
  const content = stringValue(record?.content);
  const path = stringValue(record?.path) ?? artifactName;

  if (record && isPreviewTable(record)) {
    return <ToolFormattedResult toolName="preview_table" result={record} variant="console" />;
  }
  if (content) {
    if (isMarkdownFilePath(path)) {
      return <ArtifactMarkdownPreview content={content} />;
    }
    return (
      <pre className="max-h-80 overflow-auto whitespace-pre-wrap rounded-lg bg-surface-subtle p-3 text-xs">
        {content}
      </pre>
    );
  }
  if (typeof value === "string") {
    if (isMarkdownFilePath(artifactName)) {
      return <ArtifactMarkdownPreview content={value} />;
    }
    return (
      <pre className="max-h-80 overflow-auto whitespace-pre-wrap rounded-lg bg-surface-subtle p-3 text-xs">
        {value}
      </pre>
    );
  }
  return <ToolRawFallback result={value} variant="console" collapsible={false} />;
}

function TraceEmptyLine({ text }: { text: string }) {
  return (
    <p className="rounded-lg border border-dashed border-border bg-surface-subtle px-2.5 py-2 text-xs text-muted-light">
      {text}
    </p>
  );
}

function isPreviewTable(value: Record<string, unknown>): boolean {
  return Array.isArray(value.columns) && Array.isArray(value.rows);
}

function recordValue(value: unknown): Record<string, unknown> | undefined {
  return typeof value === "object" && value !== null ? value as Record<string, unknown> : undefined;
}

function stringValue(value: unknown): string | undefined {
  return typeof value === "string" && value.trim().length > 0 ? value : undefined;
}

function decisionReasonText(record: Record<string, unknown> | undefined): string | undefined {
  if (!record) return undefined;
  const direct = stringValue(record.reason) ?? stringValue(record.message) ?? stringValue(record.summary);
  if (direct) return direct;
  if (Array.isArray(record.reasons)) {
    const reasons = record.reasons.filter(
      (item): item is string => typeof item === "string" && item.trim().length > 0,
    );
    return reasons.length > 0 ? reasons.join(", ") : undefined;
  }
  return undefined;
}

function formatCount(value: number): string {
  return new Intl.NumberFormat("en-US").format(value);
}

function formatKeyLabel(value: string): string {
  return value
    .replace(/[_-]+/gu, " ")
    .replace(/\b\w/gu, (char) => char.toUpperCase());
}

function truncateText(value: string, maxLength: number): string {
  return value.length > maxLength ? `${value.slice(0, maxLength - 1)}...` : value;
}

function TraceArtifactReadableDetail({
  detail,
  node,
}: {
  detail: TraceArtifactDetail;
  node: TraceDagNodeDto;
}) {
  return (
    <div className="mt-4 grid gap-4">
      <TraceMetricGrid
        metrics={[
          detail.artifactType ? { label: "Type", value: detail.artifactType } : null,
          detail.mimeType ? { label: "MIME", value: detail.mimeType } : null,
        ].filter(Boolean) as Array<{ label: string; value: string }>}
      />
      <TraceDetailSection title="Output preview">
        {detail.preview !== undefined ? (
          <TracePreviewValue artifactName={detail.name ?? node.label} value={detail.preview} />
        ) : (
          <TraceEmptyLine text="This output does not have a stored preview." />
        )}
      </TraceDetailSection>
    </div>
  );
}

function TraceTerminalReadableDetail({
  detail,
  summary,
}: {
  detail: TraceTerminalDetail;
  summary?: string;
}) {
  const message = detail.error ?? detail.message ?? summary;
  return (
    <div className="mt-4">
      <TraceTextPreview title={detail.error ? "Run error" : "Run result"} text={message ?? "Run completed."} />
    </div>
  );
}

function TraceField({ label, value }: { label: string; value?: string }) {
  if (!value) return null;
  return (
    <div className="flex min-w-0 items-center gap-2">
      <span className="w-14 shrink-0 text-muted-light">{label}</span>
      <span className="truncate font-mono text-muted">{value}</span>
    </div>
  );
}

function TraceEdgeList({ title, edges }: { title: string; edges: TraceDagEdgeDto[] }) {
  if (edges.length === 0) return null;
  return (
    <div className="mt-4">
      <div className="text-[11px] font-semibold text-muted-light">{title}</div>
      <div className="mt-2 flex flex-wrap gap-1">
        {edges.map((edge) => (
          <span key={edge.id} className="rounded bg-surface-subtle px-1.5 py-0.5 text-[10px] text-muted">
            {edge.label ?? edge.kind}
          </span>
        ))}
      </div>
    </div>
  );
}
