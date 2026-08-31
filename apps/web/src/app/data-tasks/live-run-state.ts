import type {
  EvidenceRef,
  EvidenceResolutionDiagnostics,
  EvidenceResolutionIssue,
} from "@datafoundry/contracts";
import type {
  ArtifactDetail,
  DataArtifact,
  DataStepKind,
  DataStepPayload,
  SchemaTable,
  TimelineEvent,
} from "./data-task-state";
import { dataStepKindForTool, emptyStepPayload, toolDisplayTitle } from "./data-task-state";
import { formatRunErrorMessage } from "./run-error-message";
import { parseToolResultRecord } from "./tool-result-normalize";

export type LiveTaskStatus = "pending" | "running" | "completed" | "failed";

export type LivePlanTask = {
  id: string;
  title: string;
  status: LiveTaskStatus;
};

export type LiveAudit = {
  id: string;
  datasourceId?: string;
  status?: string;
  rowCount?: number;
  elapsedMs?: number;
};

export type LiveRunStatus = "idle" | "running" | "suspended" | "completed" | "failed" | "canceled";

export type LiveToolCallRecord = {
  id: string;
  name: string;
  status: "running" | "success" | "failed";
  /** Linked ACTIVITY STEP step_id when correlated with a backend tool wrapper. */
  stepId?: string;
  /** Raw payload from AG-UI TOOL_CALL_RESULT when CopilotKit thread lacks tool message. */
  result?: string;
  startedAtMs?: number;
  finishedAtMs?: number;
};

export type ToolCallStats = {
  total: number;
  success: number;
  failed: number;
  byTool: Record<string, { calls: number; success: number; failed: number }>;
};

export type SqlUsageStats = {
  total: number;
  success: number;
  failed: number;
  rowsScanned: number;
  elapsedMs: number;
};

export type TokenUsageStats = {
  inputTokens: number;
  outputTokens: number;
  costUsd?: number;
};

export type LiveTokenUsageRecord = TokenUsageStats & {
  stepNumber?: number;
  stepId?: string;
  toolCallId?: string;
  toolName?: string;
  model?: string;
};

export type RunUsageSnapshot = {
  runStatus: LiveRunStatus;
  errorMessage?: string;
  durationMs?: number;
  toolCalls: ToolCallStats;
  sql: SqlUsageStats;
  artifactCount: number;
  tokens: TokenUsageStats;
  tokenUsageReported: boolean;
  models: string[];
};

export type SessionUsageStats = {
  runCount: number;
  completedRuns: number;
  failedRuns: number;
  toolCalls: ToolCallStats;
  sql: SqlUsageStats;
  artifactCount: number;
  tokens: TokenUsageStats;
  tokenUsageReported: boolean;
  models: string[];
};

export type LiveSandboxOutput = {
  kind: string;
  receivedAt: number;
  payload: unknown;
};

export type LiveWorkspaceMetadata = {
  toolCallId?: string;
  toolName?: string;
  receivedAt: number;
  payload: unknown;
};

export type LiveSkillSelection = {
  mode?: string;
  selected: Array<{
    id: string;
    name?: string;
    revision?: number;
    tags?: string[];
  }>;
  effectiveToolPolicy?: Record<string, unknown>;
  audit: unknown[];
  raw: unknown;
};

export type LiveGoalSnapshot = {
  objective?: string;
  status?: string;
  source?: string;
  raw: unknown;
};

export type LiveResolvedRunConfig = {
  activeDatasourceId?: string;
  activeLlmProfileId?: string;
  enabledDatasourceIds?: string[];
  enabledKnowledgeIds?: string[];
  enabledMcpServerIds?: string[];
  enabledSkillIds?: string[];
  selectedSkills?: Array<{ id: string; name?: string }>;
  fileIds?: string[];
  evidenceRefs?: EvidenceRef[];
  evidenceResolution?: EvidenceResolutionDiagnostics;
  raw: unknown;
};

export type LiveContextReport = {
  name: "context.compiled" | "context.prompt-verified";
  receivedAt: number;
  value: unknown;
};

export type LiveSessionTitle = {
  sessionId: string;
  title: string;
};

export type LiveRunHistoryEntry = {
  startedAt?: number;
  finishedAt?: number;
  status: LiveRunStatus;
  errorMessage?: string;
  /** Cumulative toolCalls.length when this segment ended. */
  toolCallEndIndex: number;
  /** Cumulative audits.length when this segment ended. */
  auditEndIndex: number;
};

export type LiveRun = {
  runId?: string;
  plan: LivePlanTask[];
  events: TimelineEvent[];
  artifacts: DataArtifact[];
  audits: LiveAudit[];
  runStatus: LiveRunStatus;
  errorMessage?: string;
  toolCalls: LiveToolCallRecord[];
  runStartedAt?: number;
  runFinishedAt?: number;
  tokenUsage?: TokenUsageStats;
  tokenUsageEvents: LiveTokenUsageRecord[];
  workspaceMetadata: LiveWorkspaceMetadata[];
  sandboxOutputs: LiveSandboxOutput[];
  skillSelection?: LiveSkillSelection;
  goal?: LiveGoalSnapshot;
  resolvedRunConfig?: LiveResolvedRunConfig;
  sessionTitle?: LiveSessionTitle;
  contextReports: LiveContextReport[];
  /** Completed run segments within the current chat thread. */
  runHistory?: LiveRunHistoryEntry[];
};

type AgUiLikeEvent = {
  type?: string;
  [key: string]: unknown;
};

const defaultPlan: LivePlanTask[] = [
  { id: "schema", title: "Inspect data source schema", status: "pending" },
  { id: "sql", title: "Generate and run read-only SQL", status: "pending" },
  { id: "final", title: "Generate final answer", status: "pending" },
];

export function createInitialLiveRun(): LiveRun {
  return {
    plan: defaultPlan,
    events: [],
    artifacts: [],
    audits: [],
    runStatus: "idle",
    toolCalls: [],
    tokenUsageEvents: [],
    workspaceMetadata: [],
    sandboxOutputs: [],
    contextReports: [],
    runHistory: [],
  };
}

export function getSegmentToolCallStartIndex(liveRun: LiveRun): number {
  return liveRun.runHistory?.at(-1)?.toolCallEndIndex ?? 0;
}

export function getSegmentAuditStartIndex(liveRun: LiveRun): number {
  return liveRun.runHistory?.at(-1)?.auditEndIndex ?? 0;
}

export function deriveSegmentRunUsage(liveRun: LiveRun): RunUsageSnapshot {
  const toolCallStart = getSegmentToolCallStartIndex(liveRun);
  const auditStart = getSegmentAuditStartIndex(liveRun);
  const segmentTools = liveRun.toolCalls.slice(toolCallStart);
  const segmentToolIds = new Set(segmentTools.map((call) => call.id));
  const segmentAudits = liveRun.audits.slice(auditStart);
  const segmentArtifactCount = liveRun.artifacts.filter(
    (artifact) => artifact.createdByEventId && segmentToolIds.has(artifact.createdByEventId),
  ).length;
  const base = deriveRunUsage(liveRun);

  return {
    ...base,
    toolCalls: toolCallsToStats(segmentTools),
    sql: sqlAuditsToStats(segmentAudits),
    artifactCount: segmentArtifactCount,
  };
}

export function archiveCurrentRunSegment(state: LiveRun): LiveRunHistoryEntry[] {
  if (state.runStartedAt === undefined) return state.runHistory ?? [];

  const lastEndIndex = state.runHistory?.at(-1)?.toolCallEndIndex;
  if (lastEndIndex === state.toolCalls.length) {
    return state.runHistory ?? [];
  }

  const resolvedStatus =
    state.runStatus === "running"
      ? "completed"
      : state.runStatus;

  return [
    ...(state.runHistory ?? []),
    {
      startedAt: state.runStartedAt,
      finishedAt: state.runFinishedAt ?? Date.now(),
      status: resolvedStatus,
      errorMessage: state.errorMessage,
      toolCallEndIndex: state.toolCalls.length,
      auditEndIndex: state.audits.length,
    },
  ];
}

function hasSessionActivity(state: LiveRun): boolean {
  return (
    state.toolCalls.length > 0 ||
    state.events.length > 0 ||
    state.artifacts.length > 0 ||
    state.audits.length > 0 ||
    (state.runHistory?.length ?? 0) > 0
  );
}

export function createInitialSessionUsage(): SessionUsageStats {
  return {
    runCount: 0,
    completedRuns: 0,
    failedRuns: 0,
    toolCalls: emptyToolCallStats(),
    sql: emptySqlUsageStats(),
    artifactCount: 0,
    tokens: emptyTokenUsageStats(),
    tokenUsageReported: false,
    models: [],
  };
}

function tokenUsageFromRun(liveRun: LiveRun): {
  tokens: TokenUsageStats;
  tokenUsageReported: boolean;
  models: string[];
} {
  const tokens = liveRun.tokenUsage ?? emptyTokenUsageStats();
  const tokenUsageReported =
    tokens.inputTokens > 0 ||
    tokens.outputTokens > 0 ||
    (tokens.costUsd ?? 0) > 0;
  return { tokens, tokenUsageReported, models: uniqueModels(liveRun.tokenUsageEvents) };
}

export function deriveRunUsage(liveRun: LiveRun): RunUsageSnapshot {
  const durationMs =
    liveRun.runStartedAt !== undefined && liveRun.runFinishedAt !== undefined
      ? Math.max(0, liveRun.runFinishedAt - liveRun.runStartedAt)
      : liveRun.runStartedAt !== undefined && liveRun.runStatus === "running"
        ? Math.max(0, Date.now() - liveRun.runStartedAt)
        : undefined;

  const { tokens, tokenUsageReported, models } = tokenUsageFromRun(liveRun);

  return {
    runStatus: liveRun.runStatus,
    errorMessage: liveRun.errorMessage,
    durationMs,
    toolCalls: toolCallsToStats(liveRun.toolCalls),
    sql: sqlAuditsToStats(liveRun.audits),
    artifactCount: liveRun.artifacts.length,
    tokens,
    tokenUsageReported,
    models,
  };
}

export function accumulateSessionUsage(
  session: SessionUsageStats,
  run: RunUsageSnapshot,
  runOutcome: "completed" | "failed",
): SessionUsageStats {
  return {
    runCount: session.runCount + 1,
    completedRuns: session.completedRuns + (runOutcome === "completed" ? 1 : 0),
    failedRuns: session.failedRuns + (runOutcome === "failed" ? 1 : 0),
    toolCalls: mergeToolCallStats(session.toolCalls, run.toolCalls),
    sql: mergeSqlUsageStats(session.sql, run.sql),
    artifactCount: session.artifactCount + run.artifactCount,
    tokens: mergeTokenUsageStats(session.tokens, run.tokens),
    tokenUsageReported: session.tokenUsageReported || run.tokenUsageReported,
    models: uniqueStrings([...session.models, ...run.models]),
  };
}

/** Session totals for overview; merges in-progress run without double-counting completed runs. */
export function deriveLiveSessionView(
  session: SessionUsageStats,
  liveRun: LiveRun,
): SessionUsageStats & { includesInProgressRun: boolean } {
  if (liveRun.runStatus !== "running" && liveRun.runStatus !== "suspended") {
    return { ...session, includesInProgressRun: false };
  }

  const run = deriveRunUsage(liveRun);
  return {
    runCount: session.runCount + 1,
    completedRuns: session.completedRuns,
    failedRuns: session.failedRuns,
    toolCalls: mergeToolCallStats(session.toolCalls, run.toolCalls),
    sql: mergeSqlUsageStats(session.sql, run.sql),
    artifactCount: session.artifactCount + run.artifactCount,
    tokens: mergeTokenUsageStats(session.tokens, run.tokens),
    tokenUsageReported: session.tokenUsageReported || run.tokenUsageReported,
    models: uniqueStrings([...session.models, ...run.models]),
    includesInProgressRun: true,
  };
}

export function reduceLiveRunEvent(state: LiveRun, event: AgUiLikeEvent): LiveRun {
  switch (event.type) {
    case "RUN_STARTED":
      const runId = eventRunId(event) ?? state.runId;
      if (state.runStatus === "running") {
        if (runId && state.runId && runId !== state.runId) {
          return {
            ...state,
            runId,
            errorMessage: undefined,
          };
        }
        return {
          ...state,
          ...(runId ? { runId } : {}),
        };
      }
      if (state.runStatus === "suspended") {
        return {
          ...state,
          ...(runId ? { runId } : {}),
          runStatus: "running",
          errorMessage: undefined,
        };
      }
      if (hasSessionActivity(state)) {
        return {
          ...state,
          runHistory: archiveCurrentRunSegment(state),
          ...(runId ? { runId } : { runId: undefined }),
          runStatus: "running",
          runStartedAt: Date.now(),
          runFinishedAt: undefined,
          errorMessage: undefined,
          plan: defaultPlan,
          tokenUsage: undefined,
          tokenUsageEvents: [],
          resolvedRunConfig: undefined,
          skillSelection: undefined,
          goal: undefined,
        };
      }
      return {
        ...createInitialLiveRun(),
        ...(runId ? { runId } : {}),
        runStatus: "running",
        runStartedAt: Date.now(),
      };
    case "RUN_FINISHED":
      if (state.runStatus === "completed" || state.runStatus === "canceled") {
        // STATE_DELTA may mark the run completed before RUN_FINISHED; still finalize
        // any tool calls left at "running" (e.g. TOOL_CALL_END without RESULT).
        return applyTerminalRunStatus(state, state.runStatus);
      }
      if (stringValue(event.status) === "cancelled" || stringValue(event.status) === "canceled") {
        return applyTerminalRunStatus(state, "canceled", {
          runFinishedAt: Date.now(),
        });
      }
      if (state.runStatus === "suspended") {
        return {
          ...state,
          runFinishedAt: Date.now(),
        };
      }
      return applyTerminalRunStatus(state, "completed", {
        runFinishedAt: Date.now(),
        errorMessage: undefined,
        plan: state.plan.map((task) =>
          task.status === "running" || task.id === "final"
            ? { ...task, status: "completed" }
            : task,
        ),
      });
    case "RUN_ERROR":
      if (shouldIgnoreStaleRunError(state, event)) {
        return state;
      }
      return applyTerminalRunStatus(state, "failed", {
        runFinishedAt: Date.now(),
        errorMessage: formatRunErrorMessage(stringValue(event.message) ?? undefined),
      });
    case "STATE_SNAPSHOT":
      return reduceStateSnapshot(state, event);
    case "STATE_DELTA":
      return reduceStateDelta(state, event);
    case "ACTIVITY_SNAPSHOT":
      return reduceActivitySnapshot(state, event);
    case "ACTIVITY_DELTA":
      return reduceActivityDelta(state, event);
    case "TOOL_CALL_START":
    case "TOOL_CALL_ARGS":
    case "TOOL_CALL_END":
    case "TOOL_CALL_RESULT":
      return reduceToolEvent(state, event);
    case "CUSTOM":
      return reduceCustomEvent(state, event);
    default:
      return state;
  }
}

const TERMINAL_RUN_STATUSES = new Set<LiveRunStatus>(["completed", "failed", "canceled"]);

function terminalToolCallStatus(
  runStatus: LiveRunStatus,
): "success" | "failed" | undefined {
  if (runStatus === "completed") return "success";
  if (runStatus === "failed" || runStatus === "canceled") return "failed";
  return undefined;
}

function applyTerminalRunStatus(
  state: LiveRun,
  runStatus: LiveRunStatus,
  extra: Partial<LiveRun> = {},
): LiveRun {
  const finalToolStatus = terminalToolCallStatus(runStatus);
  const toolCalls =
    finalToolStatus !== undefined
      ? finalizeRunningToolCalls(state.toolCalls, finalToolStatus)
      : state.toolCalls;
  return {
    ...state,
    runStatus,
    ...(toolCalls !== state.toolCalls ? { toolCalls } : {}),
    ...extra,
  };
}

function shouldIgnoreStaleRunStatusSnapshot(
  current: LiveRunStatus,
  incoming: LiveRunStatus,
): boolean {
  if (!TERMINAL_RUN_STATUSES.has(current)) {
    return false;
  }
  return incoming === "idle" || incoming === "running" || incoming === "suspended";
}

function shouldIgnoreStaleRunError(state: LiveRun, event: AgUiLikeEvent): boolean {
  if (state.runStatus === "completed" || state.runStatus === "canceled") {
    return true;
  }
  const incomingRunId = eventRunId(event);
  if (
    incomingRunId &&
    state.runId &&
    incomingRunId !== state.runId &&
    (state.runStatus === "running" || state.runStatus === "failed")
  ) {
    return true;
  }
  return false;
}

export function shouldIgnoreIncomingRunError(state: LiveRun): boolean {
  return state.runStatus === "completed" || state.runStatus === "canceled";
}

/**
 * While REST-hydrating a conversation, AG-UI may replay historical events.
 * Dual-channel coordination (not a backend gap): skip run-boundary events always,
 * and skip tool events for tools that are already terminal with a finish timestamp
 * (avoids e2e duration inflation on session switch).
 */
export function shouldSkipAgUiReplayDuringRestore(
  state: LiveRun,
  event: AgUiLikeEvent,
): boolean {
  const type = stringValue(event.type);
  if (
    type === "RUN_STARTED" ||
    type === "RUN_FINISHED" ||
    type === "RUN_ERROR" ||
    type === "STATE_SNAPSHOT"
  ) {
    return true;
  }

  if (
    type !== "TOOL_CALL_START" &&
    type !== "TOOL_CALL_ARGS" &&
    type !== "TOOL_CALL_END" &&
    type !== "TOOL_CALL_RESULT"
  ) {
    return false;
  }

  const toolCallId = stringValue(event.toolCallId);
  if (!toolCallId) return false;
  const existing = state.toolCalls.find((item) => item.id === toolCallId);
  if (!existing) return false;
  const terminal = existing.status === "success" || existing.status === "failed";
  return terminal && existing.finishedAtMs !== undefined;
}

function reduceStateSnapshot(state: LiveRun, event: AgUiLikeEvent): LiveRun {
  const snapshot = recordValue(event.snapshot);
  const status = liveStatusFromValue(snapshot?.runStatus);
  const runId = eventRunId(event) ?? stringValue(snapshot?.runId) ?? stringValue(snapshot?.run_id);
  if (!status && !runId) return state;
  const nextStatus =
    status && shouldIgnoreStaleRunStatusSnapshot(state.runStatus, status) ? undefined : status;
  return {
    ...state,
    ...(nextStatus ? { runStatus: nextStatus } : {}),
    ...(runId ? { runId } : {}),
  };
}

function reduceStateDelta(state: LiveRun, event: AgUiLikeEvent): LiveRun {
  const patch = patchArray(event.delta);
  if (!patch.length) return state;

  let next = state;
  for (const op of patch) {
    if (op.path === "/runStatus") {
      const status = liveStatusFromValue(op.value);
      if (
        status &&
        !shouldIgnoreStaleRunStatusSnapshot(next.runStatus, status)
      ) {
        next =
          terminalToolCallStatus(status) !== undefined
            ? applyTerminalRunStatus(next, status)
            : { ...next, runStatus: status };
      }
    }
    if (op.path === "/runId" || op.path === "/run_id") {
      const runId = stringValue(op.value);
      if (runId) next = { ...next, runId };
    }
    if (op.path === "/errorMessage") {
      const errorMessage = stringValue(op.value);
      if (errorMessage) next = { ...next, errorMessage };
    }
  }
  return next;
}

function eventRunId(event: AgUiLikeEvent): string | undefined {
  const run = recordValue(event.run);
  return (
    stringValue(event.runId) ??
    stringValue(event.run_id) ??
    stringValue(run?.id)
  );
}

function reduceActivitySnapshot(state: LiveRun, event: AgUiLikeEvent): LiveRun {
  if (event.activityType === "PLAN") {
    const content = recordValue(event.content);
    const tasks = arrayValue(content?.tasks)
      .map(parsePlanTask)
      .filter((task): task is LivePlanTask => Boolean(task));
    return tasks.length ? { ...state, plan: tasks } : state;
  }

  if (event.activityType === "STEP") {
    const content = recordValue(event.content);
    if (!content) return state;

    const stepId = stringValue(content.step_id);
    const title = stringValue(content.title) ?? stringValue(content.tool_name) ?? "Run tool";
    const toolName = stringValue(content.tool_name);
    const kind = dataStepKindForTool(toolName);
    const activityStatus = activityStatusFromValue(content.status);
    const fallbackId =
      stepId ?? stringValue(event.messageId) ?? `step-${state.events.length + 1}`;

    const toolCall = findCorrelatedToolCall(state.toolCalls, toolName, stepId) ??
      findMisnamedToolCall(state.toolCalls, toolName);
    const eventId = toolCall?.id ?? fallbackId;

    let nextState = state;

    if (toolCall && toolName && toolCall.name !== toolName) {
      nextState = upsertToolCallRecord(nextState, { ...toolCall, name: toolName });
    }

    if (toolCall && stepId) {
      const toolUpdate: LiveToolCallRecord = { ...toolCall, stepId };
      if (activityStatus === "failed" && toolCall.status === "running") {
        const errorMessage = stringValue(content.error_message) ?? "Tool execution failed";
        toolUpdate.status = "failed";
        toolUpdate.finishedAtMs = Date.now();
        if (!toolCall.result) {
          toolUpdate.result = JSON.stringify({ error: errorMessage });
        }
      }
      nextState = upsertToolCallRecord(nextState, toolUpdate);
    }

    if (toolCall && stepId && stepId !== toolCall.id) {
      nextState = {
        ...nextState,
        events: nextState.events.filter((item) => item.id !== stepId),
      };
    }

    const existing = nextState.events.find((item) => item.id === eventId);

    return upsertTimelineEvent(nextState, {
      id: eventId,
      kind,
      toolName,
      title,
      summary: statusSummary(content.status),
      thought: thoughtForActivityStatus(activityStatus, kind),
      stepId,
      activityStatus,
      payload: mergeActivityPayload(kind, content, existing),
      ...(existing?.artifactIds ? { artifactIds: existing.artifactIds } : {}),
    });
  }

  return state;
}

function reduceActivityDelta(state: LiveRun, event: AgUiLikeEvent): LiveRun {
  if (event.activityType !== "PLAN") return state;
  const patch = patchArray(event.patch);
  if (!patch.length) return state;
  return { ...state, plan: applyPlanPatch(state.plan, patch) };
}

/**
 * When ACTIVITY_SNAPSHOT STEP arrives before TOOL_CALL_START, the timeline event
 * is keyed by step_id (no toolCall yet). On TOOL_CALL_START, re-key that orphan
 * to toolCallId and attach stepId on the tool record.
 */
function absorbOrphanActivityForToolCall(
  state: LiveRun,
  toolCallId: string,
  toolName: string,
): LiveRun {
  const toolCall = state.toolCalls.find((call) => call.id === toolCallId);
  if (!toolCall || toolCall.stepId) {
    return state;
  }

  const orphan = state.events.find(
    (event) =>
      event.id !== toolCallId &&
      event.toolName === toolName &&
      Boolean(event.stepId) &&
      !state.toolCalls.some((call) => call.id === event.id || call.stepId === event.stepId),
  );
  if (!orphan?.stepId) {
    return state;
  }

  const mergedEvent: TimelineEvent = {
    ...orphan,
    id: toolCallId,
  };
  const events = [
    ...state.events.filter((event) => event.id !== orphan.id && event.id !== toolCallId),
    mergedEvent,
  ];

  return {
    ...state,
    events,
    toolCalls: state.toolCalls.map((call) =>
      call.id === toolCallId ? { ...call, stepId: orphan.stepId } : call,
    ),
  };
}

function reduceToolEvent(state: LiveRun, event: AgUiLikeEvent): LiveRun {
  const id = stringValue(event.toolCallId) ?? `${state.events.length + 1}`;
  const existing = state.toolCalls.find((item) => item.id === id);
  let existingEvent = state.events.find((item) => item.id === id);
  const toolName = resolveIncomingToolName(event, existing, existingEvent);
  const eventType = stringValue(event.type);
  const effectiveToolName =
    toolName !== "tool" && toolName !== "unknown"
      ? toolName
      : (existingEvent?.toolName ?? existing?.name ?? toolName);
  const title = toolDisplayTitle(effectiveToolName);

  let nextState = state;
  const resultPayload =
    stringValue(event.result) ?? stringValue(event.content) ?? "";
  if (eventType === "TOOL_CALL_START") {
    nextState = upsertToolCallRecord(nextState, {
      id,
      name: effectiveToolName,
      status:
        existing?.status === "success" || existing?.status === "failed"
          ? existing.status
          : "running",
      startedAtMs: existing?.startedAtMs ?? Date.now(),
    });
    // ACTIVITY_SNAPSHOT STEP often arrives before the AG-UI tool envelope and is
    // stored under step_id. Fold that orphan into this toolCallId so Trace does
    // not keep a duplicate event-only card (especially after a later RUN_STARTED).
    nextState = absorbOrphanActivityForToolCall(nextState, id, effectiveToolName);
    existingEvent = nextState.events.find((item) => item.id === id);
  } else if (eventType === "TOOL_CALL_END") {
    const resolvedStatus =
      existing?.status === "failed"
        ? "failed"
        : existing?.status === "success" || existing?.result
          ? "success"
          : "running";
    nextState = upsertToolCallRecord(nextState, {
      id,
      name: effectiveToolName,
      status: resolvedStatus,
      startedAtMs: existing?.startedAtMs ?? Date.now(),
      ...(resolvedStatus === "success" && !existing?.finishedAtMs
        ? { finishedAtMs: Date.now() }
        : {}),
    });
  } else if (eventType === "TOOL_CALL_RESULT") {
    const parsed = parseResultObject(resultPayload);
    const failed = toolResultPayloadLooksFailed(parsed);
    nextState = upsertToolCallRecord(nextState, {
      id,
      name: effectiveToolName,
      status: failed ? "failed" : "success",
      startedAtMs: existing?.startedAtMs ?? Date.now(),
      // Dual-channel: AG-UI replay must not refresh wall-clock finish time (e2e inflation).
      // Optional later: persist tool startedAt/finishedAt on ConversationToolCallDto.
      finishedAtMs: existing?.finishedAtMs ?? Date.now(),
      ...(resultPayload ? { result: resultPayload } : {}),
    });
  }

  const kind =
    dataStepKindForTool(effectiveToolName) !== "other"
      ? dataStepKindForTool(effectiveToolName)
      : existingEvent?.kind ?? dataStepKindForTool(effectiveToolName);
  const args = recordValue(event.args) ?? recordValue(event.parameters);
  const result = resultPayload;
  const sql =
    stringValue(args?.sql) ??
    stringValue(event.delta) ??
    stringValue(parseResultObject(result)?.sql) ??
    "";

  if (kind === "query") {
    const existing = nextState.events.find((item) => item.id === id) ?? existingEvent;
    const parsed = parseResultObject(result);
    const toolCall = nextState.toolCalls.find((item) => item.id === id);
    const failed =
      toolResultPayloadLooksFailed(parsed) ||
      toolCall?.status === "failed" ||
      existingEvent?.activityStatus === "failed" ||
      existing?.activityStatus === "failed";
    const rowCount = failed ? (numberValue(parsed?.row_count) ?? 0) : (numberValue(parsed?.row_count) ?? 0);
    const elapsedMs = failed ? (numberValue(parsed?.elapsed_ms) ?? 0) : (numberValue(parsed?.elapsed_ms) ?? 0);
    const existingPayload = existing?.payload as
      | { scannedRows?: number; durationMs?: number }
      | undefined;
    return finalizeToolEventState(
      upsertTimelineEvent(nextState, {
        id,
        kind,
        toolName: effectiveToolName,
        title,
        summary: summarizeSqlResult(result, parsed, event.type),
        thought: "Agent converts the natural language question into read-only SQL and runs it through the backend Data Gateway.",
        payload: {
          question: "",
          sql: sql || extractSql(existing),
          scannedRows: rowCount || existingPayload?.scannedRows || 0,
          durationMs: elapsedMs || existingPayload?.durationMs || 0,
        },
        ...(existing?.artifactIds ? { artifactIds: existing.artifactIds } : {}),
        ...(existing?.stepId ? { stepId: existing.stepId } : {}),
        ...(existing?.activityStatus ? { activityStatus: existing.activityStatus } : {}),
      }),
      eventType,
    );
  }

  if (kind === "inspect") {
    const parsedSchema = parseResultObject(result);
    const tablesFromResult = parseSchemaTables(parsedSchema);
    const existing = nextState.events.find((item) => item.id === id) ?? existingEvent;
    const existingTables =
      existing?.kind === "inspect"
        ? ((existing.payload as { tables?: SchemaTable[] } | undefined)?.tables ?? [])
        : [];
    const tables = tablesFromResult.length > 0 ? tablesFromResult : existingTables;
    return finalizeToolEventState(
      upsertTimelineEvent(nextState, {
        id,
        kind,
        toolName: effectiveToolName,
        title,
        summary: summarizeSchemaResult(result, tables, event.type),
        thought: "Agent checks the data source structure before drawing conclusions from uncertain fields.",
        payload: { tables },
        ...(existing?.artifactIds ? { artifactIds: existing.artifactIds } : {}),
        ...(existing?.stepId ? { stepId: existing.stepId } : {}),
        ...(existing?.activityStatus ? { activityStatus: existing.activityStatus } : {}),
      }),
      eventType,
    );
  }

  return finalizeToolEventState(
    upsertTimelineEvent(nextState, {
      id,
      kind,
      toolName: effectiveToolName,
      title,
      summary: summarizeGenericResult(result, event.type),
      thought: "Agent is running a data operation.",
      payload: { description: result || "", rawResult: result || undefined },
      ...(existingEvent?.artifactIds ? { artifactIds: existingEvent.artifactIds } : {}),
      ...(existingEvent?.stepId ? { stepId: existingEvent.stepId } : {}),
      ...(existingEvent?.activityStatus ? { activityStatus: existingEvent.activityStatus } : {}),
    }),
    eventType,
  );
}

function finalizeToolEventState(
  state: LiveRun,
  eventType?: string,
): LiveRun {
  if (eventType === "TOOL_CALL_RESULT") {
    return reconcileUnlinkedArtifacts(state);
  }
  return state;
}

function summarizeGenericResult(result: string, eventType?: string): string {
  if (result) return result.length > 160 ? `${result.slice(0, 160)}…` : result;
  if (eventType === "TOOL_CALL_RESULT" || eventType === "TOOL_CALL_END") {
    return "Data operation completed.";
  }
  return "Running data operation.";
}

function parseResultObject(result: string): Record<string, unknown> | null {
  return parseToolResultRecord(result);
}

function toolResultPayloadLooksFailed(parsed: Record<string, unknown> | null): boolean {
  if (!parsed) return false;
  if (parsed.status === "error") return true;
  return parsed.error !== undefined;
}

function summarizeSqlResult(
  result: string,
  parsed: Record<string, unknown> | null,
  eventType?: string,
): string {
  const rowCount = numberValue(parsed?.row_count);
  if (rowCount !== undefined) return `Executed and returned ${rowCount} rows.`;
  // Non-JSON results are usually human-readable text (or an error); keep them.
  if (result && !parsed) return result;
  if (eventType === "TOOL_CALL_RESULT") return "SQL executed.";
  return "Preparing a read-only SQL query.";
}

function summarizeSchemaResult(
  result: string,
  tables: SchemaTable[],
  eventType?: string,
): string {
  if (tables.length > 0) return `Inspected ${tables.length} tables.`;
  if (result && !parseResultObject(result)) return result;
  if (eventType === "TOOL_CALL_RESULT") return "Inspected data source schema.";
  return "Calling backend data tool.";
}

function parseSchemaTables(parsed: Record<string, unknown> | null): SchemaTable[] {
  const rawTables = arrayValue(parsed?.tables);
  const tables: SchemaTable[] = [];
  for (const rawTable of rawTables) {
    const table = recordValue(rawTable);
    const name = stringValue(table?.name);
    if (!name) continue;
    const fields = arrayValue(table?.columns)
      .map((rawColumn) => {
        const column = recordValue(rawColumn);
        const columnName = stringValue(column?.name);
        if (!columnName) return undefined;
        const type = stringValue(column?.type);
        return type ? `${columnName} · ${type}` : columnName;
      })
      .filter((field): field is string => Boolean(field));
    tables.push({ name, description: "", fields });
  }
  return tables;
}

function patchTokenUsageCorrelations(
  events: LiveTokenUsageRecord[],
  correlation: { stepId: string; toolCallId: string },
): LiveTokenUsageRecord[] {
  return events.map((record) =>
    record.toolCallId === correlation.toolCallId
      ? { ...record, stepId: correlation.stepId }
      : record,
  );
}

function reduceCustomEvent(state: LiveRun, event: AgUiLikeEvent): LiveRun {
  if (event.name === "sql_audit") {
    const value = recordValue(event.value);
    const audit: LiveAudit = {
      id: stringValue(value?.audit_log_id) ?? `audit-${state.audits.length + 1}`,
      datasourceId: stringValue(value?.datasource_id),
      status: stringValue(value?.status),
      rowCount: numberValue(value?.row_count),
      elapsedMs: numberValue(value?.elapsed_ms),
    };
    return { ...state, audits: [...state.audits, audit] };
  }

  if (event.name === "artifact") {
    const value = recordValue(event.value);
    if (!value) return state;

    const artifact = parseArtifactFromCustom(value);
    const linkedTool = findArtifactSourceTool(state, artifact, value);
    if (shouldDropIncomingDuplicateArtifact(state.artifacts, artifact)) {
      return state;
    }
    // Keep the first recorded time on upsert so Outputs sort stays stable across versions.
    const previous = state.artifacts.find((item) => item.id === artifact.id);
    const nextArtifact =
      previous?.recordedAtMs !== undefined
        ? { ...artifact, recordedAtMs: previous.recordedAtMs }
        : artifact;
    let nextState: LiveRun = {
      ...state,
      artifacts: [
        ...state.artifacts.filter((item) =>
          item.id !== nextArtifact.id && !isDuplicateReportForFileArtifact(nextArtifact, item),
        ),
        nextArtifact,
      ],
    };
    if (linkedTool) {
      nextState = linkArtifactToToolCall(nextState, artifact.id, linkedTool.id);
    }
    return reconcileUnlinkedArtifacts(nextState);
  }

  if (event.name === "token_usage.correlation") {
    const value = recordValue(event.value);
    const stepId = stringValue(value?.step_id);
    const toolCallId = stringValue(value?.tool_call_id);
    if (!stepId || !toolCallId) return state;
    return {
      ...state,
      tokenUsageEvents: patchTokenUsageCorrelations(state.tokenUsageEvents, {
        stepId,
        toolCallId,
      }),
    };
  }

  if (event.name === "token_usage") {
    const value = recordValue(event.value);
    const delta: LiveTokenUsageRecord = {
      inputTokens:
        numberValue(value?.input_tokens) ??
        numberValue(value?.prompt_tokens) ??
        0,
      outputTokens:
        numberValue(value?.output_tokens) ??
        numberValue(value?.completion_tokens) ??
        0,
      ...(numberValue(value?.cost_usd) !== undefined
        ? { costUsd: numberValue(value?.cost_usd) }
        : {}),
      ...(numberValue(value?.step_number) !== undefined
        ? { stepNumber: numberValue(value?.step_number) }
        : {}),
      ...(stringValue(value?.step_id)
        ? { stepId: stringValue(value?.step_id) }
        : {}),
      ...(stringValue(value?.tool_name) ? { toolName: stringValue(value?.tool_name) } : {}),
      ...(stringValue(value?.tool_call_id)
        ? { toolCallId: stringValue(value?.tool_call_id) }
        : {}),
      ...(stringValue(value?.model) ? { model: stringValue(value?.model) } : {}),
    };
    const deltaKey = tokenUsageRecordKey(delta);
    if (state.tokenUsageEvents.some((record) => tokenUsageRecordKey(record) === deltaKey)) {
      return state;
    }
    return {
      ...state,
      tokenUsage: mergeTokenUsageStats(state.tokenUsage ?? emptyTokenUsageStats(), delta),
      tokenUsageEvents: [...state.tokenUsageEvents, delta],
    };
  }

  if (event.name === "workspace.metadata") {
    const value = recordValue(event.value);
    return {
      ...state,
      workspaceMetadata: [
        {
          toolCallId: stringValue(value?.toolCallId),
          toolName: stringValue(value?.toolName),
          receivedAt: Date.now(),
          payload: value ?? event.value,
        },
        ...state.workspaceMetadata,
      ],
    };
  }

  if (event.name === "sandbox.output") {
    const value = recordValue(event.value);
    return {
      ...state,
      sandboxOutputs: [
        {
          kind: stringValue(value?.kind) ?? "output",
          receivedAt: Date.now(),
          payload: value ?? event.value,
        },
        ...state.sandboxOutputs,
      ],
    };
  }

  if (event.name === "skill.selection") {
    return {
      ...state,
      skillSelection: parseSkillSelection(event.value),
    };
  }

  if (event.name === "goal.updated") {
    return {
      ...state,
      goal: parseGoalSnapshot(event.value),
    };
  }

  if (event.name === "run.config.resolved") {
    return {
      ...state,
      resolvedRunConfig: parseResolvedRunConfig(event.value),
    };
  }

  if (event.name === "session.title") {
    const value = recordValue(event.value);
    const sessionId =
      stringValue(value?.session_id) ??
      stringValue(value?.sessionId) ??
      stringValue(value?.thread_id) ??
      stringValue(value?.threadId);
    const title = stringValue(value?.title);
    if (!sessionId || !title) return state;
    return {
      ...state,
      sessionTitle: { sessionId, title },
    };
  }

  if (event.name === "context.compiled" || event.name === "context.prompt-verified") {
    const name: LiveContextReport["name"] = event.name;
    return {
      ...state,
      contextReports: [
        {
          name,
          receivedAt: Date.now(),
          value: event.value,
        },
        ...state.contextReports,
      ].slice(0, 8),
    };
  }

  if (event.name === "interaction.requested" || event.name === "interaction.resolved") {
    const value = recordValue(event.value);
    const toolCallId = stringValue(value?.tool_call_id);
    const toolName = stringValue(value?.tool_name);
    if (toolCallId && toolName) {
      return applyInteractionToolIdentity(state, toolCallId, toolName);
    }
  }

  return state;
}

function parseSkillSelection(value: unknown): LiveSkillSelection {
  const record = recordValue(value);
  const selected = arrayValue(record?.selected)
    .map((item) => {
      const skill = recordValue(item);
      const id = stringValue(skill?.id);
      if (!id) return undefined;
      return {
        id,
        ...(stringValue(skill?.name) ? { name: stringValue(skill?.name) } : {}),
        ...(numberValue(skill?.revision) !== undefined
          ? { revision: numberValue(skill?.revision) }
          : {}),
        ...(stringArrayValue(skill?.tags).length > 0
          ? { tags: stringArrayValue(skill?.tags) }
          : {}),
      };
    })
    .filter((item): item is LiveSkillSelection["selected"][number] => Boolean(item));
  return {
    ...(stringValue(record?.mode) ? { mode: stringValue(record?.mode) } : {}),
    selected,
    ...(recordValue(record?.effective_tool_policy)
      ? { effectiveToolPolicy: recordValue(record?.effective_tool_policy) ?? undefined }
      : recordValue(record?.effectiveToolPolicy)
        ? { effectiveToolPolicy: recordValue(record?.effectiveToolPolicy) ?? undefined }
        : {}),
    audit: arrayValue(record?.audit),
    raw: value,
  };
}

function parseGoalSnapshot(value: unknown): LiveGoalSnapshot {
  const record = recordValue(value);
  return {
    ...(stringValue(record?.objective) ? { objective: stringValue(record?.objective) } : {}),
    ...(stringValue(record?.status) ? { status: stringValue(record?.status) } : {}),
    ...(stringValue(record?.source) ? { source: stringValue(record?.source) } : {}),
    raw: value,
  };
}

function parseResolvedRunConfig(value: unknown): LiveResolvedRunConfig {
  const record = recordValue(value);
  const activeDatasourceId = firstString(record, [
    "activeDatasourceId",
    "active_datasource_id",
  ]);
  const activeLlmProfileId = firstString(record, [
    "activeLlmProfileId",
    "requested_llm_profile_id",
  ]);
  const enabledDatasourceIds = firstStringArray(record, [
    "enabledDatasourceIds",
    "enabled_datasource_ids",
  ]);
  const enabledKnowledgeIds = firstStringArray(record, [
    "enabledKnowledgeIds",
    "enabled_knowledge_ids",
  ]);
  const enabledMcpServerIds = firstStringArray(record, [
    "enabledMcpServerIds",
    "enabled_mcp_server_ids",
  ]);
  const enabledSkillIds = firstStringArray(record, [
    "enabledSkillIds",
    "enabled_skill_ids",
    "selected_skill_ids",
  ]);
  const fileIds = firstStringArray(record, ["fileIds", "file_ids"]);
  const evidenceRefs = parseEvidenceRefs(record?.evidenceRefs ?? record?.evidence_refs);
  const evidenceResolution = parseEvidenceResolution(
    record?.evidenceResolution ?? record?.evidence_resolution,
  );
  const selectedSkills = parseSelectedSkills(record?.selectedSkills);
  const resolvedSelectedSkills =
    selectedSkills.length > 0
      ? selectedSkills
      : enabledSkillIds.map((id) => ({ id }));

  return {
    ...(activeDatasourceId ? { activeDatasourceId } : {}),
    ...(activeLlmProfileId ? { activeLlmProfileId } : {}),
    ...(enabledDatasourceIds.length > 0 ? { enabledDatasourceIds } : {}),
    ...(enabledKnowledgeIds.length > 0 ? { enabledKnowledgeIds } : {}),
    ...(enabledMcpServerIds.length > 0 ? { enabledMcpServerIds } : {}),
    ...(enabledSkillIds.length > 0 ? { enabledSkillIds } : {}),
    ...(fileIds.length > 0 ? { fileIds } : {}),
    ...(evidenceRefs.length > 0 ? { evidenceRefs } : {}),
    ...(evidenceResolution ? { evidenceResolution } : {}),
    ...(resolvedSelectedSkills.length > 0 ? { selectedSkills: resolvedSelectedSkills } : {}),
    raw: value,
  };
}

function parseEvidenceRefs(value: unknown): EvidenceRef[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is EvidenceRef => {
    const record = recordValue(item);
    return Boolean(
      record &&
        typeof record.id === "string" &&
        typeof record.kind === "string" &&
        typeof record.label === "string" &&
        typeof record.sessionId === "string",
    );
  });
}

function parseEvidenceResolution(value: unknown): EvidenceResolutionDiagnostics | undefined {
  const record = recordValue(value);
  if (!record) return undefined;
  const accepted = firstStringArray(record, ["accepted"]);
  const dropped = Array.isArray(record.dropped)
    ? record.dropped.filter((item): item is EvidenceResolutionIssue => {
        const issue = recordValue(item);
        return Boolean(issue && typeof issue.id === "string" && typeof issue.reason === "string");
      })
    : [];
  return { accepted, dropped };
}

function firstString(
  record: Record<string, unknown> | null,
  keys: string[],
): string | undefined {
  if (!record) return undefined;
  for (const key of keys) {
    const value = stringValue(record[key]);
    if (value) return value;
  }
  return undefined;
}

function firstStringArray(
  record: Record<string, unknown> | null,
  keys: string[],
): string[] {
  if (!record) return [];
  for (const key of keys) {
    if (!(key in record)) continue;
    return stringArrayValue(record[key]);
  }
  return [];
}

function parseSelectedSkills(value: unknown): Array<{ id: string; name?: string }> {
  return arrayValue(value)
    .map((item) => {
      const skill = recordValue(item);
      const id = stringValue(skill?.id);
      if (!id) return undefined;
      return {
        id,
        ...(stringValue(skill?.name) ? { name: stringValue(skill?.name) } : {}),
      };
    })
    .filter((item): item is { id: string; name?: string } => Boolean(item));
}

export function parseCsvTextPreview(
  text: string,
  delimiter = ",",
): { columns: string[]; rows: string[][] } | null {
  const lines = text.trim().split(/\r?\n/u).filter((line) => line.length > 0);
  if (lines.length < 2) return null;
  if (!lines[0].includes(delimiter) || lines[0].trimStart().startsWith("#")) {
    return null;
  }

  const parseLine = (line: string): string[] => {
    const cells: string[] = [];
    let current = "";
    let inQuotes = false;
    for (let index = 0; index < line.length; index += 1) {
      const char = line[index];
      if (char === "\"") {
        if (inQuotes && line[index + 1] === "\"") {
          current += "\"";
          index += 1;
        } else {
          inQuotes = !inQuotes;
        }
        continue;
      }
      if (char === delimiter && !inQuotes) {
        cells.push(current);
        current = "";
        continue;
      }
      current += char;
    }
    cells.push(current);
    return cells;
  };

  const columns = parseLine(lines[0]).map((column) => column.trim());
  if (columns.length < 2 || columns.every((column) => column.length === 0)) {
    return null;
  }

  const rows = lines
    .slice(1)
    .map((line) => parseLine(line).map((cell) => cell.trim()))
    .filter((row) => row.some((cell) => cell.length > 0));

  if (rows.length === 0) return null;
  return { columns, rows };
}

function parseTablePreview(
  preview: unknown,
): { columns: string[]; rows: string[][] } | null {
  if (typeof preview === "string") {
    return parseCsvTextPreview(preview);
  }

  const record = recordValue(preview);
  if (!record) return null;

  const columns = arrayValue(record.columns)
    .map((column) => (typeof column === "string" ? column : undefined))
    .filter((column): column is string => Boolean(column));
  const rawRows = arrayValue(record.rows);
  if (columns.length === 0 || rawRows.length === 0) return null;

  const rows = rawRows
    .map((row) => {
      if (Array.isArray(row)) {
        return row.map((cell) =>
          cell === null || cell === undefined ? "" : String(cell),
        );
      }
      const objectRow = recordValue(row);
      if (!objectRow) return [];
      return columns.map((column) => {
        const value = objectRow[column];
        return value === null || value === undefined ? "" : String(value);
      });
    })
    .filter((row) => row.length > 0);

  if (rows.length === 0) return null;
  return { columns, rows };
}

function parseChartPreview(preview: unknown): Extract<ArtifactDetail, { type: "chart" }> | null {
  const record = recordValue(preview);
  if (!record) return null;

  const points = parseChartPoints(record.points);
  const series = arrayValue(record.series)
    .map((item) => {
      const seriesRecord = recordValue(item);
      const name = stringValue(seriesRecord?.name);
      const seriesPoints = parseChartPoints(seriesRecord?.points);
      if (!name || seriesPoints.length === 0) return undefined;
      return { name, points: seriesPoints };
    })
    .filter((item): item is { name: string; points: Array<{ label: string; value: number }> } =>
      Boolean(item),
    );

  if (points.length === 0 && series.length === 0) return null;

  const chartType = chartTypeValue(record.chartType ?? record.chart_type ?? record.kind);
  return {
    type: "chart",
    ...(chartType ? { chartType } : {}),
    ...(stringValue(record.unit) ? { unit: stringValue(record.unit) } : {}),
    points,
    ...(series.length > 0 ? { series } : {}),
  };
}

function parseChartPoints(value: unknown): Array<{ label: string; value: number }> {
  return arrayValue(value)
    .map((item) => {
      const point = recordValue(item);
      const label =
        stringValue(point?.label) ??
        stringValue(point?.x) ??
        stringValue(point?.name);
      const valueNumber =
        numberValue(point?.value) ??
        numberValue(point?.y) ??
        numberValue(point?.count);
      if (!label || valueNumber === undefined) return undefined;
      return { label, value: valueNumber };
    })
    .filter((item): item is { label: string; value: number } => Boolean(item));
}

function previewPayload(value: unknown): unknown {
  const record = recordValue(value);
  return recordValue(record?.preview_json) ?? recordValue(record?.preview) ?? value;
}

function chartTypeValue(value: unknown): Extract<ArtifactDetail, { type: "chart" }>["chartType"] {
  if (value === "bar" || value === "line" || value === "pie") return value;
  return undefined;
}

export function artifactDetailNeedsPreviewFetch(
  artifact: Pick<DataArtifact, "previewAvailable" | "fileId" | "type">,
  detail: ArtifactDetail | undefined,
): boolean {
  const fileBacked = Boolean(artifact.fileId);
  if (!artifact.previewAvailable && !fileBacked) return false;
  if (!detail) return true;
  if (detail.type === "file" && !detail.content) return true;
  if (detail.type === "report") {
    return !detail.sections.some((section) => section.body.trim().length > 0);
  }
  return false;
}

export function mergeArtifactDetail(
  existing: ArtifactDetail | undefined,
  loaded: ArtifactDetail,
): ArtifactDetail {
  if (!existing) return loaded;
  if (existing.type === "file" && loaded.type === "file") {
    return {
      ...loaded,
      path: loaded.path || existing.path,
      size: loaded.size ?? existing.size,
      mtime: loaded.mtime ?? existing.mtime,
      tool: loaded.tool ?? existing.tool,
      content: loaded.content ?? existing.content,
    };
  }
  if (existing.type === "report" && loaded.type === "report") {
    const hasExistingBody = existing.sections.some(
      (section) => section.body.trim().length > 0,
    );
    return hasExistingBody ? existing : loaded;
  }
  return loaded;
}

/** Builds inline artifact detail from REST preview JSON or stream preview_json. */
export function artifactDetailFromPreview(
  artifact: Pick<DataArtifact, "type" | "kind" | "title">,
  preview: unknown,
): ArtifactDetail | undefined {
  const previewRecord = recordValue(preview);
  const payload = previewPayload(preview);
  const payloadRecord = recordValue(payload);
  const backendType =
    stringValue(previewRecord?.type) ??
    (artifact.type === "dataset"
      ? "table"
      : artifact.type === "file"
        ? "file"
        : artifact.type === "report"
          ? "markdown"
          : artifact.type);

  const tablePreview = parseTablePreview(payload);
  if (backendType === "table" || tablePreview) {
    if (!tablePreview) return undefined;
    return {
      type: "dataset",
      columns: tablePreview.columns,
      rows: tablePreview.rows,
    };
  }

  if (backendType === "chart") {
    return parseChartPreview(payload) ?? undefined;
  }

  if (backendType === "file") {
    const filePath = stringValue(payloadRecord?.path) ?? artifact.title;
    const fileSize = numberValue(payloadRecord?.size);
    const fileMtime = stringValue(payloadRecord?.mtime);
    const fileTool = stringValue(payloadRecord?.tool);
    const fileContent = stringValue(payloadRecord?.content);
    return {
      type: "file",
      path: filePath,
      ...(fileSize !== undefined ? { size: fileSize } : {}),
      ...(fileMtime ? { mtime: fileMtime } : {}),
      ...(fileTool ? { tool: fileTool } : {}),
      ...(fileContent ? { content: fileContent } : {}),
    };
  }

  const textContent =
    stringValue(payloadRecord?.content) ??
    stringValue(payloadRecord?.body) ??
    (typeof payload === "string" && backendType !== "table" ? payload : undefined);
  if (textContent) {
    return {
      type: "report",
      sections: [{ heading: "Preview", body: textContent }],
    };
  }

  if (typeof payload === "string" && (backendType === "table" || artifact.type === "dataset")) {
    const tablePreview = parseCsvTextPreview(payload);
    if (tablePreview) {
      return {
        type: "dataset",
        columns: tablePreview.columns,
        rows: tablePreview.rows,
      };
    }
  }

  return undefined;
}

function parseArtifactFromCustom(value: Record<string, unknown>): DataArtifact {
  const id =
    stringValue(value.id) ??
    stringValue(value.artifact_id) ??
    `artifact-${Date.now()}`;
  const title =
    stringValue(value.title) ?? stringValue(value.name) ?? "Agent output";
  const backendType = stringValue(value.type);
  const fileId = stringValue(value.file_id) ?? stringValue(value.fileId);
  const downloadUrl =
    stringValue(value.download_url) ?? stringValue(value.downloadUrl);
  const preview = value.preview_json;
  const previewRecord = recordValue(preview);
  const sourcePath = stringValue(previewRecord?.path);
  const rowCount = numberValue(previewRecord?.row_count);

  let type: DataArtifact["type"];
  let kind: DataArtifact["kind"] = "memo";
  let detail: DataArtifact["detail"];
  let summary = stringValue(value.summary) ?? stringValue(value.description);

  const tablePreview = parseTablePreview(preview);
  if (backendType === "table" || tablePreview) {
    type = "dataset";
    kind = "csv";
    if (tablePreview) {
      detail = {
        type: "dataset",
        columns: tablePreview.columns,
        rows: tablePreview.rows,
      };
      summary =
        summary ??
        `${tablePreview.rows.length.toLocaleString()} rows x ${tablePreview.columns.length} columns`;
    } else if (rowCount !== undefined) {
      summary = summary ?? `Dataset, ${rowCount.toLocaleString()} rows`;
    }
  } else if (backendType === "chart") {
    type = "chart";
    kind = "chart";
    detail = parseChartPreview(preview) ?? undefined;
    summary = summary ?? "Chart output";
  } else if (backendType === "markdown" || backendType === "html") {
    type = "report";
    kind = "memo";
    const reportContent =
      stringValue(previewRecord?.content) ??
      stringValue(previewRecord?.body) ??
      (typeof preview === "string" ? preview : undefined);
    if (reportContent) {
      detail = {
        type: "report",
        sections: [{ heading: "Preview", body: reportContent }],
      };
    }
    summary = summary ?? `${backendType} report`;
  } else if (backendType === "file") {
    type = "file";
    kind = "file";
    const filePath = stringValue(previewRecord?.path) ?? title;
    const fileSize = numberValue(previewRecord?.size);
    const fileMtime = stringValue(previewRecord?.mtime);
    const fileTool = stringValue(previewRecord?.tool);
    const fileContent = stringValue(previewRecord?.content);
    detail = {
      type: "file",
      path: filePath,
      ...(fileSize !== undefined ? { size: fileSize } : {}),
      ...(fileMtime ? { mtime: fileMtime } : {}),
      ...(fileTool ? { tool: fileTool } : {}),
      ...(fileContent ? { content: fileContent } : {}),
    };
    summary =
      summary ??
      (fileSize !== undefined
        ? `File ${filePath}（${fileSize.toLocaleString()} bytes）`
        : `File ${filePath}`);
  }

  const versionNumber = numberValue(value.version);
  const versionLabel =
    stringValue(value.version) ??
    (versionNumber !== undefined ? `v${versionNumber}` : "v1");
  const createdByToolCallId = stringValue(value.tool_call_id);
  const createdAtMs = timestampMsFromUnknown(
    value.created_at ?? value.createdAt ?? value.recorded_at_ms ?? value.recordedAtMs,
  );

  return {
    id,
    title,
    kind,
    type,
    summary: summary ?? "Output returned by the backend through an AG-UI artifact event.",
    version: versionLabel,
    ...(fileId ? { fileId } : {}),
    ...(downloadUrl ? { downloadUrl } : {}),
    ...(sourcePath ? { sourcePath } : {}),
    ...(createdByToolCallId ? { createdByToolCallId } : {}),
    detail,
    previewAvailable:
      value.preview_available === true ||
      Boolean(fileId) ||
      (preview !== undefined && preview !== null),
    recordedAtMs: createdAtMs ?? Date.now(),
    ...(stringValue(value.logical_key) ? { logicalKey: stringValue(value.logical_key) } : {}),
    ...(versionNumber !== undefined ? { versionCount: versionNumber } : {})
  };
}

function timestampMsFromUnknown(value: unknown): number | undefined {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === "string" && value.length > 0) {
    const parsed = Date.parse(value);
    return Number.isFinite(parsed) ? parsed : undefined;
  }
  return undefined;
}

function shouldDropIncomingDuplicateArtifact(
  existing: DataArtifact[],
  incoming: DataArtifact,
): boolean {
  if (incoming.type !== "report") return false;
  const incomingPath = artifactSourcePath(incoming);
  if (!incomingPath) return false;
  return existing.some((artifact) =>
    artifact.type === "file" &&
    artifactSourcePathMatches(artifact, incomingPath),
  );
}

function isDuplicateReportForFileArtifact(
  incoming: DataArtifact,
  existing: DataArtifact,
): boolean {
  if (incoming.type !== "file" || existing.type !== "report") return false;
  const incomingPath = artifactSourcePath(incoming);
  if (!incomingPath) return false;
  return artifactSourcePathMatches(existing, incomingPath);
}

function artifactSourcePathMatches(
  artifact: DataArtifact,
  sourcePath: string,
): boolean {
  const artifactPath = artifactSourcePath(artifact);
  return Boolean(
    artifactPath &&
      normalizeWorkspacePath(artifactPath) === normalizeWorkspacePath(sourcePath),
  );
}

function artifactSourcePath(artifact: DataArtifact): string | undefined {
  if (artifact.sourcePath) return artifact.sourcePath;
  if (artifact.detail?.type === "file") return artifact.detail.path;
  return undefined;
}

function extractAuditIdFromArtifactValue(value: Record<string, unknown>): string | undefined {
  const preview = recordValue(value.preview_json);
  const fromPreview = stringValue(preview?.audit_log_id);
  if (fromPreview) return fromPreview;
  const name = stringValue(value.name) ?? stringValue(value.title);
  const match = name?.match(/^SQL result\s+(.+)$/i);
  return match?.[1];
}

function extractAuditIdFromToolResult(result?: string): string | undefined {
  const parsed = parseResultObject(result ?? "");
  return stringValue(parsed?.audit_log_id);
}

function isSqlToolCall(state: LiveRun, tool: LiveToolCallRecord): boolean {
  return (
    tool.name === "run_sql_readonly" ||
    state.events.some((event) => event.id === tool.id && event.kind === "query")
  );
}

function toolCallHasLinkedArtifacts(state: LiveRun, toolCallId: string): boolean {
  const event = state.events.find((item) => item.id === toolCallId);
  return (event?.artifactIds?.length ?? 0) > 0;
}

function unlinkedSuccessfulSqlTools(state: LiveRun): LiveToolCallRecord[] {
  return state.toolCalls.filter((tool) => {
    if (!isSqlToolCall(state, tool)) return false;
    if (tool.status === "failed") return false;
    return !toolCallHasLinkedArtifacts(state, tool.id);
  });
}

function findSqlToolForArtifact(
  state: LiveRun,
  artifactValue?: Record<string, unknown>,
): LiveToolCallRecord | undefined {
  const auditId = artifactValue ? extractAuditIdFromArtifactValue(artifactValue) : undefined;
  const preview = artifactValue ? recordValue(artifactValue.preview_json) : undefined;
  const artifactRowCount = numberValue(preview?.row_count);

  if (auditId) {
    for (let index = state.toolCalls.length - 1; index >= 0; index -= 1) {
      const tool = state.toolCalls[index];
      if (!isSqlToolCall(state, tool)) continue;
      if (extractAuditIdFromToolResult(tool.result) === auditId) return tool;
    }
  }

  const unlinked = unlinkedSuccessfulSqlTools(state);

  if (artifactRowCount !== undefined) {
    const rowMatch = unlinked.find((tool) => {
      if (!tool.result) return false;
      const parsed = parseResultObject(tool.result);
      return numberValue(parsed?.row_count) === artifactRowCount;
    });
    if (rowMatch) return rowMatch;
  }

  // Artifacts usually arrive after sequential tool calls; link to the oldest
  // successful SQL call that still has no linked artifact.
  return unlinked[0];
}

const FILE_ARTIFACT_TOOLS = new Set([
  "write_file",
  "edit_file",
  "execute_command",
  "publish_artifact",
  "promote_workspace_file",
]);

type FileArtifactMeta = {
  path?: string;
  tool?: string;
};

function extractFileArtifactMeta(value: Record<string, unknown>): FileArtifactMeta {
  const preview = recordValue(value.preview_json);
  return {
    path: stringValue(preview?.path) ?? stringValue(value.name) ?? stringValue(value.title),
    tool: stringValue(preview?.tool),
  };
}

function fileArtifactMetaFromDataArtifact(artifact: DataArtifact): FileArtifactMeta {
  if (artifact.detail?.type === "file") {
    return {
      path: artifact.detail.path,
      tool: artifact.detail.tool,
    };
  }
  return { path: artifact.title };
}

function normalizeWorkspacePath(relativePath: string): string {
  return relativePath.replace(/\\/gu, "/").replace(/^\.\/+/, "");
}

function extractObservationFromToolResult(result?: string): string | undefined {
  if (!result) return undefined;
  const parsed = parseResultObject(result);
  const observation = stringValue(parsed?.observation);
  if (observation) return observation;
  const trimmed = result.trim();
  if (trimmed.startsWith("{")) return undefined;
  return trimmed;
}

function extractWorkspacePathFromToolResult(result?: string): string | undefined {
  const observation = extractObservationFromToolResult(result);
  if (!observation) return undefined;

  const firstLine = observation.split("\n")[0]?.trim() ?? observation.trim();
  const wroteMatch = /^Wrote \d+ bytes to (.+)$/u.exec(firstLine);
  if (wroteMatch?.[1]) return wroteMatch[1].trim();

  const replacedMatch = /^Replaced \d+ occurrence(?:s)? in (.+)$/u.exec(firstLine);
  if (replacedMatch?.[1]) {
    return replacedMatch[1].replace(/\s+\(lines [^)]+\)$/u, "").trim();
  }

  return undefined;
}

function workspaceToolMatchesFileArtifact(
  tool: LiveToolCallRecord,
  meta: FileArtifactMeta,
): boolean {
  if (meta.tool && tool.name !== meta.tool) return false;
  if (!meta.path) return true;

  const resultPath = extractWorkspacePathFromToolResult(tool.result);
  if (!resultPath) return true;
  return normalizeWorkspacePath(resultPath) === normalizeWorkspacePath(meta.path);
}

function findFileToolForArtifact(
  state: LiveRun,
  artifactValue?: Record<string, unknown>,
): LiveToolCallRecord | undefined {
  const meta = artifactValue ? extractFileArtifactMeta(artifactValue) : undefined;
  return findFileToolForArtifactByMeta(state, meta ?? {});
}

function findFileToolForArtifactByMeta(
  state: LiveRun,
  meta: FileArtifactMeta,
): LiveToolCallRecord | undefined {
  const candidates = state.toolCalls.filter(
    (tool) =>
      FILE_ARTIFACT_TOOLS.has(tool.name) &&
      tool.status === "success" &&
      workspaceToolMatchesFileArtifact(tool, meta),
  );

  const unlinked = candidates.filter((tool) => !toolCallHasLinkedArtifacts(state, tool.id));
  if (unlinked.length > 0) return unlinked[0];

  if (meta.path && meta.tool && candidates.length === 1) return candidates[0];
  return undefined;
}

function artifactValueFromDataArtifact(artifact: DataArtifact): Record<string, unknown> {
  const value: Record<string, unknown> = {
    id: artifact.id,
    title: artifact.title,
    name: artifact.title,
    summary: artifact.summary,
    type: artifact.type ?? artifact.kind,
  };
  if (artifact.fileId) value.file_id = artifact.fileId;
  if (artifact.downloadUrl) value.download_url = artifact.downloadUrl;
  if (artifact.createdByToolCallId) value.tool_call_id = artifact.createdByToolCallId;

  if (artifact.detail?.type === "dataset") {
    value.type = "table";
    value.preview_json = {
      columns: artifact.detail.columns,
      rows: artifact.detail.rows,
      row_count: artifact.detail.rows?.length ?? 0,
    };
  } else if (artifact.detail?.type === "file") {
    value.type = "file";
    value.preview_json = {
      path: artifact.detail.path,
      size: artifact.detail.size,
      tool: artifact.detail.tool,
    };
  }

  return value;
}

/** Re-link artifacts missing `createdByEventId` after restore or out-of-order events. */
export function reconcileLiveRunArtifacts(state: LiveRun): LiveRun {
  let nextState = state;
  for (const artifact of state.artifacts) {
    if (artifact.createdByEventId) continue;
    const linkedTool = findArtifactSourceTool(nextState, artifact);
    if (linkedTool) {
      nextState = linkArtifactToToolCall(nextState, artifact.id, linkedTool.id);
    }
  }
  return nextState;
}

function reconcileUnlinkedArtifacts(state: LiveRun): LiveRun {
  return reconcileLiveRunArtifacts(state);
}

function findArtifactSourceTool(
  state: LiveRun,
  artifact: DataArtifact,
  artifactValue?: Record<string, unknown>,
): LiveToolCallRecord | undefined {
  const value = artifactValue ?? artifactValueFromDataArtifact(artifact);

  // R-018 / session-output: authoritative tool_call_id is the sole source of truth.
  // Prefer DataArtifact.createdByToolCallId, then event value; never fall back to
  // heuristics when an id is present (avoids row_count / path collisions).
  const authoritativeToolCallId =
    artifact.createdByToolCallId ?? stringValue(value.tool_call_id);
  if (authoritativeToolCallId) {
    return state.toolCalls.find((tool) => tool.id === authoritativeToolCallId);
  }

  // Legacy heuristics for artifacts that predate metadata_json.tool_call_id.
  if (artifact.type === "file" || artifact.kind === "file") {
    return findFileToolForArtifact(state, value);
  }
  if (
    artifact.type === "dataset" ||
    artifact.detail?.type === "dataset" ||
    stringValue(value.type) === "table"
  ) {
    return findSqlToolForArtifact(state, value);
  }
  if (stringValue(value.type) === "file") {
    return findFileToolForArtifact(state, value);
  }

  // Title-only artifact payloads: link only when a single SQL tool is waiting.
  const unlinkedSql = unlinkedSuccessfulSqlTools(state);
  if (unlinkedSql.length === 1) return unlinkedSql[0];

  return undefined;
}

function linkArtifactToToolCall(
  state: LiveRun,
  artifactId: string,
  toolCallId: string,
): LiveRun {
  const events = state.events.map((event) =>
    event.id === toolCallId
      ? {
          ...event,
          artifactIds: [...(event.artifactIds ?? []), artifactId].filter(
            (id, index, array) => array.indexOf(id) === index,
          ),
        }
      : event,
  );
  const artifacts = state.artifacts.map((artifact) =>
    artifact.id === artifactId
      ? { ...artifact, createdByEventId: toolCallId }
      : artifact,
  );
  return { ...state, events, artifacts };
}

function applyInteractionToolIdentity(
  state: LiveRun,
  toolCallId: string,
  toolName: string,
): LiveRun {
  const existingCall = state.toolCalls.find((item) => item.id === toolCallId);
  const existingEvent = state.events.find((item) => item.id === toolCallId);
  const title = toolDisplayTitle(toolName);
  const kind = dataStepKindForTool(toolName);

  let next = state;
  if (existingCall) {
    next = upsertToolCallRecord(next, { ...existingCall, name: toolName });
  }
  if (existingEvent) {
    next = upsertTimelineEvent(next, {
      ...existingEvent,
      kind,
      toolName,
      title,
    });
  } else if (existingCall) {
    next = upsertTimelineEvent(next, {
      id: toolCallId,
      kind,
      toolName,
      title,
      summary: statusSummary(existingCall.status === "success" ? "completed" : existingCall.status),
      thought: "Agent is collaborating with the user.",
      payload: emptyStepPayload(kind),
    });
  }
  return next;
}

function upsertTimelineEvent(
  state: LiveRun,
  event: Omit<TimelineEvent, "ts">,
): LiveRun {
  const existingIndex = state.events.findIndex((item) => item.id === event.id);
  if (existingIndex === -1) {
    return { ...state, events: [...state.events, { ...event, ts: currentTime() }] };
  }
  const events = [...state.events];
  events[existingIndex] = {
    ...events[existingIndex],
    ...event,
    ts: events[existingIndex].ts,
  };
  return { ...state, events };
}

function applyPlanPatch(tasks: LivePlanTask[], patch: PlanPatch[]): LivePlanTask[] {
  const next = tasks.map((task) => ({ ...task }));
  for (const op of patch) {
    const match = op.path.match(/^\/tasks\/(\d+)\/status$/);
    if (!match) continue;
    const index = Number(match[1]);
    const status = planStatusFromValue(op.value);
    if (!Number.isInteger(index) || !next[index] || !status) continue;
    next[index] = { ...next[index], status };
  }
  return next;
}

type PlanPatch = { op?: string; path: string; value?: unknown };

function patchArray(value: unknown): PlanPatch[] {
  return arrayValue(value).filter(
    (item): item is PlanPatch =>
      typeof item === "object" &&
      item !== null &&
      typeof (item as { path?: unknown }).path === "string",
  );
}

function parsePlanTask(value: unknown): LivePlanTask | null {
  const record = recordValue(value);
  const id = stringValue(record?.id);
  const title = stringValue(record?.title);
  const status = planStatusFromValue(record?.status);
  return id && title && status ? { id, title, status } : null;
}

function planStatusFromValue(value: unknown): LiveTaskStatus | null {
  if (value === "pending" || value === "running" || value === "completed" || value === "failed") {
    return value;
  }
  return null;
}

function liveStatusFromValue(value: unknown): LiveRunStatus | null {
  if (
    value === "running" ||
    value === "suspended" ||
    value === "completed" ||
    value === "failed" ||
    value === "canceled"
  ) {
    return value;
  }
  if (value === "cancelled") return "canceled";
  return null;
}

function statusSummary(value: unknown): string {
  const status = stringValue(value);
  if (status === "running") return "Running.";
  if (status === "completed") return "Completed.";
  if (status === "failed") return "Failed.";
  if (status === "canceled" || status === "cancelled") return "Canceled.";
  return "Pending.";
}

export function resolveIncomingToolName(
  event: AgUiLikeEvent,
  existing?: LiveToolCallRecord,
  existingEvent?: TimelineEvent,
): string {
  const fromEvent =
    stringValue(event.toolCallName) ??
    stringValue(event.toolName) ??
    stringValue(event.name);
  if (fromEvent && fromEvent !== "tool" && fromEvent !== "unknown") {
    return fromEvent;
  }
  if (existing?.name && existing.name !== "tool" && existing.name !== "unknown") {
    return existing.name;
  }
  if (
    existingEvent?.toolName &&
    existingEvent.toolName !== "tool" &&
    existingEvent.toolName !== "unknown"
  ) {
    return existingEvent.toolName;
  }
  return fromEvent ?? existing?.name ?? existingEvent?.toolName ?? "tool";
}

function findMisnamedToolCall(
  toolCalls: LiveToolCallRecord[],
  toolName: string | undefined,
): LiveToolCallRecord | undefined {
  if (!toolName) return undefined;
  const candidates = toolCalls.filter(
    (call) =>
      !call.stepId &&
      (call.name === "tool" || call.name === "unknown") &&
      (call.status === "running" || call.status === "success"),
  );
  if (candidates.length === 1) return candidates[0];
  return candidates.find((call) => call.status === "running") ?? candidates.at(-1);
}

export function resolveProducedArtifacts(
  liveRun: LiveRun,
  event: TimelineEvent | null,
  artifacts: DataArtifact[],
): DataArtifact[] {
  if (!event) return [];

  const toolCall = resolveToolCallForEvent(liveRun, event);
  const linkedEventIds = new Set<string>([event.id]);
  if (toolCall) linkedEventIds.add(toolCall.id);
  if (event.stepId) {
    for (const timelineEvent of liveRun.events) {
      if (timelineEvent.stepId === event.stepId) linkedEventIds.add(timelineEvent.id);
    }
  }
  if (toolCall?.stepId) {
    for (const timelineEvent of liveRun.events) {
      if (timelineEvent.stepId === toolCall.stepId) linkedEventIds.add(timelineEvent.id);
    }
  }

  const artifactIds = new Set<string>();
  for (const eventId of linkedEventIds) {
    const timelineEvent = liveRun.events.find((item) => item.id === eventId);
    timelineEvent?.artifactIds?.forEach((id) => artifactIds.add(id));
  }
  for (const artifact of artifacts) {
    if (artifact.createdByEventId && linkedEventIds.has(artifact.createdByEventId)) {
      artifactIds.add(artifact.id);
    }
  }

  return artifacts.filter((artifact) => artifactIds.has(artifact.id));
}

export function findCorrelatedToolCall(
  toolCalls: LiveToolCallRecord[],
  toolName: string | undefined,
  stepId: string | undefined,
): LiveToolCallRecord | undefined {
  if (!toolName) return undefined;
  const sameName = toolCalls.filter((call) => call.name === toolName);
  if (stepId) {
    const byStep = sameName.find((call) => call.stepId === stepId);
    if (byStep) return byStep;
  }
  const runningUnlinked = sameName.find((call) => call.status === "running" && !call.stepId);
  if (runningUnlinked) return runningUnlinked;
  const unlinked = sameName.find((call) => !call.stepId);
  if (unlinked) return unlinked;
  // Do not fall back to an already-linked same-name tool: ACTIVITY often arrives
  // before the next TOOL_CALL_START and must stay as a step_id orphan until then.
  return undefined;
}

export function resolveToolCallForEvent(
  liveRun: LiveRun,
  event: TimelineEvent | null | undefined,
): LiveToolCallRecord | undefined {
  if (!event) return undefined;
  const direct = liveRun.toolCalls.find((call) => call.id === event.id);
  if (direct) return direct;
  if (event.stepId) {
    const byStep = liveRun.toolCalls.find((call) => call.stepId === event.stepId);
    if (byStep) return byStep;
  }
  return findCorrelatedToolCall(liveRun.toolCalls, event.toolName, event.stepId);
}

const SANDBOX_TOOL_NAMES = new Set(["execute_command"]);

export function resolveWorkspaceMetadataForToolCall(
  liveRun: LiveRun,
  toolCallId?: string,
): LiveWorkspaceMetadata | undefined {
  if (!toolCallId) return undefined;
  return liveRun.workspaceMetadata.find((entry) => entry.toolCallId === toolCallId);
}

export function resolveSandboxOutputsForToolCall(
  liveRun: LiveRun,
  toolCall?: LiveToolCallRecord,
): LiveSandboxOutput[] {
  if (!toolCall || liveRun.sandboxOutputs.length === 0) {
    return [];
  }
  const start = toolCall.startedAtMs ?? 0;
  const end = toolCall.finishedAtMs ?? Date.now() + 60_000;
  const windowed = liveRun.sandboxOutputs.filter(
    (output) => output.receivedAt >= start - 500 && output.receivedAt <= end + 5_000,
  );
  if (windowed.length > 0) {
    return windowed;
  }
  if (SANDBOX_TOOL_NAMES.has(toolCall.name)) {
    return liveRun.sandboxOutputs;
  }
  return [];
}

export function formatSandboxOutputText(output: LiveSandboxOutput): string {
  const record = recordValue(output.payload);
  const text =
    stringValue(record?.text) ??
    stringValue(record?.output) ??
    stringValue(record?.value);
  if (text) return text;
  const exitCode = numberValue(record?.code ?? record?.exitCode);
  if (output.kind === "exit" && exitCode !== undefined) {
    return `exit code: ${exitCode}`;
  }
  if (output.kind === "command") {
    return stringValue(record?.command) ?? "";
  }
  try {
    return JSON.stringify(output.payload, null, 2);
  } catch {
    return String(output.payload ?? "");
  }
}

export function formatWorkspaceMetadataSummary(entry: LiveWorkspaceMetadata): string {
  const record = recordValue(entry.payload);
  const status = stringValue(record?.status);
  const path = stringValue(record?.path);
  const parts = [
    entry.toolName ? `Tool ${entry.toolName}` : undefined,
    status ? `Status ${status}` : undefined,
    path ? `Path ${path}` : undefined,
  ].filter(Boolean);
  if (parts.length > 0) {
    return parts.join(" · ");
  }
  try {
    return JSON.stringify(entry.payload, null, 2);
  } catch {
    return String(entry.payload ?? "");
  }
}

export function resolveTraceToolStatus(
  toolStatus: LiveToolCallRecord["status"],
  activityStatus?: TimelineEvent["activityStatus"],
): LiveToolCallRecord["status"] {
  if (activityStatus === "failed" || toolStatus === "failed") return "failed";
  if (activityStatus === "completed" && toolStatus === "running") return "success";
  return toolStatus;
}

export type StepTokenUsageSnapshot = TokenUsageStats & {
  reported: boolean;
  models: string[];
  /** True when usage was matched only via step_number fallback (no tool_call_id/step_id). */
  approximate?: boolean;
};

function tokenUsageRecordKey(record: LiveTokenUsageRecord): string {
  return [
    record.toolCallId ?? "",
    record.stepId ?? "",
    record.stepNumber ?? "",
    record.model ?? "",
    record.inputTokens,
    record.outputTokens,
    record.costUsd ?? "",
  ].join("|");
}

function aggregateTokenUsageRecords(
  records: LiveTokenUsageRecord[],
  input: { approximate?: boolean } = {},
): StepTokenUsageSnapshot {
  const seen = new Set<string>();
  const uniqueRecords: LiveTokenUsageRecord[] = [];
  for (const record of records) {
    const key = tokenUsageRecordKey(record);
    if (seen.has(key)) {
      continue;
    }
    seen.add(key);
    uniqueRecords.push(record);
  }

  const tokens = uniqueRecords.reduce(
    (current, record) => mergeTokenUsageStats(current, record),
    emptyTokenUsageStats(),
  );
  return {
    ...tokens,
    reported:
      tokens.inputTokens > 0 ||
      tokens.outputTokens > 0 ||
      (tokens.costUsd ?? 0) > 0,
    models: uniqueModels(uniqueRecords),
    approximate: input.approximate || undefined,
  };
}

export function resolveTokenUsageForToolCallIds(
  liveRun: LiveRun,
  toolCallIds: string[],
): StepTokenUsageSnapshot {
  const toolCallIdSet = new Set(toolCallIds);
  const stepIds = new Set(
    liveRun.toolCalls
      .filter((call) => toolCallIdSet.has(call.id) && call.stepId)
      .map((call) => call.stepId as string),
  );
  const exactMatches = liveRun.tokenUsageEvents.filter(
    (record) =>
      (record.toolCallId && toolCallIdSet.has(record.toolCallId)) ||
      (record.stepId && stepIds.has(record.stepId)),
  );
  if (exactMatches.length > 0) {
    return aggregateTokenUsageRecords(exactMatches);
  }

  const toolIndices = toolCallIds
    .map((id) => liveRun.toolCalls.findIndex((call) => call.id === id))
    .filter((index) => index >= 0);
  const maxStepNumber =
    toolIndices.length > 0 ? Math.max(...toolIndices) + 1 : undefined;
  const fallbackMatches =
    maxStepNumber !== undefined
      ? liveRun.tokenUsageEvents.filter((record) => record.stepNumber === maxStepNumber)
      : [];
  return aggregateTokenUsageRecords(fallbackMatches, {
    approximate: fallbackMatches.length > 0,
  });
}

export function resolveTokenUsageForEvent(
  liveRun: LiveRun,
  event: TimelineEvent | null | undefined,
): StepTokenUsageSnapshot {
  if (!event) {
    return { ...emptyTokenUsageStats(), reported: false, models: [] };
  }
  const toolCall = resolveToolCallForEvent(liveRun, event);
  const toolCallIndex = toolCall
    ? liveRun.toolCalls.findIndex((call) => call.id === toolCall.id)
    : -1;
  const stepNumber = toolCallIndex >= 0 ? toolCallIndex + 1 : undefined;

  const isExactMatch = (record: LiveTokenUsageRecord) => {
    if (
      record.toolCallId &&
      (record.toolCallId === event.id || record.toolCallId === toolCall?.id)
    ) {
      return true;
    }
    if (
      record.stepId &&
      (record.stepId === event.stepId || record.stepId === toolCall?.stepId)
    ) {
      return true;
    }
    return false;
  };

  const exactMatches = liveRun.tokenUsageEvents.filter(isExactMatch);
  const matches =
    exactMatches.length > 0
      ? exactMatches
      : liveRun.tokenUsageEvents.filter(
          (record) =>
            stepNumber !== undefined && record.stepNumber === stepNumber,
        );
  const approximate = exactMatches.length === 0 && matches.length > 0;

  return aggregateTokenUsageRecords(matches, { approximate });
}

function activityStatusFromValue(value: unknown): TimelineEvent["activityStatus"] | undefined {
  const status = stringValue(value);
  if (status === "running" || status === "completed" || status === "failed") {
    return status;
  }
  return undefined;
}

function thoughtForActivityStatus(
  status: TimelineEvent["activityStatus"] | undefined,
  kind: DataStepKind,
): string {
  if (status === "failed") {
    return kind === "query"
      ? "Read-only SQL did not complete successfully. Agent will adjust based on the error."
      : "The data tool step did not complete successfully.";
  }
  if (status === "completed") {
    return kind === "query"
      ? "Agent completed the read-only SQL query through the backend Data Gateway."
      : "Agent completed the current step through the backend data tool.";
  }
  return kind === "query"
    ? "Agent converts the natural language question into read-only SQL and runs it through the backend Data Gateway."
    : "Agent is advancing the analysis through the backend data tool.";
}

function mergeActivityPayload(
  kind: DataStepKind,
  content: Record<string, unknown>,
  existing?: TimelineEvent,
): DataStepPayload {
  const base = existing?.payload ?? emptyStepPayload(kind);

  if (kind === "query") {
    const payload = base as {
      question: string;
      sql: string;
      scannedRows: number;
      durationMs: number;
      errorMessage?: string;
    };
    const sql = stringValue(content.sql) ?? payload.sql;
    const output = recordValue(content.content);
    const scannedRows = numberValue(output?.row_count) ?? payload.scannedRows;
    const durationMs = numberValue(output?.elapsed_ms) ?? payload.durationMs;
    const errorMessage = stringValue(content.error_message) ?? payload.errorMessage;
    return { ...payload, sql, scannedRows, durationMs, errorMessage };
  }

  if (kind === "inspect") {
    const output = recordValue(content.content);
    const tables = output ? parseSchemaTables(output) : [];
    if (tables.length > 0) {
      return { tables };
    }
    return base;
  }

  const errorMessage = stringValue(content.error_message);
  if (errorMessage) {
    return { description: errorMessage, rawResult: errorMessage };
  }

  return base;
}

function latestAudit(state: LiveRun): LiveAudit | undefined {
  return state.audits[0];
}

function extractSql(event?: TimelineEvent): string {
  if (!event || event.kind !== "query") return "";
  const payload = event.payload as { sql?: string };
  return payload.sql ?? "";
}

function recordValue(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function arrayValue(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function stringArrayValue(value: unknown): string[] {
  return arrayValue(value).filter((item): item is string => typeof item === "string" && item.trim() !== "");
}

function stringValue(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value : undefined;
}

function numberValue(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function currentTime(): string {
  return new Intl.DateTimeFormat("en-US", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(new Date());
}

function emptyToolCallStats(): ToolCallStats {
  return { total: 0, success: 0, failed: 0, byTool: {} };
}

function emptySqlUsageStats(): SqlUsageStats {
  return { total: 0, success: 0, failed: 0, rowsScanned: 0, elapsedMs: 0 };
}

function emptyTokenUsageStats(): TokenUsageStats {
  return { inputTokens: 0, outputTokens: 0 };
}

function uniqueStrings(values: string[]): string[] {
  return [...new Set(values.filter(Boolean))];
}

function uniqueModels(records: Array<{ model?: string }>): string[] {
  return uniqueStrings(records.map((record) => record.model).filter((model): model is string => Boolean(model)));
}

function mergeTokenUsageStats(
  left: TokenUsageStats,
  right: TokenUsageStats,
): TokenUsageStats {
  const costUsd =
    left.costUsd !== undefined || right.costUsd !== undefined
      ? (left.costUsd ?? 0) + (right.costUsd ?? 0)
      : undefined;
  return {
    inputTokens: left.inputTokens + right.inputTokens,
    outputTokens: left.outputTokens + right.outputTokens,
    ...(costUsd !== undefined ? { costUsd } : {}),
  };
}

function upsertToolCallRecord(
  state: LiveRun,
  record: LiveToolCallRecord,
): LiveRun {
  const existingIndex = state.toolCalls.findIndex((item) => item.id === record.id);
  if (existingIndex === -1) {
    return { ...state, toolCalls: [...state.toolCalls, record] };
  }
  const toolCalls = [...state.toolCalls];
  toolCalls[existingIndex] = { ...toolCalls[existingIndex], ...record };
  return { ...state, toolCalls };
}

function finalizeRunningToolCalls(
  toolCalls: LiveToolCallRecord[],
  finalStatus: "success" | "failed",
): LiveToolCallRecord[] {
  return toolCalls.map((call) =>
    call.status === "running" ? { ...call, status: finalStatus } : call,
  );
}

function toolCallsToStats(toolCalls: LiveToolCallRecord[]): ToolCallStats {
  const stats = emptyToolCallStats();
  for (const call of toolCalls) {
    stats.total += 1;
    if (call.status === "success") stats.success += 1;
    if (call.status === "failed") stats.failed += 1;
    if (call.status === "running") stats.success += 0;

    const bucket = stats.byTool[call.name] ?? { calls: 0, success: 0, failed: 0 };
    bucket.calls += 1;
    if (call.status === "success") bucket.success += 1;
    if (call.status === "failed") bucket.failed += 1;
    stats.byTool[call.name] = bucket;
  }
  return stats;
}

function mergeToolCallStats(left: ToolCallStats, right: ToolCallStats): ToolCallStats {
  const byTool = { ...left.byTool };
  for (const [name, bucket] of Object.entries(right.byTool)) {
    const existing = byTool[name] ?? { calls: 0, success: 0, failed: 0 };
    byTool[name] = {
      calls: existing.calls + bucket.calls,
      success: existing.success + bucket.success,
      failed: existing.failed + bucket.failed,
    };
  }
  return {
    total: left.total + right.total,
    success: left.success + right.success,
    failed: left.failed + right.failed,
    byTool,
  };
}

function isSqlAuditFailure(status?: string): boolean {
  if (!status) return false;
  const normalized = status.toLowerCase();
  return normalized !== "success" && normalized !== "succeeded";
}

function sqlAuditsToStats(audits: LiveAudit[]): SqlUsageStats {
  const stats = emptySqlUsageStats();
  for (const audit of audits) {
    stats.total += 1;
    if (isSqlAuditFailure(audit.status)) stats.failed += 1;
    else stats.success += 1;
    stats.rowsScanned += audit.rowCount ?? 0;
    stats.elapsedMs += audit.elapsedMs ?? 0;
  }
  return stats;
}

function mergeSqlUsageStats(left: SqlUsageStats, right: SqlUsageStats): SqlUsageStats {
  return {
    total: left.total + right.total,
    success: left.success + right.success,
    failed: left.failed + right.failed,
    rowsScanned: left.rowsScanned + right.rowsScanned,
    elapsedMs: left.elapsedMs + right.elapsedMs,
  };
}
