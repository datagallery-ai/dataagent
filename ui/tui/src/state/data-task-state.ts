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
  | "delegate" // task tool handing work to a subagent
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
    case "task":
      return "delegate";
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
    case "delegate":
      return "子代理";
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
  createdByToolCallId?: string | undefined;
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



export function normalizeLlmSettings(
  settings?: Record<string, string>,
): {
  provider: string;
  baseUrl: string;
  apiKey: string;
  modelName: string;
} {
  return {
    provider: settings?.provider ?? "openai-compatible",
    baseUrl: settings?.baseUrl ?? settings?.base_url ?? "",
    apiKey: settings?.apiKey ?? settings?.api_key ?? "",
    modelName:
      settings?.modelName ?? settings?.model ?? settings?.model_name ?? "",
  };
}

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
        description: "使用服务端 .env 中的 LLM_MODEL、LLM_BASE_URL、LLM_API_KEY",
        enabled: true,
        builtin: true,
        settings: {
          provider: "openai-compatible",
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

function isWorkspaceConfigStore(value: unknown): value is WorkspaceConfigStore {
  if (typeof value !== "object" || value === null) return false;
  const record = value as Record<string, unknown>;
  return (["db", "kb", "mcp", "llm", "skill"] as const).every((kind) =>
    Array.isArray(record[kind]),
  );
}

export function loadWorkspaceConfig(): WorkspaceConfigStore {
  if (typeof window === "undefined") return defaultWorkspaceConfig();
  try {
    const raw = window.localStorage.getItem(WORKSPACE_CONFIG_STORAGE_KEY);
    if (!raw) return defaultWorkspaceConfig();
    const parsed = JSON.parse(raw) as unknown;
    return isWorkspaceConfigStore(parsed) ? mergeWorkspaceConfig(parsed) : defaultWorkspaceConfig();
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
