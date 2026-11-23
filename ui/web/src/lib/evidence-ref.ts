/** A rectangular cell range in a table preview, using 0-based inclusive indices. */
export type EvidenceCellRange = {
  r0: number;
  c0: number;
  r1: number;
  c1: number;
};

export type EvidenceKind =
  | "table"
  | "chart"
  | "report"
  | "file"
  | "sql"
  | "schema"
  | "preview"
  | "knowledge"
  | "step";

/**
 * Optional sub-selection inside an evidence object. Present when a user references
 * only part of an artifact (a table region, or a highlighted text span) rather than
 * the whole thing. The backend may ignore this and treat the reference as whole.
 */
export type EvidenceSelection =
  | { mode: "cells" | "rows" | "cols"; range: EvidenceCellRange; columns?: string[] }
  | { mode: "text"; quote: string; offset?: number };

export type EvidenceRefSource = {
  artifactId?: string;
  toolCallId?: string;
  eventId?: string;
  auditLogId?: string;
  fileId?: string;
  datasourceId?: string;
  tableName?: string;
  documentId?: string;
  chunkId?: string;
  /** Fine-grained selection within the referenced object; omitted means whole-object. */
  selection?: EvidenceSelection;
};

export type EvidenceRef = {
  id: string;
  kind: EvidenceKind;
  label: string;
  summary?: string;
  sessionId: string;
  runId?: string;
  source: EvidenceRefSource;
};

export type EvidenceResolutionIssue = {
  id: string;
  reason:
    | "invalid"
    | "not_found"
    | "session_mismatch"
    | "run_mismatch"
    | "unsupported"
    | "resolution_failed";
  message?: string;
};

export type EvidenceResolutionDiagnostics = {
  accepted: string[];
  dropped: EvidenceResolutionIssue[];
};
