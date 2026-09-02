import { AbstractAgent, EventType, type BaseEvent, type RunAgentInput } from "@ag-ui/client";
import { createCustomEvent, type AgentRunContext } from "@datafoundry/agent-runtime";
import { type MeResponse } from "@datafoundry/contracts";
import { type FileAssetService } from "@datafoundry/files";
import { RunEventWriter, type MetadataStore } from "@datafoundry/metadata";
import { Observable } from "rxjs";

import {
  buildHitlSuspendBridgeEvents,
  extractInteractionResume,
  InteractionRuntimeAdapter
} from "./interaction-runtime-adapter.js";
import { persistCurrentUserMessage } from "./conversation-memory.js";
import { createRunAgentAssembly, createRunAgentContext } from "./run-agent-assembly.js";
import { RunCancelRegistry } from "./run-cancel-registry.js";
import { resolveRunConfig } from "./run-config-resolver.js";
import { RunEventPipeline } from "./run-event-pipeline.js";
import { RunFinalizer, createRunStatusDelta } from "./run-finalizer.js";
import { resolveRunIdentity } from "./run-identity-orchestrator.js";
import { extractLastUserText } from "./run-input.js";
import { createMetadataRunMemoryAssembly } from "./run-memory-assembly.js";
import { checkpointIdFromRunInput } from "./run-checkpoint-resume.js";
import { startSessionTitleTask } from "./session-title.js";
import { TaskPlanProjector } from "./task-plan-projector.js";
import { ToolCallResultBridge } from "./tool-call-result-bridge.js";
import type { RuntimeTransport } from "./runtime/types.js";
import { assistantMessageIdFromEvent } from "./protocol-run-completion.js";

export const emitEarlyRunFailure = (
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

export const persistEarlyFailedUserMessage = (input: {
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
    console.warn("[data-foundry] failed to persist early failed user message", error);
  }
};

export type DataFoundryAgUiAgentInput = {
  fileAssetService: FileAssetService;
  metadataStore: MetadataStore;
  memoryExtractionTimeoutMs: number;
  runCancelRegistry: RunCancelRegistry;
  runtime: RuntimeTransport;
  user: MeResponse;
  workspaceId: string;
};

export class DataFoundryAgUiAgent extends AbstractAgent {
  private input: DataFoundryAgUiAgentInput;

  constructor(input: DataFoundryAgUiAgentInput) {
    super({
      agentId: "dataFoundry",
      description: "DataFoundry control-plane agent backed by an external Deep Agents runtime."
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
        const normalizedRunInput = runId === runInput.runId ? runInput : { ...runInput, runId };
        const userInput = extractLastUserText(normalizedRunInput) ?? "CopilotKit AG-UI run";
        if (checkpointIdFromRunInput(normalizedRunInput)) {
          persistEarlyFailedUserMessage({
            errorMessage: "CHECKPOINT_RESUME_DISABLED",
            isResume: Boolean(interactionResume),
            metadataStore: this.input.metadataStore,
            runId,
            runInput: normalizedRunInput,
            sessionId,
            userId: this.input.user.id,
            userInput
          });
          emitEarlyRunFailure(subscriber, runId, "CHECKPOINT_RESUME_DISABLED");
          return;
        }

        let effectiveRunConfig;
        let modelProvider;
        let modelSettings;
        let runTimeoutMs;
        try {
          ({
            effectiveRunConfig,
            modelProvider,
            modelSettings,
            runTimeoutMs
          } = resolveRunConfig({
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
        const memoryAssembly = createMetadataRunMemoryAssembly({
          isResume,
          metadataStore: this.input.metadataStore,
          modelName: modelProvider.model_name,
          runId,
          runInput: normalizedRunInput,
          ...(selectedDatasourceId ? { selectedDatasourceId } : {}),
          sessionId,
          userId: this.input.user.id,
          userInput,
          evidenceRefs: effectiveRunConfig.evidenceRefs
        });
        const runContext: AgentRunContext = createRunAgentContext({
          effectiveRunConfig,
          modelProvider,
          runId,
          ...(selectedDatasourceId ? { selectedDatasourceId } : {}),
          sessionId,
          userId: this.input.user.id,
          userInput,
          workspaceId: this.input.workspaceId
        });
        const agentAssembly = createRunAgentAssembly({
          messages: memoryAssembly.conversationMessages,
          modelProvider,
          runtime: this.input.runtime
        });
        const taskPlanProjector = new TaskPlanProjector(runContext);
        const toolCallResultBridge = new ToolCallResultBridge();
        const runAbortController = new AbortController();
        const interactionRuntime = new InteractionRuntimeAdapter(
          this.input.metadataStore,
          this.input.user.id,
          sessionId,
          runId
        );
        const eventPipeline = new RunEventPipeline({
          conversationMemoryObserver: memoryAssembly.conversationMemoryObserver,
          runEventWriter,
          runId,
          sessionId,
          taskPlanProjector,
          toolCallResultBridge,
          userId: this.input.user.id,
          sink: (event) => subscriber.next(event)
        });
        const emit = (event: BaseEvent): void => {
          eventPipeline.emit(event);
        };
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

        let suspended = false;
        let resumeResolved = false;
        let finalization: Promise<void> | undefined;
        let unregisterCancel = (): void => undefined;
        let runTimeout: ReturnType<typeof setTimeout> | undefined;
        let terminalStarted = false;
        let sessionTitleStarted = false;
        let lastAssistantMessageId: string | undefined;
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
          void this.input.runtime.cancelRun(runId, reason).catch(() => undefined);
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

        const runtimeRequest = agentAssembly.buildRunRequest({
          ...(interactionResume ? { interactionResume } : {}),
          messages: agentAssembly.governedMessages,
          runId,
          sessionId,
          userId: this.input.user.id,
          workspaceId: this.input.workspaceId
        });

        try {
          for await (const event of this.input.runtime.startRun(runtimeRequest, {
            signal: runAbortController.signal
          }) as AsyncIterable<BaseEvent>) {
            if (terminalStarted) {
              break;
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
              subscriber.next({
                type: EventType.RUN_FINISHED,
                timestamp: Date.now()
              });
              break;
            }
            if (event.type === EventType.RUN_FINISHED && suspended) {
              continue;
            }
            if (event.type === EventType.RUN_FINISHED && interactionResume?.response === false) {
              terminalStarted = true;
              clearRunTimeout();
              unregisterCancel();
              finalization = finalizer.cancel({
                interactionResolvedEvent: interactionRuntime.cancel(interactionResume),
                terminalEvent: event
              });
              break;
            }
            if (event.type === EventType.RUN_FINISHED) {
              terminalStarted = true;
              clearRunTimeout();
              unregisterCancel();
              finalization = finalizer.finish({ terminalEvent: event });
              break;
            }
            if (event.type === EventType.RUN_ERROR) {
              failRun("AG-UI run error", event);
              break;
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
                active_llm_profile_id: effectiveRunConfig.activeLlmProfileId,
                requested_llm_profile_id: effectiveRunConfig.activeLlmProfileId,
                runtime_provider: "deepagents",
                workspace_id: this.input.workspaceId,
                ...(runTimeoutMs !== undefined ? { run_timeout_ms: runTimeoutMs } : {})
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
                  emit,
                  metadataStore: this.input.metadataStore,
                  model: modelProvider.kind === "openai-compatible" ? modelProvider.model : undefined,
                  modelTemperature: modelSettings?.temperature,
                  sessionId,
                  userId: this.input.user.id,
                  userInput
                });
              }
            }
          }
        } catch (error) {
          const message = error instanceof Error ? error.message : "Unknown runtime error";
          failRun(message);
        }

        if (runTimeoutMs !== undefined && !terminalStarted) {
          runTimeout = setTimeout(() => {
            runAbortController.abort(new Error(`RUN_TIMEOUT:${runTimeoutMs}`));
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
        });

        if (finalization) {
          await finalization;
        }
        if (!subscriber.closed) {
          subscriber.complete();
        }
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
};
