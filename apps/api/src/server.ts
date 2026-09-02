import {
  CopilotRuntime,
  ExperimentalEmptyAdapter,
  copilotRuntimeNodeHttpEndpoint
} from "@copilotkit/runtime";
import {
  resolveSkillCacheDir,
  type AgentMemoryMode
} from "@datafoundry/agent-runtime";
import { LocalArtifactService, SessionOutputService } from "@datafoundry/artifacts";
import { type MeResponse, createEnvConfig, createErrorResult, createSuccessResult } from "@datafoundry/contracts";
import { LocalDataGateway } from "@datafoundry/data-gateway";
import { LocalFileAssetService } from "@datafoundry/files";
import { LocalKnowledgeService } from "@datafoundry/knowledge";
import {
  createMetadataStore,
  type UserRecord,
  type MetadataStore
} from "@datafoundry/metadata";
import {
  buildSkillResourcePayload,
  configResourceToSkillRecord,
  materializeSkillPackages,
  parseSkillPackage
} from "@datafoundry/skills";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { createServer as createHttpServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { handleConfigApiRequest } from "./config-api.js";
import { createAsyncMemoByKey, createStartupTimer } from "./async-memo.js";
import { ensureBuiltinDtcGrowthDatasource } from "./builtin-dtc-growth-datasource.js";
import { reclaimOrphanedQueuedAndRunningRuns } from "./stale-active-runs.js";
import { loadPasswordAuthConfig } from "./auth/config.js";
import { AuthService, type AuthIdentity } from "./auth/service.js";
import { serverDefaultConnectionStatus, isServerLlmEnvConfigured } from "./model-profile-connection-status.js";
import {
  handleAuthApiRequest,
  isUnsafeMethod,
  resolvePasswordSessionIdentity,
  sendAuthError
} from "./auth/routes.js";
import { RunCancelRegistry } from "./run-cancel-registry.js";
import { createRuntimeTransport, probeRuntimeHealth } from "./runtime/factory.js";
import { DataFoundryAgUiAgent } from "./runtime-agent.js";
import type { RuntimeHealth, RuntimeTransport } from "./runtime/types.js";

const COPILOTKIT_PATH = "/api/copilotkit";
const DEFAULT_WORKSPACE_ID = "default";
const SERVER_DIR = dirname(fileURLToPath(import.meta.url));
const BUILTIN_SKILL_ROOT = join(SERVER_DIR, "../../../packages/skills/builtin");
const skillCacheSignatures = new Map<string, string>();
const legacyDemoRemovedUsers = new Set<string>();
/** Set true only after createServer finishes required control-plane init. */
let serverReady = false;
let startupTimings: Record<string, number> = {};
let startupTotalMs = 0;
let runtimeHealth: RuntimeHealth | undefined;

export type CreateServerOptions = {
  conversationMemoryMode?: AgentMemoryMode | undefined;
  memoryExtractionTimeoutMs?: number | undefined;
  metadataStore?: MetadataStore;
  runtime?: RuntimeTransport;
};

export const createServer = async (options: CreateServerOptions = {}): Promise<Server> => {
  const timer = createStartupTimer();
  serverReady = false;

  const envConfig = createEnvConfig(process.env);
  const authConfig = loadPasswordAuthConfig(process.env);
  const metadataStore = await timer.measure("metadata_store", () =>
    options.metadataStore ??
    createMetadataStore({
      database_path: process.env.METADATA_DB_PATH ?? join(envConfig.storage.root_dir, "metadata", "workbench.sqlite"),
      ...(envConfig.storage.secret_master_key ? { secret_master_key: envConfig.storage.secret_master_key } : {})
    }),
  );
  const fileAssetService = new LocalFileAssetService(metadataStore, {
    storageRoot: process.env.FILE_ASSET_STORAGE_ROOT ?? join(envConfig.storage.root_dir, "files")
  });
  const dataGateway = new LocalDataGateway(metadataStore, {
    defaultLimit: envConfig.sql.default_limit,
    maxLimit: envConfig.sql.max_limit,
    timeoutMs: envConfig.sql.timeout_ms,
    workspaceId: DEFAULT_WORKSPACE_ID
  }, fileAssetService);
  const artifactService = new LocalArtifactService(metadataStore, fileAssetService);
  const sessionOutputService = new SessionOutputService(metadataStore, fileAssetService);
  const knowledgeService = new LocalKnowledgeService(metadataStore, {
    embedding: {
      provider: envConfig.embedding.provider,
      model: envConfig.embedding.model,
      base_url: envConfig.embedding.base_url,
      ...(envConfig.embedding.api_key ? { api_key: envConfig.embedding.api_key } : {})
    }
  });
  const runtime = options.runtime ?? createRuntimeTransport({
    ...(process.env.RUNTIME_SERVICE_TOKEN ? { token: process.env.RUNTIME_SERVICE_TOKEN } : {})
  });
  const runCancelRegistry = new RunCancelRegistry();
  const authService = new AuthService(metadataStore, authConfig);
  runtimeHealth = await timer.measure("runtime_health", () => probeRuntimeHealth(runtime));

  // After restart, cancel-registry is empty — reclaim queued/running rows left by dead workers.
  const reclaimedActiveRuns = await timer.measure("stale_active_run_reclaim", () =>
    reclaimOrphanedQueuedAndRunningRuns({
      metadataStore,
      runCancelRegistry,
    }),
  );
  if (reclaimedActiveRuns > 0) {
    console.log(`[startup] stale active run reclaim: canceled=${reclaimedActiveRuns}`);
  }

  startupTimings = timer.timings();
  startupTotalMs = timer.totalMs();
  serverReady = true;
  console.log(
    `[startup] createServer ready in ${startupTotalMs}ms`,
    JSON.stringify(startupTimings),
  );

  const server = createHttpServer(async (request, response) => {
    try {
      const requestUrl = new URL(request.url ?? "/", `http://${request.headers.host ?? "127.0.0.1"}`);

      if (request.method === "GET" && requestUrl.pathname === "/healthz") {
        sendJson(response, 200, createSuccessResult({ status: "ok" }));
        return;
      }

      if (request.method === "GET" && requestUrl.pathname === "/ready") {
        if (!serverReady) {
          sendJson(response, 503, createErrorResult("INTERNAL_ERROR", "Server is still starting."));
          return;
        }
        sendJson(response, 200, createSuccessResult({
          status: "ready",
          startup_ms: startupTotalMs,
          phases: startupTimings,
          control_plane: "ready",
          runtime: runtimeHealth ?? await probeRuntimeHealth(runtime)
        }));
        return;
      }

      if (request.method === "OPTIONS" && requestUrl.pathname.startsWith("/api/v1/")) {
        sendCorsPreflight(response);
        return;
      }

      if (requestUrl.pathname.startsWith("/api/v1/auth/")) {
        let identity: AuthIdentity | undefined;
        try {
          identity = resolvePasswordSessionIdentity(authService, request);
        } catch {
          identity = undefined;
        }
        if (await handleAuthApiRequest(request, response, requestUrl.pathname, {
          authService,
          cookiePath: authConfig.cookiePath,
          cookieSecure: authConfig.cookieSecure,
          ...(identity ? { identity } : {})
        })) {
          return;
        }
      }

      const authContext = resolveRequestAuth(request, authService);
      if (isUnsafeMethod(request.method)) {
        authService.validateCsrf(authContext.identity, headerString(request.headers["x-csrf-token"]));
      }
      removeLegacyBuiltinDemoDataSourceOnce(metadataStore, authContext.user.id);
      await ensureBuiltinConfigResourcesOnce(
        fileAssetService,
        metadataStore,
        authContext.user.id,
        authContext.workspaceId,
      );

      const configResponse = await handleConfigApiRequest(request, requestUrl.pathname, {
        dataGateway,
        fileAssetService,
        knowledgeService,
        metadataStore,
        runCancelRegistry,
        userId: authContext.user.id,
        workspaceId: authContext.workspaceId
      });
      if (configResponse) {
        if (Buffer.isBuffer(configResponse.body)) {
          response.writeHead(configResponse.status, {
            "Access-Control-Allow-Origin": "*",
            ...configResponse.headers
          });
          response.end(configResponse.body);
        } else {
          sendJson(response, configResponse.status, configResponse.body);
        }
        return;
      }

      if (isCopilotKitPath(requestUrl.pathname)) {
        if (request.method === "OPTIONS") {
          sendCorsPreflight(response);
          return;
        }

        await handleCopilotKitRequest({
          request,
          response,
          metadataStore,
          fileAssetService,
          memoryExtractionTimeoutMs: options.memoryExtractionTimeoutMs
            ?? envConfig.memory.completed_extraction_timeout_ms,
          runCancelRegistry,
          runtime,
          user: authContext.user,
          workspaceId: authContext.workspaceId
        });
        return;
      }

      sendJson(response, 404, createErrorResult("RESOURCE_NOT_FOUND", "Route not found."));
    } catch (error) {
      if (error instanceof Error && error.name === "AuthError") {
        sendAuthError(response, error);
        return;
      }
      const message = error instanceof Error ? error.message : "Unknown server error";

      if (!response.headersSent) {
        if (message.startsWith("UNAUTHORIZED:")) {
          sendJson(response, 401, createErrorResult("UNAUTHORIZED", message.slice("UNAUTHORIZED:".length)));
          return;
        }
        if (message.startsWith("FORBIDDEN:")) {
          sendJson(response, 403, createErrorResult("FORBIDDEN", message.slice("FORBIDDEN:".length)));
          return;
        }
        sendJson(response, 500, createErrorResult("NOT_ENABLED", message));
        return;
      }

      response.destroy(error instanceof Error ? error : new Error(message));
    }
  });

  server.on("close", () => {
    metadataStore.close();
  });

  return server;
};

type HandleCopilotKitRequestInput = {
  request: IncomingMessage;
  response: ServerResponse;
  metadataStore: MetadataStore;
  fileAssetService: LocalFileAssetService;
  memoryExtractionTimeoutMs: number;
  runCancelRegistry: RunCancelRegistry;
  runtime: RuntimeTransport;
  user: MeResponse;
  workspaceId: string;
};

const handleCopilotKitRequest = async ({
  request,
  response,
  metadataStore,
  fileAssetService,
  memoryExtractionTimeoutMs,
  runCancelRegistry,
  runtime,
  user,
  workspaceId
}: HandleCopilotKitRequestInput): Promise<void> => {
  const copilotRuntime = new CopilotRuntime({
    agents: {
      dataFoundry: new DataFoundryAgUiAgent({
        fileAssetService,
        memoryExtractionTimeoutMs,
        metadataStore,
        runCancelRegistry,
        runtime,
        user,
        workspaceId
      }) as never
    }
  });
  const endpointOptions = {
    endpoint: COPILOTKIT_PATH,
    runtime: copilotRuntime,
    serviceAdapter: new ExperimentalEmptyAdapter(),
    cors: {
      origin: "*"
    }
  } as unknown as Parameters<typeof copilotRuntimeNodeHttpEndpoint>[0];
  const endpoint = copilotRuntimeNodeHttpEndpoint(endpointOptions);
  await endpoint(request, response);
};

const isCopilotKitPath = (pathname: string): boolean =>
  pathname === COPILOTKIT_PATH || pathname.startsWith(`${COPILOTKIT_PATH}/`);

const sendCorsPreflight = (response: ServerResponse): void => {
  response.writeHead(204, {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, PATCH, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization, Idempotency-Key, If-Match, X-CSRF-Token",
    "Access-Control-Max-Age": "86400"
  });
  response.end();
};

type RequestAuthContext = {
  identity: AuthIdentity;
  user: MeResponse;
  workspaceId: string;
};

const resolveRequestAuth = (
  request: IncomingMessage,
  authService: AuthService
): RequestAuthContext => {
  const identity = resolvePasswordSessionIdentity(authService, request);
  return {
    identity,
    user: userRecordToMeResponse(identity.user),
    workspaceId: identity.workspace.id
  };
};

const headerString = (value: string | string[] | undefined): string | undefined =>
  Array.isArray(value) ? value[0] : value;

const userRecordToMeResponse = (user: UserRecord): MeResponse => ({
  id: user.id,
  ...(user.email ? { email: user.email } : {}),
  ...(user.display_name ? { display_name: user.display_name } : {})
});

const sendJson = (response: ServerResponse, statusCode: number, body: unknown): void => {
  response.writeHead(statusCode, {
    "Access-Control-Allow-Origin": "*",
    "Content-Type": "application/json; charset=utf-8"
  });
  response.end(JSON.stringify(body));
};

const BUILTIN_DEMO_DATASOURCE_ID = "api-duckdb-demo";

/** Drop previously auto-seeded builtin demo datasources; they are no longer injected by default. */
const removeLegacyBuiltinDemoDataSource = (metadataStore: MetadataStore, userId: string): void => {
  const current = metadataStore.dataSources.find({
    user_id: userId,
    datasource_id: BUILTIN_DEMO_DATASOURCE_ID
  });
  if (!current) return;
  try {
    const config = JSON.parse(current.config_json) as Record<string, unknown>;
    if (config.builtin === true && config.mode === "demo") {
      metadataStore.dataSources.delete({
        user_id: userId,
        datasource_id: BUILTIN_DEMO_DATASOURCE_ID
      });
    }
  } catch {
    // Ignore malformed legacy rows; leave non-demo datasources untouched.
  }
};

const removeLegacyBuiltinDemoDataSourceOnce = (metadataStore: MetadataStore, userId: string): void => {
  if (legacyDemoRemovedUsers.has(userId)) return;
  removeLegacyBuiltinDemoDataSource(metadataStore, userId);
  legacyDemoRemovedUsers.add(userId);
};

const BUILTIN_SKILL_SOURCES = [
  { id: "data-analysis", path: join(BUILTIN_SKILL_ROOT, "data-analysis", "SKILL.md") }
] as const;

const ensureBuiltinConfigResources = async (
  fileAssetService: LocalFileAssetService,
  metadataStore: MetadataStore,
  userId: string,
  workspaceId: string
): Promise<void> => {
  const common = { workspace_id: workspaceId, user_id: userId };
  const currentServerDefault = metadataStore.configResources.find({
    ...common,
    kind: "model-profile",
    id: "server-default"
  });
  if (isServerLlmEnvConfigured(process.env)) {
    if (!currentServerDefault) {
      metadataStore.configResources.upsert({
        ...common,
        kind: "model-profile",
        id: "server-default",
        name: "default",
        description: "Uses the server LLM environment variables.",
        payload: { provider: "server", modelName: "server", baseUrl: "server" },
        builtin: true,
        status: "untested"
      });
    } else {
      const nextStatus = serverDefaultConnectionStatus({
        currentStatus: currentServerDefault.status,
        storedFingerprint: stringRecordValue(currentServerDefault.payload, "llmEnvFingerprint"),
        env: process.env
      });
      if (nextStatus !== currentServerDefault.status) {
        metadataStore.configResources.upsert({
          ...common,
          kind: "model-profile",
          id: "server-default",
          name: currentServerDefault.name,
          ...(currentServerDefault.description ? { description: currentServerDefault.description } : {}),
          payload: currentServerDefault.payload,
          default_enabled: currentServerDefault.default_enabled,
          builtin: true,
          status: nextStatus,
          expected_revision: currentServerDefault.revision
        });
      }
    }
  } else if (currentServerDefault?.builtin) {
    metadataStore.configResources.delete({
      ...common,
      kind: "model-profile",
      id: "server-default"
    });
  }
  ensureBuiltinDtcGrowthDatasource({
    metadataStore,
    userId,
    workspaceId
  });

  for (const source of BUILTIN_SKILL_SOURCES) {
    const content = readFileSync(source.path);
    const contentSha256 = createHash("sha256").update(content).digest("hex");
    const current = metadataStore.configResources.find({ ...common, kind: "skill", id: source.id });
    const currentPackageRefId = stringRecordValue(current?.payload, "packageFileRefId");
    const currentContentSha256 = stringRecordValue(current?.payload, "builtinContentSha256");
    if (currentPackageRefId && currentContentSha256 === contentSha256 && current?.status === "valid") {
      continue;
    }
    const parsed = await parseSkillPackage({
      content,
      filename: "SKILL.md",
      mimeType: "text/markdown"
    });
    const packageRef = fileAssetService.createRef({
      user_id: userId,
      workspace_id: workspaceId,
      filename: "SKILL.md",
      content,
      declared_mime_type: "text/markdown",
      source: "skill-package",
      metadata: { builtin: true, kind: "skill-package", skill: parsed.name, version: parsed.version }
    });
    metadataStore.configResources.upsert({
      ...common,
      kind: "skill",
      id: source.id,
      name: parsed.name,
      description: parsed.description,
      payload: {
        ...buildSkillResourcePayload({
          fields: { packageSource: `builtin://${source.id}` },
          packageFileRefId: packageRef.ref.id,
          parsed
        }),
        builtinContentSha256: contentSha256,
        builtinSource: `builtin://${source.id}`
      },
      builtin: true,
      default_enabled: false,
      status: "valid"
    });
  }
  await materializeConfiguredSkillCache(fileAssetService, metadataStore, userId, workspaceId);
};

/**
 * Builtins are idempotent for a process lifetime (content changes require redeploy).
 * Memoize per user/workspace and coalesce concurrent first hits.
 */
const ensureBuiltinConfigResourcesOnce = createAsyncMemoByKey(
  ensureBuiltinConfigResources,
  (_fileAssetService, _metadataStore, userId, workspaceId) => `${userId}:${workspaceId}`,
);

const materializeConfiguredSkillCache = async (
  fileAssetService: LocalFileAssetService,
  metadataStore: MetadataStore,
  userId: string,
  workspaceId: string
): Promise<void> => {
  const skills = metadataStore.configResources.list({
    workspace_id: workspaceId,
    user_id: userId,
    kind: "skill"
  }).map(configResourceToSkillRecord)
    .filter((skill) => skill.status === "valid" && Boolean(skill.packageFileRefId));
  const signature = skills.map((skill) => `${skill.id}:${skill.revision}:${skill.packageFileRefId}`).sort().join("|");
  const cacheKey = `${userId}:${workspaceId}`;
  if (skillCacheSignatures.get(cacheKey) === signature) {
    return;
  }
  if (skills.length === 0) {
    skillCacheSignatures.set(cacheKey, signature);
    return;
  }
  const workspaceRoot = process.env.WORKSPACE_ROOT ?? join(process.env.STORAGE_ROOT_DIR ?? "storage", "workspaces");
  const skillCacheDir = resolveSkillCacheDir({
    runContext: {
      user_id: userId,
      workspace_id: workspaceId,
      session_id: "skill-cache",
      run_id: "skill-cache",
      selected_datasource_id: "",
      enabled_datasource_ids: [],
      user_input: "",
      chat_mode: "server",
      model_name: "skill-cache"
    },
    workspaceRoot
  });
  await materializeSkillPackages({
    fileAssetService,
    runDir: skillCacheDir,
    skills,
    userId,
    workspaceId
  });
  skillCacheSignatures.set(cacheKey, signature);
};

const stringRecordValue = (record: Record<string, unknown> | undefined, key: string): string | undefined => {
  const value = record?.[key];
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
};
