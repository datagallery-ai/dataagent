import type {
  DataArtifact,
  DataStepKind,
  DataStepPayload,
  SchemaTable,
  TimelineEvent,
  TimelineStep,
} from "./data-task-state.js";
import { dataStepKindForTool, emptyStepPayload } from "./data-task-state.js";

type ArtifactDetailValue = NonNullable<DataArtifact["detail"]>;

export type LiveTaskStatus = "pending" | "running" | "completed" | "failed";

export type LivePlanTask = {
  id: string;
  title: string;
  status: LiveTaskStatus;
};

export type LiveAudit = {
  id: string;
  datasourceId?: string | undefined;
  status?: string | undefined;
  rowCount?: number | undefined;
  elapsedMs?: number | undefined;
};

export type LiveRunStatus = "idle" | "running" | "completed" | "failed";

export type LiveToolCallStatus =
  | "pending"
  | "running"
  | "success"
  | "failed"
  | "cancelled";

export type LiveToolCallRecord = {
  id: string;
  name: string;
  status: LiveToolCallStatus;
  /** Run identity this tool call belongs to; keeps reused provider call ids scoped. */
  runId?: string | undefined;
  /** Linked ACTIVITY STEP step_id when correlated with a backend tool wrapper. */
  stepId?: string | undefined;
  /** Raw AG-UI tool arguments from TOOL_CALL_START / TOOL_CALL_ARGS. */
  args?: unknown | undefined;
  /** Raw payload from AG-UI TOOL_CALL_RESULT when CopilotKit thread lacks tool message. */
  result?: string | undefined;
  /** Short result preview restored from persisted conversation metadata. */
  resultPreview?: string | undefined;
  startedAtMs?: number | undefined;
  finishedAtMs?: number | undefined;
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
};

export type RunUsageSnapshot = {
  runStatus: LiveRunStatus;
  errorMessage?: string | undefined;
  durationMs?: number | undefined;
  toolCalls: ToolCallStats;
  sql: SqlUsageStats;
  artifactCount: number;
  tokens: TokenUsageStats;
  tokenUsageReported: boolean;
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
};

export type LiveRun = {
  runId?: string | undefined;
  agentResponseComplete?: boolean | undefined;
  plan: LivePlanTask[];
  events: TimelineEvent[];
  artifacts: DataArtifact[];
  audits: LiveAudit[];
  runStatus: LiveRunStatus;
  errorMessage?: string | undefined;
  toolCalls: LiveToolCallRecord[];
  runStartedAt?: number | undefined;
  runFinishedAt?: number | undefined;
  tokenUsage?: TokenUsageStats | undefined;
};

type AgUiLikeEvent = {
  type?: string;
  [key: string]: unknown;
};

const defaultPlan: LivePlanTask[] = [
  { id: "schema", title: "检查数据源 schema", status: "pending" },
  { id: "sql", title: "生成并执行只读 SQL", status: "pending" },
  { id: "final", title: "生成最终回答", status: "pending" },
];

export function createInitialLiveRun(): LiveRun {
  return {
    plan: defaultPlan,
    events: [],
    artifacts: [],
    audits: [],
    runStatus: "idle",
    toolCalls: [],
  };
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
  };
}

function tokenUsageFromRun(liveRun: LiveRun): {
  tokens: TokenUsageStats;
  tokenUsageReported: boolean;
} {
  const tokens = liveRun.tokenUsage ?? emptyTokenUsageStats();
  const tokenUsageReported =
    tokens.inputTokens > 0 ||
    tokens.outputTokens > 0;
  return { tokens, tokenUsageReported };
}

export function deriveRunUsage(liveRun: LiveRun): RunUsageSnapshot {
  const durationMs =
    liveRun.runStartedAt !== undefined && liveRun.runFinishedAt !== undefined
      ? Math.max(0, liveRun.runFinishedAt - liveRun.runStartedAt)
      : liveRun.runStartedAt !== undefined && liveRun.runStatus === "running"
        ? Math.max(0, Date.now() - liveRun.runStartedAt)
        : undefined;

  const { tokens, tokenUsageReported } = tokenUsageFromRun(liveRun);

  return {
    runStatus: liveRun.runStatus,
    errorMessage: liveRun.errorMessage,
    durationMs,
    toolCalls: toolCallsToStats(liveRun.toolCalls),
    sql: sqlAuditsToStats(liveRun.audits),
    artifactCount: artifactCountForRun(liveRun),
    tokens,
    tokenUsageReported,
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
  };
}

/** Session totals for overview; merges in-progress run without double-counting completed runs. */
export function deriveLiveSessionView(
  session: SessionUsageStats,
  liveRun: LiveRun,
): SessionUsageStats & { includesInProgressRun: boolean } {
  if (liveRun.runStatus !== "running") {
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
    includesInProgressRun: true,
  };
}

export function reduceLiveRunEvent(state: LiveRun, event: AgUiLikeEvent): LiveRun {
  switch (event.type) {
    case "RUN_STARTED":
      return {
        ...createInitialLiveRun(),
        artifacts: state.artifacts,
        ...optionalRunId(runIdFromEvent(event)),
        agentResponseComplete: false,
        runStatus: "running",
        runStartedAt: Date.now(),
      };
    case "RUN_FINISHED":
      if (isTerminalRunStatus(state.runStatus) && state.runStatus !== "completed") {
        return state;
      }
      return completeRunState(state, "completed", "success");
    case "RUN_ERROR":
      if (isTerminalRunStatus(state.runStatus) && state.runStatus !== "failed") {
        return state;
      }
      return completeRunState(
        state,
        "failed",
        "failed",
        stringValue(event.message) ?? "Agent 运行失败",
      );
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

export function isTerminalToolCallStatus(status: LiveToolCallStatus): boolean {
  return status === "success" || status === "failed" || status === "cancelled";
}

function isTerminalRunStatus(status: LiveRunStatus): boolean {
  return status === "completed" || status === "failed";
}

function artifactCountForRun(liveRun: LiveRun): number {
  return liveRun.artifacts.filter((artifact) => isArtifactFromCurrentRun(liveRun, artifact)).length;
}

function isArtifactFromCurrentRun(liveRun: LiveRun, artifact: DataArtifact): boolean {
  if (liveRun.runStartedAt === undefined) return true;
  return artifact.recordedAtMs === undefined || artifact.recordedAtMs >= liveRun.runStartedAt;
}

function applyRunStatus(state: LiveRun, status: LiveRunStatus): LiveRun {
  if (state.runStatus === status && !isTerminalRunStatus(status)) return state;
  if (isTerminalRunStatus(state.runStatus) && state.runStatus !== status) return state;
  if (status === "completed") return completeRunState(state, "completed", "success");
  if (status === "failed") return completeRunState(state, "failed", "failed");
  return { ...state, runStatus: status };
}

function completeRunState(
  state: LiveRun,
  runStatus: Extract<LiveRunStatus, "completed" | "failed">,
  finalToolStatus: Extract<LiveToolCallStatus, "success" | "failed">,
  errorMessage?: string,
): LiveRun {
  return {
    ...state,
    agentResponseComplete: true,
    runStatus,
    ...(errorMessage ? { errorMessage } : {}),
    runFinishedAt: state.runFinishedAt ?? Date.now(),
    plan: runStatus === "completed"
      ? state.plan.map((task) =>
          task.status === "running" || task.id === "final"
            ? { ...task, status: "completed" }
            : task,
        )
      : state.plan,
    toolCalls: finalizeRunningToolCalls(state.toolCalls, finalToolStatus),
  };
}

function adoptRunId(state: LiveRun, runId: string): LiveRun {
  if (state.runId === runId) return state;
  const previousRunId = state.runId;
  const toolCalls = state.toolCalls.map((toolCall) =>
    toolCall.runId === undefined || toolCall.runId === previousRunId
      ? { ...toolCall, runId }
      : toolCall,
  );
  return { ...state, runId, toolCalls };
}

export function planTasksToTimelineSteps(tasks: LivePlanTask[]): TimelineStep[] {
  return tasks.map((task) => ({
    id: task.id,
    label: task.title,
    linkedEventId: task.id,
  }));
}

function reduceStateSnapshot(state: LiveRun, event: AgUiLikeEvent): LiveRun {
  const snapshot = recordValue(event.snapshot);
  const status = liveStatusFromValue(snapshot?.runStatus);
  const runId = stringValue(snapshot?.runId);
  if (!status && !runId) return state;

  let next = state;
  if (runId) next = adoptRunId(next, runId);
  if (status) next = applyRunStatus(next, status);
  return next;
}

function reduceStateDelta(state: LiveRun, event: AgUiLikeEvent): LiveRun {
  const patch = patchArray(event.delta);
  if (!patch.length) return state;

  let next = state;
  for (const op of patch) {
    if (op.path === "/runStatus") {
      const status = liveStatusFromValue(op.value);
      if (status) next = applyRunStatus(next, status);
    }
    if (op.path === "/errorMessage") {
      const errorMessage = stringValue(op.value);
      if (errorMessage) next = { ...next, errorMessage };
    }
  }
  return next;
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
    const title = stringValue(content.title) ?? stringValue(content.tool_name) ?? "执行工具";
    const toolName = stringValue(content.tool_name);
    const kind = dataStepKindForTool(toolName);
    const activityStatus = activityStatusFromValue(content.status);
    const fallbackId =
      stepId ?? stringValue(event.messageId) ?? `step-${state.events.length + 1}`;

    const toolCall = findCorrelatedToolCall(state.toolCalls, toolName, stepId);
    const eventId = toolCall?.id ?? fallbackId;

    let nextState = state;

    if (toolName && activityStatus) {
      const now = Date.now();
      const nextToolStatus = toolCall
        ? resolveTraceToolStatus(toolCall.status, activityStatus)
        : activityStatus === "failed"
          ? "failed"
          : activityStatus === "completed"
            ? "success"
            : "running";
      const activityResult = toolResultFromActivityContent(content, activityStatus);
      nextState = upsertToolCallRecord(nextState, {
        id: eventId,
        name: toolName,
        status: nextToolStatus,
        ...optionalRunId(state.runId),
        ...(stepId ? { stepId } : toolCall?.stepId ? { stepId: toolCall.stepId } : {}),
        startedAtMs: toolCall?.startedAtMs ?? now,
        ...(nextToolStatus !== "running"
          ? { finishedAtMs: toolCall?.finishedAtMs ?? now }
          : {}),
        ...(toolCall?.result
          ? { result: toolCall.result }
          : activityResult
            ? { result: activityResult }
            : {}),
      });
    }

    if (toolCall && stepId && stepId !== toolCall.id) {
      nextState = {
        ...nextState,
        events: nextState.events.filter((item) => item.id !== stepId),
      };
    }

    const existing = nextState.events.find((item) => item.id === eventId);

    const withTimelineEvent = upsertTimelineEvent(nextState, {
      id: eventId,
      kind,
      toolName: toolName ?? undefined,
      title,
      summary: statusSummary(content.status),
      thought: thoughtForActivityStatus(activityStatus, kind),
      stepId: stepId ?? undefined,
      activityStatus: activityStatus ?? undefined,
      payload: mergeActivityPayload(kind, content, existing),
      ...(existing?.artifactIds ? { artifactIds: existing.artifactIds } : {}),
    });
    return activityStatus === "completed"
      ? reconcileUnlinkedArtifacts(withTimelineEvent)
      : withTimelineEvent;
  }

  return state;
}

function reduceActivityDelta(state: LiveRun, event: AgUiLikeEvent): LiveRun {
  if (event.activityType !== "PLAN") return state;
  const patch = patchArray(event.patch);
  if (!patch.length) return state;
  return { ...state, plan: applyPlanPatch(state.plan, patch) };
}

function reduceToolEvent(state: LiveRun, event: AgUiLikeEvent): LiveRun {
  const incomingToolName =
    stringValue(event.toolCallName) ??
    stringValue(event.toolName) ??
    stringValue(event.name);
  const eventType = stringValue(event.type);
  const explicitId = stringValue(event.toolCallId);
  const directToolCall = explicitId
    ? state.toolCalls.find((item) => item.id === explicitId)
    : undefined;
  const directEvent = explicitId
    ? state.events.find((item) => item.id === explicitId)
    : undefined;
  const correlationToolName = isSpecificToolName(incomingToolName)
    ? incomingToolName
    : directToolCall?.name ?? directEvent?.toolName;
  const startFallbackToolCall =
    eventType === "TOOL_CALL_START" && !directToolCall && !explicitId
      ? findRunningToolCall(state.toolCalls, correlationToolName)
      : undefined;
  const resultFallbackToolCall =
    eventType !== "TOOL_CALL_START" && !directToolCall
      ? explicitId
        ? isSpecificToolName(correlationToolName)
          ? findRunningToolCall(state.toolCalls, correlationToolName)
          : findLatestRunningToolCall(state.toolCalls)
        : isSpecificToolName(correlationToolName)
          ? findCorrelatedToolCall(state.toolCalls, correlationToolName, undefined)
          : findLatestRunningToolCall(state.toolCalls)
      : undefined;
  const correlatedToolCall = eventType === "TOOL_CALL_START"
    ? directToolCall ?? startFallbackToolCall
    : directToolCall ?? resultFallbackToolCall;
  const correlatedEvent = correlatedToolCall
    ? state.events.find((item) => item.id === correlatedToolCall.id)
    : undefined;
  const toolName = resolveIncomingToolName(
    event,
    directToolCall ?? correlatedToolCall,
    directEvent ?? correlatedEvent,
  );
  const id = explicitId ?? correlatedToolCall?.id ?? `${toolName}-${state.events.length + 1}`;
  const recordRunId = runIdFromEvent(event) ?? state.runId;

  let nextState = state;
  if (
    explicitId &&
    correlatedToolCall &&
    correlatedToolCall.id !== explicitId &&
    !directToolCall
  ) {
    nextState = rekeyToolCall(nextState, correlatedToolCall.id, explicitId);
  }

  const incomingArgs = event.args ?? event.parameters;
  const resultPayload = resultPayloadString(event.result ?? event.content);
  if (eventType === "TOOL_CALL_START") {
    const existing = nextState.toolCalls.find((item) => item.id === id);
    const recordArgs = incomingArgs !== undefined ? incomingArgs : existing?.args;
    nextState = upsertToolCallRecord(nextState, {
      id,
      name: toolName,
      status: "running",
      ...optionalRunId(recordRunId),
      ...(existing?.stepId ? { stepId: existing.stepId } : {}),
      ...(recordArgs !== undefined ? { args: recordArgs } : {}),
      ...(existing?.result ? { result: existing.result } : {}),
      ...(existing?.resultPreview ? { resultPreview: existing.resultPreview } : {}),
      startedAtMs: existing?.startedAtMs ?? Date.now(),
    });
  } else if (eventType === "TOOL_CALL_ARGS") {
    const existing = nextState.toolCalls.find((item) => item.id === id);
    const recordArgs = incomingArgs !== undefined ? incomingArgs : existing?.args;
    nextState = upsertToolCallRecord(nextState, {
      id,
      name: toolName,
      status: existing?.status ?? "running",
      ...optionalRunId(recordRunId),
      startedAtMs: existing?.startedAtMs ?? Date.now(),
      ...(existing?.stepId ? { stepId: existing.stepId } : {}),
      ...(existing?.finishedAtMs ? { finishedAtMs: existing.finishedAtMs } : {}),
      ...(recordArgs !== undefined ? { args: recordArgs } : {}),
      ...(existing?.result ? { result: existing.result } : {}),
      ...(existing?.resultPreview ? { resultPreview: existing.resultPreview } : {}),
    });
  } else if (eventType === "TOOL_CALL_END") {
    const existing = nextState.toolCalls.find((item) => item.id === id);
    const status = existing && existing.status !== "running" ? existing.status : "running";
    const recordArgs = incomingArgs !== undefined ? incomingArgs : existing?.args;
    nextState = upsertToolCallRecord(nextState, {
      id,
      name: toolName,
      status,
      ...optionalRunId(recordRunId),
      startedAtMs: existing?.startedAtMs ?? Date.now(),
      ...(existing?.stepId ? { stepId: existing.stepId } : {}),
      ...(existing?.finishedAtMs ? { finishedAtMs: existing.finishedAtMs } : {}),
      ...(recordArgs !== undefined ? { args: recordArgs } : {}),
      ...(existing?.result ? { result: existing.result } : {}),
      ...(existing?.resultPreview ? { resultPreview: existing.resultPreview } : {}),
    });
  } else if (eventType === "TOOL_CALL_RESULT") {
    const existing = nextState.toolCalls.find((item) => item.id === id);
    const parsed = parseResultObject(resultPayload);
    const failed = toolResultPayloadLooksFailed(parsed);
    const recordArgs = incomingArgs !== undefined ? incomingArgs : existing?.args;
    nextState = upsertToolCallRecord(nextState, {
      id,
      name: toolName,
      status: failed ? "failed" : "success",
      ...optionalRunId(recordRunId),
      startedAtMs: existing?.startedAtMs ?? Date.now(),
      finishedAtMs: Date.now(),
      ...(existing?.stepId ? { stepId: existing.stepId } : {}),
      ...(recordArgs !== undefined ? { args: recordArgs } : {}),
      ...(resultPayload ? { result: resultPayload } : {}),
      ...(existing?.resultPreview ? { resultPreview: existing.resultPreview } : {}),
    });
  }

  const kind = dataStepKindForTool(toolName);
  const title =
    toolName === "run_sql_readonly"
      ? "生成并执行 SQL"
      : toolName === "inspect_schema"
        ? "检查数据源 Schema"
        : toolName;
  const args = recordValue(incomingArgs);
  const sql = stringValue(args?.sql) ?? stringValue(event.delta) ?? "";
  const result = resultPayload;

  if (kind === "query") {
    const existing = nextState.events.find((item) => item.id === id);
    const parsed = parseResultObject(result);
    const rowCount = numberValue(parsed?.row_count) ?? latestAudit(state)?.rowCount ?? 0;
    const elapsedMs = numberValue(parsed?.elapsed_ms) ?? latestAudit(state)?.elapsedMs ?? 0;
    return finalizeToolEventState(upsertTimelineEvent(nextState, {
      id,
      kind,
      toolName,
      title,
      summary: summarizeSqlResult(result, parsed, event.type),
      thought: "Agent 将自然语言问题转换成只读 SQL，并通过后端 Data Gateway 执行。",
      payload: {
        question: "",
        sql: sql || extractSql(existing),
        scannedRows: rowCount,
        durationMs: elapsedMs,
      },
    }), eventType);
  }

  if (kind === "inspect") {
    const parsedSchema = parseResultObject(result);
    const tables = parseSchemaTables(parsedSchema);
    return finalizeToolEventState(upsertTimelineEvent(nextState, {
      id,
      kind,
      toolName,
      title,
      summary: summarizeSchemaResult(result, tables, event.type),
      thought: "Agent 先确认数据源结构，避免在不可靠字段上直接下结论。",
      payload: { tables },
    }), eventType);
  }

  return finalizeToolEventState(upsertTimelineEvent(nextState, {
    id,
    kind,
    toolName,
    title,
    summary: summarizeGenericResult(result, event.type),
    thought: "Agent 正在执行一次数据操作。",
    payload: {
      description: result || "",
      rawResult: result && result.length > 0 ? result : undefined,
    },
  }), eventType);
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
  if (toolResultPayloadLooksFailed(parseResultObject(result))) return "执行失败。";
  if (result) return result.length > 160 ? `${result.slice(0, 160)}…` : result;
  if (eventType === "TOOL_CALL_RESULT" || eventType === "TOOL_CALL_END") {
    return "数据操作已完成。";
  }
  return "正在执行数据操作。";
}

function parseResultObject(result: string): Record<string, unknown> | null {
  if (!result) return null;
  try {
    const parsed = JSON.parse(result);
    return recordValue(parsed);
  } catch {
    return null;
  }
}

function resultPayloadString(value: unknown): string {
  if (typeof value === "string") return value;
  if (value === undefined || value === null) return "";
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function toolResultFromActivityContent(
  content: Record<string, unknown>,
  status: TimelineEvent["activityStatus"],
): string | undefined {
  if (status === "failed") {
    return JSON.stringify({
      error: stringValue(content.error_message) ?? "Tool execution failed",
    });
  }

  if (status !== "completed") return undefined;
  const output = content.content ?? content.output;
  const payload = resultPayloadString(output);
  return payload || undefined;
}

function toolResultPayloadLooksFailed(parsed: Record<string, unknown> | null): boolean {
  if (!parsed) return false;
  const status = stringValue(parsed.status);
  if (status === "error" || status === "failed") return true;
  if (parsed.isError === true) return true;
  if (parsed.error !== undefined) return true;

  const result = recordValue(parsed.result);
  if (!result) return false;
  const resultStatus = stringValue(result.status);
  return (
    resultStatus === "error" ||
    resultStatus === "failed" ||
    result.isError === true ||
    result.error !== undefined
  );
}

function summarizeSqlResult(
  result: string,
  parsed: Record<string, unknown> | null,
  eventType?: string,
): string {
  if (toolResultPayloadLooksFailed(parsed)) return "执行失败。";
  const rowCount = numberValue(parsed?.row_count);
  if (rowCount !== undefined) return `已执行，返回 ${rowCount} 行。`;
  // Non-JSON results are usually human-readable text (or an error); keep them.
  if (result && !parsed) return result;
  if (eventType === "TOOL_CALL_RESULT") return "SQL 已执行。";
  return "正在准备只读 SQL 查询。";
}

function summarizeSchemaResult(
  result: string,
  tables: SchemaTable[],
  eventType?: string,
): string {
  if (toolResultPayloadLooksFailed(parseResultObject(result))) return "执行失败。";
  if (tables.length > 0) return `已检查 ${tables.length} 张表。`;
  if (result && !parseResultObject(result)) return result;
  if (eventType === "TOOL_CALL_RESULT") return "已检查数据源 schema。";
  return "正在调用后端数据工具。";
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

function reduceCustomEvent(state: LiveRun, event: AgUiLikeEvent): LiveRun {
  if (event.name === "run.response.completed") {
    return { ...state, agentResponseComplete: true };
  }

  if (event.name === "sql_audit") {
    const value = recordValue(event.value);
    const audit: LiveAudit = {
      id: stringValue(value?.audit_log_id) ?? `audit-${state.audits.length + 1}`,
      datasourceId: stringValue(value?.datasource_id) ?? undefined,
      status: stringValue(value?.status) ?? undefined,
      rowCount: numberValue(value?.row_count) ?? undefined,
      elapsedMs: numberValue(value?.elapsed_ms) ?? undefined,
    };
    return { ...state, audits: [audit, ...state.audits] };
  }

  if (event.name === "artifact") {
    const value = recordValue(event.value);
    if (!value) return state;

    const artifact = dataArtifactFromArtifactValue(value);
    let nextState: LiveRun = {
      ...state,
      artifacts: [
        artifact,
        ...state.artifacts.filter((item) => item.id !== artifact.id),
      ],
    };
    const sourceTool = findArtifactSourceTool(nextState, artifact, value);
    if (sourceTool) {
      nextState = linkArtifactToToolCall(nextState, artifact.id, sourceTool.id);
    }
    return nextState;
  }

  if (event.name === "token_usage") {
    const value = recordValue(event.value);
    const delta: TokenUsageStats = {
      inputTokens:
        numberValue(value?.input_tokens) ??
        numberValue(value?.prompt_tokens) ??
        0,
      outputTokens:
        numberValue(value?.output_tokens) ??
        numberValue(value?.completion_tokens) ??
        0,
    };
    return {
      ...state,
      tokenUsage: mergeTokenUsageStats(state.tokenUsage ?? emptyTokenUsageStats(), delta),
    };
  }

  return state;
}

function parseTablePreview(
  preview: unknown,
): { columns: string[]; rows: string[][] } | null {
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

function chartTypeValue(
  value: unknown,
): Extract<DataArtifact["detail"], { type: "chart" }>["chartType"] {
  if (value === "bar" || value === "line" || value === "pie") return value;
  return undefined;
}

function parseChartPreview(
  preview: unknown,
): Extract<DataArtifact["detail"], { type: "chart" }> | null {
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

function previewPayload(value: unknown): unknown {
  const record = recordValue(value);
  return recordValue(record?.preview_json) ?? recordValue(record?.preview) ?? value;
}

function textContentFromRecord(record: Record<string, unknown> | null): string | undefined {
  return (
    stringValue(record?.content) ??
    stringValue(record?.body) ??
    stringValue(record?.markdown) ??
    stringValue(record?.text) ??
    stringValue(record?.preview) ??
    stringValue(record?.preview_json)
  );
}

export function artifactDetailNeedsPreviewFetch(
  artifact: Pick<DataArtifact, "previewAvailable" | "fileId" | "type">,
  detail: ArtifactDetailValue | undefined,
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
  existing: ArtifactDetailValue | undefined,
  loaded: ArtifactDetailValue,
): ArtifactDetailValue {
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

export function artifactDetailFromPreview(
  artifact: Pick<DataArtifact, "type" | "kind" | "title">,
  preview: unknown,
): ArtifactDetailValue | undefined {
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
    const fileContent =
      textContentFromRecord(payloadRecord) ??
      (typeof payload === "string" ? stringValue(payload) : undefined);
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
    textContentFromRecord(payloadRecord) ??
    (typeof payload === "string" && backendType !== "table" ? payload : undefined);
  if (textContent) {
    return {
      type: "report",
      sections: [{ heading: "预览", body: textContent }],
    };
  }

  return undefined;
}

export function dataArtifactFromArtifactValue(value: Record<string, unknown>): DataArtifact {
  const id =
    stringValue(value.id) ??
    stringValue(value.artifact_id) ??
    `artifact-${Date.now()}`;
  const title =
    stringValue(value.title) ?? stringValue(value.name) ?? "Agent 产出物";
  const backendType = stringValue(value.type);
  const fileId = stringValue(value.file_id) ?? stringValue(value.fileId);
  const downloadUrl =
    stringValue(value.download_url) ?? stringValue(value.downloadUrl);
  const preview = value.preview_json;
  const previewRecord = recordValue(preview);
  const mimeType =
    stringValue(value.mime_type) ??
    stringValue(value.mimeType) ??
    stringValue(previewRecord?.mime_type) ??
    stringValue(previewRecord?.mimeType);
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
        `${tablePreview.rows.length.toLocaleString()} 行 × ${tablePreview.columns.length} 列`;
    } else if (rowCount !== undefined) {
      summary = summary ?? `数据集，${rowCount.toLocaleString()} 行`;
    }
  } else if (backendType === "chart") {
    type = "chart";
    kind = "chart";
    detail = parseChartPreview(preview) ?? undefined;
    summary = summary ?? "图表产出";
  } else if (backendType === "markdown" || backendType === "html") {
    type = "report";
    kind = "memo";
    const textContent =
      textContentFromRecord(previewRecord) ??
      (typeof preview === "string" ? stringValue(preview) : undefined);
    if (textContent) {
      detail = {
        type: "report",
        sections: [{ heading: "预览", body: textContent }],
      };
    }
    summary = summary ?? `${backendType} 报告`;
  } else if (backendType === "file") {
    type = "file";
    kind = "file";
    const filePath = stringValue(previewRecord?.path) ?? title;
    const fileSize = numberValue(previewRecord?.size);
    const fileMtime = stringValue(previewRecord?.mtime);
    const fileTool = stringValue(previewRecord?.tool);
    const fileContent =
      textContentFromRecord(previewRecord) ??
      (typeof preview === "string" ? stringValue(preview) : undefined);
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
        ? `文件 ${filePath}（${fileSize.toLocaleString()} bytes）`
        : `文件 ${filePath}`);
  }

  return {
    id,
    title,
    kind,
    type: type ?? undefined,
    summary: summary ?? "后端返回的产出物。",
    version: stringValue(value.version) ?? "v1",
    fileId: fileId ?? undefined,
    downloadUrl: downloadUrl ?? undefined,
    mimeType: mimeType ?? undefined,
    detail: detail ?? undefined,
    previewAvailable:
      value.preview_available === true ||
      Boolean(fileId) ||
      (preview !== undefined && preview !== null),
    recordedAtMs: Date.now(),
  };
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
      if (!tool || !isSqlToolCall(state, tool)) continue;
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
  path?: string | undefined;
  tool?: string | undefined;
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

  if (artifact.detail?.type === "dataset") {
    value.type = "table";
    value.preview_json = {
      columns: artifact.detail.columns,
      rows: artifact.detail.rows,
      row_count: artifact.detail.rows.length,
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

function reconcileUnlinkedArtifacts(state: LiveRun): LiveRun {
  let nextState = state;
  for (const artifact of state.artifacts) {
    if (artifact.createdByEventId) continue;
    if (!isArtifactFromCurrentRun(state, artifact)) continue;
    const linkedTool = findArtifactSourceTool(nextState, artifact);
    if (linkedTool) {
      nextState = linkArtifactToToolCall(nextState, artifact.id, linkedTool.id);
    }
  }
  return nextState;
}

function findArtifactSourceTool(
  state: LiveRun,
  artifact: DataArtifact,
  artifactValue?: Record<string, unknown>,
): LiveToolCallRecord | undefined {
  const value = artifactValue ?? artifactValueFromDataArtifact(artifact);

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

function rekeyToolCall(
  state: LiveRun,
  fromId: string,
  toId: string,
): LiveRun {
  if (fromId === toId) return state;

  const fromIndex = state.toolCalls.findIndex((item) => item.id === fromId);
  if (fromIndex === -1) return state;

  const toIndex = state.toolCalls.findIndex((item) => item.id === toId);
  const fromCall = state.toolCalls[fromIndex];
  const toCall = toIndex >= 0 ? state.toolCalls[toIndex] : undefined;
  if (!fromCall) return state;

  const mergedCall: LiveToolCallRecord = {
    ...fromCall,
    ...toCall,
    id: toId,
    name: isSpecificToolName(toCall?.name) ? toCall.name : fromCall.name,
    status: toCall?.status ?? fromCall.status,
    runId: toCall?.runId ?? fromCall.runId,
    startedAtMs: fromCall.startedAtMs ?? toCall?.startedAtMs,
    finishedAtMs: toCall?.finishedAtMs ?? fromCall.finishedAtMs,
    stepId: toCall?.stepId ?? fromCall.stepId,
    args: toCall?.args ?? fromCall.args,
    result: toCall?.result ?? fromCall.result,
    resultPreview: toCall?.resultPreview ?? fromCall.resultPreview,
  };

  const toolCalls = state.toolCalls.filter(
    (item) => item.id !== fromId && item.id !== toId,
  );
  toolCalls.splice(Math.min(fromIndex, toolCalls.length), 0, mergedCall);

  const events = state.events.map((event) =>
    event.id === fromId ? { ...event, id: toId } : event,
  );
  const artifacts = state.artifacts.map((artifact) =>
    artifact.createdByEventId === fromId
      ? { ...artifact, createdByEventId: toId }
      : artifact,
  );

  return { ...state, toolCalls, events, artifacts };
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
  const existingEvent = events[existingIndex];
  if (existingEvent) {
    events[existingIndex] = {
      ...existingEvent,
      ...event,
      ts: existingEvent.ts,
    };
  }
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
  if (value === "running" || value === "completed" || value === "failed") return value;
  return null;
}

export function runIdFromEvent(event: AgUiLikeEvent): string | undefined {
  return stringValue(event.runId) ??
    stringValue(recordValue(event.snapshot)?.runId) ??
    stringValue(recordValue(event.value)?.runId);
}

const optionalRunId = (runId: string | undefined): { runId?: string | undefined } =>
  runId ? { runId } : {};

function statusSummary(value: unknown): string {
  const status = stringValue(value);
  if (status === "running") return "正在执行。";
  if (status === "completed") return "已完成。";
  if (status === "failed") return "执行失败。";
  return "等待执行。";
}

function isSpecificToolName(value: string | undefined): value is string {
  return Boolean(value && value !== "tool" && value !== "unknown");
}

function resolveIncomingToolName(
  event: AgUiLikeEvent,
  existing?: LiveToolCallRecord,
  existingEvent?: TimelineEvent,
): string {
  const fromEvent =
    stringValue(event.toolCallName) ??
    stringValue(event.toolName) ??
    stringValue(event.name);
  if (isSpecificToolName(fromEvent)) return fromEvent;
  if (isSpecificToolName(existing?.name)) return existing.name;
  if (isSpecificToolName(existingEvent?.toolName)) return existingEvent.toolName;
  return fromEvent ?? existing?.name ?? existingEvent?.toolName ?? "tool";
}

function findRunningToolCall(
  toolCalls: LiveToolCallRecord[],
  toolName: string | undefined,
): LiveToolCallRecord | undefined {
  if (!isSpecificToolName(toolName)) return undefined;
  for (let index = toolCalls.length - 1; index >= 0; index -= 1) {
    const call = toolCalls[index];
    if (call?.name === toolName && call.status === "running") return call;
  }
  return undefined;
}

function findLatestRunningToolCall(
  toolCalls: LiveToolCallRecord[],
): LiveToolCallRecord | undefined {
  for (let index = toolCalls.length - 1; index >= 0; index -= 1) {
    const call = toolCalls[index];
    if (call?.status === "running") return call;
  }
  return undefined;
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
  return sameName.at(-1);
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

export function resolveTraceToolStatus(
  toolStatus: LiveToolCallRecord["status"],
  activityStatus?: TimelineEvent["activityStatus"],
): LiveToolCallRecord["status"] {
  if (toolStatus === "failed" || toolStatus === "cancelled") return toolStatus;
  if (activityStatus === "failed") return "failed";
  if (activityStatus === "completed" && !isTerminalToolCallStatus(toolStatus)) {
    return "success";
  }
  if (activityStatus === "running" && !isTerminalToolCallStatus(toolStatus)) {
    return "running";
  }
  return toolStatus;
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
      ? "只读 SQL 执行未成功，Agent 将根据错误信息调整策略。"
      : "数据工具步骤未成功完成。";
  }
  if (status === "completed") {
    return kind === "query"
      ? "Agent 已通过后端 Data Gateway 完成只读 SQL 查询。"
      : "Agent 已通过后端数据工具完成当前步骤。";
  }
  return kind === "query"
    ? "Agent 将自然语言问题转换成只读 SQL，并通过后端 Data Gateway 执行。"
    : "Agent 正在通过后端数据工具推进当前分析。";
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
    const errorMessage = stringValue(content.error_message) ?? payload.errorMessage ?? undefined;
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

function stringValue(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value : undefined;
}

function numberValue(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function currentTime(): string {
  return new Intl.DateTimeFormat("zh-CN", {
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

function mergeTokenUsageStats(
  left: TokenUsageStats,
  right: TokenUsageStats,
): TokenUsageStats {
  return {
    inputTokens: left.inputTokens + right.inputTokens,
    outputTokens: left.outputTokens + right.outputTokens,
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
  const existing = toolCalls[existingIndex];
  if (!existing) return state;
  toolCalls[existingIndex] = mergeToolCallRecord(existing, record);
  return { ...state, toolCalls };
}

function finalizeRunningToolCalls(
  toolCalls: LiveToolCallRecord[],
  finalStatus: Extract<LiveToolCallStatus, "success" | "failed">,
): LiveToolCallRecord[] {
  const finishedAtMs = Date.now();
  return toolCalls.map((call) =>
    !isTerminalToolCallStatus(call.status)
      ? { ...call, status: finalStatus, finishedAtMs: call.finishedAtMs ?? finishedAtMs }
      : call,
  );
}

function mergeToolCallRecord(
  existing: LiveToolCallRecord,
  incoming: LiveToolCallRecord,
): LiveToolCallRecord {
  if (
    isTerminalToolCallStatus(existing.status) &&
    !isTerminalToolCallStatus(incoming.status)
  ) {
    return {
      ...existing,
      name: isSpecificToolName(existing.name) ? existing.name : incoming.name,
      runId: existing.runId ?? incoming.runId,
      stepId: existing.stepId ?? incoming.stepId,
      startedAtMs: existing.startedAtMs ?? incoming.startedAtMs,
      finishedAtMs: existing.finishedAtMs ?? incoming.finishedAtMs,
      args: existing.args ?? incoming.args,
      result: existing.result ?? incoming.result,
      resultPreview: existing.resultPreview ?? incoming.resultPreview,
    };
  }

  if (existing.status === "failed" && incoming.status === "success") {
    return {
      ...existing,
      runId: existing.runId ?? incoming.runId,
      stepId: existing.stepId ?? incoming.stepId,
      startedAtMs: existing.startedAtMs ?? incoming.startedAtMs,
      finishedAtMs: existing.finishedAtMs ?? incoming.finishedAtMs,
      args: existing.args ?? incoming.args,
      result: existing.result ?? incoming.result,
      resultPreview: existing.resultPreview ?? incoming.resultPreview,
    };
  }

  return { ...existing, ...incoming };
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
