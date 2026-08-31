import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useT } from "../../../../i18n/locale-context";
import { translateRunStatus } from "../../../../i18n/status-labels";
import {
  Bar,
  BarChart,
  Cell,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type {
  ArtifactDetail,
  DataArtifact,
  GenericStepPayload,
  TimelineEvent,
} from "../../data-task-state";
import type { JobDto } from "../../../../lib/config-api";
import { configApi } from "../../../../lib/config-api";
import { dataStepKindForTool, dataStepLabel, hasCapability, toolDisplayTitle } from "../../data-task-state";
import { artifactExportClient } from "../../artifact-export-client";
import {
  artifactDetailFromPreview,
  artifactDetailNeedsPreviewFetch,
  deriveLiveSessionView,
  deriveRunUsage,
  formatSandboxOutputText,
  formatWorkspaceMetadataSummary,
  mergeArtifactDetail,
  parseCsvTextPreview,
  resolveProducedArtifacts,
  resolveSandboxOutputsForToolCall,
  resolveTokenUsageForEvent,
  resolveTokenUsageForToolCallIds,
  resolveToolCallForEvent,
  resolveTraceToolStatus,
  resolveWorkspaceMetadataForToolCall,
  type LiveRun,
  type LiveToolCallRecord,
  type SessionUsageStats,
} from "../../live-run-state";
import {
  deriveProcessGroupUsage,
  type ProcessToolGroup,
} from "../../process-tool-groups";
import type { TaskSelection } from "../../data-tasks-app";
import { overviewSectionPlan } from "../../task-console-layout";
import {
  filterTableRows,
  sortTableRows,
  tableToCsv,
  type TableSortState,
} from "../../table-rows";
import { RunConfigurationPanel } from "./RunConfigurationPanel";
import { TraceList } from "./TraceList";
import { EmbeddedTraceDag } from "./TraceOverlay";
import { ArtifactMarkdownPreview } from "./ArtifactMarkdownPreview";
import { ActionMenu, type ActionMenuItem } from "./ActionMenu";
import { IconDots, IconOpen } from "./console-icons";
import { canFormatExport, useArtifactExportActions } from "../../artifact-actions";
import {
  consoleCodeBlockBaseClass,
  consoleCodeInnerClass,
  consoleScrollXShellClass,
  consoleStepsListClass,
  consoleTableShellClass,
} from "./console-scroll-styles";
import { ToolFormattedResult, ToolFailureResult } from "../../tool-result-format";
import { parseSqlToolResult, sqlFromToolPayload } from "../../tool-result-normalize";
import { toolResultLooksLikeError } from "../../tool-call-display";
import {
  btnGhostClass,
  btnPrimaryClass,
  btnSecondaryClass,
  emptyStateClass,
  artifactToneForType,
  kpiValueClass,
  metricLabelClass,
  metricValueClass,
  panelShellClass,
  panelTitleClass,
  sectionLabelClass,
  stepKindTone,
} from "../../ui-tokens";

type ActiveSelection = Exclude<TaskSelection, null>;

type ConsoleTab = "overview" | "trace" | "outputs" | "detail";

export type TaskConsoleProps = {
  artifacts: DataArtifact[];
  liveRun: LiveRun;
  toolGroups: ProcessToolGroup[];
  sessionUsage: SessionUsageStats;
  selection: TaskSelection;
  visibleEvents: TimelineEvent[];
  currentQuestion?: string;
  /** When set (e.g. from TraceOverlay), jump to Outputs and expand this artifact. */
  artifactFocusId?: string | null;
  onArtifactFocusHandled?: () => void;
  onClearSelection: () => void;
  onClose?: () => void;
  onMentionArtifact?: (artifact: DataArtifact) => void;
  /** Opens an artifact in its own full-panel page (a peer of the console). */
  onOpenArtifactPage?: (artifactId: string) => void;
  /** Opens an artifact in a full-page preview overlay (modal). */
  onPreviewArtifact?: (artifactId: string) => void;
  onOpenTrace: () => void;
  onPromoteArtifact?: (artifact: DataArtifact) => Promise<void> | void;
  onArtifactExportJob?: (job: JobDto) => void;
  onSelectEvent: (eventId: string) => void;
  onSelectToolGroup: (groupId: string) => void;
  promotedArtifactIds?: ReadonlySet<string>;
  sessionId?: string;
  onCreateCheckpointBranch?: (checkpointId: string) => Promise<void> | void;
};

function runStatusLabel(status: LiveRun["runStatus"], t: ReturnType<typeof useT>): string {
  return translateRunStatus(status, t);
}

function toolStatusLabel(
  status: LiveToolCallRecord["status"],
  t: ReturnType<typeof useT>,
): string {
  switch (status) {
    case "running":
      return t("common.running");
    case "success":
      return t("common.completed");
    case "failed":
      return t("common.failed");
  }
}

function formatDuration(ms?: number): string {
  if (ms === undefined) return "—";
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function formatCount(value: number): string {
  return value.toLocaleString();
}

/** Single-step elapsed time; running/unfinished steps show a soft placeholder. */
function stepDurationLabel(
  call: LiveToolCallRecord | undefined,
  t: ReturnType<typeof useT>,
): string {
  if (!call) return "—";
  if (call.startedAtMs !== undefined && call.finishedAtMs !== undefined) {
    return formatDuration(Math.max(0, call.finishedAtMs - call.startedAtMs));
  }
  if (call.status === "running") return t("common.running");
  return "—";
}

export function TaskConsole({
  artifacts,
  liveRun,
  toolGroups,
  sessionUsage,
  selection,
  visibleEvents,
  currentQuestion,
  onClearSelection,
  onClose,
  onMentionArtifact,
  onOpenArtifactPage,
  onPreviewArtifact,
  onOpenTrace,
  onPromoteArtifact,
  onArtifactExportJob,
  onSelectEvent,
  onSelectToolGroup,
  promotedArtifactIds,
  sessionId,
  onCreateCheckpointBranch,
}: TaskConsoleProps) {
  const t = useT();
  const [activeTab, setActiveTab] = useState<ConsoleTab>("overview");
  const runUsage = useMemo(() => deriveRunUsage(liveRun), [liveRun]);
  const sessionView = useMemo(
    () => deriveLiveSessionView(sessionUsage, liveRun),
    [liveRun, sessionUsage],
  );
  const overviewSections = overviewSectionPlan({
    hasToolDistribution: Object.keys(runUsage.toolCalls.byTool).length > 0,
  });

  // Only step/action selection drives the detail tab; artifacts expand in-place on Outputs.
  useEffect(() => {
    if (selection?.type === "action" || selection?.type === "toolGroup") {
      setActiveTab("detail");
    }
  }, [selection]);

  const openArtifactOrOutputs = (artifactId: string) => {
    onClearSelection();
    if (onOpenArtifactPage) {
      onOpenArtifactPage(artifactId);
      return;
    }
    setActiveTab("outputs");
  };

  const handleTabClick = (tab: ConsoleTab) => {
    if (tab !== "detail") onClearSelection();
    setActiveTab(tab);
  };

  const selectedEvent =
    selection?.type === "action"
      ? visibleEvents.find((event) => event.id === selection.id) ?? null
      : null;
  const selectedGroup =
    selection?.type === "toolGroup"
      ? toolGroups.find((group) => group.id === selection.id) ?? null
      : null;

  return (
    <section
      data-guide-id="run-console"
      className="flex h-full min-h-0 w-full flex-1 flex-col overflow-hidden border-l border-border bg-surface"
    >
      <header className="flex h-16 items-center justify-between gap-3 border-b border-border bg-surface px-4">
        <div className="min-w-0">
          <h2 className={panelTitleClass}>{t("console.title")}</h2>
          <div className="mt-1 flex min-w-0 items-center gap-2 text-[11px] text-muted-light">
            <span className="inline-flex items-center gap-1.5">
              <span
                className={[
                  "h-1.5 w-1.5 rounded-full",
                  liveRun.runStatus === "running"
                    ? "bg-primary-light"
                    : liveRun.runStatus === "failed"
                      ? "bg-step-error"
                      : liveRun.runStatus === "completed"
                        ? "bg-step-success"
                        : "bg-muted-light",
                ].join(" ")}
              />
              {runStatusLabel(liveRun.runStatus, t)}
            </span>
            <span className="text-border">/</span>
            <span className="tabular">
              {liveRun.runStatus !== "idle" ? formatDuration(runUsage.durationMs) : t("common.notStarted")}
            </span>
          </div>
        </div>
        {onClose ? (
          <button
            type="button"
            onClick={onClose}
            aria-label={t("console.close")}
            title={t("console.close")}
            className={`flex h-8 w-8 shrink-0 items-center justify-center ${btnGhostClass}`}
          >
            <span aria-hidden="true" className="text-lg leading-none">
              ×
            </span>
          </button>
        ) : null}
      </header>

      <nav className="flex shrink-0 items-center gap-0.5 overflow-x-auto border-b border-border px-2 py-2 [-ms-overflow-style:none] [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
        <TabButton active={activeTab === "overview"} onClick={() => handleTabClick("overview")}>
          {t("console.tabs.overview")}
        </TabButton>
        <TabButton
          active={activeTab === "trace"}
          badge={liveRun.toolCalls.length}
          onClick={() => handleTabClick("trace")}
        >
          {t("console.tabs.trace")}
        </TabButton>
        <TabButton
          active={activeTab === "outputs"}
          badge={artifacts.length}
          onClick={() => handleTabClick("outputs")}
        >
          {t("console.tabs.outputs")}
        </TabButton>
        <TabButton
          active={activeTab === "detail"}
          badge={selection?.type === "action" || selection?.type === "toolGroup" ? 1 : undefined}
          onClick={() => handleTabClick("detail")}
        >
          {t("console.tabs.details")}
        </TabButton>
      </nav>

      <div className="min-h-0 min-w-0 flex-1 overflow-y-auto overflow-x-hidden p-4">
        {activeTab === "overview" ? (
          <div className="grid gap-4">
            {overviewSections.map((section) => {
              if (section.id === "conclusion") {
                return (
                  <ConclusionZone
                    key={section.id}
                    currentQuestion={currentQuestion}
                    liveRun={liveRun}
                    toolGroups={toolGroups}
                    sessionUsage={sessionUsage}
                  />
                );
              }
              if (section.id === "progress") {
                return (
                  <DynamicStepsList
                    key={section.id}
                    liveRun={liveRun}
                    toolGroups={toolGroups}
                    events={visibleEvents}
                    onSelectEvent={onSelectEvent}
                    onSelectToolGroup={onSelectToolGroup}
                  />
                );
              }
              return <ToolDistributionZone key={section.id} liveRun={liveRun} />;
            })}
          </div>
        ) : null}

        {activeTab === "trace" ? (
          <EvidenceZone
            artifacts={artifacts}
            liveRun={liveRun}
            onCreateCheckpointBranch={onCreateCheckpointBranch}
            onOpenTrace={onOpenTrace}
            onSelectArtifact={openArtifactOrOutputs}
            onSelectEvent={onSelectEvent}
            sessionId={sessionId}
          />
        ) : null}

        {activeTab === "outputs" ? (
          <DeliverablesZone
            artifacts={artifacts}
            events={visibleEvents}
            onMentionArtifact={onMentionArtifact}
            onOpenArtifactPage={onOpenArtifactPage}
            onPreviewArtifact={onPreviewArtifact}
            onPromoteArtifact={onPromoteArtifact}
            onArtifactExportJob={onArtifactExportJob}
            onSelectEvent={onSelectEvent}
            promotedArtifactIds={promotedArtifactIds}
          />
        ) : null}

        {activeTab === "detail" ? (
          selection?.type === "toolGroup" ? (
            <ToolGroupDetailView
              artifacts={artifacts}
              events={visibleEvents}
              group={selectedGroup}
              liveRun={liveRun}
              onBack={onClearSelection}
              onSelectEvent={onSelectEvent}
            />
          ) : selection?.type === "action" ? (
            <DetailView
              artifacts={artifacts}
              event={selectedEvent}
              events={visibleEvents}
              liveRun={liveRun}
              selection={selection}
              onBack={onClearSelection}
              onSelectEvent={onSelectEvent}
            />
          ) : (
            <EmptyState
              title={t("console.empty.noStepSelected")}
              description={t("console.empty.noStepSelectedDescription")}
              actionLabel={t("console.empty.goToOverview")}
              onAction={() => handleTabClick("overview")}
            />
          )
        ) : null}
      </div>
    </section>
  );
}

function TabButton({
  active,
  badge,
  onClick,
  children,
}: {
  active: boolean;
  badge?: number;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={[
        "flex shrink-0 cursor-pointer items-center gap-1 whitespace-nowrap rounded-lg px-2.5 py-1.5 text-xs font-semibold transition-colors duration-200",
        active
          ? "bg-primary text-white"
          : "text-muted hover:bg-surface-subtle hover:text-foreground",
      ].join(" ")}
    >
      {children}
      {badge !== undefined && badge > 0 ? (
        <span
          className={[
            "rounded-full px-1.5 text-[10px] font-bold leading-4",
            active ? "bg-white/20 text-white" : "bg-surface-subtle text-muted",
          ].join(" ")}
        >
          {badge}
        </span>
      ) : null}
    </button>
  );
}

function ConsoleSection({
  title,
  badge,
  collapsible = false,
  defaultExpanded = true,
  children,
}: {
  title: string;
  badge?: ReactNode;
  collapsible?: boolean;
  defaultExpanded?: boolean;
  children: ReactNode;
}) {
  const [expanded, setExpanded] = useState(defaultExpanded);

  useEffect(() => {
    if (defaultExpanded) setExpanded(true);
  }, [defaultExpanded]);

  const headerContent = (
    <>
      <h3 className={panelTitleClass}>{title}</h3>
      <span className="flex min-w-0 shrink-0 items-center gap-2">
        {badge}
        {collapsible ? (
          <span
            aria-hidden="true"
            className={[
              "inline-flex h-6 w-6 items-center justify-center rounded-md text-muted-light transition-transform duration-200",
              expanded ? "rotate-180" : "",
            ].join(" ")}
          >
            <svg viewBox="0 0 16 16" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
              <path d="m4 6 4 4 4-4" />
            </svg>
          </span>
        ) : null}
      </span>
    </>
  );

  return (
    <section className={panelShellClass}>
      {collapsible ? (
        <button
          type="button"
          aria-expanded={expanded}
          onClick={() => setExpanded((value) => !value)}
          className="flex w-full cursor-pointer items-center justify-between gap-3 rounded-lg text-left transition-colors duration-200 hover:bg-surface-subtle focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary-light/50"
        >
          <span className="flex min-w-0 flex-1 items-center justify-between gap-3 px-1 py-1">
            {headerContent}
          </span>
        </button>
      ) : (
        <div className="flex items-center justify-between gap-3">{headerContent}</div>
      )}
      {expanded ? <div className="mt-3">{children}</div> : null}
    </section>
  );
}

// Section 1: Summary first - current question, status, and run-level metrics.
function ConclusionZone({
  currentQuestion,
  liveRun,
  toolGroups,
  sessionUsage,
}: {
  currentQuestion?: string;
  liveRun: LiveRun;
  toolGroups: ProcessToolGroup[];
  sessionUsage: SessionUsageStats;
}) {
  const t = useT();
  const runUsage = useMemo(() => deriveRunUsage(liveRun), [liveRun]);
  const groupUsage = useMemo(
    () => deriveProcessGroupUsage(toolGroups, liveRun),
    [liveRun, toolGroups],
  );
  const hasRun = liveRun.runStatus !== "idle";
  const toolRatio =
    runUsage.toolCalls.total > 0
      ? `${runUsage.toolCalls.success}/${runUsage.toolCalls.total}`
      : "—";
  const successRate =
    runUsage.toolCalls.total > 0
      ? `${Math.round((runUsage.toolCalls.success / runUsage.toolCalls.total) * 100)}%`
      : "—";

  const sessionView = useMemo(
    () => deriveLiveSessionView(sessionUsage, liveRun),
    [liveRun, sessionUsage],
  );
  const tokenReported = sessionView.tokenUsageReported;
  const displayTokens = sessionView.tokens;
  const displayModels = sessionView.models;
  const totalTokens = displayTokens.inputTokens + displayTokens.outputTokens;
  const tokenKpi =
    hasRun && tokenReported ? formatCount(totalTokens) : "—";
  const tokenLine =
    hasRun && tokenReported
      ? `In ${formatCount(displayTokens.inputTokens)} / Out ${formatCount(
          displayTokens.outputTokens,
        )}${displayModels.length > 0 ? ` · ${displayModels.slice(0, 2).join(" / ")}` : ""}${
          displayTokens.costUsd !== undefined ? ` · $${displayTokens.costUsd.toFixed(4)}` : ""
        }`
      : undefined;

  return (
    <ConsoleSection title={t("console.sections.summary")}>
      <div>
        <div className={sectionLabelClass}>{t("console.currentQuestion")}</div>
        <p className="mt-1 text-sm leading-6 text-foreground">
          {currentQuestion ?? (
            <span className="text-muted-light">{t("console.startPrompt")}</span>
          )}
        </p>
      </div>

      {liveRun.errorMessage ? (
        <p className="mt-3 rounded-lg bg-step-error/10 px-3 py-2 text-xs leading-5 text-step-error">
          {liveRun.errorMessage}
        </p>
      ) : null}

      <div className="mt-4 grid grid-cols-2 gap-2">
        <KpiMetric
          label={liveRun.runHistory?.length ? t("console.stepsSession") : t("console.steps")}
          value={hasRun ? formatCount(groupUsage.stepCount) : "—"}
          meta={
            hasRun && groupUsage.toolCallCount > 0
              ? t("console.toolCallsMeta", { count: formatCount(groupUsage.toolCallCount) })
              : undefined
          }
          accentClass="text-primary"
        />
        <KpiMetric
          label={t("console.successRate")}
          value={successRate}
          meta={toolRatio}
          accentClass="text-step-success"
        />
        <KpiMetric
          label={liveRun.runHistory?.length ? t("console.sections.outputsSession") : t("console.sections.outputs")}
          value={hasRun ? formatCount(runUsage.artifactCount) : "—"}
          meta={t("console.itemsMeta")}
          accentClass="text-step-query"
        />
        <KpiMetric
          label={t("console.tokenCost")}
          value={tokenKpi}
          meta={tokenLine}
          accentClass={tokenReported ? "text-step-knowledge" : "text-muted-light"}
          stackMeta
        />
      </div>
    </ConsoleSection>
  );
}

// Section 2: Progress derived from actual tool calls; each step can open details.
function DynamicStepsList({
  liveRun,
  toolGroups,
  events,
  onSelectEvent,
  onSelectToolGroup,
}: {
  liveRun: LiveRun;
  toolGroups: ProcessToolGroup[];
  events: TimelineEvent[];
  onSelectEvent: (eventId: string) => void;
  onSelectToolGroup: (groupId: string) => void;
}) {
  const t = useT();
  const eventById = useMemo(
    () => new Map(events.map((event) => [event.id, event] as const)),
    [events],
  );
  const toolCallById = useMemo(
    () => new Map(liveRun.toolCalls.map((call) => [call.id, call] as const)),
    [liveRun.toolCalls],
  );
  const badge = toolGroups.length > 0 ? (
    <span className="tabular text-xs font-medium text-muted-light">
      {toolGroups.filter((group) => group.status === "success").length}/{toolGroups.length}
    </span>
  ) : null;

  return (
    <ConsoleSection title={t("console.sections.progress")} badge={badge}>
      {toolGroups.length === 0 ? (
        <EmptyState
          title={t("console.empty.noStepsYet")}
          description={t("console.empty.noStepsYetDescription")}
        />
      ) : (
        <ol className={["grid gap-1.5", consoleStepsListClass].join(" ")}>
          {toolGroups.map((group) => {
            const calls = group.toolCallIds
              .map((id) => toolCallById.get(id))
              .filter((call): call is LiveToolCallRecord => Boolean(call));
            const primaryCall = calls[0];
            const primaryEvent = primaryCall ? eventById.get(primaryCall.id) : undefined;
            const rawName = primaryEvent?.toolName ?? primaryCall?.name;
            const kind = calls.length > 1 ? "other" : dataStepKindForTool(rawName);
            const kindLabel = dataStepLabel(kind, t);
            const tone = stepKindTone(kind);
            const groupDuration =
              group.startedAtMs !== undefined && group.finishedAtMs !== undefined
                ? formatDuration(Math.max(0, group.finishedAtMs - group.startedAtMs))
                : group.status === "running"
                  ? t("common.running")
                  : "—";
            const displayTitle =
              calls.length === 1
                ? toolDisplayTitle(rawName, t)
                : group.title;
            const body = (
              <>
                <span className={["h-8 w-1 shrink-0 rounded-full", tone.bar].join(" ")} />
                <StepStatusDot status={group.status} />
                <span className="min-w-0 flex-1">
                  <span
                    className={[
                      "block truncate text-xs leading-5",
                      group.status === "failed"
                        ? "font-medium text-step-error"
                        : group.status === "running"
                          ? "font-medium text-foreground"
                          : "text-muted",
                    ].join(" ")}
                  >
                    {displayTitle}
                  </span>
                  <span className="text-[10px] text-muted-light">
                    {calls.length > 1
                      ? t("steps.stepMetaTools", {
                          n: group.stepNumber,
                          kind: kindLabel,
                          count: calls.length,
                        })
                      : t("steps.stepMeta", { n: group.stepNumber, kind: kindLabel })}
                  </span>
                </span>
                <span className="shrink-0 text-right text-[10px] text-muted-light">
                  <span className="block">{toolStatusLabel(group.status, t)}</span>
                  <span className="tabular block font-mono">{groupDuration}</span>
                </span>
              </>
            );
            return (
              <li
                key={group.id}
                className={group.status === "running" ? "step-streaming rounded-lg" : "step-enter"}
              >
                <button
                  type="button"
                  onClick={() => onSelectToolGroup(group.id)}
                  className={[
                    "flex w-full cursor-pointer items-center gap-2 rounded-lg border px-2 py-1.5 text-left transition-colors duration-200",
                    tone.border,
                    group.status === "running" ? tone.bg : "border-transparent hover:bg-surface-subtle",
                  ].join(" ")}
                >
                  {body}
                </button>
                {calls.length > 1 ? (
                  <div className="ml-5 mt-1 grid gap-1 border-l border-border pl-2">
                    {calls.map((call) => {
                      const event = eventById.get(call.id);
                      const title = toolDisplayTitle(event?.toolName ?? call.name, t);
                      return (
                        <button
                          key={call.id}
                          type="button"
                          onClick={() => onSelectEvent(call.id)}
                          className="flex min-w-0 cursor-pointer items-center gap-2 rounded-md px-2 py-1 text-left text-[11px] transition-colors duration-150 hover:bg-surface"
                        >
                          <StepStatusDot status={call.status} compact />
                          <span className="min-w-0 flex-1 truncate text-muted">{title}</span>
                          <span className="shrink-0 tabular font-mono text-muted-light">
                            {stepDurationLabel(call, t)}
                          </span>
                        </button>
                      );
                    })}
                  </div>
                ) : null}
              </li>
            );
          })}
        </ol>
      )}
    </ConsoleSection>
  );
}

// Section 3: Tool distribution, derived from whatever tools ran.
function ToolDistributionZone({ liveRun }: { liveRun: LiveRun }) {
  const t = useT();
  const runUsage = useMemo(() => deriveRunUsage(liveRun), [liveRun]);
  const entries = Object.entries(runUsage.toolCalls.byTool);
  if (entries.length === 0) return null;

  return (
    <ConsoleSection title={t("console.toolDistribution")} collapsible defaultExpanded={false}>
      <div className="grid gap-1.5">
        {entries.map(([name, bucket]) => {
          const kind = dataStepKindForTool(name);
          const kindLabel = dataStepLabel(kind, t);
          const tone = stepKindTone(kind);
          const share = runUsage.toolCalls.total > 0
            ? Math.max(8, Math.round((bucket.calls / runUsage.toolCalls.total) * 100))
            : 0;
          return (
            <div
              key={name}
              className="grid gap-2 rounded-lg border border-border bg-surface-subtle px-2.5 py-2"
            >
              <div className="flex items-center gap-2">
                <span className="min-w-0 flex-1">
                  <span className="block truncate font-mono text-xs text-foreground">{name}</span>
                  <span className={["text-[10px]", tone.text].join(" ")}>{kindLabel}</span>
                </span>
                <span className="tabular shrink-0 text-[11px] text-muted">{bucket.calls} calls</span>
                {bucket.failed > 0 ? (
                  <span className="shrink-0 rounded-full bg-step-error/10 px-2 py-0.5 text-[10px] font-semibold text-step-error">
                    Failed {bucket.failed}
                  </span>
                ) : null}
              </div>
              <div className="h-1.5 overflow-hidden rounded-full bg-surface">
                <div className={["h-full rounded-full", tone.bar].join(" ")} style={{ width: `${share}%` }} />
              </div>
            </div>
          );
        })}
      </div>
    </ConsoleSection>
  );
}

function StepStatusDot({
  status,
  compact = false,
}: {
  status: LiveToolCallRecord["status"];
  compact?: boolean;
}) {
  const tone =
    status === "success"
      ? "border-step-success bg-step-success"
      : status === "running"
        ? "border-primary-light bg-primary-light/20"
        : "border-step-error bg-step-error";
  return (
    <span
      className={[
        "flex shrink-0 items-center justify-center rounded-full border-2",
        compact ? "h-3 w-3" : "h-4 w-4",
        tone,
      ].join(" ")}
    >
      {status === "success" ? (
        <svg viewBox="0 0 12 12" className={compact ? "h-2 w-2 text-white" : "h-2.5 w-2.5 text-white"} fill="none" stroke="currentColor" strokeWidth={2.5} strokeLinecap="round" strokeLinejoin="round" aria-hidden>
          <path d="m2.5 6.5 2.5 2.5 4.5-5" />
        </svg>
      ) : status === "running" ? (
        <span className={compact ? "h-1 w-1 rounded-full bg-primary-light" : "h-1.5 w-1.5 rounded-full bg-primary-light"} />
      ) : (
        <span className="text-[9px] font-bold leading-none text-white">!</span>
      )}
    </span>
  );
}

// Section 3: Data trail, an inline trace that can expand full-screen.
function EvidenceZone({
  artifacts,
  liveRun,
  onCreateCheckpointBranch,
  onOpenTrace,
  onSelectArtifact,
  onSelectEvent,
  sessionId,
}: {
  artifacts: DataArtifact[];
  liveRun: LiveRun;
  onCreateCheckpointBranch?: (checkpointId: string) => Promise<void> | void;
  onOpenTrace: () => void;
  onSelectArtifact: (artifactId: string) => void;
  onSelectEvent: (eventId: string) => void;
  sessionId?: string;
}) {
  const t = useT();
  return (
    <div className="grid gap-4">
      <EmbeddedTraceDag
        artifacts={artifacts}
        liveRun={liveRun}
        onCreateCheckpointBranch={onCreateCheckpointBranch}
        onOpenFullscreen={onOpenTrace}
        onSelectArtifact={onSelectArtifact}
        onSelectEvent={onSelectEvent}
        sessionId={sessionId}
      />
      <RunConfigurationPanel liveRun={liveRun} />
      <ConsoleSection title={t("console.dataTrail")}>
        <TraceList
          artifacts={artifacts}
          liveRun={liveRun}
          onSelectArtifact={onSelectArtifact}
          onSelectEvent={onSelectEvent}
        />
      </ConsoleSection>
    </div>
  );
}

// Section 4: Outputs — static peek per card; open the peer page to read/reference.
type OutputsSortOrder = "oldest" | "newest";

function compareOutputsByTime(left: DataArtifact, right: DataArtifact): number {
  const leftMs = left.recordedAtMs ?? Number.POSITIVE_INFINITY;
  const rightMs = right.recordedAtMs ?? Number.POSITIVE_INFINITY;
  if (leftMs !== rightMs) {
    return leftMs - rightMs;
  }
  return left.id.localeCompare(right.id);
}

function DeliverablesZone({
  artifacts,
  events,
  onMentionArtifact,
  onOpenArtifactPage,
  onPreviewArtifact,
  onPromoteArtifact,
  onArtifactExportJob,
  onSelectEvent,
  promotedArtifactIds,
}: {
  artifacts: DataArtifact[];
  events: TimelineEvent[];
  onMentionArtifact?: (artifact: DataArtifact) => void;
  onOpenArtifactPage?: (artifactId: string) => void;
  onPreviewArtifact?: (artifactId: string) => void;
  onPromoteArtifact?: (artifact: DataArtifact) => Promise<void> | void;
  onArtifactExportJob?: (job: JobDto) => void;
  onSelectEvent: (eventId: string) => void;
  promotedArtifactIds?: ReadonlySet<string>;
}) {
  const t = useT();
  const exportReady = hasCapability("artifact.export");
  const [sortOrder, setSortOrder] = useState<OutputsSortOrder>("oldest");
  const sortedArtifacts = useMemo(() => {
    const next = [...artifacts].sort(compareOutputsByTime);
    return sortOrder === "oldest" ? next : next.reverse();
  }, [artifacts, sortOrder]);
  const badge = artifacts.length > 0 ? (
    <span className="flex items-center gap-2">
      <span className="inline-flex overflow-hidden rounded-md border border-border bg-surface text-[10px] font-semibold leading-4 text-muted">
        <button
          type="button"
          aria-pressed={sortOrder === "oldest"}
          onClick={() => setSortOrder("oldest")}
          className={[
            "px-1.5 py-0.5 transition-colors",
            sortOrder === "oldest" ? "bg-primary-light/15 text-primary" : "hover:text-foreground",
          ].join(" ")}
        >
          {t("common.oldest")}
        </button>
        <button
          type="button"
          aria-pressed={sortOrder === "newest"}
          onClick={() => setSortOrder("newest")}
          className={[
            "border-l border-border px-1.5 py-0.5 transition-colors",
            sortOrder === "newest" ? "bg-primary-light/15 text-primary" : "hover:text-foreground",
          ].join(" ")}
        >
          {t("common.newest")}
        </button>
      </span>
      <span className="tabular rounded-full bg-primary-light/15 px-1.5 text-[10px] font-bold leading-4 text-primary">
        {artifacts.length}
      </span>
    </span>
  ) : null;

  return (
    <div data-guide-id="run-output" className="grid gap-4">
      <ConsoleSection title={t("console.sections.outputs")} badge={badge}>
        {artifacts.length === 0 ? (
          <EmptyState
            title={t("console.empty.noOutputs")}
            description={t("console.empty.noOutputsDescription")}
          />
        ) : (
          <div className="grid gap-3">
            {sortedArtifacts.map((artifact) => {
              const sourceEvent = artifact.createdByEventId
                ? events.find((event) => event.id === artifact.createdByEventId) ?? null
                : null;
              return (
                <ArtifactCard
                  key={artifact.id}
                  artifact={artifact}
                  sourceEvent={sourceEvent}
                  exportReady={exportReady}
                  promoted={promotedArtifactIds?.has(artifact.id) ?? false}
                  onOpenArtifactPage={onOpenArtifactPage}
                  onPreviewArtifact={onPreviewArtifact}
                  onMentionArtifact={onMentionArtifact}
                  onPromoteArtifact={onPromoteArtifact}
                  onArtifactExportJob={onArtifactExportJob}
                  onSelectEvent={onSelectEvent}
                />
              );
            })}
            <p className="text-[11px] leading-4 text-muted-light">
              {exportReady
                ? t("console.outputsHintExport")
                : t("console.outputsHintBackend")}
            </p>
          </div>
        )}
      </ConsoleSection>
    </div>
  );
}

function ArtifactCard({
  artifact,
  sourceEvent,
  exportReady,
  promoted,
  onOpenArtifactPage,
  onPreviewArtifact,
  onMentionArtifact,
  onPromoteArtifact,
  onArtifactExportJob,
  onSelectEvent,
}: {
  artifact: DataArtifact;
  sourceEvent: TimelineEvent | null;
  exportReady: boolean;
  promoted: boolean;
  onOpenArtifactPage?: (artifactId: string) => void;
  onPreviewArtifact?: (artifactId: string) => void;
  onMentionArtifact?: (artifact: DataArtifact) => void;
  onPromoteArtifact?: (artifact: DataArtifact) => Promise<void> | void;
  onArtifactExportJob?: (job: JobDto) => void;
  onSelectEvent: (eventId: string) => void;
}) {
  const t = useT();
  const { busy, error, setError, downloadWhole, downloadFormat, exportJob } =
    useArtifactExportActions(onArtifactExportJob);
  const [promoting, setPromoting] = useState(false);
  const [showHistory, setShowHistory] = useState(false);
  const promoteReady = hasCapability("artifact.promote");
  const canMention = artifact.detail?.type === "file" && Boolean(artifact.detail.path);
  const formatExport = canFormatExport(artifact);
  const downloadBusy = busy !== null;
  const isSessionOutput = Boolean(artifact.logicalKey?.startsWith("session_file:"));
  const hasHistory = isSessionOutput && Boolean(artifact.versionCount && artifact.versionCount > 1);

  const handlePromote = async () => {
    if (!onPromoteArtifact || promoted || promoting) return;
    setPromoting(true);
    setError(null);
    try {
      await onPromoteArtifact(artifact);
    } catch (err) {
      setError(err instanceof Error ? err.message : t("console.failedToAddToProject"));
    } finally {
      setPromoting(false);
    }
  };

  const overflowItems: ActionMenuItem[] = [];
  if (canMention && onMentionArtifact) {
    overflowItems.push({
      key: "mention",
      label: t("console.mention"),
      onSelect: () => onMentionArtifact(artifact),
    });
  }
  if (promoteReady && onPromoteArtifact) {
    overflowItems.push({
      key: "promote",
      label: promoted
        ? t("console.addedToProject")
        : promoting
          ? t("console.adding")
          : t("console.addToProject"),
      disabled: promoted || promoting,
      onSelect: () => {
        void handlePromote();
      },
    });
  }
  if (sourceEvent) {
    overflowItems.push({
      key: "source",
      label: t("console.viewSource"),
      onSelect: () => onSelectEvent(sourceEvent.id),
    });
  }

  const downloadItems: ActionMenuItem[] = [
    {
      key: "whole",
      label: busy === "whole" ? t("console.downloadingEllipsis") : t("console.downloadFile"),
      disabled: downloadBusy,
      onSelect: () => void downloadWhole(artifact),
    },
    {
      key: "csv",
      label: busy === "csv" ? t("console.preparingCsv") : t("console.downloadCsv"),
      disabled: downloadBusy,
      onSelect: () => void downloadFormat(artifact, "csv"),
    },
    {
      key: "xlsx",
      label: busy === "xlsx" ? t("console.preparingXlsx") : t("console.downloadXlsx"),
      disabled: downloadBusy,
      onSelect: () => void downloadFormat(artifact, "xlsx"),
    },
    {
      key: "job",
      label: busy === "job" ? t("console.submitting") : t("console.backgroundExportXlsx"),
      disabled: downloadBusy,
      onSelect: () => void exportJob(artifact, "xlsx"),
    },
  ];

  return (
    <div className="min-w-0 rounded-xl border border-border bg-surface-subtle">
      <div className="min-w-0 p-3">
        <ArtifactCardHeader artifact={artifact} sourceEvent={sourceEvent} />
        <ArtifactStaticSnippet artifact={artifact} />
      </div>
      {error ? (
        <p className="border-t border-border bg-step-error/10 px-3 py-1.5 text-[11px] text-step-error">
          {error}
        </p>
      ) : null}
      {showHistory ? (
        <ArtifactVersionHistory artifactId={artifact.id} onClose={() => setShowHistory(false)} />
      ) : null}
      <div className="flex flex-wrap items-center justify-end gap-2 rounded-b-xl border-t border-border bg-surface px-3 py-2">
        <button
          type="button"
          onClick={() => onPreviewArtifact?.(artifact.id)}
          disabled={!onPreviewArtifact}
          className={`${btnPrimaryClass} disabled:cursor-not-allowed disabled:opacity-60`}
          title={t("console.previewTitle")}
        >
          {t("console.preview")}
        </button>
        <button
          type="button"
          onClick={() => onOpenArtifactPage?.(artifact.id)}
          disabled={!onOpenArtifactPage}
          className={`${btnSecondaryClass} disabled:cursor-not-allowed disabled:opacity-60`}
          title={t("console.citeTitle")}
        >
          {t("console.cite")}
        </button>
        {hasHistory && exportReady ? (
          <button
            type="button"
            onClick={() => setShowHistory((v) => !v)}
            className={`${btnSecondaryClass}`}
            title={t("console.viewVersionHistory")}
          >
            {showHistory
              ? t("console.hideHistory")
              : t("console.historyCount", { count: artifact.versionCount ?? 0 })}
          </button>
        ) : null}
        {exportReady ? (
          formatExport ? (
            <ActionMenu
              items={downloadItems}
              placement="up"
              align="right"
              triggerClass={`inline-flex items-center gap-1 ${btnSecondaryClass}`}
              triggerLabel={t("console.download")}
              ariaLabel={t("console.downloadOutput")}
            />
          ) : (
            <button
              type="button"
              onClick={() => void downloadWhole(artifact)}
              disabled={downloadBusy}
              className={`${btnSecondaryClass} disabled:cursor-not-allowed disabled:opacity-60`}
              title={
                downloadBusy
                  ? t("console.downloadingEllipsis")
                  : t("console.downloadOutputFile")
              }
            >
              {busy === "whole" ? t("console.downloading") : t("console.download")}
            </button>
          )
        ) : null}
        {overflowItems.length > 0 ? (
          <ActionMenu
            items={overflowItems}
            placement="up"
            align="right"
            triggerClass={`inline-flex items-center ${btnGhostClass}`}
            triggerIcon={<IconDots className="h-4 w-4" />}
            ariaLabel={t("console.moreActions")}
            title={t("console.moreActions")}
            showChevron={false}
          />
        ) : null}
      </div>
    </div>
  );
}

function ArtifactVersionHistory({
  artifactId,
  onClose,
}: {
  artifactId: string;
  onClose: () => void;
}) {
  const t = useT();
  const [versions, setVersions] = useState<Array<{
    id: string;
    version: number;
    fileId?: string;
    downloadUrl?: string;
    createdAt: string;
  }> | null>(null);
  const [downloadingId, setDownloadingId] = useState<string | null>(null);
  const [fetchError, setFetchError] = useState<string | null>(null);

  useEffect(() => {
    setVersions(null);
    setFetchError(null);
    void configApi.listArtifactVersions(artifactId)
      .then((res) => setVersions(res.versions))
      .catch(() => setFetchError(t("console.failedToLoadVersionHistory")));
  }, [artifactId, t]);

  const handleDownload = async (versionId: string) => {
    if (downloadingId) return;
    setDownloadingId(versionId);
    try {
      const { blob, filename } = await configApi.downloadArtifactVersion(artifactId, versionId);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      a.click();
      URL.revokeObjectURL(url);
    } catch {
      // silent
    } finally {
      setDownloadingId(null);
    }
  };

  return (
    <div className="border-t border-border bg-surface-subtle px-3 py-2">
      <div className="mb-1 flex items-center justify-between">
        <span className="text-[11px] font-medium text-muted">{t("console.versionHistory")}</span>
        <button
          type="button"
          onClick={onClose}
          className="text-[11px] text-muted-light hover:text-foreground"
        >
          {t("common.close")}
        </button>
      </div>
      {fetchError ? (
        <p className="text-[11px] text-step-error">{fetchError}</p>
      ) : versions === null ? (
        <p className="text-[11px] text-muted-light">{t("common.loading")}</p>
      ) : versions.length === 0 ? (
        <p className="text-[11px] text-muted-light">{t("console.noVersionsYet")}</p>
      ) : (
        <ul className="space-y-1">
          {[...versions].reverse().map((v) => (
            <li key={v.id} className="flex items-center justify-between gap-2">
              <span className="text-[11px] text-muted">
                v{v.version} · {new Date(v.createdAt).toLocaleString()}
              </span>
              {v.fileId ? (
                <button
                  type="button"
                  disabled={downloadingId === v.id}
                  onClick={() => void handleDownload(v.id)}
                  className={`text-[11px] ${btnGhostClass} disabled:opacity-50`}
                >
                  {downloadingId === v.id ? t("console.downloadingEllipsis") : t("console.download")}
                </button>
              ) : (
                <span className="text-[11px] text-muted-light">{t("console.noFile")}</span>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/**
 * Zero-click static peek rendered directly on the collapsed card. Uses only
 * in-memory detail/summary; never triggers a per-card network preview fetch.
 */
function ArtifactStaticSnippet({ artifact }: { artifact: DataArtifact }) {
  const detail = artifact.detail;

  if (!detail) {
    return (
      <p className="mt-3 rounded-lg border border-dashed border-border bg-surface px-2.5 py-2 text-[11px] leading-4 text-muted-light">
        {artifact.summary ? `${truncateText(artifact.summary, 140)} · ` : ""}Open to view full content
      </p>
    );
  }

  if (detail.type === "sql") {
    return (
      <pre className="mt-3 max-h-24 overflow-hidden rounded-lg border border-border bg-surface px-2.5 py-2 font-mono text-[10px] leading-4 text-muted">
        <code>{firstLines(detail.sql, 5)}</code>
      </pre>
    );
  }

  if (detail.type === "dataset") {
    const maxCols = 4;
    const maxRows = 4;
    const cols = detail.columns.slice(0, maxCols);
    const moreCols = detail.columns.length > maxCols;
    const rows = detail.rows.slice(0, maxRows);
    return (
      <div className="mt-3 overflow-hidden rounded-lg border border-border bg-surface">
        <table className="w-full table-fixed text-left text-[10px]">
          <thead className="bg-surface-subtle text-muted-light">
            <tr>
              {cols.map((column) => (
                <th key={column} className="truncate px-2 py-1 font-semibold">
                  {column}
                </th>
              ))}
              {moreCols ? <th className="w-6 px-2 py-1 text-center">…</th> : null}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, rowIndex) => (
              <tr key={rowIndex} className="border-t border-border">
                {row.slice(0, maxCols).map((cell, cellIndex) => (
                  <td key={cellIndex} className="truncate px-2 py-1 text-muted">
                    {formatConsoleTableCell(cell)}
                  </td>
                ))}
                {moreCols ? <td className="px-2 py-1 text-center text-muted-light">…</td> : null}
              </tr>
            ))}
          </tbody>
        </table>
        {detail.rows.length > maxRows ? (
          <div className="border-t border-border px-2 py-1 text-[10px] text-muted-light">
            {detail.rows.length.toLocaleString()} rows · open to view all
          </div>
        ) : null}
      </div>
    );
  }

  if (detail.type === "chart") {
    const pointCount =
      (detail.series ?? []).reduce((sum, series) => sum + series.points.length, 0) ||
      detail.points.length;
    return (
      <p className="mt-3 rounded-lg border border-border bg-surface px-2.5 py-2 text-[11px] leading-4 text-muted">
        {detail.chartType ?? "chart"} · {pointCount} points · open to view chart
      </p>
    );
  }

  if (detail.type === "report") {
    const first = detail.sections[0];
    return (
      <p className="mt-3 rounded-lg border border-border bg-surface px-2.5 py-2 text-[11px] leading-4 text-muted">
        {first ? truncateText(`${first.heading} — ${first.body}`, 180) : "Open to view full report"}
      </p>
    );
  }

  // file
  if (detail.content) {
    return (
      <pre className="mt-3 max-h-24 overflow-hidden rounded-lg border border-border bg-surface px-2.5 py-2 font-mono text-[10px] leading-4 text-muted">
        <code>{firstLines(detail.content, 5)}</code>
      </pre>
    );
  }
  return (
    <p className="mt-3 rounded-lg border border-dashed border-border bg-surface px-2.5 py-2 text-[11px] leading-4 text-muted-light">
      {detail.path}
      {detail.size !== undefined ? ` · ${detail.size.toLocaleString()} bytes` : ""} · open to view full content
    </p>
  );
}

function truncateText(value: string, max: number): string {
  const normalized = value.replace(/\s+/gu, " ").trim();
  return normalized.length > max ? `${normalized.slice(0, max)}…` : normalized;
}

function firstLines(value: string, count: number): string {
  const lines = value.split(/\r?\n/u);
  const head = lines.slice(0, count).join("\n");
  return lines.length > count ? `${head}\n…` : head;
}

function eventForToolCall(
  events: TimelineEvent[],
  call: LiveToolCallRecord | undefined,
): TimelineEvent | undefined {
  if (!call) return undefined;
  return (
    events.find((event) => event.id === call.id) ??
    (call.stepId ? events.find((event) => event.stepId === call.stepId) : undefined)
  );
}

function resolveGroupProducedArtifacts(
  liveRun: LiveRun,
  group: ProcessToolGroup,
  events: TimelineEvent[],
  artifacts: DataArtifact[],
): DataArtifact[] {
  const artifactIds = new Set<string>();
  const linkedEventIds = new Set(group.toolCallIds);
  for (const event of events) {
    if (group.toolCallIds.includes(event.id)) {
      linkedEventIds.add(event.id);
      event.artifactIds?.forEach((id) => artifactIds.add(id));
    }
  }
  for (const call of liveRun.toolCalls) {
    if (!group.toolCallIds.includes(call.id) || !call.stepId) continue;
    for (const event of events) {
      if (event.stepId === call.stepId) {
        linkedEventIds.add(event.id);
        event.artifactIds?.forEach((id) => artifactIds.add(id));
      }
    }
  }
  for (const artifact of artifacts) {
    if (artifact.createdByEventId && linkedEventIds.has(artifact.createdByEventId)) {
      artifactIds.add(artifact.id);
    }
  }
  return artifacts.filter((artifact) => artifactIds.has(artifact.id));
}

function resolveTokenUsageForGroup(
  liveRun: LiveRun,
  group: ProcessToolGroup,
): ReturnType<typeof resolveTokenUsageForEvent> {
  return resolveTokenUsageForToolCallIds(liveRun, group.toolCallIds);
}

function ToolGroupDetailView({
  artifacts,
  events,
  group,
  liveRun,
  onBack,
  onSelectEvent,
}: {
  artifacts: DataArtifact[];
  events: TimelineEvent[];
  group: ProcessToolGroup | null;
  liveRun: LiveRun;
  onBack: () => void;
  onSelectEvent: (eventId: string) => void;
}) {
  const t = useT();
  const calls = useMemo(
    () =>
      group
        ? group.toolCallIds
            .map((id) => liveRun.toolCalls.find((call) => call.id === id))
            .filter((call): call is LiveToolCallRecord => Boolean(call))
        : [],
    [group, liveRun.toolCalls],
  );
  const [selectedToolCallId, setSelectedToolCallId] = useState<string | null>(
    calls[0]?.id ?? null,
  );

  useEffect(() => {
    setSelectedToolCallId(calls[0]?.id ?? null);
  }, [calls, group?.id]);

  if (!group) {
    return (
      <EmptyState
        title={t("console.noProcessStepSelected")}
        description={t("console.noProcessStepSelectedDescription")}
      />
    );
  }

  const selectedCall =
    calls.find((call) => call.id === selectedToolCallId) ?? calls[0];
  const selectedEvent = eventForToolCall(events, selectedCall);
  const producedArtifacts = resolveGroupProducedArtifacts(liveRun, group, events, artifacts);
  const selectedProducedArtifacts = selectedEvent
    ? resolveProducedArtifacts(liveRun, selectedEvent, artifacts)
    : [];
  const tokenUsage = resolveTokenUsageForGroup(liveRun, group);
  const groupDuration =
    group.startedAtMs !== undefined && group.finishedAtMs !== undefined
      ? formatDuration(Math.max(0, group.finishedAtMs - group.startedAtMs))
      : group.status === "running"
        ? t("common.running")
        : "—";

  return (
    <div className="grid gap-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className={sectionLabelClass}>{t("console.stepDetails", { n: group.stepNumber })}</div>
          <h3 className="mt-1 text-sm font-semibold text-foreground">
            {group.title}
          </h3>
          <p className="mt-1 text-xs leading-5 text-muted">{group.summary}</p>
        </div>
        <button type="button" onClick={onBack} className={btnSecondaryClass}>
          {t("common.back")}
        </button>
      </div>

      <div className="grid grid-cols-2 gap-2">
        <Metric label="Step duration" value={groupDuration} />
        <Metric label="Status" value={toolStatusLabel(group.status, t)} />
        <Metric label="Tool calls" value={`${calls.length} calls`} />
        <Metric label="Outputs" value={`${producedArtifacts.length} items`} />
      </div>

      <Panel title={t("console.toolCalls")}>
        <div className="divide-y divide-border overflow-hidden rounded-lg border border-border bg-surface">
          {calls.map((call) => {
            const event = eventForToolCall(events, call);
            const active = selectedCall?.id === call.id;
            return (
              <button
                key={call.id}
                type="button"
                onClick={() => setSelectedToolCallId(call.id)}
                className={[
                  "flex w-full cursor-pointer items-center gap-2 px-3 py-2 text-left transition-colors duration-150",
                  active ? "bg-surface-subtle" : "hover:bg-surface-subtle",
                ].join(" ")}
              >
                <StepStatusDot status={call.status} compact />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-xs font-medium text-foreground">
                    {toolDisplayTitle(call.name, t)}
                  </span>
                  <span className="mt-0.5 block truncate font-mono text-[10px] text-muted-light">
                    {call.name}
                  </span>
                </span>
                <span className="shrink-0 text-right text-[10px] text-muted-light">
                  <span className="block">{toolStatusLabel(call.status, t)}</span>
                  <span className="tabular block font-mono">{stepDurationLabel(call, t)}</span>
                </span>
              </button>
            );
          })}
        </div>
      </Panel>

      <Panel title={t("console.usage")}>
        <TokenUsagePanel usage={tokenUsage} />
      </Panel>

      <Panel title={t("console.toolDetails")}>
        {selectedEvent && selectedCall ? (
          <div className="grid gap-3">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <div className={sectionLabelClass}>
                  {dataStepLabel(selectedEvent.kind, t)} · {selectedEvent.ts}
                </div>
                <div className="mt-1 truncate text-xs font-semibold text-foreground">
                  {toolDisplayTitle(selectedEvent.toolName ?? selectedCall.name, t)}
                </div>
              </div>
              <button
                type="button"
                onClick={() => onSelectEvent(selectedEvent.id)}
                className={btnSecondaryClass}
              >
                Tool details
              </button>
            </div>
            <EventPayloadView
              event={selectedEvent}
              producedArtifacts={selectedProducedArtifacts}
              toolCall={selectedCall}
            />
          </div>
        ) : (
          <p className="text-xs leading-5 text-muted-light">
            This tool call is not yet linked to a displayable step event.
          </p>
        )}
      </Panel>

      <Panel title={t("console.outputLineage")}>
        {producedArtifacts.length > 0 ? (
          <div className="grid gap-2">
            {producedArtifacts.map((artifact) => (
              <div
                key={artifact.id}
                className="border-b border-border pb-2 last:border-b-0 last:pb-0"
              >
                <div className="truncate text-xs font-semibold text-foreground">
                  {artifact.title}
                </div>
                <p className="mt-1 text-[11px] leading-4 text-muted-light">
                  {artifact.summary}
                </p>
              </div>
            ))}
          </div>
        ) : (
          <p className="text-xs text-muted-light">{t("console.stepNoOutputsYet")}</p>
        )}
      </Panel>
    </div>
  );
}

function DetailView({
  artifacts,
  event,
  events,
  liveRun,
  selection,
  onBack,
  onSelectEvent,
}: {
  artifacts: DataArtifact[];
  event: TimelineEvent | null;
  events: TimelineEvent[];
  liveRun: LiveRun;
  selection: ActiveSelection;
  onBack: () => void;
  onSelectEvent: (eventId: string) => void;
}) {
  const toolCall = event ? resolveToolCallForEvent(liveRun, event) : undefined;
  const producedArtifacts = event
    ? resolveProducedArtifacts(liveRun, event, artifacts)
    : [];

  return (
    <ActionDetail
      event={event}
      liveRun={liveRun}
      producedArtifacts={producedArtifacts}
      toolCall={toolCall}
      onBack={onBack}
    />
  );
}

function ArtifactDetailPanel({
  artifact,
  events,
  onBack,
  onSelectEvent,
}: {
  artifact: DataArtifact | null;
  events: TimelineEvent[];
  onBack: () => void;
  onSelectEvent: (eventId: string) => void;
}) {
  const t = useT();
  if (!artifact) {
    return (
      <EmptyState
        title={t("console.noContentSelected")}
        description={t("console.noContentSelectedDescription")}
      />
    );
  }
  const sourceEvent = artifact.createdByEventId
    ? events.find((event) => event.id === artifact.createdByEventId)
    : null;

  return (
    <div className="grid min-w-0 gap-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className={sectionLabelClass}>
            {artifact.type ?? artifact.kind} · {artifact.version ?? "v1"}
          </div>
          <h3 className="mt-1 text-sm font-semibold text-foreground">
            {artifact.title}
          </h3>
          <p className="mt-1 text-xs leading-5 text-muted">
            {artifact.summary}
          </p>
        </div>
        <button
          type="button"
          onClick={onBack}
          className={btnSecondaryClass}
        >
          {t("common.back")}
        </button>
      </div>

      {sourceEvent && (
        <div className="rounded-lg border border-border bg-surface-subtle p-3">
          <div className={sectionLabelClass}>
            Source
          </div>
          <button
            type="button"
            onClick={() => onSelectEvent(sourceEvent.id)}
            className="mt-1 text-left text-xs font-semibold text-primary underline-offset-2 hover:underline"
          >
            {sourceEvent.title} →
          </button>
        </div>
      )}

      <ArtifactExpandedDetail artifact={artifact} />
    </div>
  );
}

function detailStatusLabel(
  toolCall: LiveToolCallRecord | undefined,
  activityStatus: TimelineEvent["activityStatus"] | undefined,
  t: ReturnType<typeof useT>,
): string {
  if (toolCall) {
    return toolStatusLabel(resolveTraceToolStatus(toolCall.status, activityStatus), t);
  }
  if (activityStatus === "failed") return t("common.failed");
  if (activityStatus === "completed") return t("common.completed");
  if (activityStatus === "running") return t("common.running");
  return "—";
}

function ActionDetail({
  event,
  liveRun,
  producedArtifacts,
  toolCall,
  onBack,
}: {
  event: TimelineEvent | null;
  liveRun: LiveRun;
  producedArtifacts: DataArtifact[];
  toolCall?: LiveToolCallRecord;
  onBack: () => void;
}) {
  const t = useT();
  const [expandedArtifactId, setExpandedArtifactId] = useState<string | null>(
    null,
  );
  const workspaceMetadata = resolveWorkspaceMetadataForToolCall(
    liveRun,
    toolCall?.id,
  );
  const sandboxOutputs = resolveSandboxOutputsForToolCall(liveRun, toolCall);

  if (!event) {
    return (
      <EmptyState
        title={t("console.noActionSelected")}
        description={t("console.noActionSelectedDescription")}
      />
    );
  }

  return (
    <div className="grid gap-3">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className={sectionLabelClass}>
            {dataStepLabel(event.kind, t)} · {event.ts}
          </div>
          <h3 className="mt-1 text-sm font-semibold text-foreground">
            {toolDisplayTitle(event.toolName, t)}
          </h3>
          <p className="mt-1 text-xs leading-5 text-muted">
            {event.summary}
          </p>
        </div>
        <button
          type="button"
          onClick={onBack}
          className={btnSecondaryClass}
        >
          {t("common.back")}
        </button>
      </div>

      <div className="grid grid-cols-2 gap-2">
        <Metric label="Step duration" value={stepDurationLabel(toolCall, t)} />
        <Metric
          label="Status"
          value={detailStatusLabel(toolCall, event.activityStatus, t)}
        />
      </div>

      {event.thought && (
        <Panel title={t("console.reasoning")}>
          <p className="text-sm italic leading-6 text-muted">
            {event.thought}
          </p>
        </Panel>
      )}

      <Panel title={t("console.actionDetails")}>
        <EventPayloadView
          event={event}
          producedArtifacts={producedArtifacts}
          toolCall={toolCall}
        />
      </Panel>

      {workspaceMetadata ? (
        <Panel title={t("console.workspaceMetadata")}>
          <p className="text-xs leading-5 text-muted">
            {formatWorkspaceMetadataSummary(workspaceMetadata)}
          </p>
          <div className={consoleScrollXShellClass}>
            <pre className={[consoleCodeBlockBaseClass, "max-h-48"].join(" ")}>
              <code className={consoleCodeInnerClass}>
                {JSON.stringify(workspaceMetadata.payload, null, 2)}
              </code>
            </pre>
          </div>
        </Panel>
      ) : null}

      {sandboxOutputs.length > 0 ? (
        <Panel title={t("console.sandboxOutput")}>
          <div className="grid gap-2">
            {sandboxOutputs.map((output, index) => (
              <div
                key={`${output.kind}-${output.receivedAt}-${index}`}
                className="rounded-lg border border-border bg-surface-subtle p-2.5"
              >
                <div className="text-[10px] font-semibold uppercase tracking-wide text-muted-light">
                  {output.kind}
                </div>
                <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap font-mono text-[11px] leading-4 text-muted">
                  {formatSandboxOutputText(output)}
                </pre>
              </div>
            ))}
          </div>
        </Panel>
      ) : null}

      <Panel title={t("console.outputLineage")}>
        {producedArtifacts.length > 0 ? (
          <div className="grid gap-2">
            {producedArtifacts.map((artifact) => {
              const expanded = expandedArtifactId === artifact.id;
              return (
                <div
                  key={artifact.id}
                  className="overflow-hidden rounded-lg border border-border bg-surface-subtle"
                >
                  <button
                    type="button"
                    onClick={() =>
                      setExpandedArtifactId((current) =>
                        current === artifact.id ? null : artifact.id,
                      )
                    }
                    className="w-full cursor-pointer px-3 py-2 text-left transition-colors duration-200 hover:bg-surface"
                  >
                    <div className="text-xs font-semibold text-foreground">
                      {artifact.title}
                    </div>
                    <p className="mt-1 text-[11px] leading-4 text-muted-light">
                      {artifact.summary}
                    </p>
                    <span className="mt-1 inline-block text-[10px] font-medium text-muted">
                      {expanded
                        ? t("console.collapse")
                        : artifact.detail || artifact.previewAvailable
                          ? t("console.expandContent")
                          : t("console.noDetails")}
                    </span>
                  </button>
                  {expanded ? (
                    <div className="max-h-[min(480px,55vh)] overflow-y-auto overflow-x-hidden border-t border-border px-3 pb-3 pt-2">
                      <ArtifactExpandedDetail artifact={artifact} />
                    </div>
                  ) : null}
                </div>
              );
            })}
            <p className="text-[11px] leading-4 text-muted-light">
              See the Outputs tab for the full list.
            </p>
          </div>
        ) : (
          <p className="text-xs text-muted-light">{t("console.actionNoOutputs")}</p>
        )}
      </Panel>
    </div>
  );
}

function TokenUsagePanel({
  usage,
}: {
  usage: ReturnType<typeof resolveTokenUsageForEvent>;
}) {
  if (!usage.reported) {
    return (
      <div className="rounded-lg border border-dashed border-border bg-surface-subtle p-3">
        <div className="mb-2 inline-flex rounded-full bg-surface px-2 py-0.5 text-[10px] font-semibold text-muted-light">
          Backend unsupported
        </div>
        <p className="text-xs leading-5 text-muted-light">
          Per-step token, model, and cost usage require backend token_usage events. Run-level usage is shown in Overview.
        </p>
      </div>
    );
  }

  const total = usage.inputTokens + usage.outputTokens;
  const inputShare =
    total > 0 ? Math.max(8, Math.round((usage.inputTokens / total) * 100)) : 0;
  const outputShare =
    total > 0 ? Math.max(8, Math.round((usage.outputTokens / total) * 100)) : 0;

  return (
    <div className="grid gap-3">
      {usage.approximate ? (
        <div className="inline-flex w-fit rounded-full bg-step-warning/10 px-2 py-0.5 text-[10px] font-semibold text-step-warning">
          Approximate match (step_number only)
        </div>
      ) : null}
      <div className="grid grid-cols-2 gap-2">
        <Metric label="Input tokens" value={formatCount(usage.inputTokens)} />
        <Metric label="Output tokens" value={formatCount(usage.outputTokens)} />
      </div>
      <div className="grid gap-2 rounded-lg border border-border bg-surface-subtle p-3">
        <TokenUsageBar
          label="Input"
          value={usage.inputTokens}
          width={inputShare}
          tone="bg-step-knowledge"
        />
        <TokenUsageBar
          label="Output"
          value={usage.outputTokens}
          width={outputShare}
          tone="bg-primary-light"
        />
      </div>
      <div className="flex flex-wrap items-center gap-2 text-[11px] text-muted-light">
        {usage.models.length > 0 ? (
          usage.models.map((model) => (
            <span
              key={model}
              className="rounded-full border border-border bg-surface px-2 py-0.5 font-medium text-muted"
            >
              {model}
            </span>
          ))
        ) : null}
        {usage.costUsd !== undefined ? (
          <span className="rounded-full bg-step-knowledge/10 px-2 py-0.5 font-semibold text-step-knowledge">
            ${usage.costUsd.toFixed(4)}
          </span>
        ) : null}
      </div>
    </div>
  );
}

function TokenUsageBar({
  label,
  value,
  width,
  tone,
}: {
  label: string;
  value: number;
  width: number;
  tone: string;
}) {
  return (
    <div className="grid grid-cols-[42px_1fr_64px] items-center gap-2">
      <span className="text-[11px] font-medium text-muted-light">{label}</span>
      <div className="h-2 overflow-hidden rounded-full bg-surface">
        <div
          className={["h-full rounded-full", tone].join(" ")}
          style={{ width: `${width}%` }}
        />
      </div>
      <span className="tabular text-right text-[11px] font-semibold text-muted">
        {formatCount(value)}
      </span>
    </div>
  );
}

function SchemaFieldChip({ field }: { field: string }) {
  const [name, type] = field.split(" · ");
  return (
    <span className="inline-flex overflow-hidden rounded border border-border bg-surface font-mono text-[10px]">
      <span className="px-1.5 py-0.5 text-muted">{name}</span>
      {type ? (
        <span className="border-l border-border bg-surface-subtle px-1.5 py-0.5 text-muted-light">
          {type}
        </span>
      ) : null}
    </span>
  );
}

function EventPayloadView({
  event,
  producedArtifacts = [],
  toolCall,
}: {
  event: TimelineEvent;
  producedArtifacts?: DataArtifact[];
  toolCall?: LiveToolCallRecord;
}) {
  const t = useT();
  if (event.kind === "inspect") {
    const payload = event.payload as {
      tables: Array<{ name: string; description: string; fields: string[] }>;
    };
    if (payload.tables.length === 0) {
      if (toolCall?.result) {
        if (toolResultLooksLikeError(toolCall.result)) {
          return (
            <ToolFailureResult
              toolName={toolCall.name ?? event.toolName ?? "inspect_schema"}
              result={toolCall.result}
            />
          );
        }
        return (
          <ToolFormattedResult
            toolName={toolCall.name ?? event.toolName ?? "inspect_schema"}
            result={toolCall.result}
            variant="console"
          />
        );
      }
      return (
        <p className="text-xs leading-5 text-muted-light">
          Agent is inspecting the selected data source schema (inspect_schema).
        </p>
      );
    }
    return (
      <div className="grid gap-2">
        {payload.tables.map((table) => (
          <div key={table.name} className="rounded-lg border border-border bg-surface-subtle p-3">
            <div className="font-mono text-xs font-semibold text-foreground">
              {table.name}
            </div>
            <p className="mt-1 text-[11px] leading-4 text-muted">
              {table.description}
            </p>
            <div className="mt-2 flex flex-wrap gap-1">
              {table.fields.map((field) => (
                <SchemaFieldChip key={field} field={field} />
              ))}
            </div>
          </div>
        ))}
      </div>
    );
  }

  if (event.kind === "query") {
    const payload = event.payload as {
      question: string;
      sql: string;
      scannedRows: number;
      durationMs: number;
      errorMessage?: string;
    };
    const failed = event.activityStatus === "failed" || toolCall?.status === "failed";
    const errorMessage = payload.errorMessage;
    const resolvedSql =
      payload.sql ||
      (toolCall?.result ? sqlFromToolPayload(undefined, toolCall.result) : undefined) ||
      "";
    const datasetDetail = producedArtifacts.find(
      (artifact) => artifact.detail?.type === "dataset",
    )?.detail;
    return (
      <div className="grid gap-3">
        {payload.question && <Metric label="Question" value={payload.question} />}
        {resolvedSql ? (
          <div className={[consoleScrollXShellClass, "min-w-0"].join(" ")}>
            <pre className={[consoleCodeBlockBaseClass, "max-h-80"].join(" ")}>
              <code className={consoleCodeInnerClass}>{resolvedSql}</code>
            </pre>
          </div>
        ) : failed ? (
          <p className="text-xs leading-5 text-step-error">
            {errorMessage || "Read-only SQL execution failed and returned no SQL parameters."}
          </p>
        ) : (
          <p className="text-xs leading-5 text-muted-light">
            Generating read-only SQL. Parameters will appear here after they arrive.
          </p>
        )}
        {failed && errorMessage && resolvedSql ? (
          <p className="rounded-lg bg-step-error/10 px-2.5 py-2 text-xs leading-5 text-step-error">
            {errorMessage}
          </p>
        ) : null}
        {(payload.scannedRows > 0 || payload.durationMs > 0) && (
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
            <span>Scanned {payload.scannedRows.toLocaleString()} rows</span>
            <span>·</span>
            <span>{payload.durationMs}ms</span>
          </div>
        )}
        {datasetDetail?.type === "dataset" ? (
          <div className="grid gap-2">
            <div className={sectionLabelClass}>{t("console.resultPreview")}</div>
            <ArtifactDetailView detail={datasetDetail} />
          </div>
        ) : toolCall?.result && toolResultLooksLikeError(toolCall.result) ? (
          <ToolFailureResult
            toolName={toolCall.name ?? event.toolName ?? "run_sql_readonly"}
            result={toolCall.result}
          />
        ) : toolCall?.result && parseSqlToolResult(toolCall.result) ? (
          <div className="grid gap-2">
            <div className={sectionLabelClass}>{t("console.resultPreview")}</div>
            <ToolFormattedResult
              toolName={toolCall.name ?? event.toolName ?? "run_sql_readonly"}
              result={toolCall.result}
              variant="console"
            />
          </div>
        ) : (
          <p className="rounded-lg border border-dashed border-border bg-surface-subtle px-2.5 py-2 text-[11px] leading-4 text-muted-light">
            SQL result tables are shown from linked dataset artifacts. This step has not returned preview rows yet.
          </p>
        )}
      </div>
    );
  }

  const payload = event.payload as GenericStepPayload;
  const toolName = event.toolName ?? toolCall?.name;
  const formattedResult = toolCall?.result ?? payload.rawResult;

  if (toolName && formattedResult) {
    if (toolResultLooksLikeError(formattedResult)) {
      return (
        <div className="grid gap-3">
          {payload.description ? (
            <p className="text-xs leading-5 text-muted">{payload.description}</p>
          ) : null}
          <ToolFailureResult toolName={toolName} result={formattedResult} />
        </div>
      );
    }
    return (
      <div className="grid gap-3">
        {payload.description ? (
          <p className="text-xs leading-5 text-muted">{payload.description}</p>
        ) : null}
        <ToolFormattedResult
          toolName={toolName}
          result={formattedResult}
          variant="console"
        />
      </div>
    );
  }

  return (
    <div className="grid gap-3">
      {payload.description ? (
        <p className="text-xs leading-5 text-muted">{payload.description}</p>
      ) : (
        <p className="text-xs leading-5 text-muted-light">
          This data operation has no additional parameters yet.
        </p>
      )}
      {payload.rawResult ? (
        <div className={consoleScrollXShellClass}>
          <pre className={[consoleCodeBlockBaseClass, "max-h-80"].join(" ")}>
            <code className={consoleCodeInnerClass}>{payload.rawResult}</code>
          </pre>
        </div>
      ) : null}
    </div>
  );
}

function ArtifactExpandedDetail({
  artifact,
  exportReady = hasCapability("artifact.export"),
}: {
  artifact: DataArtifact;
  exportReady?: boolean;
}) {
  const t = useT();
  const [detail, setDetail] = useState<ArtifactDetail | undefined>(artifact.detail);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setDetail(artifact.detail);
    setError(null);
  }, [artifact.detail, artifact.id]);

  useEffect(() => {
    if (!exportReady || !artifactDetailNeedsPreviewFetch(artifact, detail)) {
      return;
    }

    let cancelled = false;
    setLoading(true);
    setError(null);

    void artifactExportClient
      .fetchPreview(artifact.id)
      .then((preview) => {
        if (cancelled) {
          return;
        }
        const loaded = artifactDetailFromPreview(artifact, preview);
        if (loaded) {
          setDetail((current) => mergeArtifactDetail(current, loaded));
          return;
        }
        setError("Preview data is empty or unsupported.");
      })
      .catch((fetchError: unknown) => {
        if (cancelled) {
          return;
        }
        setError(
          fetchError instanceof Error ? fetchError.message : t("console.failedToLoadPreview"),
        );
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [
    artifact.id,
    artifact.kind,
    artifact.previewAvailable,
    artifact.title,
    artifact.type,
    detail,
    exportReady,
  ]);

  if (detail) {
    return <ArtifactDetailView detail={detail} artifact={artifact} />;
  }

  if (loading) {
    return <p className="text-xs text-muted-light">{t("console.loadingPreview")}</p>;
  }

  if (error) {
    return (
      <p className="rounded-lg bg-step-error/10 px-2.5 py-2 text-xs text-step-error">
        {error}
      </p>
    );
  }

  return (
    <EmptyState
      title={t("console.noDetails")}
      description={
        artifact.previewAvailable && exportReady
          ? "Preview could not be loaded after expanding. Try again later."
          : "This output does not include viewable details yet."
      }
    />
  );
}

function ArtifactDetailView({
  detail,
  artifact,
}: {
  detail: ArtifactDetail;
  artifact?: DataArtifact;
}) {
  if (detail.type === "sql") {
    return (
      <div className="grid min-w-0 gap-3">
        <div className={consoleScrollXShellClass}>
          <pre className={[consoleCodeBlockBaseClass, "max-h-80"].join(" ")}>
            <code className={consoleCodeInnerClass}>{detail.sql}</code>
          </pre>
        </div>
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
          <span className="rounded-full bg-step-success/10 px-2.5 py-1 font-semibold text-step-success">
            Executed
          </span>
          <span>Scanned {detail.scannedRows.toLocaleString()} rows</span>
          <span>·</span>
          <span>{detail.durationMs}ms</span>
        </div>
      </div>
    );
  }

  if (detail.type === "dataset") {
    return <DatasetDetailView detail={detail} />;
  }

  if (detail.type === "chart") {
    return <ChartDetailView detail={detail} />;
  }

  if (detail.type === "file") {
    return <FileDetailView detail={detail} artifact={artifact} />;
  }

  return (
    <div className="grid gap-2">
      {detail.sections.map((section) => (
        <div
          key={section.heading}
          className="rounded-xl border border-border bg-surface p-3"
        >
          <div className="text-xs font-semibold text-foreground">
            {section.heading}
          </div>
          <div className="mt-1 text-xs leading-5 text-muted">
            <ArtifactMarkdownPreview content={section.body} />
          </div>
        </div>
      ))}
    </div>
  );
}

type FileKind = "image" | "markdown" | "csv" | "tsv" | "json" | "yaml" | "html" | "pdf" | "text";

function classifyFileKind(path: string): FileKind {
  const ext = /\.([a-z0-9]+)$/u.exec(path.toLowerCase())?.[1];
  switch (ext) {
    case "png":
    case "jpg":
    case "jpeg":
    case "gif":
    case "webp":
    case "svg":
    case "bmp":
    case "ico":
      return "image";
    case "md":
    case "markdown":
    case "mdx":
      return "markdown";
    case "csv":
      return "csv";
    case "tsv":
      return "tsv";
    case "json":
    case "jsonl":
    case "geojson":
      return "json";
    case "yaml":
    case "yml":
      return "yaml";
    case "html":
    case "htm":
      return "html";
    case "pdf":
      return "pdf";
    default:
      return "text";
  }
}

function tryFormatJson(content: string): string | undefined {
  try {
    return JSON.stringify(JSON.parse(content), null, 2);
  } catch {
    return undefined;
  }
}

function FileCodeBlock({ content, bare = false }: { content: string; bare?: boolean }) {
  return (
    <div className={consoleScrollXShellClass}>
      <pre className={[consoleCodeBlockBaseClass, bare ? "max-h-[70vh]" : "max-h-80"].join(" ")}>
        <code className={consoleCodeInnerClass}>{content}</code>
      </pre>
    </div>
  );
}

function FileNotEmbeddedNote({ downloadable }: { downloadable: boolean }) {
  return (
    <p className="text-[11px] leading-4 text-muted-light">
      {downloadable
        ? "Inline preview is unavailable for this file (binary or too large). Use Download to get the full file."
        : "File content is not embedded for preview (too large or non-text)."}
    </p>
  );
}

function HtmlFilePreview({ content }: { content: string }) {
  const t = useT();
  const [showSource, setShowSource] = useState(false);
  return (
    <div className="grid gap-2">
      <div className="flex justify-end">
        <button
          type="button"
          onClick={() => setShowSource((value) => !value)}
          className={`h-7 ${btnSecondaryClass}`}
        >
          {showSource ? t("console.rendered") : t("console.source")}
        </button>
      </div>
      {showSource ? (
        <FileCodeBlock content={content} />
      ) : (
        <iframe
          sandbox=""
          srcDoc={content}
          title={t("console.htmlPreview")}
          className="h-80 w-full rounded-lg border border-border bg-white"
        />
      )}
    </div>
  );
}

export function FileDetailView({
  detail,
  artifact,
  bare = false,
}: {
  detail: Extract<ArtifactDetail, { type: "file" }>;
  artifact?: DataArtifact;
  /** On a dedicated page, skip the nested card chrome and duplicated meta line. */
  bare?: boolean;
}) {
  const kind = classifyFileKind(detail.path);
  const fileBacked = Boolean(artifact?.fileId);
  const imageSrc =
    kind === "image" && artifact?.fileId
      ? artifactExportClient.contentUrl(artifact.id)
      : undefined;
  const pdfSrc =
    kind === "pdf" && artifact?.fileId
      ? artifactExportClient.contentUrl(artifact.id)
      : undefined;

  const renderBody = () => {
    if (kind === "image") {
      if (!imageSrc) {
        return <FileNotEmbeddedNote downloadable={fileBacked} />;
      }
      return (
        <a href={imageSrc} target="_blank" rel="noreferrer" className="block">
          {/* eslint-disable-next-line @next/next/no-img-element */}
          <img
            src={imageSrc}
            alt={detail.path}
            className="max-h-80 w-auto max-w-full rounded-lg border border-border bg-white object-contain"
          />
        </a>
      );
    }

    if (kind === "pdf") {
      if (!pdfSrc) {
        return <FileNotEmbeddedNote downloadable={fileBacked} />;
      }
      return (
        <iframe
          src={pdfSrc}
          title={detail.path}
          className={[
            "w-full rounded-lg border border-border bg-white",
            bare ? "h-[70vh]" : "h-80",
          ].join(" ")}
        />
      );
    }

    if (!detail.content) {
      return <FileNotEmbeddedNote downloadable={fileBacked} />;
    }

    if (kind === "markdown") {
      return <ArtifactMarkdownPreview content={detail.content} bare={bare} />;
    }

    if (kind === "csv" || kind === "tsv") {
      const parsed = parseCsvTextPreview(detail.content, kind === "tsv" ? "\t" : ",");
      if (parsed) {
        return (
          <DatasetDetailView
            detail={{ type: "dataset", columns: parsed.columns, rows: parsed.rows }}
          />
        );
      }
      return <FileCodeBlock content={detail.content} bare={bare} />;
    }

    if (kind === "json") {
      return <FileCodeBlock content={tryFormatJson(detail.content) ?? detail.content} bare={bare} />;
    }

    if (kind === "html") {
      return <HtmlFilePreview content={detail.content} />;
    }

    return <FileCodeBlock content={detail.content} bare={bare} />;
  };

  if (bare) {
    return <div className="grid min-w-0 gap-2 text-xs text-muted">{renderBody()}</div>;
  }

  return (
    <div className="grid gap-2 rounded-xl border border-border bg-surface-subtle p-3 text-xs text-muted">
      <div className="font-mono font-semibold text-foreground">{detail.path}</div>
      <div className="flex flex-wrap gap-3 text-muted">
        {detail.size !== undefined ? <span>{detail.size.toLocaleString()} bytes</span> : null}
        {detail.mtime ? <span>modified {detail.mtime}</span> : null}
        {detail.tool ? <span>via {detail.tool}</span> : null}
      </div>
      {renderBody()}
    </div>
  );
}

function downloadTextFile(filename: string, content: string, mimeType: string) {
  if (typeof document === "undefined") return;
  const blob = new Blob([content], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

type ChartDisplayType = "bar" | "line" | "pie";

function isChartDisplayType(value: string | undefined): value is ChartDisplayType {
  return value === "bar" || value === "line" || value === "pie";
}

async function downloadSvgAsPng(container: HTMLDivElement | null, filename: string) {
  if (!container) return;
  const svg = container.querySelector("svg");
  if (!svg) return;
  const serializer = new XMLSerializer();
  const source = serializer.serializeToString(svg);
  const svgBlob = new Blob([source], { type: "image/svg+xml;charset=utf-8" });
  const url = URL.createObjectURL(svgBlob);
  const image = new Image();
  await new Promise<void>((resolve, reject) => {
    image.onload = () => resolve();
    image.onerror = () => reject(new Error("Failed to export chart"));
    image.src = url;
  });
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.ceil(svg.getBoundingClientRect().width));
  canvas.height = Math.max(1, Math.ceil(svg.getBoundingClientRect().height));
  const context = canvas.getContext("2d");
  if (!context) {
    URL.revokeObjectURL(url);
    return;
  }
  context.fillStyle = "#ffffff";
  context.fillRect(0, 0, canvas.width, canvas.height);
  context.drawImage(image, 0, 0, canvas.width, canvas.height);
  URL.revokeObjectURL(url);
  const pngUrl = canvas.toDataURL("image/png");
  const link = document.createElement("a");
  link.href = pngUrl;
  link.download = filename;
  link.click();
}

export function ChartDetailView({
  detail,
}: {
  detail: Extract<ArtifactDetail, { type: "chart" }>;
}) {
  const t = useT();
  const points = detail.points.length > 0
    ? detail.points
    : detail.series?.[0]?.points ?? [];
  const initialType = isChartDisplayType(detail.chartType) ? detail.chartType : "bar";
  const [chartType, setChartType] = useState<ChartDisplayType>(initialType);
  const chartRef = useRef<HTMLDivElement | null>(null);

  if (points.length === 0) {
    return (
      <EmptyState
        title={t("console.noChartData")}
        description={t("console.noChartDataDescription")}
      />
    );
  }

  const unit = detail.unit ?? "";
  const chartData = points.map((point) => ({
    label: point.label,
    value: point.value,
  }));

  return (
    <div className="grid gap-3 rounded-xl border border-border bg-surface-subtle p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap gap-1">
          {(["bar", "line", "pie"] as const).map((type) => (
            <button
              key={type}
              type="button"
              onClick={() => setChartType(type)}
              className={[
                "h-7 rounded-md px-2 text-[11px] font-semibold transition",
                chartType === type
                  ? "bg-slate-900 text-white"
                  : "border border-border bg-surface text-muted hover:text-foreground",
              ].join(" ")}
            >
              {type}
            </button>
          ))}
        </div>
        <button
          type="button"
          onClick={() => {
            void downloadSvgAsPng(chartRef.current, "chart-preview.png").catch(() => undefined);
          }}
          className={`h-7 ${btnSecondaryClass}`}
        >
          Export PNG
        </button>
      </div>
      <div ref={chartRef} className="h-64 min-w-0">
        <ResponsiveContainer width="100%" height="100%">
          {chartType === "line" ? (
            <LineChart data={chartData} margin={{ top: 8, right: 8, bottom: 8, left: 0 }}>
              <XAxis dataKey="label" tick={{ fontSize: 11 }} />
              <YAxis tick={{ fontSize: 11 }} />
              <Tooltip formatter={(value) => [`${unit}${value}`, "value"]} />
              <Line
                type="monotone"
                dataKey="value"
                stroke="var(--step-visualize)"
                strokeWidth={2}
                dot={{ r: 3 }}
              />
            </LineChart>
          ) : chartType === "pie" ? (
            <PieChart>
              <Tooltip formatter={(value) => [`${unit}${value}`, "value"]} />
              <Pie
                data={chartData}
                dataKey="value"
                nameKey="label"
                outerRadius={86}
                label={(entry) => String((entry as { name?: unknown }).name ?? "")}
              >
                {chartData.map((point, index) => (
                  <Cell
                    key={point.label}
                    fill={index % 2 === 0 ? "var(--step-visualize)" : "var(--primary-light)"}
                  />
                ))}
              </Pie>
            </PieChart>
          ) : (
            <BarChart data={chartData} margin={{ top: 8, right: 8, bottom: 8, left: 0 }}>
              <XAxis dataKey="label" tick={{ fontSize: 11 }} />
              <YAxis tick={{ fontSize: 11 }} />
              <Tooltip formatter={(value) => [`${unit}${value}`, "value"]} />
              <Bar dataKey="value" fill="var(--step-visualize)" radius={[6, 6, 0, 0]} />
            </BarChart>
          )}
        </ResponsiveContainer>
      </div>
      {detail.series && detail.series.length > 0 ? (
        <div className="flex flex-wrap gap-1">
          {detail.series.map((series) => (
            <span
              key={series.name}
              className="rounded-full border border-border bg-surface px-2 py-0.5 text-[10px] font-medium text-muted"
            >
              {series.name}
            </span>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function DatasetDetailView({
  detail,
}: {
  detail: Extract<ArtifactDetail, { type: "dataset" }>;
}) {
  const t = useT();
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<TableSortState | null>(null);
  const rows = useMemo(
    () => sortTableRows(filterTableRows(detail.rows, query), sort),
    [detail.rows, query, sort],
  );

  const toggleSort = (columnIndex: number) => {
    setSort((current) => {
      if (!current || current.columnIndex !== columnIndex) {
        return { columnIndex, direction: "asc" };
      }
      if (current.direction === "asc") {
        return { columnIndex, direction: "desc" };
      }
      return null;
    });
  };

  const exportCsv = () => {
    downloadTextFile(
      "dataset-preview.csv",
      tableToCsv(detail.columns, rows),
      "text/csv;charset=utf-8",
    );
  };

  return (
    <div className="grid gap-2">
      <div className="flex flex-wrap items-center gap-2">
        <label className="min-w-[180px] flex-1">
          <span className="sr-only">{t("console.searchResultTable")}</span>
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            className="h-8 w-full rounded-lg border border-border bg-white px-2.5 text-xs text-slate-900 outline-none transition placeholder:text-slate-400 focus:border-slate-400"
            placeholder={t("console.searchResultTable")}
          />
        </label>
        <button
          type="button"
          onClick={exportCsv}
          className={`h-8 ${btnSecondaryClass}`}
          title={t("console.exportFilteredResult")}
        >
          Export current view CSV
        </button>
        <span className="text-[11px] text-muted-light">
          {rows.length.toLocaleString()} / {detail.rows.length.toLocaleString()} rows
        </span>
      </div>
      <div className={consoleTableShellClass}>
        <div className="max-h-[min(480px,60vh)] overflow-y-auto">
          <table className="min-w-max w-full text-left text-xs">
            <thead className="sticky top-0 z-10 bg-surface-subtle text-muted-light shadow-[0_1px_0_0_var(--border)]">
              <tr>
                {detail.columns.map((column, columnIndex) => {
                  const active = sort?.columnIndex === columnIndex;
                  const suffix = active
                    ? sort.direction === "asc"
                      ? " ↑"
                      : " ↓"
                    : "";
                  return (
                    <th key={column} className="whitespace-nowrap px-3 py-2 font-semibold">
                      <button
                        type="button"
                        onClick={() => toggleSort(columnIndex)}
                        className="cursor-pointer text-left transition hover:text-foreground"
                        title={`Sort by ${column} `}
                      >
                        {column}
                        {suffix}
                      </button>
                    </th>
                  );
                })}
              </tr>
            </thead>
            <tbody>
              {rows.length === 0 ? (
                <tr>
                  <td
                    colSpan={Math.max(detail.columns.length, 1)}
                    className="border-t border-border px-3 py-6 text-center text-muted-light"
                  >
                    No matching result rows.
                  </td>
                </tr>
              ) : (
                rows.map((row, rowIndex) => (
                  <tr key={rowIndex} className="border-t border-border">
                    {row.map((cell, cellIndex) => (
                      <td
                        key={cellIndex}
                        className={[
                          "whitespace-nowrap px-3 py-2",
                          cellIndex === 0
                            ? "font-medium text-foreground"
                            : "text-muted",
                        ].join(" ")}
                      >
                        {formatConsoleTableCell(cell)}
                      </td>
                    ))}
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

function formatConsoleTableCell(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") {
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

function ArtifactCardHeader({
  artifact,
  sourceEvent,
}: {
  artifact: DataArtifact;
  sourceEvent?: TimelineEvent | null;
}) {
  const t = useT();
  const label = artifact.type ?? artifact.kind;
  const tone = artifactToneForType(artifact.type ?? artifact.kind);

  return (
    <>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="truncate text-sm font-semibold text-foreground">
            {artifact.title}
          </div>
          <p className="mt-1 text-xs leading-5 text-muted">
            {artifact.summary}
          </p>
        </div>
        <span
          className={[
            "inline-flex shrink-0 items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-semibold",
            tone.bg,
            tone.border,
            tone.text,
          ].join(" ")}
        >
          <span className="text-[10px] leading-none">{tone.icon}</span>
          {label}
        </span>
      </div>
      <div className="mt-2 flex min-w-0 items-center gap-1.5 text-[11px] leading-4">
        {sourceEvent ? (
          <>
            <span
              className={[
                "shrink-0 rounded-full px-2 py-0.5 font-semibold",
                stepKindTone(sourceEvent.kind).bg,
                stepKindTone(sourceEvent.kind).text,
              ].join(" ")}
            >
              {dataStepLabel(sourceEvent.kind, t)}
            </span>
            <span
              className="min-w-0 truncate text-muted-light"
              title={toolDisplayTitle(sourceEvent.toolName, t)}
            >
              From {toolDisplayTitle(sourceEvent.toolName, t)}
            </span>
          </>
        ) : (
          <span className="text-muted-light">{t("console.sourceStepNotLinked")}</span>
        )}
      </div>
      <div className="mt-3 text-[11px] text-muted-light">
        <span>{artifact.version ?? "v1"}</span>
      </div>
    </>
  );
}

function EmptyState({
  title,
  description,
  actionLabel,
  onAction,
}: {
  title: string;
  description: string;
  actionLabel?: string;
  onAction?: () => void;
}) {
  return (
    <div className={`${emptyStateClass} p-5`}>
      <div
        aria-hidden="true"
        className="mx-auto mb-3 flex h-9 w-9 items-center justify-center rounded-xl border border-border bg-surface text-sm font-semibold text-muted-light"
      >
        ·
      </div>
      <div className="text-sm font-semibold text-foreground">{title}</div>
      <p className="mt-2 text-xs leading-5 text-muted">{description}</p>
      {actionLabel && onAction ? (
        <button
          type="button"
          onClick={onAction}
          className={`mt-4 ${btnSecondaryClass}`}
        >
          {actionLabel}
        </button>
      ) : null}
    </div>
  );
}

function Panel({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className={panelShellClass}>
      <h3 className={`mb-3 ${panelTitleClass}`}>{title}</h3>
      {children}
    </section>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-border bg-surface-subtle p-3">
      <div className={metricLabelClass}>{label}</div>
      <div className={`mt-1 ${metricValueClass}`}>{value}</div>
    </div>
  );
}

function KpiMetric({
  label,
  value,
  meta,
  accentClass,
  stackMeta = false,
}: {
  label: string;
  value: string;
  meta?: string;
  accentClass: string;
  stackMeta?: boolean;
}) {
  return (
    <div className="rounded-xl border border-border bg-surface-subtle p-3 shadow-sm">
      <div className={metricLabelClass}>{label}</div>
      {stackMeta ? (
        <div className="mt-1 grid min-w-0 gap-0.5">
          <span className={`${kpiValueClass} ${accentClass}`}>{value}</span>
          {meta ? (
            <span className="break-words text-[11px] font-medium leading-snug text-muted-light">
              {meta}
            </span>
          ) : null}
        </div>
      ) : (
        <div className="mt-1 flex min-w-0 items-end gap-1.5">
          <span className={`${kpiValueClass} ${accentClass}`}>{value}</span>
          {meta ? (
            <span className="mb-1 shrink text-[11px] font-medium text-muted-light">
              {meta}
            </span>
          ) : null}
        </div>
      )}
    </div>
  );
}
