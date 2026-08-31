import {
  clampRightPanelWidth,
  RIGHT_PANEL_DEFAULT_WIDTH,
} from "./workspace-layout.js";

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
 * more data tools (see frontend-backend-capability-requests.md #5/#6). Unknown
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
export function dataStepLabel(kind: DataStepKind): string {
  switch (kind) {
    case "inspect":
      return "结构检查";
    case "query":
      return "数据查询";
    case "transform":
      return "数据加工";
    case "fetch":
      return "取数";
    case "visualize":
      return "可视化";
    case "knowledge":
      return "知识检索";
    case "other":
      return "数据操作";
  }
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
      chartType?: ChartArtifactType | undefined;
      unit?: string | undefined;
      points: ChartArtifactPoint[];
      series?: ChartArtifactSeries[] | undefined;
    }
  | {
      type: "report";
      sections: Array<{ heading: string; body: string }>;
    }
  | {
      type: "file";
      path: string;
      size?: number | undefined;
      mtime?: string | undefined;
      tool?: string | undefined;
      content?: string | undefined;
    };

export interface DataArtifact {
  id: string;
  title: string;
  kind: ArtifactKind;
  type?: DataArtifactType | undefined;
  summary: string;
  version?: string | undefined;
  /** FileAssetRef id behind file-backed artifacts, when the backend provides one. */
  fileId?: string | undefined;
  /** Backend download URL for file-backed artifacts, when available. */
  downloadUrl?: string | undefined;
  /** Backend MIME type for file-backed artifacts, when available. */
  mimeType?: string | undefined;
  createdByEventId?: string | undefined;
  detail?: ArtifactDetail | undefined;
  /** When true, full preview can be fetched via artifact REST API. */
  previewAvailable?: boolean | undefined;
  /** Milliseconds since epoch when the artifact was recorded or restored. */
  recordedAtMs?: number | undefined;
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
  errorMessage?: string | undefined;
}

/** Generic data step payload for any non-SQL/non-schema operation. */
export interface GenericStepPayload {
  description: string;
  rawResult?: string | undefined;
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
  toolName?: string | undefined;
  /** Backend ACTIVITY STEP `step_id` (e.g. sql-1), when distinct from AG-UI toolCallId. */
  stepId?: string | undefined;
  /** Latest status from ACTIVITY_SNAPSHOT STEP content.status. */
  activityStatus?: ActivityStepStatus | undefined;
  /** The agent's reasoning shown above the action — the ReAct signature. */
  thought?: string | undefined;
  artifactIds?: string[] | undefined;
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
 * Client-side chat session. The backend has no session list / persistence API,
 * only a per-run `threadId`. Each session here owns its own `threadId`, which
 * keeps CopilotKit's per-(agentId, threadId) agent clone isolated.
 */
export interface ChatSession {
  id: string;
  threadId: string;
  title: string;
  createdAt: number;
}

const SESSIONS_STORAGE_KEY = "data-tasks:sessions:v1";

function newThreadId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `thread-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export function createChatSession(title = "新数据任务"): ChatSession {
  const threadId = newThreadId();
  return {
    id: threadId,
    threadId,
    title,
    createdAt: Date.now(),
  };
}

export function loadChatSessions(): ChatSession[] {
  if (typeof window === "undefined") return [];
  try {
    const raw = window.localStorage.getItem(SESSIONS_STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(isChatSession);
  } catch {
    return [];
  }
}

export function persistChatSessions(sessions: ChatSession[]): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(
      SESSIONS_STORAGE_KEY,
      JSON.stringify(sessions),
    );
  } catch {
    // Ignore quota / serialization errors — sessions stay in-memory.
  }
}

function isChatSession(value: unknown): value is ChatSession {
  if (typeof value !== "object" || value === null) return false;
  const record = value as Record<string, unknown>;
  return (
    typeof record.id === "string" &&
    typeof record.threadId === "string" &&
    typeof record.title === "string" &&
    typeof record.createdAt === "number"
  );
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
    const matchValue = match[1];
    if (!matchValue) continue;
    let value = matchValue.trim();
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
    return { error: "文件为空。" };
  }

  const match = trimmed.match(SKILL_FRONTMATTER_RE);
  if (!match) {
    return { error: "缺少 YAML frontmatter（文件须以 --- 开头并闭合）。" };
  }

  const frontmatter = match[1];
  const bodyMatch = match[2];
  if (!frontmatter || !bodyMatch) {
    return { error: "无法解析 frontmatter 或内容。" };
  }
  const body = bodyMatch.trim();
  const name =
    readFrontmatterScalar(frontmatter, "name") ||
    fileName.replace(/\.md$/i, "") ||
    "未命名 Skill";
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
      readFrontmatterScalar(frontmatter, "allowedTools") ||
      "",
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
        "ZIP 包导入需后端 POST /api/v1/skills 支持，当前请先上传 SKILL.md 文件。",
    };
  }
  return { error: "仅支持 .md（SKILL.md）或 .zip 技能包。" };
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
    name: "数据分析",
    description: "回答指标查询、深度分析和报告类数据问题",
  },
  {
    id: "tabular-file-import",
    name: "表格文件导入",
    description: "将 CSV/Excel/JSON/Parquet 导入工作区并规范化",
  },
  {
    id: "data-cleaning-for-load",
    name: "入库前清洗",
    description: "清洗校验表格数据，处理缺失、类型与去重",
  },
  {
    id: "batch-file-merge",
    name: "批量文件合并",
    description: "多文件对齐字段、去重合并并生成合并报告",
  },
  {
    id: "etl-pipeline-patterns",
    name: "ETL 管道模式",
    description: "设计可靠的 staging、幂等与增量导入流程",
  },
  {
    id: "api-json-ingest",
    name: "API/JSON 接入",
    description: "从 API 或 JSON/JSONL 拉取并展平为表格文件",
  },
  {
    id: "database-load-planning",
    name: "数据库入库规划",
    description: "映射字段并规划只读校验后的装载策略",
  },
];

export const DEFAULT_SKILL_ID = DATA_SKILLS[0]!.id;

const SKILL_STORAGE_KEY = "data-tasks:skill:v1";
const ACTIVE_LLM_STORAGE_KEY = "data-tasks:active-llm:v1";
const RIGHT_PANEL_WIDTH_STORAGE_KEY = "data-tasks:right-panel-width:v1";

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

export function loadSelectedSkillId(): string {
  if (typeof window === "undefined") return DEFAULT_SKILL_ID;
  try {
    const raw = window.localStorage.getItem(SKILL_STORAGE_KEY);
    if (!raw) return DEFAULT_SKILL_ID;
    return DATA_SKILLS.some((skill) => skill.id === raw) ? raw : DEFAULT_SKILL_ID;
  } catch {
    return DEFAULT_SKILL_ID;
  }
}

export function persistSelectedSkillId(skillId: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(SKILL_STORAGE_KEY, skillId);
  } catch {
    // Ignore quota errors — selection stays in-memory.
  }
}

export function getDataSkill(skillId: string): DataSkill {
  const skill = DATA_SKILLS.find((item) => item.id === skillId);
  if (!skill) {
    return DATA_SKILLS[0]!;
  }
  return skill;
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
  /**
   * Reserved for the backend secretRef model: once a config-management API
   * stores credentials, this points at the server-side secret. Frontend never
   * holds the plaintext after that. Unused until backend lands.
   */
  secretRef?: string;
  /** Reserved: connectivity result from backend `test`. Defaults to untested. */
  status?: ConfigItemStatus;
}

export type ConfigFieldDef = {
  key: string;
  label: string;
  placeholder?: string;
  helpText?: string;
  inputType?: "text" | "password" | "url" | "select" | "number";
  options?: Array<{ value: string; label: string }>;
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
  readOnly?: (item: WorkspaceConfigItem) => boolean;
};

/**
 * Backend capability flags. All false today (see
 * frontend-backend-capability-requests.md). Flip to true when the matching
 * backend capability ships; gated fields/options then appear automatically.
 */
export type BackendCapability =
  | "datasource.server" // PostgreSQL / MySQL adapters (#2)
  | "datasource.queryPolicy" // per-datasource maxRows/timeout wired (#5)
  | "llm.samplingParams" // temperature/maxTokens consumed (#4)
  | "artifact.export"; // artifact preview/download API (#9)

export const BACKEND_CAPABILITIES: Record<BackendCapability, boolean> = {
  "datasource.server": false,
  "datasource.queryPolicy": false,
  "llm.samplingParams": false,
  "artifact.export": false,
};

export function hasCapability(capability: BackendCapability): boolean {
  return BACKEND_CAPABILITIES[capability];
}

/**
 * Datasource engine types. Only types the backend Data Gateway can actually
 * adapt are exposed: duckdb / sqlite / csv / xlsx (file-backed). postgresql/mysql
 * are disabled placeholders and bigquery/snowflake have no backend code, so they
 * are intentionally NOT offered here (kept only in the backend roadmap doc).
 */
export const DB_TYPE_OPTIONS = [
  { value: "duckdb", label: "DuckDB（文件）" },
  { value: "sqlite", label: "SQLite（文件）" },
  { value: "csv", label: "CSV 文件" },
  { value: "xlsx", label: "Excel 文件" },
] as const;

/** Server DB types, gated behind `datasource.server` capability. */
export const DB_SERVER_TYPE_OPTIONS = [
  { value: "postgresql", label: "PostgreSQL" },
  { value: "mysql", label: "MySQL" },
] as const;

export const DB_MODE_OPTIONS = [
  { value: "readonly", label: "只读（当前唯一支持）" },
] as const;

const DB_SERVER_TYPES = DB_SERVER_TYPE_OPTIONS.map((option) => option.value);

function dbTypeOf(settings: Record<string, string>): string {
  return settings.type ?? "duckdb";
}

function isDbServerType(settings: Record<string, string>): boolean {
  return DB_SERVER_TYPES.includes(
    dbTypeOf(settings) as (typeof DB_SERVER_TYPES)[number],
  );
}

/** DB type list grows with backend capability (server types appear when on). */
function dbTypeOptions(): Array<{ value: string; label: string }> {
  return hasCapability("datasource.server")
    ? [...DB_TYPE_OPTIONS, ...DB_SERVER_TYPE_OPTIONS]
    : [...DB_TYPE_OPTIONS];
}

/** Chat models use one OpenAI-compatible provider path; vendor choice lives in baseUrl/modelName. */
export const LLM_PROVIDER_OPTIONS = [
  { value: "openai-compatible", label: "OpenAI 兼容 (LLM_PROVIDER=openai-compatible)" },
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
  return `${items.length} 项默认可用`;
}

export function getLlmDisplayLabel(item: WorkspaceConfigItem): string {
  const normalized = normalizeLlmSettings(item.settings);
  return normalized.modelName || item.name;
}

export function getLlmOptionSubtitle(item: WorkspaceConfigItem): string {
  if (item.builtin) return "服务端环境变量";
  const normalized = normalizeLlmSettings(item.settings);
  return [normalized.provider, item.description].filter(Boolean).join(" · ");
}

export function getEnabledLlmItems(
  workspaceConfig: WorkspaceConfigStore,
): WorkspaceConfigItem[] {
  return workspaceConfig.llm;
}

export function loadActiveLlmId(workspaceConfig: WorkspaceConfigStore): string {
  const enabled = getEnabledLlmItems(workspaceConfig);
  const fallback = enabled[0]?.id ?? "server-default";
  if (typeof window === "undefined") return fallback;
  try {
    const raw = window.localStorage.getItem(ACTIVE_LLM_STORAGE_KEY);
    if (!raw) return fallback;
    return enabled.some((item) => item.id === raw) ? raw : fallback;
  } catch {
    return fallback;
  }
}

export function persistActiveLlmId(llmId: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(ACTIVE_LLM_STORAGE_KEY, llmId);
  } catch {
    // Ignore quota errors.
  }
}

/** MCP transport types aligned with common MCP client configs. */
export const MCP_TRANSPORT_OPTIONS = [
  { value: "sse", label: "SSE (Server-Sent Events)" },
  { value: "streamable-http", label: "Streamable HTTP" },
  { value: "stdio", label: "stdio（本地命令）" },
] as const;

export function normalizeMcpSettings(
  settings?: Record<string, string>,
): {
  transport: string;
  serverUrl: string;
  apiKey: string;
} {
  return {
    transport: settings?.transport ?? "sse",
    serverUrl: settings?.serverUrl ?? settings?.url ?? settings?.endpoint ?? "",
    apiKey: settings?.apiKey ?? settings?.api_key ?? "",
  };
}

export function summarizeMcpItems(
  items: WorkspaceConfigItem[],
  emptyLabel: string,
): string {
  if (items.length === 0) return emptyLabel;
  if (items.length === 1) return items[0].name;
  return `${items.length} 项默认可用`;
}

export function getEnabledMcpItems(
  workspaceConfig: WorkspaceConfigStore,
): WorkspaceConfigItem[] {
  return workspaceConfig.mcp;
}

export const WORKSPACE_CONFIG_FIELDS: Record<
  WorkspaceConfigKind,
  ConfigFieldDef[]
> = {
  // 仅暴露后端 Data Gateway 真正能消费的字段。多数据库连接参数（host/账号/
  // BigQuery/Snowflake 等）后端 adapter 未实现，故不在 UI 出现；契约见后端文档。
  db: [
    {
      key: "datasourceId",
      label: "数据源 ID",
      placeholder: "my-dataset",
      helpText: "传给 Agent 的稳定标识（forwardedProps.datasourceId）。内置项不可改。",
      required: true,
      readOnly: (item) => !!item.builtin,
      fullWidth: true,
    },
    {
      key: "type",
      label: "数据源类型",
      inputType: "select",
      options: dbTypeOptions(),
      helpText: "当前后端可连接：DuckDB / SQLite / CSV / Excel（文件）。",
      required: true,
      readOnly: (item) => !!item.builtin,
    },
    {
      key: "mode",
      label: "访问模式",
      inputType: "select",
      options: [...DB_MODE_OPTIONS],
      helpText: "强制只读，写操作在 Data Gateway 层被拒绝。",
      readOnly: () => true,
    },
    {
      key: "filePath",
      label: "文件路径",
      placeholder: "上传本地文件，或粘贴已有服务端路径",
      helpText:
        "选择本地 DuckDB / SQLite / CSV / Excel 文件上传后自动填入服务端路径；也可手动填写 API 进程可访问的路径。",
      required: true,
      fullWidth: true,
      visibleWhen: (s) => !isDbServerType(s),
    },
    // 以下为 gated-off：后端 PostgreSQL/MySQL adapter（#2）就绪后翻 datasource.server。
    {
      key: "host",
      label: "Host",
      placeholder: "127.0.0.1",
      required: true,
      requiresCapability: "datasource.server",
      visibleWhen: isDbServerType,
    },
    {
      key: "port",
      label: "Port",
      inputType: "number",
      placeholder: "5432",
      required: true,
      requiresCapability: "datasource.server",
      visibleWhen: isDbServerType,
    },
    {
      key: "database",
      label: "Database",
      placeholder: "analytics",
      required: true,
      requiresCapability: "datasource.server",
      visibleWhen: isDbServerType,
    },
    {
      key: "schema",
      label: "Schema",
      placeholder: "public",
      requiresCapability: "datasource.server",
      visibleWhen: isDbServerType,
    },
    {
      key: "username",
      label: "用户名",
      placeholder: "readonly_user",
      required: true,
      requiresCapability: "datasource.server",
      visibleWhen: isDbServerType,
    },
    {
      key: "password",
      label: "密码",
      inputType: "password",
      placeholder: "••••••",
      helpText: "仅保存在浏览器 localStorage，不经 AG-UI 协议外发；后端由 secretRef 解析。",
      requiresCapability: "datasource.server",
      visibleWhen: isDbServerType,
    },
    // gated-off：后端把 SQL_* / per-datasource 策略接入 Gateway（#5）后翻 datasource.queryPolicy。
    {
      key: "maxRows",
      label: "最大返回行数",
      inputType: "number",
      placeholder: "10000",
      requiresCapability: "datasource.queryPolicy",
    },
    {
      key: "timeoutMs",
      label: "查询超时 (ms)",
      inputType: "number",
      placeholder: "30000",
      requiresCapability: "datasource.queryPolicy",
    },
  ],
  // 后端 packages/knowledge 仅有接口、无实现，先保留最小骨架。
  kb: [
    {
      key: "indexName",
      label: "索引名称",
      placeholder: "metrics-docs",
      required: true,
      fullWidth: true,
    },
    {
      key: "retrievalTopK",
      label: "检索 Top K",
      inputType: "number",
      placeholder: "5",
    },
  ],
  // 后端无 MCP 实现，先保留最小连接骨架。
  mcp: [
    {
      key: "transport",
      label: "Transport",
      inputType: "select",
      options: [...MCP_TRANSPORT_OPTIONS],
      helpText: "远程 MCP 常用 SSE 或 Streamable HTTP；stdio 用于本地进程。",
      required: true,
      fullWidth: true,
    },
    {
      key: "serverUrl",
      label: "Endpoint / 启动命令",
      placeholder: "https://example.com/mcp/sse",
      helpText: "远程传输填 MCP 服务 URL；stdio 填本地启动命令。",
      required: true,
      fullWidth: true,
    },
  ],
  llm: [
    {
      key: "provider",
      label: "Provider",
      inputType: "select",
      options: [...LLM_PROVIDER_OPTIONS],
      helpText:
        "对应服务端 LLM_PROVIDER。所有 chat 模型都通过 OpenAI 兼容 /chat/completions 调用。",
      required: true,
      readOnly: (item) => !!item.builtin,
      fullWidth: true,
    },
    {
      key: "baseUrl",
      label: "Base URL",
      inputType: "url",
      placeholder: "https://dashscope.aliyuncs.com/compatible-mode/v1",
      helpText: "对应 LLM_BASE_URL，OpenAI 兼容 Chat Completions 根路径（不含 /chat/completions）。",
      required: true,
      readOnly: (item) => !!item.builtin,
      fullWidth: true,
    },
    {
      key: "apiKey",
      label: "API Key",
      inputType: "password",
      placeholder: "sk-...",
      helpText:
        "对应 LLM_API_KEY。仅保存在浏览器 localStorage，不经 AG-UI 协议外发；当前 dataFoundry 仍读取服务端环境变量，后端接入后由 secretRef 解析。",
      readOnly: (item) => !!item.builtin,
      fullWidth: true,
    },
    {
      key: "modelName",
      label: "Model Name",
      placeholder: "qwen-plus",
      helpText: "对应 LLM_MODEL，即 chat model id（如 gpt-4o、qwen-plus、deepseek-chat）。",
      required: true,
      readOnly: (item) => !!item.builtin,
      fullWidth: true,
    },
    // gated-off：后端确认按 run 消费采样参数（#4）后翻 llm.samplingParams。
    {
      key: "temperature",
      label: "Temperature",
      inputType: "number",
      placeholder: "0.2",
      requiresCapability: "llm.samplingParams",
      readOnly: (item) => !!item.builtin,
    },
    {
      key: "maxTokens",
      label: "Max Tokens",
      inputType: "number",
      placeholder: "4096",
      requiresCapability: "llm.samplingParams",
      readOnly: (item) => !!item.builtin,
    },
  ],
  // Skill 为上传包模型：元数据只读展示，正文经上传写入 settings.packageContent。
  skill: [
    {
      key: "packageFileName",
      label: "包文件",
      readOnly: () => true,
      fullWidth: true,
    },
    {
      key: "packageFormat",
      label: "格式",
      readOnly: () => true,
    },
    {
      key: "packageVersion",
      label: "版本",
      readOnly: () => true,
    },
    {
      key: "allowedTools",
      label: "允许工具",
      readOnly: () => true,
      fullWidth: true,
      visibleWhen: (settings) => (settings.allowedTools ?? "").trim().length > 0,
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
      };
    case "kb":
      return { indexName: name || "custom-kb", retrievalTopK: "5" };
    case "mcp":
      return {
        transport: "sse",
        serverUrl: name ? `https://${name}` : "",
        apiKey: "",
      };
    case "llm":
      return {
        provider: "openai-compatible",
        baseUrl: "",
        apiKey: "",
        modelName: name || "qwen-plus",
      };
    case "skill":
      return {
        packageFileName: "",
        packageFormat: "",
        packageVersion: "",
        packageSource: "",
        allowedTools: "",
        packageContent: "",
      };
  }
}

/** Fields that should currently render/validate given the item's settings. */
export function visibleConfigFields(
  panel: WorkspaceConfigKind,
  settings: Record<string, string>,
): ConfigFieldDef[] {
  return WORKSPACE_CONFIG_FIELDS[panel].filter(
    (field) =>
      (!field.requiresCapability || hasCapability(field.requiresCapability)) &&
      (!field.visibleWhen || field.visibleWhen(settings)),
  );
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

export type WorkspaceConfigStore = Record<
  WorkspaceConfigKind,
  WorkspaceConfigItem[]
>;

const WORKSPACE_CONFIG_STORAGE_KEY = "data-tasks:workspace-config:v1";

function defaultWorkspaceConfig(): WorkspaceConfigStore {
  return {
    db: [],
    kb: [],
    mcp: [],
    llm: [
      {
        id: "server-default",
        name: "default",
        description: "使用 dataFoundry 服务端 .env 中的 LLM_PROVIDER / LLM_BASE_URL / LLM_MODEL",
        enabled: true,
        builtin: true,
        settings: {
          provider: "服务端 (LLM_PROVIDER)",
          baseUrl: "服务端 (LLM_BASE_URL)",
          apiKey: "",
          modelName: "服务端 (LLM_MODEL)",
        },
      },
    ],
    skill: DATA_SKILLS.map((skill) => ({
      id: skill.id,
      name: skill.name,
      description: skill.description,
      enabled: true,
      builtin: true,
      settings: builtinSkillSettings(skill.id),
    })),
  };
}

function mergeWorkspaceConfig(stored: WorkspaceConfigStore): WorkspaceConfigStore {
  const defaults = defaultWorkspaceConfig();
  const mergeKind = (kind: WorkspaceConfigKind): WorkspaceConfigItem[] => {
    const builtinIds = new Set(defaults[kind].map((item) => item.id));
    const storedBuiltin = stored[kind].filter((item) => builtinIds.has(item.id));
    const storedCustom = stored[kind].filter((item) => !builtinIds.has(item.id));
    const mergedBuiltin = defaults[kind].map((item) => {
      const hit = storedBuiltin.find((storedItem) => storedItem.id === item.id);
      if (!hit) return item;
      const mergedSettings =
        kind === "llm"
          ? {
              ...item.settings,
              ...normalizeLlmSettings(hit.settings),
            }
          : kind === "skill"
            ? {
                ...item.settings,
                ...normalizeSkillSettings(hit.settings),
              }
            : { ...item.settings, ...hit.settings };
      return {
        ...item,
        enabled: true,
        name: hit.name || item.name,
        description: hit.description || item.description,
        settings: mergedSettings,
      };
    });
    return [
      ...mergedBuiltin,
      ...storedCustom.map((item) => ({
        ...item,
        settings:
          kind === "llm"
            ? {
                ...defaultSettingsForKind(kind, item.name),
                ...normalizeLlmSettings(item.settings),
              }
            : kind === "skill"
              ? {
                  ...defaultSettingsForKind(kind, item.name),
                  ...normalizeSkillSettings(item.settings),
                }
              : { ...defaultSettingsForKind(kind, item.name), ...item.settings },
      })),
    ];
  };

  return {
    db: mergeKind("db"),
    kb: mergeKind("kb"),
    mcp: mergeKind("mcp"),
    llm: mergeKind("llm"),
    skill: mergeKind("skill"),
  };
}

function isLegacyWorkspaceConfigStore(
  value: unknown,
): value is Omit<WorkspaceConfigStore, "mcp"> {
  if (typeof value !== "object" || value === null) return false;
  const record = value as Record<string, unknown>;
  return (["db", "kb", "llm", "skill"] as const).every((kind) =>
    Array.isArray(record[kind]),
  );
}

function isWorkspaceConfigStore(value: unknown): value is WorkspaceConfigStore {
  if (typeof value !== "object" || value === null) return false;
  const record = value as Record<string, unknown>;
  return (["db", "kb", "mcp", "llm", "skill"] as const).every((kind) =>
    Array.isArray(record[kind]),
  );
}

function normalizeStoredWorkspaceConfig(value: unknown): WorkspaceConfigStore {
  if (isWorkspaceConfigStore(value)) return value;
  if (isLegacyWorkspaceConfigStore(value)) {
    return { ...value, mcp: [] };
  }
  return defaultWorkspaceConfig();
}

export function loadWorkspaceConfig(): WorkspaceConfigStore {
  if (typeof window === "undefined") return defaultWorkspaceConfig();
  try {
    const raw = window.localStorage.getItem(WORKSPACE_CONFIG_STORAGE_KEY);
    if (!raw) return defaultWorkspaceConfig();
    const parsed = JSON.parse(raw) as unknown;
    return mergeWorkspaceConfig(normalizeStoredWorkspaceConfig(parsed));
  } catch {
    return defaultWorkspaceConfig();
  }
}

export function persistWorkspaceConfig(store: WorkspaceConfigStore): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(WORKSPACE_CONFIG_STORAGE_KEY, JSON.stringify(store));
  } catch {
    // Ignore quota errors.
  }
}

export function summarizeConfigItems(
  items: WorkspaceConfigItem[],
  emptyLabel: string,
): string {
  if (items.length === 0) return emptyLabel;
  if (items.length === 1) return items[0].name;
  return `${items.length} 项默认可用`;
}

export function createWorkspaceConfigItem(
  kind: WorkspaceConfigKind,
  name: string,
  description: string,
): WorkspaceConfigItem {
  const id =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `custom-${Date.now()}`;
  return {
    id,
    name,
    description,
    enabled: true,
    settings: defaultSettingsForKind(kind, name),
  };
}
