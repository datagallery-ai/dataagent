import { z } from "zod";

// ==================== Common Schemas ====================

const CommonFieldsSchema = z.object({
  id: z.string(),
  name: z.string(),
  description: z.string(),
  defaultEnabled: z.boolean(),
  builtin: z.boolean(),
  createdAt: z.string(),
  updatedAt: z.string(),
  revision: z.number(),
});

const ApiResultSchema = <T extends z.ZodTypeAny>(dataSchema: T) =>
  z.union([
    z.object({
      success: z.literal(true),
      data: dataSchema,
    }),
    z.object({
      success: z.literal(false),
      error: z.object({
        code: z.string(),
        message: z.string(),
        details: z.record(z.string(), z.unknown()).optional(),
      }),
    }),
  ]);

// ==================== Datasource Schemas ====================

const DatasourceConnectionSchema = z.object({
  host: z.string().optional(),
  port: z.number().optional(),
  database: z.string().optional(),
  schema: z.string().optional(),
  username: z.string().optional(),
  account: z.string().optional(),
  warehouse: z.string().optional(),
  role: z.string().optional(),
  projectId: z.string().optional(),
  dataset: z.string().optional(),
  location: z.string().optional(),
  connectString: z.string().optional(),
  uri: z.string().optional(),
  url: z.string().optional(),
  node: z.string().optional(),
  keyPattern: z.string().optional(),
  indexPattern: z.string().optional(),
  connectionString: z.string().optional(),
  catalog: z.string().optional(),
  path: z.string().optional(),
  warehouseId: z.string().optional(),
  token: z.string().optional(),
  transport: z.string().optional(),
  auth: z.string().optional(),
  secretRef: z.string().optional(),
  filePath: z.string().optional(),
});

const DatasourceSchema = CommonFieldsSchema.extend({
  type: z.enum([
    "duckdb",
    "postgresql",
    "mysql",
    "sqlite",
    "csv",
    "xlsx",
    "clickhouse",
    "snowflake",
    "bigquery",
    "sqlserver",
    "oracle",
    "mongodb",
    "gaussdb",
    "access",
    "redis",
    "starrocks",
    "trino",
    "presto",
    "spark",
    "databricks",
    "redshift",
    "elasticsearch",
    "opensearch",
    "doris",
    "mariadb",
    "tidb",
    "oceanbase",
    "greenplum",
  ]),
  mode: z.enum(["readonly"]).default("readonly"),
  connection: DatasourceConnectionSchema.optional(),
  config: z.record(z.string(), z.unknown()).optional(),
  introspection: z
    .object({
      cache: z.boolean(),
      refreshIntervalSec: z.number(),
      tableAllowlist: z.array(z.string()),
    })
    .optional(),
  queryPolicy: z
    .object({
      maxRows: z.number(),
      timeoutMs: z.number(),
      denyWrite: z.boolean(),
      maskFields: z.array(z.string()),
    })
    .optional(),
  samplePolicy: z
    .object({
      allowSample: z.boolean(),
      maxSampleRows: z.number(),
    })
    .optional(),
  connectionStatus: z.enum(["connected", "failed", "untested", "disabled"]).optional(),
});

const DatasourceListSchema = z.array(DatasourceSchema);

const DatasourceTestResultSchema = z.object({
  status: z.string(),
  latencyMs: z.number().optional(),
  detail: z.record(z.string(), z.unknown()).optional(),
});

const DatasourceSchemaResponseSchema = z.object({
  tables: z.array(
    z.object({
      name: z.string(),
      columns: z.array(
        z.object({
          name: z.string(),
          type: z.string(),
          nullable: z.boolean().optional(),
        })
      ),
    })
  ),
  revision: z.number().optional(),
  cachedAt: z.string().optional(),
});

// ==================== Model Profile Schemas ====================

const ModelProfileSchema = CommonFieldsSchema.extend({
  provider: z.string().default("openai-compatible"),
  baseUrl: z.string().optional(),
  modelName: z.string(),
  secretRef: z.string().optional(),
  temperature: z.number().optional(),
  maxTokens: z.number().optional(),
  timeoutMs: z.number().optional(),
  fallbackProfileId: z.string().optional(),
  capabilities: z
    .object({
      reasoning: z.string().optional(),
      toolCall: z.string().optional(),
    })
    .optional(),
  connectionStatus: z.enum(["connected", "failed", "untested", "disabled"]).optional(),
});

const ModelProfileListSchema = z.array(ModelProfileSchema);

const ModelTestResultSchema = z.object({
  status: z.string(),
  latencyMs: z.number().optional(),
  detail: z.record(z.string(), z.unknown()).optional(),
});

// ==================== Skill Schemas ====================

const SkillSchema = CommonFieldsSchema.extend({
  version: z.string().optional(),
  packageFormat: z.enum(["skill-md", "zip"]),
  packageFileName: z.string().optional(),
  packageSource: z.string().optional(),
  allowedTools: z.array(z.string()).optional(),
  manifest: z
    .object({
      entry: z.string(),
      files: z.array(z.string()),
      sizeBytes: z.number(),
    })
    .optional(),
  defaultDbIds: z.array(z.string()).optional(),
  defaultKbIds: z.array(z.string()).optional(),
  defaultMcpIds: z.array(z.string()).optional(),
  modelProfileId: z.string().optional(),
  validationStatus: z.enum(["valid", "invalid", "untested"]).optional(),
});

const SkillListSchema = z.array(SkillSchema);

// ==================== MCP Server Schemas ====================

const McpServerSchema = CommonFieldsSchema.extend({
  transport: z.enum(["streamable-http", "sse"]),
  serverUrl: z.string(),
  authType: z.enum(["none", "bearer", "custom-header"]).optional(),
  secretRef: z.string().optional(),
  healthStatus: z.enum(["connected", "failed", "untested", "disabled"]).optional(),
  toolManifest: z
    .array(
      z.object({
        name: z.string(),
        description: z.string().optional(),
      })
    )
    .optional(),
});

const McpServerListSchema = z.array(McpServerSchema);

const McpTestResultSchema = z.object({
  status: z.string(),
  latencyMs: z.number().optional(),
  detail: z.record(z.string(), z.unknown()).optional(),
});

// ==================== Knowledge Base Schemas ====================

const KnowledgeBaseSchema = CommonFieldsSchema.extend({
  scope: z.enum(["personal", "workspace", "project"]).optional(),
  sources: z
    .array(
      z.object({
        type: z.enum(["file", "url", "db-doc"]),
        ref: z.string(),
      })
    )
    .optional(),
  embeddingProvider: z.string().optional(),
  embeddingModel: z.string().optional(),
  retrievalTopK: z.number().optional(),
  scoreThreshold: z.number().optional(),
  rerankEnabled: z.boolean().optional(),
  citationRequired: z.boolean().optional(),
  indexStatus: z.enum(["ready", "building", "failed", "empty"]).optional(),
});

const KnowledgeBaseListSchema = z.array(KnowledgeBaseSchema);

const KnowledgeSearchResultSchema = z.object({
  results: z.array(
    z.object({
      content: z.string(),
      score: z.number(),
      metadata: z.record(z.string(), z.unknown()).optional(),
    })
  ),
});

// ==================== Workspace Config Schemas ====================

const WorkspaceConfigSchema = z.object({
  datasources: DatasourceListSchema,
  knowledgeBases: KnowledgeBaseListSchema,
  mcpServers: McpServerListSchema,
  modelProfiles: ModelProfileListSchema,
  skills: SkillListSchema,
});

const RunDefaultsSchema = z.object({
  enabledDatasourceIds: z.array(z.string()),
  enabledKnowledgeIds: z.array(z.string()),
  enabledMcpServerIds: z.array(z.string()),
  activeDatasourceId: z.string().optional(),
  activeLlmProfileId: z.string().optional(),
  activeSkillId: z.string().optional(),
});

const CapabilitiesSchema = z.object({
  datasource: z
    .object({
      supportedTypes: z.array(z.string()),
      server: z.boolean().optional(),
      queryPolicy: z.boolean().optional(),
    })
    .optional(),
  knowledge: z
    .object({
      vectorSearch: z.boolean().optional(),
      rerank: z.boolean().optional(),
    })
    .optional(),
  mcp: z
    .object({
      transports: z.array(z.string()).optional(),
    })
    .optional(),
  llm: z
    .object({
      providers: z.array(z.string()).optional(),
      samplingParams: z.boolean().optional(),
    })
    .optional(),
  skill: z
    .object({
      formats: z.array(z.string()).optional(),
    })
    .optional(),
});

// ==================== Session Schemas ====================

const SessionActiveRunSchema = z.object({
  sessionId: z.string(),
  activeRunId: z.string(),
  status: z.enum(["queued", "running", "suspended"]),
  startedAt: z.string(),
  userInputPreview: z.string(),
});

const SessionListItemSchema = z.object({
  id: z.string(),
  threadId: z.string(),
  title: z.string().optional(),
  titleSource: z.string().optional(),
  createdAt: z.string().optional(),
  updatedAt: z.string().optional(),
  lastMessageAt: z.string().optional(),
  activeRun: SessionActiveRunSchema.nullish(),
});

const SessionListResponseSchema = z.object({
  sessions: z.array(SessionListItemSchema),
  nextCursor: z.string().optional(),
});

const ConversationContentPartSchema = z.object({
  type: z.enum(["reasoning", "text"]),
  text: z.string(),
});

const ConversationMessageSchema = z.object({
  id: z.string(),
  runId: z.string(),
  role: z.enum(["assistant", "user"]),
  source: z.enum(["agent", "client"]),
  messageId: z.string().optional(),
  contentText: z.string(),
  contentParts: z.array(ConversationContentPartSchema).optional(),
  content: z
    .object({
      parts: z.array(ConversationContentPartSchema).optional(),
    })
    .passthrough()
    .nullable()
    .optional(),
  position: z.number(),
  createdAt: z.string(),
});

const ConversationSummarySchema = z.object({
  id: z.string(),
  sourceRunId: z.string().optional(),
  fromPosition: z.number(),
  toPosition: z.number(),
  summaryText: z.string(),
  createdAt: z.string(),
});

const ConversationRunEventRefSchema = z.object({
  runId: z.string(),
  eventCount: z.number(),
  firstSeq: z.number().optional(),
  lastSeq: z.number().optional(),
});

const ConversationCheckpointSchema = z.object({
  runId: z.string(),
  status: z.enum(["queued", "running", "suspended", "completed", "failed", "canceled"]),
  messageStartPosition: z.number().optional(),
  messageEndPosition: z.number().optional(),
  firstEventSeq: z.number().optional(),
  lastEventSeq: z.number().optional(),
  startedAt: z.string(),
  finishedAt: z.string().optional(),
  errorMessage: z.string().optional(),
});

const ConversationToolCallSchema = z.object({
  runId: z.string(),
  id: z.string().optional(),
  toolCallId: z.string(),
  status: z.enum(["completed", "failed", "pending"]),
  name: z.string().optional(),
  toolName: z.string().optional(),
  args: z.unknown().optional(),
  result: z.unknown().optional(),
  callEventSeq: z.number().optional(),
  endEventSeq: z.number().optional(),
  resultEventSeq: z.number().optional(),
  parentMessageId: z.string().optional(),
  resultMessageId: z.string().optional(),
  resultPreview: z.string().optional(),
});

const SessionConversationSchema = z.object({
  sessionId: z.string(),
  title: z.string().optional(),
  titleSource: z.string().optional(),
  updatedAt: z.string().optional(),
  messages: z.array(ConversationMessageSchema),
  summary: ConversationSummarySchema.optional(),
  runEventRefs: z.array(ConversationRunEventRefSchema),
  checkpoints: z.array(ConversationCheckpointSchema).optional(),
  toolCalls: z.array(ConversationToolCallSchema),
  activeRun: SessionActiveRunSchema.nullish(),
});

const SessionArtifactSchema = z.object({
  id: z.string(),
  type: z.string(),
  name: z.string(),
  runId: z.string().optional(),
  toolCallId: z.string().optional(),
  stepId: z.string().optional(),
  fileId: z.string().nullable().optional(),
  downloadUrl: z.string().optional(),
  mimeType: z.string().optional(),
  preview_json: z.unknown().optional(),
  preview_available: z.boolean().optional(),
  createdAt: z.string().optional(),
});

const SessionArtifactListSchema = z.object({
  artifacts: z.array(SessionArtifactSchema),
});

const SessionTitleSchema = z.object({
  sessionId: z.string(),
  title: z.string(),
  titleSource: z.string().optional(),
  updatedAt: z.string().optional(),
});

// ==================== Type Exports ====================

export type Datasource = z.infer<typeof DatasourceSchema>;
export type DatasourceList = z.infer<typeof DatasourceListSchema>;
export type DatasourceTestResult = z.infer<typeof DatasourceTestResultSchema>;
export type DatasourceSchemaResponse = z.infer<typeof DatasourceSchemaResponseSchema>;

export type ModelProfile = z.infer<typeof ModelProfileSchema>;
export type ModelProfileList = z.infer<typeof ModelProfileListSchema>;
export type ModelTestResult = z.infer<typeof ModelTestResultSchema>;

export type Skill = z.infer<typeof SkillSchema>;
export type SkillList = z.infer<typeof SkillListSchema>;

export type McpServer = z.infer<typeof McpServerSchema>;
export type McpServerList = z.infer<typeof McpServerListSchema>;
export type McpTestResult = z.infer<typeof McpTestResultSchema>;

export type KnowledgeBase = z.infer<typeof KnowledgeBaseSchema>;
export type KnowledgeBaseList = z.infer<typeof KnowledgeBaseListSchema>;
export type KnowledgeSearchResult = z.infer<typeof KnowledgeSearchResultSchema>;

export type WorkspaceConfig = z.infer<typeof WorkspaceConfigSchema>;
export type RunDefaults = z.infer<typeof RunDefaultsSchema>;
export type Capabilities = z.infer<typeof CapabilitiesSchema>;
export type SessionListItem = z.infer<typeof SessionListItemSchema>;
export type SessionListResponse = z.infer<typeof SessionListResponseSchema>;
export type ConversationMessage = z.infer<typeof ConversationMessageSchema>;
export type ConversationSummary = z.infer<typeof ConversationSummarySchema>;
export type ConversationRunEventRef = z.infer<typeof ConversationRunEventRefSchema>;
export type ConversationCheckpoint = z.infer<typeof ConversationCheckpointSchema>;
export type ConversationToolCall = z.infer<typeof ConversationToolCallSchema>;
export type SessionConversation = z.infer<typeof SessionConversationSchema>;
export type SessionArtifact = z.infer<typeof SessionArtifactSchema>;
export type SessionArtifactList = z.infer<typeof SessionArtifactListSchema>;
export type SessionTitle = z.infer<typeof SessionTitleSchema>;

// ==================== Client Configuration ====================

export interface ConfigClientConfig {
  baseUrl: string;
  timeout?: number | undefined;
  fetchImpl?: typeof fetch | undefined;
  onError?: ((error: ConfigClientError) => void) | undefined;
}

export class ConfigClientError extends Error {
  public code?: string | undefined;
  public statusCode?: number | undefined;
  public details?: Record<string, unknown> | undefined;

  constructor(
    message: string,
    code?: string,
    statusCode?: number,
    details?: Record<string, unknown>
  ) {
    super(message);
    this.name = "ConfigClientError";
    this.code = code;
    this.statusCode = statusCode;
    this.details = details;
  }
}

// ==================== Config Client ====================

export class ConfigClient {
  private baseUrl: string;
  private timeout: number;
  private fetchImpl: typeof fetch;
  private onError?: ((error: ConfigClientError) => void) | undefined;

  constructor(config: ConfigClientConfig) {
    this.baseUrl = config.baseUrl.replace(/\/$/, "");
    this.timeout = config.timeout ?? 30000;
    this.fetchImpl = config.fetchImpl ?? globalThis.fetch.bind(globalThis);
    this.onError = config.onError;
  }

  // ==================== Private Request Method ====================

  private async request<T>(
    method: "GET" | "POST" | "PATCH" | "DELETE",
    path: string,
    options?: {
      body?: unknown;
      params?: Record<string, string | number | boolean>;
      headers?: Record<string, string>;
      schema?: z.ZodTypeAny;
    }
  ): Promise<T> {
    const url = new URL(`${this.baseUrl}${path}`);

    if (options?.params) {
      Object.entries(options.params).forEach(([key, value]) => {
        url.searchParams.append(key, String(value));
      });
    }

    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), this.timeout);

    try {
      const fetchOptions: RequestInit = {
        method,
        headers: {
          "Content-Type": "application/json",
          ...options?.headers,
        },
        signal: controller.signal,
      };

      if (options?.body) {
        fetchOptions.body = JSON.stringify(options.body);
      }

      const response = await this.fetchImpl(url.toString(), fetchOptions).finally(() => clearTimeout(timeoutId));

      if (!response.ok) {
        throw await this.errorFromResponse(response);
      }

      const json = await response.json();

      // Validate with ApiResult wrapper
      if (options?.schema) {
        const resultSchema = ApiResultSchema(options.schema);
        const parsed = resultSchema.parse(json);

        if (!parsed.success) {
          throw new ConfigClientError(
            parsed.error.message,
            parsed.error.code,
            response.status,
            parsed.error.details
          );
        }

        return parsed.data as T;
      }

      // Fallback: assume success and return data directly
      if (json.success === false) {
        throw new ConfigClientError(
          json.error?.message || "Unknown error",
          json.error?.code,
          response.status,
          json.error?.details
        );
      }

      return (json.data || json) as T;
    } catch (error) {
      if (error instanceof ConfigClientError) {
        this.onError?.(error);
        throw error;
      }

      if (error instanceof Error) {
        if (error.name === "AbortError") {
          const timeoutError = new ConfigClientError(
            `Request timed out after ${this.timeout}ms`,
            "TIMEOUT_ERROR"
          );
          this.onError?.(timeoutError);
          throw timeoutError;
        }

        const networkError = new ConfigClientError(
          `Network request failed: ${error.message}`,
          "NETWORK_ERROR"
        );
        this.onError?.(networkError);
        throw networkError;
      }

      const unknownError = new ConfigClientError(
        `Request failed: ${String(error)}`,
        "UNKNOWN_ERROR"
      );
      this.onError?.(unknownError);
      throw unknownError;
    }
  }

  private async requestText(
    method: "GET",
    path: string,
    options?: {
      params?: Record<string, string | number | boolean>;
      headers?: Record<string, string>;
    }
  ): Promise<string> {
    const url = new URL(`${this.baseUrl}${path}`);

    if (options?.params) {
      Object.entries(options.params).forEach(([key, value]) => {
        url.searchParams.append(key, String(value));
      });
    }

    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), this.timeout);

    try {
      const response = await this.fetchImpl(url.toString(), {
        method,
        headers: {
          Accept: "text/plain, text/markdown, text/*, */*",
          ...options?.headers,
        },
        signal: controller.signal,
      }).finally(() => clearTimeout(timeoutId));

      if (!response.ok) {
        throw await this.errorFromResponse(response);
      }

      return await response.text();
    } catch (error) {
      if (error instanceof ConfigClientError) {
        this.onError?.(error);
        throw error;
      }

      if (error instanceof Error) {
        if (error.name === "AbortError") {
          const timeoutError = new ConfigClientError(
            `Request timed out after ${this.timeout}ms`,
            "TIMEOUT_ERROR"
          );
          this.onError?.(timeoutError);
          throw timeoutError;
        }

        const networkError = new ConfigClientError(
          `Network request failed: ${error.message}`,
          "NETWORK_ERROR"
        );
        this.onError?.(networkError);
        throw networkError;
      }

      const unknownError = new ConfigClientError(
        `Request failed: ${String(error)}`,
        "UNKNOWN_ERROR"
      );
      this.onError?.(unknownError);
      throw unknownError;
    }
  }

  private async errorFromResponse(response: Response): Promise<ConfigClientError> {
    let message = `HTTP ${response.status}: ${response.statusText}`;
    let code: string | undefined;
    let details: Record<string, unknown> | undefined;

    try {
      const text = await response.text();
      if (text) {
        const json = JSON.parse(text);
        message = json.error?.message || json.message || message;
        code = json.error?.code || json.code;
        details = json.error?.details || json.details;
      }
    } catch {
      // Keep default message if parsing fails
    }

    return new ConfigClientError(message, code, response.status, details);
  }

  // ==================== Datasource Methods ====================

  async listDatasources(): Promise<DatasourceList> {
    return this.request<DatasourceList>("GET", "/api/v1/datasources", {
      schema: DatasourceListSchema,
    });
  }

  async createDatasource(
    datasource: Omit<Datasource, keyof z.infer<typeof CommonFieldsSchema>> & {
      credentials?: {
        password?: string;
        apiKey?: string;
        credentialsJson?: string;
      };
    }
  ): Promise<Datasource> {
    return this.request<Datasource>("POST", "/api/v1/datasources", {
      body: datasource,
      schema: DatasourceSchema,
    });
  }

  async getDatasource(id: string): Promise<Datasource> {
    return this.request<Datasource>("GET", `/api/v1/datasources/${id}`, {
      schema: DatasourceSchema,
    });
  }

  async updateDatasource(
    id: string,
    updates: Partial<Datasource> & {
      revision?: number;
      credentials?: {
        password?: string;
        apiKey?: string;
        credentialsJson?: string;
      };
      clearCredentials?: boolean;
    }
  ): Promise<Datasource> {
    return this.request<Datasource>("PATCH", `/api/v1/datasources/${id}`, {
      body: updates,
      schema: DatasourceSchema,
    });
  }

  async deleteDatasource(id: string): Promise<void> {
    await this.request<void>("DELETE", `/api/v1/datasources/${id}`);
  }

  async testDatasource(id: string): Promise<DatasourceTestResult> {
    return this.request<DatasourceTestResult>("POST", `/api/v1/datasources/${id}/test`, {
      schema: DatasourceTestResultSchema,
    });
  }

  async introspectDatasource(id: string): Promise<{ jobId?: string; status: string }> {
    return this.request<{ jobId?: string; status: string }>(
      "POST",
      `/api/v1/datasources/${id}/introspect`
    );
  }

  async getDatasourceSchema(id: string): Promise<DatasourceSchemaResponse> {
    return this.request<DatasourceSchemaResponse>(
      "GET",
      `/api/v1/datasources/${id}/schema`,
      {
        schema: DatasourceSchemaResponseSchema,
      }
    );
  }

  // ==================== Session Methods ====================

  async listSessions(options: { limit?: number; cursor?: string } = {}): Promise<SessionListResponse> {
    return this.request<SessionListResponse>("GET", "/api/v1/sessions", {
      params: {
        ...(options.limit !== undefined ? { limit: options.limit } : {}),
        ...(options.cursor ? { cursor: options.cursor } : {}),
      },
      schema: SessionListResponseSchema,
    });
  }

  async getSessionConversation(sessionId: string, limit = 80): Promise<SessionConversation> {
    return this.request<SessionConversation>(
      "GET",
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/conversation`,
      {
        params: { limit },
        schema: SessionConversationSchema,
      }
    );
  }

  async listSessionArtifacts(sessionId: string): Promise<SessionArtifactList> {
    return this.request<SessionArtifactList>("GET", "/api/v1/artifacts", {
      params: { sessionId },
      schema: SessionArtifactListSchema,
    });
  }

  async getArtifactPreview(id: string): Promise<unknown> {
    return this.request<unknown>(
      "GET",
      `/api/v1/artifacts/${encodeURIComponent(id)}/preview`
    );
  }

  async getArtifactContent(id: string): Promise<string> {
    return this.requestText(
      "GET",
      `/api/v1/artifacts/${encodeURIComponent(id)}/content`
    );
  }

  async patchSessionTitle(sessionId: string, title: string): Promise<SessionTitle> {
    return this.request<SessionTitle>(
      "PATCH",
      `/api/v1/sessions/${encodeURIComponent(sessionId)}`,
      {
        body: { title },
        schema: SessionTitleSchema,
      }
    );
  }

  async deleteSession(sessionId: string): Promise<{
    sessionId: string;
    deleted: boolean;
    deletedSessionIds: string[];
  }> {
    return this.request(
      "DELETE",
      `/api/v1/sessions/${encodeURIComponent(sessionId)}`,
    );
  }

  // ==================== Model Profile Methods ====================

  async listModels(): Promise<ModelProfileList> {
    return this.request<ModelProfileList>("GET", "/api/v1/model-profiles", {
      schema: ModelProfileListSchema,
    });
  }

  async createModel(
    model: Omit<ModelProfile, keyof z.infer<typeof CommonFieldsSchema>> & {
      credentials?: {
        apiKey?: string;
      };
    }
  ): Promise<ModelProfile> {
    return this.request<ModelProfile>("POST", "/api/v1/model-profiles", {
      body: model,
      schema: ModelProfileSchema,
    });
  }

  async getModel(id: string): Promise<ModelProfile> {
    return this.request<ModelProfile>("GET", `/api/v1/model-profiles/${id}`, {
      schema: ModelProfileSchema,
    });
  }

  async updateModel(
    id: string,
    updates: Partial<ModelProfile> & {
      revision?: number;
      credentials?: {
        apiKey?: string;
      };
      clearCredentials?: boolean;
    }
  ): Promise<ModelProfile> {
    return this.request<ModelProfile>("PATCH", `/api/v1/model-profiles/${id}`, {
      body: updates,
      schema: ModelProfileSchema,
    });
  }

  async deleteModel(id: string): Promise<void> {
    await this.request<void>("DELETE", `/api/v1/model-profiles/${id}`);
  }

  async testModel(id: string): Promise<ModelTestResult> {
    return this.request<ModelTestResult>("POST", `/api/v1/model-profiles/${id}/test`, {
      schema: ModelTestResultSchema,
    });
  }

  // ==================== Skill Methods ====================

  async listSkills(): Promise<SkillList> {
    return this.request<SkillList>("GET", "/api/v1/skills", {
      schema: SkillListSchema,
    });
  }

  async uploadSkill(file: File | Blob, filename?: string): Promise<Skill> {
    const formData = new FormData();
    formData.append("file", file, filename);

    const url = `${this.baseUrl}/api/v1/skills`;
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), this.timeout);

    try {
      const response = await this.fetchImpl(url, {
        method: "POST",
        body: formData,
        signal: controller.signal,
      }).finally(() => clearTimeout(timeoutId));

      if (!response.ok) {
        throw await this.errorFromResponse(response);
      }

      const json = await response.json();
      const resultSchema = ApiResultSchema(SkillSchema);
      const parsed = resultSchema.parse(json);

      if (!parsed.success) {
        throw new ConfigClientError(
          parsed.error.message,
          parsed.error.code,
          response.status,
          parsed.error.details
        );
      }

      return parsed.data;
    } catch (error) {
      if (error instanceof ConfigClientError) {
        this.onError?.(error);
        throw error;
      }
      throw error;
    }
  }

  async getSkill(id: string): Promise<Skill> {
    return this.request<Skill>("GET", `/api/v1/skills/${id}`, {
      schema: SkillSchema,
    });
  }

  async updateSkill(id: string, updates: Partial<Skill>): Promise<Skill> {
    return this.request<Skill>("PATCH", `/api/v1/skills/${id}`, {
      body: updates,
      schema: SkillSchema,
    });
  }

  async deleteSkill(id: string): Promise<void> {
    await this.request<void>("DELETE", `/api/v1/skills/${id}`);
  }

  async downloadSkillPackage(id: string): Promise<Blob> {
    const url = `${this.baseUrl}/api/v1/skills/${id}/package`;
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), this.timeout);

    try {
      const response = await this.fetchImpl(url, {
        method: "GET",
        signal: controller.signal,
      }).finally(() => clearTimeout(timeoutId));

      if (!response.ok) {
        throw await this.errorFromResponse(response);
      }

      return await response.blob();
    } catch (error) {
      if (error instanceof ConfigClientError) {
        this.onError?.(error);
        throw error;
      }
      throw error;
    }
  }

  async validateSkill(id: string): Promise<{ status: string; errors?: string[] }> {
    return this.request<{ status: string; errors?: string[] }>(
      "POST",
      `/api/v1/skills/${id}/validate`
    );
  }

  async replaceSkillPackage(id: string, file: File | Blob, filename?: string): Promise<Skill> {
    const formData = new FormData();
    formData.append("file", file, filename);

    const url = `${this.baseUrl}/api/v1/skills/${id}/replace`;
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), this.timeout);

    try {
      const response = await this.fetchImpl(url, {
        method: "POST",
        body: formData,
        signal: controller.signal,
      }).finally(() => clearTimeout(timeoutId));

      if (!response.ok) {
        throw await this.errorFromResponse(response);
      }

      const json = await response.json();
      const resultSchema = ApiResultSchema(SkillSchema);
      const parsed = resultSchema.parse(json);

      if (!parsed.success) {
        throw new ConfigClientError(
          parsed.error.message,
          parsed.error.code,
          response.status,
          parsed.error.details
        );
      }

      return parsed.data;
    } catch (error) {
      if (error instanceof ConfigClientError) {
        this.onError?.(error);
        throw error;
      }
      throw error;
    }
  }

  // ==================== MCP Server Methods ====================

  async listMcpServers(): Promise<McpServerList> {
    return this.request<McpServerList>("GET", "/api/v1/mcp-servers", {
      schema: McpServerListSchema,
    });
  }

  async createMcpServer(
    server: Omit<McpServer, keyof z.infer<typeof CommonFieldsSchema>> & {
      credentials?: {
        bearerToken?: string;
        customHeader?: { name: string; value: string };
      };
    }
  ): Promise<McpServer> {
    return this.request<McpServer>("POST", "/api/v1/mcp-servers", {
      body: server,
      schema: McpServerSchema,
    });
  }

  async getMcpServer(id: string): Promise<McpServer> {
    return this.request<McpServer>("GET", `/api/v1/mcp-servers/${id}`, {
      schema: McpServerSchema,
    });
  }

  async updateMcpServer(
    id: string,
    updates: Partial<McpServer> & {
      revision?: number;
      credentials?: {
        bearerToken?: string;
        customHeader?: { name: string; value: string };
      };
      clearCredentials?: boolean;
    }
  ): Promise<McpServer> {
    return this.request<McpServer>("PATCH", `/api/v1/mcp-servers/${id}`, {
      body: updates,
      schema: McpServerSchema,
    });
  }

  async deleteMcpServer(id: string): Promise<void> {
    await this.request<void>("DELETE", `/api/v1/mcp-servers/${id}`);
  }

  async testMcpServer(id: string): Promise<McpTestResult> {
    return this.request<McpTestResult>("POST", `/api/v1/mcp-servers/${id}/test`, {
      schema: McpTestResultSchema,
    });
  }

  async getMcpServerTools(id: string): Promise<{ tools: Array<{ name: string; description?: string }> }> {
    return this.request<{ tools: Array<{ name: string; description?: string }> }>(
      "GET",
      `/api/v1/mcp-servers/${id}/tools`
    );
  }

  // ==================== Knowledge Base Methods ====================

  async listKnowledgeBases(): Promise<KnowledgeBaseList> {
    return this.request<KnowledgeBaseList>("GET", "/api/v1/knowledge-bases", {
      schema: KnowledgeBaseListSchema,
    });
  }

  async createKnowledgeBase(
    kb: Omit<KnowledgeBase, keyof z.infer<typeof CommonFieldsSchema>> & {
      credentials?: {
        apiKey?: string;
      };
    }
  ): Promise<KnowledgeBase> {
    return this.request<KnowledgeBase>("POST", "/api/v1/knowledge-bases", {
      body: kb,
      schema: KnowledgeBaseSchema,
    });
  }

  async getKnowledgeBase(id: string): Promise<KnowledgeBase> {
    return this.request<KnowledgeBase>("GET", `/api/v1/knowledge-bases/${id}`, {
      schema: KnowledgeBaseSchema,
    });
  }

  async updateKnowledgeBase(
    id: string,
    updates: Partial<KnowledgeBase> & {
      revision?: number;
      credentials?: {
        apiKey?: string;
      };
      clearCredentials?: boolean;
    }
  ): Promise<KnowledgeBase> {
    return this.request<KnowledgeBase>("PATCH", `/api/v1/knowledge-bases/${id}`, {
      body: updates,
      schema: KnowledgeBaseSchema,
    });
  }

  async deleteKnowledgeBase(id: string): Promise<void> {
    await this.request<void>("DELETE", `/api/v1/knowledge-bases/${id}`);
  }

  async uploadDocument(id: string, file: File | Blob, filename?: string): Promise<{ status: string; fileId?: string }> {
    const formData = new FormData();
    formData.append("file", file, filename);

    const url = `${this.baseUrl}/api/v1/knowledge-bases/${id}/files`;
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), this.timeout);

    try {
      const response = await this.fetchImpl(url, {
        method: "POST",
        body: formData,
        signal: controller.signal,
      }).finally(() => clearTimeout(timeoutId));

      if (!response.ok) {
        throw await this.errorFromResponse(response);
      }

      const json = await response.json();

      if (json.success === false) {
        throw new ConfigClientError(
          json.error?.message || "Upload failed",
          json.error?.code,
          response.status,
          json.error?.details
        );
      }

      return json.data || json;
    } catch (error) {
      if (error instanceof ConfigClientError) {
        this.onError?.(error);
        throw error;
      }
      throw error;
    }
  }

  async reindexKnowledgeBase(id: string): Promise<{ jobId?: string; status: string }> {
    return this.request<{ jobId?: string; status: string }>(
      "POST",
      `/api/v1/knowledge-bases/${id}/reindex`
    );
  }

  async searchKnowledgeBase(
    id: string,
    query: string,
    options?: { topK?: number; scoreThreshold?: number }
  ): Promise<KnowledgeSearchResult> {
    return this.request<KnowledgeSearchResult>(
      "POST",
      `/api/v1/knowledge-bases/${id}/search`,
      {
        body: { query, ...options },
        schema: KnowledgeSearchResultSchema,
      }
    );
  }

  // ==================== Workspace Config Methods ====================

  async getWorkspaceConfig(): Promise<WorkspaceConfig> {
    return this.request<WorkspaceConfig>("GET", "/api/v1/workspace-config", {
      schema: WorkspaceConfigSchema,
    });
  }

  async updateWorkspaceConfig(updates: Partial<WorkspaceConfig>): Promise<WorkspaceConfig> {
    return this.request<WorkspaceConfig>("PATCH", "/api/v1/workspace-config", {
      body: updates,
      schema: WorkspaceConfigSchema,
    });
  }

  // ==================== Run Defaults Methods ====================

  async getRunDefaults(): Promise<RunDefaults> {
    return this.request<RunDefaults>("GET", "/api/v1/run-defaults", {
      schema: RunDefaultsSchema,
    });
  }

  // ==================== Capabilities Methods ====================

  async getCapabilities(): Promise<Capabilities> {
    return this.request<Capabilities>("GET", "/api/v1/capabilities", {
      schema: CapabilitiesSchema,
    });
  }
}
