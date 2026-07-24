import {
  createErrorResult,
  createSuccessResult,
  type ApiResult,
  type AppErrorCode,
  type EvidenceKind,
  type EvidenceRef
} from "@datafoundry/contracts";
import {
  createModelProviderFromEnv,
  createModelProviderFromProfile,
  probeModelProvider,
  resolveSkillCacheDir,
  resolveSessionWorkspaceDir,
  resolveWorkspaceDir,
  STATIC_AGENT_TOOL_NAMES
} from "@datafoundry/agent-runtime";
import { fileAssetRefDto, type FileAssetService, mimeTypeForFilename, safeFilename } from "@datafoundry/files";
import {
  buildSkillResourcePayload,
  materializeSkillPackages,
  parseSkillPackage,
  selectSkillsForRun
} from "@datafoundry/skills";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { SSEClientTransport } from "@modelcontextprotocol/sdk/client/sse.js";
import { StdioClientTransport } from "@modelcontextprotocol/sdk/client/stdio.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import type { Transport } from "@modelcontextprotocol/sdk/shared/transport.js";
import {
  artifactRecordToSummary,
  type ArtifactRecord,
  type CheckpointRecord,
  type ConfigResourceKind,
  type ConfigResourceRecord,
  type ContextPackageSnapshotRecord,
  type ConversationMessageRecord,
  type ConversationSummaryRecord,
  type DataSourceRecord,
  type FileAssetRefSource,
  type InteractionRecord,
  type JobRecord,
  type MetadataStore,
  type QueryHistoryRecord,
  type RunRecord,
  type RunEventRecord,
  type SessionBranchRecord,
  type SessionRecord,
  type UserRecord
} from "@datafoundry/metadata";
import { randomUUID } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import type { IncomingMessage } from "node:http";
import { basename, join, resolve, sep } from "node:path";
import writeXlsxFile, { type SheetData } from "write-excel-file/node";

import { resolveEffectiveRunConfig } from "./run-input.js";
import {
  llmEnvFingerprint,
  modelProfileConnectivityPayloadChanged,
  preferConnectedResourceId,
  resolveModelProfileSaveStatus
} from "./model-profile-connection-status.js";
import {
  isRevisionConflictError,
  modelProfileTestFailureMessage,
  modelProfileTestSuccessReason
} from "./model-profile-test.js";
import { handleCapabilitiesRequest } from "./routes/capabilities.js";
import type { ConfigApiContext, ConfigApiResponse } from "./routes/types.js";
import {
  connectPolicyMcpClient,
  type PolicyMcpClientConfig
} from "./policy-mcp-middleware.js";
import { sessionTitleDto } from "./session-title.js";
import {
  createSessionBranch,
  latestVisibleConversationSummary,
  listConversationBranchOptions,
  listVisibleConversationMessages,
  resolveSessionLineage,
  type ConversationBranchOption
} from "./session-branching.js";
import { buildSessionTraceDag } from "./trace-dag.js";
import { readMultipartFiles, readMultipartUpload } from "./upload-parser.js";
import { knowledgeDocumentTextFromFile } from "./knowledge-document-text.js";
import { resolveLiveSessionActiveRun } from "./stale-active-runs.js";

const MAX_JSON_BODY_BYTES = 1024 * 1024;
const DEFAULT_WORKSPACE_ID = "default";

/**
 * Map the user-facing `origin` label (R-021) to the internal FileAssetRefSource enum
 * so the files panel can filter by display label without coupling to the enum.
 * Unknown labels map to undefined and are dropped.
 */
const originToSource = (origin: string): string | undefined => {
  switch (origin.toLowerCase()) {
    case "uploaded":
      return "upload";
    case "generated":
      return "artifact";
    case "saved":
      return "workspace";
    default:
      return undefined;
  }
};

export type { ConfigApiContext, ConfigApiResponse } from "./routes/types.js";

const RESOURCE_PATHS: Record<string, ConfigResourceKind> = {
  "knowledge-bases": "knowledge-base",
  "mcp-servers": "mcp-server",
  "model-profiles": "model-profile",
  skills: "skill"
};
const CHAT_UPLOAD_MAX_FILES = 1;
const CHAT_UPLOAD_MAX_FILE_BYTES = 20 * 1024 * 1024;
const DATASOURCE_UPLOAD_MAX_FILES = 1;
const DATASOURCE_UPLOAD_MAX_FILE_BYTES = 100 * 1024 * 1024;
const XLSX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
const CHAT_UPLOAD_TYPES = new Set([
  "application/json",
  "application/pdf",
  XLSX_MIME_TYPE,
  "text/csv",
  "text/plain",
  "text/tab-separated-values"
]);
const CHAT_UPLOAD_EXTENSIONS = new Set([".csv", ".json", ".parquet", ".pdf", ".tsv", ".txt", ".xlsx"]);
const DATASOURCE_UPLOAD_EXTENSIONS = new Set([
  ".accdb",
  ".csv",
  ".db",
  ".duckdb",
  ".mdb",
  ".parquet",
  ".sqlite",
  ".sqlite3",
  ".tsv",
  ".xls",
  ".xlsx"
]);
const DEFAULT_DATALINK_API_TIMEOUT_MS = 30_000;

/** Handle one configuration REST request, or return undefined for a non-config path. */
export const handleConfigApiRequest = async (
  request: IncomingMessage,
  pathname: string,
  context: ConfigApiContext
): Promise<ConfigApiResponse | undefined> => {
  if (!pathname.startsWith("/api/v1/")) {
    return undefined;
  }
  try {
    return await routeConfigRequest(request, pathname, {
      ...context,
      workspaceId: context.workspaceId ?? DEFAULT_WORKSPACE_ID
    });
  } catch (error) {
    return errorResponse(error);
  }
};

const routeConfigRequest = async (
  request: IncomingMessage,
  pathname: string,
  context: Required<ConfigApiContext>
): Promise<ConfigApiResponse> => {
  const segments = pathname.slice("/api/v1/".length).split("/").filter(Boolean);
  const root = segments[0] ?? "";

  if (root === "me") {
    return handleMeRequest(request, context);
  }
  if (root === "dev") {
    return handleDevIdentityRequest(request, segments.slice(1), context);
  }
  if (root === "datasource-types" && request.method === "GET") {
    return ok(await context.dataGateway.supportTypes());
  }
  if (root === "datasources") {
    return handleDatasourceRequest(request, segments.slice(1), context);
  }
  if (root === "datalink" || root === "datagraph") {
    return handleDatalinkRequest(request, segments.slice(1), context);
  }
  if (root === "workspace-config") {
    if (request.method === "GET") {
      return ok(buildWorkspaceConfig(context));
    }
    if (request.method === "PATCH") {
      return handleWorkspaceConfigPatch(request, context);
    }
    return methodNotAllowed();
  }
  if (root === "run-defaults" && request.method === "GET") {
    return ok(buildRunDefaults(context));
  }
  if (root === "capabilities") {
    return handleCapabilitiesRequest(request.method);
  }
  if (root === "chat" && segments[1] === "uploads") {
    if (request.method !== "POST") {
      return methodNotAllowed();
    }
    return handleChatUpload(request, context);
  }
  if (root === "sessions") {
    return handleSessionRequest(request, segments.slice(1), context);
  }
  if (root === "checkpoints") {
    return handleCheckpointRequest(request, segments.slice(1), context);
  }
  if (root === "jobs") {
    return handleJobRequest(request, segments.slice(1), context);
  }
  if (root === "runs") {
    return handleRunRequest(request, segments.slice(1), context);
  }
  if (root === "query-history") {
    return handleQueryHistoryRequest(request, segments.slice(1), context);
  }
  if (root === "artifacts") {
    return handleArtifactRequest(request, segments.slice(1), context);
  }
  if (root === "files") {
    return handleFileRequest(request, segments.slice(1), context);
  }
  if (root === "skills" && segments[1] === "select" && request.method === "POST") {
    return handleSkillSelectionPreview(request, context);
  }
  const kind = RESOURCE_PATHS[root];
  if (kind) {
    return handleGenericResourceRequest(request, segments.slice(1), context, kind);
  }
  return fail(404, "RESOURCE_NOT_FOUND", `Unknown API resource: ${root}`);
};

const handleMeRequest = (
  request: IncomingMessage,
  context: Required<ConfigApiContext>
): ConfigApiResponse => {
  if (request.method !== "GET") {
    return methodNotAllowed();
  }
  const user = context.metadataStore.users.getById({ user_id: context.userId });
  return ok({
    user: devIdentityUserDto(user),
    workspace: defaultWorkspaceDto(context.workspaceId)
  });
};

const handleDevIdentityRequest = async (
  request: IncomingMessage,
  segments: string[],
  context: Required<ConfigApiContext>
): Promise<ConfigApiResponse> => {
  if (!isDevIdentityApiEnabled()) {
    return fail(404, "RESOURCE_NOT_FOUND", "Dev identity API is not enabled.");
  }
  const resource = segments[0];
  if (resource === "identities" && request.method === "GET") {
    return ok({
      users: context.metadataStore.users.list().map(devIdentityUserDto),
      currentUserId: context.userId,
      workspace: defaultWorkspaceDto(context.workspaceId)
    });
  }
  if (resource === "users" && request.method === "POST") {
    const body = await readJsonBody(request);
    const id = sanitizeDevUserId(stringValue(body.id) ?? stringValue(body.userId) ?? "");
    const email = sanitizeOptionalEmail(stringValue(body.email), id);
    const displayName = sanitizeDisplayName(
      stringValue(body.displayName) ?? stringValue(body.display_name) ?? id
    );
    const user = context.metadataStore.users.upsertDevUser({
      id,
      email,
      display_name: displayName,
      dev_token: `dev-token-${id}`
    });
    return ok({ user: devIdentityUserDto(user) }, 201);
  }
  return fail(404, "RESOURCE_NOT_FOUND", `Unknown dev identity resource: ${resource ?? ""}`);
};

const isDevIdentityApiEnabled = (): boolean =>
  process.env.NODE_ENV !== "production" || process.env.DATAFOUNDRY_ENABLE_DEV_IDENTITY_API === "true";

const handleChatUpload = async (
  request: IncomingMessage,
  context: Required<ConfigApiContext>
): Promise<ConfigApiResponse> => {
  if (!isMultipart(request)) {
    throw new Error("CHAT_UPLOAD_MULTIPART_REQUIRED");
  }
  const upload = await readMultipartFiles(request, {
    maxFileBytes: CHAT_UPLOAD_MAX_FILE_BYTES,
    maxFiles: CHAT_UPLOAD_MAX_FILES,
    maxTotalBytes: CHAT_UPLOAD_MAX_FILE_BYTES
  });
  const file = upload.files[0];
  if (!file) {
    throw new Error("UPLOAD_FILE_REQUIRED");
  }
  if (!isSupportedChatUpload(file.filename, file.mimeType)) {
    throw new Error("UNSUPPORTED_FILE_TYPE");
  }
  const sessionId = stringValue(upload.fields.sessionId)
    ?? stringValue(upload.fields.session_id)
    ?? stringValue(upload.fields.threadId)
    ?? stringValue(upload.fields.thread_id)
    ?? stringHeader(request.headers["x-session-id"])
    ?? stringHeader(request.headers["x-thread-id"]);
  if (!sessionId) {
    throw new Error("CHAT_UPLOAD_SESSION_REQUIRED");
  }
  const workspaceRoot = process.env.WORKSPACE_ROOT ?? join(process.env.STORAGE_ROOT_DIR ?? "storage", "workspaces");
  const workspaceDir = resolveSessionWorkspaceDir({
    runContext: {
      user_id: context.userId,
      workspace_id: context.workspaceId,
      session_id: sessionId,
      run_id: "chat-upload",
      user_input: "chat upload",
      chat_mode: "copilotkit",
      model_name: "chat-upload"
    },
    workspaceRoot
  });
  const uploadDir = resolve(workspaceDir, "uploads");
  if (!uploadDir.startsWith(`${workspaceDir}${sep}`)) {
    throw new Error("WORKSPACE_PATH_ESCAPE");
  }
  mkdirSync(uploadDir, { recursive: true });
  const filename = uniqueUploadFilename(uploadDir, file.filename);
  const targetPath = resolve(uploadDir, filename);
  if (!targetPath.startsWith(`${uploadDir}${sep}`)) {
    throw new Error("WORKSPACE_PATH_ESCAPE");
  }
  writeFileSync(targetPath, file.content);
  // R-025: register a session-scoped FileAssetRef (source=upload, session_id set) so the
  // uploaded file is listed by GET /api/v1/files?scope=session&sessionId=...&origin=uploaded.
  // Best-effort: a failure to register must not fail the upload (the file is already on disk).
  let fileId: string | undefined;
  try {
    const resolved = context.fileAssetService.createRef({
      user_id: context.userId,
      workspace_id: context.workspaceId,
      session_id: sessionId,
      run_id: "chat-upload",
      filename,
      content: file.content,
      declared_mime_type: file.mimeType || mimeTypeForFilename(filename),
      source: "upload",
      metadata: { kind: "chat-upload", session_id: sessionId }
    });
    fileId = resolved.ref.id;
  } catch {
    // best-effort; the on-disk file remains usable as a chat attachment
  }
  return {
    body: {
      mimeType: file.mimeType || mimeTypeForFilename(filename),
      path: `uploads/${filename}`,
      size: file.content.length,
      ...(fileId ? { fileId } : {})
    },
    status: 200
  };
};

const handleDatasourceUpload = async (
  request: IncomingMessage,
  context: Required<ConfigApiContext>
): Promise<ConfigApiResponse> => {
  if (!isMultipart(request)) {
    throw new Error("DATASOURCE_UPLOAD_MULTIPART_REQUIRED");
  }
  const upload = await readMultipartFiles(request, {
    maxFileBytes: DATASOURCE_UPLOAD_MAX_FILE_BYTES,
    maxFiles: DATASOURCE_UPLOAD_MAX_FILES,
    maxTotalBytes: DATASOURCE_UPLOAD_MAX_FILE_BYTES
  });
  const file = upload.files[0];
  if (!file) {
    throw new Error("UPLOAD_FILE_REQUIRED");
  }
  if (file.content.length === 0) {
    throw new Error("UPLOAD_FILE_EMPTY");
  }
  if (!isSupportedDatasourceUpload(file.filename)) {
    throw new Error("UNSUPPORTED_FILE_TYPE");
  }
  const workspaceRoot = process.env.WORKSPACE_ROOT ?? join(process.env.STORAGE_ROOT_DIR ?? "storage", "workspaces");
  const workspaceDir = resolveWorkspaceDir({
    runContext: {
      user_id: context.userId,
      workspace_id: context.workspaceId,
      session_id: "datasource-upload",
      run_id: "datasource-upload",
      user_input: "datasource upload",
      chat_mode: "config",
      model_name: "datasource-upload"
    },
    workspaceRoot
  });
  const uploadDir = resolve(workspaceDir, "datasources");
  if (!uploadDir.startsWith(`${workspaceDir}${sep}`)) {
    throw new Error("WORKSPACE_PATH_ESCAPE");
  }
  mkdirSync(uploadDir, { recursive: true });
  const filename = uniqueUploadFilename(uploadDir, file.filename);
  const targetPath = resolve(uploadDir, filename);
  if (!targetPath.startsWith(`${uploadDir}${sep}`)) {
    throw new Error("WORKSPACE_PATH_ESCAPE");
  }
  writeFileSync(targetPath, file.content);
  return {
    body: {
      path: targetPath,
      originalName: basename(file.filename) || filename,
      size: file.content.length,
      mimeType: file.mimeType || mimeTypeForFilename(filename)
    },
    status: 200
  };
};

const handleSessionRequest = async (
  request: IncomingMessage,
  segments: string[],
  context: Required<ConfigApiContext>
): Promise<ConfigApiResponse> => {
  const sessionId = segments[0];
  const action = segments[1];
  if (!sessionId && request.method === "GET") {
    const requestUrl = new URL(request.url ?? "/", "http://127.0.0.1");
    const limit = clampInteger(Number.parseInt(requestUrl.searchParams.get("limit") ?? "", 10), 1, 200, 50);
    const cursor = requestUrl.searchParams.get("cursor");
    const records = context.metadataStore.sessions.list({
      user_id: context.userId,
      limit,
      ...(cursor ? { cursor } : {})
    });
    return ok({
      sessions: records.map((session) =>
        sessionListDto(
          session,
          resolveLiveSessionActiveRun({
            metadataStore: context.metadataStore,
            runCancelRegistry: context.runCancelRegistry,
            userId: context.userId,
            sessionId: session.id
          })
        )
      ),
      ...(records.length === limit ? { nextCursor: encodeSessionCursor(records.at(-1) as SessionRecord) } : {})
    });
  }
  if (!sessionId) {
    return fail(400, "BAD_REQUEST", "Session id is required.");
  }
  if (!action && request.method === "PATCH") {
    const body = await readJsonBody(request);
    const title = stringValue(body.title)?.trim();
    if (!title) {
      throw new Error("SESSION_TITLE_REQUIRED");
    }
    const session = context.metadataStore.sessions.create({
      user_id: context.userId,
      id: sessionId,
      title: title.slice(0, 80),
      title_source: "user"
    });
    return ok(sessionTitleDto(session));
  }
  if (!action && request.method === "DELETE") {
    const result = context.metadataStore.sessions.delete({
      user_id: context.userId,
      session_id: sessionId
    });
    if (!result.deleted) {
      throw new Error(`Session not found: ${sessionId}`);
    }
    return ok({
      sessionId,
      deleted: true,
      deletedSessionIds: result.deletedSessionIds
    });
  }
  if (action === "branches" && request.method === "POST") {
    const body = await readJsonBody(request);
    const runId = stringValue(body.runId ?? body.run_id);
    const checkpointId = stringValue(body.checkpointId ?? body.checkpoint_id);
    if (runId && checkpointId) {
      throw new Error("BRANCH_TARGET_AMBIGUOUS");
    }
    if (!runId && !checkpointId) {
      throw new Error("BRANCH_TARGET_REQUIRED");
    }
    const title = stringValue(body.title);
    const branchTarget = checkpointId ? { checkpointId } : { runId: runId ?? "" };
    const created = createSessionBranch({
      activeSessionId: sessionId,
      metadataStore: context.metadataStore,
      ...branchTarget,
      ...(title ? { title } : {}),
      userId: context.userId
    });
    return ok(sessionBranchCreatedDto(created), 201);
  }
  if (action === "checkpoints") {
    if (request.method !== "GET") {
      return methodNotAllowed();
    }
    const requestUrl = new URL(request.url ?? "/", "http://127.0.0.1");
    const limit = clampInteger(Number.parseInt(requestUrl.searchParams.get("limit") ?? "", 10), 1, 500, 200);
    return ok({
      sessionId,
      checkpoints: context.metadataStore.checkpoints
        .listBySession({ user_id: context.userId, session_id: sessionId, limit })
        .map(contextCheckpointDto)
    });
  }
  if (action === "trace-dag") {
    if (request.method !== "GET") {
      return methodNotAllowed();
    }
    const requestUrl = new URL(request.url ?? "/", "http://127.0.0.1");
    const limit = clampInteger(Number.parseInt(requestUrl.searchParams.get("limit") ?? "", 10), 1, 300, 200);
    return ok(buildSessionTraceDag({
      limit,
      metadataStore: context.metadataStore,
      sessionId,
      userId: context.userId
    }));
  }
  if (action !== "conversation") {
    return methodNotAllowed();
  }
  if (request.method !== "GET") {
    return methodNotAllowed();
  }

  const session = context.metadataStore.sessions.get({ user_id: context.userId, session_id: sessionId });
  const lineage = resolveSessionLineage({
    metadataStore: context.metadataStore,
    sessionId,
    userId: context.userId
  });
  const requestUrl = new URL(request.url ?? "/", "http://127.0.0.1");
  const limit = clampInteger(Number.parseInt(requestUrl.searchParams.get("limit") ?? "", 10), 1, 200, 80);
  const messages = listVisibleConversationMessages({
    lineage,
    limit,
    metadataStore: context.metadataStore,
    userId: context.userId
  });
  const latestSummary = latestVisibleConversationSummary({
    lineage,
    metadataStore: context.metadataStore,
    sessionId,
    userId: context.userId
  });
  const pendingInteractions = context.metadataStore.interactions.listPendingBySession({
    user_id: context.userId,
    session_id: sessionId
  });
  // R-018: checkpoints must cover every run referenced by messages, pending HITL,
  // or summary — not only message run_ids (user-only / interaction-only runs).
  const runIds = [...new Set([
    ...messages.map((message) => message.run_id),
    ...pendingInteractions.map((interaction) => interaction.run_id),
    ...(latestSummary?.source_run_id ? [latestSummary.source_run_id] : [])
  ])];
  const runEventGroups = runIds.map((runId) => ({
    runId,
    events: context.metadataStore.runEvents.listByRun({ user_id: context.userId, run_id: runId })
  }));
  const visiblePositions = new Map(messages.map((message, index) => [message.id, index + 1]));
  const pendingInteractionRunIds = new Set(
    pendingInteractions.map((interaction) => interaction.run_id)
  );
  const checkpoints = runCheckpointDtos({
    context,
    messages,
    pendingInteractionRunIds,
    runEventGroups,
    runIds,
    visiblePositions
  });
  const branchOptions = listConversationBranchOptions({
    lineage,
    metadataStore: context.metadataStore,
    sessionId,
    userId: context.userId
  });
  // R-018: authoritative HITL tool names (ask_user / submit_plan) keyed by tool_call_id,
  // from the interactions table (all statuses). Overrides any mislabeled AG-UI event name.
  const authoritativeToolNames = new Map(
    context.metadataStore.interactions
      .listBySession({ user_id: context.userId, session_id: sessionId })
      .map((interaction) => [interaction.tool_call_id, interaction.tool_name])
  );
  // R-018: tool_call_ids the run is currently suspended on (pending HITL). Authoritative
  // "awaiting user" marker so restore does not infer suspension from a missing result.
  const awaitingInteractionToolCallIds = new Set(
    pendingInteractions.map((interaction) => interaction.tool_call_id)
  );
  const eventToolCalls = runEventGroups.flatMap(({ runId, events }) =>
    toolCallPairDtos(runId, events, authoritativeToolNames, awaitingInteractionToolCallIds)
  );
  const toolCalls = mergePendingInteractionToolCalls(
    eventToolCalls,
    pendingInteractions,
    authoritativeToolNames
  );

  const activeRun = resolveLiveSessionActiveRun({
    metadataStore: context.metadataStore,
    runCancelRegistry: context.runCancelRegistry,
    userId: context.userId,
    sessionId
  });

  return ok({
    sessionId,
    title: session.title ?? "",
    titleSource: session.title_source ?? "fallback",
    updatedAt: session.updated_at,
    messages: messages.map((message, index) => conversationMessageDto(message, index + 1)),
    ...(latestSummary ? { summary: conversationSummaryDto(latestSummary) } : {}),
    runEventRefs: runEventGroups.map(({ runId, events }) => runEventRefDto(runId, events)),
    ...(checkpoints.length > 0 ? { checkpoints } : {}),
    ...(lineage.branch ? { branch: sessionBranchDto(lineage.branch, session) } : {}),
    ...(branchOptions.length > 0 ? { branches: branchOptions.map(conversationBranchOptionDto) } : {}),
    toolCalls,
    pendingInteractions: pendingInteractions.map(pendingInteractionDto),
    restorableCustomEvents: runEventGroups.flatMap(({ runId, events }) =>
      restorableCustomEventDtos(runId, events)
    ),
    ...(activeRun ? { activeRun: sessionActiveRunDto(activeRun) } : {})
  });
};

const handleCheckpointRequest = async (
  request: IncomingMessage,
  segments: string[],
  context: Required<ConfigApiContext>
): Promise<ConfigApiResponse> => {
  const checkpointId = segments[0];
  const action = segments[1];
  if (!checkpointId) {
    return methodNotAllowed();
  }
  if (request.method !== "GET") {
    return methodNotAllowed();
  }
  const checkpoint = context.metadataStore.checkpoints.get({
    user_id: context.userId,
    checkpoint_id: checkpointId
  });
  if (!action) {
    return ok(contextCheckpointDto(checkpoint));
  }
  if (action !== "context-package") {
    return methodNotAllowed();
  }
  const snapshot = context.metadataStore.contextPackageSnapshots.get({
    user_id: context.userId,
    id: checkpoint.context_package_id
  });
  return ok({
    checkpoint: contextCheckpointDto(checkpoint),
    contextPackage: contextPackageSnapshotDto(snapshot)
  });
};

const handleDatasourceRequest = async (
  request: IncomingMessage,
  segments: string[],
  context: Required<ConfigApiContext>
): Promise<ConfigApiResponse> => {
  const id = segments[0];
  const action = segments[1];
  if (id === "uploads") {
    if (request.method !== "POST") {
      return methodNotAllowed();
    }
    return handleDatasourceUpload(request, context);
  }
  if (!id && request.method === "GET") {
    return ok(context.metadataStore.dataSources.list({ user_id: context.userId }).map(dataSourceDto));
  }
  if (!id && request.method === "POST") {
    const body = await readJsonBody(request);
    const resourceId = stringValue(body.id) ?? slugify(stringValue(body.name) ?? `datasource-${randomUUID()}`);
    return ok(await saveDatasource(body, resourceId, context), 201);
  }
  if (!id) {
    return methodNotAllowed();
  }
  if (action === "test" && request.method === "POST") {
    const startedAt = Date.now();
    try {
      const result = await context.dataGateway.testConnect({
        user_id: context.userId,
        workspace_id: context.workspaceId,
        datasource_id: id
      });
      return ok({ ...result, latencyMs: Date.now() - startedAt, status: "connected" });
    } catch (error) {
      context.metadataStore.dataSources.touchTest({ user_id: context.userId, datasource_id: id, status: "failed" });
      throw new Error(`DATASOURCE_TEST_FAILED:${messageOf(error)}`);
    }
  }
  if (action === "introspect" && request.method === "POST") {
    const job = context.metadataStore.configJobs.create({
      workspace_id: context.workspaceId,
      user_id: context.userId,
      type: "datasource-introspect",
      resource_id: id,
      ...(request.headers["idempotency-key"]
        ? { idempotency_key: String(request.headers["idempotency-key"]) }
        : {})
    });
    if (job.status !== "queued") {
      return ok(job, 202);
    }
    context.metadataStore.configJobs.update({
      id: job.id,
      workspace_id: context.workspaceId,
      user_id: context.userId,
      status: "running",
      progress: 10
    });
    try {
      const snapshot = await refreshDatasourceSchemaSnapshot(id, context);
      const completed = context.metadataStore.configJobs.update({
        id: job.id,
        workspace_id: context.workspaceId,
        user_id: context.userId,
        status: "completed",
        progress: 100,
        result: snapshot.payload.schema
      });
      return ok(completed, 202);
    } catch (error) {
      context.metadataStore.configJobs.update({
        id: job.id,
        workspace_id: context.workspaceId,
        user_id: context.userId,
        status: "failed",
        error: { message: messageOf(error) }
      });
      throw error;
    }
  }
  if (action === "schema" && request.method === "GET") {
    const snapshot = await resolveDatasourceSchemaSnapshot(id, context);
    const requestUrl = new URL(request.url ?? "/", "http://127.0.0.1");
    return ok(schemaBrowserDto(snapshot.payload, {
      includeStats: requestUrl.searchParams.get("includeStats") === "true",
      query: requestUrl.searchParams.get("q") ?? undefined
    }));
  }
  if (request.method === "GET") {
    return ok(dataSourceDto(context.metadataStore.dataSources.get({ user_id: context.userId, datasource_id: id })));
  }
  if (request.method === "PATCH") {
    return ok(await saveDatasource(await readJsonBody(request), id, context));
  }
  if (request.method === "DELETE") {
    const current = context.metadataStore.dataSources.get({ user_id: context.userId, datasource_id: id });
    if (current.credential_ref) {
      context.metadataStore.secrets.delete({
        ref: current.credential_ref,
        workspace_id: context.workspaceId,
        user_id: context.userId
      });
    }
    context.metadataStore.dataSources.delete({ user_id: context.userId, datasource_id: id });
    return ok({ deleted: true, id });
  }
  return methodNotAllowed();
};

const handleDatalinkRequest = async (
  request: IncomingMessage,
  segments: string[],
  context: Required<ConfigApiContext>
): Promise<ConfigApiResponse> => {
  const target = segments[0];
  const action = segments[1];
  if (target === "servers" && request.method === "GET") {
    return ok({ servers: listDatalinkServers(context) });
  }
  if (!target) {
    return methodNotAllowed();
  }

  const serverId = decodeURIComponent(target);
  const resource = getDatalinkServer(context, serverId);
  if (action === "graph" && request.method === "GET") {
    const result = await callDatalinkApi(resource, context, "/show");
    return ok({
      graph: normalizeDatalinkGraph(result),
      server: datalinkServerDto(resource)
    });
  }
  if (action === "explore" && request.method === "POST") {
    const body = await readJsonBody(request);
    const query = stringValue(body.query);
    if (!query) {
      throw new Error("DATALINK_QUERY_REQUIRED");
    }
    const maxNodes = numberValue(body.maxNodes) ?? numberValue(body.max_nodes);
    const focus = stringValue(body.focus);
    const result = await callDatalinkApi(resource, context, "/explore", {
      query,
      ...(maxNodes !== undefined ? { max_nodes: Math.floor(maxNodes) } : {}),
      ...(focus ? { focus } : {})
    });
    return ok({ result: result.text, server: datalinkServerDto(resource) });
  }
  if (action === "tables" && request.method === "POST") {
    const body = await readJsonBody(request);
    const source = stringValue(body.source);
    if (!source) {
      throw new Error("DATALINK_SOURCE_REQUIRED");
    }
    const result = await callDatalinkApi(resource, context, "/add-table", {
      source,
      table: stringValue(body.table) ?? null,
      source_type: stringValue(body.sourceType ?? body.source_type) ?? "csv",
      schema_name: stringValue(body.schemaName ?? body.schema_name) ?? null
    });
    return ok({ result: result.text, server: datalinkServerDto(resource) });
  }
  if (action === "tables" && request.method === "DELETE") {
    const body = await readJsonBody(request);
    const tableId = stringValue(body.tableId ?? body.table_id) ?? (segments[2] ? decodeURIComponent(segments[2]) : "");
    if (!tableId) {
      throw new Error("DATALINK_TABLE_ID_REQUIRED");
    }
    const result = await callDatalinkApi(resource, context, "/remove-table", {
      table_id: tableId,
      cleanup_orphans: booleanValue(body.cleanupOrphans ?? body.cleanup_orphans, true)
    });
    return ok({ result: result.text, server: datalinkServerDto(resource) });
  }
  if (action === "rebuild" && request.method === "POST") {
    const body = await readJsonBody(request);
    const result = await callDatalinkApi(resource, context, "/rebuild", {
      ...(stringValue(body.mode) ? { mode: stringValue(body.mode) } : {})
    });
    return ok({ result: result.text, server: datalinkServerDto(resource) });
  }
  return methodNotAllowed();
};

type DatalinkApiCallResult = {
  data: unknown;
  text: string;
};

const listDatalinkServers = (context: Required<ConfigApiContext>): Record<string, unknown>[] =>
  context.metadataStore.configResources.list({
    workspace_id: context.workspaceId,
    user_id: context.userId,
    kind: "mcp-server"
  }).filter(isDatalinkServer).map(datalinkServerDto);

const getDatalinkServer = (
  context: Required<ConfigApiContext>,
  serverId: string
): ConfigResourceRecord => {
  const resource = context.metadataStore.configResources.get({
    id: serverId,
    workspace_id: context.workspaceId,
    user_id: context.userId,
    kind: "mcp-server"
  });
  if (!isDatalinkServer(resource)) {
    throw new Error(`DATALINK_SERVER_NOT_FOUND:${serverId}`);
  }
  return resource;
};

const isDatalinkServer = (resource: ConfigResourceRecord): boolean => {
  const tools = datalinkToolManifest(resource);
  if (tools.some((tool) => isDatalinkManifestToolName(tool.name))) {
    return true;
  }
  const name = resource.name.toLowerCase();
  const id = resource.id.toLowerCase();
  return name.includes("datalink") || name.includes("datagraph") || id.includes("datalink") || id.includes("datagraph");
};

const isDatalinkManifestToolName = (toolName: string): boolean =>
  toolName.startsWith("datalink_") || toolName.startsWith("datagraph_");

const datalinkServerDto = (resource: ConfigResourceRecord): Record<string, unknown> => {
  const tools = datalinkToolManifest(resource);
  return {
    id: resource.id,
    name: datalinkServerDisplayName(resource),
    description: resource.description ?? "",
    healthStatus: resource.status,
    serverUrl: stringValue(resource.payload.serverUrl) ?? stringValue(resource.payload.url) ?? "",
    apiUrl: stringValue(resource.payload.apiUrl) ?? "",
    transport: stringValue(resource.payload.transport) ?? "streamable-http",
    toolCount: tools.length,
    toolNames: tools.map((tool) => tool.name),
    updatedAt: resource.updated_at
  };
};

const datalinkServerDisplayName = (resource: ConfigResourceRecord): string => {
  if (resource.name.toLowerCase() === "datagraph") {
    return "datalink";
  }
  return resource.name;
};

const datalinkToolManifest = (resource: ConfigResourceRecord): Array<{ name: string }> => {
  const manifest = arrayValue(resource.payload.toolManifest);
  if (!manifest) {
    return [];
  }
  return manifest.flatMap((tool) => {
    const name = isRecord(tool) ? stringValue(tool.name) : undefined;
    return name ? [{ name }] : [];
  });
};

const callDatalinkApi = async (
  resource: ConfigResourceRecord,
  context: Required<ConfigApiContext>,
  path: string,
  body?: Record<string, unknown>
): Promise<DatalinkApiCallResult> => {
  const url = datalinkApiEndpoint(resource, path);
  const headers = new Headers(datalinkMcpHeaders(resource, context));
  headers.set("Accept", "application/json");
  if (body !== undefined) {
    headers.set("Content-Type", "application/json");
  }
  const timeoutMs = numberValue(resource.payload.timeoutMs) ?? DEFAULT_DATALINK_API_TIMEOUT_MS;
  let response: Response;
  try {
    response = await fetch(url, {
      method: body === undefined ? "GET" : "POST",
      headers,
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      signal: AbortSignal.timeout(timeoutMs)
    });
  } catch (error) {
    throw new Error(`DATALINK_API_REQUEST_FAILED:${error instanceof Error ? error.message : String(error)}`);
  }
  const text = await response.text();
  if (!response.ok) {
    throw new Error(`DATALINK_API_FAILED:${response.status}:${text}`);
  }
  if (!response.headers.get("content-type")?.includes("application/json")) {
    return { data: text, text };
  }
  try {
    const data = text ? JSON.parse(text) : {};
    return { data, text: datalinkApiResponseText(data) };
  } catch {
    throw new Error("DATALINK_API_INVALID_RESPONSE");
  }
};

const datalinkApiEndpoint = (resource: ConfigResourceRecord, path: string): string => {
  const rawUrl = stringValue(resource.payload.apiUrl);
  if (!rawUrl) {
    throw new Error("DATALINK_API_URL_REQUIRED");
  }
  let baseUrl: URL;
  try {
    baseUrl = new URL(rawUrl);
  } catch {
    throw new Error("DATALINK_API_URL_INVALID");
  }
  if (baseUrl.protocol !== "http:" && baseUrl.protocol !== "https:") {
    throw new Error("DATALINK_API_URL_INVALID");
  }
  const normalizedBase = baseUrl.href.endsWith("/") ? baseUrl.href : `${baseUrl.href}/`;
  return new URL(path.replace(/^\/+/, ""), normalizedBase).toString();
};

const datalinkMcpHeaders = (
  resource: ConfigResourceRecord,
  context: Required<ConfigApiContext>
): Record<string, string> | undefined => {
  const secret = resource.secret_ref
    ? context.metadataStore.secrets.get({
        ref: resource.secret_ref,
        workspace_id: context.workspaceId,
        user_id: context.userId
      })
    : {};
  const configured = recordStringMapValue(resource.payload.headers) ?? recordStringMapValue(secret.headers);
  const token = stringValue(secret.token) ?? stringValue(secret.apiKey);
  if ((stringValue(resource.payload.authType) ?? "none") === "bearer" && token) {
    return { ...configured, Authorization: `Bearer ${token}` };
  }
  return configured;
};

const datalinkApiResponseText = (data: unknown): string => {
  if (typeof data === "string") {
    return data;
  }
  const result = stringValue(recordValue(data)?.result);
  return result ?? JSON.stringify(data);
};

const normalizeDatalinkGraph = (result: DatalinkApiCallResult): Record<string, unknown> => {
  const parsed = recordValue(result.data) ?? {};
  const graph = recordValue(parsed.graph) ?? parsed;
  const nodes = arrayValue(graph.nodes);
  const edges = arrayValue(graph.edges);
  if (!nodes || !edges) {
    throw new Error("DATALINK_GRAPH_INVALID");
  }
  return { nodes, edges };
};

const saveDatasource = async (
  body: Record<string, unknown>,
  id: string,
  context: Required<ConfigApiContext>
): Promise<Record<string, unknown>> => withConfigTransaction(
  context.metadataStore,
  () => saveDatasourceInTransaction(body, id, context)
);

const resolveDatasourceSchemaSnapshot = async (
  datasourceId: string,
  context: Required<ConfigApiContext>
): Promise<ConfigResourceRecord> => {
  const snapshot = context.metadataStore.configResources.find({
    id: datasourceId,
    workspace_id: context.workspaceId,
    user_id: context.userId,
    kind: "datasource-schema"
  });
  if (!snapshot || isDatasourceSchemaExpired(datasourceId, snapshot, context)) {
    return refreshDatasourceSchemaSnapshot(datasourceId, context);
  }
  return snapshot;
};

const refreshDatasourceSchemaSnapshot = async (
  datasourceId: string,
  context: Required<ConfigApiContext>
): Promise<ConfigResourceRecord> => {
  const schema = await context.dataGateway.inspectSchema({
    user_id: context.userId,
    workspace_id: context.workspaceId,
    datasource_id: datasourceId
  });
  return context.metadataStore.configResources.upsert({
    id: datasourceId,
    workspace_id: context.workspaceId,
    user_id: context.userId,
    kind: "datasource-schema",
    name: datasourceId,
    payload: { schema, adapterSchemaVersion: 1, inspectedAt: new Date().toISOString() },
    status: "ready"
  });
};

const schemaBrowserDto = (
  payload: Record<string, unknown>,
  options: { includeStats: boolean; query?: string | undefined }
): Record<string, unknown> => {
  const schema = recordValue(payload.schema) ?? payload;
  const datasourceId = stringValue(schema.datasource_id);
  const query = options.query?.trim().toLowerCase();
  const rawTables = Array.isArray(schema.tables) ? schema.tables : [];
  const tables = rawTables.map((table) => schemaBrowserTableDto(table, options.includeStats))
    .filter((table) => {
      if (!query) {
        return true;
      }
      const tableName = stringValue(table.name)?.toLowerCase() ?? "";
      const columns = Array.isArray(table.columns) ? table.columns : [];
      return tableName.includes(query) || columns.some((column) =>
        isRecord(column) && typeof column.name === "string" && column.name.toLowerCase().includes(query)
      );
    });
  return {
    ...(datasourceId ? { datasourceId, datasource_id: datasourceId } : {}),
    tables,
    inspectedAt: stringValue(payload.inspectedAt),
    adapterSchemaVersion: payload.adapterSchemaVersion ?? 1
  };
};

const schemaBrowserTableDto = (value: unknown, includeStats: boolean): Record<string, unknown> => {
  const table = recordValue(value) ?? {};
  const columns = Array.isArray(table.columns) ? table.columns : [];
  return {
    name: stringValue(table.name) ?? "",
    table: stringValue(table.name) ?? "",
    description: stringValue(table.description) ?? "",
    sampleAvailable: true,
    columns: columns.map(schemaBrowserColumnDto),
    ...(includeStats ? { stats: schemaStatsDto(table) } : {})
  };
};

const schemaBrowserColumnDto = (value: unknown): Record<string, unknown> => {
  const column = recordValue(value) ?? {};
  return {
    name: stringValue(column.name) ?? "",
    type: stringValue(column.type) ?? "unknown",
    nullable: column.nullable === undefined ? undefined : Boolean(column.nullable),
    description: stringValue(column.description) ?? ""
  };
};

const schemaStatsDto = (table: Record<string, unknown>): Record<string, unknown> => ({
  rowCount: numberValue(table.row_count) ?? numberValue(table.rowCount),
  sizeBytes: numberValue(table.size_bytes) ?? numberValue(table.sizeBytes)
});

const isDatasourceSchemaExpired = (
  datasourceId: string,
  snapshot: ConfigResourceRecord,
  context: Required<ConfigApiContext>
): boolean => {
  const refreshIntervalSec = datasourceRefreshIntervalSec(datasourceId, context);
  if (refreshIntervalSec === undefined) {
    return false;
  }
  const inspectedAt = stringValue(snapshot.payload.inspectedAt);
  if (!inspectedAt) {
    return true;
  }
  const inspectedTime = Date.parse(inspectedAt);
  if (!Number.isFinite(inspectedTime)) {
    return true;
  }
  return Date.now() - inspectedTime >= refreshIntervalSec * 1000;
};

const datasourceRefreshIntervalSec = (
  datasourceId: string,
  context: Required<ConfigApiContext>
): number | undefined => {
  const datasource = context.metadataStore.dataSources.get({ user_id: context.userId, datasource_id: datasourceId });
  const config = parseRecord(datasource.config_json);
  const introspection = recordValue(config.introspection);
  const refreshIntervalSec = numberValue(introspection?.refreshIntervalSec);
  return refreshIntervalSec !== undefined && refreshIntervalSec > 0 ? Math.floor(refreshIntervalSec) : undefined;
};

const saveDatasourceInTransaction = (
  body: Record<string, unknown>,
  id: string,
  context: Required<ConfigApiContext>
): Record<string, unknown> => {
  const existing = findDatasource(context.metadataStore, context.userId, id);
  const existingConfig = existing ? parseRecord(existing.config_json) : {};
  const rawConfig = recordValue(body.config) ?? recordValue(body.connection) ?? recordValue(body.settings) ?? {};
  const inputConfig = { ...rawConfig };
  const inlinePassword = stringValue(inputConfig.password) ?? stringValue(body.password);
  delete inputConfig.password;
  const credentials = recordValue(body.credentials) ?? (inlinePassword ? { password: inlinePassword } : undefined);
  let secretRef = existing?.credential_ref;
  if (credentials) {
    secretRef = context.metadataStore.secrets.put({
      workspace_id: context.workspaceId,
      user_id: context.userId,
      owner_kind: "datasource",
      owner_id: id,
      value: credentials,
      ...(secretRef ? { secret_ref: secretRef } : {})
    });
  } else if (body.clearCredentials === true && secretRef) {
    context.metadataStore.secrets.delete({
      ref: secretRef,
      workspace_id: context.workspaceId,
      user_id: context.userId
    });
    secretRef = undefined;
  }
  const policy = recordValue(body.queryPolicy) ?? recordValue(inputConfig.queryPolicy);
  const introspection = recordValue(body.introspection) ?? recordValue(inputConfig.introspection);
  const samplePolicy = recordValue(body.samplePolicy) ?? recordValue(inputConfig.samplePolicy);
  const maskFields = arrayValue(body.maskFields) ?? arrayValue(inputConfig.maskFields);
  const expectedRevision = numberValue(body.revision);
  const type = stringValue(body.type) ?? existing?.type ?? "duckdb";
  const normalizedConfig = normalizeDatasourceConfig(type, inputConfig);
  const description = stringValue(body.description) ?? existing?.description;
  const config = {
    ...existingConfig,
    ...normalizedConfig,
    ...(policy ? { queryPolicy: { ...recordValue(existingConfig.queryPolicy), ...policy } } : {}),
    ...(introspection ? { introspection: { ...recordValue(existingConfig.introspection), ...introspection } } : {}),
    ...(samplePolicy ? { samplePolicy: { ...recordValue(existingConfig.samplePolicy), ...samplePolicy } } : {}),
    ...(maskFields ? { maskFields } : {}),
    defaultEnabled: booleanValue(body.defaultEnabled, booleanValue(existingConfig.defaultEnabled, true)),
    builtin: booleanValue(body.builtin, booleanValue(existingConfig.builtin, false)),
    mode: "readonly"
  };
  const record = context.metadataStore.dataSources.create({
    user_id: context.userId,
    id,
    name: stringValue(body.name) ?? existing?.name ?? id,
    type,
    config,
    ...(secretRef ? { credential_ref: secretRef } : {}),
    ...(description ? { description } : {}),
    status: existing?.status ?? "ready",
    ...(expectedRevision !== undefined ? { expected_revision: expectedRevision } : {})
  });
  return dataSourceDto(record);
};

const handleGenericResourceRequest = async (
  request: IncomingMessage,
  segments: string[],
  context: Required<ConfigApiContext>,
  kind: ConfigResourceKind
): Promise<ConfigApiResponse> => {
  const id = segments[0];
  const action = segments[1];
  if (!id && request.method === "GET") {
    return ok(context.metadataStore.configResources.list({
      workspace_id: context.workspaceId,
      user_id: context.userId,
      kind
    }).map(configResourceDto));
  }
  if (!id && request.method === "POST") {
    const body = kind === "skill" && isMultipart(request)
      ? await skillUploadBody(request, context)
      : await readJsonBody(request);
    const resourceId = stringValue(body.id) ?? slugify(stringValue(body.name) ?? `${kind}-${randomUUID()}`);
    return ok(saveConfigResource(body, resourceId, kind, context), 201);
  }
  if (!id) {
    return methodNotAllowed();
  }
  const targetResource = context.metadataStore.configResources.get({
    id,
    workspace_id: context.workspaceId,
    user_id: context.userId,
    kind
  });
  if (kind === "knowledge-base" && action === "files" && !segments[2] && request.method === "GET") {
    const documents = context.knowledgeService.listDocuments({
      user_id: context.userId,
      collection_id: id
    }).map(knowledgeDocumentDto);
    return ok({ documents });
  }
  if (kind === "knowledge-base" && action === "files" && segments[2] === "import" && request.method === "POST") {
    const body = await readJsonBody(request);
    const fileIds = stringArrayValue(body.fileIds ?? body.file_ids);
    if (fileIds.length === 0) {
      throw new Error("KNOWLEDGE_FILE_IDS_REQUIRED");
    }
    const results = await Promise.all(fileIds.map(async (fileId) => {
      try {
        const resolved = context.fileAssetService.getRef({
          user_id: context.userId,
          workspace_id: context.workspaceId,
          id: fileId
        });
        const file = context.fileAssetService.readRef({
          user_id: context.userId,
          workspace_id: context.workspaceId,
          id: fileId
        });
        const content = await knowledgeDocumentTextFromFile(resolved.ref.filename, file.mimeType, file.body);
        const document = await context.knowledgeService.ingestText({
          user_id: context.userId,
          workspace_id: context.workspaceId,
          collection_id: id,
          filename: resolved.ref.filename,
          content,
          file_asset_ref_id: resolved.ref.id,
          mime_type: file.mimeType
        });
        return { fileId, document: knowledgeDocumentDto(document), status: "ready" };
      } catch (error) {
        return { fileId, error: messageOf(error), status: "failed" };
      }
    }));
    if (results.some((result) => result.status === "ready")) {
      context.metadataStore.configResources.upsert({
        id,
        workspace_id: context.workspaceId,
        user_id: context.userId,
        kind,
        name: targetResource.name,
        ...(targetResource.description ? { description: targetResource.description } : {}),
        payload: targetResource.payload,
        ...(targetResource.secret_ref ? { secret_ref: targetResource.secret_ref } : {}),
        default_enabled: targetResource.default_enabled,
        builtin: targetResource.builtin,
        status: "ready",
        expected_revision: targetResource.revision
      });
    }
    return ok({ results }, 207);
  }
  // Single-document hard delete: clears document + FTS chunks + embeddings.
  if (
    kind === "knowledge-base"
    && action === "files"
    && segments[2]
    && segments[2] !== "import"
    && !segments[3]
    && request.method === "DELETE"
  ) {
    const documentId = decodeURIComponent(segments[2]);
    return ok(context.knowledgeService.deleteDocument({
      user_id: context.userId,
      collection_id: id,
      document_id: documentId
    }));
  }
  // Single-document reindex/retry: success marks status ready.
  if (
    kind === "knowledge-base"
    && action === "files"
    && segments[2]
    && segments[2] !== "import"
    && segments[3] === "reindex"
    && request.method === "POST"
  ) {
    const documentId = decodeURIComponent(segments[2]);
    const document = await context.knowledgeService.reindexDocument({
      user_id: context.userId,
      workspace_id: context.workspaceId,
      collection_id: id,
      document_id: documentId
    });
    return ok(knowledgeDocumentDto(document));
  }
  if (kind === "knowledge-base" && action === "files" && request.method === "POST") {
    const upload = isMultipart(request) ? await readMultipartUpload(request) : undefined;
    const body = upload ? upload.fields : await readJsonBody(request);
    const filename = upload?.file.filename ?? stringValue(body.filename) ?? "document.txt";
    const mimeType = upload?.file.mimeType ?? stringValue(body.mimeType) ?? "";
    // Text + PDF — extension whitelist via knowledgeDocumentTextFromFile.
    // JSON body path must use the same gate whenever a filename is present (incl. default),
    // so `{filename:"doc.docx", content:"..."}` cannot skip validation.
    let content: string | undefined;
    if (upload) {
      content = await knowledgeDocumentTextFromFile(filename, mimeType, upload.file.content);
    } else {
      const raw = stringValue(body.content);
      if (!raw) {
        throw new Error("KNOWLEDGE_DOCUMENT_CONTENT_REQUIRED");
      }
      content = await knowledgeDocumentTextFromFile(filename, mimeType || "text/plain", Buffer.from(raw, "utf8"));
    }
    if (!content) {
      throw new Error("KNOWLEDGE_DOCUMENT_CONTENT_REQUIRED");
    }
    const document = await context.knowledgeService.ingestText({
      user_id: context.userId,
      workspace_id: context.workspaceId,
      collection_id: id,
      filename,
      content,
      ...(mimeType ? { mime_type: mimeType } : {})
    });
    context.metadataStore.configResources.upsert({
      id,
      workspace_id: context.workspaceId,
      user_id: context.userId,
      kind,
      name: targetResource.name,
      ...(targetResource.description ? { description: targetResource.description } : {}),
      payload: targetResource.payload,
      ...(targetResource.secret_ref ? { secret_ref: targetResource.secret_ref } : {}),
      default_enabled: targetResource.default_enabled,
      builtin: targetResource.builtin,
      status: "ready",
      expected_revision: targetResource.revision
    });
    return ok(knowledgeDocumentDto(document), 201);
  }
  if (kind === "knowledge-base" && action === "search" && request.method === "POST") {
    const body = await readJsonBody(request);
    const query = stringValue(body.query);
    if (!query) {
      throw new Error("KNOWLEDGE_QUERY_REQUIRED");
    }
    const topK = numberValue(body.topK);
    return ok(await context.knowledgeService.retrieve({
      user_id: context.userId,
      workspace_id: context.workspaceId,
      collection_id: id,
      query,
      ...(topK !== undefined ? { top_k: topK } : {})
    }));
  }
  if (kind === "knowledge-base" && action === "reindex" && request.method === "POST") {
    const job = context.metadataStore.configJobs.create({
      workspace_id: context.workspaceId,
      user_id: context.userId,
      type: "knowledge-reindex",
      resource_id: id,
      ...(request.headers["idempotency-key"]
        ? { idempotency_key: String(request.headers["idempotency-key"]) }
        : {})
    });
    if (job.status !== "queued") {
      return ok(job, 202);
    }
    context.metadataStore.configJobs.update({
      id: job.id,
      workspace_id: context.workspaceId,
      user_id: context.userId,
      status: "running",
      progress: 10
    });
    try {
      const result = await context.knowledgeService.reindex({
        user_id: context.userId,
        workspace_id: context.workspaceId,
        collection_id: id
      });
      return ok(context.metadataStore.configJobs.update({
        id: job.id,
        workspace_id: context.workspaceId,
        user_id: context.userId,
        status: "completed",
        progress: 100,
        result: {
          documents: context.knowledgeService.listDocuments({ user_id: context.userId, collection_id: id }).length,
          ...result
        }
      }), 202);
    } catch (error) {
      context.metadataStore.configJobs.update({
        id: job.id,
        workspace_id: context.workspaceId,
        user_id: context.userId,
        status: "failed",
        error: { message: messageOf(error) }
      });
      throw error;
    }
  }
  if (kind === "skill" && action === "replace" && request.method === "POST") {
    return ok(saveConfigResource(await skillUploadBody(request, context), id, kind, context));
  }
  if (kind === "skill" && action === "validate" && request.method === "POST") {
    const current = context.metadataStore.configResources.get({
      id,
      workspace_id: context.workspaceId,
      user_id: context.userId,
      kind
    });
    return ok({ id, revision: current.revision, validationStatus: "valid" });
  }
  if (kind === "skill" && action === "package" && request.method === "GET") {
    const current = context.metadataStore.configResources.get({
      id,
      workspace_id: context.workspaceId,
      user_id: context.userId,
      kind
    });
    return ok({
      packageFileRefId: current.payload.packageFileRefId,
      packageFileName: current.payload.packageFileName,
      packageFormat: current.payload.packageFormat
    });
  }
  if (kind === "skill" && action === "download" && request.method === "GET") {
    const current = context.metadataStore.configResources.get({
      id,
      workspace_id: context.workspaceId,
      user_id: context.userId,
      kind
    });
    const packageFileRefId = stringValue(current.payload.packageFileRefId);
    if (!packageFileRefId) {
      throw new Error(`SKILL_PACKAGE_FILE_REF_REQUIRED:${id}`);
    }
    const resolved = context.fileAssetService.getRef({
      user_id: context.userId,
      workspace_id: context.workspaceId,
      id: packageFileRefId
    });
    const file = context.fileAssetService.readRef({
      user_id: context.userId,
      workspace_id: context.workspaceId,
      id: packageFileRefId
    });
    return {
      body: file.body,
      headers: {
        "Content-Disposition": `attachment; filename="${safeDownloadName(resolved.ref.filename, file.mimeType)}"`,
        "Content-Type": file.mimeType
      },
      status: 200
    };
  }
  if (action === "test" && request.method === "POST") {
    const startedAt = Date.now();
    const resource = context.metadataStore.configResources.get({
      id,
      workspace_id: context.workspaceId,
      user_id: context.userId,
      kind
    });
    if (kind === "mcp-server") {
      let tools: Array<Record<string, unknown>>;
      try {
        tools = await listMcpTools(resource, context);
      } catch (error) {
        // Persist failed status best-effort; never let a revision race mask the probe reason.
        try {
          persistResourceTestStatus({
            context,
            resource,
            status: "failed"
          });
        } catch (persistError) {
          if (!isRevisionConflictError(persistError)) {
            throw persistError;
          }
        }
        throw new Error(mcpTestFailureMessage(error));
      }
      const updated = persistResourceTestStatus({
        context,
        resource,
        status: "connected",
        payload: { ...resource.payload, toolManifest: tools }
      });
      return ok({
        id,
        latencyMs: Date.now() - startedAt,
        status: "connected",
        toolCount: tools.length,
        revision: updated.revision
      });
    }
    if (kind === "model-profile") {
      let probe: { model: string; text: string };
      try {
        const provider = resolveProfileProvider(resource, context);
        probe = await probeModelProvider(provider, numberValue(resource.payload.timeoutMs) ?? 30000);
      } catch (error) {
        // Persist failed status best-effort; never let a revision race mask the probe reason.
        try {
          persistResourceTestStatus({
            context,
            resource,
            status: "failed"
          });
        } catch (persistError) {
          if (!isRevisionConflictError(persistError)) {
            throw persistError;
          }
        }
        throw new Error(modelProfileTestFailureMessage(error));
      }
      const payload: Record<string, unknown> = {
        ...resource.payload,
        capabilities: { reasoning: "unknown", toolCall: "untested" }
      };
      if (resource.id === "server-default") {
        payload.llmEnvFingerprint = llmEnvFingerprint(process.env);
      }
      const updated = persistResourceTestStatus({
        context,
        resource,
        status: "connected",
        payload
      });
      const reason = modelProfileTestSuccessReason({
        model: probe.model,
        response: probe.text
      });
      return ok({
        id,
        latencyMs: Date.now() - startedAt,
        model: probe.model,
        response: probe.text,
        reason,
        status: "connected",
        revision: updated.revision
      });
    }
    // kb / skill have no connectivity probe — keep stored status and avoid fake "connected".
    return ok({
      id,
      status: resource.status || "untested",
      tested: false,
      reason: "Connectivity probe is not available for this resource type.",
      revision: resource.revision
    });
  }
  if (action === "tools" && request.method === "GET" && kind === "mcp-server") {
    const resource = context.metadataStore.configResources.get({
      id,
      workspace_id: context.workspaceId,
      user_id: context.userId,
      kind
    });
    return ok(await listMcpTools(resource, context));
  }
  if (request.method === "GET") {
    return ok(configResourceDto(context.metadataStore.configResources.get({
      id,
      workspace_id: context.workspaceId,
      user_id: context.userId,
      kind
    })));
  }
  if (request.method === "PATCH") {
    return ok(saveConfigResource(await readJsonBody(request), id, kind, context));
  }
  if (request.method === "DELETE") {
    const current = context.metadataStore.configResources.get({
      id,
      workspace_id: context.workspaceId,
      user_id: context.userId,
      kind
    });
    if (kind === "knowledge-base") {
      context.metadataStore.db.prepare(
        "DELETE FROM knowledge_embeddings WHERE user_id = ? AND collection_id = ?"
      ).run(context.userId, id);
      context.metadataStore.db.prepare(
        "DELETE FROM knowledge_chunks WHERE user_id = ? AND collection_id = ?"
      ).run(context.userId, id);
      context.metadataStore.db.prepare(
        "DELETE FROM knowledge_documents WHERE user_id = ? AND collection_id = ?"
      ).run(context.userId, id);
    }
    context.metadataStore.configResources.delete({
      id,
      workspace_id: context.workspaceId,
      user_id: context.userId,
      kind
    });
    if (current.secret_ref) {
      context.metadataStore.secrets.delete({
        ref: current.secret_ref,
        workspace_id: context.workspaceId,
        user_id: context.userId
      });
    }
    return ok({ deleted: true, id });
  }
  return methodNotAllowed();
};

const saveConfigResource = (
  body: Record<string, unknown>,
  id: string,
  kind: ConfigResourceKind,
  context: Required<ConfigApiContext>
): Record<string, unknown> => withConfigTransaction(
  context.metadataStore,
  () => saveConfigResourceInTransaction(body, id, kind, context)
);

const saveConfigResourceInTransaction = (
  body: Record<string, unknown>,
  id: string,
  kind: ConfigResourceKind,
  context: Required<ConfigApiContext>
): Record<string, unknown> => {
  const current = context.metadataStore.configResources.find({
    id,
    workspace_id: context.workspaceId,
    user_id: context.userId,
    kind
  });
  if (current?.builtin && kind === "skill") {
    const mutableKeys = new Set(["defaultEnabled", "revision"]);
    const readonlyKeys = Object.keys(body).filter((key) => !mutableKeys.has(key));
    if (readonlyKeys.length > 0) {
      throw new Error(`BUILTIN_RESOURCE_READONLY:${id}`);
    }
  }
  const credentials = resourceCredentials(body, kind);
  let secretRef = current?.secret_ref;
  if (credentials) {
    secretRef = context.metadataStore.secrets.put({
      workspace_id: context.workspaceId,
      user_id: context.userId,
      owner_kind: kind,
      owner_id: id,
      value: credentials,
      ...(secretRef ? { secret_ref: secretRef } : {})
    });
  } else if (body.clearCredentials === true && secretRef) {
    context.metadataStore.secrets.delete({
      ref: secretRef,
      workspace_id: context.workspaceId,
      user_id: context.userId
    });
    secretRef = undefined;
  }
  const reserved = new Set([
    "apiKey", "api_key", "builtin", "clearCredentials", "credentials", "defaultEnabled", "description", "headers",
    "id", "name", "password", "revision", "status", "token"
  ]);
  const payload = Object.fromEntries(Object.entries(body).filter(([key]) => !reserved.has(key)));
  const mergedPayload = { ...(current?.payload ?? {}), ...payload };
  if (kind === "model-profile") {
    validateModelFallback(id, mergedPayload, context);
  }
  const description = stringValue(body.description) ?? current?.description;
  const expectedRevision = numberValue(body.revision);
  const explicitStatus = kind === "model-profile" ? undefined : resourceStatusValue(body, kind);
  const status = kind === "model-profile"
    ? resolveModelProfileSaveStatus({
        explicitStatus,
        currentStatus: current?.status,
        isNew: !current,
        credentialsUpdated: Boolean(credentials),
        connectivityChanged: current
          ? modelProfileConnectivityPayloadChanged(current.payload, mergedPayload)
          : false
      })
    : explicitStatus ?? current?.status ?? defaultResourceStatus(kind);
  const record = context.metadataStore.configResources.upsert({
    id,
    workspace_id: context.workspaceId,
    user_id: context.userId,
    kind,
    name: stringValue(body.name) ?? current?.name ?? id,
    ...(description ? { description } : {}),
    payload: mergedPayload,
    ...(body.clearCredentials === true ? { secret_ref: null } : secretRef ? { secret_ref: secretRef } : {}),
    default_enabled: booleanValue(body.defaultEnabled, current?.default_enabled ?? true),
    builtin: booleanValue(body.builtin, current?.builtin ?? false),
    status,
    ...(expectedRevision !== undefined ? { expected_revision: expectedRevision } : {})
  });
  return configResourceDto(record);
};

const validateModelFallback = (
  profileId: string,
  payload: Record<string, unknown>,
  context: Required<ConfigApiContext>
): void => {
  const visited = new Set([profileId]);
  let fallbackId = stringValue(payload.fallbackProfileId);
  while (fallbackId) {
    if (visited.has(fallbackId)) {
      throw new Error(`MODEL_FALLBACK_CYCLE:${fallbackId}`);
    }
    visited.add(fallbackId);
    const fallback = context.metadataStore.configResources.get({
      id: fallbackId,
      workspace_id: context.workspaceId,
      user_id: context.userId,
      kind: "model-profile"
    });
    fallbackId = stringValue(fallback.payload.fallbackProfileId);
  }
};

const handleJobRequest = (
  request: IncomingMessage,
  segments: string[],
  context: Required<ConfigApiContext>
): ConfigApiResponse => {
  const id = segments[0];
  if (!id) {
    return fail(400, "BAD_REQUEST", "Job id is required.");
  }
  if (request.method === "GET") {
    return ok(artifactExportJobDto(context.metadataStore.configJobs.get({
      id,
      workspace_id: context.workspaceId,
      user_id: context.userId
    })));
  }
  if (request.method === "POST" && segments[1] === "cancel") {
    const current = context.metadataStore.configJobs.get({
      id,
      workspace_id: context.workspaceId,
      user_id: context.userId
    });
    if (current.status !== "queued" && current.status !== "running") {
      return ok(artifactExportJobDto(current));
    }
    return ok(artifactExportJobDto(context.metadataStore.configJobs.update({
      id,
      workspace_id: context.workspaceId,
      user_id: context.userId,
      status: "canceled"
    })));
  }
  return methodNotAllowed();
};

const handleRunRequest = async (
  request: IncomingMessage,
  segments: string[],
  context: Required<ConfigApiContext>
): Promise<ConfigApiResponse> => {
  const id = segments[0];
  if (!id) {
    return fail(400, "BAD_REQUEST", "Run id is required.");
  }
  if (segments[1] !== "cancel" || request.method !== "POST") {
    return methodNotAllowed();
  }
  const body = await readJsonBody(request);
  const reason = stringValue(body.reason);
  const result = context.runCancelRegistry.cancel({
    userId: context.userId,
    runId: id,
    ...(reason ? { reason } : {})
  });
  if (result.canceled) {
    return ok({ canceled: true, runId: result.runId, sessionId: result.sessionId });
  }
  const run = context.metadataStore.runs.find({ user_id: context.userId, run_id: id });
  if (run && (run.status === "queued" || run.status === "running" || run.status === "suspended")) {
    context.metadataStore.runs.updateStatus({
      user_id: context.userId,
      run_id: id,
      status: "canceled"
    });
    return ok({ canceled: true, runId: id, persistedOnly: true });
  }
  return ok({ canceled: false, reason: result.reason, runId: id }, 404);
};

const handleQueryHistoryRequest = async (
  request: IncomingMessage,
  segments: string[],
  context: Required<ConfigApiContext>
): Promise<ConfigApiResponse> => {
  const id = segments[0];
  if (!id && request.method === "GET") {
    const requestUrl = new URL(request.url ?? "/", "http://127.0.0.1");
    const limit = clampInteger(Number.parseInt(requestUrl.searchParams.get("limit") ?? "", 10), 1, 200, 50);
    const favorite = requestUrl.searchParams.get("favorite");
    const records = context.metadataStore.queryHistory.list({
      user_id: context.userId,
      workspace_id: context.workspaceId,
      limit,
      ...(requestUrl.searchParams.get("sessionId")
        ? { session_id: requestUrl.searchParams.get("sessionId") ?? "" }
        : {}),
      ...(requestUrl.searchParams.get("session_id")
        ? { session_id: requestUrl.searchParams.get("session_id") ?? "" }
        : {}),
      ...(requestUrl.searchParams.get("datasourceId")
        ? { datasource_id: requestUrl.searchParams.get("datasourceId") ?? "" }
        : {}),
      ...(requestUrl.searchParams.get("datasource_id")
        ? { datasource_id: requestUrl.searchParams.get("datasource_id") ?? "" }
        : {}),
      ...(favorite === "true" ? { favorite: true } : favorite === "false" ? { favorite: false } : {})
    });
    return ok({ queries: records.map(queryHistoryDto) });
  }
  if (!id) {
    return methodNotAllowed();
  }
  if ((segments[1] === "favorite" || segments[1] === "unfavorite") && request.method === "POST") {
    const record = context.metadataStore.queryHistory.setFavorite({
      user_id: context.userId,
      workspace_id: context.workspaceId,
      id,
      favorite: segments[1] === "favorite"
    });
    return ok(queryHistoryDto(record));
  }
  if (request.method === "PATCH") {
    const body = await readJsonBody(request);
    if (typeof body.favorite !== "boolean") {
      throw new Error("QUERY_HISTORY_FAVORITE_REQUIRED");
    }
    return ok(queryHistoryDto(context.metadataStore.queryHistory.setFavorite({
      user_id: context.userId,
      workspace_id: context.workspaceId,
      id,
      favorite: body.favorite
    })));
  }
  return methodNotAllowed();
};

const handleFileRequest = async (
  request: IncomingMessage,
  segments: string[],
  context: Required<ConfigApiContext>
): Promise<ConfigApiResponse> => {
  const id = segments[0];
  if (!id && request.method === "GET") {
    const requestUrl = new URL(request.url ?? "/", "http://127.0.0.1");
    // Filtering (R-021). Three orthogonal dimensions, all backward compatible:
    //  - `source`/`sources`: comma-separated internal source enum
    //    (artifact|knowledge|run-attachment|skill-package|upload|workspace).
    //  - `origin`: comma-separated display label (uploaded|generated|saved), mapped
    //    1:1 to the internal source enum. Lets the frontend ask for "files the user
    //    can reuse across sessions" without knowing the internal enum.
    //  - `scope`: `session` | `workspace`. `session` ⇒ refs with a session_id;
    //    `workspace` ⇒ refs with session_id IS NULL (cross-session assets).
    //  - `sessionId`: restrict to one session's refs (implies scope=session).
    const sourceParam = requestUrl.searchParams.get("source") ?? requestUrl.searchParams.get("sources");
    const originParam = requestUrl.searchParams.get("origin") ?? requestUrl.searchParams.get("origins");
    const scopeParam = requestUrl.searchParams.get("scope");
    const sessionIdParam = requestUrl.searchParams.get("sessionId") ?? requestUrl.searchParams.get("session_id");
    const sourcesFromOrigin = originParam
      ? originParam.split(",").map((s) => s.trim()).filter(Boolean).map(originToSource).filter(Boolean)
      : [];
    const sources = sourceParam
      ? sourceParam.split(",").map((s) => s.trim()).filter(Boolean)
      : (sourcesFromOrigin.length ? sourcesFromOrigin : undefined);
    // Resolve the session filter: an explicit sessionId wins; otherwise scope=session
    // without a sessionId returns all refs that have any session_id; scope=workspace
    // maps to session_id=null (cross-session assets only).
    let sessionIdFilter: string | null | undefined;
    let hasSessionFilter: boolean | undefined;
    if (sessionIdParam) {
      sessionIdFilter = sessionIdParam;
    } else if (scopeParam === "workspace") {
      sessionIdFilter = null;
    } else if (scopeParam === "session") {
      hasSessionFilter = true;
    }
    const listOne = (source?: string) => context.fileAssetService.listRefs({
      user_id: context.userId,
      workspace_id: context.workspaceId,
      ...(source ? { source: source as FileAssetRefSource } : {}),
      ...(sessionIdFilter !== undefined ? { session_id: sessionIdFilter } : {}),
      ...(hasSessionFilter ? { has_session: true } : {})
    });
    const all = sources
      ? sources.map((source) => listOne(source)).flat()
      : listOne();
    return ok({ files: all.filter(isListableFileAssetRef).map(fileAssetRefDto) });
  }
  if (!id && request.method === "POST") {
    if (!isMultipart(request)) {
      throw new Error("FILE_MULTIPART_REQUIRED");
    }
    const upload = await readMultipartFiles(request, {
      maxFiles: numberFromEnv("FILE_UPLOAD_MAX_FILES", 20),
      maxFileBytes: numberFromEnv("FILE_UPLOAD_MAX_BYTES", 25 * 1024 * 1024),
      maxTotalBytes: numberFromEnv("FILE_UPLOAD_MAX_TOTAL_BYTES", 100 * 1024 * 1024)
    });
    // New files default to the session scope: the upload lands in the per-session
    // directory so only this session sees it until promoted. A sessionId is required
    // (header or multipart field), mirroring the chat-upload contract.
    const sessionId = stringValue(upload.fields.sessionId)
      ?? stringValue(upload.fields.session_id)
      ?? stringValue(upload.fields.threadId)
      ?? stringValue(upload.fields.thread_id)
      ?? stringHeader(request.headers["x-session-id"])
      ?? stringHeader(request.headers["x-thread-id"]);
    if (!sessionId) {
      throw new Error("FILE_UPLOAD_SESSION_REQUIRED");
    }
    const workspaceRoot = process.env.WORKSPACE_ROOT
      ?? join(process.env.STORAGE_ROOT_DIR ?? "storage", "workspaces");
    const sessionDir = resolveSessionWorkspaceDir({
      runContext: {
        user_id: context.userId,
        workspace_id: context.workspaceId,
        session_id: sessionId,
        run_id: "file-upload",
        selected_datasource_id: "",
        enabled_datasource_ids: [],
        user_input: "",
        chat_mode: "config",
        model_name: "file-upload"
      },
      workspaceRoot
    });
    const files = upload.files.map((file) => {
      const resolved = context.fileAssetService.createRef({
        user_id: context.userId,
        workspace_id: context.workspaceId,
        session_id: sessionId,
        run_id: "file-upload",
        filename: file.filename,
        content: file.content,
        declared_mime_type: file.mimeType || mimeTypeForFilename(file.filename),
        source: "upload",
        metadata: { fields: upload.fields }
      });
      // Materialize into the per-session directory (session-scoped). The asset store is
      // the source of truth; this hardlink/copy makes the file visible to the agent's
      // list_files. Best-effort: a failure to materialize must not fail the upload.
      try {
        const targetPath = resolve(sessionDir, safeFilename(resolved.ref.filename));
        if (targetPath.startsWith(`${sessionDir}${sep}`)) {
          context.fileAssetService.materializeRefToPath({
            ref: resolved.ref,
            targetPath,
            linkStrategy: "hardlink"
          });
        }
      } catch {
        // session materialization is best-effort; the ref is already persisted
      }
      return fileAssetRefDto(resolved);
    });
    return ok({ files }, 201);
  }
  if (!id) {
    return methodNotAllowed();
  }
  // POST /api/v1/files/:id/promote — promote a session-scoped file ref into the
  // cross-session workspace root (R-026 / file promote). Accepts the file_id (ref id),
  // reuses promoteFileToWorkspace (idempotent, no byte copy), and materializes the file
  // into the workspace root so other sessions can read it.
  if (segments[1] === "promote" && request.method === "POST") {
    const source = context.fileAssetService.getRef({
      user_id: context.userId,
      workspace_id: context.workspaceId,
      id
    });
    const workspaceRoot = process.env.WORKSPACE_ROOT
      ?? join(process.env.STORAGE_ROOT_DIR ?? "storage", "workspaces");
    const workspaceDir = resolveWorkspaceDir({
      runContext: {
        user_id: context.userId,
        workspace_id: context.workspaceId,
        session_id: "promote",
        run_id: "promote",
        selected_datasource_id: "",
        enabled_datasource_ids: [],
        user_input: "",
        chat_mode: "config",
        model_name: "promote"
      },
      workspaceRoot
    });
    // Materialize the file into the workspace root (best-effort hardlink/copy).
    try {
      const targetPath = resolve(workspaceDir, safeFilename(source.ref.filename));
      if (targetPath.startsWith(`${workspaceDir}${sep}`)) {
        context.fileAssetService.materializeRefToPath({
          ref: source.ref,
          targetPath,
          linkStrategy: "hardlink"
        });
      }
    } catch {
      // best-effort; the ref promotion below is the source of truth
    }
    const resolved = context.fileAssetService.promoteFileToWorkspace({
      user_id: context.userId,
      workspace_id: context.workspaceId,
      file_asset_ref_id: id,
      filename: source.ref.filename,
      ...(source.ref.declared_mime_type ? { declared_mime_type: source.ref.declared_mime_type } : {})
    });
    return ok({
      ...fileAssetRefDto(resolved),
      downloadUrl: `/api/v1/files/${resolved.ref.id}/download`
    });
  }
  if (request.method === "GET" && segments[1] === "download") {
    const resolved = context.fileAssetService.getRef({
      user_id: context.userId,
      workspace_id: context.workspaceId,
      id
    });
    const file = context.fileAssetService.readRef({
      user_id: context.userId,
      workspace_id: context.workspaceId,
      id
    });
    return {
      body: file.body,
      headers: {
        "Content-Disposition": `attachment; filename="${safeDownloadName(resolved.ref.filename, file.mimeType)}"`,
        "Content-Type": file.mimeType
      },
      status: 200
    };
  }
  if (request.method === "GET") {
    return ok(fileAssetRefDto(context.fileAssetService.getRef({
      user_id: context.userId,
      workspace_id: context.workspaceId,
      id
    })));
  }
  if (request.method === "DELETE") {
    const deleted = context.fileAssetService.deleteRef({
      user_id: context.userId,
      workspace_id: context.workspaceId,
      id
    });
    return ok({ deleted: true, id: deleted.id });
  }
  return methodNotAllowed();
};

/**
 * DTO for the session artifact list (R-023). Mirrors the shape the frontend
 * `SessionArtifactsRestore` expects: stable `fileId` (nullable for pure-preview
 * table/chart), `downloadUrl` only when there is a backing file, parsed `preview_json`.
 */
const sessionArtifactDto = (artifact: ArtifactRecord, context: Required<ConfigApiContext>): Record<string, unknown> => {
  const origin = artifactOriginFromMetadata(artifact.metadata_json);
  const storedPreview = parseStoredArtifactPreview(artifact.preview_json);
  const meta = parseMetadataJson(artifact.metadata_json);
  const logicalKey = typeof meta?.logical_key === "string" ? meta.logical_key : undefined;
  const versions = context.metadataStore.artifactVersions.listByArtifact({
    user_id: context.userId,
    artifact_id: artifact.id
  });
  return {
    id: artifact.id,
    type: artifact.type,
    name: artifact.name,
    // R-018: authoritative origin so the frontend links the artifact to its run /
    // tool call / step without heuristics (replaces reconcileLiveRunArtifacts).
    runId: artifact.run_id,
    ...(origin.tool_call_id ? { toolCallId: origin.tool_call_id } : {}),
    ...(origin.step_id ? { stepId: origin.step_id } : {}),
    ...(artifact.file_asset_ref_id ? { fileId: artifact.file_asset_ref_id } : { fileId: null }),
    ...(artifact.file_asset_ref_id
      ? { downloadUrl: `/api/v1/artifacts/${artifact.id}/download` }
      : {}),
    ...(artifact.mime_type ? { mimeType: artifact.mime_type } : {}),
    preview_json: storedPreview === undefined ? null : storedPreview,
    preview_available: storedPreview !== undefined || Boolean(artifact.file_asset_ref_id),
    createdAt: artifact.created_at,
    ...(logicalKey ? { logicalKey } : {}),
    versionCount: versions.length
  };
};

/**
 * Extract R-018 origin handles (`tool_call_id` / `step_id`) that producers persist in
 * an artifact's `metadata_json`. Returns empty handles when absent or malformed.
 */
const artifactOriginFromMetadata = (
  metadataJson: string | undefined
): { step_id?: string; tool_call_id?: string } => {
  const record = parseMetadataJson(metadataJson);
  if (!record) return {};
  const toolCallId = typeof record.tool_call_id === "string" ? record.tool_call_id : undefined;
  const stepId = typeof record.step_id === "string" ? record.step_id : undefined;
  return {
    ...(toolCallId ? { tool_call_id: toolCallId } : {}),
    ...(stepId ? { step_id: stepId } : {})
  };
};

const parseMetadataJson = (metadataJson: string | undefined): Record<string, unknown> | null => {
  if (!metadataJson) return null;
  try {
    const parsed = JSON.parse(metadataJson);
    if (typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>;
    }
  } catch {
    // malformed JSON
  }
  return null;
};

const isListableFileAssetRef = (
  input: ReturnType<FileAssetService["listRefs"]>[number]
): boolean => {
  if (input.ref.source === "skill-package") {
    return false;
  }
  // Older skill uploads were persisted as source="upload"; keep them out of the
  // reusable workspace file library by honoring their metadata marker.
  return parseMetadataJson(input.ref.metadata_json)?.kind !== "skill-package";
};

const handleArtifactRequest = async (
  request: IncomingMessage,
  segments: string[],
  context: Required<ConfigApiContext>
): Promise<ConfigApiResponse> => {
  const id = segments[0];
  // R-023: list artifacts for a session (session-restore). No id, GET, ?sessionId=.
  if (!id) {
    if (request.method !== "GET") {
      return methodNotAllowed();
    }
    const requestUrl = new URL(request.url ?? "/", "http://127.0.0.1");
    const sessionId = requestUrl.searchParams.get("sessionId") ?? requestUrl.searchParams.get("session_id");
    if (!sessionId) {
      throw new Error("ARTIFACT_LIST_SESSION_ID_REQUIRED");
    }
    const records = context.metadataStore.artifacts.listBySession({
      user_id: context.userId,
      session_id: sessionId
    });
    return ok({ artifacts: records.map((a) => sessionArtifactDto(a, context)) });
  }
  // R-022: promote a file-type artifact into a cross-session workspace asset.
  if (segments[1] === "promote") {
    if (request.method !== "POST") {
      return methodNotAllowed();
    }
    const artifact = context.metadataStore.artifacts.get({ user_id: context.userId, artifact_id: id });
    if (!artifact.file_asset_ref_id) {
      // Only file-backed artifacts (table/chart preview-only types) can be promoted.
      throw new Error("ARTIFACT_PROMOTE_NO_FILE");
    }
    const resolved = context.fileAssetService.promoteFileToWorkspace({
      user_id: context.userId,
      workspace_id: context.workspaceId,
      file_asset_ref_id: artifact.file_asset_ref_id,
      filename: artifact.name,
      ...(artifact.mime_type ? { declared_mime_type: artifact.mime_type } : {})
    });
    return ok({
      ...fileAssetRefDto(resolved),
      downloadUrl: `/api/v1/files/${resolved.ref.id}/download`
    });
  }
  if (segments[1] === "export") {
    if (request.method !== "POST") {
      return methodNotAllowed();
    }
    const artifact = context.metadataStore.artifacts.get({ user_id: context.userId, artifact_id: id });
    const body = await readJsonBody(request);
    const format = (stringValue(body.format) ?? "csv").toLowerCase();
    if (format !== "csv" && format !== "xlsx") {
      throw new Error(`ARTIFACT_EXPORT_FORMAT_UNSUPPORTED:${format}`);
    }
    const idempotencyKey = stringValue(body.idempotencyKey);
    const job = context.metadataStore.configJobs.create({
      workspace_id: context.workspaceId,
      user_id: context.userId,
      type: "artifact-export",
      resource_id: artifact.id,
      ...(idempotencyKey ? { idempotency_key: idempotencyKey } : {})
    });
    queueArtifactExportJob({
      artifact,
      context,
      format,
      job
    });
    return ok(artifactExportJobDto(job), 202);
  }
  // Versions list: GET /api/v1/artifacts/:id/versions
  if (segments[1] === "versions" && !segments[2]) {
    if (request.method !== "GET") return methodNotAllowed();
    const artifact = context.metadataStore.artifacts.get({ user_id: context.userId, artifact_id: id });
    const versions = context.metadataStore.artifactVersions.listByArtifact({
      user_id: context.userId,
      artifact_id: artifact.id
    });
    return ok({
      versions: versions.map((v) => ({
        id: v.id,
        version: v.version,
        ...(v.file_asset_ref_id ? { fileId: v.file_asset_ref_id } : {}),
        ...(v.file_asset_ref_id
          ? { downloadUrl: `/api/v1/artifacts/${artifact.id}/versions/${v.id}/download` }
          : {}),
        createdAt: v.created_at
      }))
    });
  }
  // Version download: GET /api/v1/artifacts/:id/versions/:versionId/download
  if (segments[1] === "versions" && segments[2] && segments[3] === "download") {
    if (request.method !== "GET") return methodNotAllowed();
    const versionId = segments[2];
    const version = context.metadataStore.artifactVersions.get({
      user_id: context.userId,
      version_id: versionId
    });
    if (!version.file_asset_ref_id) {
      throw new Error("VERSION_UNAVAILABLE");
    }
    const artifact = context.metadataStore.artifacts.get({ user_id: context.userId, artifact_id: id });
    const file = context.fileAssetService.readRef({
      user_id: context.userId,
      workspace_id: context.workspaceId,
      id: version.file_asset_ref_id
    });
    const filename = safeDownloadName(artifact.name, file.mimeType, artifact.type, artifact.mime_type);
    return {
      body: file.body,
      headers: {
        "Content-Disposition": `attachment; filename="${filename}"`,
        "Content-Type": file.mimeType || "application/octet-stream"
      },
      status: 200
    };
  }
  if (request.method !== "GET") {
    return methodNotAllowed();
  }
  const artifact = context.metadataStore.artifacts.get({ user_id: context.userId, artifact_id: id });
  if (segments[1] === "preview") {
    const storedPreview = parseStoredArtifactPreview(artifact.preview_json);
    // Session-file ingest stores a metadata stub (path/mime/type) without body text.
    // Treat that as missing so file-backed markdown/html/text still synthesize content.
    if (isRenderableArtifactPreview(storedPreview, artifact.type)) {
      return ok(storedPreview);
    }
    const synthesized = synthesizeArtifactPreviewFromFile(artifact, context);
    if (synthesized) {
      return ok(synthesized);
    }
    return ok(storedPreview ?? null);
  }
  if (segments[1] === "content" || segments[1] === "download") {
    const requestUrl = new URL(request.url ?? "/", "http://127.0.0.1");
    const format = requestUrl.searchParams.get("format")?.toLowerCase();
    if (format && format !== "csv" && format !== "xlsx") {
      throw new Error(`ARTIFACT_EXPORT_FORMAT_UNSUPPORTED:${format}`);
    }
    const file = artifact.file_asset_ref_id
      ? context.fileAssetService.readRef({
          user_id: context.userId,
          workspace_id: context.workspaceId,
          id: artifact.file_asset_ref_id
        })
      : artifactFileContent(artifact.storage_path);
    const preview = artifact.preview_json ? JSON.parse(artifact.preview_json) as unknown : null;
    const content = format === "xlsx"
      ? await serializeArtifactXlsx(artifact.name, file, preview)
      : format === "csv"
        ? serializeArtifactCsv(artifact.name, file, preview)
        : file ?? serializeArtifactPreview(preview, segments[1] === "download");
    const filename = format === "xlsx"
      ? safeDownloadNameWithExtension(artifact.name, "xlsx")
      : format === "csv"
        ? safeDownloadNameWithExtension(artifact.name, "csv")
        : safeDownloadName(artifact.name, content.mimeType, artifact.type, artifact.mime_type);
    const responseMimeType = resolveArtifactContentType(artifact, content.mimeType);
    return {
      body: content.body,
      headers: {
        "Content-Disposition": `${segments[1] === "download" ? "attachment" : "inline"}; filename="${filename}"`,
        "Content-Type": responseMimeType
      },
      status: 200
    };
  }
  return ok({
    ...artifactRecordToSummary(artifact),
    mimeType: artifact.mime_type,
    metadata: artifact.metadata_json ? JSON.parse(artifact.metadata_json) as unknown : undefined,
    createdAt: artifact.created_at
  });
};

const conversationMessageDto = (
  message: ConversationMessageRecord,
  visiblePosition = message.position
): Record<string, unknown> => ({
  id: message.id,
  runId: message.run_id,
  role: message.role,
  source: message.source,
  ...(message.message_id ? { messageId: message.message_id } : {}),
  contentText: message.content_text,
  ...conversationMessageEvidenceRefsDto(message),
  ...conversationMessageContentPartsDto(message),
  position: visiblePosition,
  createdAt: message.created_at
});

const conversationMessageContentPartsDto = (
  message: ConversationMessageRecord
): Record<string, unknown> => {
  const content = parseRecord(message.content_json);
  const parts = contentPartsFromUnknown(content.parts);
  return parts.length > 0 ? { contentParts: parts } : {};
};

const contentPartsFromUnknown = (
  value: unknown
): Array<{ type: "reasoning" | "text"; text: string }> => {
  if (!Array.isArray(value)) {
    return [];
  }
  const parts: Array<{ type: "reasoning" | "text"; text: string }> = [];
  for (const item of value) {
    if (!item || typeof item !== "object" || Array.isArray(item)) {
      continue;
    }
    const record = item as Record<string, unknown>;
    const type = record.type;
    const text = typeof record.text === "string" ? record.text : "";
    if ((type === "reasoning" || type === "text") && text.trim()) {
      parts.push({ type, text });
    }
  }
  return parts;
};

const conversationMessageEvidenceRefsDto = (message: ConversationMessageRecord): Record<string, unknown> => {
  const content = parseRecord(message.content_json);
  const refs = evidenceRefsFromUnknown(content.evidenceRefs ?? content.evidence_refs);
  return refs.length > 0 ? { evidenceRefs: refs } : {};
};

const truncateUserInputPreview = (value: string, maxLength = 120): string => {
  const normalized = value.replace(/\s+/g, " ").trim();
  if (normalized.length <= maxLength) {
    return normalized;
  }
  return `${normalized.slice(0, Math.max(0, maxLength - 1))}…`;
};

const sessionActiveRunDto = (run: RunRecord): Record<string, unknown> => ({
  sessionId: run.session_id,
  activeRunId: run.id,
  status: run.status,
  startedAt: run.started_at,
  userInputPreview: truncateUserInputPreview(run.user_input)
});

const sessionListDto = (
  session: SessionRecord,
  activeRun?: RunRecord | null
): Record<string, unknown> => ({
  id: session.id,
  threadId: session.id,
  title: session.title ?? "",
  titleSource: session.title_source ?? "fallback",
  createdAt: session.created_at,
  updatedAt: session.updated_at,
  lastMessageAt: session.last_message_at ?? session.updated_at,
  ...(activeRun ? { activeRun: sessionActiveRunDto(activeRun) } : {})
});

const sessionBranchCreatedDto = (
  input: { branch: SessionBranchRecord; session: SessionRecord }
): Record<string, unknown> => ({
  ...sessionBranchDto(input.branch, input.session),
  session: sessionListDto(
    input.session,
    // Branch create does not need active-run lookup; omit to keep response light.
    null
  )
});

const sessionBranchDto = (
  branch: SessionBranchRecord,
  session?: SessionRecord
): Record<string, unknown> => ({
  id: branch.id,
  sessionId: branch.child_session_id,
  threadId: branch.child_session_id,
  parentSessionId: branch.parent_session_id,
  rootSessionId: branch.root_session_id,
  forkRunId: branch.fork_run_id,
  ...(branch.fork_checkpoint_id ? { forkCheckpointId: branch.fork_checkpoint_id } : {}),
  forkMessageEndPosition: branch.fork_message_end_position,
  createdAt: branch.created_at,
  ...(session?.title ? { title: session.title } : {})
});

const conversationBranchOptionDto = (branch: ConversationBranchOption): Record<string, unknown> => ({
  sessionId: branch.sessionId,
  threadId: branch.sessionId,
  parentSessionId: branch.parentSessionId,
  rootSessionId: branch.rootSessionId,
  forkRunId: branch.forkRunId,
  ...(branch.forkCheckpointId ? { forkCheckpointId: branch.forkCheckpointId } : {}),
  forkMessageEndPosition: branch.forkMessageEndPosition,
  isOriginal: branch.isOriginal,
  createdAt: branch.createdAt,
  ...(branch.title ? { title: branch.title } : {})
});

const queryHistoryDto = (record: QueryHistoryRecord): Record<string, unknown> => ({
  id: record.id,
  sessionId: record.session_id,
  runId: record.run_id,
  datasourceId: record.datasource_id,
  sql: record.sql_text,
  rowCount: record.row_count,
  elapsedMs: record.elapsed_ms,
  favorite: record.favorite,
  createdAt: record.created_at,
  updatedAt: record.updated_at
});

const artifactExportJobDto = (job: JobRecord): Record<string, unknown> => ({
  id: job.id,
  type: job.type,
  artifactId: job.resource_id,
  status: job.status,
  progress: job.progress,
  ...(job.result !== undefined ? { result: job.result } : {}),
  ...(job.error !== undefined ? { error: job.error } : {}),
  createdAt: job.created_at,
  ...(job.started_at ? { startedAt: job.started_at } : {}),
  ...(job.finished_at ? { finishedAt: job.finished_at } : {})
});

const encodeSessionCursor = (session: SessionRecord): string => Buffer.from(JSON.stringify({
  id: session.id,
  sort_at: session.last_message_at ?? session.updated_at
}), "utf8").toString("base64url");

const conversationSummaryDto = (summary: ConversationSummaryRecord): Record<string, unknown> => ({
  id: summary.id,
  ...(summary.source_run_id ? { sourceRunId: summary.source_run_id } : {}),
  fromPosition: summary.from_position,
  toPosition: summary.to_position,
  summaryText: summary.summary_text,
  createdAt: summary.created_at
});

const runEventRefDto = (runId: string, events: RunEventRecord[]): Record<string, unknown> => ({
  runId,
  eventCount: events.length,
  ...(events[0] ? { firstSeq: events[0].seq } : {}),
  ...(events.at(-1) ? { lastSeq: events.at(-1)?.seq } : {})
});

const runCheckpointDtos = (input: {
  context: Required<ConfigApiContext>;
  messages: ConversationMessageRecord[];
  pendingInteractionRunIds?: Set<string>;
  runEventGroups: Array<{ events: RunEventRecord[]; runId: string }>;
  runIds: string[];
  visiblePositions?: Map<string, number>;
}): Record<string, unknown>[] => {
  const eventsByRun = new Map(input.runEventGroups.map((group) => [group.runId, group.events]));
  return input.runIds.flatMap((runId) => {
    const events = eventsByRun.get(runId) ?? [];
    const artifactIds = input.context.metadataStore.artifacts
      .listByRun({ user_id: input.context.userId, run_id: runId })
      .map((artifact) => artifact.id);
    const run = input.context.metadataStore.runs.find({
      user_id: input.context.userId,
      run_id: runId
    });
    if (!run) {
      // Legacy / event-only runs without a `runs` row: still emit an authoritative
      // checkpoint so the frontend never has to guess status (R-018). Status is derived
      // from the persisted terminal event rather than a frontend "completed" fallback.
      if (events.length === 0) {
        // HITL-only legacy: pending interaction exists but neither runs row nor events.
        if (input.pendingInteractionRunIds?.has(runId)) {
          return [{
            runId,
            status: "suspended"
          }];
        }
        return [];
      }
      return [
        eventOnlyRunCheckpointDto({
          artifactIds,
          events,
          messages: input.messages.filter((message) => message.run_id === runId),
          runId,
          ...(input.visiblePositions ? { visiblePositions: input.visiblePositions } : {})
        })
      ];
    }
    return [
      runCheckpointDto({
        artifactIds,
        events,
        messages: input.messages.filter((message) => message.run_id === runId),
        run,
        ...(input.visiblePositions ? { visiblePositions: input.visiblePositions } : {})
      })
    ];
  });
};

/** Map a terminal AG-UI run status to its canonical terminal event name, if any. */
const terminalEventForStatus = (status: string): string | undefined => {
  if (status === "completed") {
    return "RUN_FINISHED";
  }
  if (status === "failed" || status === "canceled") {
    return "RUN_ERROR";
  }
  return undefined;
};

/** Derive an authoritative run status from persisted events when no `runs` row exists. */
const statusFromTerminalEvent = (events: RunEventRecord[]): string => {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const record = events[index];
    if (!record) {
      continue;
    }
    const event = parseRecord(record.payload_json);
    const type = stringValue(event.type);
    if (type === "RUN_FINISHED") {
      return "completed";
    }
    if (type === "RUN_ERROR") {
      return "failed";
    }
  }
  return "running";
};

const eventOnlyRunCheckpointDto = (input: {
  artifactIds: string[];
  events: RunEventRecord[];
  messages: ConversationMessageRecord[];
  runId: string;
  visiblePositions?: Map<string, number>;
}): Record<string, unknown> => {
  const positions = input.messages.map((message) => input.visiblePositions?.get(message.id) ?? message.position);
  const firstEvent = input.events[0];
  const lastEvent = input.events.at(-1);
  const status = statusFromTerminalEvent(input.events);
  const terminalEvent = terminalEventForStatus(status);
  return {
    runId: input.runId,
    status,
    ...(positions.length > 0
      ? {
          messageStartPosition: Math.min(...positions),
          messageEndPosition: Math.max(...positions)
        }
      : {}),
    ...(firstEvent ? { firstEventSeq: firstEvent.seq } : {}),
    ...(lastEvent ? { lastEventSeq: lastEvent.seq } : {}),
    ...(terminalEvent ? { terminalEvent } : {}),
    ...(input.artifactIds.length > 0 ? { artifactIds: input.artifactIds } : {})
  };
};

const runCheckpointDto = (input: {
  artifactIds?: string[];
  events: RunEventRecord[];
  messages: ConversationMessageRecord[];
  run: RunRecord;
  visiblePositions?: Map<string, number>;
}): Record<string, unknown> => {
  const positions = input.messages.map((message) => input.visiblePositions?.get(message.id) ?? message.position);
  const firstEvent = input.events[0];
  const lastEvent = input.events.at(-1);
  const terminalEvent = terminalEventForStatus(input.run.status);
  const artifactIds = input.artifactIds ?? [];
  return {
    runId: input.run.id,
    status: input.run.status,
    ...(positions.length > 0
      ? {
          messageStartPosition: Math.min(...positions),
          messageEndPosition: Math.max(...positions)
        }
      : {}),
    ...(firstEvent ? { firstEventSeq: firstEvent.seq } : {}),
    ...(lastEvent ? { lastEventSeq: lastEvent.seq } : {}),
    startedAt: input.run.started_at,
    ...(input.run.finished_at ? { finishedAt: input.run.finished_at } : {}),
    ...(input.run.error_message ? { errorMessage: input.run.error_message } : {}),
    ...(terminalEvent ? { terminalEvent } : {}),
    ...(artifactIds.length > 0 ? { artifactIds } : {})
  };
};

const contextCheckpointDto = (checkpoint: CheckpointRecord): Record<string, unknown> => ({
  id: checkpoint.id,
  sessionId: checkpoint.session_id,
  runId: checkpoint.run_id,
  branchId: checkpoint.branch_id,
  eventSeq: checkpoint.event_seq,
  contextPackageId: checkpoint.context_package_id,
  contextPackageRevision: checkpoint.context_package_revision,
  kind: checkpoint.kind,
  status: checkpoint.status,
  label: checkpoint.label,
  ...(checkpoint.context_plan_id ? { contextPlanId: checkpoint.context_plan_id } : {}),
  ...(checkpoint.parent_checkpoint_id ? { parentCheckpointId: checkpoint.parent_checkpoint_id } : {}),
  ...(checkpoint.step_number !== undefined ? { stepNumber: checkpoint.step_number } : {}),
  ...(checkpoint.step_id ? { stepId: checkpoint.step_id } : {}),
  ...(checkpoint.tool_call_id ? { toolCallId: checkpoint.tool_call_id } : {}),
  ...(checkpoint.message_position !== undefined ? { messagePosition: checkpoint.message_position } : {}),
  createdAt: checkpoint.created_at
});

const contextPackageSnapshotDto = (snapshot: ContextPackageSnapshotRecord): Record<string, unknown> => ({
  id: snapshot.id,
  sessionId: snapshot.session_id,
  runId: snapshot.run_id,
  packageId: snapshot.package_id,
  revision: snapshot.revision,
  payload: parseRecord(snapshot.payload_json),
  ...(snapshot.plan_json ? { plan: parseRecord(snapshot.plan_json) } : {}),
  createdAt: snapshot.created_at
});

const RESTORABLE_CUSTOM_EVENT_NAMES = new Set([
  "token_usage",
  "token_usage.correlation",
  "workspace.metadata",
  "sandbox.output",
  "goal.updated",
  "sql_audit",
  // Run-scoped configuration/context events the frontend RunConfigurationPanel restores
  // on reload. These are emitted+persisted during the live run; without them the panel
  // hangs on "Waiting for run.config.resolved…" after a session is reopened.
  "run.config.resolved",
  "skill.selection",
  "context.compiled",
  "context.prompt-verified"
]);

const pendingInteractionDto = (interaction: InteractionRecord): Record<string, unknown> => {
  const payload = parseJsonValue(interaction.payload_json);
  const interruptEvent = interaction.interrupt_event_json
    ? parseJsonValue(interaction.interrupt_event_json)
    : undefined;
  const resumeSchema =
    interruptEvent && typeof interruptEvent === "object" && interruptEvent !== null
      ? (interruptEvent as Record<string, unknown>).resumeSchema
      : undefined;
  return {
    interactionId: interaction.id,
    runId: interaction.run_id,
    toolCallId: interaction.tool_call_id,
    toolName: interaction.tool_name,
    ...(interruptEvent !== undefined ? { interruptEvent } : {}),
    ...(payload !== undefined ? { payload } : {}),
    ...(resumeSchema !== undefined ? { resumeSchema } : {})
  };
};

const restorableCustomEventDtos = (
  runId: string,
  events: RunEventRecord[]
): Array<Record<string, unknown>> =>
  events.flatMap((eventRecord) => {
    const event = parseRecord(eventRecord.payload_json);
    if (stringValue(event.type) !== "CUSTOM") {
      return [];
    }
    const name = stringValue(event.name);
    if (!name || !RESTORABLE_CUSTOM_EVENT_NAMES.has(name)) {
      return [];
    }
    const value = event.value ?? event.payload ?? event.content;
    if (value === undefined) {
      return [];
    }
    return [{
      runId,
      seq: eventRecord.seq,
      name,
      value
    }];
  });

const parseJsonValue = (value: string): unknown => {
  try {
    return JSON.parse(value) as unknown;
  } catch {
    return undefined;
  }
};

const toolCallPairDtos = (
  runId: string,
  events: RunEventRecord[],
  authoritativeToolNames?: Map<string, string>,
  awaitingInteractionToolCallIds?: Set<string>
): Array<Record<string, unknown>> => {
  const calls = new Map<string, {
    args?: unknown;
    callEventSeq?: number;
    endEventSeq?: number;
    parentMessageId?: string;
    result?: unknown;
    resultEventSeq?: number;
    resultMessageId?: string;
    resultPreview?: string;
    status: "completed" | "failed" | "pending";
    toolCallId: string;
    toolName?: string;
  }>();

  events.forEach((eventRecord) => {
    const event = parseRecord(eventRecord.payload_json);
    const type = stringValue(event.type);
    const toolCallId = stringValue(event.toolCallId);
    if (!type || !toolCallId) {
      return;
    }
    const existing = calls.get(toolCallId) ?? { status: "pending" as const, toolCallId };
    const toolName = stringValue(event.toolCallName) ?? existing.toolName;

    if (type === "TOOL_CALL_START" || type === "TOOL_CALL_END") {
      // AG-UI tool-call events are passthrough; capture args if the middleware populated
      // `args` / `input` / `argsText` (R-027 conversation-restore wants stable args).
      const args = existing.args ?? toolCallArgs(event);
      const parentMessageId =
        type === "TOOL_CALL_START"
          ? stringValue(event.parentMessageId) ?? existing.parentMessageId
          : existing.parentMessageId;
      calls.set(toolCallId, {
        ...existing,
        ...(toolName ? { toolName } : {}),
        ...(args !== undefined ? { args } : {}),
        ...(type === "TOOL_CALL_START" ? { callEventSeq: eventRecord.seq } : {}),
        ...(type === "TOOL_CALL_END" ? { endEventSeq: eventRecord.seq } : {}),
        ...(parentMessageId ? { parentMessageId } : {})
      });
      return;
    }

    if (type === "TOOL_CALL_RESULT") {
      const resultMessageId = stringValue(event.messageId);
      const resultPreview = previewToolResult(event.content);
      calls.set(toolCallId, {
        ...existing,
        ...(toolName ? { toolName } : {}),
        resultEventSeq: eventRecord.seq,
        ...(resultMessageId ? { resultMessageId } : {}),
        ...(resultPreview ? { resultPreview } : {}),
        // R-027: full result (may be large; frontend truncates as needed).
        result: event.content,
        status: isToolResultError(event.content) ? "failed" : "completed"
      });
    }
  });

  return [...calls.values()].map((call) => {
    // R-018: HITL tool names (ask_user / submit_plan) from the interactions table are
    // authoritative and override any mislabeled name carried on the AG-UI events.
    const authoritativeName = authoritativeToolNames?.get(call.toolCallId);
    const resolvedToolName = authoritativeName ?? call.toolName;
    // R-018: authoritative "run is suspended waiting on this HITL tool" flag, from the
    // interactions table — distinguishes "awaiting user" from "still running" without the
    // frontend having to guess from a missing result.
    const awaitingInteraction = awaitingInteractionToolCallIds?.has(call.toolCallId) ?? false;
    return {
    runId,
    // R-027: stable id/name/args/result fields plus the legacy toolCallId/toolName aliases.
    id: call.toolCallId,
    toolCallId: call.toolCallId,
    status: call.status,
    ...(awaitingInteraction ? { awaitingInteraction: true } : {}),
    ...(resolvedToolName ? { name: resolvedToolName, toolName: resolvedToolName } : {}),
    ...(call.args !== undefined ? { args: call.args } : {}),
    ...(call.result !== undefined ? { result: call.result } : {}),
    ...(call.callEventSeq !== undefined ? { callEventSeq: call.callEventSeq } : {}),
    ...(call.endEventSeq !== undefined ? { endEventSeq: call.endEventSeq } : {}),
    ...(call.resultEventSeq !== undefined ? { resultEventSeq: call.resultEventSeq } : {}),
    ...(call.parentMessageId ? { parentMessageId: call.parentMessageId } : {}),
    ...(call.resultMessageId ? { resultMessageId: call.resultMessageId } : {}),
    ...(call.resultPreview ? { resultPreview: call.resultPreview } : {})
    };
  });
};

/**
 * Read-path defense for HITL: when an interactions row exists but TOOL_CALL_START was
 * never persisted (legacy suspend), synthesize a pending tool call DTO so restore does
 * not depend on frontend bootstrap alone.
 */
const mergePendingInteractionToolCalls = (
  eventToolCalls: Array<Record<string, unknown>>,
  pendingInteractions: InteractionRecord[],
  authoritativeToolNames: Map<string, string>
): Array<Record<string, unknown>> => {
  if (pendingInteractions.length === 0) {
    return eventToolCalls;
  }
  const knownIds = new Set(
    eventToolCalls
      .map((call) => stringValue(call.toolCallId) ?? stringValue(call.id))
      .filter((id): id is string => Boolean(id))
  );
  const synthesized = pendingInteractions.flatMap((interaction) => {
    if (knownIds.has(interaction.tool_call_id)) {
      return [];
    }
    const toolName =
      authoritativeToolNames.get(interaction.tool_call_id) ?? interaction.tool_name;
    const args = parseJsonValue(interaction.payload_json);
    return [{
      runId: interaction.run_id,
      id: interaction.tool_call_id,
      toolCallId: interaction.tool_call_id,
      status: "pending",
      awaitingInteraction: true,
      name: toolName,
      toolName,
      ...(args !== undefined ? { args } : {})
    }];
  });
  return [...eventToolCalls, ...synthesized];
};

/**
 * Extract tool-call args from a passthrough AG-UI tool-call event (R-027). The schema is
 * passthrough, so args may appear as a structured `args`/`input` object or as `argsText`.
 * Returns undefined when none is present.
 */
const toolCallArgs = (event: Record<string, unknown>): unknown => {
  if (event.args !== undefined) {
    return event.args;
  }
  if (event.input !== undefined) {
    return event.input;
  }
  if (typeof event.argsText === "string" && event.argsText.length > 0) {
    return event.argsText;
  }
  return undefined;
};

const previewToolResult = (value: unknown): string | undefined => {
  if (value === undefined) {
    return undefined;
  }
  const text = typeof value === "string" ? value : JSON.stringify(value);
  return text.length > 1000
    ? `${text.slice(0, 1000)}\n[tool result preview truncated: original_chars=${text.length}]`
    : text;
};

const isToolResultError = (value: unknown): boolean => {
  const parsed = typeof value === "string" ? tryParseRecord(value) : recordValue(value);
  return Boolean(parsed && (parsed.isError === true || typeof parsed.error === "string"));
};

const queueArtifactExportJob = (input: {
  artifact: ArtifactRecord;
  context: Required<ConfigApiContext>;
  format: "csv" | "xlsx";
  job: JobRecord;
}): void => {
  void Promise.resolve().then(async () => {
    const { artifact, context, format, job } = input;
    try {
      if (isJobCanceled(context, job.id)) {
        return;
      }
      context.metadataStore.configJobs.update({
        id: job.id,
        workspace_id: context.workspaceId,
        user_id: context.userId,
        status: "running",
        progress: 10
      });
      const file = artifact.file_asset_ref_id
        ? context.fileAssetService.readRef({
            user_id: context.userId,
            workspace_id: context.workspaceId,
            id: artifact.file_asset_ref_id
          })
        : artifactFileContent(artifact.storage_path);
      const preview = artifact.preview_json ? JSON.parse(artifact.preview_json) as unknown : null;
      const content = format === "xlsx"
        ? await serializeArtifactXlsx(artifact.name, file, preview)
        : serializeArtifactCsv(artifact.name, file, preview);
      const filename = format === "xlsx"
        ? safeDownloadNameWithExtension(artifact.name, "xlsx")
        : safeDownloadNameWithExtension(artifact.name, "csv");
      if (isJobCanceled(context, job.id)) {
        return;
      }
      context.metadataStore.configJobs.update({
        id: job.id,
        workspace_id: context.workspaceId,
        user_id: context.userId,
        status: "running",
        progress: 80
      });
      const resolved = context.fileAssetService.createRef({
        user_id: context.userId,
        workspace_id: context.workspaceId,
        filename,
        content: content.body,
        declared_mime_type: content.mimeType,
        source: "artifact",
        ...(artifact.session_id ? { session_id: artifact.session_id } : {}),
        ...(artifact.run_id ? { run_id: artifact.run_id } : {}),
        metadata: {
          artifact_id: artifact.id,
          export_format: format,
          job_id: job.id
        }
      });
      if (isJobCanceled(context, job.id)) {
        return;
      }
      context.metadataStore.configJobs.update({
        id: job.id,
        workspace_id: context.workspaceId,
        user_id: context.userId,
        status: "completed",
        progress: 100,
        result: {
          artifactId: artifact.id,
          downloadUrl: `/api/v1/files/${resolved.ref.id}/download`,
          fileId: resolved.ref.id,
          filename: resolved.ref.filename,
          format,
          mimeType: content.mimeType,
          sizeBytes: resolved.asset.size_bytes
        }
      });
    } catch (error) {
      if (isJobCanceled(input.context, input.job.id)) {
        return;
      }
      input.context.metadataStore.configJobs.update({
        id: input.job.id,
        workspace_id: input.context.workspaceId,
        user_id: input.context.userId,
        status: "failed",
        progress: 100,
        error: {
          message: error instanceof Error ? error.message : "ARTIFACT_EXPORT_FAILED"
        }
      });
    }
  });
};

const isJobCanceled = (context: Required<ConfigApiContext>, jobId: string): boolean =>
  context.metadataStore.configJobs.get({
    id: jobId,
    workspace_id: context.workspaceId,
    user_id: context.userId
  }).status === "canceled";

const artifactFileContent = (storagePath: string | undefined): { body: Buffer; mimeType: string } | undefined => {
  if (!storagePath) {
    return undefined;
  }
  const root = resolve(process.env.ARTIFACT_STORAGE_ROOT ?? "storage/artifacts");
  const path = resolve(storagePath);
  if (path !== root && !path.startsWith(`${root}${sep}`)) {
    throw new Error("ARTIFACT_STORAGE_PATH_INVALID");
  }
  return { body: readFileSync(path), mimeType: mimeTypeForPath(path) };
};

/** When preview_json is absent but the artifact is file-backed, synthesize a text preview. */
const synthesizeArtifactPreviewFromFile = (
  artifact: ArtifactRecord,
  context: Required<ConfigApiContext>
): Record<string, unknown> | null => {
  if (!artifact.file_asset_ref_id) {
    return null;
  }
  const file = context.fileAssetService.readRef({
    user_id: context.userId,
    workspace_id: context.workspaceId,
    id: artifact.file_asset_ref_id
  });
  const mime = (file.mimeType || artifact.mime_type || "").toLowerCase();
  const isTextLike =
    mime.startsWith("text/") ||
    mime.includes("markdown") ||
    mime.includes("json") ||
    mime.includes("csv") ||
    mime.includes("html") ||
    /\.(md|txt|csv|tsv|json|html|htm)$/i.test(artifact.name);
  if (!isTextLike) {
    return {
      type: artifact.type,
      path: artifact.name,
      size: file.body.length,
      tool: "session_output"
    };
  }
  const maxChars = 200_000;
  const content = file.body.toString("utf8").slice(0, maxChars);
  if (artifact.type === "markdown" || artifact.type === "html" || artifact.type === "file") {
    return {
      type: artifact.type,
      content,
      path: artifact.name,
      size: file.body.length,
      tool: "session_output"
    };
  }
  // table/chart without stored preview_json: still expose raw text so clients can fall back.
  return {
    type: artifact.type,
    content,
    path: artifact.name,
    size: file.body.length,
    tool: "session_output"
  };
};

/**
 * Returns a real preview payload when stored JSON is present and non-null.
 * Distinguishes missing / JSON `null` from usable data.
 */
const parseStoredArtifactPreview = (previewJson: string | undefined): unknown | undefined => {
  if (!previewJson || !previewJson.trim() || previewJson.trim() === "null") {
    return undefined;
  }
  try {
    const parsed = JSON.parse(previewJson) as unknown;
    return parsed === null ? undefined : parsed;
  } catch {
    return undefined;
  }
};

/**
 * True when stored preview already has enough payload for the UI to render.
 * Session-output ingest writes a path/mime stub without `content` / table rows;
 * those stubs must fall through to file synthesis.
 */
const isRenderableArtifactPreview = (preview: unknown, artifactType: string): boolean => {
  if (preview === undefined || preview === null) {
    return false;
  }
  if (typeof preview === "string") {
    return preview.trim().length > 0;
  }
  if (typeof preview !== "object" || Array.isArray(preview)) {
    return false;
  }
  const record = preview as Record<string, unknown>;
  if (typeof record.content === "string" && record.content.length > 0) {
    return true;
  }
  if (typeof record.body === "string" && record.body.length > 0) {
    return true;
  }
  if (Array.isArray(record.columns) && Array.isArray(record.rows)) {
    return true;
  }
  if (Array.isArray(record.points) || Array.isArray(record.series)) {
    return true;
  }
  // Non-text file stubs (image/binary) are still useful as metadata-only previews.
  if (artifactType === "image" || artifactType === "file") {
    return typeof record.path === "string" || typeof record.file_id === "string";
  }
  return false;
};

const serializeArtifactPreview = (
  preview: unknown,
  preferCsv: boolean
): { body: Buffer; mimeType: string } => {
  const csv = preferCsv ? previewCsv(preview) : undefined;
  if (csv !== undefined) {
    return { body: Buffer.from(csv, "utf8"), mimeType: "text/csv; charset=utf-8" };
  }
  return {
    body: Buffer.from(JSON.stringify(preview, null, 2), "utf8"),
    mimeType: "application/json; charset=utf-8"
  };
};

const serializeArtifactCsv = (
  artifactName: string,
  file: { body: Buffer; mimeType: string } | undefined,
  preview: unknown
): { body: Buffer; mimeType: string } => {
  if (file && (file.mimeType.startsWith("text/csv") || artifactName.toLowerCase().endsWith(".csv"))) {
    return { body: file.body, mimeType: "text/csv; charset=utf-8" };
  }
  const csv = previewCsv(preview);
  if (csv !== undefined) {
    return { body: Buffer.from(csv, "utf8"), mimeType: "text/csv; charset=utf-8" };
  }
  const sheetData = file ? sheetDataFromFileContent(artifactName, file) : sheetDataFromPreview(preview);
  const rows = sheetData.map((row) => row.map((cell) => {
    if (cell === null || cell === undefined) {
      return "";
    }
    if (typeof cell === "object" && "value" in cell) {
      return csvCellValue(cell.value);
    }
    return csvCellValue(cell);
  }));
  const body = `${rows.map((row) => row.map(escapeCsvCell).join(",")).join("\n")}\n`;
  return { body: Buffer.from(body, "utf8"), mimeType: "text/csv; charset=utf-8" };
};

const serializeArtifactXlsx = async (
  artifactName: string,
  file: { body: Buffer; mimeType: string } | undefined,
  preview: unknown
): Promise<{ body: Buffer; mimeType: string }> => {
  if (file && isXlsxContent(artifactName, file.mimeType)) {
    return { body: file.body, mimeType: XLSX_MIME_TYPE };
  }
  const sheetData = file
    ? sheetDataFromFileContent(artifactName, file)
    : sheetDataFromPreview(preview);
  const body = await writeXlsxFile(sheetData, {
    sheet: safeSheetName(artifactName),
    columns: inferSheetColumns(sheetData)
  }).toBuffer();
  return { body, mimeType: XLSX_MIME_TYPE };
};

const previewCsv = (preview: unknown): string | undefined => {
  if (!isRecord(preview) || !Array.isArray(preview.columns) || !Array.isArray(preview.rows)) {
    return undefined;
  }
  const columns = preview.columns.filter((column): column is string => typeof column === "string");
  if (columns.length === 0) {
    return undefined;
  }
  const escape = (value: unknown): string => {
    const text = value === null || value === undefined ? "" : String(value);
    return /[",\n\r]/u.test(text) ? `"${text.replace(/"/gu, '""')}"` : text;
  };
  const lines = [columns.map(escape).join(",")];
  preview.rows.forEach((row) => {
    if (Array.isArray(row)) {
      lines.push(row.map(escape).join(","));
    } else if (isRecord(row)) {
      lines.push(columns.map((column) => escape(row[column])).join(","));
    }
  });
  return `${lines.join("\n")}\n`;
};

const csvCellValue = (value: unknown): string => {
  if (value === null || value === undefined) {
    return "";
  }
  if (value instanceof Date) {
    return value.toISOString();
  }
  return typeof value === "object" ? JSON.stringify(value) : String(value);
};

const escapeCsvCell = (value: string): string =>
  /[",\n\r]/u.test(value) ? `"${value.replace(/"/gu, '""')}"` : value;

const sheetDataFromFileContent = (artifactName: string, file: { body: Buffer; mimeType: string }): SheetData => {
  const lowerName = artifactName.toLowerCase();
  const mimeType = file.mimeType.toLowerCase();
  if (mimeType.startsWith("text/csv") || lowerName.endsWith(".csv")) {
    return csvToSheetData(file.body.toString("utf8"));
  }
  if (mimeType.includes("json") || lowerName.endsWith(".json")) {
    return sheetDataFromJsonText(file.body.toString("utf8"));
  }
  if (mimeType.startsWith("text/") || lowerName.endsWith(".txt") || lowerName.endsWith(".md")) {
    return file.body.toString("utf8").split(/\r?\n/u).map((line) => [line]);
  }
  return [["content"], [`Binary artifact cannot be converted to tabular XLSX: ${artifactName}`]];
};

const sheetDataFromPreview = (preview: unknown): SheetData => {
  if (isRecord(preview) && Array.isArray(preview.columns) && Array.isArray(preview.rows)) {
    const columns = preview.columns.map((column) => String(column));
    const rows = preview.rows.map((row) => {
      if (Array.isArray(row)) {
        return row.map(spreadsheetCellValue);
      }
      if (isRecord(row)) {
        return columns.map((column) => spreadsheetCellValue(row[column]));
      }
      return [spreadsheetCellValue(row)];
    });
    return [columns, ...rows];
  }
  return unknownToSheetData(preview);
};

const sheetDataFromJsonText = (text: string): SheetData => {
  try {
    return unknownToSheetData(JSON.parse(text) as unknown);
  } catch {
    return [["content"], [text]];
  }
};

const unknownToSheetData = (value: unknown): SheetData => {
  if (Array.isArray(value)) {
    if (value.length === 0) {
      return [["value"]];
    }
    if (value.every(isRecord)) {
      const columns = [...new Set(value.flatMap((entry) => Object.keys(entry)))];
      return [columns, ...value.map((entry) => columns.map((column) => spreadsheetCellValue(entry[column])))];
    }
    return [["value"], ...value.map((entry) => [spreadsheetCellValue(entry)])];
  }
  if (isRecord(value)) {
    return [["key", "value"], ...Object.entries(value).map(([key, entry]) => [key, spreadsheetCellValue(entry)])];
  }
  return [["value"], [spreadsheetCellValue(value)]];
};

const csvToSheetData = (text: string): SheetData => {
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = "";
  let inQuotes = false;
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    const next = text[index + 1];
    if (char === '"' && inQuotes && next === '"') {
      cell += '"';
      index += 1;
      continue;
    }
    if (char === '"') {
      inQuotes = !inQuotes;
      continue;
    }
    if (char === "," && !inQuotes) {
      row.push(cell);
      cell = "";
      continue;
    }
    if ((char === "\n" || char === "\r") && !inQuotes) {
      if (char === "\r" && next === "\n") {
        index += 1;
      }
      row.push(cell);
      rows.push(row);
      row = [];
      cell = "";
      continue;
    }
    cell += char;
  }
  if (cell.length > 0 || row.length > 0) {
    row.push(cell);
    rows.push(row);
  }
  return rows.length > 0 ? rows.map((cells) => cells.map(spreadsheetCellValue)) : [["value"]];
};

const spreadsheetCellValue = (value: unknown): string | number | boolean | Date | null => {
  if (value === undefined || value === null) {
    return null;
  }
  if (typeof value === "number" || typeof value === "boolean") {
    return value;
  }
  if (value instanceof Date) {
    return value;
  }
  if (typeof value !== "string") {
    return JSON.stringify(value);
  }
  const trimmed = value.trim();
  if (trimmed === "") {
    return "";
  }
  if (trimmed === "true") {
    return true;
  }
  if (trimmed === "false") {
    return false;
  }
  if (/^-?(?:0|[1-9]\d*)(?:\.\d+)?$/u.test(trimmed) && !/^0\d/u.test(trimmed)) {
    const numberValue = Number(trimmed);
    if (Number.isFinite(numberValue)) {
      return numberValue;
    }
  }
  return value;
};

const inferSheetColumns = (sheetData: SheetData): Array<{ width: number }> => {
  const columnCount = sheetData.reduce((max, row) => Math.max(max, row.length), 0);
  return Array.from({ length: columnCount }, (_, columnIndex) => {
    const maxLength = sheetData.reduce((max, row) => {
      const cell = row[columnIndex];
      const text = cell === null || cell === undefined
        ? ""
        : typeof cell === "object" && "value" in cell
          ? String(cell.value ?? "")
          : String(cell);
      return Math.max(max, text.length);
    }, 8);
    return { width: Math.min(Math.max(maxLength + 2, 10), 40) };
  });
};

const isXlsxContent = (artifactName: string, mimeType: string): boolean =>
  mimeType === XLSX_MIME_TYPE || artifactName.toLowerCase().endsWith(".xlsx");

const safeSheetName = (name: string): string => {
  const sheet = basename(name).replace(/[:\\/?*[\]]/gu, " ").trim();
  return (sheet || "Artifact").slice(0, 31);
};

const resolveArtifactContentType = (
  artifact: { type: string; mime_type?: string | undefined },
  contentMimeType: string,
): string => {
  if (artifact.mime_type && artifact.mime_type !== "application/octet-stream") {
    return artifact.mime_type;
  }
  if (contentMimeType && contentMimeType !== "application/octet-stream") {
    return contentMimeType;
  }
  switch (artifact.type) {
    case "table":
      return "text/csv; charset=utf-8";
    case "markdown":
      return "text/markdown; charset=utf-8";
    case "html":
      return "text/html; charset=utf-8";
    default:
      return contentMimeType || "application/octet-stream";
  }
};

const safeDownloadName = (
  name: string,
  mimeType: string,
  artifactType?: string,
  declaredMimeType?: string | null,
): string => {
  const safe = basename(name).replace(/[^a-zA-Z0-9._-]+/gu, "-") || "artifact";
  if (safe.includes(".")) {
    return safe;
  }
  const effectiveMime = declaredMimeType && declaredMimeType !== "application/octet-stream"
    ? declaredMimeType
    : mimeType;
  if (effectiveMime === XLSX_MIME_TYPE || effectiveMime.includes("spreadsheetml")) {
    return `${safe}.xlsx`;
  }
  if (effectiveMime.startsWith("text/csv") || artifactType === "table") {
    return `${safe}.csv`;
  }
  if (effectiveMime.startsWith("text/markdown") || artifactType === "markdown") {
    return `${safe}.md`;
  }
  if (effectiveMime.startsWith("text/html") || artifactType === "html") {
    return `${safe}.html`;
  }
  if (effectiveMime.startsWith("text/")) {
    return `${safe}.txt`;
  }
  return `${safe}.json`;
};

const safeDownloadNameWithExtension = (name: string, extension: string): string => {
  const safe = basename(name).replace(/[^a-zA-Z0-9._-]+/gu, "-") || "artifact";
  const extensionWithDot = extension.startsWith(".") ? extension : `.${extension}`;
  const dot = safe.lastIndexOf(".");
  return `${dot > 0 ? safe.slice(0, dot) : safe}${extensionWithDot}`;
};

const numberFromEnv = (name: string, fallback: number): number => {
  const parsed = Number.parseInt(process.env[name] ?? "", 10);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : fallback;
};

const mimeTypeForPath = (path: string): string => {
  const extension = path.toLowerCase().split(".").pop();
  const mimeTypes: Record<string, string> = {
    csv: "text/csv; charset=utf-8",
    json: "application/json; charset=utf-8",
    md: "text/markdown; charset=utf-8",
    png: "image/png",
    svg: "image/svg+xml",
    txt: "text/plain; charset=utf-8"
  };
  return mimeTypes[extension ?? ""] ?? "application/octet-stream";
};

const handleWorkspaceConfigPatch = async (
  request: IncomingMessage,
  context: Required<ConfigApiContext>
): Promise<ConfigApiResponse> => {
  const body = await readJsonBody(request);
  const updated = withConfigTransaction(context.metadataStore, () => {
    const datasourceUpdates = workspaceUpdates(body.datasources);
    for (const update of datasourceUpdates) {
      const current = context.metadataStore.dataSources.get({ user_id: context.userId, datasource_id: update.id });
      const config = parseRecord(current.config_json);
      context.metadataStore.dataSources.create({
        user_id: context.userId,
        id: current.id,
        name: current.name,
        type: current.type,
        config: { ...config, defaultEnabled: update.defaultEnabled },
        ...(current.credential_ref ? { credential_ref: current.credential_ref } : {}),
        ...(current.description ? { description: current.description } : {}),
        status: current.status,
        ...(update.revision !== undefined ? { expected_revision: update.revision } : {})
      });
    }
    const groups: Array<{ key: string; kind: ConfigResourceKind }> = [
      { key: "knowledgeBases", kind: "knowledge-base" },
      { key: "mcpServers", kind: "mcp-server" },
      { key: "modelProfiles", kind: "model-profile" },
      { key: "skills", kind: "skill" }
    ];
    for (const group of groups) {
      for (const update of workspaceUpdates(body[group.key])) {
        const current = context.metadataStore.configResources.get({
          id: update.id,
          workspace_id: context.workspaceId,
          user_id: context.userId,
          kind: group.kind
        });
        context.metadataStore.configResources.upsert({
          id: current.id,
          workspace_id: current.workspace_id,
          user_id: current.user_id,
          kind: current.kind,
          name: current.name,
          ...(current.description ? { description: current.description } : {}),
          payload: current.payload,
          ...(current.secret_ref ? { secret_ref: current.secret_ref } : {}),
          default_enabled: update.defaultEnabled,
          builtin: current.builtin,
          status: current.status,
          ...(update.revision !== undefined ? { expected_revision: update.revision } : {})
        });
      }
    }
    return buildWorkspaceConfig(context);
  });
  return ok(updated);
};

const buildWorkspaceConfig = (context: Required<ConfigApiContext>): Record<string, unknown> => ({
  datasources: context.metadataStore.dataSources.list({ user_id: context.userId }).map(dataSourceDto),
  knowledgeBases: listConfig(context, "knowledge-base"),
  mcpServers: listConfig(context, "mcp-server"),
  modelProfiles: listConfig(context, "model-profile"),
  skills: listConfig(context, "skill")
});

const buildRunDefaults = (context: Required<ConfigApiContext>): Record<string, unknown> => {
  const datasourceIds = context.metadataStore.dataSources.list({ user_id: context.userId })
    .filter((item) => booleanValue(parseRecord(item.config_json).defaultEnabled, true) && item.status === "ready")
    .map((item) => item.id);
  const enabled = (kind: ConfigResourceKind): ConfigResourceRecord[] =>
    context.metadataStore.configResources.list({
      workspace_id: context.workspaceId,
      user_id: context.userId,
      kind
    }).filter((item) => item.default_enabled);
  const modelProfiles = enabled("model-profile");
  return {
    enabledDatasourceIds: datasourceIds,
    enabledKnowledgeIds: enabled("knowledge-base").map((item) => item.id),
    enabledMcpServerIds: enabled("mcp-server").map((item) => item.id),
    enabledSkillIds: enabled("skill").map((item) => item.id),
    ...(datasourceIds[0] ? { activeDatasourceId: datasourceIds[0] } : {}),
    activeLlmProfileId: preferConnectedResourceId(modelProfiles),
    activeSkillId: enabled("skill")[0]?.id
  };
};

const listConfig = (context: Required<ConfigApiContext>, kind: ConfigResourceKind): Record<string, unknown>[] =>
  context.metadataStore.configResources.list({
    workspace_id: context.workspaceId,
    user_id: context.userId,
    kind
  }).map(configResourceDto);

const devIdentityUserDto = (user: UserRecord): Record<string, unknown> => ({
  id: user.id,
  ...(user.email ? { email: user.email } : {}),
  ...(user.display_name ? { displayName: user.display_name } : {}),
  ...(user.dev_token ? { devToken: user.dev_token } : {})
});

const defaultWorkspaceDto = (workspaceId: string): Record<string, unknown> => ({
  id: workspaceId,
  name: workspaceId === DEFAULT_WORKSPACE_ID ? "Default workspace" : workspaceId
});

const sanitizeDevUserId = (value: string): string => {
  const id = value.trim();
  if (!/^[a-zA-Z0-9._-]{1,128}$/u.test(id)) {
    throw new Error("INVALID_DEV_USER_ID");
  }
  return id;
};

const sanitizeDisplayName = (value: string): string => {
  const normalized = value.replace(/\s+/g, " ").trim();
  if (!normalized) {
    throw new Error("DEV_USER_DISPLAY_NAME_REQUIRED");
  }
  return normalized.slice(0, 80);
};

const sanitizeOptionalEmail = (value: string | undefined, id: string): string => {
  const normalized = value?.trim();
  if (!normalized) {
    return `${id}@local.dev`;
  }
  if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/u.test(normalized)) {
    throw new Error("INVALID_DEV_USER_EMAIL");
  }
  return normalized.slice(0, 254);
};

const dataSourceDto = (record: DataSourceRecord): Record<string, unknown> => {
  const config = parseRecord(record.config_json);
  return {
    id: record.id,
    name: record.name,
    description: record.description ?? "",
    type: record.type,
    mode: "readonly",
    config,
    secretRef: record.credential_ref,
    hasSecret: Boolean(record.credential_ref),
    defaultEnabled: booleanValue(config.defaultEnabled, true),
    builtin: booleanValue(config.builtin, false),
    connectionStatus: datasourceStatus(record),
    revision: record.revision,
    createdAt: record.created_at,
    updatedAt: record.updated_at
  };
};

const configResourceDto = (record: ConfigResourceRecord): Record<string, unknown> => ({
  id: record.id,
  name: record.name,
  description: record.description ?? "",
  ...publicPayload(record.payload),
  secretRef: record.secret_ref,
  hasSecret: Boolean(record.secret_ref),
  defaultEnabled: record.default_enabled,
  builtin: record.builtin,
  [resourceStatusField(record.kind)]: record.status,
  revision: record.revision,
  createdAt: record.created_at,
  updatedAt: record.updated_at
});

const knowledgeDocumentDto = (document: {
  id: string;
  user_id: string;
  collection_id: string;
  filename: string;
  file_asset_ref_id?: string;
  mime_type: string;
  status: string;
}): Record<string, unknown> => ({
  id: document.id,
  userId: document.user_id,
  collectionId: document.collection_id,
  filename: document.filename,
  ...(document.file_asset_ref_id ? { fileAssetRefId: document.file_asset_ref_id } : {}),
  mimeType: document.mime_type,
  status: document.status
});

const readJsonBody = async (request: IncomingMessage): Promise<Record<string, unknown>> => {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of request) {
    const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    size += buffer.length;
    if (size > MAX_JSON_BODY_BYTES) {
      throw new Error("REQUEST_BODY_TOO_LARGE");
    }
    chunks.push(buffer);
  }
  if (chunks.length === 0) {
    return {};
  }
  const parsed: unknown = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  if (!isRecord(parsed)) {
    throw new Error("JSON_OBJECT_REQUIRED");
  }
  return parsed;
};

const skillUploadBody = async (
  request: IncomingMessage,
  context: Required<ConfigApiContext>
): Promise<Record<string, unknown>> => {
  if (!isMultipart(request)) {
    throw new Error("SKILL_MULTIPART_REQUIRED");
  }
  const upload = await readMultipartUpload(request);
  const parsed = await parseSkillPackage(upload.file);
  const knownTools = new Set<string>(STATIC_AGENT_TOOL_NAMES);
  for (const toolName of skillUploadMcpToolNames(upload.fields, context)) {
    knownTools.add(toolName);
  }
  const unknownTools = parsed.allowedTools.filter((tool) => !knownTools.has(tool) && !isLegacyMcpToolName(tool));
  if (unknownTools.length > 0) {
    throw new Error(`SKILL_ALLOWED_TOOL_UNKNOWN:${unknownTools.join(",")}`);
  }
  const packageRef = context.fileAssetService.createRef({
    user_id: context.userId,
    workspace_id: context.workspaceId,
    filename: parsed.packageFileName,
    content: upload.file.content,
    declared_mime_type: upload.file.mimeType || mimeTypeForFilename(parsed.packageFileName),
    source: "skill-package",
    metadata: { kind: "skill-package", skill: parsed.name, version: parsed.version }
  });
  await materializeSkillPackages({
    fileAssetService: context.fileAssetService,
    runDir: resolveConfigSkillCacheDir(context),
    skills: [{
      allowedTools: parsed.allowedTools,
      builtin: false,
      defaultDbIds: [],
      defaultEnabled: true,
      defaultKbIds: [],
      defaultMcpIds: [],
      deniedTools: parsed.deniedTools,
      description: parsed.description,
      id: slugify(parsed.name),
      name: parsed.name,
      packageEntry: parsed.manifest.entry,
      packageFileRefId: packageRef.ref.id,
      packageFiles: parsed.manifest.files,
      packageFormat: parsed.packageFormat,
      revision: 1,
      scope: "workspace",
      status: "valid",
      tags: parsed.tags,
      userInvocable: parsed.userInvocable,
      version: parsed.version
    }],
    userId: context.userId,
    workspaceId: context.workspaceId
  });
  return {
    ...upload.fields,
    ...buildSkillResourcePayload({
      fields: upload.fields,
      packageFileRefId: packageRef.ref.id,
      parsed
    }),
    description: parsed.description,
    name: parsed.name,
    status: "valid"
  };
};

const skillUploadMcpToolNames = (
  fields: Record<string, string>,
  context: Required<ConfigApiContext>
): string[] => {
  const serverIds = csvStringValue(fields.defaultMcpIds ?? fields.default_mcp_ids);
  const names: string[] = [];
  for (const serverId of serverIds) {
    const resource = context.metadataStore.configResources.get({
      id: serverId,
      workspace_id: context.workspaceId,
      user_id: context.userId,
      kind: "mcp-server"
    });
    const manifest = resource.payload.toolManifest;
    const allowlist = mcpToolAllowlistValue(resource.payload.toolAllowlist);
    if (!Array.isArray(manifest)) {
      continue;
    }
    for (const tool of manifest) {
      if (!isRecord(tool) || typeof tool.name !== "string") {
        continue;
      }
      if (matchesMcpToolAllowlist(resource.id, tool.name, allowlist)) {
        names.push(tool.name);
      }
    }
  }
  return names;
};

const isLegacyMcpToolName = (toolName: string): boolean => toolName.startsWith("mcp__");

const resolveConfigSkillCacheDir = (context: Required<ConfigApiContext>): string => {
  const workspaceRoot = process.env.WORKSPACE_ROOT ?? join(process.env.STORAGE_ROOT_DIR ?? "storage", "workspaces");
  return resolveSkillCacheDir({
    runContext: {
      user_id: context.userId,
      workspace_id: context.workspaceId,
      session_id: "skill-cache",
      run_id: "skill-cache",
      selected_datasource_id: "",
      enabled_datasource_ids: [],
      user_input: "",
      chat_mode: "config",
      model_name: "skill-cache"
    },
    workspaceRoot
  });
};

const handleSkillSelectionPreview = async (
  request: IncomingMessage,
  context: Required<ConfigApiContext>
): Promise<ConfigApiResponse> => {
  const body = await readJsonBody(request);
  const userInput = stringValue(body.user_input) ?? stringValue(body.userInput) ?? "";
  const runConfig = recordValue(body.run_config) ?? recordValue(body.runConfig) ?? {};
  const effectiveRunConfig = resolveEffectiveRunConfig(
    {
      context: [],
      forwardedProps: { run_config: runConfig },
      messages: [{ id: "skill-selection-preview", role: "user", content: userInput }],
      runId: "skill-selection-preview",
      threadId: "skill-selection-preview"
    } as never,
    context.metadataStore,
    context.userId,
    undefined,
    context.workspaceId
  );
  const selection = selectSkillsForRun({
    metadataStore: context.metadataStore,
    runConfig: effectiveRunConfig,
    userId: context.userId,
    userInput,
    workspaceId: context.workspaceId
  });
  return ok({
    audit: selection.audit,
    effectivePolicy: selection.effectiveToolPolicy,
    skills: selection.selectedSkills.map((skill) => ({
      id: skill.id,
      name: skill.name,
      description: skill.description,
      revision: skill.revision,
      tags: skill.tags
    }))
  });
};

const errorResponse = (error: unknown): ConfigApiResponse => {
  const message = messageOf(error);
  if (message.startsWith("REVISION_CONFLICT")) {
    return fail(409, "REVISION_CONFLICT", message);
  }
  if (message.startsWith("RUN_NOT_BRANCHABLE")) {
    return fail(409, "RUN_NOT_BRANCHABLE", message);
  }
  if (message.startsWith("RUN_NOT_VISIBLE")) {
    return fail(404, "RESOURCE_NOT_FOUND", message);
  }
  if (message.startsWith("SESSION_BRANCH_CYCLE")) {
    return fail(409, "REVISION_CONFLICT", message);
  }
  if (message.startsWith("SESSION_BRANCH_PARENT_NOT_VISIBLE")) {
    return fail(404, "RESOURCE_NOT_FOUND", message);
  }
  if (message.includes("NOT_FOUND") || message.includes("not found")) {
    return fail(404, "RESOURCE_NOT_FOUND", message);
  }
  if (message.startsWith("SECRET_MASTER_KEY_REQUIRED")) {
    return fail(503, "SECRET_MASTER_KEY_REQUIRED", message);
  }
  if (message.startsWith("DATASOURCE_TEST_FAILED")) {
    return fail(422, "DATASOURCE_TEST_FAILED", message);
  }
  if (
    message.startsWith("MCP_TEST_FAILED")
    || message.startsWith("MCP_SERVER_")
    || message.startsWith("MCP_STDIO_")
  ) {
    return fail(422, "BAD_REQUEST", message);
  }
  // Preview-only table/chart artifacts have no file_asset_ref_id and cannot be promoted.
  if (message.startsWith("ARTIFACT_PROMOTE_NO_FILE")) {
    return fail(422, "BAD_REQUEST", message);
  }
  if (
    message.startsWith("UNSUPPORTED_FILE_TYPE")
    || message.startsWith("KNOWLEDGE_FILE_TYPE_UNSUPPORTED")
  ) {
    return fail(415, "UNSUPPORTED_FILE_TYPE", message);
  }
  // PDF is an allowed knowledge type; empty/unparseable payloads are client errors.
  if (
    message.startsWith("KNOWLEDGE_PDF_EMPTY")
    || message.startsWith("KNOWLEDGE_PDF_PARSE_FAILED")
  ) {
    return fail(422, "BAD_REQUEST", message);
  }
  if (message.startsWith("PROVIDER_") || message.startsWith("MODEL_FALLBACK")) {
    return fail(422, "PROVIDER_TEST_FAILED", message);
  }
  if (message.startsWith("BUILTIN_RESOURCE_READONLY") || message.startsWith("SECRET_OWNER_MISMATCH")) {
    return fail(409, "CONFLICT", message);
  }
  if (error instanceof SyntaxError || message.includes("REQUIRED") || message.includes("INVALID")) {
    return fail(400, "BAD_REQUEST", message);
  }
  return fail(500, "INTERNAL_ERROR", message);
};

const ok = (data: unknown, status = 200): ConfigApiResponse => ({ body: createSuccessResult(data), status });
const fail = (status: number, code: AppErrorCode, message: string): ConfigApiResponse => ({
  body: createErrorResult(code, message),
  status
});
const methodNotAllowed = (): ConfigApiResponse => fail(405, "BAD_REQUEST", "Method not allowed.");
const messageOf = (value: unknown): string => value instanceof Error ? value.message : String(value);
const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null;
const recordValue = (value: unknown): Record<string, unknown> | undefined => isRecord(value) ? value : undefined;
const arrayValue = (value: unknown): unknown[] | undefined => Array.isArray(value) ? value : undefined;
const stringValue = (value: unknown): string | undefined =>
  typeof value === "string" && value.trim() ? value.trim() : undefined;
const numberValue = (value: unknown): number | undefined =>
  typeof value === "number" && Number.isFinite(value) ? value : undefined;
const booleanValue = (value: unknown, fallback: boolean): boolean => typeof value === "boolean" ? value : fallback;
const clampInteger = (value: number, min: number, max: number, fallback: number): number =>
  Number.isInteger(value) ? Math.min(max, Math.max(min, value)) : fallback;
const stringArrayValue = (value: unknown): string[] =>
  Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string" && item.trim().length > 0)
    : [];
const evidenceRefsFromUnknown = (value: unknown): EvidenceRef[] => {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.filter(isEvidenceRef);
};
const isEvidenceRef = (value: unknown): value is EvidenceRef => {
  if (!isRecord(value) || !isRecord(value.source)) {
    return false;
  }
  return (
    typeof value.id === "string" &&
    isEvidenceKind(value.kind) &&
    typeof value.label === "string" &&
    typeof value.sessionId === "string"
  );
};
const isEvidenceKind = (value: unknown): value is EvidenceKind =>
  value === "table" ||
  value === "chart" ||
  value === "report" ||
  value === "file" ||
  value === "sql" ||
  value === "schema" ||
  value === "preview" ||
  value === "knowledge" ||
  value === "step";
const parseRecord = (value: string): Record<string, unknown> => {
  const parsed: unknown = JSON.parse(value);
  return isRecord(parsed) ? parsed : {};
};
const tryParseRecord = (value: string): Record<string, unknown> | undefined => {
  try {
    return parseRecord(value);
  } catch {
    return undefined;
  }
};
const slugify = (value: string): string => value.toLowerCase().replace(/[^a-z0-9_-]+/gu, "-").replace(/^-|-$/gu, "");
// A datasource is only "connected" once it has actually passed a connectivity
// test (`last_test_at` is set). A freshly created/seeded "ready" record that has
// never been tested reports "untested" so the client can gate + auto-test it.
const datasourceStatus = (record: DataSourceRecord): string => {
  if (record.status === "ready") {
    return record.last_test_at ? "connected" : "untested";
  }
  return record.status;
};
const resourceStatusField = (kind: ConfigResourceKind): string => {
  if (kind === "knowledge-base") {
    return "indexStatus";
  }
  if (kind === "mcp-server") {
    return "healthStatus";
  }
  if (kind === "skill") {
    return "validationStatus";
  }
  return "connectionStatus";
};
const resourceStatusValue = (body: Record<string, unknown>, kind: ConfigResourceKind): string | undefined =>
  stringValue(body[resourceStatusField(kind)]) ?? stringValue(body.status);
const defaultResourceStatus = (kind: ConfigResourceKind): string => kind === "knowledge-base" ? "empty" : "untested";
const workspaceUpdates = (value: unknown): Array<{ id: string; defaultEnabled: boolean; revision?: number }> => {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.map((entry) => {
    if (typeof entry === "string") {
      return { id: entry, defaultEnabled: true };
    }
    if (!isRecord(entry) || !stringValue(entry.id) || typeof entry.defaultEnabled !== "boolean") {
      throw new Error("WORKSPACE_CONFIG_UPDATE_INVALID");
    }
    const revision = numberValue(entry.revision);
    return {
      id: stringValue(entry.id) as string,
      defaultEnabled: entry.defaultEnabled,
      ...(revision !== undefined ? { revision } : {})
    };
  });
};

const withConfigTransaction = <T>(metadataStore: MetadataStore, operation: () => T): T => {
  metadataStore.db.exec("BEGIN IMMEDIATE");
  try {
    const result = operation();
    metadataStore.db.exec("COMMIT");
    return result;
  } catch (error) {
    metadataStore.db.exec("ROLLBACK");
    throw error;
  }
};
const findDatasource = (store: MetadataStore, userId: string, id: string): DataSourceRecord | undefined => {
  try {
    return store.dataSources.get({ user_id: userId, datasource_id: id });
  } catch {
    return undefined;
  }
};

const isMultipart = (request: IncomingMessage): boolean =>
  String(request.headers["content-type"] ?? "").toLowerCase().startsWith("multipart/form-data");

const isSupportedChatUpload = (filename: string, mimeType: string): boolean => {
  const lowerName = filename.toLowerCase();
  const dot = lowerName.lastIndexOf(".");
  const extension = dot >= 0 ? lowerName.slice(dot) : "";
  return CHAT_UPLOAD_TYPES.has(mimeType.toLowerCase()) || CHAT_UPLOAD_EXTENSIONS.has(extension);
};

const isSupportedDatasourceUpload = (filename: string): boolean => {
  const lowerName = filename.toLowerCase();
  const dot = lowerName.lastIndexOf(".");
  const extension = dot >= 0 ? lowerName.slice(dot) : "";
  return DATASOURCE_UPLOAD_EXTENSIONS.has(extension);
};

const uniqueUploadFilename = (directory: string, filename: string): string => {
  const safe = safeUploadFilename(filename);
  const dot = safe.lastIndexOf(".");
  const stem = dot > 0 ? safe.slice(0, dot) : safe;
  const extension = dot > 0 ? safe.slice(dot) : "";
  let candidate = safe;
  let counter = 2;
  while (existsSync(resolve(directory, candidate))) {
    candidate = `${stem}-${counter}${extension}`;
    counter += 1;
  }
  return candidate;
};

const safeUploadFilename = (filename: string): string => {
  const safe = basename(filename).replace(/[^a-zA-Z0-9._ -]+/gu, "-").trim();
  if (!safe || safe === "." || safe === "..") {
    return `upload-${randomUUID()}`;
  }
  return safe;
};

const stringHeader = (value: string | string[] | undefined): string | undefined =>
  Array.isArray(value) ? value[0] : value;

const publicPayload = (payload: Record<string, unknown>): Record<string, unknown> =>
  Object.fromEntries(Object.entries(payload).filter(([key]) =>
    !["apiKey", "api_key", "headers", "packageBase64", "packageContent", "password", "token"].includes(key)));

const resourceCredentials = (
  body: Record<string, unknown>,
  kind: ConfigResourceKind
): Record<string, unknown> | undefined => {
  const explicit = recordValue(body.credentials);
  if (explicit) {
    return explicit;
  }
  const keys = kind === "model-profile" || kind === "knowledge-base"
    ? ["apiKey", "api_key"]
    : kind === "mcp-server"
      ? ["token", "apiKey", "headers"]
      : [];
  const entries = keys.flatMap((key) => body[key] === undefined ? [] : [[key, body[key]] as [string, unknown]]);
  return entries.length > 0 ? Object.fromEntries(entries) : undefined;
};

const normalizeDatasourceConfig = (
  type: string,
  config: Record<string, unknown>
): Record<string, unknown> => {
  const filePath = stringValue(config.filePath) ?? stringValue(config.file_path);
  if ((type === "sqlite" || type === "duckdb" || type === "access") && filePath) {
    return { ...config, path: filePath, filePath: undefined, file_path: undefined };
  }
  if ((type === "csv" || type === "xlsx") && filePath) {
    return { ...config, file_path: filePath, filePath: undefined };
  }
  return config;
};

const listMcpTools = async (
  resource: ConfigResourceRecord,
  context: Required<ConfigApiContext>
): Promise<Array<Record<string, unknown>>> => {
  const transport = stringValue(resource.payload.transport) ?? "streamable-http";
  const urlOrCommand = stringValue(resource.payload.serverUrl)
    ?? stringValue(resource.payload.url)
    ?? (transport === "stdio" ? stringValue(resource.payload.command) : undefined);
  if (!urlOrCommand || (transport !== "streamable-http" && transport !== "sse" && transport !== "stdio")) {
    throw new Error("MCP_SERVER_CONFIG_INVALID");
  }
  const secret = resource.secret_ref
    ? context.metadataStore.secrets.get({
        ref: resource.secret_ref,
        workspace_id: context.workspaceId,
        user_id: context.userId
      })
    : {};
  const token = stringValue(secret.token) ?? stringValue(secret.apiKey);
  const headers = token ? { Authorization: `Bearer ${token}` } : undefined;
  const requestOptions = headers ? { requestInit: { headers } } : undefined;
  const client = new Client({ name: "datafoundry-config", version: "0.2.0" });
  try {
    const clientTransport = transport === "stdio"
      ? new StdioClientTransport({
          ...resolveStdioCommand(resource.payload, urlOrCommand),
          stderr: "pipe"
        })
      : transport === "sse"
        ? new SSEClientTransport(new URL(urlOrCommand), requestOptions)
        : new StreamableHTTPClientTransport(new URL(urlOrCommand), requestOptions);
    await client.connect(clientTransport as unknown as Transport);
    const result = await client.listTools();
    const allowlist = mcpToolAllowlistValue(resource.payload.toolAllowlist);
    return result.tools
      .filter((tool) => matchesMcpToolAllowlist(resource.id, tool.name, allowlist))
      .map((tool) => ({
        name: tool.name,
        description: tool.description ?? "",
        inputSchema: tool.inputSchema
      }));
  } finally {
    await client.close().catch(() => undefined);
  }
};

const resolveStdioCommand = (
  payload: Record<string, unknown>,
  fallbackCommand: string
): { args?: string[]; command: string; cwd?: string; env?: Record<string, string> } => {
  const command = stringValue(payload.command);
  const args = stringArrayValue(payload.args);
  const cwd = stringValue(payload.cwd);
  const env = recordStringMapValue(payload.env);
  if (command) {
    return {
      command,
      ...(args.length > 0 ? { args } : {}),
      ...(cwd ? { cwd } : {}),
      ...(env ? { env } : {})
    };
  }
  const parts = splitCommandLine(fallbackCommand);
  const head = parts[0];
  if (!head) {
    throw new Error("MCP_STDIO_COMMAND_REQUIRED");
  }
  return {
    command: head,
    ...(parts.length > 1 ? { args: parts.slice(1) } : {}),
    ...(cwd ? { cwd } : {}),
    ...(env ? { env } : {})
  };
};

const mcpToolAllowlistValue = (value: unknown): string[] | undefined => {
  const values = Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string" && item.trim().length > 0)
    : csvStringValue(value);
  return values.length > 0 ? values : undefined;
};

const csvStringValue = (value: unknown): string[] => {
  if (typeof value !== "string") {
    return [];
  }
  return value.split(",").map((item) => item.trim()).filter(Boolean);
};

const matchesMcpToolAllowlist = (
  serverId: string,
  toolName: string,
  allowlist: string[] | undefined
): boolean => {
  if (!allowlist || allowlist.length === 0) {
    return true;
  }
  return mcpToolAllowlistCandidates(toolName).some((candidate) => {
    const namespaced = `mcp__${sanitizeMcpName(serverId)}__${sanitizeMcpName(candidate)}`;
    return allowlist.includes(candidate) || allowlist.includes(namespaced);
  });
};

const mcpToolAllowlistCandidates = (toolName: string): string[] => {
  if (toolName === "datalink_show" || toolName === "datagraph_show") {
    return ["datalink_show", "datagraph_show"];
  }
  if (toolName === "datalink_explore" || toolName === "datagraph_explore") {
    return ["datalink_explore", "datagraph_explore"];
  }
  return [toolName];
};

const recordStringMapValue = (value: unknown): Record<string, string> | undefined => {
  if (!isRecord(value)) {
    return undefined;
  }
  const entries = Object.entries(value).filter((entry): entry is [string, string] => typeof entry[1] === "string");
  return entries.length > 0 ? Object.fromEntries(entries) : undefined;
};

const splitCommandLine = (value: string): string[] => {
  const parts: string[] = [];
  let current = "";
  let quote: "\"" | "'" | undefined;
  for (let index = 0; index < value.length; index += 1) {
    const char = value[index];
    if ((char === "\"" || char === "'") && !quote) {
      quote = char;
      continue;
    }
    if (char === quote) {
      quote = undefined;
      continue;
    }
    if (/\s/u.test(char ?? "") && !quote) {
      if (current) {
        parts.push(current);
        current = "";
      }
      continue;
    }
    current += char;
  }
  if (current) {
    parts.push(current);
  }
  return parts;
};

const sanitizeMcpName = (value: string): string => value.replace(/[^a-zA-Z0-9_-]/gu, "_");

const mcpTestFailureMessage = (error: unknown): string => {
  const message = error instanceof Error ? error.message : String(error);
  const trimmed = message.trim() || "MCP server probe failed.";
  if (
    trimmed.startsWith("MCP_TEST_FAILED:")
    || trimmed.startsWith("MCP_SERVER_")
    || trimmed.startsWith("MCP_STDIO_")
  ) {
    return trimmed;
  }
  return `MCP_TEST_FAILED:${trimmed}`;
};

/**
 * Persist config-resource test status; retry on revision conflict.
 * Success writes must not surface REVISION_CONFLICT after a completed probe —
 * concurrent saves can bump revision while the probe runs; keep merging onto
 * the latest row. If retries still conflict, force-write without expected_revision.
 */
const persistResourceTestStatus = (input: {
  context: Required<ConfigApiContext>;
  resource: ConfigResourceRecord;
  status: "connected" | "failed";
  payload?: Record<string, unknown>;
}): ConfigResourceRecord => {
  const write = (
    current: ConfigResourceRecord,
    options?: { force?: boolean },
  ): ConfigResourceRecord =>
    input.context.metadataStore.configResources.upsert({
      id: current.id,
      workspace_id: input.context.workspaceId,
      user_id: input.context.userId,
      kind: current.kind,
      name: current.name,
      ...(current.description ? { description: current.description } : {}),
      payload: input.payload ?? current.payload,
      ...(current.secret_ref ? { secret_ref: current.secret_ref } : {}),
      default_enabled: current.default_enabled,
      builtin: current.builtin,
      status: input.status,
      ...(options?.force ? {} : { expected_revision: current.revision })
    });

  const readLatest = (): ConfigResourceRecord =>
    input.context.metadataStore.configResources.get({
      id: input.resource.id,
      workspace_id: input.context.workspaceId,
      user_id: input.context.userId,
      kind: input.resource.kind
    });

  const maxAttempts = 5;
  let current = input.resource;
  let lastError: unknown;
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    try {
      return write(current);
    } catch (error) {
      lastError = error;
      if (!isRevisionConflictError(error)) {
        throw error;
      }
      current = readLatest();
      if (current.status === input.status && input.status === "connected") {
        return current;
      }
    }
  }
  if (input.status === "connected") {
    // Probe already succeeded — force persist so the UI never sees REVISION_CONFLICT.
    return write(readLatest(), { force: true });
  }
  throw lastError instanceof Error ? lastError : new Error(String(lastError));
};

const resolveProfileProvider = (
  resource: ConfigResourceRecord,
  context: Required<ConfigApiContext>
): Exclude<ReturnType<typeof createModelProviderFromEnv>, { kind: "mock" }> => {
  const provider = resource.id === "server-default"
    ? createModelProviderFromEnv(process.env)
    : createModelProviderFromProfile({
        provider: stringValue(resource.payload.provider) ?? "openai-compatible",
        model: stringValue(resource.payload.modelName) ?? stringValue(resource.payload.model) ?? "",
        base_url: stringValue(resource.payload.baseUrl) ?? stringValue(resource.payload.base_url) ?? "",
        ...profileApiKey(resource, context)
      });
  if (provider.kind === "mock") {
    throw new Error(`PROVIDER_CONFIG_MISSING:${resource.id}`);
  }
  return provider;
};

const profileApiKey = (
  resource: ConfigResourceRecord,
  context: Required<ConfigApiContext>
): { api_key?: string } => {
  if (!resource.secret_ref) {
    return {};
  }
  const secret = context.metadataStore.secrets.get({
    ref: resource.secret_ref,
    workspace_id: context.workspaceId,
    user_id: context.userId
  });
  const apiKey = stringValue(secret.apiKey) ?? stringValue(secret.api_key);
  return apiKey ? { api_key: apiKey } : {};
};
