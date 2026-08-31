import type { BaseEvent, EventType } from "@ag-ui/core";

export type ApiResult<T> = {
  success: boolean;
  data?: T;
  error?: {
    code: AppErrorCode;
    message: string;
    details?: unknown;
  };
};

export type MeResponse = {
  id: string;
  email?: string;
  display_name?: string;
};

export type Citation = {
  document_id: string;
  chunk_id: string;
  filename: string;
  quote: string;
  page_number?: number;
  section_title?: string;
  score?: number;
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

/** A rectangular cell range in a table preview, using 0-based inclusive indices. */
export type EvidenceCellRange = {
  r0: number;
  c0: number;
  r1: number;
  c1: number;
};

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

export type RunEventEnvelope = {
  type: EventType;
  run_id: string;
  session_id: string;
  seq: number;
  ts: string;
  event: BaseEvent;
};

export type DataSourceSummary = {
  id: string;
  name: string;
  type: string;
  status: "ready" | "disabled" | "failed";
  description?: string;
};

export type ToolName =
  | "retrieve_knowledge"
  | "list_data_sources"
  | "inspect_schema"
  | "preview_table"
  | "run_sql_readonly"
  | "profile_dataset"
  | "generate_report"
  | "export_artifact"
  /** @deprecated Runtime no longer registers this tool; eligible files are auto-ingested as Session Outputs. */
  | "publish_artifact"
  | "promote_workspace_file"
  | "list_workspace_files"
  | "read_workspace_file";

export type RetrieveKnowledgeToolInput = {
  collection_id: string;
  query: string;
  top_k?: number;
};

export type ListDataSourcesToolInput = {
  enabled_only?: boolean;
};

export type InspectSchemaToolInput = {
  datasource_id: string;
  table_names?: string[];
};

export type PreviewTableToolInput = {
  datasource_id: string;
  table: string;
  limit?: number;
};

export type RunSqlReadonlyToolInput = {
  datasource_id: string;
  sql: string;
  limit?: number;
  timeout_ms?: number;
};

export type ProfileDatasetToolInput = {
  datasource_id: string;
  table?: string;
  file_id?: string;
};

/**
 * Chart artifact `preview_json` contract (R-015). The backend produces a chart artifact
 * whose `preview_json` is a `ChartPreview`; the frontend renders it (bar/line/pie). This
 * is a backend-owned rule-based structure — there is no agent `create_chart` tool; the
 * model never assembles chart data.
 */
export type ChartPreviewPoint = { label: string; value: number };
export type ChartPreviewSeries = { name: string; points: ChartPreviewPoint[] };
export type ChartPreviewType = "bar" | "line" | "pie";
export type ChartPreview = {
  /** Chart kind; the frontend falls back to `bar` when absent/unknown. */
  chartType: ChartPreviewType;
  /** Optional unit label rendered on the value axis (e.g. "单", "元"). */
  unit?: string;
  /** Single-series data. At least one of `points`/`series` must be non-empty. */
  points: ChartPreviewPoint[];
  /** Multi-series data. When non-empty, takes precedence over `points`. */
  series?: ChartPreviewSeries[];
};

export type GenerateReportToolInput = {
  title: string;
  summary: string;
  artifact_ids?: string[];
  citations?: Citation[];
  format: "markdown" | "html";
};

export type ExportArtifactToolInput = {
  artifact_id: string;
  format?: "json" | "csv" | "html" | "md" | "png";
};

/** @deprecated Runtime no longer registers this tool; eligible files are auto-ingested as Session Outputs. */
export type PublishArtifactToolInput = {
  path: string;
  name?: string;
  type?: ArtifactType;
  preview?: unknown;
};

export type PromoteWorkspaceFileToolInput = {
  /** Session-relative path of the file to promote into the cross-session workspace root. */
  path: string;
  filename?: string;
  description?: string;
};

export type ListWorkspaceFilesToolInput = {
  /** Optional subdirectory under the workspace root to list; defaults to the root. */
  path?: string;
};

export type ReadWorkspaceFileToolInput = {
  /** Workspace-root-relative path of the cross-session file to read (read-only). */
  path: string;
};

export type ToolInputMap = {
  retrieve_knowledge: RetrieveKnowledgeToolInput;
  list_data_sources: ListDataSourcesToolInput;
  inspect_schema: InspectSchemaToolInput;
  preview_table: PreviewTableToolInput;
  run_sql_readonly: RunSqlReadonlyToolInput;
  profile_dataset: ProfileDatasetToolInput;
  generate_report: GenerateReportToolInput;
  export_artifact: ExportArtifactToolInput;
  /** @deprecated Runtime no longer registers this tool; eligible files are auto-ingested as Session Outputs. */
  publish_artifact: PublishArtifactToolInput;
  promote_workspace_file: PromoteWorkspaceFileToolInput;
  list_workspace_files: ListWorkspaceFilesToolInput;
  read_workspace_file: ReadWorkspaceFileToolInput;
};

export type ArtifactType = "table" | "chart" | "markdown" | "html" | "file" | "image" | "citation_bundle";

export type ArtifactSummary = {
  id: string;
  type: ArtifactType;
  name: string;
  preview_json?: unknown;
  /**
   * Authoritative origin of the artifact so the frontend can link it back to the
   * producing run / tool call / step without heuristics (R-018). Populated from the
   * artifact record's `run_id` and its `metadata_json` when present.
   */
  run_id?: string;
  tool_call_id?: string;
  step_id?: string;
};

export type AppErrorCode =
  | "BAD_REQUEST"
  | "CONFLICT"
  | "CSRF_INVALID"
  | "DATASOURCE_TEST_FAILED"
  | "EMAIL_NOT_VERIFIED"
  | "FORBIDDEN"
  | "INTERNAL_ERROR"
  | "JOB_NOT_FOUND"
  | "PROVIDER_TEST_FAILED"
  | "REGISTRATION_CLOSED"
  | "REVISION_CONFLICT"
  | "SECRET_MASTER_KEY_REQUIRED"
  | "UNAUTHORIZED"
  | "RESOURCE_NOT_FOUND"
  | "NOT_ENABLED"
  | "UNSUPPORTED_FILE_TYPE"
  | "PARSE_FAILED"
  | "REINDEX_REQUIRED"
  | "RUN_NOT_BRANCHABLE"
  | "SQL_BLOCKED"
  | "SQL_TIMEOUT"
  | "PROVIDER_CONFIG_MISSING"
  | "PROVIDER_RATE_LIMITED"
  | "RATE_LIMITED";

export const createSuccessResult = <T>(data: T): ApiResult<T> => ({
  success: true,
  data
});

export const createErrorResult = (
  code: AppErrorCode,
  message: string,
  details?: unknown
): ApiResult<never> => ({
  success: false,
  error: {
    code,
    message,
    ...(details === undefined ? {} : { details })
  }
});

export type EnvVariableSpec = {
  name: string;
  required: boolean;
  default_value?: string;
  description: string;
};

export type EnvConfig = {
  api: {
    host: string;
    port: number;
  };
  llm: {
    provider: string;
    model: string;
    base_url: string;
    api_key?: string;
  };
  embedding: {
    provider: string;
    model: string;
    dim: number;
    output_type: "dense";
    base_url: string;
    api_key?: string;
  };
  storage: {
    root_dir: string;
    secret_master_key?: string;
  };
  memory: {
    completed_extraction_timeout_ms: number;
  };
  sql: {
    default_limit: number;
    max_limit: number;
    timeout_ms: number;
  };
};

export const ENV_VARIABLE_SPECS: EnvVariableSpec[] = [
  { name: "API_HOST", required: false, default_value: "127.0.0.1", description: "Agent runtime bind host." },
  { name: "API_PORT", required: false, default_value: "8787", description: "Agent runtime bind port." },
  {
    name: "LLM_PROVIDER",
    required: false,
    default_value: "openai-compatible",
    description: "Chat model provider. Only OpenAI-compatible chat completions are supported."
  },
  { name: "LLM_MODEL", required: false, default_value: "qwen-plus", description: "Chat model name." },
  { name: "LLM_BASE_URL", required: true, description: "OpenAI-compatible chat completions base URL." },
  {
    name: "AGENT_MODEL_CONTEXT_WINDOW",
    required: false,
    default_value: "128000",
    description: "Model total context window (tokens) used for prompt budget planning. Set to match the chat model."
  },
  { name: "LLM_API_KEY", required: true, description: "Chat provider API key." },
  { name: "EMBEDDING_PROVIDER", required: false, default_value: "bailian", description: "Embedding provider." },
  {
    name: "EMBEDDING_MODEL",
    required: false,
    default_value: "text-embedding-v4",
    description: "Embedding model name."
  },
  { name: "EMBEDDING_DIM", required: false, default_value: "1024", description: "Embedding vector dimension." },
  { name: "EMBEDDING_OUTPUT_TYPE", required: false, default_value: "dense", description: "Embedding output type." },
  { name: "EMBEDDING_BASE_URL", required: true, description: "OpenAI-compatible embeddings base URL." },
  { name: "EMBEDDING_API_KEY", required: true, description: "Embedding provider API key." },
  { name: "STORAGE_ROOT_DIR", required: false, default_value: "storage", description: "Local storage root." },
  { name: "METADATA_DB_PATH", required: false, description: "SQLite metadata database path." },
  { name: "SECRET_MASTER_KEY", required: false, description: "Master key for local AES-GCM credential storage." },
  {
    name: "MEMORY_EXTRACTION_TIMEOUT_MS",
    required: false,
    default_value: "2000",
    description: "Maximum wait for post-run summary and long-term memory extraction in ms."
  },
  { name: "SQL_DEFAULT_LIMIT", required: false, default_value: "100", description: "Default read-only SQL row limit." },
  { name: "SQL_MAX_LIMIT", required: false, default_value: "1000", description: "Maximum read-only SQL row limit." },
  { name: "SQL_TIMEOUT_MS", required: false, default_value: "10000", description: "Read-only SQL timeout in ms." }
];

export const createEnvConfig = (env: Record<string, string | undefined>): EnvConfig => ({
  api: {
    host: env.API_HOST ?? "127.0.0.1",
    port: Number.parseInt(env.API_PORT ?? "8787", 10)
  },
  llm: {
    provider: env.LLM_PROVIDER ?? "openai-compatible",
    model: env.LLM_MODEL ?? "qwen-plus",
    base_url: env.LLM_BASE_URL ?? "https://dashscope.aliyuncs.com/compatible-mode/v1",
    ...(env.LLM_API_KEY ? { api_key: env.LLM_API_KEY } : {})
  },
  embedding: {
    provider: env.EMBEDDING_PROVIDER ?? "bailian",
    model: env.EMBEDDING_MODEL ?? "text-embedding-v4",
    dim: Number.parseInt(env.EMBEDDING_DIM ?? "1024", 10),
    output_type: "dense",
    base_url: env.EMBEDDING_BASE_URL ?? "https://dashscope.aliyuncs.com/compatible-mode/v1",
    ...(env.EMBEDDING_API_KEY ? { api_key: env.EMBEDDING_API_KEY } : {})
  },
  storage: {
    root_dir: env.STORAGE_ROOT_DIR ?? "storage",
    ...(env.SECRET_MASTER_KEY ? { secret_master_key: env.SECRET_MASTER_KEY } : {})
  },
  memory: {
    completed_extraction_timeout_ms: Number.parseInt(env.MEMORY_EXTRACTION_TIMEOUT_MS ?? "2000", 10)
  },
  sql: {
    default_limit: Number.parseInt(env.SQL_DEFAULT_LIMIT ?? "100", 10),
    max_limit: Number.parseInt(env.SQL_MAX_LIMIT ?? "1000", 10),
    timeout_ms: Number.parseInt(env.SQL_TIMEOUT_MS ?? "10000", 10)
  }
});
