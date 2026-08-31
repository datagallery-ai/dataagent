import {
  clampRightPanelWidth,
  clampLeftPanelWidth,
  LEFT_PANEL_DEFAULT_WIDTH,
  RIGHT_PANEL_DEFAULT_WIDTH,
} from "./workspace-layout";
import type { EvidenceRef } from "@datafoundry/contracts";
import type { TranslateFn } from "../../i18n/types";

export type ArtifactKind = "chart" | "csv" | "memo" | "dashboard" | "file";
export type DataArtifactType = "dataset" | "chart" | "sql" | "report" | "file";
export type ChartArtifactType = "bar" | "line" | "pie";
export type ChartArtifactPoint = { label: string; value: number };
export type ChartArtifactSeries = {
  name: string;
  points: ChartArtifactPoint[];
};

/**
 * Tool-agnostic data step kinds. The console is organized around the data-task
 * lifecycle, not around specific SQL tools, so any backend data operation maps
 * to one of these. Today the backend only emits `inspect` (inspect_schema) and
 * `query` (run_sql_readonly); the remaining kinds light up as the backend ships
 * more data tools (see docs/engineering/2026-06-25-backend-requirements.md). Unknown
 * tools degrade to `other` instead of masquerading as a schema inspection.
 */
export type DataStepKind =
  | "inspect" // structure / schema inspection
  | "query" // SQL / data query
  | "transform" // data shaping / cleaning
  | "fetch" // table / row fetch
  | "visualize" // chart / visualization
  | "knowledge" // RAG / knowledge retrieval
  | "other"; // any other backend data operation

/** Maps a backend tool name to a tool-agnostic data step kind. */
export function dataStepKindForTool(toolName?: string): DataStepKind {
  switch (toolName) {
    case "inspect_schema":
    case "list_data_sources":
      return "inspect";
    case "run_sql_readonly":
      return "query";
    case "preview_table":
      return "fetch";
    case "retrieve_knowledge":
      return "knowledge";
    default:
      return "other";
  }
}

/** Short human label for a data step kind (used in trace/console chips). */
export function dataStepLabel(kind: DataStepKind, t?: TranslateFn): string {
  if (t) {
    return t(`steps.${kind}`);
  }
  switch (kind) {
    case "inspect":
      return "Schema";
    case "query":
      return "Query";
    case "transform":
      return "Transform";
    case "fetch":
      return "Fetch";
    case "visualize":
      return "Visualize";
    case "knowledge":
      return "Knowledge";
    case "other":
      return "Data operation";
  }
}

const toolDisplayTitles: Record<string, string> = {
  list_data_sources: "List data sources",
  inspect_schema: "Inspect data source schema",
  preview_table: "Preview table",
  run_sql_readonly: "Run SQL query",
  retrieve_knowledge: "Retrieve knowledge",
  read_file: "Read file",
  edit_file: "Edit file",
  write_file: "Write file",
  list_files: "Browse workspace files",
  grep: "Search file contents",
  mkdir: "Create directory",
  file_stat: "Get file info",
  execute_command: "Run command",
  promote_workspace_file: "Promote workspace file",
  task_write: "Write task plan",
  task_update: "Update task",
  task_complete: "Complete task",
  task_check: "Check task status",
  ask_user: "Ask user",
  submit_plan: "Submit plan",
};

/** Human-readable title for a backend tool name (console / trace / progress). */
export function toolDisplayTitle(toolName?: string, t?: TranslateFn): string {
  if (!toolName || toolName === "tool" || toolName === "unknown") {
    return t ? t("tools.runTool") : "Run tool";
  }
  const trimmed = toolName.trim();
  if (/[\u4e00-\u9fff]/.test(trimmed)) {
    return trimmed;
  }
  const lookupName = trimmed.startsWith("mcp__")
    ? trimmed.split("__").slice(2).join("__")
    : trimmed;
  if (t) {
    for (const key of [`tools.${lookupName}`, `tools.${trimmed}`]) {
      const translated = t(key);
      if (translated !== key) return translated;
    }
  }
  return toolDisplayTitles[lookupName] ?? toolDisplayTitles[trimmed] ?? lookupName;
}

export type ArtifactDetail =
  | {
      type: "sql";
      sql: string;
      scannedRows: number;
      durationMs: number;
    }
  | {
      type: "dataset";
      columns: string[];
      rows: string[][];
    }
  | {
      type: "chart";
      chartType?: ChartArtifactType;
      unit?: string;
      points: ChartArtifactPoint[];
      series?: ChartArtifactSeries[];
    }
  | {
      type: "report";
      sections: Array<{ heading: string; body: string }>;
    }
  | {
      type: "file";
      path: string;
      size?: number;
      mtime?: string;
      tool?: string;
      content?: string;
    };

export interface DataArtifact {
  id: string;
  title: string;
  kind: ArtifactKind;
  type?: DataArtifactType;
  summary: string;
  version?: string;
  /** FileAssetRef id behind file-backed artifacts, when the backend provides one. */
  fileId?: string;
  /** Backend download URL for file-backed artifacts, when available. */
  downloadUrl?: string;
  /** Workspace-relative source path used for artifact de-duplication and restore linking. */
  sourcePath?: string;
  createdByEventId?: string;
  /**
   * Authoritative producing tool_call_id from the backend artifact contract (R-018).
   * When present it is the sole source of truth for linking; no heuristics are used.
   */
  createdByToolCallId?: string;
  detail?: ArtifactDetail;
  /** When true, full preview can be fetched via artifact REST API. */
  previewAvailable?: boolean;
  /** Milliseconds since epoch when the artifact event was received. */
  recordedAtMs?: number;
  /** Logical key for session-file outputs (`session_file:<path>`). Present when artifact was auto-ingested. */
  logicalKey?: string;
  /** Latest version number. Present when emitted by auto-ingest or loaded via restore API. */
  versionCount?: number;
}

export interface TimelineStep {
  id: string;
  label: string;
  /** The timeline event whose appearance marks this step complete. */
  linkedEventId: string;
}

export interface SchemaTable {
  name: string;
  description: string;
  fields: string[];
}

/** Schema/inspect step payload. */
export interface SchemaStepPayload {
  tables: SchemaTable[];
}

/** SQL/query step payload. */
export interface QueryStepPayload {
  question: string;
  sql: string;
  scannedRows: number;
  durationMs: number;
  errorMessage?: string;
}

/** Generic data step payload for any non-SQL/non-schema operation. */
export interface GenericStepPayload {
  description: string;
  rawResult?: string;
}

export type DataStepPayload =
  | SchemaStepPayload
  | QueryStepPayload
  | GenericStepPayload;

export type ActivityStepStatus = "running" | "completed" | "failed";

export interface TimelineEvent {
  id: string;
  kind: DataStepKind;
  ts: string;
  title: string;
  summary: string;
  /** Backend tool name behind this step, when known. */
  toolName?: string;
  /** Backend ACTIVITY STEP `step_id` (e.g. sql-1), when distinct from AG-UI toolCallId. */
  stepId?: string;
  /** Latest status from ACTIVITY_SNAPSHOT STEP content.status. */
  activityStatus?: ActivityStepStatus;
  /** The agent's reasoning shown above the action — the ReAct signature. */
  thought?: string;
  artifactIds?: string[];
  payload: DataStepPayload;
}

/** An empty payload sized to the step kind, for steps not yet resolved. */
export function emptyStepPayload(kind: DataStepKind): DataStepPayload {
  if (kind === "query") {
    return { question: "", sql: "", scannedRows: 0, durationMs: 0 };
  }
  if (kind === "inspect") {
    return { tables: [] };
  }
  return { description: "" };
}

/**
 * Client-side chat session. Each session owns a `threadId`; the UI registers a
 * session-local CopilotKit agent id for that thread and routes it to dataFoundry.
 */
/** Per-session disabled resource ids (store "off" list; default = all enabled). */
export type SessionDisabledMap = Record<
  Exclude<WorkspaceConfigKind, "llm">,
  string[]
>;

export interface SessionConfigOverride {
  disabled: SessionDisabledMap;
}

export type ChatSessionTitleSource = "default" | "auto-snippet" | "llm" | "user";

export interface ChatSession {
  id: string;
  threadId: string;
  title: string;
  titleSource?: ChatSessionTitleSource;
  createdAt: number;
  updatedAt?: number;
  lastMessageAt?: number;
  /** Pinned sessions stay at the top of the sidebar list. */
  pinned?: boolean;
  /** When the session was pinned; used to order multiple pinned sessions. */
  pinnedAt?: number;
  /** Per-session resource enablement; omitted = all workspace resources enabled. */
  config?: SessionConfigOverride;
}

const SESSIONS_STORAGE_KEY = "data-tasks:sessions:v2";

function scopedStorageKey(
  baseKey: string,
  scopedSuffix: string,
  scopeKey?: string | null,
): string {
  return scopeKey ? `data-tasks:${scopeKey}:${scopedSuffix}` : baseKey;
}

const SCOPED_SESSIONS_STORAGE_SUFFIX = "sessions:v3";
const SCOPED_ACTIVE_LLM_STORAGE_SUFFIX = "active-llm:v3";

/**
 * Client-side id generator that works outside secure contexts.
 * `crypto.randomUUID` is unavailable (or throws) on plain HTTP hosts like LAN IPs.
 */
export function createClientId(prefix = "id"): string {
  try {
    if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
      return crypto.randomUUID();
    }
  } catch {
    // Non-secure context or restricted Web Crypto.
  }
  return `${prefix}-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function newThreadId(): string {
  return createClientId("thread");
}

export function createChatSession(title = "New data task"): ChatSession {
  const threadId = newThreadId();
  const now = Date.now();
  return {
    id: threadId,
    threadId,
    title,
    titleSource: "default",
    createdAt: now,
    updatedAt: now,
  };
}

function normalizeSessionTitle(title: string): string {
  return title.replace(/\s+/g, " ").trim();
}

export function deriveSnippetTitle(text: string): string {
  const normalized = normalizeSessionTitle(text);
  if (!normalized) return "New data task";
  const maxChars = 23;
  return normalized.length > maxChars
    ? `${normalized.slice(0, maxChars)}…`
    : normalized;
}

export function deleteChatSession(
  sessions: ChatSession[],
  id: string,
): ChatSession[] {
  return sessions.filter((session) => session.id !== id);
}

export function togglePinChatSession(
  sessions: ChatSession[],
  id: string,
): ChatSession[] {
  const now = Date.now();
  return sessions.map((session) => {
    if (session.id !== id) return session;
    const nextPinned = !session.pinned;
    return {
      ...session,
      pinned: nextPinned,
      pinnedAt: nextPinned ? now : undefined,
    };
  });
}

export function sortChatSessions(sessions: ChatSession[]): ChatSession[] {
  return [...sessions].sort((left, right) => {
    const leftPinned = Boolean(left.pinned);
    const rightPinned = Boolean(right.pinned);
    if (leftPinned !== rightPinned) {
      return Number(rightPinned) - Number(leftPinned);
    }
    if (leftPinned && rightPinned) {
      return (right.pinnedAt ?? 0) - (left.pinnedAt ?? 0);
    }
    return right.createdAt - left.createdAt;
  });
}

export function renameChatSession(
  sessions: ChatSession[],
  id: string,
  title: string,
): ChatSession[] {
  const normalized = normalizeSessionTitle(title);
  if (!normalized) return sessions;
  return sessions.map((session) =>
    session.id === id
      ? { ...session, title: normalized, titleSource: "user" }
      : session,
  );
}

export function applyAutoTitle(
  sessions: ChatSession[],
  id: string,
  title: string,
  source: Exclude<ChatSessionTitleSource, "default" | "user">,
): ChatSession[] {
  const normalized = normalizeSessionTitle(title);
  if (!normalized) return sessions;
  return sessions.map((session) => {
    if (
      (session.id !== id && session.threadId !== id) ||
      session.titleSource === "user"
    ) {
      return session;
    }
    return { ...session, title: normalized, titleSource: source };
  });
}

export type ServerChatSessionDto = {
  id?: string;
  sessionId?: string;
  threadId?: string;
  title?: string;
  titleSource?: ChatSessionTitleSource | string;
  createdAt?: string;
  updatedAt?: string;
  lastMessageAt?: string;
};

function timestampFromIso(value: string | undefined, fallback: number): number {
  if (!value) return fallback;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function normalizeTitleSource(value: string | undefined): ChatSessionTitleSource {
  if (value === "auto-snippet" || value === "llm" || value === "user") return value;
  // Backend persists non-LLM auto titles as "fallback"; treat like a snippet.
  if (value === "fallback") return "auto-snippet";
  return "default";
}

export function serverSessionDtoToChatSession(dto: ServerChatSessionDto): ChatSession {
  const threadId = dto.threadId ?? dto.sessionId ?? dto.id ?? newThreadId();
  const createdAt = timestampFromIso(dto.createdAt, Date.now());
  const updatedAt = timestampFromIso(dto.updatedAt, createdAt);
  return {
    id: dto.id ?? dto.sessionId ?? threadId,
    threadId,
    title: normalizeSessionTitle(dto.title ?? "") || "New data task",
    titleSource: normalizeTitleSource(dto.titleSource),
    createdAt,
    updatedAt,
    lastMessageAt: timestampFromIso(dto.lastMessageAt, updatedAt),
  };
}

export function mergeServerChatSessions(
  localSessions: ChatSession[],
  serverSessions: ServerChatSessionDto[],
): ChatSession[] {
  const localById = new Map<string, ChatSession>();
  const localByThreadId = new Map<string, ChatSession>();
  const matchedLocalIds = new Set<string>();
  for (const session of localSessions) {
    localById.set(session.id, session);
    localByThreadId.set(session.threadId, session);
  }

  const mergedServer = serverSessions.map((dto) => {
    const server = serverSessionDtoToChatSession(dto);
    const local = localById.get(server.id) ?? localByThreadId.get(server.threadId);
    if (!local) return server;
    matchedLocalIds.add(local.id);
    return {
      ...server,
      pinned: local.pinned,
      pinnedAt: local.pinnedAt,
      config: local.config,
    };
  });
  const localOnly = localSessions.filter((session) => !matchedLocalIds.has(session.id));
  return sortChatSessions(dedupeChatSessions([...mergedServer, ...localOnly]));
}

export function dedupeChatSessions(sessions: ChatSession[]): ChatSession[] {
  const seenIds = new Set<string>();
  const seenThreadIds = new Set<string>();
  const unique: ChatSession[] = [];

  for (const session of sessions) {
    if (seenIds.has(session.id) || seenThreadIds.has(session.threadId)) {
      continue;
    }
    seenIds.add(session.id);
    seenThreadIds.add(session.threadId);
    unique.push(session);
  }

  return unique;
}

export function loadChatSessions(scopeKey?: string | null): ChatSession[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(
      scopedStorageKey(SESSIONS_STORAGE_KEY, SCOPED_SESSIONS_STORAGE_SUFFIX, scopeKey),
    );
    if (!raw) return [];
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) return [];
    return sortChatSessions(dedupeChatSessions(parsed.filter(isChatSession)));
  } catch {
    return [];
  }
}

export function persistChatSessions(sessions: ChatSession[], scopeKey?: string | null): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(
      scopedStorageKey(SESSIONS_STORAGE_KEY, SCOPED_SESSIONS_STORAGE_SUFFIX, scopeKey),
      JSON.stringify(sessions),
    );
  } catch {
    // Ignore quota / serialization errors — sessions stay in-memory.
  }
}

function isStringArray(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((entry) => typeof entry === "string");
}

function isSessionDisabledMap(value: unknown): value is SessionDisabledMap {
  if (typeof value !== "object" || value === null) return false;
  const record = value as Record<string, unknown>;
  return (
    isStringArray(record.db) &&
    isStringArray(record.kb) &&
    isStringArray(record.mcp) &&
    isStringArray(record.skill)
  );
}

function isSessionConfigOverride(value: unknown): value is SessionConfigOverride {
  if (typeof value !== "object" || value === null) return false;
  const record = value as Record<string, unknown>;
  return isSessionDisabledMap(record.disabled);
}

function isChatSession(value: unknown): value is ChatSession {
  if (typeof value !== "object" || value === null) return false;
  const record = value as Record<string, unknown>;
  if (
    typeof record.id !== "string" ||
    typeof record.threadId !== "string" ||
    typeof record.title !== "string" ||
    typeof record.createdAt !== "number"
  ) {
    return false;
  }
  if (record.config !== undefined && !isSessionConfigOverride(record.config)) {
    return false;
  }
  if (record.pinned !== undefined && typeof record.pinned !== "boolean") {
    return false;
  }
  if (record.pinnedAt !== undefined && typeof record.pinnedAt !== "number") {
    return false;
  }
  if (
    record.titleSource !== undefined &&
    record.titleSource !== "default" &&
    record.titleSource !== "auto-snippet" &&
    record.titleSource !== "llm" &&
    record.titleSource !== "user"
  ) {
    return false;
  }
  return true;
}

/** Client-side skill catalog. Backend skill execution is not wired yet. */
export interface DataSkill {
  id: string;
  name: string;
  description: string;
}

export type SkillPackageFormat = "skill-md" | "zip" | "builtin";

export interface ParsedSkillPackage {
  fileName: string;
  format: Exclude<SkillPackageFormat, "builtin">;
  name: string;
  description: string;
  version: string;
  allowedTools: string;
  content: string;
}

const SKILL_FRONTMATTER_RE = /^---\s*\r?\n([\s\S]*?)\r?\n---\s*\r?\n([\s\S]*)$/;

function readFrontmatterScalar(frontmatter: string, key: string): string {
  const lines = frontmatter.split(/\r?\n/);
  for (const line of lines) {
    const match = line.match(
      new RegExp(`^${key}:\\s*(.+?)\\s*$`),
    );
    if (!match) continue;
    let value = match[1].trim();
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }
    return value;
  }
  return "";
}

export function parseSkillMdContent(
  content: string,
  fileName: string,
): ParsedSkillPackage | { error: string } {
  const trimmed = content.trim();
  if (!trimmed) {
    return { error: "File is empty." };
  }

  const match = trimmed.match(SKILL_FRONTMATTER_RE);
  if (!match) {
    return { error: "Missing YAML frontmatter (file must start and close with ---)." };
  }

  const frontmatter = match[1];
  const body = match[2].trim();
  const name =
    readFrontmatterScalar(frontmatter, "name") ||
    fileName.replace(/\.md$/i, "") ||
    "Untitled Skill";
  const description =
    readFrontmatterScalar(frontmatter, "description") ||
    body.split(/\r?\n/).find((line) => line.trim().length > 0)?.trim() ||
    name;

  return {
    fileName,
    format: "skill-md",
    name,
    description,
    version: readFrontmatterScalar(frontmatter, "version") || "1.0.0",
    allowedTools:
      readFrontmatterScalar(frontmatter, "allowed-tools") ||
      readFrontmatterScalar(frontmatter, "allowedTools"),
    content: trimmed,
  };
}

export async function parseSkillPackageFile(
  file: File,
): Promise<ParsedSkillPackage | { error: string }> {
  const lower = file.name.toLowerCase();
  if (lower.endsWith(".md")) {
    return parseSkillMdContent(await file.text(), file.name);
  }
  if (lower.endsWith(".zip")) {
    return {
      error:
        "ZIP package import requires backend POST /api/v1/skills support. Upload SKILL.md for now.",
    };
  }
  return { error: "Only .md (SKILL.md) or .zip Skill packages are supported." };
}

export function skillSettingsFromPackage(
  pkg: ParsedSkillPackage,
): Record<string, string> {
  return {
    packageFileName: pkg.fileName,
    packageFormat: pkg.format,
    packageVersion: pkg.version,
    allowedTools: pkg.allowedTools,
    packageContent: pkg.content,
  };
}

export function builtinSkillSettings(skillId: string): Record<string, string> {
  return {
    packageFileName: "SKILL.md",
    packageFormat: "skill-md",
    packageVersion: "1.0.0",
    packageSource: `builtin://${skillId}`,
    allowedTools: "",
    packageContent: "",
  };
}

export function normalizeSkillSettings(
  settings?: Record<string, string>,
): Record<string, string> {
  return {
    packageFileName: settings?.packageFileName ?? "",
    packageFormat: settings?.packageFormat ?? "",
    packageVersion: settings?.packageVersion ?? "",
    packageSource: settings?.packageSource ?? "",
    allowedTools: settings?.allowedTools ?? "",
    packageContent: settings?.packageContent ?? "",
    defaultDbIds: settings?.defaultDbIds ?? "",
    defaultKbIds: settings?.defaultKbIds ?? "",
    defaultMcpIds: settings?.defaultMcpIds ?? "",
    modelProfileId: settings?.modelProfileId ?? "",
  };
}

export function isSkillSettingsValid(settings: Record<string, string>): boolean {
  if (settings.packageSource?.startsWith("builtin://")) return true;
  return (settings.packageContent ?? "").trim().length > 0;
}

export const SKILL_PACKAGE_LOCAL_ONLY_KEYS = ["packageContent"] as const;

export const DATA_SKILLS: DataSkill[] = [
  {
    id: "data-analysis",
    name: "Data analysis",
    description: "Answer data questions from metric lookups to full reports",
  },
  {
    id: "tabular-file-import",
    name: "Tabular file import",
    description: "Import CSV/Excel/JSON/Parquet files into the workspace",
  },
  {
    id: "data-cleaning-for-load",
    name: "Data cleaning for load",
    description: "Clean and validate tabular data before analysis or load",
  },
  {
    id: "batch-file-merge",
    name: "Batch file merge",
    description: "Merge multiple tabular files with mapping and dedup",
  },
  {
    id: "etl-pipeline-patterns",
    name: "ETL pipeline patterns",
    description: "Design reliable staging, idempotent, and incremental loads",
  },
  {
    id: "api-json-ingest",
    name: "API / JSON ingest",
    description: "Ingest API or JSON/JSONL payloads into tabular files",
  },
  {
    id: "database-load-planning",
    name: "Database load planning",
    description: "Map files to tables and plan read-only-validated loads",
  },
];

export const DEFAULT_SKILL_ID = DATA_SKILLS[0].id;

const ACTIVE_LLM_STORAGE_KEY = "data-tasks:active-llm:v2";
const RIGHT_PANEL_WIDTH_STORAGE_KEY = "data-tasks:right-panel-width:v2";
const LEFT_PANEL_WIDTH_STORAGE_KEY = "data-tasks:left-panel-width:v1";

export function loadRightPanelWidth(): number {
  if (typeof window === "undefined") return RIGHT_PANEL_DEFAULT_WIDTH;
  try {
    const raw = window.localStorage.getItem(RIGHT_PANEL_WIDTH_STORAGE_KEY);
    const fallback = RIGHT_PANEL_DEFAULT_WIDTH;
    const stored =
      raw && Number.isFinite(Number.parseFloat(raw)) && Number.parseFloat(raw) > 0
        ? Number.parseFloat(raw)
        : fallback;
    return clampRightPanelWidth(stored);
  } catch {
    return RIGHT_PANEL_DEFAULT_WIDTH;
  }
}

export function persistRightPanelWidth(width: number): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(
      RIGHT_PANEL_WIDTH_STORAGE_KEY,
      String(Math.round(width)),
    );
  } catch {
    // Ignore quota errors — width stays in-memory.
  }
}

export function loadLeftPanelWidth(): number {
  if (typeof window === "undefined") return LEFT_PANEL_DEFAULT_WIDTH;
  try {
    const raw = window.localStorage.getItem(LEFT_PANEL_WIDTH_STORAGE_KEY);
    const fallback = LEFT_PANEL_DEFAULT_WIDTH;
    const stored =
      raw && Number.isFinite(Number.parseFloat(raw)) && Number.parseFloat(raw) > 0
        ? Number.parseFloat(raw)
        : fallback;
    return clampLeftPanelWidth(stored);
  } catch {
    return LEFT_PANEL_DEFAULT_WIDTH;
  }
}

export function persistLeftPanelWidth(width: number): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(
      LEFT_PANEL_WIDTH_STORAGE_KEY,
      String(Math.round(width)),
    );
  } catch {
    // Ignore quota errors — width stays in-memory.
  }
}

export type WorkspaceConfigKind = "db" | "kb" | "mcp" | "llm" | "skill";

/** Connectivity status; populated once backend `test`/`introspect` lands. */
export type ConfigItemStatus = "connected" | "failed" | "untested";

export interface WorkspaceConfigItem {
  id: string;
  name: string;
  description: string;
  enabled: boolean;
  builtin?: boolean;
  settings?: Record<string, string>;
  secretRef?: string;
  hasSecret?: boolean;
  revision?: number;
  /** Connectivity result from backend `test`. Defaults to untested. */
  status?: ConfigItemStatus;
}

export function workspaceConfigItemStatusBadge(
  item: WorkspaceConfigItem,
  t: TranslateFn,
): { label: string; className: string } | null {
  const label = configItemStatusLabel(item.status, t);
  if (item.status === "connected") {
    return { label, className: "bg-emerald-50 text-emerald-700" };
  }
  if (item.status === "failed") {
    return { label, className: "bg-rose-50 text-rose-700" };
  }
  return { label, className: "bg-slate-100 text-slate-400" };
}

/**
 * A configuration is only usable once it has passed a connectivity `test`.
 * `failed` and `untested` items must not be selectable/used anywhere.
 */
export function isConfigItemUsable(
  item: Pick<WorkspaceConfigItem, "status"> | null | undefined,
): boolean {
  return item?.status === "connected";
}

/** Short status word for compact rows/pickers (mirrors the badge labels). */
export function configItemStatusLabel(
  status: ConfigItemStatus | undefined,
  t: TranslateFn,
): string {
  if (status === "connected") return t("common.connected");
  if (status === "failed") return t("common.unavailable");
  return t("common.notTested");
}

export type ConfigFieldDef = {
  key: string;
  label: string;
  placeholder?: string;
  helpText?: string;
  inputType?: "text" | "password" | "url" | "select" | "number" | "boolean" | "textarea";
  options?: Array<{ value: string; label: string }>;
  /** Select option values rendered disabled until pendingCapability activates. */
  pendingOptionValues?: string[];
  fullWidth?: boolean;
  required?: boolean;
  /** Field is only rendered/validated when this predicate passes (conditional fields). */
  visibleWhen?: (settings: Record<string, string>) => boolean;
  /**
   * Gates the field behind a backend capability. While the capability is off
   * the field is hidden/not validated. Flip the flag in BACKEND_CAPABILITIES
   * once the backend supports it — no field re-authoring needed.
   */
  requiresCapability?: BackendCapability;
  /**
   * Placeholder capability: field stays visible but disabled with a badge until
   * the backend ships. Flip matching entry in PENDING_CAPABILITIES to true.
   */
  pendingCapability?: PendingCapability;
  /** When set, pending state applies only if this predicate is true. */
  pendingWhen?: (settings: Record<string, string>) => boolean;
  /** Dynamic select options (e.g. fallback profile list). */
  getOptions?: (context: ConfigFieldOptionsContext) => Array<{ value: string; label: string }>;
  readOnly?: (item: WorkspaceConfigItem) => boolean;
};

export type ConfigFieldOptionsContext = {
  workspaceConfig?: WorkspaceConfigStore;
  currentItemId?: string;
};

/**
 * Backend capability flags. All false today (see
 * docs/engineering/2026-06-25-backend-requirements.md). Flip to true when the matching
 * backend capability ships; gated fields/options then appear automatically.
 */
export type BackendCapability =
  | "datasource.server" // PostgreSQL / MySQL adapters (#2)
  | "datasource.queryPolicy" // per-datasource maxRows/timeout wired (#5)
  | "llm.samplingParams" // temperature/maxTokens consumed (#4)
  | "artifact.export" // artifact preview/download API (#9)
  | "artifact.list" // session artifact list/restore API
  | "artifact.promote" // promote artifact-backed file into workspace assets
  | "chat.imageInput" // chat multimodal image parts consumed (#13a)
  | "chat.fileUpload" // chat file upload endpoint to session workspace (#13b)
  | "conversation.title" // LLM-generated session title sync
  | "interaction.resume" // restored HITL resume after refresh / session switch
  | "files"; // workspace FileAssetRef library API

export const BACKEND_CAPABILITIES: Record<BackendCapability, boolean> = {
  "datasource.server": false,
  "datasource.queryPolicy": false,
  "llm.samplingParams": false,
  "artifact.export": true,
  "artifact.list": false,
  "artifact.promote": false,
  "chat.imageInput": false,
  "chat.fileUpload": false,
  "conversation.title": false,
  "interaction.resume": false,
  files: false,
};

export function hasCapability(capability: BackendCapability): boolean {
  return BACKEND_CAPABILITIES[capability];
}

/** Applies runtime capability flags from `GET /api/v1/capabilities`. */
export function setLiveBackendCapabilities(
  mapped: Record<BackendCapability, boolean>,
): void {
  for (const capability of Object.keys(mapped) as BackendCapability[]) {
    BACKEND_CAPABILITIES[capability] = mapped[capability];
  }
}

/**
 * Placeholder capabilities for DB-GPT parity fields not yet implemented on the
 * backend. Flip to true when the matching backend feature ships.
 */
export type PendingCapability =
  | "datasource.extendedTypes"
  | "datasource.introspectionPolicy"
  | "datasource.samplePolicy"
  | "datasource.fieldMasking"
  | "kb.vectorStore"
  | "kb.rerank"
  | "kb.citationPolicy"
  | "kb.chunking"
  | "kb.graphRag"
  | "kb.scope"
  | "mcp.stdio"
  | "mcp.toolPolicy"
  | "llm.advancedSampling"
  | "skill.resourceBinding";

export const PENDING_CAPABILITIES: Record<PendingCapability, boolean> = {
  "datasource.extendedTypes": false,
  "datasource.introspectionPolicy": false,
  "datasource.samplePolicy": false,
  "datasource.fieldMasking": false,
  "kb.vectorStore": false,
  "kb.rerank": false,
  "kb.citationPolicy": false,
  "kb.chunking": false,
  "kb.graphRag": false,
  "kb.scope": false,
  "mcp.stdio": false,
  "mcp.toolPolicy": false,
  "llm.advancedSampling": false,
  "skill.resourceBinding": false,
};

export function hasPendingCapability(capability: PendingCapability): boolean {
  return PENDING_CAPABILITIES[capability];
}

/** Applies runtime pending flags (future API hook; defaults stay false). */
export function setLivePendingCapabilities(
  mapped: Partial<Record<PendingCapability, boolean>>,
): void {
  for (const capability of Object.keys(mapped) as PendingCapability[]) {
    const value = mapped[capability];
    if (typeof value === "boolean") {
      PENDING_CAPABILITIES[capability] = value;
    }
  }
}

export function isFieldHiddenByCapability(field: ConfigFieldDef): boolean {
  return Boolean(field.requiresCapability && !hasCapability(field.requiresCapability));
}

export function isFieldPending(
  field: ConfigFieldDef,
  settings: Record<string, string> = {},
): boolean {
  if (field.pendingWhen && !field.pendingWhen(settings)) {
    return false;
  }
  if (!field.pendingCapability) {
    return false;
  }
  return !hasPendingCapability(field.pendingCapability);
}

export function isFieldDisabled(
  field: ConfigFieldDef,
  item: WorkspaceConfigItem,
  settings: Record<string, string>,
): boolean {
  return Boolean(field.readOnly?.(item) || isFieldPending(field, settings));
}

/**
 * Datasource engine types supported by the backend today plus DB-GPT roadmap
 * types shown as disabled placeholders until adapters land.
 */
export const DB_TYPE_OPTIONS = [
  { value: "duckdb", label: "DuckDB (file)" },
  { value: "sqlite", label: "SQLite (file)" },
  { value: "csv", label: "CSV file" },
  { value: "xlsx", label: "Excel file" },
] as const;

/** Server DB types, gated behind `datasource.server` capability. */
export const DB_SERVER_TYPE_OPTIONS = [
  { value: "postgresql", label: "PostgreSQL" },
  { value: "mysql", label: "MySQL" },
] as const;

/** DB-GPT extended types — visible in UI, pending backend adapters. */
export const DB_PENDING_TYPE_OPTIONS = [
  { value: "clickhouse", label: "ClickHouse" },
  { value: "oracle", label: "Oracle" },
  { value: "mssql", label: "SQL Server" },
  { value: "hive", label: "Hive" },
  { value: "spark", label: "Spark" },
  { value: "vertica", label: "Vertica" },
  { value: "bigquery", label: "BigQuery" },
  { value: "snowflake", label: "Snowflake" },
] as const;

export const DB_MODE_OPTIONS = [
  { value: "readonly", label: "Read-only (currently the only supported mode)" },
] as const;

const DB_SERVER_TYPES = DB_SERVER_TYPE_OPTIONS.map((option) => option.value);
const DB_PENDING_TYPES = DB_PENDING_TYPE_OPTIONS.map((option) => option.value);
const DB_FILE_TYPES = DB_TYPE_OPTIONS.map((option) => option.value);
/** File-backed types that may appear via live capability lists (not host/port servers). */
const DB_LOCAL_FILE_EXTENDED_TYPES = ["access"] as const;
let liveDbTypeOptions: Array<{ value: string; label: string; enabled: boolean }> = [
  ...DB_TYPE_OPTIONS.map((option) => ({ ...option, enabled: true })),
  ...DB_SERVER_TYPE_OPTIONS.map((option) => ({ ...option, enabled: true })),
  ...DB_PENDING_TYPE_OPTIONS.map((option) => ({ ...option, enabled: false })),
];

export function setLiveDatasourceTypes(
  types: Array<{ enabled: boolean; label: string; name: string }>,
): void {
  if (types.length === 0) {
    return;
  }
  liveDbTypeOptions = types.map((type) => ({
    value: type.name,
    label: type.label,
    enabled: type.enabled,
  }));
}

function dbTypeOf(settings: Record<string, string>): string {
  return settings.type ?? "duckdb";
}

function isDbExtendedPendingType(settings: Record<string, string>): boolean {
  const type = dbTypeOf(settings);
  return isDbExtendedType(type) && !isDbTypeEnabled(type);
}

function isDbBigQueryType(settings: Record<string, string>): boolean {
  return dbTypeOf(settings) === "bigquery";
}

function isDbSnowflakeType(settings: Record<string, string>): boolean {
  return dbTypeOf(settings) === "snowflake";
}

function isDbClickHouseType(settings: Record<string, string>): boolean {
  return dbTypeOf(settings) === "clickhouse";
}

function isDbLocalFileType(settings: Record<string, string>): boolean {
  const type = dbTypeOf(settings);
  return (
    DB_FILE_TYPES.includes(type as (typeof DB_FILE_TYPES)[number]) ||
    DB_LOCAL_FILE_EXTENDED_TYPES.includes(
      type as (typeof DB_LOCAL_FILE_EXTENDED_TYPES)[number],
    )
  );
}

function isDbAccessType(settings: Record<string, string>): boolean {
  return dbTypeOf(settings) === "access";
}

function isDbServerType(settings: Record<string, string>): boolean {
  if (isDbLocalFileType(settings)) return false;
  const type = dbTypeOf(settings);
  return (
    DB_SERVER_TYPES.includes(type as (typeof DB_SERVER_TYPES)[number]) ||
    isDbExtendedType(type)
  );
}

function isDbExtendedType(type: string): boolean {
  return (
    DB_PENDING_TYPES.includes(type as (typeof DB_PENDING_TYPES)[number]) ||
    liveDbTypeOptions.some((option) =>
      option.value === type && !DB_FILE_TYPES.includes(type as (typeof DB_FILE_TYPES)[number])
        && !DB_SERVER_TYPES.includes(type as (typeof DB_SERVER_TYPES)[number]))
  );
}

function isDbTypeEnabled(type: string): boolean {
  return liveDbTypeOptions.some((option) => option.value === type && option.enabled);
}

/** DB type list grows with backend capability (server types appear when on). */
function dbTypeOptions(): Array<{ value: string; label: string }> {
  return liveDbTypeOptions
    .filter((option) => option.enabled || isDbExtendedType(option.value))
    .filter((option) => !DB_SERVER_TYPES.includes(option.value as (typeof DB_SERVER_TYPES)[number])
      || hasCapability("datasource.server"))
    .map((option) => ({
      value: option.value,
      label: option.label,
    }));
}

export const EMBEDDING_PROVIDER_OPTIONS = [
  { value: "bailian", label: "Bailian DashScope (bailian)" },
  { value: "openai-compatible", label: "OpenAI compatible" },
  { value: "openai", label: "OpenAI" },
] as const;

export const KB_VECTOR_STORE_OPTIONS = [
  { value: "local-sqlite", label: "Local SQLite (current default)" },
  { value: "chroma", label: "Chroma" },
  { value: "milvus", label: "Milvus" },
  { value: "pgvector", label: "pgvector" },
  { value: "elasticsearch", label: "Elasticsearch" },
] as const;

export const KB_SCOPE_OPTIONS = [
  { value: "personal", label: "Personal" },
  { value: "workspace", label: "Workspace" },
  { value: "project", label: "Project" },
] as const;

export const MCP_AUTH_TYPE_OPTIONS = [
  { value: "none", label: "No authentication" },
  { value: "bearer", label: "Bearer Token" },
  { value: "custom-header", label: "Custom header (pending backend)" },
] as const;

/** Chat models use one OpenAI-compatible provider path; vendor choice lives in baseUrl/modelName. */
export const LLM_PROVIDER_OPTIONS = [
  { value: "openai-compatible", label: "OpenAI compatible (LLM_PROVIDER=openai-compatible)" },
] as const;

function normalizeLlmProvider(provider?: string): string {
  const normalized = provider?.trim().toLowerCase().replaceAll("_", "-") ?? "";
  if (
    normalized === "bailian"
    || normalized === "deepseek"
    || normalized === "openai"
    || normalized === "openai-compatible"
  ) {
    return "openai-compatible";
  }
  return normalized || "openai-compatible";
}

export function normalizeLlmSettings(
  settings?: Record<string, string>,
): {
  provider: string;
  baseUrl: string;
  apiKey: string;
  modelName: string;
} {
  return {
    provider: normalizeLlmProvider(settings?.provider),
    baseUrl: settings?.baseUrl ?? settings?.base_url ?? "",
    apiKey: settings?.apiKey ?? settings?.api_key ?? "",
    modelName:
      settings?.modelName ?? settings?.model ?? settings?.model_name ?? "",
  };
}

export function summarizeLlmItems(
  items: WorkspaceConfigItem[],
  emptyLabel: string,
): string {
  if (items.length === 0) return emptyLabel;
  if (items.length === 1) {
    return getLlmDisplayLabel(items[0]);
  }
  return `${items.length} available by default`;
}

export function getLlmDisplayLabel(item: WorkspaceConfigItem): string {
  const name = item.name.trim();
  if (name) return name;
  const normalized = normalizeLlmSettings(item.settings);
  return normalized.modelName;
}

export function getLlmOptionSubtitle(item: WorkspaceConfigItem): string {
  if (item.builtin) return "Server environment variables";
  const normalized = normalizeLlmSettings(item.settings);
  return [normalized.provider, normalized.modelName, item.description]
    .filter(Boolean)
    .join(" · ");
}

export function getEnabledLlmItems(
  workspaceConfig: WorkspaceConfigStore,
): WorkspaceConfigItem[] {
  return workspaceConfig.llm;
}

export function loadActiveLlmId(
  workspaceConfig: WorkspaceConfigStore,
  scopeKey?: string | null,
): string | null {
  const enabled = getEnabledLlmItems(workspaceConfig);
  const usable = enabled.filter(isConfigItemUsable);
  const fallback = usable[0]?.id ?? enabled[0]?.id ?? null;
  if (typeof window === "undefined") return fallback;
  try {
    const raw = window.localStorage.getItem(
      scopedStorageKey(ACTIVE_LLM_STORAGE_KEY, SCOPED_ACTIVE_LLM_STORAGE_SUFFIX, scopeKey),
    );
    if (!raw) return fallback;
    const stored = enabled.find((item) => item.id === raw);
    if (!stored) return fallback;
    if (isConfigItemUsable(stored) || usable.length === 0) return stored.id;
    return fallback;
  } catch {
    return fallback;
  }
}

export function persistActiveLlmId(llmId: string, scopeKey?: string | null): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(
      scopedStorageKey(ACTIVE_LLM_STORAGE_KEY, SCOPED_ACTIVE_LLM_STORAGE_SUFFIX, scopeKey),
      llmId,
    );
  } catch {
    // Ignore quota errors.
  }
}

/** Keeps a valid dialog selection; otherwise falls back to an available profile.
 * Prefer connectivity-proven (`connected`) profiles so failed defaults are not sticky.
 */
export function resolveActiveLlmProfileId(
  enabledProfiles: WorkspaceConfigItem[],
  activeLlmId: string | null,
  fallback: string | null,
): string | null {
  const enabledIds = new Set(enabledProfiles.map((profile) => profile.id));
  const usable = enabledProfiles.filter(isConfigItemUsable);
  const usableIds = new Set(usable.map((profile) => profile.id));

  if (activeLlmId && usableIds.has(activeLlmId)) return activeLlmId;
  if (fallback && usableIds.has(fallback)) return fallback;
  if (usable[0]) return usable[0].id;

  if (activeLlmId && enabledIds.has(activeLlmId)) return activeLlmId;
  if (fallback && enabledIds.has(fallback)) return fallback;
  return enabledProfiles[0]?.id ?? null;
}

/** MCP transport types aligned with common MCP client configs. */
export const MCP_TRANSPORT_OPTIONS = [
  { value: "sse", label: "SSE (Server-Sent Events)" },
  { value: "streamable-http", label: "Streamable HTTP" },
  { value: "stdio", label: "stdio (local command)" },
] as const;

export function normalizeMcpSettings(
  settings?: Record<string, string>,
): {
  transport: string;
  serverUrl: string;
  apiUrl: string;
  apiKey: string;
  authType: string;
  toolAllowlist: string;
  timeoutMs: string;
  command: string;
  args: string;
  cwd: string;
  env: string;
} {
  return {
    transport: settings?.transport ?? "sse",
    serverUrl: settings?.serverUrl ?? settings?.url ?? settings?.endpoint ?? "",
    apiUrl: settings?.apiUrl ?? "",
    apiKey: settings?.apiKey ?? settings?.api_key ?? settings?.token ?? "",
    authType: settings?.authType ?? "none",
    toolAllowlist: settings?.toolAllowlist ?? "",
    timeoutMs: settings?.timeoutMs ?? "",
    command: settings?.command ?? "",
    args: settings?.args ?? "",
    cwd: settings?.cwd ?? "",
    env: settings?.env ?? "",
  };
}

export function isMcpStdioTransport(settings: Record<string, string>): boolean {
  return settings.transport === "stdio";
}

export function normalizeKbSettings(
  settings?: Record<string, string>,
): Record<string, string> {
  return {
    indexName: settings?.indexName ?? "",
    retrievalTopK: settings?.retrievalTopK ?? "5",
    scoreThreshold: settings?.scoreThreshold ?? "0.2",
    embeddingProvider: settings?.embeddingProvider ?? "bailian",
    embeddingModel: settings?.embeddingModel ?? "text-embedding-v4",
    embeddingBaseUrl:
      settings?.embeddingBaseUrl ??
      settings?.embedding_base_url ??
      "https://dashscope.aliyuncs.com/compatible-mode/v1",
    embeddingApiKey: settings?.embeddingApiKey ?? settings?.embedding_api_key ?? "",
    vectorStore: settings?.vectorStore ?? "local-sqlite",
    rerankEnabled: settings?.rerankEnabled ?? "false",
    rerankModel: settings?.rerankModel ?? "",
    citationRequired: settings?.citationRequired ?? "true",
    chunkSize: settings?.chunkSize ?? "1600",
    chunkOverlap: settings?.chunkOverlap ?? "200",
    scope: settings?.scope ?? "workspace",
    graphRagEnabled: settings?.graphRagEnabled ?? "false",
    indexStatus: settings?.indexStatus ?? "",
  };
}

export function normalizeLlmSettingsExtended(
  settings?: Record<string, string>,
): Record<string, string> {
  const base = normalizeLlmSettings(settings);
  return {
    ...base,
    fallbackProfileId: settings?.fallbackProfileId ?? "",
    timeoutMs: settings?.timeoutMs ?? "300000",
    temperature: settings?.temperature ?? "",
    maxTokens: settings?.maxTokens ?? "",
    topP: settings?.topP ?? "",
    frequencyPenalty: settings?.frequencyPenalty ?? "",
    presencePenalty: settings?.presencePenalty ?? "",
    reasoningModel: settings?.reasoningModel ?? "false",
    contextLength: settings?.contextLength ?? "",
  };
}

export function summarizeMcpItems(
  items: WorkspaceConfigItem[],
  emptyLabel: string,
): string {
  if (items.length === 0) return emptyLabel;
  if (items.length === 1) return items[0].name;
  return `${items.length} available by default`;
}

export const WORKSPACE_CONFIG_FIELDS: Record<
  WorkspaceConfigKind,
  ConfigFieldDef[]
> = {
  db: [
    {
      key: "datasourceId",
      label: "Data source ID",
      placeholder: "my-dataset",
      helpText: "Stable identifier forwarded to the Agent (forwardedProps.datasourceId).",
      required: true,
      fullWidth: true,
    },
    {
      key: "type",
      label: "Data source type",
      inputType: "select",
      getOptions: () => dbTypeOptions(),
      pendingOptionValues: [...DB_PENDING_TYPES],
      helpText:
        "Implemented: DuckDB / SQLite / CSV / Excel / PostgreSQL / MySQL / ClickHouse. " +
        "Disabled extension types are marked pending backend.",
      required: true,
    },
    {
      key: "mode",
      label: "Access mode",
      inputType: "select",
      options: [...DB_MODE_OPTIONS],
      helpText: "The backend currently supports read-only queries only (run_sql_readonly).",
    },
    {
      key: "filePath",
      label: "File path",
      placeholder: "Upload a local file, or paste an existing server path",
      helpText:
        "Choose a local DuckDB / SQLite / CSV / Excel / Access file to upload. " +
        "The server stores it under your workspace and fills this path automatically. " +
        "You can still paste an existing server-readable path if you already have one.",
      required: true,
      fullWidth: true,
      visibleWhen: (s) => isDbLocalFileType(s) && !(isDbAccessType(s) && (s.connectionString ?? "").trim()),
    },
    {
      key: "connectionString",
      label: "ODBC connection string",
      inputType: "password",
      placeholder: "Driver={Microsoft Access Driver (*.mdb, *.accdb)};DBQ=/data/sales.accdb;",
      helpText:
        "Optional for Access. When set, overrides building the ODBC string from the file path.",
      fullWidth: true,
      visibleWhen: isDbAccessType,
    },
    {
      key: "host",
      label: "Host",
      placeholder: "127.0.0.1",
      required: true,
      requiresCapability: "datasource.server",
      visibleWhen: isDbServerType,
      pendingWhen: isDbExtendedPendingType,
      pendingCapability: "datasource.extendedTypes",
    },
    {
      key: "port",
      label: "Port",
      inputType: "number",
      placeholder: "5432",
      required: true,
      requiresCapability: "datasource.server",
      visibleWhen: isDbServerType,
      pendingWhen: isDbExtendedPendingType,
      pendingCapability: "datasource.extendedTypes",
    },
    {
      key: "database",
      label: "Database",
      placeholder: "analytics",
      required: true,
      requiresCapability: "datasource.server",
      visibleWhen: isDbServerType,
      pendingWhen: isDbExtendedPendingType,
      pendingCapability: "datasource.extendedTypes",
    },
    {
      key: "schema",
      label: "Schema",
      placeholder: "public",
      requiresCapability: "datasource.server",
      visibleWhen: (s) =>
        isDbServerType(s) && !isDbBigQueryType(s) && !isDbSnowflakeType(s) && !isDbClickHouseType(s),
      pendingWhen: isDbExtendedPendingType,
      pendingCapability: "datasource.extendedTypes",
    },
    {
      key: "username",
      label: "Username",
      placeholder: "readonly_user",
      required: true,
      requiresCapability: "datasource.server",
      visibleWhen: (s) => isDbServerType(s) && !isDbBigQueryType(s),
      pendingWhen: isDbExtendedPendingType,
      pendingCapability: "datasource.extendedTypes",
    },
    {
      key: "password",
      label: "Password",
      inputType: "password",
      placeholder: "••••••",
      helpText: "Stored in secretRef; read APIs never return plaintext.",
      requiresCapability: "datasource.server",
      visibleWhen: (s) => isDbServerType(s) && !isDbBigQueryType(s),
      pendingWhen: isDbExtendedPendingType,
      pendingCapability: "datasource.extendedTypes",
    },
    {
      key: "secure",
      label: "HTTPS",
      inputType: "boolean",
      requiresCapability: "datasource.server",
      visibleWhen: isDbClickHouseType,
      pendingWhen: isDbExtendedPendingType,
      pendingCapability: "datasource.extendedTypes",
    },
    {
      key: "projectId",
      label: "Project ID",
      placeholder: "my-gcp-project",
      required: true,
      visibleWhen: isDbBigQueryType,
      pendingWhen: isDbBigQueryType,
      pendingCapability: "datasource.extendedTypes",
      fullWidth: true,
    },
    {
      key: "dataset",
      label: "Dataset",
      placeholder: "analytics",
      required: true,
      visibleWhen: isDbBigQueryType,
      pendingWhen: isDbBigQueryType,
      pendingCapability: "datasource.extendedTypes",
    },
    {
      key: "credentialsJson",
      label: "Credentials JSON",
      inputType: "password",
      placeholder: "{ ... }",
      visibleWhen: isDbBigQueryType,
      pendingWhen: isDbBigQueryType,
      pendingCapability: "datasource.extendedTypes",
      fullWidth: true,
    },
    {
      key: "account",
      label: "Account",
      placeholder: "xy12345.us-east-1",
      required: true,
      visibleWhen: isDbSnowflakeType,
      pendingWhen: isDbSnowflakeType,
      pendingCapability: "datasource.extendedTypes",
    },
    {
      key: "warehouse",
      label: "Warehouse",
      placeholder: "COMPUTE_WH",
      required: true,
      visibleWhen: isDbSnowflakeType,
      pendingWhen: isDbSnowflakeType,
      pendingCapability: "datasource.extendedTypes",
    },
    {
      key: "tableAllowlist",
      label: "Table allowlist",
      placeholder: "orders, customers",
      helpText: "Comma-separated. Leave empty to allow all tables.",
      pendingCapability: "datasource.introspectionPolicy",
      fullWidth: true,
    },
    {
      key: "refreshIntervalSec",
      label: "Schema refresh interval (seconds)",
      inputType: "number",
      placeholder: "3600",
      pendingCapability: "datasource.introspectionPolicy",
    },
    {
      key: "denyWrite",
      label: "Deny writes",
      inputType: "boolean",
      helpText: "The backend currently enforces read-only access; this policy switch is pending.",
      pendingCapability: "datasource.introspectionPolicy",
    },
    {
      key: "maskFields",
      label: "Masked fields",
      placeholder: "phone, email",
      helpText: "Comma-separated field names to mask in query results.",
      pendingCapability: "datasource.fieldMasking",
      fullWidth: true,
    },
    {
      key: "allowSample",
      label: "Allow sampling",
      inputType: "boolean",
      pendingCapability: "datasource.samplePolicy",
    },
    {
      key: "maxSampleRows",
      label: "Max sample rows",
      inputType: "number",
      placeholder: "100",
      visibleWhen: (s) => s.allowSample === "true",
      pendingCapability: "datasource.samplePolicy",
    },
    {
      key: "maxRows",
      label: "Max returned rows",
      inputType: "number",
      placeholder: "10000",
      requiresCapability: "datasource.queryPolicy",
    },
    {
      key: "timeoutMs",
      label: "Query timeout (ms)",
      inputType: "number",
      placeholder: "30000",
      requiresCapability: "datasource.queryPolicy",
    },
  ],
  kb: [
    {
      key: "indexName",
      label: "Index name",
      placeholder: "metrics-docs",
      required: true,
      fullWidth: true,
    },
    {
      key: "scope",
      label: "Scope",
      inputType: "select",
      options: [...KB_SCOPE_OPTIONS],
      pendingCapability: "kb.scope",
    },
    {
      key: "retrievalTopK",
      label: "Retrieval Top K",
      inputType: "number",
      placeholder: "5",
    },
    {
      key: "scoreThreshold",
      label: "Score threshold",
      inputType: "number",
      placeholder: "0.2",
      helpText: "Default filtering threshold for knowledge search and retrieve_knowledge.",
    },
    {
      key: "embeddingProvider",
      label: "Embedding Provider",
      inputType: "select",
      options: [...EMBEDDING_PROVIDER_OPTIONS],
      fullWidth: true,
    },
    {
      key: "embeddingModel",
      label: "Embedding Model",
      placeholder: "text-embedding-v4",
      fullWidth: true,
    },
    {
      key: "embeddingBaseUrl",
      label: "Embedding Base URL",
      inputType: "url",
      placeholder: "https://dashscope.aliyuncs.com/compatible-mode/v1",
      fullWidth: true,
    },
    {
      key: "embeddingApiKey",
      label: "Embedding API Key",
      inputType: "password",
      placeholder: "sk-...",
      helpText: "Stored in secretRef. Falls back to server EMBEDDING_* environment variables when unset.",
      fullWidth: true,
    },
    {
      key: "vectorStore",
      label: "Vector store type",
      inputType: "select",
      options: [...KB_VECTOR_STORE_OPTIONS],
      pendingCapability: "kb.vectorStore",
      fullWidth: true,
    },
    {
      key: "rerankEnabled",
      label: "Enable rerank",
      inputType: "boolean",
      pendingCapability: "kb.rerank",
    },
    {
      key: "rerankModel",
      label: "Rerank Model",
      placeholder: "gte-rerank",
      visibleWhen: (s) => s.rerankEnabled === "true",
      pendingCapability: "kb.rerank",
      fullWidth: true,
    },
    {
      key: "citationRequired",
      label: "Require citations",
      inputType: "boolean",
      helpText: "Answers must include KB citations.",
      pendingCapability: "kb.citationPolicy",
    },
    {
      key: "chunkSize",
      label: "Chunk size",
      inputType: "number",
      placeholder: "1600",
      pendingCapability: "kb.chunking",
    },
    {
      key: "chunkOverlap",
      label: "Chunk overlap",
      inputType: "number",
      placeholder: "200",
      pendingCapability: "kb.chunking",
    },
    {
      key: "graphRagEnabled",
      label: "GraphRAG",
      inputType: "boolean",
      pendingCapability: "kb.graphRag",
    },
  ],
  mcp: [
    {
      key: "transport",
      label: "Transport",
      inputType: "select",
      options: [...MCP_TRANSPORT_OPTIONS],
      pendingOptionValues: ["stdio"],
      helpText: "Remote MCP commonly uses SSE or Streamable HTTP; stdio uses a local launch command.",
      required: true,
      fullWidth: true,
    },
    {
      key: "serverUrl",
      label: "Endpoint / launch command",
      placeholder: "https://example.com/mcp/sse",
      helpText:
        "For remote transports, enter the MCP service URL. " +
        "For stdio, a full launch command can be used as fallback.",
      visibleWhen: (settings) => !isMcpStdioTransport(settings),
      required: true,
      fullWidth: true,
    },
    {
      key: "apiUrl",
      label: "Data API endpoint",
      placeholder: "https://example.com",
      helpText: "Used by the DataLink workspace graph. Leave empty for MCP-only services.",
      visibleWhen: (settings) => !isMcpStdioTransport(settings),
      fullWidth: true,
    },
    {
      key: "command",
      label: "Executable",
      placeholder: "/usr/bin/npx",
      helpText: "In stdio mode, command + args are preferred. Leave empty to fall back to the full command above.",
      visibleWhen: isMcpStdioTransport,
      pendingCapability: "mcp.stdio",
      required: true,
      fullWidth: true,
    },
    {
      key: "args",
      label: "Launch arguments",
      placeholder: "-y @modelcontextprotocol/server-filesystem /data",
      helpText: "Space-separated; written to the backend args array.",
      visibleWhen: isMcpStdioTransport,
      pendingCapability: "mcp.stdio",
      fullWidth: true,
    },
    {
      key: "cwd",
      label: "Working directory",
      placeholder: "/home/agent/workspace",
      visibleWhen: isMcpStdioTransport,
      pendingCapability: "mcp.stdio",
      fullWidth: true,
    },
    {
      key: "env",
      label: "Environment variables (JSON)",
      inputType: "textarea",
      placeholder: '{ "NODE_ENV": "production" }',
      helpText: "JSON object with string keys and values.",
      visibleWhen: isMcpStdioTransport,
      pendingCapability: "mcp.stdio",
      fullWidth: true,
    },
    {
      key: "authType",
      label: "Authentication method",
      inputType: "select",
      options: [...MCP_AUTH_TYPE_OPTIONS],
      pendingOptionValues: ["custom-header"],
      visibleWhen: (settings) => !isMcpStdioTransport(settings),
    },
    {
      key: "apiKey",
      label: "Token / API Key",
      inputType: "password",
      placeholder: "••••••",
      helpText: "Stored in secretRef for Bearer authentication.",
      visibleWhen: (settings) =>
        !isMcpStdioTransport(settings) && (settings.authType ?? "none") !== "none",
      fullWidth: true,
    },
    {
      key: "toolAllowlist",
      label: "Tool allowlist",
      placeholder: "search, fetch_page",
      helpText: "Comma-separated. Leave empty to allow all tools in the manifest.",
      pendingCapability: "mcp.toolPolicy",
      fullWidth: true,
    },
    {
      key: "timeoutMs",
      label: "Per-tool timeout (ms)",
      inputType: "number",
      placeholder: "30000",
      pendingCapability: "mcp.toolPolicy",
    },
  ],
  llm: [
    {
      key: "provider",
      label: "Provider",
      inputType: "select",
      options: [...LLM_PROVIDER_OPTIONS],
      helpText:
        "All chat models use the OpenAI-compatible /chat/completions path.",
      required: true,
      fullWidth: true,
    },
    {
      key: "baseUrl",
      label: "Base URL",
      inputType: "url",
      placeholder: "https://dashscope.aliyuncs.com/compatible-mode/v1",
      helpText: "OpenAI-compatible Chat Completions base URL (without /chat/completions).",
      required: true,
      fullWidth: true,
    },
    {
      key: "apiKey",
      label: "API Key",
      inputType: "password",
      placeholder: "sk-...",
      helpText: "Stored in secretRef; never sent through AG-UI during runs.",
      fullWidth: true,
    },
    {
      key: "modelName",
      label: "Model Name",
      placeholder: "qwen-plus",
      helpText: "Chat model id (for example gpt-4o, qwen-plus, deepseek-chat).",
      required: true,
      fullWidth: true,
    },
    {
      key: "fallbackProfileId",
      label: "Fallback Profile",
      inputType: "select",
      placeholder: "None",
      helpText: "Fallback chain used when the primary profile fails.",
      getOptions: ({ workspaceConfig, currentItemId }) => {
        const profiles = workspaceConfig?.llm ?? [];
        return profiles
          .filter((profile) => profile.id !== currentItemId)
          .map((profile) => ({
            value: profile.id,
            label: profile.name || profile.id,
          }));
      },
      fullWidth: true,
    },
    {
      key: "timeoutMs",
      label: "Timeout (ms)",
      inputType: "number",
      placeholder: "300000",
      helpText:
        "Whole-run timeout for multi-step agent tasks (not just a single LLM call). " +
        "Use 300000–600000 for deep analysis.",
    },
    {
      key: "temperature",
      label: "Temperature",
      inputType: "number",
      placeholder: "0.2",
      requiresCapability: "llm.samplingParams",
    },
    {
      key: "maxTokens",
      label: "Max Tokens",
      inputType: "number",
      placeholder: "4096",
      requiresCapability: "llm.samplingParams",
    },
    {
      key: "topP",
      label: "Top P",
      inputType: "number",
      placeholder: "1.0",
      requiresCapability: "llm.samplingParams",
    },
    {
      key: "frequencyPenalty",
      label: "Frequency Penalty",
      inputType: "number",
      placeholder: "0",
      requiresCapability: "llm.samplingParams",
    },
    {
      key: "presencePenalty",
      label: "Presence Penalty",
      inputType: "number",
      placeholder: "0",
      requiresCapability: "llm.samplingParams",
    },
    {
      key: "reasoningModel",
      label: "Reasoning Model",
      inputType: "boolean",
      pendingCapability: "llm.advancedSampling",
    },
    {
      key: "contextLength",
      label: "Context Length",
      inputType: "number",
      placeholder: "128000",
      pendingCapability: "llm.advancedSampling",
    },
  ],
  skill: [
    {
      key: "packageFileName",
      label: "Package file",
      readOnly: () => true,
      fullWidth: true,
    },
    {
      key: "packageFormat",
      label: "Format",
      readOnly: () => true,
    },
    {
      key: "packageVersion",
      label: "Version",
      readOnly: () => true,
    },
    {
      key: "allowedTools",
      label: "Allowed tools",
      readOnly: () => true,
      fullWidth: true,
      visibleWhen: (settings) => (settings.allowedTools ?? "").trim().length > 0,
    },
    {
      key: "defaultDbIds",
      label: "Default data sources",
      placeholder: "sales-db, analytics-pg",
      helpText: "Comma-separated datasource ids. Added automatically to the run when the skill matches.",
      pendingCapability: "skill.resourceBinding",
      fullWidth: true,
    },
    {
      key: "defaultKbIds",
      label: "Default knowledge bases",
      placeholder: "metrics-docs",
      pendingCapability: "skill.resourceBinding",
      fullWidth: true,
    },
    {
      key: "defaultMcpIds",
      label: "Default MCP",
      placeholder: "notion",
      pendingCapability: "skill.resourceBinding",
      fullWidth: true,
    },
    {
      key: "modelProfileId",
      label: "Default model profile",
      placeholder: "qwen-plus-default",
      pendingCapability: "skill.resourceBinding",
      fullWidth: true,
    },
  ],
};

export function defaultSettingsForKind(
  kind: WorkspaceConfigKind,
  name = "",
): Record<string, string> {
  switch (kind) {
    case "db":
      return {
        datasourceId: name || "custom-datasource",
        type: "sqlite",
        mode: "readonly",
        filePath: "",
        denyWrite: "true",
        allowSample: "true",
        maxSampleRows: "100",
      };
    case "kb":
      return normalizeKbSettings({ indexName: name || "custom-kb" });
    case "mcp":
      return {
        transport: "sse",
        serverUrl: name ? `https://${name}` : "",
        apiUrl: "",
        apiKey: "",
        authType: "none",
        command: "",
        args: "",
        cwd: "",
        env: "",
      };
    case "llm":
      return normalizeLlmSettingsExtended({
        provider: "openai-compatible",
        baseUrl: "",
        apiKey: "",
        modelName: name || "qwen-plus",
      });
    case "skill":
      return normalizeSkillSettings({
        packageFileName: "",
        packageFormat: "",
        packageVersion: "",
        packageSource: "",
        allowedTools: "",
        packageContent: "",
      });
  }
}

/** Fields visible in the detail form (includes pending placeholders). */
export function renderableConfigFields(
  panel: WorkspaceConfigKind,
  settings: Record<string, string>,
): ConfigFieldDef[] {
  return WORKSPACE_CONFIG_FIELDS[panel].filter(
    (field) =>
      (panel === "skill" ? !isFieldHiddenByCapability(field) : true) &&
      (!field.visibleWhen || field.visibleWhen(settings)),
  );
}

/** Fields that should currently render/validate given the item's settings. */
export function visibleConfigFields(
  panel: WorkspaceConfigKind,
  settings: Record<string, string>,
): ConfigFieldDef[] {
  const fields = renderableConfigFields(panel, settings);
  if (panel !== "skill") {
    return fields;
  }
  return fields.filter((field) => !isFieldPending(field, settings));
}

export function resolveConfigFieldOptions(
  field: ConfigFieldDef,
  context: ConfigFieldOptionsContext,
): Array<{ value: string; label: string }> {
  if (field.getOptions) {
    return field.getOptions(context);
  }
  return field.options ?? [];
}

export function isSelectOptionPending(
  field: ConfigFieldDef,
  optionValue: string,
): boolean {
  if (!field.pendingOptionValues?.includes(optionValue)) {
    return false;
  }
  if (optionValue === "stdio") {
    return !hasPendingCapability("mcp.stdio");
  }
  if (optionValue === "custom-header") {
    return true;
  }
  if (field.pendingCapability) {
    return !hasPendingCapability(field.pendingCapability);
  }
  return DB_PENDING_TYPES.includes(optionValue as (typeof DB_PENDING_TYPES)[number])
    && !isDbTypeEnabled(optionValue);
}

/**
 * Unified validation: name present, plus every visible required field filled.
 * Builtin read-only fields are exempt (their values are server-managed).
 */
export function isWorkspaceConfigItemValid(
  panel: WorkspaceConfigKind,
  item: WorkspaceConfigItem,
  settings: Record<string, string>,
): boolean {
  if (!item.name.trim()) return false;
  if (panel === "skill") {
    return isSkillSettingsValid(settings);
  }
  return visibleConfigFields(panel, settings).every((field) => {
    if (!field.required) return true;
    if (field.readOnly?.(item)) return true;
    return (settings[field.key] ?? "").trim().length > 0;
  });
}

/** Compare editable fields for save/cancel dirty detection (ignores revision/status). */
export function workspaceConfigItemDraftEquals(
  a: WorkspaceConfigItem,
  b: WorkspaceConfigItem,
): boolean {
  if (a.name.trim() !== b.name.trim()) return false;
  if (a.description.trim() !== b.description.trim()) return false;
  if (a.enabled !== b.enabled) return false;
  const aSettings = a.settings ?? {};
  const bSettings = b.settings ?? {};
  const keys = new Set([...Object.keys(aSettings), ...Object.keys(bSettings)]);
  for (const key of keys) {
    if ((aSettings[key] ?? "").trim() !== (bSettings[key] ?? "").trim()) {
      return false;
    }
  }
  return true;
}

export type WorkspaceConfigStore = Record<
  WorkspaceConfigKind,
  WorkspaceConfigItem[]
>;

/** Empty local shell until workspace config is loaded from the API. */
export function defaultWorkspaceConfig(): WorkspaceConfigStore {
  return {
    db: [],
    kb: [],
    mcp: [],
    llm: [],
    skill: [],
  };
}

export function summarizeConfigItems(
  items: WorkspaceConfigItem[],
  emptyLabel: string,
): string {
  if (items.length === 0) return emptyLabel;
  if (items.length === 1) return items[0].name;
  return `${items.length} available by default`;
}

export function createWorkspaceConfigItem(
  kind: WorkspaceConfigKind,
  name: string,
  description: string,
): WorkspaceConfigItem {
  return {
    id: createClientId("custom"),
    name,
    description,
    enabled: true,
    settings: defaultSettingsForKind(kind, name),
  };
}

// ---------------------------------------------------------------------------
// Session config — layer between workspace defaults and per-run @ mentions.
// Each chat session stores a per-kind *disabled* list (default = all enabled).
// ---------------------------------------------------------------------------

export function emptySessionDisabledMap(): SessionDisabledMap {
  return { db: [], kb: [], mcp: [], skill: [] };
}

export function getSessionDisabled(
  session: ChatSession | null | undefined,
): SessionDisabledMap {
  return session?.config?.disabled ?? emptySessionDisabledMap();
}

export function sessionEnabledItems(
  store: WorkspaceConfigStore,
  kind: PerRunMentionKind,
  session: ChatSession | null | undefined,
): WorkspaceConfigItem[] {
  const disabled = new Set(getSessionDisabled(session)[kind]);
  return store[kind].filter((item) => !disabled.has(item.id));
}

export function sessionEnabledIds(
  store: WorkspaceConfigStore,
  kind: PerRunMentionKind,
  session: ChatSession | null | undefined,
): string[] {
  return sessionEnabledItems(store, kind, session).map((item) => item.id);
}

export function sessionRunnableDatasourceItems(
  store: WorkspaceConfigStore,
  session: ChatSession | null | undefined,
): WorkspaceConfigItem[] {
  return sessionEnabledItems(store, "db", session).filter(isConfigItemUsable);
}

export function sessionRunnableDatasourceIds(
  store: WorkspaceConfigStore,
  session: ChatSession | null | undefined,
): string[] {
  return sessionRunnableDatasourceItems(store, session).map((item) => item.id);
}

export function toggleSessionResource(
  session: ChatSession,
  kind: PerRunMentionKind,
  id: string,
): ChatSession {
  const disabled = getSessionDisabled(session);
  const current = disabled[kind];
  const nextIds = current.includes(id)
    ? current.filter((value) => value !== id)
    : [...current, id];
  return {
    ...session,
    config: {
      disabled: { ...disabled, [kind]: nextIds },
    },
  };
}

export function sessionResourceCounts(
  store: WorkspaceConfigStore,
  kind: PerRunMentionKind,
  session: ChatSession | null | undefined,
): { enabled: number; total: number } {
  const total = kind === "db"
    ? store.db.filter(isConfigItemUsable).length
    : store[kind].length;
  const enabled = kind === "db"
    ? sessionRunnableDatasourceItems(store, session).length
    : sessionEnabledItems(store, kind, session).length;
  return { enabled, total };
}

// ---------------------------------------------------------------------------
// Per-run @ mentions — layer-3 override: specify active/mentioned for one run.
// `@` picks from the session-enabled set only; it does not narrow `enabled*Ids`.
// LLM is excluded — it has its own model picker.
// ---------------------------------------------------------------------------

export type PerRunMentionKind = Exclude<WorkspaceConfigKind, "llm">;

export const PER_RUN_MENTION_KINDS: PerRunMentionKind[] = [
  "db",
  "kb",
  "mcp",
  "skill",
];

/** Session-scoped db/kb toggles lock after the first user message or run. */
export const SESSION_LOCKABLE_RESOURCE_KINDS: PerRunMentionKind[] = ["db", "kb"];

export type SessionStartedHints = {
  runCount?: number;
  messageCount?: number;
  hasRunHistory?: boolean;
};

export function isSessionStarted(
  session: ChatSession | null | undefined,
  hints?: SessionStartedHints,
): boolean {
  if (!session) return false;
  if (session.lastMessageAt != null && session.lastMessageAt > 0) return true;
  if ((hints?.runCount ?? 0) > 0) return true;
  if ((hints?.messageCount ?? 0) > 0) return true;
  if (hints?.hasRunHistory) return true;
  return false;
}

export function isSessionResourceKindLocked(
  session: ChatSession | null | undefined,
  kind: PerRunMentionKind,
  hints?: SessionStartedHints,
): boolean {
  return (
    SESSION_LOCKABLE_RESOURCE_KINDS.includes(kind) &&
    isSessionStarted(session, hints)
  );
}

export const SESSION_RESOURCE_LABEL: Record<PerRunMentionKind, string> = {
  db: "Data source",
  kb: "Knowledge",
  mcp: "MCP",
  skill: "Skills",
};

export const SESSION_HEADER_RESOURCE_KINDS: PerRunMentionKind[] = ["db", "kb"];

export type PerRunSelection = Record<PerRunMentionKind, string[]>;

export function emptyPerRunSelection(): PerRunSelection {
  return { db: [], kb: [], mcp: [], skill: [] };
}

export function countPerRunMentions(selection: PerRunSelection): number {
  return PER_RUN_MENTION_KINDS.reduce(
    (total, kind) => total + selection[kind].length,
    0,
  );
}

/**
 * Per-kind metadata for the `@` picker. `backendSupported` reflects whether the
 * selection has a real runtime effect today: only `db` is honored (via
 * `datasource_id` / `forwardedProps`). The rest ride along in `run_config` for
 * forward-compatibility and are surfaced with a 「Backend unsupported」 hint so the UI never
 * implies an effect the backend can't yet deliver.
 */
export const PER_RUN_MENTION_META: Record<
  PerRunMentionKind,
  { label: string; token: string; backendSupported: boolean }
> = {
  db: { label: "Data sources", token: "db", backendSupported: true },
  kb: { label: "Knowledge", token: "kb", backendSupported: false },
  mcp: { label: "MCP", token: "mcp", backendSupported: false },
  skill: { label: "Skills", token: "skill", backendSupported: false },
};

export type MentionSupportMap = Record<PerRunMentionKind, boolean>;

/** Updates `@` picker hints from runtime knowledge/mcp/skills capability flags. */
export function setLiveMentionSupport(support: MentionSupportMap): void {
  for (const kind of PER_RUN_MENTION_KINDS) {
    PER_RUN_MENTION_META[kind].backendSupported = support[kind];
  }
}

/** Shared per-kind palette for sidebar labels and session/@ pills. */
const CONFIG_APPEARANCE = {
  db: {
    badge:
      "bg-surface-subtle text-muted ring-1 ring-inset ring-border",
    pill: "border-border bg-surface text-foreground hover:border-muted-light hover:bg-surface-subtle",
    pillOpen:
      "border-muted-light bg-surface-subtle text-foreground",
    chip: "border-border bg-surface-subtle text-muted",
  },
  kb: {
    badge:
      "bg-surface-subtle text-muted ring-1 ring-inset ring-border",
    pill: "border-border bg-surface text-foreground hover:border-muted-light hover:bg-surface-subtle",
    pillOpen:
      "border-muted-light bg-surface-subtle text-foreground",
    chip: "border-border bg-surface-subtle text-muted",
  },
  mcp: {
    badge:
      "bg-surface-subtle text-muted ring-1 ring-inset ring-border",
    pill: "border-border bg-surface text-foreground hover:border-muted-light hover:bg-surface-subtle",
    pillOpen:
      "border-muted-light bg-surface-subtle text-foreground",
    chip: "border-border bg-surface-subtle text-muted",
  },
  llm: {
    badge:
      "bg-surface-subtle text-muted ring-1 ring-inset ring-border",
    pill: "border-border bg-surface text-foreground hover:border-muted-light hover:bg-surface-subtle",
    pillOpen:
      "border-muted-light bg-surface-subtle text-foreground",
    chip: "border-border bg-surface-subtle text-muted",
  },
  skill: {
    badge:
      "bg-surface-subtle text-muted ring-1 ring-inset ring-border",
    pill: "border-border bg-surface text-foreground hover:border-muted-light hover:bg-surface-subtle",
    pillOpen:
      "border-muted-light bg-surface-subtle text-foreground",
    chip: "border-border bg-surface-subtle text-muted",
  },
} as const satisfies Record<
  WorkspaceConfigKind,
  {
    badge: string;
    pill: string;
    pillOpen: string;
    chip: string;
  }
>;

export const WORKSPACE_CONFIG_SHORT_LABEL: Record<WorkspaceConfigKind, string> = {
  db: "DB",
  kb: "KB",
  mcp: "MCP",
  llm: "LLM",
  skill: "SKILL",
};

export const WORKSPACE_CONFIG_BADGE_CLASS: Record<WorkspaceConfigKind, string> =
  {
    db: CONFIG_APPEARANCE.db.badge,
    kb: CONFIG_APPEARANCE.kb.badge,
    mcp: CONFIG_APPEARANCE.mcp.badge,
    llm: CONFIG_APPEARANCE.llm.badge,
    skill: CONFIG_APPEARANCE.skill.badge,
  };

export const PER_RUN_MENTION_APPEARANCE: Record<
  PerRunMentionKind,
  { badge: string; pill: string; pillOpen: string; chip: string }
> = {
  db: CONFIG_APPEARANCE.db,
  kb: CONFIG_APPEARANCE.kb,
  mcp: CONFIG_APPEARANCE.mcp,
  skill: CONFIG_APPEARANCE.skill,
};

export interface MentionResource {
  kind: PerRunMentionKind;
  id: string;
  name: string;
  description: string;
  backendSupported: boolean;
}

export type FileMentionScope = "workspace" | "session";

export interface FileMentionResource {
  id: string;
  fileId: string;
  name: string;
  description: string;
  scope: FileMentionScope;
  path?: string;
  backendSupported: boolean;
}

export interface PerRunFileSelection {
  fileIds: string[];
  pinnedPaths: string[];
}

export type FileAssetRefLike = {
  id: string;
  filename: string;
  source?: string;
  sessionId?: string;
  mimeType?: string;
  sizeBytes?: number;
};

/** Lists session-enabled resources for the `@` picker. */
export function buildMentionResources(
  store: WorkspaceConfigStore,
  session: ChatSession | null | undefined,
): MentionResource[] {
  const resources: MentionResource[] = [];
  for (const kind of PER_RUN_MENTION_KINDS) {
    const items = kind === "db"
      ? sessionRunnableDatasourceItems(store, session)
      : sessionEnabledItems(store, kind, session);
    for (const item of items) {
      resources.push({
        kind,
        id: item.id,
        name: item.name,
        description: item.description,
        backendSupported: PER_RUN_MENTION_META[kind].backendSupported,
      });
    }
  }
  return resources;
}

export function emptyPerRunFileSelection(): PerRunFileSelection {
  return { fileIds: [], pinnedPaths: [] };
}

export function filterWorkspaceAssetFiles<T extends FileAssetRefLike>(files: T[]): T[] {
  return files.filter((file) =>
    !file.sessionId && (file.source === "upload" || file.source === "workspace"),
  );
}

export function fileMentionFromWorkspaceAsset(file: FileAssetRefLike): FileMentionResource {
  return {
    id: `workspace:${file.id}`,
    fileId: file.id,
    name: file.filename,
    description: [file.mimeType, file.sizeBytes !== undefined ? `${file.sizeBytes} B` : ""]
      .filter(Boolean)
      .join(" · "),
    scope: "workspace",
    backendSupported: true,
  };
}

export function fileMentionFromArtifact(artifact: DataArtifact): FileMentionResource | null {
  if (!artifact.fileId) return null;
  const path = artifact.detail?.type === "file" ? artifact.detail.path : undefined;
  if (!path) return null;
  return {
    id: `session:${artifact.id}`,
    fileId: artifact.fileId,
    name: artifact.title,
    description: artifact.summary,
    scope: "session",
    ...(path ? { path } : {}),
    backendSupported: true,
  };
}

export function togglePerRunFileMention(
  selection: PerRunFileSelection,
  resource: FileMentionResource,
): PerRunFileSelection {
  if (resource.scope === "workspace") {
    const exists = selection.fileIds.includes(resource.fileId);
    return {
      ...selection,
      fileIds: exists
        ? selection.fileIds.filter((id) => id !== resource.fileId)
        : [...selection.fileIds, resource.fileId],
    };
  }

  const pinnedPath = resource.path;
  if (!pinnedPath) return selection;
  const exists = selection.pinnedPaths.includes(pinnedPath);
  return {
    ...selection,
    pinnedPaths: exists
      ? selection.pinnedPaths.filter((path) => path !== pinnedPath)
      : [...selection.pinnedPaths, pinnedPath],
  };
}

export function removePerRunFileMention(
  selection: PerRunFileSelection,
  resource: FileMentionResource,
): PerRunFileSelection {
  if (resource.scope === "workspace") {
    return {
      ...selection,
      fileIds: selection.fileIds.filter((id) => id !== resource.fileId),
    };
  }
  return {
    ...selection,
    pinnedPaths: resource.path
      ? selection.pinnedPaths.filter((path) => path !== resource.path)
      : selection.pinnedPaths,
  };
}

export function countPerRunFileMentions(selection: PerRunFileSelection): number {
  return selection.fileIds.length + selection.pinnedPaths.length;
}

export function togglePerRunMention(
  selection: PerRunSelection,
  kind: PerRunMentionKind,
  id: string,
): PerRunSelection {
  const current = selection[kind];
  const next = current.includes(id)
    ? current.filter((value) => value !== id)
    : [...current, id];
  return { ...selection, [kind]: next };
}

export function removePerRunMention(
  selection: PerRunSelection,
  kind: PerRunMentionKind,
  id: string,
): PerRunSelection {
  return { ...selection, [kind]: selection[kind].filter((v) => v !== id) };
}

/** Drops @ mentions for resources no longer session-enabled or removed from workspace. */
export function prunePerRunSelection(
  store: WorkspaceConfigStore,
  session: ChatSession | null | undefined,
  selection: PerRunSelection,
): PerRunSelection {
  const next = emptyPerRunSelection();
  let changed = false;
  for (const kind of PER_RUN_MENTION_KINDS) {
    const available = new Set(
      kind === "db"
        ? sessionRunnableDatasourceIds(store, session)
        : sessionEnabledIds(store, kind, session),
    );
    const kept = selection[kind].filter((id) => available.has(id));
    if (kept.length !== selection[kind].length) changed = true;
    next[kind] = kept;
  }
  return changed ? next : selection;
}

export type RunConfigPayload = {
  enabledDatasourceIds: string[];
  enabledKnowledgeIds: string[];
  enabledMcpServerIds: string[];
  enabledSkillIds: string[];
  activeDatasourceId?: string;
  activeLlmProfileId: string | null;
  activeSkillId: string;
  mentioned: PerRunSelection;
  fileIds: string[];
  pinnedPaths: string[];
  evidenceRefs: EvidenceRef[];
};

export interface BuildRunConfigOptions {
  activeLlmId: string | null;
  defaultDatasourceId?: string;
  session?: ChatSession | null;
  perRunSelection?: PerRunSelection;
  perRunFiles?: PerRunFileSelection;
  evidenceRefs?: EvidenceRef[];
  defaultSkillId?: string;
}

function filterMentionedIds(
  ids: readonly string[],
  available: ReadonlySet<string>,
): string[] {
  return ids.filter((id) => available.has(id));
}

/**
 * Builds the forward-compatible `run_config` payload (config-management-api.md
 * §5). Ids / selections only — never credentials.
 *
 * - `enabled*Ids` = session-enabled set (all available this session); `@` does
 *   not narrow these.
 * - `active*` = first `@` mention for that kind, else default.
 * - `mentioned` = this run's `@` picks (subset of session-enabled).
 */
export function buildRunConfig(
  store: WorkspaceConfigStore,
  options: BuildRunConfigOptions,
): RunConfigPayload {
  const selection = options.perRunSelection ?? emptyPerRunSelection();
  const fileSelection = options.perRunFiles ?? emptyPerRunFileSelection();
  const session = options.session;
  const enabledDb = sessionRunnableDatasourceIds(store, session);
  const enabledKb = sessionEnabledIds(store, "kb", session);
  const enabledMcp = sessionEnabledIds(store, "mcp", session);
  const enabledSkill = sessionEnabledIds(store, "skill", session);
  const enabledDbSet = new Set(enabledDb);
  const enabledKbSet = new Set(enabledKb);
  const enabledMcpSet = new Set(enabledMcp);
  const enabledSkillSet = new Set(enabledSkill);

  const mentioned: PerRunSelection = {
    db: filterMentionedIds(selection.db, enabledDbSet),
    kb: filterMentionedIds(selection.kb, enabledKbSet),
    mcp: filterMentionedIds(selection.mcp, enabledMcpSet),
    skill: filterMentionedIds(selection.skill, enabledSkillSet),
  };

  const activeDatasourceId = mentioned.db[0]
    ?? (enabledDb.length > 0
      ? (options.defaultDatasourceId && enabledDbSet.has(options.defaultDatasourceId)
        ? options.defaultDatasourceId
        : enabledDb[0])
      : undefined);

  const activeSkillId =
    mentioned.skill[0] ??
    (options.defaultSkillId && enabledSkillSet.has(options.defaultSkillId)
      ? options.defaultSkillId
      : (enabledSkill[0] ?? options.defaultSkillId ?? DEFAULT_SKILL_ID));

  return {
    enabledDatasourceIds: enabledDb,
    enabledKnowledgeIds: enabledKb,
    enabledMcpServerIds: enabledMcp,
    enabledSkillIds: enabledSkill,
    ...(activeDatasourceId ? { activeDatasourceId } : {}),
    activeLlmProfileId: options.activeLlmId,
    activeSkillId,
    mentioned,
    fileIds: uniqueStrings(fileSelection.fileIds),
    pinnedPaths: uniqueStrings(fileSelection.pinnedPaths),
    evidenceRefs: uniqueEvidenceRefs(options.evidenceRefs ?? []),
  };
}

export type RunForwardedProps = {
  checkpointId?: string;
  checkpoint_id?: string;
  datasourceId?: string;
  run_config: RunConfigPayload;
};

/** CopilotKit `forwardedProps` payload for each agent run (highest backend merge priority). */
export function buildRunForwardedProps(
  datasourceId: string | undefined,
  runConfig: RunConfigPayload,
  checkpointId?: string,
): RunForwardedProps {
  return {
    ...(checkpointId ? { checkpointId, checkpoint_id: checkpointId } : {}),
    ...(datasourceId ? { datasourceId } : {}),
    run_config: runConfig,
  };
}

/** Merge HITL resume command into the current run forwarded props. */
export function mergeRunForwardedPropsWithCommand(
  base: RunForwardedProps,
  command: Record<string, unknown>,
): RunForwardedProps & { command: Record<string, unknown> } {
  return {
    ...base,
    command,
  };
}

/** Patch LangGraph-visible agent state so thread checkpoints carry the latest run_config. */
export function buildAgentRunStatePatch(
  forwardedProps: RunForwardedProps,
  prevState: unknown,
): Record<string, unknown> {
  const prev =
    typeof prevState === "object" && prevState !== null
      ? (prevState as Record<string, unknown>)
      : {};
  const { errorMessage: _errorMessage, runStatus: _runStatus, ...rest } = prev;
  return {
    ...rest,
    ...(forwardedProps.checkpointId ? { checkpointId: forwardedProps.checkpointId } : {}),
    ...(forwardedProps.checkpoint_id ? { checkpoint_id: forwardedProps.checkpoint_id } : {}),
    ...(forwardedProps.datasourceId ? { datasourceId: forwardedProps.datasourceId } : {}),
    run_config: forwardedProps.run_config,
  };
}

function uniqueStrings(values: readonly string[]): string[] {
  return [...new Set(values.filter(Boolean))];
}

function uniqueEvidenceRefs(values: readonly EvidenceRef[]): EvidenceRef[] {
  const seen = new Set<string>();
  const refs: EvidenceRef[] = [];
  for (const value of values) {
    if (!value.id || seen.has(value.id)) continue;
    seen.add(value.id);
    refs.push(value);
  }
  return refs;
}

/** Honors a per-run `@db` mention within the session-enabled db set. */
export function resolveActiveDatasourceId(
  store: WorkspaceConfigStore,
  session: ChatSession | null | undefined,
  selection: PerRunSelection,
  fallback?: string,
): string | undefined {
  const enabled = sessionRunnableDatasourceIds(store, session);
  const enabledSet = new Set(enabled);
  const mentioned = selection.db.find((id) => enabledSet.has(id));
  if (mentioned) return mentioned;
  if (enabled.length === 0) return undefined;
  if (fallback && enabledSet.has(fallback)) return fallback;
  return enabled[0];
}

/**
 * Returns a human-readable reason when a run must be blocked because the active
 * model (or the active datasource, when one is selected) has not passed a
 * connectivity test. Returns `null` when the run may proceed.
 */
export function resolveSendBlockReason(
  store: WorkspaceConfigStore,
  activeLlmId: string | null,
  activeDatasourceId: string | undefined,
  t: TranslateFn,
): string | null {
  const llm = store.llm.find((item) => item.id === activeLlmId) ?? store.llm[0] ?? null;
  if (llm && !isConfigItemUsable(llm)) {
    return t("chatInput.modelBlocked", { name: llm.name });
  }
  if (activeDatasourceId) {
    const db = store.db.find((item) => item.id === activeDatasourceId);
    if (db && !isConfigItemUsable(db)) {
      return t("chatInput.datasourceBlocked", { name: db.name });
    }
  }
  return null;
}
