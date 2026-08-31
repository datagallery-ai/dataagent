import type {
  EvidenceKind,
  EvidenceRef,
  EvidenceSelection,
} from "@datafoundry/contracts";

import type { DataArtifact } from "./data-task-state";
import type { LiveAudit, LiveRun } from "./live-run-state";
import { buildTraceTimeline, type TraceEntry } from "./trace-timeline";

export type EvidenceOrigin = "artifact" | "audit" | "trace" | "knowledge";

export type EvidenceCard = {
  ref: EvidenceRef;
  title: string;
  subtitle?: string;
  preview?: string;
  origin: EvidenceOrigin;
  sortKey: number;
};

/** Builds the frontend evidence catalog from the live run state without mutating it. */
export function buildEvidenceCardsFromLiveRun(liveRun: LiveRun, sessionId?: string | null): EvidenceCard[] {
  const cards: EvidenceCard[] = [];
  const seen = new Set<string>();
  const runId = liveRun.runId;
  const refSessionId = sessionId ?? "";

  const addCard = (card: EvidenceCard): void => {
    if (!card.ref.id || seen.has(card.ref.id)) return;
    seen.add(card.ref.id);
    cards.push(card);
  };

  liveRun.artifacts.forEach((artifact, index) => {
    addCard(artifactEvidenceCard(artifact, refSessionId, runId, index));
  });

  const traceEntries = buildTraceTimeline(liveRun);
  const usedAuditIds = new Set<string>();
  traceEntries.forEach((entry, index) => {
    if (entry.kind !== "tool") return;
    const audit = entryIsSql(entry) ? pickAuditForSql(liveRun.audits, usedAuditIds, entry.scannedRows) : undefined;
    if (audit) usedAuditIds.add(audit.id);
    for (const card of traceEvidenceCards(entry, refSessionId, runId, audit, index)) {
      addCard(card);
    }
  });

  return cards.sort((left, right) => left.sortKey - right.sortKey || left.title.localeCompare(right.title));
}

/** Adds or removes an evidence ref by its stable id. */
export function toggleEvidenceRef(refs: readonly EvidenceRef[], ref: EvidenceRef): EvidenceRef[] {
  return refs.some((entry) => entry.id === ref.id)
    ? refs.filter((entry) => entry.id !== ref.id)
    : uniqueEvidenceRefs([...refs, ref]);
}

/** Removes one evidence ref by id. */
export function removeEvidenceRef(refs: readonly EvidenceRef[], id: string): EvidenceRef[] {
  return refs.filter((entry) => entry.id !== id);
}

/** Removes duplicate evidence refs while preserving first-seen order. */
export function uniqueEvidenceRefs(refs: readonly EvidenceRef[]): EvidenceRef[] {
  const seen = new Set<string>();
  const uniqueRefs: EvidenceRef[] = [];
  for (const ref of refs) {
    if (!ref.id || seen.has(ref.id)) continue;
    seen.add(ref.id);
    uniqueRefs.push(ref);
  }
  return uniqueRefs;
}

/** Full label for tooltips and screen readers. */
export function evidenceChipTooltip(ref: EvidenceRef): string {
  const base = `${evidenceKindLabel(ref.kind)}: ${ref.label}`;
  const selection = ref.source.selection;
  return selection ? `${base} · ${describeEvidenceSelection(selection)}` : base;
}

/** Formats a compact label suitable for input chips, including any sub-selection. */
export function evidenceChipLabel(ref: EvidenceRef): string {
  const kind = evidenceKindLabel(ref.kind);
  const name = compactEvidenceLabel(ref.label, ref.kind);
  const selection = ref.source.selection;
  const base = `${kind} · ${name}`;
  return selection ? `${base} · ${describeEvidenceSelection(selection, { compact: true })}` : base;
}

/** Human-readable description of a fine-grained selection, e.g. "B2:D9 (12 cells)". */
export function describeEvidenceSelection(
  selection: EvidenceSelection,
  options?: { compact?: boolean; maxQuoteLength?: number },
): string {
  const compact = options?.compact ?? false;
  const maxQuoteLength = options?.maxQuoteLength ?? (compact ? 20 : 48);
  if (selection.mode === "text") {
    const quote = selection.quote.replace(/\s+/gu, " ").trim();
    const clipped = quote.length > maxQuoteLength ? `${quote.slice(0, maxQuoteLength)}…` : quote;
    return `"${clipped}"`;
  }
  const { range } = selection;
  const rowCount = Math.abs(range.r1 - range.r0) + 1;
  const colCount = Math.abs(range.c1 - range.c0) + 1;
  if (selection.mode === "rows") {
    if (compact) {
      return rowCount === 1 ? `row ${range.r0 + 1}` : `rows ${range.r0 + 1}–${range.r1 + 1}`;
    }
    return rowCount === 1 ? `row ${range.r0 + 1}` : `rows ${range.r0 + 1}–${range.r1 + 1} (${rowCount} rows)`;
  }
  if (selection.mode === "cols") {
    if (selection.columns && selection.columns.length > 0) {
      const limit = compact ? 2 : 3;
      const names = selection.columns.slice(0, limit).join(", ");
      const suffix = selection.columns.length > limit ? `, +${selection.columns.length - limit}` : "";
      return compact ? `cols ${names}${suffix}` : `cols ${names}${suffix}`;
    }
    if (compact) {
      return colCount === 1
        ? `col ${columnLetter(range.c0)}`
        : `cols ${columnLetter(range.c0)}–${columnLetter(range.c1)}`;
    }
    return colCount === 1
      ? `col ${columnLetter(range.c0)}`
      : `cols ${columnLetter(range.c0)}–${columnLetter(range.c1)} (${colCount} cols)`;
  }
  const start = `${columnLetter(range.c0)}${range.r0 + 1}`;
  const end = `${columnLetter(range.c1)}${range.r1 + 1}`;
  if (compact) {
    return start === end ? start : `${start}:${end}`;
  }
  const cellCount = rowCount * colCount;
  return start === end ? `${start} (1 cell)` : `${start}:${end} (${cellCount} cells)`;
}

const SQL_RESULT_LABEL = /^SQL result\s+[0-9a-f-]{8,}(?:\.[a-z0-9]+)?$/iu;

function compactEvidenceLabel(label: string, kind: EvidenceKind): string {
  const trimmed = label.trim();
  if (!trimmed) return evidenceKindLabel(kind);

  if (SQL_RESULT_LABEL.test(trimmed)) {
    const extension = trimmed.match(/(\.[a-z0-9]+)$/iu)?.[1];
    return extension ? `SQL result${extension}` : "SQL result";
  }

  const basename = trimmed.includes("/") ? (trimmed.split("/").pop() ?? trimmed) : trimmed;
  if (basename.length <= 36) return basename;
  return `${basename.slice(0, 33)}…`;
}

/** Converts a 0-based column index to a spreadsheet-style letter (0 -> A, 26 -> AA). */
export function columnLetter(index: number): string {
  let value = Math.max(0, index);
  let label = "";
  do {
    label = String.fromCharCode(65 + (value % 26)) + label;
    value = Math.floor(value / 26) - 1;
  } while (value >= 0);
  return label;
}

/** Builds the whole-artifact evidence reference (id and source) for an artifact. */
export function artifactEvidenceRef(
  artifact: DataArtifact,
  sessionId: string,
  runId?: string,
): EvidenceRef {
  const kind = artifactEvidenceKind(artifact);
  return {
    id: `artifact:${artifact.id}`,
    kind,
    label: artifact.title,
    summary: artifact.summary,
    sessionId,
    ...(runId ? { runId } : {}),
    source: {
      artifactId: artifact.id,
      ...(artifact.fileId ? { fileId: artifact.fileId } : {}),
      ...(artifact.createdByEventId ? { eventId: artifact.createdByEventId } : {}),
    },
  };
}

export function evidenceKindLabel(kind: EvidenceKind): string {
  switch (kind) {
    case "table":
      return "Table";
    case "chart":
      return "Chart";
    case "report":
      return "Report";
    case "file":
      return "File";
    case "sql":
      return "SQL";
    case "schema":
      return "Schema";
    case "preview":
      return "Preview";
    case "knowledge":
      return "Knowledge";
    case "step":
      return "Step";
  }
}

const artifactEvidenceCard = (
  artifact: DataArtifact,
  sessionId: string,
  runId: string | undefined,
  index: number,
): EvidenceCard => {
  const kind = artifactEvidenceKind(artifact);
  const ref = artifactEvidenceRef(artifact, sessionId, runId);
  return {
    ref,
    title: `${evidenceKindLabel(kind)} · ${artifact.title}`,
    subtitle: artifact.summary || undefined,
    preview: artifactPreviewText(artifact),
    origin: "artifact",
    sortKey: artifact.recordedAtMs ?? 10_000 + index,
  };
};

const artifactEvidenceKind = (artifact: DataArtifact): EvidenceKind => {
  if (artifact.detail?.type === "dataset" || artifact.type === "dataset" || artifact.type === "sql") return "table";
  if (artifact.detail?.type === "chart" || artifact.type === "chart") return "chart";
  if (artifact.detail?.type === "report" || artifact.type === "report") return "report";
  if (artifact.detail?.type === "file" || artifact.type === "file" || artifact.kind === "file") return "file";
  return "report";
};

const artifactPreviewText = (artifact: DataArtifact): string | undefined => {
  if (artifact.detail?.type === "dataset") {
    const columns = artifact.detail.columns.slice(0, 6).join(", ");
    return `Columns: ${columns}; sample rows: ${artifact.detail.rows.length}`;
  }
  if (artifact.detail?.type === "chart") {
    const pointCount = (artifact.detail.series ?? []).reduce((sum, series) => sum + series.points.length, 0);
    return `${artifact.detail.chartType ?? "chart"} · ${pointCount || artifact.detail.points.length} points`;
  }
  if (artifact.detail?.type === "file") {
    return artifact.detail.path;
  }
  return artifact.summary || undefined;
};

const traceEvidenceCards = (
  entry: TraceEntry,
  sessionId: string,
  runId: string | undefined,
  audit: LiveAudit | undefined,
  index: number,
): EvidenceCard[] => {
  const cards: EvidenceCard[] = [stepEvidenceCard(entry, sessionId, runId, index)];
  if (entryIsSql(entry)) {
    cards.push(sqlEvidenceCard(entry, sessionId, runId, audit, index));
  }
  if (entry.schemaTables && entry.schemaTables.length > 0) {
    cards.push(schemaEvidenceCard(entry, sessionId, runId, index));
  }
  if (entry.toolName === "preview_table") {
    cards.push(previewEvidenceCard(entry, sessionId, runId, index));
  }
  if (entry.toolName === "retrieve_knowledge") {
    cards.push(knowledgeEvidenceCard(entry, sessionId, runId, index));
  }
  return cards;
};

const stepEvidenceCard = (
  entry: TraceEntry,
  sessionId: string,
  runId: string | undefined,
  index: number,
): EvidenceCard => {
  const id = entry.toolCallId ? `step:${entry.toolCallId}` : `step:${entry.eventId ?? entry.id}`;
  return {
    ref: {
      id,
      kind: "step",
      label: entry.title,
      summary: entry.summary,
      sessionId,
      ...(runId ? { runId } : {}),
      source: {
        ...(entry.toolCallId ? { toolCallId: entry.toolCallId } : {}),
        ...(entry.eventId ? { eventId: entry.eventId } : {}),
      },
    },
    title: `Step · ${entry.title}`,
    subtitle: entry.summary,
    preview: entry.sql ?? entry.rawResult,
    origin: "trace",
    sortKey: (entry.tsMs ?? 20_000 + index) + 0.1,
  };
};

const sqlEvidenceCard = (
  entry: TraceEntry,
  sessionId: string,
  runId: string | undefined,
  audit: LiveAudit | undefined,
  index: number,
): EvidenceCard => {
  const id = audit?.id ? `sql-audit:${audit.id}` : `sql:${entry.toolCallId ?? entry.eventId ?? entry.id}`;
  const label = entry.title || "Run query";
  const datasourceId = entry.datasourceId ?? audit?.datasourceId;
  return {
    ref: {
      id,
      kind: "sql",
      label,
      summary: entry.summary,
      sessionId,
      ...(runId ? { runId } : {}),
      source: {
        ...(audit?.id ? { auditLogId: audit.id } : {}),
        ...(entry.toolCallId ? { toolCallId: entry.toolCallId } : {}),
        ...(entry.eventId ? { eventId: entry.eventId } : {}),
        ...(datasourceId ? { datasourceId } : {}),
      },
    },
    title: `SQL · ${label}`,
    subtitle: entry.summary,
    preview: entry.sql,
    origin: audit ? "audit" : "trace",
    sortKey: (entry.tsMs ?? 20_000 + index) + 0.2,
  };
};

const schemaEvidenceCard = (
  entry: TraceEntry,
  sessionId: string,
  runId: string | undefined,
  index: number,
): EvidenceCard => {
  const tables = entry.schemaTables ?? [];
  const id = `schema:${entry.toolCallId ?? entry.eventId ?? entry.id}`;
  return {
    ref: {
      id,
      kind: "schema",
      label: tables.length === 1 ? tables[0]?.name ?? "Schema" : `${tables.length} tables`,
      summary: entry.summary,
      sessionId,
      ...(runId ? { runId } : {}),
      source: {
        ...(entry.toolCallId ? { toolCallId: entry.toolCallId } : {}),
        ...(entry.eventId ? { eventId: entry.eventId } : {}),
        ...(tables.length === 1 && tables[0]?.name ? { tableName: tables[0].name } : {}),
      },
    },
    title: `Schema · ${tables.length} tables`,
    subtitle: entry.summary,
    preview: tables.map((table) => table.name).slice(0, 8).join(", "),
    origin: "trace",
    sortKey: (entry.tsMs ?? 20_000 + index) + 0.3,
  };
};

const previewEvidenceCard = (
  entry: TraceEntry,
  sessionId: string,
  runId: string | undefined,
  index: number,
): EvidenceCard => ({
  ref: {
    id: `preview:${entry.toolCallId ?? entry.eventId ?? entry.id}`,
    kind: "preview",
    label: entry.title,
    summary: entry.summary,
    sessionId,
    ...(runId ? { runId } : {}),
    source: {
      ...(entry.toolCallId ? { toolCallId: entry.toolCallId } : {}),
      ...(entry.eventId ? { eventId: entry.eventId } : {}),
    },
  },
  title: `Preview · ${entry.title}`,
  subtitle: entry.summary,
  preview: entry.rawResult,
  origin: "trace",
  sortKey: (entry.tsMs ?? 20_000 + index) + 0.4,
});

const knowledgeEvidenceCard = (
  entry: TraceEntry,
  sessionId: string,
  runId: string | undefined,
  index: number,
): EvidenceCard => ({
  ref: {
    id: `knowledge:${entry.toolCallId ?? entry.eventId ?? entry.id}`,
    kind: "knowledge",
    label: entry.title,
    summary: entry.summary,
    sessionId,
    ...(runId ? { runId } : {}),
    source: {
      ...(entry.toolCallId ? { toolCallId: entry.toolCallId } : {}),
      ...(entry.eventId ? { eventId: entry.eventId } : {}),
    },
  },
  title: `Knowledge · ${entry.title}`,
  subtitle: entry.summary,
  preview: entry.rawResult,
  origin: "knowledge",
  sortKey: (entry.tsMs ?? 20_000 + index) + 0.5,
});

const entryIsSql = (entry: TraceEntry): boolean =>
  entry.toolName === "run_sql_readonly" || Boolean(entry.sql);

const pickAuditForSql = (
  audits: LiveAudit[],
  usedAuditIds: Set<string>,
  scannedRows?: number,
): LiveAudit | undefined => {
  if (scannedRows !== undefined && scannedRows > 0) {
    const matched = audits.find((audit) => !usedAuditIds.has(audit.id) && audit.rowCount === scannedRows);
    if (matched) return matched;
  }
  return audits.find((audit) => !usedAuditIds.has(audit.id));
};
