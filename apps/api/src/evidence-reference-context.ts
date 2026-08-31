import type {
  EvidenceRef,
  EvidenceResolutionDiagnostics,
  EvidenceResolutionIssue,
  EvidenceSelection
} from "@datafoundry/contracts";
import {
  createAgentContextItem,
  createAgentContextSourceMetadata,
  type AgentContextItem
} from "@datafoundry/agent-runtime";
import type {
  MetadataStore,
  RunEventRecord,
  RunRecord,
  SqlAuditLogRecord
} from "@datafoundry/metadata";

type ResolveEvidenceReferenceContextInput = {
  evidenceRefs: EvidenceRef[];
  metadataStore: MetadataStore;
  sessionId: string;
  userId: string;
  workspaceId: string;
  maxCharsPerEvidence?: number;
};

export type ResolvedEvidenceReferenceContext = {
  diagnostics: EvidenceResolutionDiagnostics;
  items: AgentContextItem[];
};

/** Resolves client EvidenceRef handles into server-authoritative ContextItems for prompt assembly. */
export function resolveEvidenceReferenceContext(
  input: ResolveEvidenceReferenceContextInput
): ResolvedEvidenceReferenceContext {
  const items: AgentContextItem[] = [];
  const accepted: string[] = [];
  const dropped: EvidenceResolutionIssue[] = [];
  const seen = new Set<string>();

  for (const ref of input.evidenceRefs) {
    if (seen.has(ref.id)) continue;
    seen.add(ref.id);
    if (ref.sessionId !== input.sessionId) {
      dropped.push({ id: ref.id, reason: "session_mismatch" });
      continue;
    }
    const result = resolveEvidenceRef(input, ref);
    if (result.issue) {
      dropped.push(result.issue);
      continue;
    }
    items.push(...result.items);
    accepted.push(ref.id);
  }

  return { diagnostics: { accepted, dropped }, items };
}

const resolveEvidenceRef = (
  input: ResolveEvidenceReferenceContextInput,
  ref: EvidenceRef
): { items: AgentContextItem[]; issue?: never } | { items?: never; issue: EvidenceResolutionIssue } => {
  try {
    if (ref.source.artifactId) {
      return { items: artifactEvidenceItems(input, ref, ref.source.artifactId) };
    }
    if (ref.source.auditLogId) {
      return { items: sqlAuditEvidenceItems(input, ref, ref.source.auditLogId) };
    }
    if (ref.source.toolCallId || ref.runId) {
      return { items: toolEventEvidenceItems(input, ref) };
    }
    return { issue: { id: ref.id, reason: "unsupported" } };
  } catch (error) {
    return {
      issue: {
        id: ref.id,
        reason: issueReasonFromError(error),
        message: error instanceof Error ? error.message : String(error)
      }
    };
  }
};

const artifactEvidenceItems = (
  input: ResolveEvidenceReferenceContextInput,
  ref: EvidenceRef,
  artifactId: string
): AgentContextItem[] => {
  const artifact = input.metadataStore.artifacts.get({ user_id: input.userId, artifact_id: artifactId });
  assertSession(ref, artifact.session_id);
  const preview = parseJsonValue(artifact.preview_json);
  const metadata = parseJsonValue(artifact.metadata_json);
  const maxChars = input.maxCharsPerEvidence ?? 6000;
  const focus = resolveSelectionFocus(ref.source.selection, preview, maxChars);
  const includeFullPreview = preview !== undefined && (!focus || !focus.replaceFullPreview);
  // When a table subset is already inlined, omit file_id so the model is not nudged
  // to open the full artifact file for a partial cite.
  const fileIdLine = artifact.file_asset_ref_id && !focus?.replaceFullPreview
    ? `file_id=${artifact.file_asset_ref_id}`
    : undefined;
  const content = evidenceText({
    body: [
      `artifact_id=${artifact.id}`,
      `artifact_type=${artifact.type}`,
      `artifact_name=${artifact.name}`,
      fileIdLine,
      includeFullPreview ? `preview=${boundJson(preview, maxChars)}` : undefined,
      metadata !== undefined ? `metadata=${boundJson(metadata, 1200)}` : undefined,
      focus?.replaceFullPreview
        ? "note: selected subset is already inlined below; do not open the full artifact file unless the user asks for broader context"
        : undefined,
      ...(focus?.lines ?? [])
    ],
    ref
  });
  return [
    createEvidenceItem(input, ref, "model", content, "tool"),
    createEvidenceItem(input, ref, "artifact-ref", { artifact_id: artifact.id, source: "evidence-focus" }, "tool")
  ];
};

const sqlAuditEvidenceItems = (
  input: ResolveEvidenceReferenceContextInput,
  ref: EvidenceRef,
  auditLogId: string
): AgentContextItem[] => {
  const audit = input.metadataStore.sqlAuditLogs.get({ user_id: input.userId, audit_log_id: auditLogId });
  if (audit.run_id) {
    const run = input.metadataStore.runs.get({ user_id: input.userId, run_id: audit.run_id });
    assertSession(ref, run.session_id);
  }
  const focus = resolveSelectionFocus(ref.source.selection, undefined, input.maxCharsPerEvidence ?? 6000);
  const content = evidenceText({
    body: [
      `audit_log_id=${audit.id}`,
      `datasource_id=${audit.datasource_id}`,
      `status=${audit.status}`,
      audit.row_count !== undefined ? `row_count=${audit.row_count}` : undefined,
      audit.elapsed_ms !== undefined ? `elapsed_ms=${audit.elapsed_ms}` : undefined,
      audit.blocked_reason ? `blocked_reason=${audit.blocked_reason}` : undefined,
      "sql:",
      boundText(audit.sql_text, input.maxCharsPerEvidence ?? 6000),
      ...(focus?.lines ?? [])
    ],
    ref
  });
  return [
    createEvidenceItem(input, ref, "model", content, "tool", audit.datasource_id),
    createEvidenceItem(input, ref, "activity", sqlAuditActivity(audit), "tool", audit.datasource_id),
    createEvidenceItem(input, ref, "audit-ref", { audit_log_id: audit.id, source: "evidence-focus" }, "tool")
  ];
};

const toolEventEvidenceItems = (
  input: ResolveEvidenceReferenceContextInput,
  ref: EvidenceRef
): AgentContextItem[] => {
  const run = requireEvidenceRun(input, ref);
  const events = input.metadataStore.runEvents.listByRun({ user_id: input.userId, run_id: run.id });
  const event = findToolEvent(events, ref);
  if (!event) {
    throw new Error(`EVIDENCE_TOOL_EVENT_NOT_FOUND:${ref.id}`);
  }
  const content = evidenceText({
    body: [
      `run_id=${run.id}`,
      event.toolName ? `tool_name=${event.toolName}` : undefined,
      event.toolCallId ? `tool_call_id=${event.toolCallId}` : undefined,
      event.args !== undefined ? `args=${boundJson(event.args, 1600)}` : undefined,
      event.result !== undefined ? `result=${boundJson(event.result, input.maxCharsPerEvidence)}` : undefined
    ],
    ref
  });
  return [
    createEvidenceItem(input, ref, "model", content, ref.kind === "knowledge" ? "knowledge" : "tool"),
    createEvidenceItem(input, ref, "activity", toolEventActivity(event), "tool")
  ];
};

const createEvidenceItem = (
  input: ResolveEvidenceReferenceContextInput,
  ref: EvidenceRef,
  visibility: AgentContextItem["visibility"],
  content: unknown,
  trust: AgentContextItem["trust"],
  datasourceId?: string
): AgentContextItem => createAgentContextItem({
  id: `evidence:${ref.id}:${visibility}`,
  sourceType: "evidence-focus",
  sourceId: ref.id,
  groupId: `evidence:${ref.id}`,
  visibility,
  trust,
  retention: visibility === "model" ? "active" : "reference",
  priority: visibility === "model" ? 70 : 65,
  content,
  metadata: createAgentContextSourceMetadata({
    dedupeKeys: evidenceDedupeKeys(ref),
    exclusivityKey: `evidence:${ref.id}`,
    overlapKeys: evidenceOverlapKeys(ref),
    scope: {
      ...(datasourceId ?? ref.source.datasourceId
        ? { datasourceId: datasourceId ?? ref.source.datasourceId }
        : {}),
      sessionId: input.sessionId,
      userId: input.userId
    },
    sourceKind: "evidence-focus",
    sourceOwner: "user-selection"
  }, {
    atomic: false,
    evidenceId: ref.id,
    evidenceKind: ref.kind,
    evidenceLabel: ref.label,
    groupKind: "source"
  })
});

const evidenceText = (input: { body: Array<string | undefined>; ref: EvidenceRef }): string => [
  `<evidence_ref id="${escapeAttribute(input.ref.id)}" kind="${escapeAttribute(input.ref.kind)}"`,
  ` label="${escapeAttribute(input.ref.label)}">`,
  input.ref.summary ? `summary: ${escapeText(input.ref.summary)}` : undefined,
  ...input.body.filter((line): line is string => Boolean(line)),
  "</evidence_ref>"
].filter((line): line is string => Boolean(line)).join("\n");

const sqlAuditActivity = (audit: SqlAuditLogRecord): Record<string, unknown> => ({
  audit_log_id: audit.id,
  datasource_id: audit.datasource_id,
  status: audit.status,
  ...(audit.row_count !== undefined ? { row_count: audit.row_count } : {}),
  ...(audit.elapsed_ms !== undefined ? { elapsed_ms: audit.elapsed_ms } : {})
});

const toolEventActivity = (event: ResolvedToolEvent): Record<string, unknown> => ({
  ...(event.toolCallId ? { tool_call_id: event.toolCallId } : {}),
  ...(event.toolName ? { tool_name: event.toolName } : {}),
  ...(event.resultSeq !== undefined ? { result_seq: event.resultSeq } : {})
});

const requireEvidenceRun = (input: ResolveEvidenceReferenceContextInput, ref: EvidenceRef): RunRecord => {
  const runId = ref.runId;
  if (!runId) {
    throw new Error(`EVIDENCE_RUN_REQUIRED:${ref.id}`);
  }
  const run = input.metadataStore.runs.get({ user_id: input.userId, run_id: runId });
  assertSession(ref, run.session_id);
  return run;
};

type ResolvedToolEvent = {
  args?: unknown;
  result?: unknown;
  resultSeq?: number;
  toolCallId?: string;
  toolName?: string;
};

const findToolEvent = (events: RunEventRecord[], ref: EvidenceRef): ResolvedToolEvent | undefined => {
  const toolCallId = ref.source.toolCallId;
  let resolved: ResolvedToolEvent | undefined;
  for (const eventRecord of events) {
    const event = parseRecord(eventRecord.payload_json);
    const eventToolCallId = stringValue(event.toolCallId);
    if (toolCallId && eventToolCallId !== toolCallId) {
      continue;
    }
    if (!toolCallId && ref.source.eventId && stringValue(event.id) !== ref.source.eventId) {
      continue;
    }
    const toolName = stringValue(event.toolCallName);
    if (event.type === "TOOL_CALL_START" || event.type === "TOOL_CALL_END") {
      resolved = {
        ...resolved,
        ...(eventToolCallId ? { toolCallId: eventToolCallId } : {}),
        ...(toolName ? { toolName } : {}),
        ...(event.args !== undefined ? { args: event.args } : {}),
        ...(event.input !== undefined && resolved?.args === undefined ? { args: event.input } : {})
      };
    }
    if (event.type === "TOOL_CALL_RESULT") {
      resolved = {
        ...resolved,
        ...(eventToolCallId ? { toolCallId: eventToolCallId } : {}),
        ...(toolName ? { toolName } : {}),
        result: event.content,
        resultSeq: eventRecord.seq
      };
    }
  }
  return resolved;
};

const assertSession = (ref: EvidenceRef, sessionId: string): void => {
  if (ref.sessionId !== sessionId) {
    throw new Error(`EVIDENCE_SESSION_MISMATCH:${ref.id}`);
  }
};

const issueReasonFromError = (error: unknown): EvidenceResolutionIssue["reason"] => {
  const message = error instanceof Error ? error.message : String(error);
  if (message.includes("SESSION_MISMATCH")) return "session_mismatch";
  if (message.includes("not found") || message.includes("NOT_FOUND")) return "not_found";
  if (message.includes("RUN_REQUIRED") || message.includes("UNSUPPORTED")) return "unsupported";
  return "resolution_failed";
};

const evidenceDedupeKeys = (ref: EvidenceRef): string[] => [
  `evidence:${ref.id}`,
  ...(ref.source.artifactId ? [`artifact:${ref.source.artifactId}`] : []),
  ...(ref.source.auditLogId ? [`audit:${ref.source.auditLogId}`] : []),
  ...(ref.source.toolCallId ? [`tool-call:${ref.source.toolCallId}`] : [])
];

const evidenceOverlapKeys = (ref: EvidenceRef): string[] => [
  `evidence:${ref.id}`,
  ...(ref.source.artifactId ? [`artifact:${ref.source.artifactId}`] : []),
  ...(ref.source.auditLogId ? [`audit:${ref.source.auditLogId}`] : [])
];

const parseRecord = (value: string): Record<string, unknown> => {
  try {
    const parsed: unknown = JSON.parse(value);
    return isRecord(parsed) ? parsed : {};
  } catch {
    return {};
  }
};

const parseJsonValue = (value: string | undefined): unknown => {
  if (value === undefined) {
    return undefined;
  }
  try {
    return JSON.parse(value) as unknown;
  } catch {
    return undefined;
  }
};

const boundJson = (value: unknown, maxChars = 6000): string => boundText(safeJson(value), maxChars);

const boundText = (value: string, maxChars: number): string =>
  value.length <= maxChars ? value : `${value.slice(0, Math.max(0, maxChars - 80))}\n[truncated]`;

const safeJson = (value: unknown): string => {
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
};

const stringValue = (value: unknown): string | undefined =>
  typeof value === "string" && value.trim().length > 0 ? value : undefined;

const escapeAttribute = (value: string): string =>
  value.replaceAll("&", "&amp;").replaceAll('"', "&quot;").replaceAll("<", "&lt;");

const escapeText = (value: string): string =>
  value.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

type ParsedTable = { columns: string[]; rows: string[][] };

type SelectionFocus = { lines: string[]; replaceFullPreview: boolean };

/**
 * Translates a fine-grained `EvidenceSelection` into focus lines appended to an
 * `<evidence_ref>` body. For table selections that can be sliced, it emits only
 * the selected subset (and replaces the full preview) so the model receives the
 * user's chosen part rather than the whole artifact. Out-of-range or unparsable
 * selections degrade safely to the whole preview plus a selection note.
 */
export const resolveSelectionFocus = (
  selection: EvidenceSelection | undefined,
  preview: unknown,
  maxChars: number
): SelectionFocus | null => {
  if (!selection) {
    return null;
  }
  if (selection.mode === "text") {
    return {
      lines: ["selection=text", `selected_quote=${boundText(selection.quote, Math.min(maxChars, 1600))}`],
      replaceFullPreview: false
    };
  }
  const description = describeSelectionRange(selection);
  const table = parseTablePreview(preview);
  if (!table) {
    return { lines: [`selection=${description}`], replaceFullPreview: false };
  }
  const sliced = sliceTable(table, selection);
  if (!sliced) {
    return { lines: [`selection=${description}`], replaceFullPreview: false };
  }
  return {
    lines: [
      `selection=${description}`,
      `full_context: columns=[${table.columns.join(", ")}] total_rows=${table.rows.length}`,
      "selected_subset:",
      boundText(renderTableText(sliced), maxChars)
    ],
    replaceFullPreview: true
  };
};

const parseTablePreview = (preview: unknown): ParsedTable | null => {
  if (!isRecord(preview)) {
    return null;
  }
  const columns = Array.isArray(preview.columns)
    ? preview.columns.filter((column): column is string => typeof column === "string")
    : [];
  const rawRows = Array.isArray(preview.rows) ? preview.rows : [];
  if (columns.length === 0 || rawRows.length === 0) {
    return null;
  }
  const rows = rawRows
    .map((row) => {
      if (Array.isArray(row)) {
        return row.map((cell) => (cell === null || cell === undefined ? "" : String(cell)));
      }
      if (isRecord(row)) {
        return columns.map((column) => {
          const value = row[column];
          return value === null || value === undefined ? "" : String(value);
        });
      }
      return [];
    })
    .filter((row) => row.length > 0);
  if (rows.length === 0) {
    return null;
  }
  return { columns, rows };
};

const clampIndex = (value: number, max: number): number =>
  Math.max(0, Math.min(Number.isFinite(value) ? Math.trunc(value) : 0, max));

const sliceTable = (
  table: ParsedTable,
  selection: Extract<EvidenceSelection, { mode: "cells" | "rows" | "cols" }>
): ParsedTable | null => {
  const lastRow = table.rows.length - 1;
  const lastCol = table.columns.length - 1;
  const minR = Math.min(selection.range.r0, selection.range.r1);
  const maxR = Math.max(selection.range.r0, selection.range.r1);
  const minC = Math.min(selection.range.c0, selection.range.c1);
  const maxC = Math.max(selection.range.c0, selection.range.c1);
  // No overlap with the table at all → caller degrades to the whole preview.
  if (maxR < 0 || maxC < 0 || minR > lastRow || minC > lastCol) {
    return null;
  }
  const r0 = clampIndex(minR, lastRow);
  const r1 = clampIndex(maxR, lastRow);
  const c0 = clampIndex(minC, lastCol);
  const c1 = clampIndex(maxC, lastCol);
  const columns = table.columns.slice(c0, c1 + 1);
  const rows = table.rows.slice(r0, r1 + 1).map((row) => row.slice(c0, c1 + 1));
  if (columns.length === 0 || rows.length === 0) {
    return null;
  }
  return { columns, rows };
};

const renderTableText = (table: ParsedTable): string => {
  const header = table.columns.join(" | ");
  const body = table.rows.map((row) => row.join(" | ")).join("\n");
  return `${header}\n${body}`;
};

const columnLetter = (index: number): string => {
  let value = Math.max(0, Math.trunc(index));
  let label = "";
  do {
    label = String.fromCharCode(65 + (value % 26)) + label;
    value = Math.floor(value / 26) - 1;
  } while (value >= 0);
  return label;
};

const describeSelectionRange = (
  selection: Extract<EvidenceSelection, { mode: "cells" | "rows" | "cols" }>
): string => {
  const { range } = selection;
  if (selection.mode === "rows") {
    return `rows ${range.r0 + 1}-${range.r1 + 1}`;
  }
  if (selection.mode === "cols") {
    const names = selection.columns && selection.columns.length > 0 ? ` (${selection.columns.join(", ")})` : "";
    return `cols ${columnLetter(range.c0)}-${columnLetter(range.c1)}${names}`;
  }
  return `${columnLetter(range.c0)}${range.r0 + 1}:${columnLetter(range.c1)}${range.r1 + 1}`;
};
