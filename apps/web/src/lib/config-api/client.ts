import type {
  ApiResult,
  ArtifactExportFormat,
  ArtifactDto,
  ArtifactVersionDto,
  BackendCapabilitiesResponse,
  DatalinkGraphResponseDto,
  DatalinkServersResponseDto,
  DatalinkToolResponseDto,
  DatasourceDto,
  DatasourceSchemaDto,
  DatasourceTablePreviewDto,
  DatasourceTypeDto,
  FileAssetRefDto,
  JobDto,
  KnowledgeBaseDto,
  KnowledgeDocumentDto,
  MeResponseDto,
  McpServerDto,
  ModelProfileDto,
  QueryHistoryItemDto,
  QueryHistoryListResponseDto,
  RunCancelDto,
  RunDefaultsDto,
  SessionBranchDto,
  SessionConversationDto,
  SessionListResponseDto,
  SessionTitleDto,
  SkillDto,
  TraceDagDto,
  WorkspaceConfigDto,
} from "./types";
import { ConfigApiError as ConfigApiErrorClass } from "./types";


export type ConfigApiIdentity = {
  userId: string;
  displayName?: string;
  email?: string;
};

let currentIdentity: ConfigApiIdentity | null = null;

export function setConfigApiIdentity(identity: ConfigApiIdentity | null): void {
  currentIdentity = identity;
}

export function clearConfigApiIdentity(): void {
  currentIdentity = null;
}

export function configApiIdentityHeaders(): Record<string, string> {
  return {};
}

export function getConfigApiBaseUrl(): string {
  const configured = process.env.NEXT_PUBLIC_CONFIG_API_URL;
  if (configured !== undefined) {
    return configured.replace(/\/$/u, "");
  }
  return "";
}

export function getAgentRuntimeUrl(): string {
  return "/api/copilotkit";
}

function csrfHeader(method: string | undefined): Record<string, string> {
  if (!isUnsafeMethod(method)) {
    return {};
  }
  const token = csrfCookie();
  return token ? { "X-CSRF-Token": token } : {};
}

function csrfCookie(): string | undefined {
  if (typeof document === "undefined") {
    return undefined;
  }
  return document.cookie
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith("df_csrf="))
    ?.slice("df_csrf=".length);
}

function isUnsafeMethod(method: string | undefined): boolean {
  return method === "POST" || method === "PATCH" || method === "PUT" || method === "DELETE";
}

async function parseJsonResponse<T>(response: Response): Promise<T> {
  const text = await response.text();
  if (!text) {
    throw new ConfigApiErrorClass("INTERNAL_ERROR", "Empty response body", response.status);
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    throw new ConfigApiErrorClass(
      "INTERNAL_ERROR",
      `Invalid JSON response (${response.status})`,
      response.status,
    );
  }
  return parsed as T;
}

async function requestEnvelope<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const baseUrl = getConfigApiBaseUrl();
  const identityHeaders = configApiIdentityHeaders();
  const response = await fetch(`${baseUrl}${path}`, {
    ...init,
    credentials: "same-origin",
    headers: {
      Accept: "application/json",
      ...identityHeaders,
      ...csrfHeader(init?.method),
      ...(init?.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
      ...init?.headers,
    },
  });

  const envelope = await parseJsonResponse<ApiResult<T>>(response);
  if (!envelope.success) {
    throw new ConfigApiErrorClass(
      envelope.error.code,
      envelope.error.message,
      response.status,
    );
  }
  return envelope.data;
}

async function requestRaw(path: string, init?: RequestInit): Promise<Response> {
  const baseUrl = getConfigApiBaseUrl();
  const response = await fetch(`${baseUrl}${path}`, {
    ...init,
    credentials: "same-origin",
    headers: {
      ...configApiIdentityHeaders(),
      ...csrfHeader(init?.method),
      ...init?.headers,
    },
  });
  if (!response.ok) {
    const text = await response.text();
    throw new ConfigApiErrorClass(
      "INTERNAL_ERROR",
      text || `Request failed (${response.status})`,
      response.status,
    );
  }
  return response;
}

function queryString(params: URLSearchParams): string {
  const query = params.toString();
  return query ? `?${query}` : "";
}

export const configApi = {
  register(body: { displayName?: string; email: string; password: string }): Promise<{
    user: { id: string; email?: string; displayName?: string };
    workspace: { id: string; name?: string };
    verificationToken?: string;
  }> {
    return requestEnvelope("/api/v1/auth/register", {
      method: "POST",
      body: JSON.stringify(body),
    });
  },

  login(body: { email: string; password: string }): Promise<MeResponseDto> {
    return requestEnvelope<MeResponseDto>("/api/v1/auth/login", {
      method: "POST",
      body: JSON.stringify(body),
    });
  },

  verifyEmail(body: { token: string }): Promise<{ user: { id: string; email?: string; displayName?: string } }> {
    return requestEnvelope("/api/v1/auth/verify-email", {
      method: "POST",
      body: JSON.stringify(body),
    });
  },

  logout(): Promise<{ ok: boolean }> {
    return requestEnvelope<{ ok: boolean }>("/api/v1/auth/logout", { method: "POST" });
  },

  logoutAll(): Promise<{ ok: boolean }> {
    return requestEnvelope<{ ok: boolean }>("/api/v1/auth/logout-all", { method: "POST" });
  },

  forgotPassword(body: { email: string }): Promise<{ ok: boolean; resetToken?: string }> {
    return requestEnvelope("/api/v1/auth/password/forgot", {
      method: "POST",
      body: JSON.stringify(body),
    });
  },

  resetPassword(body: { password: string; token: string }): Promise<{ ok: boolean }> {
    return requestEnvelope("/api/v1/auth/password/reset", {
      method: "POST",
      body: JSON.stringify(body),
    });
  },

  changePassword(body: { currentPassword: string; newPassword: string }): Promise<{ ok: boolean }> {
    return requestEnvelope("/api/v1/auth/password/change", {
      method: "POST",
      body: JSON.stringify(body),
    });
  },

  getMe(): Promise<MeResponseDto> {
    return requestEnvelope<MeResponseDto>("/api/v1/me");
  },

  getAuthStatus(): Promise<{ publicBaseUrl: string; registrationEnabled: boolean }> {
    return requestEnvelope<{ publicBaseUrl: string; registrationEnabled: boolean }>("/api/v1/auth/status");
  },

  getCapabilities(): Promise<BackendCapabilitiesResponse> {
    return requestEnvelope<BackendCapabilitiesResponse>("/api/v1/capabilities");
  },

  getWorkspaceConfig(): Promise<WorkspaceConfigDto> {
    return requestEnvelope<WorkspaceConfigDto>("/api/v1/workspace-config");
  },

  getRunDefaults(): Promise<RunDefaultsDto> {
    return requestEnvelope<RunDefaultsDto>("/api/v1/run-defaults");
  },

  async uploadChatFile(
    file: File,
    sessionId?: string | null,
  ): Promise<{ path: string; mimeType: string; size: number }> {
    const form = new FormData();
    form.append("file", file);
    if (sessionId) {
      form.append("sessionId", sessionId);
      form.append("threadId", sessionId);
    }
    const response = await requestRaw("/api/v1/chat/uploads", {
      method: "POST",
      body: form,
    });
    return parseJsonResponse<{ path: string; mimeType: string; size: number }>(
      response,
    );
  },

  async uploadDatasourceFile(
    file: File,
  ): Promise<{ path: string; originalName: string; size: number; mimeType: string }> {
    const form = new FormData();
    form.append("file", file);
    const response = await requestRaw("/api/v1/datasources/uploads", {
      method: "POST",
      body: form,
    });
    return parseJsonResponse<{
      path: string;
      originalName: string;
      size: number;
      mimeType: string;
    }>(response);
  },

  getSessionConversation(sessionId: string, limit?: number): Promise<SessionConversationDto> {
    const params = new URLSearchParams();
    if (limit !== undefined) {
      params.set("limit", String(limit));
    }
    return requestEnvelope<SessionConversationDto>(
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/conversation${queryString(params)}`,
    );
  },

  getSessionTraceDag(sessionId: string, limit?: number): Promise<TraceDagDto> {
    const params = new URLSearchParams();
    if (limit !== undefined) {
      params.set("limit", String(limit));
    }
    return requestEnvelope<TraceDagDto>(
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/trace-dag${queryString(params)}`,
    ).then(normalizeTraceDagDto);
  },

  createSessionBranch(
    sessionId: string,
    input: { checkpointId?: string; runId?: string; title?: string },
  ): Promise<SessionBranchDto> {
    return requestEnvelope<SessionBranchDto>(`/api/v1/sessions/${encodeURIComponent(sessionId)}/branches`, {
      method: "POST",
      body: JSON.stringify(input),
    });
  },

  listSessions(options: { limit?: number; cursor?: string } = {}): Promise<SessionListResponseDto> {
    const params = new URLSearchParams();
    if (options.limit !== undefined) params.set("limit", String(options.limit));
    if (options.cursor) params.set("cursor", options.cursor);
    return requestEnvelope<SessionListResponseDto>(`/api/v1/sessions${queryString(params)}`);
  },

  patchSessionTitle(sessionId: string, title: string): Promise<SessionTitleDto> {
    return requestEnvelope<SessionTitleDto>(`/api/v1/sessions/${encodeURIComponent(sessionId)}`, {
      method: "PATCH",
      body: JSON.stringify({ title }),
    });
  },

  deleteSession(sessionId: string): Promise<{ sessionId: string; deleted: boolean; deletedSessionIds: string[] }> {
    return requestEnvelope<{ sessionId: string; deleted: boolean; deletedSessionIds: string[] }>(
      `/api/v1/sessions/${encodeURIComponent(sessionId)}`,
      { method: "DELETE" },
    );
  },

  listDatasourceTypes(): Promise<DatasourceTypeDto[]> {
    return requestEnvelope<DatasourceTypeDto[]>("/api/v1/datasource-types");
  },

  listDatalinkServers(): Promise<DatalinkServersResponseDto> {
    return requestEnvelope<DatalinkServersResponseDto>("/api/v1/datalink/servers");
  },

  getDatalinkGraph(serverId: string): Promise<DatalinkGraphResponseDto> {
    return requestEnvelope<DatalinkGraphResponseDto>(
      `/api/v1/datalink/${encodeURIComponent(serverId)}/graph`,
    );
  },

  exploreDatalink(
    serverId: string,
    body: { focus?: string; maskCredential?: boolean; maxNodes?: number; query: string },
  ): Promise<DatalinkToolResponseDto> {
    return requestEnvelope<DatalinkToolResponseDto>(
      `/api/v1/datalink/${encodeURIComponent(serverId)}/explore`,
      { method: "POST", body: JSON.stringify(body) },
    );
  },

  addDatalinkTable(
    serverId: string,
    body: { schemaName?: string; source: string; sourceType?: string; table?: string },
  ): Promise<DatalinkToolResponseDto> {
    return requestEnvelope<DatalinkToolResponseDto>(
      `/api/v1/datalink/${encodeURIComponent(serverId)}/tables`,
      { method: "POST", body: JSON.stringify(body) },
    );
  },

  removeDatalinkTable(
    serverId: string,
    body: { cleanupOrphans?: boolean; tableId: string },
  ): Promise<DatalinkToolResponseDto> {
    return requestEnvelope<DatalinkToolResponseDto>(
      `/api/v1/datalink/${encodeURIComponent(serverId)}/tables`,
      { method: "DELETE", body: JSON.stringify(body) },
    );
  },

  rebuildDatalink(serverId: string): Promise<DatalinkToolResponseDto> {
    return requestEnvelope<DatalinkToolResponseDto>(
      `/api/v1/datalink/${encodeURIComponent(serverId)}/rebuild`,
      { method: "POST", body: JSON.stringify({}) },
    );
  },

  createDatasource(body: Record<string, unknown>): Promise<DatasourceDto> {
    return requestEnvelope<DatasourceDto>("/api/v1/datasources", {
      method: "POST",
      body: JSON.stringify(body),
    });
  },

  patchDatasource(id: string, body: Record<string, unknown>): Promise<DatasourceDto> {
    return requestEnvelope<DatasourceDto>(`/api/v1/datasources/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  },

  deleteDatasource(id: string): Promise<{ deleted: boolean; id: string }> {
    return requestEnvelope(`/api/v1/datasources/${encodeURIComponent(id)}`, {
      method: "DELETE",
    });
  },

  testDatasource(id: string): Promise<Record<string, unknown>> {
    return requestEnvelope(`/api/v1/datasources/${encodeURIComponent(id)}/test`, {
      method: "POST",
    });
  },

  introspectDatasource(id: string, idempotencyKey?: string): Promise<JobDto> {
    return requestEnvelope<JobDto>(`/api/v1/datasources/${encodeURIComponent(id)}/introspect`, {
      method: "POST",
      headers: idempotencyKey ? { "Idempotency-Key": idempotencyKey } : undefined,
    });
  },

  getDatasourceSchema(
    id: string,
    options: { q?: string; includeStats?: boolean } = {},
  ): Promise<DatasourceSchemaDto> {
    const params = new URLSearchParams();
    if (options.q) params.set("q", options.q);
    if (options.includeStats) params.set("includeStats", "true");
    return requestEnvelope<DatasourceSchemaDto>(
      `/api/v1/datasources/${encodeURIComponent(id)}/schema${queryString(params)}`,
    );
  },

  getDatasourceTablePreview(
    id: string,
    table: string,
    options: { schema?: string; limit?: number; offset?: number; orderBy?: string } = {},
  ): Promise<DatasourceTablePreviewDto> {
    const params = new URLSearchParams();
    if (options.schema) params.set("schema", options.schema);
    if (options.limit !== undefined) params.set("limit", String(options.limit));
    if (options.offset !== undefined) params.set("offset", String(options.offset));
    if (options.orderBy) params.set("orderBy", options.orderBy);
    return requestEnvelope<DatasourceTablePreviewDto>(
      `/api/v1/datasources/${encodeURIComponent(id)}/tables/${encodeURIComponent(table)}/preview${queryString(params)}`,
    );
  },

  createKnowledgeBase(body: Record<string, unknown>): Promise<KnowledgeBaseDto> {
    return requestEnvelope<KnowledgeBaseDto>("/api/v1/knowledge-bases", {
      method: "POST",
      body: JSON.stringify(body),
    });
  },

  patchKnowledgeBase(id: string, body: Record<string, unknown>): Promise<KnowledgeBaseDto> {
    return requestEnvelope<KnowledgeBaseDto>(
      `/api/v1/knowledge-bases/${encodeURIComponent(id)}`,
      { method: "PATCH", body: JSON.stringify(body) },
    );
  },

  deleteKnowledgeBase(id: string): Promise<{ deleted: boolean; id: string }> {
    return requestEnvelope(`/api/v1/knowledge-bases/${encodeURIComponent(id)}`, {
      method: "DELETE",
    });
  },

  testKnowledgeBase(id: string): Promise<Record<string, unknown>> {
    return requestEnvelope(`/api/v1/knowledge-bases/${encodeURIComponent(id)}/test`, {
      method: "POST",
    });
  },

  uploadKnowledgeFile(
    id: string,
    file: File,
  ): Promise<KnowledgeDocumentDto> {
    const form = new FormData();
    form.append("file", file);
    return requestEnvelope<KnowledgeDocumentDto>(`/api/v1/knowledge-bases/${encodeURIComponent(id)}/files`, {
      method: "POST",
      body: form,
    });
  },

  listKnowledgeFiles(id: string): Promise<{ documents: KnowledgeDocumentDto[] }> {
    return requestEnvelope<{ documents: KnowledgeDocumentDto[] }>(
      `/api/v1/knowledge-bases/${encodeURIComponent(id)}/files`,
    );
  },

  deleteKnowledgeFile(
    id: string,
    documentId: string,
  ): Promise<{ deleted: boolean; id: string }> {
    return requestEnvelope<{ deleted: boolean; id: string }>(
      `/api/v1/knowledge-bases/${encodeURIComponent(id)}/files/${encodeURIComponent(documentId)}`,
      { method: "DELETE" },
    );
  },

  reindexKnowledgeFile(id: string, documentId: string): Promise<KnowledgeDocumentDto> {
    return requestEnvelope<KnowledgeDocumentDto>(
      `/api/v1/knowledge-bases/${encodeURIComponent(id)}/files/${encodeURIComponent(documentId)}/reindex`,
      { method: "POST" },
    );
  },

  reindexKnowledgeBase(id: string, idempotencyKey?: string): Promise<JobDto> {
    return requestEnvelope<JobDto>(
      `/api/v1/knowledge-bases/${encodeURIComponent(id)}/reindex`,
      {
        method: "POST",
        headers: idempotencyKey ? { "Idempotency-Key": idempotencyKey } : undefined,
      },
    );
  },

  createMcpServer(body: Record<string, unknown>): Promise<McpServerDto> {
    return requestEnvelope<McpServerDto>("/api/v1/mcp-servers", {
      method: "POST",
      body: JSON.stringify(body),
    });
  },

  patchMcpServer(id: string, body: Record<string, unknown>): Promise<McpServerDto> {
    return requestEnvelope<McpServerDto>(`/api/v1/mcp-servers/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  },

  deleteMcpServer(id: string): Promise<{ deleted: boolean; id: string }> {
    return requestEnvelope(`/api/v1/mcp-servers/${encodeURIComponent(id)}`, {
      method: "DELETE",
    });
  },

  testMcpServer(id: string): Promise<Record<string, unknown>> {
    return requestEnvelope(`/api/v1/mcp-servers/${encodeURIComponent(id)}/test`, {
      method: "POST",
    });
  },

  getMcpTools(id: string): Promise<Array<Record<string, unknown>>> {
    return requestEnvelope(`/api/v1/mcp-servers/${encodeURIComponent(id)}/tools`);
  },

  createModelProfile(body: Record<string, unknown>): Promise<ModelProfileDto> {
    return requestEnvelope<ModelProfileDto>("/api/v1/model-profiles", {
      method: "POST",
      body: JSON.stringify(body),
    });
  },

  patchModelProfile(id: string, body: Record<string, unknown>): Promise<ModelProfileDto> {
    return requestEnvelope<ModelProfileDto>(
      `/api/v1/model-profiles/${encodeURIComponent(id)}`,
      { method: "PATCH", body: JSON.stringify(body) },
    );
  },

  deleteModelProfile(id: string): Promise<{ deleted: boolean; id: string }> {
    return requestEnvelope(`/api/v1/model-profiles/${encodeURIComponent(id)}`, {
      method: "DELETE",
    });
  },

  testModelProfile(id: string): Promise<Record<string, unknown>> {
    return requestEnvelope(`/api/v1/model-profiles/${encodeURIComponent(id)}/test`, {
      method: "POST",
    });
  },

  createSkill(form: FormData): Promise<SkillDto> {
    return requestEnvelope<SkillDto>("/api/v1/skills", {
      method: "POST",
      body: form,
    });
  },

  patchSkill(id: string, body: Record<string, unknown>): Promise<SkillDto> {
    return requestEnvelope<SkillDto>(`/api/v1/skills/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    });
  },

  deleteSkill(id: string): Promise<{ deleted: boolean; id: string }> {
    return requestEnvelope(`/api/v1/skills/${encodeURIComponent(id)}`, {
      method: "DELETE",
    });
  },

  testSkill(id: string): Promise<Record<string, unknown>> {
    return requestEnvelope(`/api/v1/skills/${encodeURIComponent(id)}/test`, {
      method: "POST",
    });
  },

  validateSkill(id: string): Promise<Record<string, unknown>> {
    return requestEnvelope(`/api/v1/skills/${encodeURIComponent(id)}/validate`, {
      method: "POST",
    });
  },

  replaceSkill(id: string, form: FormData): Promise<SkillDto> {
    return requestEnvelope<SkillDto>(`/api/v1/skills/${encodeURIComponent(id)}/replace`, {
      method: "POST",
      body: form,
    });
  },

  getJob(id: string): Promise<JobDto> {
    return requestEnvelope<JobDto>(`/api/v1/jobs/${encodeURIComponent(id)}`);
  },

  cancelJob(id: string): Promise<JobDto> {
    return requestEnvelope<JobDto>(`/api/v1/jobs/${encodeURIComponent(id)}/cancel`, {
      method: "POST",
    });
  },

  cancelRun(id: string, reason?: string): Promise<RunCancelDto> {
    return requestEnvelope<RunCancelDto>(`/api/v1/runs/${encodeURIComponent(id)}/cancel`, {
      method: "POST",
      body: JSON.stringify(reason ? { reason } : {}),
    });
  },

  getArtifact(id: string): Promise<ArtifactDto> {
    return requestEnvelope<ArtifactDto>(`/api/v1/artifacts/${encodeURIComponent(id)}`);
  },

  getArtifactPreview(id: string): Promise<Record<string, unknown>> {
    return requestEnvelope(`/api/v1/artifacts/${encodeURIComponent(id)}/preview`);
  },

  listSessionArtifacts(sessionId: string): Promise<{ artifacts: ArtifactDto[] }> {
    const params = new URLSearchParams({ sessionId });
    return requestEnvelope<{ artifacts: ArtifactDto[] }>(`/api/v1/artifacts?${params.toString()}`);
  },

  promoteArtifact(id: string): Promise<FileAssetRefDto> {
    return requestEnvelope<FileAssetRefDto>(`/api/v1/artifacts/${encodeURIComponent(id)}/promote`, {
      method: "POST",
    });
  },

  listArtifactVersions(id: string): Promise<{ versions: ArtifactVersionDto[] }> {
    return requestEnvelope<{ versions: ArtifactVersionDto[] }>(
      `/api/v1/artifacts/${encodeURIComponent(id)}/versions`
    );
  },

  async downloadArtifactVersion(
    id: string,
    versionId: string,
  ): Promise<{ blob: Blob; filename: string }> {
    const response = await requestRaw(
      `/api/v1/artifacts/${encodeURIComponent(id)}/versions/${encodeURIComponent(versionId)}/download`
    );
    const disposition = response.headers.get("Content-Disposition") ?? "";
    const match = /filename="([^"]+)"/u.exec(disposition);
    const filename = match?.[1] ?? `artifact-version-${versionId}`;
    const blob = await response.blob();
    return { blob, filename };
  },

  exportArtifact(
    id: string,
    format: ArtifactExportFormat,
    idempotencyKey?: string,
  ): Promise<JobDto> {
    return requestEnvelope<JobDto>(`/api/v1/artifacts/${encodeURIComponent(id)}/export`, {
      method: "POST",
      body: JSON.stringify({
        format,
        ...(idempotencyKey ? { idempotencyKey } : {}),
      }),
    });
  },

  async downloadArtifact(
    id: string,
    format?: ArtifactExportFormat,
  ): Promise<{ blob: Blob; filename: string }> {
    const params = new URLSearchParams();
    if (format) params.set("format", format);
    const response = await requestRaw(
      `/api/v1/artifacts/${encodeURIComponent(id)}/download${queryString(params)}`,
    );
    const disposition = response.headers.get("Content-Disposition") ?? "";
    const match = /filename="([^"]+)"/u.exec(disposition);
    const filename = match?.[1] ?? `artifact-${id}`;
    const blob = await response.blob();
    return { blob, filename };
  },

  listWorkspaceFiles(options: {
    scope?: "session" | "workspace";
    origin?: string[];
    source?: string[];
    sessionId?: string;
  } = {}): Promise<{ files: FileAssetRefDto[] }> {
    const params = new URLSearchParams();
    if (options.scope) params.set("scope", options.scope);
    if (options.origin?.length) params.set("origin", options.origin.join(","));
    if (options.source?.length) params.set("source", options.source.join(","));
    if (options.sessionId) params.set("sessionId", options.sessionId);
    return requestEnvelope<{ files: FileAssetRefDto[] }>(`/api/v1/files${queryString(params)}`);
  },

  async uploadWorkspaceFiles(
    files: File[],
    sessionId?: string | null,
  ): Promise<{ files: FileAssetRefDto[] }> {
    const form = new FormData();
    for (const file of files) {
      form.append("file", file);
    }
    if (sessionId) {
      form.append("sessionId", sessionId);
      form.append("threadId", sessionId);
    }
    return requestEnvelope<{ files: FileAssetRefDto[] }>("/api/v1/files", {
      method: "POST",
      body: form,
    });
  },

  promoteWorkspaceFile(id: string): Promise<FileAssetRefDto> {
    return requestEnvelope<FileAssetRefDto>(`/api/v1/files/${encodeURIComponent(id)}/promote`, {
      method: "POST",
    });
  },

  async downloadWorkspaceFile(id: string): Promise<{ blob: Blob; filename: string }> {
    const response = await requestRaw(`/api/v1/files/${encodeURIComponent(id)}/download`);
    const disposition = response.headers.get("Content-Disposition") ?? "";
    const match = /filename="([^"]+)"/u.exec(disposition);
    const filename = match?.[1] ?? `file-${id}`;
    const blob = await response.blob();
    return { blob, filename };
  },

  deleteWorkspaceFile(id: string): Promise<{ deleted: boolean; id: string }> {
    return requestEnvelope(`/api/v1/files/${encodeURIComponent(id)}`, {
      method: "DELETE",
    });
  },

  listQueryHistory(options: {
    sessionId?: string;
    datasourceId?: string;
    favorite?: boolean;
    limit?: number;
  } = {}): Promise<QueryHistoryListResponseDto> {
    const params = new URLSearchParams();
    if (options.sessionId) params.set("sessionId", options.sessionId);
    if (options.datasourceId) params.set("datasourceId", options.datasourceId);
    if (options.favorite !== undefined) params.set("favorite", String(options.favorite));
    if (options.limit !== undefined) params.set("limit", String(options.limit));
    return requestEnvelope<QueryHistoryListResponseDto>(
      `/api/v1/query-history${queryString(params)}`,
    );
  },

  favoriteQueryHistory(id: string, favorite: boolean): Promise<QueryHistoryItemDto> {
    return requestEnvelope<QueryHistoryItemDto>(
      `/api/v1/query-history/${encodeURIComponent(id)}/${favorite ? "favorite" : "unfavorite"}`,
      { method: "POST" },
    );
  },
};

/** Keep Trace clients compatible with API responses persisted before semantic sections existed. */
export function normalizeTraceDagDto(
  input: Omit<TraceDagDto, "sections"> & { sections?: TraceDagDto["sections"] },
): TraceDagDto {
  return {
    ...input,
    sections: Array.isArray(input.sections) ? input.sections : [],
  };
}

export { ConfigApiError } from "./types";
