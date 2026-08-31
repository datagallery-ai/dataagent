import type {
  ArtifactDetail,
  DataArtifact,
  GenericStepPayload,
  SchemaTable,
  TimelineEvent,
} from "./data-task-state";
import { dataStepKindForTool, dataStepLabel } from "./data-task-state";
import type { LiveAudit, LiveRun, LiveToolCallRecord } from "./live-run-state";
import { deriveRunUsage, resolveTraceToolStatus } from "./live-run-state";
import { parseToolResultRecord } from "./tool-result-normalize";

export type TraceEntryKind =
  | "run_started"
  | "run_finished"
  | "run_suspended"
  | "run_failed"
  | "tool"
  | "artifact";

export type TraceEntry = {
  id: string;
  kind: TraceEntryKind;
  ts?: string;
  tsMs?: number;
  title: string;
  summary: string;
  toolName?: string;
  toolStatus?: LiveToolCallRecord["status"];
  toolCallId?: string;
  eventId?: string;
  artifactIds?: string[];
  artifactDetail?: ArtifactDetail;
  sql?: string;
  scannedRows?: number;
  durationMs?: number;
  auditStatus?: string;
  datasourceId?: string;
  rawResult?: string;
  schemaTables?: SchemaTable[];
  errorMessage?: string;
};

export function formatTraceTime(ms: number): string {
  return new Intl.DateTimeFormat("en-US", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(ms));
}

function toolTitle(name: string): string {
  switch (name) {
    case "inspect_schema":
      return "Inspect data source schema";
    case "run_sql_readonly":
      return "Run SQL query";
    default:
      return name;
  }
}

function toolKindLabel(name: string): string {
  switch (name) {
    case "inspect_schema":
      return "Tool · Schema";
    case "run_sql_readonly":
      return "Tool · SQL";
    default:
      return `Tool · ${dataStepLabel(dataStepKindForTool(name))}`;
  }
}

function activityStatusToToolStatus(
  activityStatus?: TimelineEvent["activityStatus"],
): LiveToolCallRecord["status"] | undefined {
  if (activityStatus === "failed") return "failed";
  if (activityStatus === "completed") return "success";
  if (activityStatus === "running") return "running";
  return undefined;
}

function toolStatusLabel(status: LiveToolCallRecord["status"]): string {
  switch (status) {
    case "running":
      return "Running";
    case "success":
      return "Completed";
    case "failed":
      return "Failed";
  }
}

function auditStatusLabel(status?: string): string {
  if (!status) return "—";
  switch (status.toLowerCase()) {
    case "success":
    case "succeeded":
      return "Success";
    case "error":
    case "failed":
      return "Failed";
    default:
      return status;
  }
}

function parseResultObject(result?: string): Record<string, unknown> | null {
  if (!result) return null;
  return parseToolResultRecord(result);
}

function parseSchemaTables(parsed: Record<string, unknown> | null): SchemaTable[] {
  const rawTables = parsed?.tables;
  if (!Array.isArray(rawTables)) return [];
  const tables: SchemaTable[] = [];
  for (const rawTable of rawTables) {
    if (typeof rawTable !== "object" || rawTable === null) continue;
    const table = rawTable as Record<string, unknown>;
    const name = typeof table.name === "string" ? table.name : undefined;
    if (!name) continue;
    const fields = Array.isArray(table.columns)
      ? table.columns
          .map((rawColumn) => {
            if (typeof rawColumn !== "object" || rawColumn === null) return undefined;
            const column = rawColumn as Record<string, unknown>;
            const columnName = typeof column.name === "string" ? column.name : undefined;
            if (!columnName) return undefined;
            const type = typeof column.type === "string" ? column.type : undefined;
            return type ? `${columnName} · ${type}` : columnName;
          })
          .filter((field): field is string => Boolean(field))
      : [];
    tables.push({
      name,
      description: typeof table.description === "string" ? table.description : "",
      fields,
    });
  }
  return tables;
}

function pickAuditForSql(
  audits: LiveAudit[],
  usedAuditIds: Set<string>,
  scannedRows?: number,
): LiveAudit | undefined {
  if (scannedRows !== undefined && scannedRows > 0) {
    const matched = audits.find(
      (audit) =>
        !usedAuditIds.has(audit.id) && audit.rowCount === scannedRows,
    );
    if (matched) return matched;
  }
  return audits.find((audit) => !usedAuditIds.has(audit.id));
}

function resolveToolTimestamp(toolCall: LiveToolCallRecord): number | undefined {
  return toolCall.finishedAtMs ?? toolCall.startedAtMs;
}

function withTimestamp(
  entry: TraceEntry,
  tsMs?: number,
  fallbackMs?: number,
): TraceEntry {
  const resolved = tsMs ?? fallbackMs;
  if (resolved === undefined) return entry;
  return {
    ...entry,
    tsMs: resolved,
    ts: formatTraceTime(resolved),
  };
}

function buildToolEntry(
  toolCall: LiveToolCallRecord,
  event: TimelineEvent | undefined,
  audits: LiveAudit[],
  usedAuditIds: Set<string>,
  artifactsById: Map<string, DataArtifact>,
  fallbackMs?: number,
): TraceEntry {
  const parsed = parseResultObject(toolCall.result);
  const artifactIds = event?.artifactIds ?? [];
  const linkedArtifact = artifactIds
    .map((artifactId) => artifactsById.get(artifactId))
    .find(Boolean);
  const tsMs = resolveToolTimestamp(toolCall);

  if (toolCall.name === "run_sql_readonly" || event?.kind === "query") {
    const payload = event?.payload as
      | { sql?: string; scannedRows?: number; durationMs?: number }
      | undefined;
    const sql =
      payload?.sql ||
      (typeof parsed?.sql === "string" ? parsed.sql : "") ||
      "";
    const scannedRows =
      payload?.scannedRows ??
      (typeof parsed?.row_count === "number" ? parsed.row_count : undefined);
    const durationMs =
      payload?.durationMs ??
      (typeof parsed?.elapsed_ms === "number" ? parsed.elapsed_ms : undefined);
    const audit = pickAuditForSql(audits, usedAuditIds, scannedRows);
    if (audit) usedAuditIds.add(audit.id);

    const summary =
      event?.summary ||
      (scannedRows !== undefined
        ? `Executed and returned ${scannedRows.toLocaleString()} rows.`
        : toolCall.result && !parsed
          ? toolCall.result.slice(0, 160)
          : "Preparing a read-only SQL query.");

    return withTimestamp(
      {
        id: `tool-${toolCall.id}`,
        kind: "tool",
        ts: event?.ts,
        title: event?.title ?? toolTitle(toolCall.name),
        summary,
        toolName: toolCall.name,
        toolStatus: resolveTraceToolStatus(toolCall.status, event?.activityStatus),
        toolCallId: toolCall.id,
        eventId: event?.id,
        artifactIds,
        artifactDetail: linkedArtifact?.detail,
        sql: sql || undefined,
        scannedRows,
        durationMs: durationMs ?? audit?.elapsedMs,
        auditStatus: audit?.status,
        datasourceId: audit?.datasourceId,
        rawResult: toolCall.result,
      },
      tsMs,
      fallbackMs,
    );
  }

  if (toolCall.name === "inspect_schema" || event?.kind === "inspect") {
    const payload = event?.payload as { tables?: SchemaTable[] } | undefined;
    const schemaTables =
      payload?.tables && payload.tables.length > 0
        ? payload.tables
        : parseSchemaTables(parsed);
    const summary =
      event?.summary ||
      (schemaTables.length > 0
        ? `Loaded ${schemaTables.length} table schemas.`
        : "Inspecting data source schema.");

    return withTimestamp(
      {
        id: `tool-${toolCall.id}`,
        kind: "tool",
        ts: event?.ts,
        title: event?.title ?? toolTitle(toolCall.name),
        summary,
        toolName: toolCall.name,
        toolStatus: resolveTraceToolStatus(toolCall.status, event?.activityStatus),
        toolCallId: toolCall.id,
        eventId: event?.id,
        artifactIds,
        schemaTables,
        rawResult: toolCall.result,
      },
      tsMs,
      fallbackMs,
    );
  }

  return withTimestamp(
    {
      id: `tool-${toolCall.id}`,
      kind: "tool",
      ts: event?.ts,
      title: event?.title ?? toolTitle(toolCall.name),
      summary:
        event?.summary ||
        (toolCall.result
          ? toolCall.result.slice(0, 160)
          : `${toolTitle(toolCall.name)} ${toolStatusLabel(toolCall.status)}`),
      toolName: toolCall.name,
      toolStatus: resolveTraceToolStatus(toolCall.status, event?.activityStatus),
      toolCallId: toolCall.id,
      eventId: event?.id,
      artifactIds,
      rawResult: toolCall.result,
    },
    tsMs,
    fallbackMs,
  );
}

function buildEventOnlyEntry(
  event: TimelineEvent,
  artifactsById: Map<string, DataArtifact>,
  fallbackMs?: number,
): TraceEntry {
  const linkedArtifact = event.artifactIds
    ?.map((artifactId) => artifactsById.get(artifactId))
    .find(Boolean);

  if (event.kind === "query") {
    const payload = event.payload as {
      sql?: string;
      scannedRows?: number;
      durationMs?: number;
    };
    return withTimestamp(
      {
        id: `event-${event.id}`,
        kind: "tool",
        ts: event.ts,
        title: event.title,
        summary: event.summary,
        toolName: event.toolName ?? "run_sql_readonly",
        toolStatus: activityStatusToToolStatus(event.activityStatus) ?? "running",
        eventId: event.id,
        artifactIds: event.artifactIds,
        artifactDetail: linkedArtifact?.detail,
        sql: payload.sql || undefined,
        scannedRows: payload.scannedRows,
        durationMs: payload.durationMs,
      },
      undefined,
      fallbackMs,
    );
  }

  if (event.kind === "inspect") {
    const payload = event.payload as { tables?: SchemaTable[] };
    return withTimestamp(
      {
        id: `event-${event.id}`,
        kind: "tool",
        ts: event.ts,
        title: event.title,
        summary: event.summary,
        toolName: event.toolName ?? "inspect_schema",
        toolStatus: activityStatusToToolStatus(event.activityStatus) ?? "running",
        eventId: event.id,
        artifactIds: event.artifactIds,
        schemaTables: payload.tables,
      },
      undefined,
      fallbackMs,
    );
  }

  const payload = event.payload as GenericStepPayload;
  return withTimestamp(
    {
      id: `event-${event.id}`,
      kind: "tool",
      ts: event.ts,
      title: event.title,
      summary: event.summary,
      toolName: event.toolName,
      toolStatus: activityStatusToToolStatus(event.activityStatus) ?? "running",
      eventId: event.id,
      artifactIds: event.artifactIds,
      artifactDetail: linkedArtifact?.detail,
      rawResult: payload?.rawResult,
    },
    undefined,
    fallbackMs,
  );
}

function buildArtifactEntry(artifact: DataArtifact, fallbackMs?: number): TraceEntry {
  return withTimestamp(
    {
      id: `artifact-${artifact.id}`,
      kind: "artifact",
      title: artifact.title,
      summary: artifact.summary,
      artifactIds: [artifact.id],
      artifactDetail: artifact.detail,
    },
    artifact.recordedAtMs,
    fallbackMs,
  );
}

function sortTraceEntries(entries: TraceEntry[]): TraceEntry[] {
  return [...entries].sort((left, right) => (left.tsMs ?? 0) - (right.tsMs ?? 0));
}

function pushRunStartEntry(
  entries: TraceEntry[],
  startedAt: number | undefined,
  entryId: string,
  options?: { resumeAfterSuspend?: boolean },
): void {
  if (startedAt === undefined) return;
  entries.push({
    id: entryId,
    kind: "run_started",
    tsMs: startedAt,
    ts: formatTraceTime(startedAt),
    title: options?.resumeAfterSuspend ? "Run resumed" : "Run started",
    summary: options?.resumeAfterSuspend
      ? "Agent received your response and resumed."
      : "Agent started processing this request.",
  });
}

function historyRunEndEntryId(prefix: string, status: LiveRun["runStatus"]): string {
  if (status === "suspended") return `${prefix}-suspended`;
  if (status === "failed") return `${prefix}-failed`;
  return `${prefix}-finished`;
}

function pushRunEndEntry(
  entries: TraceEntry[],
  segment: {
    finishedAt?: number;
    status: LiveRun["runStatus"];
    errorMessage?: string;
  },
  entryId: string,
): void {
  if (segment.status === "suspended" && segment.finishedAt !== undefined) {
    entries.push({
      id: entryId,
      kind: "run_suspended",
      tsMs: segment.finishedAt,
      ts: formatTraceTime(segment.finishedAt),
      title: "Run paused",
      summary: "Agent is waiting for your response before continuing.",
    });
    return;
  }
  if (segment.status === "completed" && segment.finishedAt !== undefined) {
    entries.push({
      id: entryId,
      kind: "run_finished",
      tsMs: segment.finishedAt,
      ts: formatTraceTime(segment.finishedAt),
      title: "Run completed",
      summary: "Agent completed this task.",
    });
    return;
  }
  if (segment.status === "failed") {
    entries.push({
      id: entryId,
      kind: "run_failed",
      tsMs: segment.finishedAt,
      ts:
        segment.finishedAt !== undefined
          ? formatTraceTime(segment.finishedAt)
          : undefined,
      title: "Run failed",
      summary: segment.errorMessage ?? "Agent Run failed。",
      errorMessage: segment.errorMessage,
    });
  }
}

function pushRunBoundaryEntries(
  entries: TraceEntry[],
  segment: {
    startedAt?: number;
    finishedAt?: number;
    status: LiveRun["runStatus"];
    errorMessage?: string;
  },
  idPrefix: string,
  options?: { resumeAfterSuspend?: boolean },
): void {
  pushRunStartEntry(entries, segment.startedAt, `${idPrefix}-started`, options);
  pushRunEndEntry(
    entries,
    segment,
    historyRunEndEntryId(idPrefix, segment.status),
  );
}

export function buildTraceTimeline(liveRun: LiveRun): TraceEntry[] {
  const hasActivity =
    liveRun.runStatus !== "idle" ||
    liveRun.toolCalls.length > 0 ||
    liveRun.events.length > 0 ||
    liveRun.artifacts.length > 0 ||
    (liveRun.runHistory?.length ?? 0) > 0;

  if (!hasActivity) return [];

  const entries: TraceEntry[] = [];
  const eventById = new Map(liveRun.events.map((event) => [event.id, event]));
  const artifactsById = new Map(liveRun.artifacts.map((artifact) => [artifact.id, artifact]));
  const usedAuditIds = new Set<string>();
  const linkedArtifactIds = new Set<string>();
  let sequenceFallbackMs = liveRun.runStartedAt ?? Date.now();

  const appendToolEntries = (
    startIndex: number,
    endIndex: number,
    timeBaseline?: number,
  ) => {
    let fallbackMs = timeBaseline ?? sequenceFallbackMs;
    for (const toolCall of liveRun.toolCalls.slice(startIndex, endIndex)) {
      const event = eventById.get(toolCall.id);
      event?.artifactIds?.forEach((artifactId) => linkedArtifactIds.add(artifactId));
      entries.push(
        buildToolEntry(
          toolCall,
          event,
          liveRun.audits,
          usedAuditIds,
          artifactsById,
          (fallbackMs += 1),
        ),
      );
    }
    sequenceFallbackMs = fallbackMs;
  };

  let toolStartIndex = 0;
  for (const [index, segment] of liveRun.runHistory?.entries() ?? []) {
    const resumeAfterSuspend =
      index > 0 && liveRun.runHistory?.[index - 1]?.status === "suspended";
    pushRunStartEntry(entries, segment.startedAt, `run-history-${index}-started`, {
      resumeAfterSuspend,
    });
    appendToolEntries(toolStartIndex, segment.toolCallEndIndex, segment.startedAt);
    pushRunEndEntry(
      entries,
      segment,
      historyRunEndEntryId(`run-history-${index}`, segment.status),
    );
    toolStartIndex = segment.toolCallEndIndex;
  }

  if (liveRun.runStartedAt !== undefined) {
    const resumeAfterSuspend = liveRun.runHistory?.at(-1)?.status === "suspended";
    pushRunStartEntry(entries, liveRun.runStartedAt, "run-started-current", {
      resumeAfterSuspend,
    });
  }

  appendToolEntries(toolStartIndex, liveRun.toolCalls.length, liveRun.runStartedAt);

  for (const event of liveRun.events) {
    if (liveRun.toolCalls.some((toolCall) => toolCall.id === event.id)) continue;
    event.artifactIds?.forEach((artifactId) => linkedArtifactIds.add(artifactId));
    entries.push(
      buildEventOnlyEntry(event, artifactsById, (sequenceFallbackMs += 1)),
    );
  }

  for (const artifact of liveRun.artifacts) {
    if (linkedArtifactIds.has(artifact.id)) continue;
    entries.push(buildArtifactEntry(artifact, (sequenceFallbackMs += 1)));
  }

  if (liveRun.runStatus === "suspended" && liveRun.runFinishedAt !== undefined) {
    pushRunEndEntry(
      entries,
      {
        finishedAt: liveRun.runFinishedAt,
        status: "suspended",
      },
      "run-suspended-current",
    );
  } else if (liveRun.runStatus === "completed" && liveRun.runFinishedAt !== undefined) {
    pushRunEndEntry(
      entries,
      {
        finishedAt: liveRun.runFinishedAt,
        status: "completed",
      },
      "run-finished-current",
    );
  } else if (liveRun.runStatus === "failed") {
    pushRunEndEntry(
      entries,
      {
        finishedAt: liveRun.runFinishedAt,
        status: "failed",
        errorMessage: liveRun.errorMessage,
      },
      "run-failed-current",
    );
  }

  return sortTraceEntries(entries);
}

export function traceTimelineStats(liveRun: LiveRun, entries: TraceEntry[]) {
  const runUsage = deriveRunUsage(liveRun);
  return {
    toolCount: liveRun.toolCalls.length,
    artifactCount: liveRun.artifacts.length,
    entryCount: entries.length,
    durationMs: runUsage.durationMs,
    runStatus: liveRun.runStatus,
  };
}

export {
  auditStatusLabel,
  toolKindLabel,
  toolStatusLabel,
  toolTitle,
};
