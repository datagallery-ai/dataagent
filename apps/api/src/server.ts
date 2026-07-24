import { AbstractAgent, EventType, type BaseEvent, type RunAgentInput } from "@ag-ui/client";
import {
  CopilotRuntime,
  ExperimentalEmptyAdapter,
  copilotRuntimeNodeHttpEndpoint
} from "@copilotkit/runtime";
import {
  CONVERSATION_WORKING_MEMORY_CONFIG,
  createTaskStateRuntime,
  createCustomEvent,
  parseAgentMemoryMode,
  resolveSkillCacheDir,
  type AgentMemoryMode,
  type TaskStateRuntime
} from "@datafoundry/agent-runtime";
import { LocalArtifactService, SessionOutputService } from "@datafoundry/artifacts";
import { type MeResponse, createEnvConfig, createErrorResult, createSuccessResult } from "@datafoundry/contracts";
import { LocalDataGateway } from "@datafoundry/data-gateway";
import { LocalFileAssetService } from "@datafoundry/files";
import { LocalKnowledgeService } from "@datafoundry/knowledge";
import {
  RunEventWriter,
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
import { Observable } from "rxjs";

import { handleConfigApiRequest } from "./config-api.js";
import { createAsyncMemoByKey, createStartupTimer } from "./async-memo.js";
import { ensureBuiltinDtcGrowthDatasource } from "./builtin-dtc-growth-datasource.js";
import { ensureBuiltinDatalinkServer } from "./builtin-datalink-server.js";
import { reclaimOrphanedQueuedAndRunningRuns } from "./stale-active-runs.js";
import { loadPasswordAuthConfig, type PasswordAuthConfig } from "./auth/config.js";
import { AuthService, type AuthIdentity } from "./auth/service.js";
import { serverDefaultConnectionStatus, isServerLlmEnvConfigured } from "./model-profile-connection-status.js";
import {
  handleAuthApiRequest,
  isUnsafeMethod,
  resolvePasswordSessionIdentity,
  sendAuthError
} from "./auth/routes.js";
import { createMetadataContextPackageRecorder } from "./context-package-recorder.js";
import { MetadataProtocolStateStore } from "./protocol-state-store.js";
import { replayPendingProtocolEvents } from "./protocol-event-recovery.js";
import { assistantMessageIdFromEvent, completeProtocolRun } from "./protocol-run-completion.js";
import { persistCurrentUserMessage } from "./conversation-memory.js";
import { resolveEvidenceReferenceContext } from "./evidence-reference-context.js";
import { createRunAgentAssembly, createRunAgentContext } from "./run-agent-assembly.js";
import { RunCheckpointProjector } from "./run-checkpoint-projector.js";
import { TraceSectionCoordinator } from "./trace-section-coordinator.js";
import { resolveCheckpointResumeSeed, type CheckpointResumeSeed } from "./run-checkpoint-resume.js";
import { resolveRunConfig } from "./run-config-resolver.js";
import { resolveRunIdentity } from "./run-identity-orchestrator.js";
import { createRunMemoryAssembly } from "./run-memory-assembly.js";
import { extractLastUserText } from "./run-input.js";
import {
  buildHitlSuspendBridgeEvents,
  extractInteractionResume,
  InteractionRuntimeAdapter
} from "./interaction-runtime-adapter.js";
import { RunCancelRegistry } from "./run-cancel-registry.js";
import { RunEventPipeline } from "./run-event-pipeline.js";
import { RunFinalizer, createRunStatusDelta } from "./run-finalizer.js";
import { startSessionTitleTask } from "./session-title.js";
import { TaskPlanProjector } from "./task-plan-projector.js";
import { ToolCallResultBridge } from "./tool-call-result-bridge.js";

const DEV_USER: MeResponse = {
  id: "dev-user",
  email: "dev@example.com",
  display_name: "Dev User"
};

const COPILOTKIT_PATH = "/api/copilotkit";
const DEFAULT_WORKSPACE_ID = "default";
const SERVER_DIR = dirname(fileURLToPath(import.meta.url));
const BUILTIN_SKILL_ROOT = join(SERVER_DIR, "../../../packages/skills/builtin");
const skillCacheSignatures = new Map<string, string>();
const legacyDemoRemovedUsers = new Set<string>();
/** Set true only after createServer finishes required init (Mastra + builtins). */
let serverReady = false;
let startupTimings: Record<string, number> = {};
let startupTotalMs = 0;

const emitEarlyRunFailure = (
  subscriber: { complete(): void; next(event: BaseEvent): void },
  runId: string,
  message: string
): void => {
  const timestamp = Date.now();
  subscriber.next({ type: EventType.RUN_STARTED, runId, timestamp });
  subscriber.next(createRunStatusDelta("failed", { errorMessage: message, runId }));
  subscriber.next({ type: EventType.RUN_ERROR, message, timestamp });
  subscriber.complete();
};

const persistEarlyFailedUserMessage = (input: {
  errorMessage: string;
  isResume: boolean;
  metadataStore: MetadataStore;
  runId: string;
  runInput: RunAgentInput;
  sessionId: string;
  userId: string;
  userInput: string;
}): void => {
  if (input.isResume || !input.userInput.trim()) {
    return;
  }
  try {
    input.metadataStore.sessions.create({
      user_id: input.userId,
      id: input.sessionId
    });
    input.metadataStore.runs.claim({
      user_id: input.userId,
      id: input.runId,
      session_id: input.sessionId,
      user_input: input.userInput,
      status: "running",
      model_name: "unresolved"
    });
    input.metadataStore.runs.updateStatus({
      user_id: input.userId,
      run_id: input.runId,
      status: "failed",
      error_message: input.errorMessage
    });
    const record = persistCurrentUserMessage({
      currentUserText: input.userInput,
      repository: input.metadataStore.conversationMessages,
      runId: input.runId,
      runInput: input.runInput,
      sessionId: input.sessionId,
      userId: input.userId
    });
    input.metadataStore.sessions.touchLastMessage({
      user_id: input.userId,
      session_id: input.sessionId,
      last_message_at: record.created_at
    });
  } catch (error) {
    // Keep the transport error visible even if best-effort history persistence fails.
    console.warn("[data-foundry] failed to persist early failed user message", error);
  }
};

export type CreateServerOptions = {
  conversationMemoryMode?: AgentMemoryMode | undefined;
  memoryExtractionTimeoutMs?: number | undefined;
  metadataStore?: MetadataStore;
  taskStateRuntime?: TaskStateRuntime;
};

export const createServer = async (options: CreateServerOptions = {}): Promise<Server> => {
  const timer = createStartupTimer();
  serverReady = false;

  const envConfig = createEnvConfig(process.env);
  const authConfig = loadPasswordAuthConfig(process.env);
  const conversationMemoryMode =
    options.conversationMemoryMode
    ?? parseAgentMemoryMode(process.env.MASTRA_CONVERSATION_MEMORY_MODE, "working-memory-readonly");
  const metadataStore = await timer.measure("metadata_store", () =>
    options.metadataStore ??
    createMetadataStore({
      database_path: process.env.METADATA_DB_PATH ?? join(envConfig.storage.root_dir, "metadata", "workbench.sqlite"),
      ...(envConfig.storage.secret_master_key ? { secret_master_key: envConfig.storage.secret_master_key } : {}),
      dev_user: {
        id: DEV_USER.id,
        email: DEV_USER.email ?? "dev@example.com",
        display_name: DEV_USER.display_name ?? "Dev User",
        dev_token: "dev-token"
      }
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
  const ownsTaskStateRuntime = options.taskStateRuntime === undefined;
  // Mastra init and builtin skill materialization are independent — overlap them.
  const taskStateRuntimePromise =
    options.taskStateRuntime
    ?? createTaskStateRuntime(
      process.env.MASTRA_STORAGE_PATH ?? join(envConfig.storage.root_dir, "mastra", "agent-state.sqlite"),
      { conversationMemoryMode }
    );
  const runCancelRegistry = new RunCancelRegistry();
  const authService = new AuthService(metadataStore, authConfig);
  ensureDevUser(metadataStore);
  removeLegacyBuiltinDemoDataSourceOnce(metadataStore, DEV_USER.id);

  const [taskStateRuntime] = await Promise.all([
    timer.measure("mastra_runtime", () => taskStateRuntimePromise),
    timer.measure("builtin_resources", () =>
      ensureBuiltinConfigResourcesOnce(fileAssetService, metadataStore, DEV_USER.id, DEFAULT_WORKSPACE_ID),
    ),
  ]);

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
        }));
        return;
      }

      if (request.method === "OPTIONS" && requestUrl.pathname.startsWith("/api/v1/")) {
        sendCorsPreflight(response);
        return;
      }

      if (isPasswordAuth(authConfig) && requestUrl.pathname.startsWith("/api/v1/auth/")) {
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

      const authContext = resolveRequestAuth(request, metadataStore, authConfig, authService);
      if (isPasswordAuth(authConfig) && isUnsafeMethod(request.method)) {
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
          dataGateway,
          artifactService,
          sessionOutputService,
          fileAssetService,
          knowledgeService,
          taskStateRuntime,
          conversationMemoryMode,
          memoryExtractionTimeoutMs: options.memoryExtractionTimeoutMs
            ?? envConfig.memory.completed_extraction_timeout_ms,
          runCancelRegistry,
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
    if (ownsTaskStateRuntime) {
      void taskStateRuntime.close();
    }
  });

  return server;
};

type HandleCopilotKitRequestInput = {
  artifactService: LocalArtifactService;
  sessionOutputService: SessionOutputService;
  conversationMemoryMode: AgentMemoryMode;
  request: IncomingMessage;
  response: ServerResponse;
  metadataStore: MetadataStore;
  dataGateway: LocalDataGateway;
  fileAssetService: LocalFileAssetService;
  knowledgeService: LocalKnowledgeService;
  memoryExtractionTimeoutMs: number;
  runCancelRegistry: RunCancelRegistry;
  taskStateRuntime: TaskStateRuntime;
  user: MeResponse;
  workspaceId: string;
};

const handleCopilotKitRequest = async ({
  request,
  response,
  metadataStore,
  dataGateway,
  artifactService,
  sessionOutputService,
  fileAssetService,
  conversationMemoryMode,
  knowledgeService,
  memoryExtractionTimeoutMs,
  runCancelRegistry,
  taskStateRuntime,
  user,
  workspaceId
}: HandleCopilotKitRequestInput): Promise<void> => {
  const runtime = new CopilotRuntime({
    agents: {
      dataFoundry: new DataFoundryAgUiAgent({
        dataGateway,
        artifactService,
        sessionOutputService,
        fileAssetService,
        conversationMemoryMode,
        knowledgeService,
        memoryExtractionTimeoutMs,
        metadataStore,
        runCancelRegistry,
        taskStateRuntime,
        user,
        workspaceId,
        workspaceRoot: process.env.WORKSPACE_ROOT ?? join(process.env.STORAGE_ROOT_DIR ?? "storage", "workspaces")
      }) as never
    }
  });
  const endpointOptions = {
    endpoint: COPILOTKIT_PATH,
    runtime,
    serviceAdapter: new ExperimentalEmptyAdapter(),
    cors: {
      origin: "*"
    }
  } as unknown as Parameters<typeof copilotRuntimeNodeHttpEndpoint>[0];
  const endpoint = copilotRuntimeNodeHttpEndpoint(endpointOptions);

  try {
    await endpoint(request, response);
  } catch (error) {
    throw error;
  }
};

type DataFoundryAgUiAgentInput = {
  artifactService: LocalArtifactService;
  sessionOutputService: SessionOutputService;
  conversationMemoryMode: AgentMemoryMode;
  dataGateway: LocalDataGateway;
  defaultDatasourceId?: string;
  fileAssetService: LocalFileAssetService;
  metadataStore: MetadataStore;
  knowledgeService: LocalKnowledgeService;
  memoryExtractionTimeoutMs: number;
  runCancelRegistry: RunCancelRegistry;
  taskStateRuntime: TaskStateRuntime;
  user: MeResponse;
  workspaceId: string;
  workspaceRoot: string;
};

class DataFoundryAgUiAgent extends AbstractAgent {
  private input: DataFoundryAgUiAgentInput;

  constructor(input: DataFoundryAgUiAgentInput) {
    super({
      agentId: "dataFoundry",
      description: "Read-only data analysis agent backed by Mastra and Data Gateway."
    });
    this.input = input;
  }

  clone(): DataFoundryAgUiAgent {
    const cloned = super.clone() as DataFoundryAgUiAgent;
    cloned.input = this.input;
    return cloned;
  }

  run(runInput: RunAgentInput): Observable<BaseEvent> {
    return new Observable<BaseEvent>((subscriber) => {
      const interactionResume = extractInteractionResume(runInput);
      const runId = interactionResume?.interrupt.runId ?? runInput.runId;
      const run = async (): Promise<void> => {
        const sessionId = runInput.threadId;
        // CopilotKit may send a fresh runId on resume; Mastra embeds runInput.runId in
        // on_interrupt payloads, so keep AG-UI identity aligned with the suspended run.
        const normalizedRunInput =
          runId === runInput.runId ? runInput : { ...runInput, runId };
        const userInput = extractLastUserText(normalizedRunInput) ?? "CopilotKit AG-UI run";
        let effectiveRunConfig;
        let mcpRuntime;
        let modelContextProfile;
        let modelProvider;
        let modelSettings;
        let reasoningModel;
        let runTimeoutMs;
        let selectedSkills;
        let skillSelection;
        try {
          ({
            effectiveRunConfig,
            mcpRuntime,
            modelContextProfile,
            modelProvider,
            modelSettings,
            reasoningModel,
            runTimeoutMs,
            selectedSkills,
            skillSelection
          } = resolveRunConfig({
            ...(this.input.defaultDatasourceId
              ? { defaultDatasourceId: this.input.defaultDatasourceId }
              : {}),
            metadataStore: this.input.metadataStore,
            runInput: normalizedRunInput,
            userId: this.input.user.id,
            userInput,
            workspaceId: this.input.workspaceId
          }));
        } catch (error) {
          const message = error instanceof Error ? error.message : String(error);
          persistEarlyFailedUserMessage({
            errorMessage: message,
            isResume: Boolean(interactionResume),
            metadataStore: this.input.metadataStore,
            runId,
            runInput: normalizedRunInput,
            sessionId,
            userId: this.input.user.id,
            userInput
          });
          emitEarlyRunFailure(subscriber, runId, message);
          return;
        }
        const runEventWriter = new RunEventWriter(this.input.metadataStore.runEvents);
        const identity = resolveRunIdentity({
          effectiveRunConfig,
          ...(interactionResume ? { interactionResume } : {}),
          metadataStore: this.input.metadataStore,
          modelName: modelProvider.model_name,
          runCancelRegistry: this.input.runCancelRegistry,
          runEventWriter,
          runInput: normalizedRunInput,
          userId: this.input.user.id,
          userInput
        });
        if (identity.kind === "replay") {
          identity.events.forEach((event) => subscriber.next(event));
          subscriber.complete();
          return;
        }
        const { isResume, selectedDatasourceId } = identity;
        let checkpointResumeSeed: CheckpointResumeSeed | undefined;
        try {
          checkpointResumeSeed = resolveCheckpointResumeSeed({
            metadataStore: this.input.metadataStore,
            runInput: normalizedRunInput,
            sessionId,
            userId: this.input.user.id
          });
        } catch (error) {
          const message = error instanceof Error ? error.message : String(error);
          persistEarlyFailedUserMessage({
            errorMessage: message,
            isResume: Boolean(interactionResume),
            metadataStore: this.input.metadataStore,
            runId,
            runInput: normalizedRunInput,
            sessionId,
            userId: this.input.user.id,
            userInput
          });
          emitEarlyRunFailure(subscriber, runId, message);
          return;
        }

        const memoryAssembly = await createRunMemoryAssembly({
          conversationMemoryMode: this.input.conversationMemoryMode,
          isResume,
          metadataStore: this.input.metadataStore,
          model: modelProvider.model,
          modelName: modelProvider.model_name,
          modelTemperature: modelSettings?.temperature,
          runId,
          runInput: normalizedRunInput,
          ...(selectedDatasourceId ? { selectedDatasourceId } : {}),
          sessionId,
          taskStateRuntime: this.input.taskStateRuntime,
          userId: this.input.user.id,
          userInput,
          evidenceRefs: effectiveRunConfig.evidenceRefs
        });
        const {
          conversationMemoryObserver,
          conversationMessages,
          longTermMemories
        } = memoryAssembly;
        const runContext = createRunAgentContext({
          effectiveRunConfig,
          modelProvider,
          runId,
          ...(selectedDatasourceId ? { selectedDatasourceId } : {}),
          sessionId,
          userId: this.input.user.id,
          userInput,
          workspaceId: this.input.workspaceId
        });
        const evidenceContext = resolveEvidenceReferenceContext({
          evidenceRefs: effectiveRunConfig.evidenceRefs,
          metadataStore: this.input.metadataStore,
          sessionId,
          userId: this.input.user.id,
          workspaceId: this.input.workspaceId
        });
        const taskPlanProjector = new TaskPlanProjector(runContext);
        const toolCallResultBridge = new ToolCallResultBridge();
        const checkpointProjector = new RunCheckpointProjector(this.input.metadataStore, this.input.user.id);
        const traceSectionCoordinator = new TraceSectionCoordinator(
          this.input.metadataStore,
          modelProvider,
          this.input.user.id
        );
        const contextPackageRecorder = createMetadataContextPackageRecorder({
          metadataStore: this.input.metadataStore,
          runId,
          sessionId,
          userId: this.input.user.id
        });
        const protocolStateStore = new MetadataProtocolStateStore(
          this.input.metadataStore,
          this.input.user.id
        );
        const runAbortController = new AbortController();
        const interactionRuntime = new InteractionRuntimeAdapter(
          this.input.metadataStore,
          this.input.user.id,
          sessionId,
          runId
        );
        const eventPipeline = new RunEventPipeline({
          checkpointProjector,
          conversationMemoryObserver,
          runEventWriter,
          runId,
          sessionId,
          taskPlanProjector,
          traceSectionCoordinator,
          toolCallResultBridge,
          userId: this.input.user.id,
          sink: (event) => subscriber.next(event)
        });
        const emit = (event: BaseEvent): void => {
          eventPipeline.emit(event);
        };
        replayPendingProtocolEvents({ runId, stateStore: protocolStateStore, emit });
        const agentAssembly = await createRunAgentAssembly({
          abortSignal: runAbortController.signal,
          contextPackageRecorder,
          contextPackageExists: (reference) => Boolean(
            this.input.metadataStore.contextPackageSnapshots.findByPackageRevision({
              user_id: this.input.user.id,
              package_id: reference.packageId,
              revision: reference.revision
            })
          ),
          dataGateway: this.input.dataGateway,
          artifactService: this.input.artifactService,
          sessionOutputService: this.input.sessionOutputService,
          effectiveRunConfig,
          ...(evidenceContext.items.length ? { evidenceContextItems: evidenceContext.items } : {}),
          fileAssetService: this.input.fileAssetService,
          emitter: { emit },
          ...(effectiveRunConfig.goal ? { goal: effectiveRunConfig.goal } : {}),
          ...(checkpointResumeSeed ? { initialContextPackage: checkpointResumeSeed.contextPackage } : {}),
          ...(interactionResume ? { interactionResume } : {}),
          knowledgeService: this.input.knowledgeService,
          longTermMemories,
          mcpRuntime,
          messages: conversationMessages,
          ...(modelContextProfile ? { modelContextProfile } : {}),
          modelProvider,
          protocolStateStore,
          ...(modelSettings ? { modelSettings } : {}),
          runContext,
          selectedSkills,
          skillSelection,
          taskStateRuntime: this.input.taskStateRuntime,
          userId: this.input.user.id,
          workspaceId: this.input.workspaceId,
          workspaceRoot: this.input.workspaceRoot
        });
        const finalizer = new RunFinalizer({
          destroyWorkspace: agentAssembly.destroyWorkspace,
          emit,
          fileAssetService: this.input.fileAssetService,
          flushCompletedMemory: (flushInput) => memoryAssembly.flushCompletedMemory(flushInput),
          flushDraftsMemory: () => {
            memoryAssembly.flushDraftsMemory();
          },
          memoryExtractionTimeoutMs: this.input.memoryExtractionTimeoutMs,
          metadataStore: this.input.metadataStore,
          runId,
          sessionId,
          userId: this.input.user.id,
          sessionDir: agentAssembly.sessionDir,
          workspaceId: this.input.workspaceId
        });
        let subscription: { unsubscribe(): void } | undefined;
        let suspended = false;
        let resumeResolved = false;
        let finalization: Promise<void> | undefined;
        let unregisterCancel = (): void => undefined;
        let runTimeout: ReturnType<typeof setTimeout> | undefined;
        let terminalStarted = false;
        let sessionTitleStarted = false;
        let lastAssistantMessageId: string | undefined;
        /** toolCallIds that already emitted TOOL_CALL_START / END in this run (HITL bridge). */
        const startedToolCallIds = new Set<string>();
        const endedToolCallIds = new Set<string>();
        const clearRunTimeout = (): void => {
          if (runTimeout) {
            clearTimeout(runTimeout);
            runTimeout = undefined;
          }
        };
        const failRun = (message: string, terminalEvent?: BaseEvent): void => {
          if (terminalStarted) {
            return;
          }
          terminalStarted = true;
          runAbortController.abort(new Error(message));
          clearRunTimeout();
          unregisterCancel();
          finalizer.fail({
            errorMessage: message,
            terminalEvent: terminalEvent ?? {
              type: EventType.RUN_ERROR,
              message,
              timestamp: Date.now()
            }
          });
        };
        const cancelRun = (reason = "RUN_CANCELLED"): void => {
          if (terminalStarted) {
            return;
          }
          terminalStarted = true;
          runAbortController.abort(new Error(reason));
          clearRunTimeout();
          unregisterCancel();
          subscription?.unsubscribe();
          finalization = finalizer.cancelRun({
            reason,
            terminalEvent: {
              type: EventType.RUN_FINISHED,
              status: "cancelled",
              timestamp: Date.now()
            } as BaseEvent
          });
          void finalization.then(() => subscriber.complete(), (error: unknown) => subscriber.error(error));
        };
        unregisterCancel = this.input.runCancelRegistry.register({
          cancel: cancelRun,
          runId,
          sessionId,
          userId: this.input.user.id
        });
        subscriber.add(() => unregisterCancel());

        if (this.input.conversationMemoryMode === "working-memory-readonly") {
          await ensureConversationWorkingMemoryThread({
            resourceId: this.input.user.id,
            taskStateRuntime: this.input.taskStateRuntime,
            threadId: sessionId
          });
        }

        subscription = agentAssembly.mastraAgent.run({
          ...normalizedRunInput,
          runId,
          messages: agentAssembly.governedMessages
        }).subscribe({
          next: (event) => {
            if (terminalStarted) {
              return;
            }
            const assistantMessageId = assistantMessageIdFromEvent(event);
            if (assistantMessageId) {
              lastAssistantMessageId = assistantMessageId;
            }
            const interactionRequested = interactionRuntime.capture(event);
            if (interactionRequested) {
              terminalStarted = true;
              clearRunTimeout();
              unregisterCancel();
              suspended = true;
              // R-018 / AG-UI verifyEvents: close the tool-call span before transport RUN_FINISHED
              // whether upstream already emitted START or Mastra skipped start/end entirely.
              const bridgeEvents = buildHitlSuspendBridgeEvents({
                interrupt: interactionRequested.interrupt,
                interactionEvent: interactionRequested.event,
                ...(event.type === EventType.CUSTOM && event.name === "on_interrupt"
                  ? { passthroughInterruptEvent: event }
                  : {}),
                state: { startedToolCallIds, endedToolCallIds }
              });
              for (const bridgeEvent of bridgeEvents) {
                if (bridgeEvent === interactionRequested.event) {
                  emit(bridgeEvent);
                  finalizer.suspend();
                  continue;
                }
                emit(bridgeEvent);
              }
              // Stream must finalize so CopilotKit can surface the interrupt UI via onRunFinalized.
              // This synthetic terminal event is transport-only; suspended runs must not replay as finished.
              subscriber.next({
                type: EventType.RUN_FINISHED,
                timestamp: Date.now()
              });
              return;
            }
            if (event.type === EventType.RUN_FINISHED && suspended) {
              return;
            }
            if (event.type === EventType.RUN_FINISHED && interactionResume?.response === false) {
              terminalStarted = true;
              clearRunTimeout();
              unregisterCancel();
              finalization = finalizer.cancel({
                interactionResolvedEvent: interactionRuntime.cancel(interactionResume),
                terminalEvent: event
              });
              return;
            }
            if (event.type === EventType.RUN_FINISHED) {
              terminalStarted = true;
              clearRunTimeout();
              unregisterCancel();
              const persistedAssistantMessage = lastAssistantMessageId
                ? undefined
                : this.input.metadataStore.conversationMessages.findLatestAssistantByRun({
                    user_id: this.input.user.id,
                    session_id: sessionId,
                    run_id: runId
                  });
              finalization = completeProtocolRun({
                finalizer,
                ...(agentAssembly.goalRuntime ? { goalRuntime: agentAssembly.goalRuntime } : {}),
                ...(lastAssistantMessageId ? { lastAssistantMessageId } : {}),
                ...(persistedAssistantMessage?.message_id
                  ? { persistedAssistantMessageId: persistedAssistantMessage.message_id }
                  : {}),
                protocol: agentAssembly.protocol,
                runId,
                terminalEvent: event
              });
              return;
            }
            if (event.type === EventType.RUN_ERROR) {
              failRun("AG-UI run error", event);
              return;
            }
            if (
              event.type === EventType.TOOL_CALL_START
              && typeof event.toolCallId === "string"
              && event.toolCallId.length > 0
            ) {
              startedToolCallIds.add(event.toolCallId);
            }
            if (
              event.type === EventType.TOOL_CALL_END
              && typeof event.toolCallId === "string"
              && event.toolCallId.length > 0
            ) {
              endedToolCallIds.add(event.toolCallId);
            }
            emit(event);

            if (event.type === EventType.RUN_STARTED) {
              agentAssembly.flushProtocolEvents();
            }

            if (
              interactionResume
              && !resumeResolved
              && event.type === EventType.TOOL_CALL_RESULT
              && event.toolCallId === interactionResume.interrupt.toolCallId
            ) {
              try {
                emit(interactionRuntime.resolve(interactionResume));
                resumeResolved = true;
              } catch (error) {
                const message = error instanceof Error ? error.message : "Interaction resume failed";
                emit({
                  type: EventType.RUN_ERROR,
                  message,
                  timestamp: Date.now()
                });
              }
            }

            if (event.type === EventType.RUN_STARTED) {
              emit(createCustomEvent("run.config.resolved", {
                active_datasource_id: effectiveRunConfig.activeDatasourceId,
                active_skill_id: effectiveRunConfig.activeSkillId,
                enabled_datasource_ids: effectiveRunConfig.enabledDatasourceIds,
                file_ids: effectiveRunConfig.fileIds,
                enabled_knowledge_ids: effectiveRunConfig.enabledKnowledgeIds,
                enabled_mcp_server_ids: effectiveRunConfig.enabledMcpServerIds,
                selected_skill_ids: selectedSkills.map((skill) => skill.id),
                skill_mode: effectiveRunConfig.skillMode,
                requested_llm_profile_id: effectiveRunConfig.activeLlmProfileId,
                active_llm_profile_id: effectiveRunConfig.activeLlmProfileId,
                workspace_id: this.input.workspaceId,
                workspace: agentAssembly.workspace,
                ...(modelContextProfile
                  ? {
                      context_window: modelContextProfile.contextWindow,
                      input_budget: Math.max(
                        modelContextProfile.contextWindow
                          - modelContextProfile.outputReserve
                          - modelContextProfile.safetyMargin,
                        0
                      )
                    }
                  : {}),
                ...(reasoningModel !== undefined ? { reasoning_model: reasoningModel } : {}),
                ...(runTimeoutMs !== undefined ? { run_timeout_ms: runTimeoutMs } : {}),
                ...(effectiveRunConfig.mentioned
                  ? {
                      mentioned: {
                        db: effectiveRunConfig.mentioned.db,
                        kb: effectiveRunConfig.mentioned.kb,
                        mcp: effectiveRunConfig.mentioned.mcp,
                        skill: effectiveRunConfig.mentioned.skill,
                        ...(effectiveRunConfig.mentioned.excluded && effectiveRunConfig.mentioned.excluded.length > 0
                          ? { excluded: effectiveRunConfig.mentioned.excluded }
                          : {})
                      }
                    }
                  : {}),
                ...((effectiveRunConfig.pinnedPaths?.length ?? 0) > 0
                  ? { pinned_paths: effectiveRunConfig.pinnedPaths }
                  : {}),
                ...(effectiveRunConfig.evidenceRefs.length > 0
                  ? {
                      evidence_refs: effectiveRunConfig.evidenceRefs,
                      evidence_resolution: evidenceContext.diagnostics
                    }
                  : {}),
                ...(effectiveRunConfig.disabledByPolicy && effectiveRunConfig.disabledByPolicy.length > 0
                  ? { disabled_by_policy: effectiveRunConfig.disabledByPolicy }
                  : {}),
                ...(effectiveRunConfig.unavailableResources && effectiveRunConfig.unavailableResources.length > 0
                  ? { unavailable_resources: effectiveRunConfig.unavailableResources }
                  : {})
              }));
              emit(createCustomEvent("skill.selection", {
                audit: skillSelection.audit,
                effective_tool_policy: skillSelection.effectiveToolPolicy,
                mode: effectiveRunConfig.skillMode,
                selected: selectedSkills.map((skill) => ({
                  id: skill.id,
                  name: skill.name,
                  revision: skill.revision,
                  tags: skill.tags
                }))
              }));
              emit({
                type: EventType.STATE_SNAPSHOT,
                snapshot: {
                  selectedDatasourceId,
                  runId,
                  runStatus: "running",
                  sessionId
                },
                timestamp: Date.now()
              });
              if (!isResume && !sessionTitleStarted) {
                sessionTitleStarted = true;
                startSessionTitleTask({
                  // Title generation is async and may finish after the agent run
                  // terminals; still forward the event while the stream is open
                  // (finalizer continues emitting after RUN_FINISHED).
                  emit,
                  metadataStore: this.input.metadataStore,
                  model: modelProvider.model,
                  modelTemperature: modelSettings?.temperature,
                  sessionId,
                  userId: this.input.user.id,
                  userInput
                });
              }
            }

          },
          error: (error: unknown) => {
            const message = error instanceof Error ? error.message : "Unknown AG-UI agent error";
            const event: BaseEvent = {
              type: EventType.RUN_ERROR,
              message,
              timestamp: Date.now()
            };
            failRun(message, event);
            subscriber.complete();
          },
          complete: () => {
            clearRunTimeout();
            unregisterCancel();
            if (finalization) {
              void finalization.then(() => subscriber.complete(), (error: unknown) => subscriber.error(error));
              return;
            }
            subscriber.complete();
          }
        });

        if (runTimeoutMs !== undefined) {
          runTimeout = setTimeout(() => {
            runAbortController.abort(new Error(`RUN_TIMEOUT:${runTimeoutMs}`));
            subscription?.unsubscribe();
            failRun(`RUN_TIMEOUT:${runTimeoutMs}`);
            subscriber.complete();
          }, runTimeoutMs);
        }

        subscriber.add(() => {
          if (!terminalStarted) {
            runAbortController.abort(new Error("RUN_SUBSCRIBER_CLOSED"));
          }
          clearRunTimeout();
          unregisterCancel();
          subscription?.unsubscribe();
        });
      };

      run().catch((error: unknown) => {
        const message = error instanceof Error ? error.message : String(error);
        persistEarlyFailedUserMessage({
          errorMessage: message,
          isResume: Boolean(interactionResume),
          metadataStore: this.input.metadataStore,
          runId,
          runInput,
          sessionId: runInput.threadId,
          userId: this.input.user.id,
          userInput: extractLastUserText(runInput) ?? "CopilotKit AG-UI run"
        });
        emitEarlyRunFailure(subscriber, runId, message);
      });
    });
  }
}

const isCopilotKitPath = (pathname: string): boolean =>
  pathname === COPILOTKIT_PATH || pathname.startsWith(`${COPILOTKIT_PATH}/`);

const sendCorsPreflight = (response: ServerResponse): void => {
  response.writeHead(204, {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, PATCH, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization, Idempotency-Key, If-Match, X-CSRF-Token, X-Dev-Token, X-Workspace-Id",
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
  metadataStore: MetadataStore,
  authConfig: PasswordAuthConfig,
  authService: AuthService
): RequestAuthContext => {
  if (isPasswordAuth(authConfig)) {
    const identity = resolvePasswordSessionIdentity(authService, request);
    return {
      identity,
      user: userRecordToMeResponse(identity.user),
      workspaceId: identity.workspace.id
    };
  }

  const token = extractAuthToken(request);
  const workspaceId = sanitizeWorkspaceId(headerString(request.headers["x-workspace-id"]));
  const devUser = metadataStore.users.getById({ user_id: DEV_USER.id });
  const devIdentity = {
    user: devUser,
    workspace: { id: workspaceId, name: workspaceId, kind: "personal" as const, owner_user_id: DEV_USER.id, created_at: "", updated_at: "" }
  };
  if (!token) {
    return { identity: devIdentity, user: DEV_USER, workspaceId };
  }
  const user = metadataStore.users.getByDevToken({ dev_token: token });
  if (!user) {
    throw new Error("UNAUTHORIZED:Invalid local auth token.");
  }
  return {
    identity: {
      user,
      workspace: {
        id: workspaceId,
        name: workspaceId,
        kind: "personal",
        owner_user_id: user.id,
        created_at: "",
        updated_at: ""
      }
    },
    user: userRecordToMeResponse(user),
    workspaceId
  };
};

const ensureConversationWorkingMemoryThread = async (input: {
  resourceId: string;
  taskStateRuntime: TaskStateRuntime;
  threadId: string;
}): Promise<void> => {
  const existing = await input.taskStateRuntime.memory.getThreadById({
    resourceId: input.resourceId,
    threadId: input.threadId
  });
  if (existing) {
    return;
  }
  await input.taskStateRuntime.memory.createThread({
    memoryConfig: CONVERSATION_WORKING_MEMORY_CONFIG,
    resourceId: input.resourceId,
    saveThread: true,
    threadId: input.threadId
  });
};

const isPasswordAuth = (config: PasswordAuthConfig): boolean => config.mode === "password";

const extractAuthToken = (request: IncomingMessage): string | undefined => {
  const authorization = headerString(request.headers.authorization);
  if (authorization?.startsWith("Bearer ")) {
    return authorization.slice("Bearer ".length).trim() || undefined;
  }
  return headerString(request.headers["x-dev-token"]);
};

const sanitizeWorkspaceId = (value: string | undefined): string => {
  const candidate = value?.trim() || DEFAULT_WORKSPACE_ID;
  if (!/^[a-zA-Z0-9._-]{1,128}$/u.test(candidate)) {
    throw new Error("UNAUTHORIZED:Invalid workspace id.");
  }
  return candidate;
};

const headerString = (value: string | string[] | undefined): string | undefined =>
  Array.isArray(value) ? value[0] : value;

const userRecordToMeResponse = (user: UserRecord): MeResponse => ({
  id: user.id,
  ...(user.email ? { email: user.email } : {}),
  ...(user.display_name ? { display_name: user.display_name } : {})
});

const ensureDevUser = (metadataStore: MetadataStore): void => {
  metadataStore.users.upsertDevUser({
    id: DEV_USER.id,
    email: DEV_USER.email ?? "dev@example.com",
    display_name: DEV_USER.display_name ?? "Dev User",
    dev_token: "dev-token"
  });
};

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
  ensureBuiltinDatalinkServer({
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
