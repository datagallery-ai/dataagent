"use client";

import {
  CopilotChat,
  CopilotChatAssistantMessage,
  CopilotChatConfigurationProvider,
  CopilotChatInput,
  CopilotChatReasoningMessage,
  CopilotChatUserMessage,
  CopilotKit,
  useAgent,
  useAgentContext,
  useAttachments,
  useCopilotChatConfiguration,
  useCopilotKit,
  useFrontendTool,
  useRenderTool,
} from "@copilotkit/react-core/v2";
import type { EvidenceRef } from "@datafoundry/contracts";
import nextDynamic from "next/dynamic";
import { Children, cloneElement, isValidElement, useCallback, createContext, useContext, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { ComponentProps, ComponentType, MouseEvent, PointerEvent as ReactPointerEvent, ReactNode } from "react";
import { z } from "zod";
import {
  buildMentionResources,
  buildRunConfig,
  buildRunForwardedProps,
  buildAgentRunStatePatch,
  createChatSession,
  createClientId,
  createWorkspaceConfigItem,
  defaultSettingsForKind,
  applyAutoTitle,
  deleteChatSession,
  deriveSnippetTitle,
  emptyPerRunFileSelection,
  sortChatSessions,
  togglePinChatSession,
  emptyPerRunSelection,
  fileMentionFromArtifact,
  fileMentionFromWorkspaceAsset,
  filterWorkspaceAssetFiles,
  getEnabledLlmItems,
  hasCapability,
  toolDisplayTitle,
  loadActiveLlmId,
  loadChatSessions,
  mergeServerChatSessions,
  normalizeLlmSettings,
  normalizeMcpSettings,
  persistActiveLlmId,
  persistChatSessions,
  isWorkspaceConfigItemValid,
  renameChatSession,
  serverSessionDtoToChatSession,
  normalizeSkillSettings,
  parseSkillPackageFile,
  prunePerRunSelection,
  removePerRunFileMention,
  removePerRunMention,
  resolveActiveLlmProfileId,
  resolveActiveDatasourceId,
  resolveSendBlockReason,
  skillSettingsFromPackage,
  SKILL_PACKAGE_LOCAL_ONLY_KEYS,
  workspaceConfigItemDraftEquals,
  togglePerRunFileMention,
  togglePerRunMention,
  toggleSessionResource,
  isSessionResourceKindLocked,
  renderableConfigFields,
  resolveConfigFieldOptions,
  isFieldPending,
  isFieldDisabled,
  isSelectOptionPending,
  normalizeKbSettings,
  normalizeLlmSettingsExtended,
  visibleConfigFields,
  workspaceConfigItemStatusBadge,
} from "./data-task-state";
import { configApi } from "../../lib/config-api/client";
import { ConfigApiError } from "../../lib/config-api";
import {
  hasMeaningfulText,
  isOrphanPreambleMergedIntoFollowingToolStep,
  mergeMessagesForStepContext,
  messageTextContent,
  reasoningMessageAbsorbedByFollowingToolStep,
  resolveToolStepThoughtContent,
} from "./assistant-thought-content";
import { createChatTextareaKeyDownHandler } from "./chat-textarea-submit";
import {
  isDisplayableToolName,
  resolveCollaborationCompletedStepLabel,
  resolveCollaborationStepLabel,
  resolveStepBadgePresentation,
  resolveStepSummaryText,
  resolveToolStepActionLabel,
} from "./step-display-label";
import { JobProgressBanner } from "./components/JobProgressBanner";
import {
  formatConfigTestError,
  formatConfigTestResult,
  type ConfigTestPresentation,
} from "./config-test-result";
import { useWorkspaceConfigApi } from "./hooks/use-workspace-config-api";
import type { DatasourceTypeDto, FileAssetRefDto, JobDto, KnowledgeDocumentDto } from "../../lib/config-api";
import type {
  CopilotChatAssistantMessageProps,
  JsonSerializable,
} from "@copilotkit/react-core/v2";
import type {
  ChatSession,
  DataArtifact,
  FileMentionResource,
  PerRunFileSelection,
  ParsedSkillPackage,
  PerRunMentionKind,
  PerRunSelection,
  SessionStartedHints,
  WorkspaceConfigItem,
  WorkspaceConfigKind,
  WorkspaceConfigStore,
} from "./data-task-state";
import {
  removeEvidenceRef,
  uniqueEvidenceRefs,
} from "./evidence";
// Heavy, first-paint-invisible panels are loaded on demand to keep them (and
// their transitive deps, notably TaskConsole + recharts) out of the /data-tasks
// first-compile module graph.
const TaskConsolePanel = nextDynamic(
  () => import("./components/task-console/TaskConsolePanel").then((m) => m.TaskConsolePanel),
  { ssr: false, loading: () => null },
);
const TaskConsoleDrawer = nextDynamic(
  () => import("./components/task-console/TaskConsoleDrawer").then((m) => m.TaskConsoleDrawer),
  { ssr: false, loading: () => null },
);
const TraceOverlay = nextDynamic(
  () => import("./components/task-console/TraceOverlay").then((m) => m.TraceOverlay),
  { ssr: false, loading: () => null },
);
const WorkspaceFileAssetsPanel = nextDynamic(
  () => import("./components/task-console/WorkspaceFileAssetsPanel").then((m) => m.WorkspaceFileAssetsPanel),
  { ssr: false, loading: () => null },
);
const DataLinkPanel = nextDynamic(
  () => import("./components/DataLinkPanel").then((m) => m.DataLinkPanel),
  { ssr: false, loading: () => null },
);
const DatasourceSchemaPreviewPopover = nextDynamic(
  () => import("./components/SchemaBrowserPanel").then((m) => m.DatasourceSchemaPreviewPopover),
  { ssr: false, loading: () => null },
);
const DatasourceExplorerPanel = nextDynamic(
  () => import("./components/DatasourceExplorerPanel").then((m) => m.DatasourceExplorerPanel),
  { ssr: false, loading: () => null },
);
const KnowledgeDocumentsPanel = nextDynamic(
  () => import("./components/KnowledgeDocumentsPanel").then((m) => m.KnowledgeDocumentsPanel),
  { ssr: false, loading: () => null },
);
const DatasourceTypeGallery = nextDynamic(
  () => import("./components/DatasourceTypeGallery").then((m) => m.DatasourceTypeGallery),
  { ssr: false, loading: () => null },
);
import { DatasourceTypeIcon } from "./components/DatasourceTypeIcon";
import { DataTaskChatInput } from "./components/chat/DataTaskChatInput";
import { QuickStartGuide } from "./components/guide/QuickStartGuide";
import {
  createChatStopHandler,
  performChatRunCancellation,
} from "./components/chat/chat-stop-handler";
import {
  CHAT_ATTACHMENT_ACCEPT,
  CHAT_ATTACHMENT_MAX_BYTES,
  buildMessageContent,
  createChatOnUpload,
  readFileAsBase64,
} from "./components/chat/chat-attachments";
import {
  createQueuedChatPrompt,
  deleteQueuedChatPrompt,
  editQueuedChatPrompt,
  isForeignSessionActiveRun,
  loadQueuedChatPrompts,
  markQueuedChatPromptInterrupting,
  parseAlreadyActiveRunId,
  persistQueuedChatPrompts,
  queuedPromptToRunInput,
  resolveQueuedSubmitMode,
  shouldShowRunningStopControl,
  takeNextQueuedChatPrompt,
  type QueuedChatPrompt,
} from "./components/chat/queued-chat-runs";
import {
  SessionBusyPrompt,
  SessionRemoteBusyBanner,
} from "./components/chat/SessionBusyPrompt";
import type { SessionActiveRunDto } from "../../lib/config-api/types";
import { scheduleChatTextareaResize } from "./components/chat/use-chat-textarea-autoresize";
import { invokeWithReportedError } from "./invoke-with-reported-error";
import { formatRunErrorMessage } from "./run-error-message";
import {
  DataTaskChatInputBindingsProvider,
  useDataTaskChatInputBindings,
} from "./components/chat/DataTaskChatInputBindingsContext";
import {
  BackendToolRuntimeProvider,
  useBackendToolPhase,
  useBackendToolResult,
} from "./backend-tool-runtime-context";
import {
  buildBackendToolPhaseMap,
  buildBackendToolResultMap,
  resolveToolDisplayStatus,
  toolDisplayStatusLabel,
  toolPendingHint,
  toolResultLooksLikeError,
  type CopilotToolStatus,
  type ToolDisplayStatus,
} from "./tool-call-display";
import {
  LiveRunEventSubscriber,
  LiveRunProvider,
  useConversationRestoreGate,
  useLiveRun,
} from "./use-data-foundry-run";
import {
  DataTaskIdentityProvider,
  DataTaskUserBar,
  useDataTaskIdentity,
} from "./data-task-identity";
import type { LiveRun } from "./live-run-state";
import {
  buildProcessToolGroups,
  processToolGroupsEqual,
  type ProcessToolGroup,
} from "./process-tool-groups";
import {
  buildCollapsedStepSummary,
  buildStepToolSummaries,
  buildToolChipSummaries,
  stepElapsedLabel,
  type StepToolStatus,
  type StepToolSummaryInput,
  type ToolChipSummary,
} from "./step-tool-summary";
import { ToolFormattedParams, ToolFailureResult, ToolFormattedResult } from "./tool-result-format";
import { parseSchemaToolResult } from "./tool-result-normalize";
import {
  chatPaneClassName,
  getLeftPanelWidth,
  getWorkspaceGridTemplateColumns,
  LEFT_PANEL_MAX_WIDTH,
  LEFT_PANEL_MIN_WIDTH,
  maxDockableRightPanelWidth,
  resolveSidebarExpandPreferences,
  RIGHT_PANEL_MIN_WIDTH,
} from "./workspace-layout";
import { usePanelResize } from "./hooks/use-panel-resize";
import { useLeftPanelResize } from "./hooks/use-left-panel-resize";
import { useChatColumnWidth } from "./hooks/use-chat-column-width";
import { useWorkspaceResponsiveLayout } from "./hooks/use-workspace-responsive-layout";
import { useWorkspaceViewportWidth } from "./hooks/use-workspace-viewport-width";
import { PanelResizeHandle } from "./components/layout/PanelResizeHandle";
import {
  ChatInitializingState,
  DataTaskWelcomeScreen,
} from "./components/chat/DataTaskWelcome";
import { SessionHeaderResourceChips } from "./components/chat/SessionResourceSummary";
import { SessionConversationRestore } from "./components/chat/SessionConversationRestore";
import { SessionConversationScrollRestore } from "./components/chat/SessionConversationScrollRestore";
import { SessionArtifactsRestore } from "./components/chat/SessionArtifactsRestore";
import { CollaborationInterruptHandler } from "./components/chat/CollaborationInterruptHandler";
import { RestoredInterruptHandler } from "./components/chat/RestoredInterruptHandler";
import { CollaborationPendingInterruptSlot } from "./components/chat/CollaborationPendingInterruptSlot";
import { usePendingCollaborationInterrupt } from "./components/chat/pending-collaboration-interrupt";
import {
  CollaborationChoiceBubble,
  CollaborationResponseBridge,
  CollaborationResponsesProvider,
  useThreadCollaborationResponsesForChat,
} from "./components/chat/collaboration-responses";
import { RichMarkdown } from "./components/task-console/ArtifactMarkdownPreview";
import {
  findPendingCollaborationToolCall,
  messageHostsPendingCollaborationSlot,
  shouldShowCollaborationRecapOnMessage,
  shouldShowPendingInterruptOnMessage,
} from "./collaboration-recap";
import {
  AgentMessageRenderSync,
  useAgentMessageRenderGeneration,
  useAgentMessageRenderSnapshot,
} from "./agent-message-render-sync";
import {
  resolveAssistantLiveToolCalls,
  resolveAssistantToolStepNumber,
  resolveStepAssistantFlags,
  shouldHideProcessStepForTimelineCollapse,
} from "./step-assistant-state";
import { btnSecondaryClass, panelTitleClass, sectionLabelClass } from "./ui-tokens";
import {
  buildDatasourceSettingsForType,
  summarizeDatasourceConnection,
} from "./datasource-metadata";
import { findCopilotChatScrollContainer } from "./chat-scroll";
import {
  setConversationScrollIntent,
  useChatAutoScrollMode,
} from "./conversation-branch-scroll";
import { useConversationBranchSnapshot } from "./conversation-branch-store";
import { resolveUserMessageBranchState } from "./conversation-branches";
import {
  getCollapsedWorkspaceRailCopy,
  getCollapsedWorkspacePreviewClassNames,
  getSessionListItemIconSlots,
  getWorkspaceResourceNavGroups,
  type WorkspaceResourceNavAction,
  type WorkspaceResourceNavGroup,
} from "./session-pane-ui";
import { LocaleProvider, useT } from "../../i18n/locale-context";
import {
  translateConfigField,
  translateConfigFieldOptions,
} from "../../i18n/config-labels";
import {
  getAgentRuntimeUrl,
  getBackendCapabilities,
  isResourcePanelSupported,
} from "../../lib/config-api";

const runtimeAgentId = "dataFoundry";
const sessionAgentIdPrefix = `${runtimeAgentId}:session:`;
const runtimeUrl = getAgentRuntimeUrl();

type CopilotSessionAgentRegistry = {
  getAgent: (agentId: string) => ({ threadId?: string } & Record<string, unknown>) | undefined;
  registerProxiedAgent: (params: { agentId: string; runtimeAgentId: string }) => unknown;
};

function dataTaskSessionAgentId(threadId: string): string {
  return `${sessionAgentIdPrefix}${encodeURIComponent(threadId)}`;
}

function threadIdFromDataTaskSessionAgentId(agentId: unknown): string | null {
  if (typeof agentId !== "string" || !agentId.startsWith(sessionAgentIdPrefix)) {
    return null;
  }
  try {
    return decodeURIComponent(agentId.slice(sessionAgentIdPrefix.length));
  } catch {
    return null;
  }
}

function reportDataFoundryRunError(
  error: unknown,
  threadId?: string | null,
  options?: { source?: "client" | "runtime" },
): void {
  const message = error instanceof Error ? error.message : String(error);
  if (typeof window === "undefined") {
    return;
  }
  window.dispatchEvent(
    new CustomEvent("datafoundry-run-error", {
      detail: {
        message,
        threadId: threadId ?? null,
        source: options?.source ?? "runtime",
      },
    }),
  );
}

function useSessionAgentRegistry(threadIds: readonly string[]): boolean {
  const { copilotkit } = useCopilotKit();
  const threadKey = threadIds.join("\n");
  const [generation, setGeneration] = useState(0);

  useLayoutEffect(() => {
    const registry = copilotkit as unknown as CopilotSessionAgentRegistry;
    let registered = false;
    for (const threadId of threadIds) {
      const localAgentId = dataTaskSessionAgentId(threadId);
      const existing = registry.getAgent(localAgentId);
      if (existing) {
        existing.threadId = threadId;
        continue;
      }
      try {
        registry.registerProxiedAgent({ agentId: localAgentId, runtimeAgentId });
        const agent = registry.getAgent(localAgentId);
        if (agent) {
          agent.threadId = threadId;
        }
        registered = true;
      } catch (error) {
        if (!registry.getAgent(localAgentId)) {
          console.warn("[data-tasks] failed to register session agent", error);
        }
      }
    }
    if (registered) {
      setGeneration((current) => current + 1);
    }
  }, [copilotkit, threadKey]);

  return useMemo(() => {
    const registry = copilotkit as unknown as CopilotSessionAgentRegistry;
    return threadIds.every((threadId) => Boolean(registry.getAgent(dataTaskSessionAgentId(threadId))));
  }, [copilotkit, generation, threadKey]);
}

export type TaskSelection =
  | { type: "artifact"; id: string }
  | { type: "toolGroup"; id: string }
  | { type: "action"; id: string }
  | null;

export type WorkspaceConfigPanelKey = "db" | "kb" | "mcp" | "skill" | "llm";

const NEW_CONFIG_ITEM_ID = "__new__";

const DATASOURCE_CARD_GRID_CLASS =
  "grid gap-3 [grid-template-columns:repeat(auto-fill,minmax(min(100%,360px),360px))]";

const CONFIG_CARD_PRIMARY_BUTTON_CLASS =
  "cursor-pointer rounded-lg bg-slate-950 px-3 py-1.5 text-xs font-semibold text-white transition-colors duration-200 hover:bg-slate-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-300";

const CONFIG_DETAIL_MAX_WIDTH_CLASS = "max-w-3xl";

async function uploadChatDataFile(
  file: File,
  sessionId?: string | null,
): Promise<{ path: string; mimeType: string; size: number }> {
  return configApi.uploadChatFile(file, sessionId);
}

type PendingBusySubmit = {
  text: string;
  attachments: ReturnType<
    ReturnType<typeof useAttachments>["consumeAttachments"]
  >;
  forwardedProps: ReturnType<
    ReturnType<typeof useDataTaskChatInputBindings>["getRunForwardedProps"]
  >;
};

type SessionBusyDialogState = {
  activeRun: SessionActiveRunDto;
  pending: PendingBusySubmit;
};

function StableDataTaskChatInput({
  inputProps,
}: {
  inputProps: ComponentProps<typeof DataTaskChatInput>;
}) {
  const t = useT();
  const bindings = useDataTaskChatInputBindings();
  const { agent } = useAgent({ agentId: bindings.agentId });
  const { copilotkit } = useCopilotKit();
  const [queuedPrompts, setQueuedPrompts] = useState<QueuedChatPrompt[]>([]);
  const [draftText, setDraftText] = useState("");
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [remoteActiveRun, setRemoteActiveRun] = useState<SessionActiveRunDto | null>(null);
  const [sessionBusyDialog, setSessionBusyDialog] = useState<SessionBusyDialogState | null>(null);
  const [busyAction, setBusyAction] = useState<"idle" | "waiting" | "interrupting">("idle");
  const awaitingQueuedRunCompletionRef = useRef(false);
  const queuedRunSawActiveRef = useRef(false);

  const capabilities = useCallback(
    () => {
      if (!bindings.capabilitiesReady) {
        return { imageInput: false, fileUpload: false };
      }
      const caps = getBackendCapabilities();
      return {
        imageInput: caps["chat.imageInput"],
        fileUpload: caps["chat.fileUpload"],
      };
    },
    [bindings.capabilitiesReady],
  );
  const onUpload = useMemo(
    () =>
      createChatOnUpload({
        capabilities,
        readBase64: (file) => readFileAsBase64(file),
        uploadDataFile: (file) => uploadChatDataFile(file, bindings.activeThreadId),
      }),
    [bindings.activeThreadId, capabilities],
  );

  const attachmentsApi = useAttachments({
    config: {
      enabled: true,
      accept: CHAT_ATTACHMENT_ACCEPT,
      maxSize: CHAT_ATTACHMENT_MAX_BYTES,
      onUpload: onUpload as never,
      onUploadFailed: ({ message }) => {
        const error = new Error(message);
        setSubmitError(formatRunErrorMessage(message));
        reportDataFoundryRunError(error, bindings.activeThreadId, { source: "client" });
      },
    },
  });

  const reportRunError = useCallback((error: unknown) => {
    awaitingQueuedRunCompletionRef.current = false;
    queuedRunSawActiveRef.current = false;
    const message = error instanceof Error ? error.message : String(error);
    setSubmitError(formatRunErrorMessage(message));
    reportDataFoundryRunError(error, bindings.activeThreadId, { source: "client" });
  }, [bindings.activeThreadId]);

  const fetchForeignActiveRun = useCallback(async (): Promise<SessionActiveRunDto | null> => {
    const threadId = bindings.activeThreadId;
    if (!threadId) return null;
    const localRunActive =
      Boolean(agent?.isRunning) ||
      bindings.liveRunStatus === "running" ||
      bindings.liveRunStatus === "suspended";
    try {
      const response = await configApi.listSessions({ limit: 50 });
      const session = response.sessions.find(
        (item) => item.id === threadId || item.threadId === threadId,
      );
      const activeRun = session?.activeRun ?? null;
      if (isForeignSessionActiveRun(activeRun, bindings.liveRunRunId, localRunActive)) {
        return activeRun;
      }
      return null;
    } catch {
      return null;
    }
  }, [agent?.isRunning, bindings.activeThreadId, bindings.liveRunRunId, bindings.liveRunStatus]);

  const clearPerRunSelections = useCallback(() => {
    bindings.onClearPerRunMentions();
    bindings.onClearPerRunFileMentions();
    bindings.onClearEvidenceRefs();
    requestAnimationFrame(scheduleChatTextareaResize);
  }, [bindings]);

  const unlockOrReportActiveRunError = useCallback(
    async (error: unknown, pending?: PendingBusySubmit) => {
      const message = error instanceof Error ? error.message : String(error);
      const blockingRunId = parseAlreadyActiveRunId(message);
      if (!blockingRunId) {
        reportRunError(error);
        return;
      }

      const foreign = await fetchForeignActiveRun();
      if (foreign) {
        setRemoteActiveRun(foreign);
        if (pending) {
          setSessionBusyDialog({
            activeRun: foreign,
            pending,
          });
          setBusyAction("idle");
        }
        return;
      }

      // Metadata can retain queued/running/suspended after the worker died.
      // Cancel may 404 if the server already reclaimed the orphan — still retry once.
      try {
        await configApi.cancelRun(blockingRunId, "stale-active-run-unlock");
      } catch {
        // Ignore cancel failures; retry below if the lock is already gone.
      }
      setRemoteActiveRun(null);
      if (pending && agent) {
        // Message was already appended by the failed dispatch; only retry the run.
        agent.setState(buildAgentRunStatePatch(pending.forwardedProps, agent.state));
        void copilotkit
          .runAgent({ agent, forwardedProps: pending.forwardedProps })
          .catch(reportRunError);
        clearPerRunSelections();
        return;
      }
      reportRunError(error);
    },
    [agent, clearPerRunSelections, copilotkit, fetchForeignActiveRun, reportRunError],
  );

  const dispatchRun = useCallback(
    ({
      text,
      attachments,
      forwardedProps,
    }: {
      text: string;
      attachments: ReturnType<typeof attachmentsApi.consumeAttachments>;
      forwardedProps: ReturnType<typeof bindings.getRunForwardedProps>;
    }) => {
      const pending: PendingBusySubmit = { text, attachments, forwardedProps };
      invokeWithReportedError(() => {
        if (!agent) return;
        bindings.onUserMessageSubmitted(text);
        agent.setState(buildAgentRunStatePatch(forwardedProps, agent.state));
        if (attachments.length > 0) {
          const content = buildMessageContent(text, attachments);
          agent.addMessage({ id: createClientId("msg"), role: "user", content });
        } else {
          agent.addMessage({ id: createClientId("msg"), role: "user", content: text });
        }
        void copilotkit
          .runAgent({ agent, forwardedProps })
          .catch((error) => {
            void unlockOrReportActiveRunError(error, pending);
          });
      }, reportRunError);
    },
    [agent, bindings, copilotkit, reportRunError, unlockOrReportActiveRunError],
  );

  useEffect(() => {
    if (!bindings.stopActiveChatRunRef) {
      return;
    }
    bindings.stopActiveChatRunRef.current = inputProps.onStop;
  }, [bindings.stopActiveChatRunRef, inputProps.onStop]);

  useEffect(() => {
    const threadId = bindings.activeThreadId;
    setQueuedPrompts(loadQueuedChatPrompts(threadId));
    setSessionBusyDialog(null);
    setBusyAction("idle");
    setRemoteActiveRun(null);
    awaitingQueuedRunCompletionRef.current = false;
    queuedRunSawActiveRef.current = false;
  }, [bindings.activeThreadId]);

  useEffect(() => {
    persistQueuedChatPrompts(bindings.activeThreadId, queuedPrompts);
  }, [bindings.activeThreadId, queuedPrompts]);

  useEffect(() => {
    const threadId = bindings.activeThreadId;
    if (!threadId || !bindings.capabilitiesReady) {
      setRemoteActiveRun(null);
      return;
    }
    let cancelled = false;
    const refresh = async () => {
      const foreign = await fetchForeignActiveRun();
      if (!cancelled) {
        setRemoteActiveRun(foreign);
      }
    };
    void refresh();
    const timer = window.setInterval(() => {
      void refresh();
    }, 3000);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [
    bindings.activeThreadId,
    bindings.capabilitiesReady,
    bindings.liveRunRunId,
    fetchForeignActiveRun,
    queuedPrompts.length,
  ]);

  const handleSubmitMessage = (value: string) => {
    setDraftText("");
    setSubmitError(null);
    invokeWithReportedError(() => {
      const forwardedProps = bindings.getRunForwardedProps();
      if (agent) {
        const blockReason = resolveSendBlockReason(
          bindings.workspaceConfig,
          bindings.activeLlmId,
          forwardedProps.datasourceId,
          t,
        );
        if (blockReason) {
          reportRunError(new Error(blockReason));
          return;
        }
      }
      const ready = attachmentsApi.consumeAttachments();
      if (agent) {
        const submitMode = resolveQueuedSubmitMode({
          agentIsRunning: Boolean(agent.isRunning),
          liveRunStatus: bindings.liveRunStatus,
        });
        if (submitMode === "queue") {
          setQueuedPrompts((current) => [
            ...current,
            createQueuedChatPrompt({
              id: createClientId("queue"),
              text: value,
              attachments: ready,
              forwardedProps,
              createdAt: Date.now(),
            }),
          ]);
          clearPerRunSelections();
          return;
        }

        void (async () => {
          const foreign = remoteActiveRun ?? (await fetchForeignActiveRun());
          if (foreign) {
            setRemoteActiveRun(foreign);
            setSessionBusyDialog({
              activeRun: foreign,
              pending: { text: value, attachments: ready, forwardedProps },
            });
            setBusyAction("idle");
            return;
          }
          dispatchRun({ text: value, attachments: ready, forwardedProps });
          clearPerRunSelections();
        })().catch(reportRunError);
        return;
      }
      inputProps.onSubmitMessage?.(value);
      clearPerRunSelections();
    }, reportRunError);
  };

  const handleBusyWait = useCallback(() => {
    if (!sessionBusyDialog) return;
    setBusyAction("waiting");
    setQueuedPrompts((current) => [
      ...current,
      createQueuedChatPrompt({
        id: createClientId("queue"),
        text: sessionBusyDialog.pending.text,
        attachments: sessionBusyDialog.pending.attachments,
        forwardedProps: sessionBusyDialog.pending.forwardedProps,
        createdAt: Date.now(),
      }),
    ]);
    setRemoteActiveRun(sessionBusyDialog.activeRun);
    setSessionBusyDialog(null);
    setBusyAction("idle");
    clearPerRunSelections();
  }, [clearPerRunSelections, sessionBusyDialog]);

  const handleBusyInterrupt = useCallback(() => {
    if (!sessionBusyDialog) return;
    const { activeRun, pending } = sessionBusyDialog;
    setBusyAction("interrupting");
    void (async () => {
      try {
        await configApi.cancelRun(activeRun.activeRunId, "user-requested-takeover");
        setRemoteActiveRun(null);
        setSessionBusyDialog(null);
        setBusyAction("idle");
        dispatchRun(pending);
        clearPerRunSelections();
      } catch (error) {
        setBusyAction("idle");
        reportRunError(error);
      }
    })();
  }, [clearPerRunSelections, dispatchRun, reportRunError, sessionBusyDialog]);

  const handleBusyDismiss = useCallback(() => {
    if (!sessionBusyDialog) return;
    setDraftText(sessionBusyDialog.pending.text);
    setSessionBusyDialog(null);
    setBusyAction("idle");
  }, [sessionBusyDialog]);

  const handleInputChange = useCallback(
    (value: string) => {
      setDraftText(value);
      if (submitError) setSubmitError(null);
      inputProps.onChange?.(value);
    },
    [inputProps, submitError],
  );
  const handleStop = useMemo(
    () =>
      createChatStopHandler({
        onCancelRun: bindings.onCancelRun,
        onStopFrontend: inputProps.onStop,
      }),
    [bindings.onCancelRun, inputProps.onStop],
  );

  useEffect(() => {
    if (!agent || queuedPrompts.length === 0) {
      return;
    }
    if (remoteActiveRun) {
      return;
    }
    const activeRun =
      Boolean(agent.isRunning) ||
      bindings.liveRunStatus === "running" ||
      bindings.liveRunStatus === "suspended";
    if (awaitingQueuedRunCompletionRef.current) {
      if (activeRun) {
        queuedRunSawActiveRef.current = true;
        return;
      }
      if (!queuedRunSawActiveRef.current) {
        return;
      }
      awaitingQueuedRunCompletionRef.current = false;
      queuedRunSawActiveRef.current = false;
    }
    if (activeRun) {
      return;
    }

    const next = takeNextQueuedChatPrompt(queuedPrompts);
    if (!next.prompt) return;
    awaitingQueuedRunCompletionRef.current = true;
    queuedRunSawActiveRef.current = false;
    setQueuedPrompts(next.queue);
    const runInput = queuedPromptToRunInput(next.prompt);
    dispatchRun(runInput);
  }, [agent, bindings.liveRunStatus, dispatchRun, queuedPrompts, remoteActiveRun]);

  const handleEditQueuedPrompt = useCallback((id: string, text: string) => {
    setQueuedPrompts((current) => editQueuedChatPrompt(current, id, text));
  }, []);

  const handleDeleteQueuedPrompt = useCallback((id: string) => {
    setQueuedPrompts((current) => deleteQueuedChatPrompt(current, id));
  }, []);

  const handleSendQueuedPromptNow = useCallback(
    (id: string) => {
      setQueuedPrompts((current) => markQueuedChatPromptInterrupting(current, id));
      void performChatRunCancellation({
        onCancelRun: bindings.onCancelRun,
        onStopFrontend: inputProps.onStop,
      });
    },
    [bindings.onCancelRun, inputProps.onStop],
  );

  const showStopControl = shouldShowRunningStopControl({
    agentIsRunning: Boolean(agent?.isRunning),
    liveRunStatus: bindings.liveRunStatus,
    draftText,
  });

  const sessionLockSlot = sessionBusyDialog ? (
    <SessionBusyPrompt
      activeRun={sessionBusyDialog.activeRun}
      busyAction={busyAction}
      onWait={handleBusyWait}
      onInterrupt={handleBusyInterrupt}
      onDismiss={handleBusyDismiss}
    />
  ) : remoteActiveRun && !showStopControl ? (
    <SessionRemoteBusyBanner activeRun={remoteActiveRun} />
  ) : null;

  return (
    <DataTaskChatInput
      {...inputProps}
      {...bindings}
      value={draftText}
      attachmentsApi={attachmentsApi}
      onChange={handleInputChange}
      onSubmitMessage={handleSubmitMessage}
      onStop={handleStop}
      isRunning={showStopControl}
      queuedPrompts={queuedPrompts}
      onEditQueuedPrompt={handleEditQueuedPrompt}
      onDeleteQueuedPrompt={handleDeleteQueuedPrompt}
      onSendQueuedPromptNow={handleSendQueuedPromptNow}
      submitError={submitError ?? bindings.liveRunErrorMessage}
      showDisclaimer={false}
      sessionLockSlot={sessionLockSlot}
    />
  );
}

// Credentials must never leave the browser through the AG-UI protocol.
// REST write paths submit credentials once; reads expose hasSecret only.
function activeLlmProfile(
  workspaceConfig: WorkspaceConfigStore,
  activeLlmId: string | null,
) {
  const enabled = getEnabledLlmItems(workspaceConfig);
  const item =
    enabled.find((entry) => entry.id === activeLlmId) ?? enabled[0] ?? null;
  if (!item) return null;
  const { apiKey, ...rest } = normalizeLlmSettings(item.settings);
  return {
    id: item.id,
    name: item.name,
    builtin: !!item.builtin,
    hasApiKey: Boolean(item.hasSecret) || apiKey.trim().length > 0,
    ...rest,
  };
}

function enabledSkillIds(workspaceConfig: WorkspaceConfigStore): string[] {
  return workspaceConfig.skill.map((item) => item.id);
}

const SECRET_SETTING_KEYS = [
  "apiKey",
  "api_key",
  "token",
  "secret",
  "password",
  "credentialsJson",
];

/**
 * Strips credential-like fields from every config item before the store is
 * embedded into AG-UI context. Secrets stay server-side via secretRef.
 */
function sanitizeWorkspaceConfig(
  workspaceConfig: WorkspaceConfigStore,
): WorkspaceConfigStore {
  const sanitizeItems = (
    items: WorkspaceConfigItem[],
    kind: WorkspaceConfigKind,
  ): WorkspaceConfigItem[] =>
    items.map((item) => {
      if (!item.settings) return item;
      const cleaned: Record<string, string> = {};
      for (const [key, value] of Object.entries(item.settings)) {
        if (SECRET_SETTING_KEYS.includes(key)) continue;
        if (
          kind === "skill" &&
          SKILL_PACKAGE_LOCAL_ONLY_KEYS.includes(
            key as (typeof SKILL_PACKAGE_LOCAL_ONLY_KEYS)[number],
          )
        ) {
          continue;
        }
        cleaned[key] = value;
      }
      if (kind === "skill") {
        cleaned.hasPackageContent = item.settings.packageContent?.trim()
          ? "true"
          : "false";
      }
      return { ...item, settings: cleaned };
    });
  return {
    db: sanitizeItems(workspaceConfig.db, "db"),
    kb: sanitizeItems(workspaceConfig.kb, "kb"),
    mcp: sanitizeItems(workspaceConfig.mcp, "mcp"),
    llm: sanitizeItems(workspaceConfig.llm, "llm"),
    skill: sanitizeItems(workspaceConfig.skill, "skill"),
  };
}

export default function DataTasksApp() {
  return (
    <LocaleProvider>
      <DataTaskIdentityProvider>
        <DataTasksCopilotShell />
      </DataTaskIdentityProvider>
    </LocaleProvider>
  );
}

function DataTasksCopilotShell() {
  const [copilotProperties, setCopilotProperties] = useState<Record<string, unknown>>(
    {},
  );
  const { authHeaders, scopeKey } = useDataTaskIdentity();

  useEffect(() => {
    setCopilotProperties({});
  }, [scopeKey]);

  return (
    <CopilotKit
      key={scopeKey}
      runtimeUrl={runtimeUrl}
      agent={runtimeAgentId}
      headers={() => authHeaders}
      useSingleEndpoint
      showDevConsole={false}
      enableInspector={false}
      properties={copilotProperties}
      onError={(event) => {
        const contextAgentId = (event as { context?: { agentId?: unknown } }).context?.agentId;
        const message =
          event instanceof Error
            ? event.message
            : String((event as { error?: unknown }).error ?? event);
        if (message.includes("Run ended without emitting a terminal event")) {
          return;
        }
        if (
          message.includes("data-workspace-metadata") &&
          message.includes("missing payload")
        ) {
          return;
        }
        console.error("[data-tasks]", event);
        window.dispatchEvent(
          new CustomEvent("datafoundry-run-error", {
            detail: {
              message,
              threadId: threadIdFromDataTaskSessionAgentId(contextAgentId),
            },
          }),
        );
      }}
    >
      <CollaborationResponsesProvider>
        <LiveRunProvider>
          <DataTaskWorkspace
            key={scopeKey}
            identityScopeKey={scopeKey}
            onCopilotPropertiesChange={setCopilotProperties}
          />
        </LiveRunProvider>
      </CollaborationResponsesProvider>
    </CopilotKit>
  );
}

function DataTaskWorkspace({
  identityScopeKey,
  onCopilotPropertiesChange,
}: {
  identityScopeKey: string;
  onCopilotPropertiesChange: (properties: Record<string, unknown>) => void;
}) {
  const t = useT();
  const defaultSessionTitle = t("session.defaultTitle");
  const {
    workspaceConfig,
    runDefaults,
    datasourceTypes,
    loading: workspaceLoading,
    capabilitiesReady,
    error: workspaceError,
    createItem,
    updateItem,
    deleteItem,
    testItem,
    introspectDatasource,
    reindexKnowledgeBase,
    uploadKnowledgeFile,
    listKnowledgeFiles,
    deleteKnowledgeFile,
    reindexKnowledgeFile,
    replaceSkillPackage,
    validateSkill,
    pollJob,
    cancelJob,
  } = useWorkspaceConfigApi();
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [selection, setSelection] = useState<TaskSelection>(null);
  const [toolGroups, setToolGroups] = useState<ProcessToolGroup[]>([]);
  const [artifactFocusId, setArtifactFocusId] = useState<string | null>(null);
  const [isTraceOpen, setIsTraceOpen] = useState(false);
  const [isConsoleDrawerOpen, setIsConsoleDrawerOpen] = useState(false);
  const [userSidebarCollapsed, setUserSidebarCollapsed] = useState(false);
  const [userRightPanelOpen, setUserRightPanelOpen] = useState(false);
  // Sessions where the user explicitly closed the task console; suppresses the
  // "auto-open on first question" behavior for that session only.
  const [rightPanelDismissedSessions, setRightPanelDismissedSessions] = useState<
    Set<string>
  >(() => new Set());
  const [configPanel, setConfigPanel] = useState<WorkspaceConfigPanelKey | null>(
    null,
  );
  const [workspaceFilesPanelOpen, setWorkspaceFilesPanelOpen] = useState(false);
  const [dataLinkPanelOpen, setDataLinkPanelOpen] = useState(false);
  const [workspaceFileAssets, setWorkspaceFileAssets] = useState<FileAssetRefDto[]>([]);
  const [promotedArtifactIds, setPromotedArtifactIds] = useState<Set<string>>(
    () => new Set(),
  );
  const [activeLlmId, setActiveLlmId] = useState<string | null>(null);
  const [activeJob, setActiveJob] = useState<JobDto | null>(null);
  const [configActionError, setConfigActionError] = useState<string | null>(null);
  const [sessionSyncError, setSessionSyncError] = useState<string | null>(null);
  const [runCancelBusy, setRunCancelBusy] = useState(false);
  // Layer-2 per-run override (data-tasks-workbench-design.md): `@`-selected capabilities for the next
  // run only. Cleared after each send so it never mutates workspace defaults.
  const [perRunSelection, setPerRunSelection] = useState<PerRunSelection>(
    emptyPerRunSelection,
  );
  const [perRunFiles, setPerRunFiles] = useState<PerRunFileSelection>(
    emptyPerRunFileSelection,
  );
  const [selectedEvidenceRefs, setSelectedEvidenceRefs] = useState<EvidenceRef[]>([]);
  const [draftPromptRequest, setDraftPromptRequest] = useState<{
    id: number;
    text: string;
  } | null>(null);
  const consumeDraftPromptRequest = useCallback((id: number) => {
    setDraftPromptRequest((current) => (current?.id === id ? null : current));
  }, []);
  const clearDraftPromptRequest = useCallback(() => {
    setDraftPromptRequest(null);
  }, []);
  const [pendingBranchRun, setPendingBranchRun] = useState<PendingBranchRun | null>(null);
  const [pendingCheckpointResume, setPendingCheckpointResume] =
    useState<PendingCheckpointResume | null>(null);
  const stopActiveChatRunRef = useRef<(() => void) | undefined>(undefined);
  const [chatColumnWidth, setChatColumnWidth] = useState(1280);
  const [layoutHydrated, setLayoutHydrated] = useState(false);

  useEffect(() => {
    setLayoutHydrated(true);
  }, []);

  const togglePerRunMentionItem = useCallback(
    (kind: PerRunMentionKind, id: string) => {
      setPerRunSelection((current) => togglePerRunMention(current, kind, id));
    },
    [],
  );
  const removePerRunMentionItem = useCallback(
    (kind: PerRunMentionKind, id: string) => {
      setPerRunSelection((current) => removePerRunMention(current, kind, id));
    },
    [],
  );
  const clearPerRunMentions = useCallback(
    () => setPerRunSelection(emptyPerRunSelection()),
    [],
  );
  const togglePerRunFileMentionItem = useCallback((resource: FileMentionResource) => {
    setPerRunFiles((current) => togglePerRunFileMention(current, resource));
  }, []);
  const removePerRunFileMentionItem = useCallback((resource: FileMentionResource) => {
    setPerRunFiles((current) => removePerRunFileMention(current, resource));
  }, []);
  const clearPerRunFileMentions = useCallback(
    () => setPerRunFiles(emptyPerRunFileSelection()),
    [],
  );
  const addSelectedEvidenceRef = useCallback((ref: EvidenceRef) => {
    setSelectedEvidenceRefs((current) => uniqueEvidenceRefs([...current, ref]));
  }, []);
  const removeSelectedEvidenceRef = useCallback((id: string) => {
    setSelectedEvidenceRefs((current) => removeEvidenceRef(current, id));
  }, []);
  const clearSelectedEvidenceRefs = useCallback(() => {
    setSelectedEvidenceRefs([]);
  }, []);

  const sidePanelOpen = Boolean(configPanel) || workspaceFilesPanelOpen || dataLinkPanelOpen;
  const {
    containerRef: gridRef,
    viewportWidth: workspaceViewportWidth,
    isViewportResizing,
  } = useWorkspaceViewportWidth(!sidePanelOpen);

  const {
    width: leftPanelWidth,
    isResizing: isLeftPanelResizing,
    onResizeStart: onLeftPanelResizeStart,
    resetWidth: resetLeftPanelWidth,
  } = useLeftPanelResize({
    enabled: !configPanel && !workspaceFilesPanelOpen && !dataLinkPanelOpen && !userSidebarCollapsed,
  });

  // Cap the right panel drag to what still fits *alongside the current left
  // sidebar* (chat squeezed to its minimum). This prevents widening it into the
  // responsive "close right first" path that made it vanish and reopen as a
  // floating drawer.
  // Viewport-derived upper bound: the right panel may grow until the chat column
  // reaches its absolute minimum, keeping that minimum constant across sizes.
  const rightPanelDockableMaxWidth = maxDockableRightPanelWidth(
    workspaceViewportWidth,
    getLeftPanelWidth(userSidebarCollapsed, leftPanelWidth),
  );
  const {
    width: rightPanelWidth,
    isResizing: isRightPanelResizing,
    onResizeStart: onRightPanelResizeStart,
    resetWidth: resetRightPanelWidth,
  } = usePanelResize({
    enabled: !configPanel && !workspaceFilesPanelOpen && !dataLinkPanelOpen,
    maxWidth: rightPanelDockableMaxWidth,
  });

  const {
    sidebarCollapsed,
    rightPanelOpen,
    isAutoLayout,
    canDockRightPanel,
  } = useWorkspaceResponsiveLayout({
    viewportWidth: workspaceViewportWidth,
    userSidebarCollapsed,
    userRightPanelOpen,
    rightPanelWidth,
    leftPanelWidth,
    enabled: !sidePanelOpen && layoutHydrated,
  });

  const isRightConsoleVisible =
    !sidePanelOpen &&
    ((canDockRightPanel && rightPanelOpen) || isConsoleDrawerOpen);

  useEffect(() => {
    if (workspaceViewportWidth > 0) {
      setChatColumnWidth(workspaceViewportWidth);
    }
  }, [workspaceViewportWidth]);

  const openTaskConsole = useCallback(() => {
    // Console and left config/files/data-link panels share the center+right
    // column — dismiss the side panel so the console can actually appear.
    setConfigPanel(null);
    setWorkspaceFilesPanelOpen(false);
    setDataLinkPanelOpen(false);
    if (activeSessionId) {
      setRightPanelDismissedSessions((current) => {
        if (!current.has(activeSessionId)) return current;
        const next = new Set(current);
        next.delete(activeSessionId);
        return next;
      });
    }
    if (canDockRightPanel) {
      setUserRightPanelOpen(true);
      return;
    }
    setIsConsoleDrawerOpen(true);
  }, [activeSessionId, canDockRightPanel]);

  const toggleSidebar = useCallback(() => {
    if (sidebarCollapsed) {
      const next = resolveSidebarExpandPreferences({
        viewportWidth: workspaceViewportWidth,
        userRightPanelOpen,
        rightPanelWidth,
        leftPanelWidth,
      });
      setUserSidebarCollapsed(next.userSidebarCollapsed);
      setUserRightPanelOpen(next.userRightPanelOpen);
      return;
    }
    setUserSidebarCollapsed(true);
  }, [
    sidebarCollapsed,
    workspaceViewportWidth,
    userRightPanelOpen,
    rightPanelWidth,
    leftPanelWidth,
  ]);

  const closeTaskConsole = useCallback(() => {
    if (activeSessionId) {
      setRightPanelDismissedSessions((current) => {
        if (current.has(activeSessionId)) return current;
        const next = new Set(current);
        next.add(activeSessionId);
        return next;
      });
    }
    // Drop selection so reopening lands on Overview instead of a stale Details tab.
    setSelection(null);
    setArtifactFocusId(null);
    if (canDockRightPanel) {
      setUserRightPanelOpen(false);
      return;
    }
    setIsConsoleDrawerOpen(false);
  }, [canDockRightPanel, activeSessionId]);

  const handleToolGroupsChange = useCallback((nextGroups: ProcessToolGroup[]) => {
    setToolGroups((current) =>
      processToolGroupsEqual(current, nextGroups) ? current : nextGroups,
    );
  }, []);

  const openConfigPanel = useCallback((panel: WorkspaceConfigPanelKey) => {
    setWorkspaceFilesPanelOpen(false);
    setDataLinkPanelOpen(false);
    setConfigPanel((current) => (current === panel ? null : panel));
  }, []);

  const openWorkspaceFilesPanel = useCallback(() => {
    setConfigPanel(null);
    setDataLinkPanelOpen(false);
    setWorkspaceFilesPanelOpen((open) => !open);
  }, []);

  const closeWorkspaceFilesPanel = useCallback(() => {
    setWorkspaceFilesPanelOpen(false);
  }, []);

  const openDataLinkPanel = useCallback(() => {
    setConfigPanel(null);
    setWorkspaceFilesPanelOpen(false);
    setDataLinkPanelOpen((open) => !open);
  }, []);

  const closeDataLinkPanel = useCallback(() => {
    setDataLinkPanelOpen(false);
  }, []);

  useEffect(() => {
    if (!canDockRightPanel || !isConsoleDrawerOpen) return;
    setIsConsoleDrawerOpen(false);
    setUserRightPanelOpen(true);
  }, [canDockRightPanel, isConsoleDrawerOpen]);

  useEffect(() => {
    const stored = loadChatSessions(identityScopeKey);
    if (stored.length > 0) {
      setSessions(stored);
      setActiveSessionId(stored[0].id);
      return;
    }
    const first = createChatSession(defaultSessionTitle);
    setSessions([first]);
    setActiveSessionId(first.id);
  }, [defaultSessionTitle, identityScopeKey]);

  useEffect(() => {
    let cancelled = false;
    void configApi.listSessions({ limit: 50 })
      .then((response) => {
        if (cancelled) return;
        if (response.sessions.length === 0) {
          setSessions((current) => {
            if (current.length > 0) return current;
            const first = createChatSession(defaultSessionTitle);
            setActiveSessionId(first.id);
            return [first];
          });
          setSessionSyncError(null);
          return;
        }
        setSessions((current) => {
          const merged = mergeServerChatSessions(current, response.sessions);
          setActiveSessionId((active) => {
            if (active && merged.some((session) => session.id === active)) return active;
            return merged[0]?.id ?? active;
          });
          return merged;
        });
        setSessionSyncError(null);
      })
      .catch((error) => {
        if (!cancelled) {
          setSessionSyncError(error instanceof Error ? error.message : "Failed to load server sessions");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [defaultSessionTitle, identityScopeKey]);

  useEffect(() => {
    if (sessions.length > 0) persistChatSessions(sessions, identityScopeKey);
  }, [identityScopeKey, sessions]);

  useEffect(() => {
    if (workspaceLoading) return;
    const enabled = getEnabledLlmItems(workspaceConfig);
    const fallback =
      runDefaults?.activeLlmProfileId ??
      loadActiveLlmId(workspaceConfig, identityScopeKey);
    const resolved = resolveActiveLlmProfileId(enabled, activeLlmId, fallback);
    if (resolved !== activeLlmId) {
      setActiveLlmId(resolved);
    }
  }, [workspaceConfig.llm, activeLlmId, runDefaults, workspaceLoading, workspaceConfig, identityScopeKey]);

  useEffect(() => {
    if (activeLlmId) persistActiveLlmId(activeLlmId, identityScopeKey);
  }, [activeLlmId, identityScopeKey]);

  const saveConfigItem = useCallback(
    async (kind: WorkspaceConfigKind, item: WorkspaceConfigItem) => {
      setConfigActionError(null);
      try {
        return await updateItem(kind, item);
      } catch (error) {
        setConfigActionError(
          error instanceof Error ? error.message : "Failed to save configuration",
        );
        throw error;
      }
    },
    [updateItem],
  );

  const addConfigItem = useCallback(
    async (
      kind: WorkspaceConfigKind,
      payload: {
        name: string;
        description: string;
        enabled?: boolean;
        settings?: Record<string, string>;
      },
      skillFile?: File,
    ) => {
      const created = createWorkspaceConfigItem(
        kind,
        payload.name,
        payload.description,
      );
      if (payload.enabled !== undefined) created.enabled = payload.enabled;
      if (payload.settings) {
        created.settings = { ...created.settings, ...payload.settings };
      }
      setConfigActionError(null);
      try {
        const createdId = await createItem(kind, created, skillFile);
        if (kind === "llm") {
          setActiveLlmId(createdId);
        }
        return createdId;
      } catch (error) {
        setConfigActionError(
          error instanceof Error ? error.message : "Failed to create configuration",
        );
        throw error;
      }
    },
    [createItem],
  );

  const activeSession =
    sessions.find((session) => session.id === activeSessionId) ??
    sessions[0] ??
    null;
  const activeThreadId = activeSession?.threadId;
  const activeAgentId = activeThreadId ? dataTaskSessionAgentId(activeThreadId) : runtimeAgentId;
  const sessionThreadIds = useMemo(() => sessions.map((session) => session.threadId), [sessions]);
  const sessionAgentsReady = useSessionAgentRegistry(sessionThreadIds);
  const activeDatasourceId = resolveActiveDatasourceId(
    workspaceConfig,
    activeSession,
    perRunSelection,
    runDefaults?.activeDatasourceId,
  );

  const enabledLlmOptions = useMemo(
    () => getEnabledLlmItems(workspaceConfig),
    [workspaceConfig],
  );

  const mentionResources = useMemo(
    () => buildMentionResources(workspaceConfig, activeSession),
    [workspaceConfig, activeSession],
  );
  const workspaceFileMentionResources = useMemo(
    () => filterWorkspaceAssetFiles(workspaceFileAssets).map(fileMentionFromWorkspaceAsset),
    [workspaceFileAssets],
  );

  const { liveRun, sessionUsage, latestQuestion, runningThreadIds } = useLiveRun(activeThreadId);
  const { isThreadRestored, setThreadRestoring } = useConversationRestoreGate();
  const agentRenderSnapshot = useAgentMessageRenderSnapshot();
  const sessionStartedHints = useMemo<SessionStartedHints>(
    () => ({
      runCount: sessionUsage.runCount,
      messageCount: agentRenderSnapshot.messageCount,
      hasRunHistory: (liveRun.runHistory?.length ?? 0) > 0,
    }),
    [
      agentRenderSnapshot.messageCount,
      liveRun.runHistory?.length,
      sessionUsage.runCount,
    ],
  );

  const toggleSessionResourceItem = useCallback(
    (kind: PerRunMentionKind, id: string) => {
      if (!activeSessionId) return;
      const session = sessions.find((item) => item.id === activeSessionId) ?? null;
      if (isSessionResourceKindLocked(session, kind, sessionStartedHints)) {
        return;
      }
      setSessions((current) =>
        current.map((session) =>
          session.id === activeSessionId
            ? toggleSessionResource(session, kind, id)
            : session,
        ),
      );
    },
    [activeSessionId, sessionStartedHints, sessions],
  );

  // Drop @ picks for resources removed from workspace or disabled in session.
  useEffect(() => {
    setPerRunSelection((current) =>
      prunePerRunSelection(workspaceConfig, activeSession, current),
    );
  }, [workspaceConfig, activeSession]);
  useEffect(() => {
    if (!capabilitiesReady || !hasCapability("conversation.title")) return;
    const sessionTitle = liveRun.sessionTitle;
    if (!sessionTitle) return;
    setSessions((current) =>
      applyAutoTitle(current, sessionTitle.sessionId, sessionTitle.title, "llm"),
    );
  }, [capabilitiesReady, liveRun.sessionTitle]);
  const refreshWorkspaceFileAssets = useCallback(async () => {
    if (!capabilitiesReady || !hasCapability("files")) {
      setWorkspaceFileAssets([]);
      return;
    }
    const response = await configApi.listWorkspaceFiles({
      scope: "workspace",
      origin: ["uploaded", "saved"],
    });
    setWorkspaceFileAssets(filterWorkspaceAssetFiles(response.files ?? []));
  }, [capabilitiesReady]);
  useEffect(() => {
    if (!capabilitiesReady || !hasCapability("files")) {
      setWorkspaceFileAssets([]);
      return;
    }
    let cancelled = false;
    void refreshWorkspaceFileAssets()
      .then(() => {
        if (!cancelled) {
          // refreshWorkspaceFileAssets already updated state.
        }
      })
      .catch(() => {
        if (!cancelled) setWorkspaceFileAssets([]);
      });
    return () => {
      cancelled = true;
    };
  }, [capabilitiesReady, refreshWorkspaceFileAssets]);
  const sessionArtifactFileMentionResources = useMemo(
    () => liveRun.artifacts.map(fileMentionFromArtifact).filter((file): file is FileMentionResource => Boolean(file)),
    [liveRun.artifacts],
  );
  const fileMentionResources = useMemo(
    () => [...sessionArtifactFileMentionResources, ...workspaceFileMentionResources],
    [sessionArtifactFileMentionResources, workspaceFileMentionResources],
  );

  const backendToolPhases = useMemo(
    () => buildBackendToolPhaseMap(liveRun.toolCalls),
    [liveRun.toolCalls],
  );
  const backendToolRuntime = useMemo(
    () => ({
      phases: backendToolPhases,
      results: buildBackendToolResultMap(liveRun.toolCalls),
    }),
    [backendToolPhases, liveRun.toolCalls],
  );

  const cancelCurrentRun = useCallback(async () => {
    if (!liveRun.runId) {
      return;
    }
    const cancellable =
      liveRun.runStatus === "running" ||
      liveRun.runStatus === "suspended";
    if (!cancellable) {
      return;
    }
    setRunCancelBusy(true);
    setConfigActionError(null);
    try {
      await configApi.cancelRun(liveRun.runId, "user-requested");
    } catch (error) {
      const message = error instanceof Error ? error.message : "Failed to cancel run";
      setConfigActionError(message);
      throw error instanceof Error ? error : new Error(message);
    } finally {
      setRunCancelBusy(false);
    }
  }, [liveRun.runId, liveRun.runStatus]);

  const stopActiveRun = useCallback(async () => {
    await performChatRunCancellation({
      onCancelRun: cancelCurrentRun,
      onStopFrontend: () => stopActiveChatRunRef.current?.(),
      throwOnCancelFailure: true,
    });
  }, [cancelCurrentRun]);

  const filteredSessions = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    const matched = normalizedQuery
      ? sessions.filter((session) =>
          session.title.toLowerCase().includes(normalizedQuery),
        )
      : sessions;
    return sortChatSessions(matched);
  }, [query, sessions]);

  useEffect(() => {
    if (pendingBranchRun && activeThreadId && pendingBranchRun.sessionId !== activeThreadId) {
      setPendingBranchRun(null);
    }
  }, [activeThreadId, pendingBranchRun]);

  const createSession = useCallback(() => {
    const next = createChatSession(defaultSessionTitle);
    setSessions((current) => sortChatSessions([next, ...current]));
    setActiveSessionId(next.id);
    setSelection(null);
    setConfigPanel(null);
    setWorkspaceFilesPanelOpen(false);
    setDataLinkPanelOpen(false);
    // A brand-new session starts with the task console closed; it auto-opens on
    // the first question (see applyFirstUserMessageTitle).
    setUserRightPanelOpen(false);
    setIsConsoleDrawerOpen(false);
    clearDraftPromptRequest();
  }, [clearDraftPromptRequest, defaultSessionTitle]);

  const renameSession = useCallback((sessionId: string, title: string) => {
    setSessions((current) => renameChatSession(current, sessionId, title));
    void configApi.patchSessionTitle(sessionId, title)
      .then((updated) => {
        setSessions((current) =>
          current.map((session) => {
            if (session.id !== sessionId && session.threadId !== sessionId) return session;
            const server = serverSessionDtoToChatSession({
              id: session.id,
              threadId: session.threadId,
              title: updated.title,
              titleSource: updated.titleSource,
              updatedAt: updated.updatedAt,
            });
            return {
              ...session,
              title: server.title,
              titleSource: server.titleSource,
              updatedAt: server.updatedAt,
            };
          }),
        );
        setSessionSyncError(null);
      })
      .catch((error) => {
        setSessionSyncError(error instanceof Error ? error.message : "Failed to sync session rename");
      });
  }, []);

  const switchConversationBranch = useCallback((
    sessionId: string,
    options?: { preserveScroll?: boolean },
  ) => {
    if (options?.preserveScroll) {
      const scrollTop = findCopilotChatScrollContainer()?.scrollTop ?? 0;
      setConversationScrollIntent({ kind: "preserve", scrollTop });
    } else {
      setConversationScrollIntent({ kind: "bottom" });
    }
    setActiveSessionId(sessionId);
    setSelection(null);
    setArtifactFocusId(null);
    setIsTraceOpen(false);
    setConfigPanel(null);
    setWorkspaceFilesPanelOpen(false);
    setDataLinkPanelOpen(false);
    clearDraftPromptRequest();
  }, [clearDraftPromptRequest]);

  const createBranchRewrite = useCallback(async ({ runId, text }: BranchRewriteRequest) => {
    if (!activeThreadId) return;
    const created = await configApi.createSessionBranch(activeThreadId, { runId });
    const branchSession = serverSessionDtoToChatSession(created.session);
    setSessions((current) => {
      const withoutDuplicate = current.filter((session) => session.id !== branchSession.id);
      return sortChatSessions([branchSession, ...withoutDuplicate]);
    });
    setPendingBranchRun({ sessionId: branchSession.threadId, text });
    switchConversationBranch(branchSession.id);
  }, [activeThreadId, switchConversationBranch]);

  const createCheckpointBranch = useCallback(async (checkpointId: string) => {
    if (!activeThreadId) return;
    const created = await configApi.createSessionBranch(activeThreadId, { checkpointId });
    const branchSession = serverSessionDtoToChatSession(created.session);
    setSessions((current) => {
      const withoutDuplicate = current.filter((session) => session.id !== branchSession.id);
      return sortChatSessions([branchSession, ...withoutDuplicate]);
    });
    setPendingCheckpointResume({ sessionId: branchSession.threadId, checkpointId });
    switchConversationBranch(branchSession.id);
    setDraftPromptRequest((current) => ({
      id: (current?.id ?? 0) + 1,
      text: "",
    }));
  }, [activeThreadId, switchConversationBranch]);

  const deleteSession = useCallback(
    (sessionId: string) => {
      const target = sessions.find((session) => session.id === sessionId);
      const serverSessionId = target?.threadId ?? sessionId;
      let previousSessions: ChatSession[] | null = null;
      let previousActiveSessionId: string | null = activeSessionId;

      setSessions((current) => {
        previousSessions = current;
        const next = deleteChatSession(current, sessionId);
        if (next.length === 0) {
          const fallback = createChatSession(defaultSessionTitle);
          setActiveSessionId(fallback.id);
          return [fallback];
        }
        if (activeSessionId === sessionId) {
          setActiveSessionId(next[0]?.id ?? null);
        }
        return next;
      });
      setSelection(null);

      void configApi.deleteSession(serverSessionId)
        .then((result) => {
          const deletedIds = new Set(result.deletedSessionIds ?? [serverSessionId]);
          setSessions((current) => {
            const filtered = current.filter(
              (session) => !deletedIds.has(session.id) && !deletedIds.has(session.threadId),
            );
            if (filtered.length === current.length) return current;
            if (filtered.length === 0) {
              const fallback = createChatSession(defaultSessionTitle);
              setActiveSessionId(fallback.id);
              return [fallback];
            }
            setActiveSessionId((active) => {
              if (active && filtered.some((session) => session.id === active)) return active;
              return filtered[0]?.id ?? active;
            });
            return filtered;
          });
          setSessionSyncError(null);
        })
        .catch((error) => {
          if (error instanceof ConfigApiError && error.status === 404) {
            setSessionSyncError(null);
            return;
          }
          if (previousSessions) {
            setSessions(previousSessions);
            setActiveSessionId(previousActiveSessionId);
          }
          setSessionSyncError(
            error instanceof Error ? error.message : "Failed to delete session on server",
          );
        });
    },
    [activeSessionId, defaultSessionTitle, sessions],
  );

  const togglePinSession = useCallback((sessionId: string) => {
    setSessions((current) => {
      const next = sortChatSessions(togglePinChatSession(current, sessionId));
      const pinned = next.find((session) => session.id === sessionId)?.pinned;
      if (pinned) {
        requestAnimationFrame(() => {
          document
            .getElementById(`session-item-${sessionId}`)
            ?.scrollIntoView({ block: "nearest" });
        });
      }
      return next;
    });
  }, []);

  const applyFirstUserMessageTitle = useCallback((text: string) => {
    if (!activeSessionId) return;
    // Auto-open the task console when a question is asked, unless the user has
    // explicitly closed it for this session.
    if (!rightPanelDismissedSessions.has(activeSessionId)) {
      openTaskConsole();
    }
    const now = Date.now();
    setSessions((current) =>
      current.map((session) => {
        if (session.id !== activeSessionId) return session;
        const titled =
          session.titleSource === "default"
            ? applyAutoTitle(
                [session],
                activeSessionId,
                deriveSnippetTitle(text),
                "auto-snippet",
              )[0]!
            : session;
        return {
          ...titled,
          lastMessageAt: titled.lastMessageAt ?? now,
          updatedAt: now,
        };
      }),
    );
  }, [activeSessionId, rightPanelDismissedSessions, openTaskConsole]);

  const handleUserMessageSubmitted = useCallback((text: string) => {
    applyFirstUserMessageTitle(text);
    setPendingCheckpointResume((current) =>
      current?.sessionId === activeThreadId ? null : current,
    );
  }, [activeThreadId, applyFirstUserMessageTitle]);

  const agentContext = useMemo<JsonSerializable>(
    () =>
      JSON.parse(
        JSON.stringify({
          activeSession: activeSession
            ? { id: activeSession.id, title: activeSession.title }
            : null,
          ...(activeDatasourceId ? { datasourceId: activeDatasourceId } : {}),
          enabledSkillIds: enabledSkillIds(workspaceConfig),
          activeLlmId,
          activeLlm: activeLlmProfile(workspaceConfig, activeLlmId),
          workspaceConfig: sanitizeWorkspaceConfig(workspaceConfig),
          liveRun: {
            runStatus: liveRun.runStatus,
            plan: liveRun.plan,
            audits: liveRun.audits,
            artifacts: liveRun.artifacts,
          },
          selection,
        }),
      ) as JsonSerializable,
    [activeSession, activeDatasourceId, activeLlmId, liveRun, selection, workspaceConfig],
  );

  const visibleArtifacts = liveRun.artifacts;
  const visibleTimelineEvents = liveRun.events;

  const runConfigPayload = useMemo(
    () =>
      buildRunConfig(workspaceConfig, {
        activeLlmId,
        defaultDatasourceId: runDefaults?.activeDatasourceId,
        session: activeSession,
        perRunSelection,
        perRunFiles,
        evidenceRefs: selectedEvidenceRefs,
      }),
    [
      activeLlmId,
      activeSession,
      perRunFiles,
      perRunSelection,
      runDefaults?.activeDatasourceId,
      selectedEvidenceRefs,
      workspaceConfig,
    ],
  );

  const activeCheckpointResumeId =
    pendingCheckpointResume && pendingCheckpointResume.sessionId === activeThreadId
      ? pendingCheckpointResume.checkpointId
      : undefined;

  const runForwardedProps = useMemo(
    () => buildRunForwardedProps(activeDatasourceId, runConfigPayload, activeCheckpointResumeId),
    [activeCheckpointResumeId, activeDatasourceId, runConfigPayload],
  );

  const runConfigInputsRef = useRef({
    workspaceConfig,
    activeLlmId,
    activeSession,
    perRunSelection,
    perRunFiles,
    selectedEvidenceRefs,
    activeDatasourceId,
    runDefaultsActiveDatasourceId: runDefaults?.activeDatasourceId,
    activeThreadId,
    pendingCheckpointResume,
  });
  runConfigInputsRef.current = {
    workspaceConfig,
    activeLlmId,
    activeSession,
    perRunSelection,
    perRunFiles,
    selectedEvidenceRefs,
    activeDatasourceId,
    runDefaultsActiveDatasourceId: runDefaults?.activeDatasourceId,
    activeThreadId,
    pendingCheckpointResume,
  };

  const getRunForwardedProps = useCallback(() => {
    const inputs = runConfigInputsRef.current;
    const runConfig = buildRunConfig(inputs.workspaceConfig, {
      activeLlmId: inputs.activeLlmId,
      defaultDatasourceId: inputs.runDefaultsActiveDatasourceId,
      session: inputs.activeSession,
      perRunSelection: inputs.perRunSelection,
      perRunFiles: inputs.perRunFiles,
      evidenceRefs: inputs.selectedEvidenceRefs,
    });
    const checkpointId =
      inputs.pendingCheckpointResume?.sessionId === inputs.activeThreadId
        ? inputs.pendingCheckpointResume.checkpointId
        : undefined;
    return buildRunForwardedProps(inputs.activeDatasourceId, runConfig, checkpointId);
  }, []);

  useLayoutEffect(() => {
    onCopilotPropertiesChange(runForwardedProps);
  }, [onCopilotPropertiesChange, runForwardedProps]);

  // Kept for CopilotKit forwardedProps compatibility; backend also consumes
  // `run_config` via effectiveRunConfig merge.
  useAgentContext({
    description: "datasource_id",
    value: activeDatasourceId ?? "",
  });
  // Forward-compatible single run config (config-management-api.md §5).
  // Backend merges this with workspace defaults into effectiveRunConfig.
  useAgentContext({
    description: "run_config",
    value: runConfigPayload,
  });
  // General workspace state for debugging / richer context (secrets stripped).
  useAgentContext({
    description: "Current data task workspace state",
    value: agentContext,
  });

  const chatInputBindings = useMemo(
    () => ({
      activeLlmId,
      llmOptions: enabledLlmOptions,
      onActiveLlmChange: setActiveLlmId,
      onOpenLlmConfig: () => openConfigPanel("llm"),
      mentionResources,
      perRunSelection,
      onTogglePerRunMention: togglePerRunMentionItem,
      onRemovePerRunMention: removePerRunMentionItem,
      onClearPerRunMentions: clearPerRunMentions,
      fileMentionResources,
      perRunFiles,
      onTogglePerRunFileMention: togglePerRunFileMentionItem,
      onRemovePerRunFileMention: removePerRunFileMentionItem,
      onClearPerRunFileMentions: clearPerRunFileMentions,
      selectedEvidenceRefs,
      onRemoveEvidenceRef: removeSelectedEvidenceRef,
      onClearEvidenceRefs: clearSelectedEvidenceRefs,
      workspaceConfig,
      activeSession,
      sessionStartedHints,
      onToggleSessionResource: toggleSessionResourceItem,
      draftPromptRequest,
      onDraftPromptConsumed: consumeDraftPromptRequest,
      chatColumnWidth,
      agentId: activeAgentId,
      activeThreadId: activeThreadId ?? null,
      capabilitiesReady,
      onUserMessageSubmitted: handleUserMessageSubmitted,
      liveRunStatus: liveRun.runStatus,
      liveRunRunId: liveRun.runId ?? null,
      liveRunErrorMessage: liveRun.errorMessage ?? null,
      onCancelRun: cancelCurrentRun,
      stopActiveRun,
      stopActiveChatRunRef,
      cancelRunBusy: runCancelBusy,
      getRunForwardedProps,
    }),
    [
      activeLlmId,
      activeAgentId,
      activeSession,
      activeThreadId,
      capabilitiesReady,
      draftPromptRequest,
      consumeDraftPromptRequest,
      handleUserMessageSubmitted,
      cancelCurrentRun,
      stopActiveRun,
      chatColumnWidth,
      enabledLlmOptions,
      openConfigPanel,
      mentionResources,
      perRunSelection,
      perRunFiles,
      fileMentionResources,
      togglePerRunMentionItem,
      removePerRunMentionItem,
      clearPerRunMentions,
      togglePerRunFileMentionItem,
      removePerRunFileMentionItem,
      clearPerRunFileMentions,
      selectedEvidenceRefs,
      removeSelectedEvidenceRef,
      clearSelectedEvidenceRefs,
      toggleSessionResourceItem,
      sessionStartedHints,
      workspaceConfig,
      liveRun.errorMessage,
      liveRun.runId,
      liveRun.runStatus,
      runCancelBusy,
      getRunForwardedProps,
    ],
  );

  const conversationBranchActions = useMemo<ConversationBranchActions>(
    () => ({
      onCreateBranchRewrite: createBranchRewrite,
      onSwitchBranch: switchConversationBranch,
    }),
    [createBranchRewrite, switchConversationBranch],
  );

  const chatInput = useMemo(
    () =>
      function BoundDataTaskChatInput(
        inputProps: ComponentProps<typeof DataTaskChatInput>,
      ) {
        return <StableDataTaskChatInput inputProps={inputProps} />;
      },
    [],
  );

  useFrontendTool(
    {
      name: "selectDataSession",
      description: "Switch the visible data task session in the UI.",
      parameters: z.object({
        sessionId: z.string().describe("Session ID to activate"),
      }),
      handler: async ({ sessionId }) => {
        const target = sessions.find((session) => session.id === sessionId);
        if (!target) return `Session not found: ${sessionId}`;
        setActiveSessionId(target.id);
        setSelection(null);
        return `Selected: ${target.title}`;
      },
      followUp: false,
    },
    [sessions],
  );

  const handleSelectToolAction = useCallback(
    (toolCallId: string) => {
      setSelection({ type: "action", id: toolCallId });
      openTaskConsole();
    },
    [openTaskConsole],
  );

  const handleSelectToolGroup = useCallback(
    (groupId: string) => {
      setSelection({ type: "toolGroup", id: groupId });
      openTaskConsole();
    },
    [openTaskConsole],
  );

  const mentionArtifactFile = useCallback((artifact: DataArtifact) => {
    const fileMention = fileMentionFromArtifact(artifact);
    if (!fileMention) return;
    setPerRunFiles((current) => togglePerRunFileMention(current, fileMention));
  }, []);

  const promoteArtifactToWorkspace = useCallback(
    async (artifact: DataArtifact) => {
      await configApi.promoteArtifact(artifact.id);
      setPromotedArtifactIds((current) => {
        const next = new Set(current);
        next.add(artifact.id);
        return next;
      });
      await refreshWorkspaceFileAssets();
    },
    [refreshWorkspaceFileAssets],
  );
  const sidePanelError = workspaceError ?? configActionError ?? sessionSyncError;

  return (
    <BackendToolRuntimeProvider runtime={backendToolRuntime}>
      <DataTaskToolRenderers onSelectToolAction={handleSelectToolAction} />
      <div
        data-guide-id="workspace-layout"
        ref={gridRef}
        className={[
          "grid h-screen min-h-[560px] overflow-hidden bg-surface-subtle text-foreground",
          isRightPanelResizing ||
          isLeftPanelResizing ||
          isAutoLayout ||
          isViewportResizing
            ? ""
            : "transition-[grid-template-columns] duration-300",
        ].join(" ")}
        style={{
          gridTemplateColumns: getWorkspaceGridTemplateColumns({
            isConfigPanelOpen: sidePanelOpen,
            isRightPanelOpen: canDockRightPanel && rightPanelOpen,
            sidebarCollapsed,
            rightPanelWidth,
            leftPanelWidth,
          }),
        }}
      >
      <SessionPane
        activeSessionId={activeSession?.id ?? null}
        activeConfigPanel={configPanel}
        activeDataLinkPanel={dataLinkPanelOpen}
        activeFilesPanel={workspaceFilesPanelOpen}
        collapsed={sidebarCollapsed}
        leftPanelWidth={leftPanelWidth}
        isLeftPanelResizing={isLeftPanelResizing}
        onLeftPanelResizeStart={onLeftPanelResizeStart}
        onResetLeftPanelWidth={resetLeftPanelWidth}
        filteredSessions={filteredSessions}
        query={query}
        sessionCount={sessions.length}
        runningThreadIds={runningThreadIds}
        workspaceFileCount={workspaceFileAssets.length}
        capabilitiesReady={capabilitiesReady}
        onCreateSession={createSession}
        onOpenConfigPanel={openConfigPanel}
        onOpenDataLinkPanel={openDataLinkPanel}
        onOpenFilesPanel={openWorkspaceFilesPanel}
        onQueryChange={setQuery}
        onToggleCollapse={toggleSidebar}
        onSelectSession={(sessionId) => {
          // Apply session id first so a single click always wins over slower panel teardown.
          setActiveSessionId(sessionId);
          // Eager loading gate for threads that have never restored in this tab.
          if (!isThreadRestored(sessionId)) {
            setThreadRestoring(sessionId, true);
          }
          setConversationScrollIntent({ kind: "bottom" });
          setSelection(null);
          setArtifactFocusId(null);
          setIsTraceOpen(false);
          setConfigPanel(null);
          setWorkspaceFilesPanelOpen(false);
          setDataLinkPanelOpen(false);
          clearDraftPromptRequest();
        }}
        onRenameSession={renameSession}
        onDeleteSession={deleteSession}
        onTogglePinSession={togglePinSession}
        workspaceConfig={workspaceConfig}
        quickStartGuide={
          <QuickStartGuide
            workspaceConfig={workspaceConfig}
            liveRun={liveRun}
            hasSubmittedTask={
              agentRenderSnapshot.messageCount > 0 ||
              liveRun.runStatus !== "idle" ||
              sessionUsage.runCount > 0
            }
            onOpenConfigPanel={openConfigPanel}
            onOpenTaskConsole={openTaskConsole}
            onUseExampleQuery={(text) =>
              setDraftPromptRequest((current) => ({
                id: (current?.id ?? 0) + 1,
                text,
              }))
            }
          />
        }
      />

      {dataLinkPanelOpen ? (
        <DataLinkPanel
          onBack={closeDataLinkPanel}
          onOpenMcpSettings={() => {
            setDataLinkPanelOpen(false);
            setWorkspaceFilesPanelOpen(false);
            setConfigPanel("mcp");
          }}
        />
      ) : workspaceFilesPanelOpen ? (
        <WorkspaceFilesLibraryPanel
          sessionId={activeThreadId}
          onBack={closeWorkspaceFilesPanel}
          onFilesChange={setWorkspaceFileAssets}
        />
      ) : configPanel ? (
        <div className="flex min-h-0 min-w-0 flex-col overflow-hidden bg-surface">
          {sidePanelError && (
            <div className="border-b border-rose-200 bg-rose-50 px-4 py-2 text-xs text-rose-800">
              {sidePanelError}
            </div>
          )}
          <JobProgressBanner
            job={activeJob}
            onCancel={(jobId) =>
              cancelJob(jobId).then((job) => setActiveJob(job))
            }
            onDismiss={() => setActiveJob(null)}
          />
          <WorkspaceConfigPanel
            panel={configPanel}
            items={workspaceConfig[configPanel]}
            workspaceConfig={workspaceConfig}
            datasourceTypes={datasourceTypes}
            loading={workspaceLoading || !capabilitiesReady}
            onAdd={(payload, skillFile) => addConfigItem(configPanel, payload, skillFile)}
            onBack={() => setConfigPanel(null)}
            onSwitchPanel={(panel) => setConfigPanel(panel)}
            onSaveItem={(item) => saveConfigItem(configPanel, item)}
            onDeleteItem={(itemId) => deleteItem(configPanel, itemId)}
            onTestItem={(itemId) => testItem(configPanel, itemId)}
            onIntrospect={
              configPanel === "db"
                ? async (itemId) => {
                    const job = await introspectDatasource(itemId);
                    setActiveJob(job);
                    const finished = await pollJob(job.id, setActiveJob);
                    setActiveJob(finished);
                  }
                : undefined
            }
            onReindex={
              configPanel === "kb"
                ? async (itemId) => {
                    const job = await reindexKnowledgeBase(itemId);
                    setActiveJob(job);
                    const finished = await pollJob(job.id, setActiveJob);
                    setActiveJob(finished);
                  }
                : undefined
            }
            onUploadKnowledgeFile={
              configPanel === "kb"
                ? (itemId, file) => uploadKnowledgeFile(itemId, file)
                : undefined
            }
            onListKnowledgeFiles={
              configPanel === "kb"
                ? (itemId) => listKnowledgeFiles(itemId)
                : undefined
            }
            onDeleteKnowledgeFile={
              configPanel === "kb"
                ? (itemId, documentId) => deleteKnowledgeFile(itemId, documentId)
                : undefined
            }
            onRetryKnowledgeFile={
              configPanel === "kb"
                ? (itemId, documentId) => reindexKnowledgeFile(itemId, documentId).then(() => undefined)
                : undefined
            }
            onReplaceSkill={
              configPanel === "skill"
                ? (itemId, file) => replaceSkillPackage(itemId, file)
                : undefined
            }
            onValidateSkill={
              configPanel === "skill"
                ? (itemId) => validateSkill(itemId)
                : undefined
            }
          />
        </div>
      ) : (
        <>
      <DataTaskChatInputBindingsProvider value={chatInputBindings}>
      <ConversationBranchActionsContext.Provider value={conversationBranchActions}>
      <ToolActionSelectionContext.Provider value={handleSelectToolAction}>
      <ToolGroupSelectionContext.Provider value={handleSelectToolGroup}>
      <div className="relative z-10 min-h-0 min-w-0 overflow-hidden">
      <ChatPane
        activeThreadId={activeThreadId}
        sessionAgentsReady={sessionAgentsReady}
        sessions={sessions}
        title={activeSession?.title ?? "Data Tasks"}
        workspaceConfig={workspaceConfig}
        activeSession={activeSession}
        liveRunStatus={liveRun.runStatus}
        liveRun={liveRun}
        runningThreadIds={runningThreadIds}
        chatInput={chatInput}
        rightPanelOpen={isRightConsoleVisible}
        onOpenRightPanel={openTaskConsole}
        onChatColumnWidthChange={setChatColumnWidth}
        onToolGroupsChange={handleToolGroupsChange}
        onUseExamplePrompt={(text) =>
          setDraftPromptRequest((current) => ({
            id: (current?.id ?? 0) + 1,
            text,
          }))
        }
        capabilitiesReady={capabilitiesReady}
        pendingBranchRun={pendingBranchRun}
        onPendingBranchRunDispatched={() => setPendingBranchRun(null)}
        getRunForwardedProps={getRunForwardedProps}
        onUserMessageSubmitted={handleUserMessageSubmitted}
      />
      </div>
      </ToolGroupSelectionContext.Provider>
      </ToolActionSelectionContext.Provider>
      </ConversationBranchActionsContext.Provider>
      </DataTaskChatInputBindingsProvider>

      {canDockRightPanel && rightPanelOpen ? (
        <div
          className="relative z-0 flex h-full min-h-0 shrink-0 isolate overflow-hidden"
          style={{
            width: rightPanelWidth,
            minWidth: rightPanelWidth,
            maxWidth: rightPanelWidth,
          }}
        >
          <PanelResizeHandle
            edge="left"
            width={rightPanelWidth}
            minWidth={RIGHT_PANEL_MIN_WIDTH}
            maxWidth={rightPanelDockableMaxWidth}
            label={t("chat.resizeConsole")}
            isResizing={isRightPanelResizing}
            onResizeStart={onRightPanelResizeStart}
            onReset={resetRightPanelWidth}
          />
          <TaskConsolePanel
            key={`task-console-dock-${activeThreadId ?? activeSession?.id ?? "no-session"}`}
            sessionId={activeSession?.id ?? ""}
            runId={liveRun.runId}
            onReferenceEvidence={addSelectedEvidenceRef}
            artifacts={visibleArtifacts}
            liveRun={liveRun}
            toolGroups={toolGroups}
            sessionUsage={sessionUsage}
            selection={selection}
            visibleEvents={visibleTimelineEvents}
            currentQuestion={latestQuestion}
            artifactFocusId={artifactFocusId}
            onArtifactFocusHandled={() => setArtifactFocusId(null)}
            onClearSelection={() => setSelection(null)}
            onClose={closeTaskConsole}
            onMentionArtifact={mentionArtifactFile}
            onOpenTrace={() => setIsTraceOpen(true)}
            onPromoteArtifact={promoteArtifactToWorkspace}
            onArtifactExportJob={setActiveJob}
            onSelectEvent={(eventId) =>
              setSelection({ type: "action", id: eventId })
            }
            onSelectToolGroup={(groupId) =>
              setSelection({ type: "toolGroup", id: groupId })
            }
            promotedArtifactIds={promotedArtifactIds}
          />
        </div>
      ) : null}
        </>
      )}

      <TaskConsoleDrawer
        key={`task-console-drawer-${activeThreadId ?? activeSession?.id ?? "no-session"}`}
        sessionId={activeSession?.id ?? ""}
        runId={liveRun.runId}
        onReferenceEvidence={addSelectedEvidenceRef}
        artifacts={visibleArtifacts}
        liveRun={liveRun}
        toolGroups={toolGroups}
        sessionUsage={sessionUsage}
        selection={selection}
        visibleEvents={visibleTimelineEvents}
        currentQuestion={latestQuestion}
        artifactFocusId={artifactFocusId}
        onArtifactFocusHandled={() => setArtifactFocusId(null)}
        onClearSelection={() => setSelection(null)}
        onMentionArtifact={mentionArtifactFile}
        isOpen={!sidePanelOpen && !canDockRightPanel && isConsoleDrawerOpen}
        onClose={closeTaskConsole}
        onOpenTrace={() => setIsTraceOpen(true)}
        onPromoteArtifact={promoteArtifactToWorkspace}
        onArtifactExportJob={setActiveJob}
        onSelectEvent={(eventId) =>
          setSelection({ type: "action", id: eventId })
        }
        onSelectToolGroup={(groupId) =>
          setSelection({ type: "toolGroup", id: groupId })
        }
        promotedArtifactIds={promotedArtifactIds}
      />

      <TraceOverlay
        artifacts={visibleArtifacts}
        liveRun={liveRun}
        sessionId={activeThreadId ?? undefined}
        isOpen={isTraceOpen}
        onClose={() => setIsTraceOpen(false)}
        onCreateCheckpointBranch={createCheckpointBranch}
        onSelectArtifact={(artifactId) => {
          setArtifactFocusId(artifactId);
          openTaskConsole();
          setIsTraceOpen(false);
        }}
        onSelectEvent={(eventId) => {
          setSelection({ type: "action", id: eventId });
          openTaskConsole();
          setIsTraceOpen(false);
        }}
      />
    </div>
    </BackendToolRuntimeProvider>
  );
}

function DataTaskToolRenderers({
  onSelectToolAction,
}: {
  onSelectToolAction: (toolCallId: string) => void;
}) {
  useRenderTool(
    {
      name: "inspect_schema",
      parameters: z.object({
        datasource_id: z.string().optional(),
        table_names: z.array(z.string()).optional(),
      }),
      render: ({ name, parameters, result, status, toolCallId }) => (
        <SchemaToolCard
          toolCallId={toolCallId}
          name={name}
          parameters={parameters}
          result={result}
          status={status}
          onSelectToolAction={onSelectToolAction}
        />
      ),
    },
    [onSelectToolAction],
  );

  useRenderTool(
    {
      name: "run_sql_readonly",
      parameters: z.object({
        sql: z.string().optional(),
        limit: z.number().optional(),
      }),
      render: ({ name, parameters, result, status, toolCallId }) => (
        <SqlToolCard
          toolCallId={toolCallId}
          name={name}
          parameters={parameters}
          result={result}
          status={status}
          onSelectToolAction={onSelectToolAction}
        />
      ),
    },
    [onSelectToolAction],
  );

  useRenderTool({
    name: "*",
    render: ({ name, parameters, result, status, toolCallId }) => (
      <GenericToolCard
        toolCallId={toolCallId}
        name={name}
        parameters={parameters}
        result={result}
        status={status}
        onSelectToolAction={onSelectToolAction}
      />
    ),
  });

  return null;
}

function useEffectiveToolResult(
  toolCallId: string | undefined,
  copilotResult?: string,
): string | undefined {
  const backendResult = useBackendToolResult(toolCallId);
  return copilotResult ?? backendResult;
}

function useResolvedToolDisplayStatus(
  toolCallId: string | undefined,
  copilotStatus: CopilotToolStatus,
  copilotResult?: string,
): ToolDisplayStatus {
  const effectiveResult = useEffectiveToolResult(toolCallId, copilotResult);
  const backendPhase = useBackendToolPhase(toolCallId);
  return resolveToolDisplayStatus({
    copilotStatus,
    backendPhase,
    hasResult: !!effectiveResult,
    resultIsError: toolResultLooksLikeError(effectiveResult),
  });
}

type ToolStatus = CopilotToolStatus;

function ToolCallSplitLayout({
  toolCallId,
  onSelectToolAction,
  children,
}: {
  toolCallId?: string;
  onSelectToolAction?: (toolCallId: string) => void;
  children: ReactNode;
}) {
  const selectable = Boolean(toolCallId && onSelectToolAction);
  const handleActivate = () => {
    if (toolCallId && onSelectToolAction) {
      onSelectToolAction(toolCallId);
    }
  };
  const handleContainerClick = (event: MouseEvent) => {
    if (!selectable) return;
    const target = event.target as HTMLElement;
    if (
      target.closest(
        "button, a, input, select, textarea, label, [data-no-tool-select]",
      )
    ) {
      return;
    }
    handleActivate();
  };

  const enhancedChildren = Children.map(children, (child) => {
    if (!isValidElement(child)) return child;
    if (child.type !== ToolInvocationCard && child.type !== ToolResultCard) {
      return child;
    }
    return cloneElement(child as React.ReactElement<{ onActivate?: () => void }>, {
      onActivate: selectable ? handleActivate : undefined,
    });
  });

  return (
    <div
      data-testid={selectable ? "selectable-tool-card" : undefined}
      className={[
        "mb-3 grid gap-2 last:mb-0",
        selectable
          ? "cursor-pointer rounded-xl ring-offset-2 transition-colors duration-150 hover:bg-surface-subtle"
          : "",
      ].join(" ")}
      onClick={selectable ? handleContainerClick : undefined}
      onKeyDown={
        selectable
          ? (event) => {
              if (event.key === "Enter" || event.key === " ") {
                event.preventDefault();
                handleActivate();
              }
            }
          : undefined
      }
      title={selectable ? "View tool details in the task console" : undefined}
    >
      {enhancedChildren}
    </div>
  );
}

function ToolInvocationCard({
  name,
  displayStatus,
  onActivate,
  children,
}: {
  name: string;
  displayStatus: ToolDisplayStatus;
  onActivate?: () => void;
  children: ReactNode;
}) {
  const t = useT();
  const statusTone = toolStatusToneClass(displayStatus);

  return (
    <div className="rounded-xl border border-border bg-surface p-3 text-sm text-muted shadow-[var(--shadow-card)]">
      <div
        className={[
          "flex items-center justify-between gap-3",
          onActivate
            ? "cursor-pointer rounded-lg transition-colors duration-150 hover:bg-surface-subtle"
            : "",
        ].join(" ")}
        onClick={
          onActivate
            ? (event) => {
                event.stopPropagation();
                onActivate();
              }
            : undefined
        }
        title={onActivate ? t("console.empty.noStepSelectedDescription") : undefined}
      >
        <div className="flex min-w-0 items-center gap-2">
          <span className="rounded-full border border-border bg-surface-subtle px-2 py-0.5 text-[11px] font-semibold text-muted">
            {t("chat.toolCalls")}
          </span>
          <strong className="truncate font-mono text-foreground">{name}</strong>
        </div>
        <span
          className={[
            "rounded-full border px-2 py-0.5 text-xs",
            statusTone,
            displayStatus === "executing" ? "animate-pulse" : "",
          ].join(" ")}
        >
          {invocationStatusLabel(displayStatus, t)}
        </span>
      </div>
      {children}
    </div>
  );
}

function ToolResultCard({
  name,
  onActivate,
  children,
}: {
  name: string;
  onActivate?: () => void;
  children: ReactNode;
}) {
  return (
    <div
      className="rounded-xl border border-border bg-surface p-3 text-sm text-muted shadow-[var(--shadow-card)]"
    >
      <div
        className={[
          "mb-2 flex items-center justify-between gap-3",
          onActivate
            ? "cursor-pointer rounded-lg transition-colors duration-150 hover:bg-surface-subtle"
            : "",
        ].join(" ")}
        onClick={
          onActivate
            ? (event) => {
                event.stopPropagation();
                onActivate();
              }
            : undefined
        }
        title={onActivate ? "View tool details in the task console" : undefined}
      >
        <div className="flex min-w-0 items-center gap-2">
          <span
            className="rounded-full border border-border bg-surface-subtle px-2 py-0.5 text-[11px] font-semibold text-muted"
          >
            Execution result
          </span>
          <span className="truncate font-mono text-xs text-muted-light">{name}</span>
        </div>
        <span
          className="rounded-full border border-step-success/25 bg-step-success/8 px-2 py-0.5 text-xs text-step-success"
        >
          Returned
        </span>
      </div>
      {children}
    </div>
  );
}

function invocationStatusLabel(
  status: ToolDisplayStatus,
  t: ReturnType<typeof useT>,
): string {
  if (status === "failed") return t("common.failed");
  if (status === "complete") return t("common.completed");
  return toolDisplayStatusLabel(status);
}

function toolStatusToneClass(displayStatus: ToolDisplayStatus): string {
  if (displayStatus === "complete") {
    return "border-border bg-surface-subtle text-muted";
  }
  if (displayStatus === "executing") {
    return "border-border bg-surface-subtle text-foreground";
  }
  if (displayStatus === "failed") {
    return "border-step-error/25 bg-step-error/8 text-step-error";
  }
  return "border-border bg-surface-subtle text-muted";
}

function ToolPendingHint({ displayStatus }: { displayStatus: ToolDisplayStatus }) {
  if (displayStatus === "complete") return null;
  const toneClass =
    displayStatus === "failed"
      ? "bg-step-error/8 text-step-error"
      : displayStatus === "executing"
        ? "bg-surface-subtle text-foreground"
        : "bg-surface-subtle text-muted";

  return (
    <p className={`mt-2 rounded-lg px-2.5 py-2 text-xs ${toneClass}`}>
      {toolPendingHint(displayStatus)}
    </p>
  );
}

function ToolResultMissingCard({ name }: { name: string }) {
  return (
    <div className="rounded-xl border border-dashed border-border bg-surface-subtle p-3 text-xs leading-5 text-muted">
      <div className="font-semibold text-foreground">Execution result not synced</div>
      <p className="mt-1">
        <span className="font-mono">{name}</span>{" "}
         tool observation has not reached the frontend thread. If the right-side Trace already has SQL audit or step
         status, this usually means the AG-UI{" "}
        <code className="text-[11px]">TOOL_CALL_RESULT</code>{" "}
         terminal event is missing. Refresh and retry, or inspect the dataFoundry runtime bridge.
      </p>
    </div>
  );
}

function renderToolFailureCard(name: string, result?: string) {
  return <ToolFailureResult toolName={name} result={result} />;
}

function SqlToolCard({
  toolCallId,
  name,
  parameters,
  result,
  status,
  onSelectToolAction,
}: {
  toolCallId?: string;
  name: string;
  parameters: { sql?: string; limit?: number } | undefined;
  result?: string;
  status: ToolStatus;
  onSelectToolAction?: (toolCallId: string) => void;
}) {
  const displayStatus = useResolvedToolDisplayStatus(toolCallId, status, result);
  const effectiveResult = useEffectiveToolResult(toolCallId, result);
  const hasResult = !!effectiveResult;
  const resultIsError = toolResultLooksLikeError(effectiveResult);

  return (
    <ToolCallSplitLayout
      toolCallId={toolCallId}
      onSelectToolAction={onSelectToolAction}
    >
      <ToolInvocationCard name={name} displayStatus={displayStatus}>
        {parameters !== undefined ? (
          <ToolFormattedParams toolName={name} parameters={parameters} />
        ) : null}
        {!hasResult && displayStatus !== "complete" && displayStatus !== "failed" && (
          <ToolPendingHint displayStatus={displayStatus} />
        )}
      </ToolInvocationCard>
      {hasResult && !resultIsError ? (
        <ToolResultCard name={name}>
          <ToolFormattedResult
            toolName={name}
            result={effectiveResult}
            variant="chat"
            showRawFallback={false}
          />
        </ToolResultCard>
      ) : displayStatus === "failed" || resultIsError ? (
        renderToolFailureCard(name, effectiveResult)
      ) : displayStatus === "complete" ? (
        <ToolResultMissingCard name={name} />
      ) : null}
    </ToolCallSplitLayout>
  );
}

type SchemaColumn = { name: string; type?: string; nullable?: boolean };
type SchemaResult = {
  datasource_id?: string;
  tables?: Array<{ name: string; columns?: SchemaColumn[] }>;
};

function SchemaToolCard({
  toolCallId,
  name,
  parameters,
  result,
  status,
  onSelectToolAction,
}: {
  toolCallId?: string;
  name: string;
  parameters: { datasource_id?: string; table_names?: string[] } | undefined;
  result?: string;
  status: ToolStatus;
  onSelectToolAction?: (toolCallId: string) => void;
}) {
  const displayStatus = useResolvedToolDisplayStatus(toolCallId, status, result);
  const effectiveResult = useEffectiveToolResult(toolCallId, result);
  const parsed = parseSchemaToolResult(effectiveResult) as SchemaResult | null;
  const hasResult = !!effectiveResult;
  const resultIsError = toolResultLooksLikeError(effectiveResult);

  return (
    <ToolCallSplitLayout
      toolCallId={toolCallId}
      onSelectToolAction={onSelectToolAction}
    >
      <ToolInvocationCard name={name} displayStatus={displayStatus}>
        {parameters !== undefined ? (
          <ToolFormattedParams toolName={name} parameters={parameters} />
        ) : null}
        {!hasResult && displayStatus !== "complete" && displayStatus !== "failed" && (
          <ToolPendingHint displayStatus={displayStatus} />
        )}
      </ToolInvocationCard>
      {hasResult && !resultIsError ? (
        <ToolResultCard name={name}>
          {parsed && Array.isArray(parsed.tables) && parsed.tables.length > 0 ? (
            <div className="grid gap-2">
              {parsed.tables.map((table) => (
                <div key={table.name} className="rounded-lg border border-border bg-surface-subtle p-2.5">
                  <div className="font-mono text-xs font-semibold text-foreground">
                    {table.name}
                  </div>
                  <div className="mt-1.5 flex flex-wrap gap-1">
                    {(table.columns ?? []).map((column) => (
                      <span
                        key={column.name}
                        className="rounded border border-border bg-surface px-1.5 py-0.5 font-mono text-[10px] text-muted"
                        title={column.type}
                      >
                        {column.name}
                        {column.type ? (
                          <span className="text-muted-light"> · {column.type}</span>
                        ) : null}
                      </span>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <ToolPayloadBlock title="Raw result" value={effectiveResult} tone="light" />
          )}
        </ToolResultCard>
      ) : displayStatus === "failed" || resultIsError ? (
        renderToolFailureCard(name, effectiveResult)
      ) : displayStatus === "complete" ? (
        <ToolResultMissingCard name={name} />
      ) : null}
    </ToolCallSplitLayout>
  );
}

function GenericToolCard({
  toolCallId,
  name,
  parameters,
  result,
  status,
  onSelectToolAction,
}: {
  toolCallId?: string;
  name: string;
  parameters: unknown;
  result?: string;
  status: ToolStatus;
  onSelectToolAction?: (toolCallId: string) => void;
}) {
  const displayStatus = useResolvedToolDisplayStatus(toolCallId, status, result);
  const effectiveResult = useEffectiveToolResult(toolCallId, result);
  const hasResult = !!effectiveResult;
  const resultIsError = toolResultLooksLikeError(effectiveResult);

  return (
    <ToolCallSplitLayout
      toolCallId={toolCallId}
      onSelectToolAction={onSelectToolAction}
    >
      <ToolInvocationCard name={name} displayStatus={displayStatus}>
        <ToolFormattedParams toolName={name} parameters={parameters} />
        {!hasResult && displayStatus !== "complete" && displayStatus !== "failed" && (
          <ToolPendingHint displayStatus={displayStatus} />
        )}
      </ToolInvocationCard>
      {hasResult && !resultIsError ? (
        <ToolResultCard name={name}>
          <ToolFormattedResult
            toolName={name}
            result={effectiveResult}
            variant="chat"
          />
        </ToolResultCard>
      ) : displayStatus === "failed" || resultIsError ? (
        renderToolFailureCard(name, effectiveResult)
      ) : displayStatus === "complete" ? (
        <ToolResultMissingCard name={name} />
      ) : null}
    </ToolCallSplitLayout>
  );
}

function ToolPayloadBlock({
  title,
  value,
  tone,
}: {
  title: string;
  value: unknown;
  tone: "dark" | "light";
}) {
  return (
    <div className="mt-2 overflow-hidden rounded-lg border border-border">
      <div className="border-b border-border bg-surface-subtle px-2.5 py-1 text-[11px] font-semibold text-muted-light">
        {title}
      </div>
      <pre
        className={[
          "max-h-44 overflow-auto whitespace-pre-wrap p-2 text-xs leading-5",
          tone === "dark"
            ? "bg-code-bg text-slate-100"
            : "bg-surface text-muted",
        ].join(" ")}
      >
        {formatPayload(value)}
      </pre>
    </div>
  );
}

type ChatRunStatus = LiveRun["runStatus"];

const ChatRunStatusContext = createContext<ChatRunStatus>("idle");
const ChatLiveRunContext = createContext<LiveRun | null>(null);
const ToolActionSelectionContext = createContext<
  ((toolCallId: string) => void) | null
>(null);
const ToolGroupSelectionContext = createContext<
  ((groupId: string) => void) | null
>(null);
const ConversationBranchActionsContext = createContext<ConversationBranchActions | null>(null);
const ProcessTimelineCollapseContext = createContext<{
  collapsed: boolean;
  toggle: () => void;
}>({
  collapsed: false,
  toggle: () => {},
});

type AssistantToolCallLike = {
  id?: string;
  function?: { name?: string };
};

function buildStepElapsedInput(input: {
  toolCalls: AssistantToolCallLike[];
  liveRun: LiveRun | null;
  isActive: boolean;
}): { status: StepToolStatus; startedAtMs?: number; finishedAtMs?: number } {
  const liveById = new Map(input.liveRun?.toolCalls.map((call) => [call.id, call]) ?? []);
  const liveCalls = input.toolCalls
    .map((call) => (call.id ? liveById.get(call.id) : undefined))
    .filter((call): call is LiveRun["toolCalls"][number] => Boolean(call));
  if (liveCalls.length === 0) {
    return { status: input.isActive ? "running" : "success" };
  }
  const status: StepToolStatus = liveCalls.some((call) => call.status === "failed")
    ? "failed"
    : liveCalls.some((call) => call.status === "running")
      ? "running"
      : "success";
  const starts = liveCalls
    .map((call) => call.startedAtMs)
    .filter((value): value is number => value !== undefined);
  const finishes = liveCalls
    .map((call) => call.finishedAtMs)
    .filter((value): value is number => value !== undefined);
  return {
    status,
    ...(starts.length > 0 ? { startedAtMs: Math.min(...starts) } : {}),
    ...(status !== "running" && finishes.length > 0
      ? { finishedAtMs: Math.max(...finishes) }
      : {}),
  };
}

function buildGroupIdForAssistantMessage(
  messageId: string | undefined,
  toolCalls: AssistantToolCallLike[],
): string | undefined {
  if (messageId) return `group-${messageId}`;
  const fallbackId = toolCalls
    .map((call) => call.id)
    .filter((id): id is string => typeof id === "string" && id.length > 0)
    .join("-");
  return fallbackId ? `group-${fallbackId}` : undefined;
}

function CopyContentButton({
  className = "",
  content,
}: {
  className?: string;
  content: string;
}) {
  const t = useT();
  const [copied, setCopied] = useState(false);
  const label = copied ? t("chat.copiedMessage") : t("chat.copyMessage");
  return (
    <>
      <button
        type="button"
        onClick={async (event) => {
          event.stopPropagation();
          try {
            await navigator.clipboard.writeText(content);
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
          } catch {
            /* clipboard unavailable */
          }
        }}
        className={[
          "cursor-pointer rounded-md p-0.5 text-muted-light transition-colors duration-150 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/20",
          className,
        ].filter(Boolean).join(" ")}
        title={label}
        aria-label={label}
      >
      {copied ? (
        <svg viewBox="0 0 20 20" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden>
          <path d="M5 10.5 8.5 14 15 7" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      ) : (
        <svg viewBox="0 0 20 20" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden>
          <path d="M4 4h8v8H4V4Z" />
          <path d="M8 8h8v8H8V8Z" />
        </svg>
      )}
    </button>
      {copied ? (
        <div
          role="status"
          className="pointer-events-none fixed inset-x-0 bottom-24 z-[80] flex justify-center px-4"
        >
          <div className="inline-flex items-center gap-2 rounded-full bg-foreground/90 px-3.5 py-2 text-xs font-medium text-white shadow-lg">
            <svg viewBox="0 0 20 20" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden>
              <path d="M5 10.5 8.5 14 15 7" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            {t("chat.addedToClipboard")}
          </div>
        </div>
      ) : null}
    </>
  );
}

type CopilotChatUserMessageProps = ComponentProps<typeof CopilotChatUserMessage>;

type BranchRewriteRequest = {
  runId: string;
  text: string;
};

type PendingBranchRun = {
  sessionId: string;
  text: string;
};

type PendingCheckpointResume = {
  checkpointId: string;
  sessionId: string;
};

async function waitForAgentIdle(
  agent: { isRunning?: boolean },
  timeoutMs = 5000,
): Promise<boolean> {
  const startedAt = Date.now();
  while (Boolean(agent.isRunning) && Date.now() - startedAt < timeoutMs) {
    await new Promise((resolve) => window.setTimeout(resolve, 50));
  }
  return !Boolean(agent.isRunning);
}

type ConversationBranchActions = {
  onCreateBranchRewrite: (request: BranchRewriteRequest) => Promise<void>;
  onSwitchBranch: (sessionId: string, options?: { preserveScroll?: boolean }) => void;
};

/** Hide reasoning bubbles that belong inside the next tool step card. */
function StepReasoningMessage({
  message,
  messages,
  ...props
}: ComponentProps<typeof CopilotChatReasoningMessage>) {
  useAgentMessageRenderGeneration();
  const chatConfig = useCopilotChatConfiguration();
  const { agent } = useAgent({ agentId: chatConfig?.agentId ?? runtimeAgentId });
  const allMessages = mergeMessagesForStepContext(
    agent.messages ?? [],
    messages ?? [],
  );
  if (reasoningMessageAbsorbedByFollowingToolStep(message, allMessages)) {
    return null;
  }
  return (
    <CopilotChatReasoningMessage
      message={message}
      messages={messages}
      header={{ className: "chat-reasoning-header" }}
      {...props}
    />
  );
}

/** Host pending HITL cards below the user turn when suspend lands before assistant bubble. */
function StepUserMessage(props: CopilotChatUserMessageProps) {
  const chatConfig = useCopilotChatConfiguration();
  const { agent } = useAgent({ agentId: chatConfig?.agentId ?? runtimeAgentId });
  const { copilotkit } = useCopilotKit();
  const bindings = useDataTaskChatInputBindings();
  const branchActions = useContext(ConversationBranchActionsContext);
  const conversation = useConversationBranchSnapshot(chatConfig?.threadId);
  const branchState = resolveUserMessageBranchState({
    activeSessionId: chatConfig?.threadId,
    conversation,
    messageId: props.message.id,
  });
  const content = messageTextContent(props.message.content);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(content);
  const [busy, setBusy] = useState(false);
  const canRewrite = Boolean(branchState);

  useEffect(() => {
    if (!editing) {
      setDraft(content);
    }
  }, [content, editing]);

  const submitRewrite = async () => {
    const text = draft.trim();
    if (!text || !branchState) {
      setEditing(false);
      return;
    }
    setBusy(true);
    try {
      if (branchState.branchable) {
        await branchActions?.onCreateBranchRewrite({ runId: branchState.runId, text });
      } else if (branchState.refreshOnly) {
        await bindings.stopActiveRun?.();
        if (
          branchState.runId &&
          branchState.runId !== bindings.liveRunRunId &&
          (branchState.status === "queued" ||
            branchState.status === "running" ||
            branchState.status === "suspended")
        ) {
          await configApi.cancelRun(branchState.runId, "user-requested");
        }
        const idle = await waitForAgentIdle(agent);
        if (!idle) {
          throw new Error("Could not stop the current run before rewriting.");
        }
        const currentMessages = agent.messages ?? [];
        const index = currentMessages.findIndex((message) => message.id === props.message.id);
        agent.setMessages(index >= 0 ? currentMessages.slice(0, index) : []);
        const forwardedProps = bindings.getRunForwardedProps();
        bindings.onUserMessageSubmitted(text);
        agent.setState(buildAgentRunStatePatch(forwardedProps, agent.state));
        agent.addMessage({ id: createClientId("msg"), role: "user", content: text });
        void copilotkit.runAgent({ agent, forwardedProps }).catch((error) => {
          reportDataFoundryRunError(error, chatConfig?.threadId);
        });
      }
      setEditing(false);
    } catch (error) {
      reportDataFoundryRunError(error, chatConfig?.threadId);
    } finally {
      setBusy(false);
    }
  };

  if (editing) {
    return (
      <>
        <div className="copilotKitMessage copilotKitUserMessage flex flex-col items-end px-3 pt-1">
          <form
            className="flex w-[min(520px,calc(100%-24px))] max-w-full flex-col gap-2 rounded-[1.35rem] bg-surface-subtle px-4 py-3 text-left text-foreground"
            onSubmit={(event) => {
              event.preventDefault();
              void submitRewrite();
            }}
          >
            <textarea
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={createChatTextareaKeyDownHandler(() => {
                void submitRewrite();
              })}
              className="min-h-16 w-full resize-none rounded-xl border border-transparent bg-transparent p-0 text-base leading-7 text-foreground outline-none transition-colors placeholder:text-muted-light"
              disabled={busy}
            />
            <div className="flex justify-end gap-2 pt-0.5">
              <button
                type="button"
                className="rounded-md px-2 py-1 text-sm text-muted-light transition-colors hover:text-foreground"
                onClick={() => setEditing(false)}
                disabled={busy}
              >
                Cancel
              </button>
              <button
                type="submit"
                className="rounded-lg bg-primary px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
                disabled={busy || draft.trim().length === 0}
              >
                Rewrite
              </button>
            </div>
          </form>
        </div>
        <CollaborationPendingInterruptSlot message={props.message} />
      </>
    );
  }

  return (
    <>
      <div className="copilotKitMessage copilotKitUserMessage flex flex-col items-end px-3 pt-1">
        <div
          className={[
            "group w-fit max-w-[min(520px,calc(100%-24px))] rounded-[1.35rem] bg-surface-subtle px-4 py-3 text-left text-base leading-7 text-foreground",
            canRewrite ? "cursor-text transition-colors hover:bg-surface-subtle/80" : "",
          ].filter(Boolean).join(" ")}
          onClick={() => {
            if (canRewrite) setEditing(true);
          }}
          title={canRewrite ? "Click to rewrite this question" : undefined}
        >
          <div className="whitespace-pre-wrap break-words">{content}</div>
        </div>
      </div>
      <div className="flex justify-end px-3 pt-1">
        <div className="flex items-center gap-1 text-muted-light">
          {branchState && branchState.total > 1 ? (
            <div className="mr-1 flex items-center gap-1">
              <button
                type="button"
                className="cursor-pointer rounded-md p-0.5 transition-colors duration-150 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/20"
                title="Previous branch"
                aria-label="Previous branch"
                onClick={() => {
                  if (branchState.previousSessionId) {
                    branchActions?.onSwitchBranch(branchState.previousSessionId, {
                      preserveScroll: true,
                    });
                  }
                }}
              >
                <ChevronIcon direction="left" />
              </button>
              <span className="min-w-8 text-center text-[11px] tabular-nums">
                {branchState.currentIndex + 1}/{branchState.total}
              </span>
              <button
                type="button"
                className="cursor-pointer rounded-md p-0.5 transition-colors duration-150 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/20"
                title="Next branch"
                aria-label="Next branch"
                onClick={() => {
                  if (branchState.nextSessionId) {
                    branchActions?.onSwitchBranch(branchState.nextSessionId, {
                      preserveScroll: true,
                    });
                  }
                }}
              >
                <ChevronIcon direction="right" />
              </button>
            </div>
          ) : null}
          <CopyContentButton content={content} />
          {branchState ? (
            <button
              type="button"
              className="cursor-pointer rounded-md p-0.5 transition-colors duration-150 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/20"
              title={branchState.branchable ? "Rewrite from here" : "Refresh this question"}
              aria-label={branchState.branchable ? "Rewrite from here" : "Refresh this question"}
              onClick={() => setEditing(true)}
            >
              <PencilMenuIcon />
            </button>
          ) : null}
        </div>
      </div>
      <CollaborationPendingInterruptSlot message={props.message} />
    </>
  );
}

/**
 * Renders one assistant turn as a process step in the ReAct loop:
 * - turns with tool calls become ReAct steps with Thinking and Tool calls panels;
 * - trailing plain-text turns stream as Answering, then settle as Answer;
 * - intermediate plain-text turns are shown as thinking/observation steps.
 * Completed steps collapse by default. The active block expands automatically,
 * then returns to default collapse when the next step takes over.
 */
function StepAssistantMessage({
  message,
  messages,
  isRunning: propIsRunning,
}: CopilotChatAssistantMessageProps) {
  useAgentMessageRenderGeneration();
  const t = useT();
  const chatConfig = useCopilotChatConfiguration();
  const { agent } = useAgent({ agentId: chatConfig?.agentId ?? runtimeAgentId });
  const liveRunStatus = useContext(ChatRunStatusContext);
  const liveRun = useContext(ChatLiveRunContext);
  const processTimelineCollapse = useContext(ProcessTimelineCollapseContext);
  const selectToolAction = useContext(ToolActionSelectionContext);
  const selectToolGroup = useContext(ToolGroupSelectionContext);
  const collaborationResponses = useThreadCollaborationResponsesForChat(chatConfig?.threadId);
  const pendingCollaborationInterrupt = usePendingCollaborationInterrupt(chatConfig?.threadId);
  const allMessages = mergeMessagesForStepContext(
    agent.messages ?? [],
    messages ?? [],
  );
  const isRunning = agent.isRunning ?? propIsRunning;
  const isOrphanPreamble = isOrphanPreambleMergedIntoFollowingToolStep(message, allMessages);

  const content = resolveToolStepThoughtContent(message, allMessages);
  const {
    isWaitingForUser,
    isCollaborationStep,
    isCollaborationComplete,
    isCollaborationFollowUpAnswer,
    isFollowUpAnswerActive,
    isLastAssistantInRun,
    isActive,
    isFinalAnswer,
    isFinalAnswerComplete,
    isThought,
    linkedCollaboration,
  } = resolveStepAssistantFlags({
    message,
    messages: allMessages,
    content,
    isRunning: Boolean(isRunning),
    liveRunStatus,
    liveRun,
    collaborationResponses,
  });
  const hostsPendingSlot = messageHostsPendingCollaborationSlot(
    message,
    pendingCollaborationInterrupt?.toolCallId,
    allMessages,
    liveRun,
    liveRunStatus,
  );
  const toolCalls = Array.isArray(message.toolCalls) ? message.toolCalls : [];
  const linkedLiveToolCall =
    linkedCollaboration && liveRun
      ? liveRun.toolCalls.find((call) => call.id === linkedCollaboration.toolCallId)
      : undefined;
  const authoritativeCollaborationToolName = linkedCollaboration?.toolName;
  const linkedToolCallId = linkedLiveToolCall?.id ?? linkedCollaboration?.toolCallId;
  const linkedToolCallName =
    authoritativeCollaborationToolName ?? linkedLiveToolCall?.name;
  const linkedToolCallEntry =
    linkedToolCallId && linkedToolCallName
      ? [
          {
            id: linkedToolCallId,
            type: "function" as const,
            function: {
              name: linkedToolCallName,
              arguments: "{}",
            },
          },
        ]
      : [];
  const correctedToolCalls =
    authoritativeCollaborationToolName && toolCalls.length > 0
      ? toolCalls.map((call) => {
          if (
            call.id !== linkedCollaboration?.toolCallId ||
            call.function?.name === authoritativeCollaborationToolName
          ) {
            return call;
          }
          return {
            ...call,
            function: {
              ...call.function,
              name: authoritativeCollaborationToolName,
              arguments: call.function?.arguments ?? "{}",
            },
          };
        })
      : toolCalls;
  const liveToolCallEntries =
    correctedToolCalls.length === 0
      ? resolveAssistantLiveToolCalls({
          message,
          messages: allMessages,
          liveRun,
        }).map((call) => ({
          id: call.id,
          type: "function" as const,
          function: {
            name: call.name,
            arguments: "{}",
          },
        }))
      : [];
  const effectiveToolCalls =
    isCollaborationComplete && linkedToolCallEntry.length > 0
      ? linkedToolCallEntry
      : correctedToolCalls.length > 0
        ? correctedToolCalls
        : linkedToolCallEntry.length > 0
          ? linkedToolCallEntry
          : liveToolCallEntries;
  const displayHasToolCalls = effectiveToolCalls.length > 0;
  const hasLiveToolCallsForCurrentMessage =
    toolCalls.length === 0 && liveToolCallEntries.length > 0;
  const currentMessageIndex = allMessages.findIndex((item) => item.id === message.id);
  const lastUserIndex =
    currentMessageIndex >= 0
      ? (allMessages
          .slice(0, currentMessageIndex + 1)
          .map((item, index) => ({ item, index }))
          .filter(({ item }) => item.role === "user")
          .at(-1)?.index ?? -1)
      : -1;
  const nextUserIndex =
    currentMessageIndex >= 0
      ? allMessages.findIndex(
          (item, index) => index > currentMessageIndex && item.role === "user",
        )
      : -1;
  const currentRunMessages =
    currentMessageIndex >= 0
      ? allMessages.slice(lastUserIndex + 1, nextUserIndex > -1 ? nextUserIndex : undefined)
      : allMessages;
  const pendingCollaborationToolCall = findPendingCollaborationToolCall(
    liveRun,
    collaborationResponses,
    liveRunStatus,
  );
  const lastAssistantInRunId = [...currentRunMessages]
    .reverse()
    .find((item) => item.role === "assistant")?.id;
  const processMessagesInRun = currentRunMessages.filter((item) => {
    if (item.role !== "assistant") return false;
    if (isOrphanPreambleMergedIntoFollowingToolStep(item, allMessages)) {
      return false;
    }
    const hasTools = ((item as { toolCalls?: unknown[] }).toolCalls?.length ?? 0) > 0;
    const hasContent = hasMeaningfulText(
      messageTextContent((item as { content?: unknown }).content),
    );
    if (hasTools) return true;
    if (item.id === message.id && hasLiveToolCallsForCurrentMessage) return true;
    // The run's trailing content-only assistant message is the final answer; it
    // renders as a separate Answer (isProcessStep excludes isFinalAnswer), so it
    // must not inflate the "Work process N steps" count.
    if (hasContent && item.id === lastAssistantInRunId) return false;
    if (hasContent) return true;
    if (
      pendingCollaborationToolCall &&
      shouldShowPendingInterruptOnMessage(
        item,
        pendingCollaborationToolCall.id,
        allMessages,
        liveRun,
        liveRunStatus,
      )
    ) {
      return true;
    }
    return false;
  });
  const isProcessStep =
    (displayHasToolCalls ||
      isWaitingForUser ||
      hostsPendingSlot ||
      (isThought && !isCollaborationComplete)) &&
    !isFinalAnswer &&
    (!isCollaborationStep || isWaitingForUser || isCollaborationComplete);
  const isFirstProcessStep =
    isProcessStep && processMessagesInRun[0]?.id === message.id;
  const processStepCount = processMessagesInRun.length;
  const hideProcessStepForTimelineCollapse =
    shouldHideProcessStepForTimelineCollapse({
      isProcessStep,
      timelineCollapsed: processTimelineCollapse.collapsed,
    });

  const stepNumber = resolveAssistantToolStepNumber({
    message,
    messages: allMessages,
    liveRun,
    collaborationResponses,
  });

  const defaultCollapsed = !isActive && (displayHasToolCalls || isThought);
  const [manualCollapsed, setManualCollapsed] = useState<boolean | null>(null);
  const collapsed =
    isProcessStep && processTimelineCollapse.collapsed && !isActive
      ? true
      : manualCollapsed ?? defaultCollapsed;
  const wasActiveRef = useRef(isActive);

  useEffect(() => {
    if (isActive) {
      setManualCollapsed(null);
    } else if (wasActiveRef.current) {
      // This block just finished streaming/executing; reset to default collapse.
      setManualCollapsed(null);
    }
    wasActiveRef.current = isActive;
  }, [isActive]);

  if (isOrphanPreamble) {
    return null;
  }

  if (!content && !displayHasToolCalls && !isWaitingForUser && !hostsPendingSlot) {
    if (isActive) {
      return <ChatAssistantLoadingRow />;
    }
    return null;
  }

  const rawToolNames = effectiveToolCalls
    .map((call) => call?.function?.name ?? "")
    .filter((name) => isDisplayableToolName(name));
  const processContent = isCollaborationFollowUpAnswer ? "" : content;
  const showSplitAnswerBlock = isCollaborationFollowUpAnswer && content.length > 0;
  const matchingRecaps = collaborationResponses.filter((response) =>
    shouldShowCollaborationRecapOnMessage(message, response, allMessages),
  );
  const toolNames = rawToolNames
    .map((call) => toolDisplayTitle(call, t))
    .filter(Boolean)
    .join("、");
  const toolSummaries = buildStepToolSummaries({
    toolCalls: effectiveToolCalls,
    liveRun,
    isActive,
    t,
  });
  const stepElapsed = stepElapsedLabel(
    buildStepElapsedInput({
      toolCalls: effectiveToolCalls,
      liveRun,
      isActive,
    }),
    t,
  );
  const collapsedStepSummary = buildCollapsedStepSummary({
    thinking: processContent,
    tools: toolSummaries,
  });
  const collapsedToolChips = buildToolChipSummaries(toolSummaries, 3);
  const processGroupId = buildGroupIdForAssistantMessage(message.id, effectiveToolCalls);
  const toolActionLabel = isCollaborationComplete
    ? resolveCollaborationCompletedStepLabel(rawToolNames, linkedCollaboration?.toolName)
    : resolveToolStepActionLabel(rawToolNames);
  const collaborationStepLabel = resolveCollaborationStepLabel(
    rawToolNames,
    isActive,
    linkedCollaboration?.toolName,
  );

  const pendingToolName = pendingCollaborationToolCall?.name;
  const waitingToolNames =
    rawToolNames.length > 0
      ? rawToolNames
      : hostsPendingSlot && pendingToolName
        ? [pendingToolName]
        : rawToolNames;

  const kindLabel = isCollaborationComplete && displayHasToolCalls
    ? toolActionLabel
    : isWaitingForUser || hostsPendingSlot
      ? resolveToolStepActionLabel(waitingToolNames)
      : isCollaborationStep
        ? collaborationStepLabel
        : isFinalAnswer
          ? isActive
            ? t("chat.answering")
            : t("chat.answer")
          : displayHasToolCalls
            ? toolActionLabel
            : t("chat.thinking");
  const stepHeaderLabel = isProcessStep
    ? isWaitingForUser || hostsPendingSlot || isCollaborationStep
      ? kindLabel
      : displayHasToolCalls
        ? null
        : kindLabel
    : kindLabel;

  const summary = resolveStepSummaryText({
    content: processContent,
    hasToolCalls: displayHasToolCalls,
    displayToolNames: toolNames,
    toolActionLabel,
    isThought,
  });
  const theme = getStepCardTheme({
    hasToolCalls: displayHasToolCalls,
    isActive,
    isFinalAnswer,
    isFinalAnswerComplete,
    isThought,
    isCollaborationStep,
    isWaitingForUser,
  });

  const toggleCollapsed = () => {
    const next = !collapsed;
    setManualCollapsed(next);
  };

  const openStepDetails = () => {
    if (collapsed) {
      setManualCollapsed(false);
    }
    if (displayHasToolCalls && processGroupId && selectToolGroup) {
      selectToolGroup(processGroupId);
    }
  };

  const handleStepHeaderClick = () => {
    if (displayHasToolCalls && processGroupId && selectToolGroup) {
      openStepDetails();
      return;
    }
    toggleCollapsed();
  };

  if (isFinalAnswer) {
    return (
      <div
        data-copilotkit
        className="copilotKitMessage copilotKitAssistantMessage step-enter mb-6 px-1"
      >
        <div className="mb-2 flex items-center gap-2 text-xs font-medium text-muted-light">
          <span className="h-1.5 w-1.5 rounded-full bg-muted-light" />
          <span>{isActive ? t("chat.answering") : t("chat.answer")}</span>
          {isActive ? (
            <span className="inline-flex items-center gap-1 rounded-full border border-border bg-surface-subtle px-2 py-0.5 text-[10px] text-muted">
              <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-muted" />
              {t("chat.generating")}
            </span>
          ) : null}
          {content ? (
            <span className="ml-auto">
              <CopyContentButton content={content} />
            </span>
          ) : null}
        </div>
        {content ? (
          <div className="max-w-none">
            <RichMarkdown content={content} density="chat" />
            {isActive && (
              <span
                className={`caret-blink ml-0.5 inline-block h-4 w-[2px] -translate-y-[1px] align-middle ${theme.caret}`}
              />
            )}
          </div>
        ) : null}
      </div>
    );
  }

  if (hideProcessStepForTimelineCollapse) {
    return isFirstProcessStep ? (
      <div className="copilotKitMessage copilotKitAssistantMessage mb-1 mt-4 flex items-center gap-2 px-1 text-xs text-muted-light">
        <button
          type="button"
          onClick={processTimelineCollapse.toggle}
          className="inline-flex cursor-pointer items-center gap-1.5 rounded-md px-1.5 py-1 font-medium transition-colors duration-150 hover:bg-surface-subtle hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/20"
          aria-expanded={false}
        >
          <span>{t("chat.workProcess")}</span>
          <span className="tabular">
            {t(processStepCount === 1 ? "chat.stepCount" : "chat.stepCountPlural", {
              count: processStepCount,
            })}
          </span>
          <StepChevron expanded={false} />
        </button>
      </div>
    ) : null;
  }

  return (
    <>
    {isFirstProcessStep ? (
      <div className="copilotKitMessage copilotKitAssistantMessage mb-1 mt-4 flex items-center gap-2 px-1 text-xs text-muted-light">
        <button
          type="button"
          onClick={processTimelineCollapse.toggle}
          className="inline-flex cursor-pointer items-center gap-1.5 rounded-md px-1.5 py-1 font-medium transition-colors duration-150 hover:bg-surface-subtle hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/20"
          aria-expanded={!processTimelineCollapse.collapsed}
        >
          <span>{t("chat.workProcess")}</span>
          <span className="tabular">
            {t(processStepCount === 1 ? "chat.stepCount" : "chat.stepCountPlural", {
              count: processStepCount,
            })}
          </span>
          <StepChevron expanded={!processTimelineCollapse.collapsed} />
        </button>
      </div>
    ) : null}
    <div
      data-copilotkit
      style={theme.glowVar}
      className={[
        "copilotKitMessage copilotKitAssistantMessage step-enter relative mb-0 pl-7 pr-1 py-1.5 transition-colors duration-200",
        isProcessStep ? "" : `rounded-xl border p-3 shadow-[var(--shadow-card)] ${theme.card}`,
      ].join(" ")}
    >
      {isProcessStep ? (
        <>
          <span className="absolute left-[9px] top-0 bottom-0 w-px bg-border" aria-hidden />
          <span className="absolute left-0 top-2.5">
            <button
              type="button"
              onClick={(event) => {
                event.stopPropagation();
                if (displayHasToolCalls) {
                  openStepDetails();
                } else {
                  toggleCollapsed();
                }
              }}
              className="cursor-pointer rounded-full focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/20"
              title={
                displayHasToolCalls
                  ? t("chat.viewStepDetails")
                  : collapsed
                    ? t("chat.expandStep")
                    : t("chat.collapseStep")
              }
              aria-label={
                displayHasToolCalls
                  ? t("chat.viewStepDetails")
                  : collapsed
                    ? t("chat.expandStep")
                    : t("chat.collapseStep")
              }
            >
              <StepBadge
                stepNumber={stepNumber}
                isFinalAnswer={false}
                isStreamingAnswer={false}
                isActive={isActive}
                isThought={isThought}
                isCollaboration={isCollaborationStep || isWaitingForUser}
                isWaitingForUser={isWaitingForUser}
              />
            </button>
          </span>
        </>
      ) : null}
      <div
        role="button"
        tabIndex={0}
        aria-expanded={!collapsed}
        onClick={handleStepHeaderClick}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            handleStepHeaderClick();
          }
        }}
        className={[
          "flex w-full cursor-pointer items-center gap-2 rounded-lg text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/20",
          isProcessStep ? "px-2 py-1 hover:bg-surface-subtle" : "",
        ].join(" ")}
      >
        {!isProcessStep ? (
          <StepBadge
            stepNumber={stepNumber}
            isFinalAnswer={isFinalAnswerComplete}
            isStreamingAnswer={isFinalAnswer && isActive}
            isActive={isActive}
            isThought={isThought}
            isCollaboration={isCollaborationStep || isWaitingForUser}
            isWaitingForUser={isWaitingForUser}
          />
        ) : null}
        <div className="flex min-w-0 flex-1 items-center gap-2">
          {stepHeaderLabel ? (
            <span className={`shrink-0 text-xs font-semibold ${theme.label}`}>
              {stepHeaderLabel}
            </span>
          ) : null}
          {displayHasToolCalls ? (
            <span
              className={[
                "min-w-0 truncate text-[10px]",
                stepHeaderLabel ? "text-muted-light" : `font-semibold ${theme.label}`,
              ].join(" ")}
            >
              {t(
                effectiveToolCalls.length === 1 ? "chat.toolCount" : "chat.toolCountPlural",
                { count: effectiveToolCalls.length, elapsed: stepElapsed },
              )}
            </span>
          ) : null}
        </div>
        {isActive && (
          <span
            className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-medium ${theme.statusPill}`}
          >
            <span className={`h-1.5 w-1.5 rounded-full animate-pulse ${theme.statusDot}`} />
            {isFinalAnswer ? t("chat.generating") : isWaitingForUser ? t("chat.waitingForInput") : isCollaborationStep ? t("chat.collaborating") : t("common.running")}
          </span>
        )}
        <div className="ml-auto flex items-center gap-0.5">
          {hasMeaningfulText(processContent) ? (
            <span onClick={(event) => event.stopPropagation()}>
              <CopyContentButton content={processContent} />
            </span>
          ) : null}
          <button
            type="button"
            onClick={(event) => {
              event.stopPropagation();
              toggleCollapsed();
            }}
            className="cursor-pointer rounded-md p-0.5 text-muted-light transition-colors duration-150 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/20"
            title={collapsed ? t("chat.expandStep") : t("chat.collapseStep")}
            aria-label={collapsed ? t("chat.expandStep") : t("chat.collapseStep")}
          >
            <StepChevron expanded={!collapsed} />
          </button>
        </div>
      </div>

      {collapsed ? (
        <div
          className={[
            "grid gap-1.5 rounded-lg text-left text-xs",
            isProcessStep ? "px-2 pb-1 text-muted" : "mt-1.5 text-muted-light",
          ].join(" ")}
        >
          {collapsedStepSummary.thinkingPreview ? (
            <button
              type="button"
              onClick={openStepDetails}
              className="line-clamp-2 cursor-pointer text-left leading-5 transition-colors duration-150 hover:text-foreground"
              title={collapsedStepSummary.thinkingPreview}
            >
              {collapsedStepSummary.thinkingPreview}
            </button>
          ) : null}
          {displayHasToolCalls ? (
            <div className="flex min-w-0 flex-wrap gap-1.5">
              {collapsedToolChips.map((chip) => (
                <ToolSummaryChip
                  key={chip.id}
                  chip={chip}
                  onSelectToolAction={selectToolAction}
                />
              ))}
            </div>
          ) : (
            <button
              type="button"
              onClick={openStepDetails}
              className="block w-full truncate rounded-lg text-left transition-colors duration-150 hover:text-foreground"
              title={summary}
            >
              {summary}
            </button>
          )}
        </div>
      ) : (
        <>
          {processContent && displayHasToolCalls ? (
            <>
              <StepSubPanel
                title={t("chat.thinking")}
                tone="thought"
                streaming={isActive}
                onHeaderClick={displayHasToolCalls ? openStepDetails : undefined}
              >
                <div className="text-sm leading-6 text-muted [&_code]:rounded [&_code]:bg-surface-subtle [&_code]:px-1 [&_ol]:my-1 [&_ol]:list-decimal [&_ol]:pl-5 [&_p]:my-1 [&_ul]:my-1 [&_ul]:list-disc [&_ul]:pl-5">
                  <CopilotChatAssistantMessage.MarkdownRenderer content={processContent} />
                  {isActive && (
                    <span className="caret-blink ml-0.5 inline-block h-4 w-[2px] -translate-y-[1px] bg-muted align-middle" />
                  )}
                </div>
              </StepSubPanel>
              <StepSubPanel
                title={t("chat.toolCalls")}
                tone="tool"
                badge={effectiveToolCalls.length}
                busy={isActive}
                onHeaderClick={openStepDetails}
              >
                <div className="flex min-w-0 flex-wrap gap-1.5">
                  {toolSummaries.map((chip) => (
                    <ToolSummaryChip
                      key={chip.id}
                      chip={chip}
                      onSelectToolAction={selectToolAction}
                    />
                  ))}
                </div>
              </StepSubPanel>
            </>
          ) : processContent ? (
            <div className="mt-2 text-sm leading-6 text-muted [&_code]:rounded [&_code]:bg-surface-subtle [&_code]:px-1 [&_ol]:my-1 [&_ol]:list-decimal [&_ol]:pl-5 [&_p]:my-1 [&_ul]:my-1 [&_ul]:list-disc [&_ul]:pl-5">
              <CopilotChatAssistantMessage.MarkdownRenderer content={processContent} />
              {isActive && (
                <span
                  className={`caret-blink ml-0.5 inline-block h-4 w-[2px] -translate-y-[1px] align-middle ${theme.caret}`}
                />
              )}
            </div>
          ) : displayHasToolCalls ? (
            <StepSubPanel
              title={t("chat.toolCalls")}
              tone="tool"
              badge={effectiveToolCalls.length}
              busy={isActive}
              onHeaderClick={openStepDetails}
            >
              <div className="flex min-w-0 flex-wrap gap-1.5">
                {toolSummaries.map((chip) => (
                  <ToolSummaryChip
                    key={chip.id}
                    chip={chip}
                    onSelectToolAction={selectToolAction}
                  />
                ))}
              </div>
            </StepSubPanel>
          ) : null}
        </>
      )}
    </div>
    {(hostsPendingSlot || isCollaborationStep || (isWaitingForUser && !isCollaborationComplete)) ? (
      <CollaborationPendingInterruptSlot message={message} />
    ) : null}
    {matchingRecaps.map((response) => (
      <CollaborationChoiceBubble key={response.id} response={response} />
    ))}
    {showSplitAnswerBlock ? (
      <div
        data-copilotkit
        className={[
          "copilotKitMessage copilotKitAssistantMessage step-enter px-1",
          isLastAssistantInRun ? "mb-6" : "mb-4",
        ].join(" ")}
      >
        <div className="mb-2 flex items-center gap-2 text-xs font-medium text-muted-light">
          <span className="h-1.5 w-1.5 rounded-full bg-muted-light" />
          <span>{isFollowUpAnswerActive ? t("chat.answering") : t("chat.answer")}</span>
          {isFollowUpAnswerActive ? (
            <span className="inline-flex items-center gap-1 rounded-full border border-border bg-surface-subtle px-2 py-0.5 text-[10px] text-muted">
              <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-muted" />
              {t("chat.generating")}
            </span>
          ) : null}
          <span className="ml-auto">
            <CopyContentButton content={content} />
          </span>
        </div>
        <div className="max-w-none">
          <RichMarkdown content={content} density="chat" />
          {isFollowUpAnswerActive ? (
            <span className="caret-blink ml-0.5 inline-block h-4 w-[2px] -translate-y-[1px] align-middle bg-primary" />
          ) : null}
        </div>
      </div>
    ) : null}
    </>
  );
}

function StepSubPanel({
  title,
  tone,
  badge,
  streaming,
  busy,
  onHeaderClick,
  children,
}: {
  title: string;
  tone: "thought" | "tool";
  badge?: number;
  streaming?: boolean;
  busy?: boolean;
  onHeaderClick?: () => void;
  children: ReactNode;
}) {
  const t = useT();
  const styles =
    tone === "thought"
      ? {
          shell: "border-border bg-surface",
          header: "border-border bg-surface-subtle",
          label: "text-muted",
          dot: "bg-muted-light",
        }
      : {
          shell: "border-border bg-surface",
          header: "border-border bg-surface-subtle",
          label: "text-muted",
          dot: "bg-muted-light",
        };

  return (
    <section className={`mt-2 overflow-hidden rounded-xl border ${styles.shell}`}>
      <button
        type="button"
        onClick={onHeaderClick}
        disabled={!onHeaderClick}
        className={[
          `flex w-full items-center gap-2 border-b px-3 py-2 ${styles.header}`,
          onHeaderClick
            ? "cursor-pointer transition-colors duration-150 hover:bg-surface focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/20"
            : "cursor-default",
        ].join(" ")}
        title={onHeaderClick ? "View step details in the task console" : undefined}
      >
        <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${styles.dot}`} />
        <span
          className={`text-[11px] font-semibold uppercase tracking-[0.06em] ${styles.label}`}
        >
          {title}
        </span>
        {badge !== undefined && badge > 0 ? (
          <span className="rounded-full border border-border bg-surface px-1.5 py-0.5 text-[10px] font-bold text-muted-light">
            {badge}
          </span>
        ) : null}
        {(streaming || busy) && (
          <span
            className={`ml-auto inline-flex items-center gap-1 text-[10px] font-medium ${styles.label}`}
          >
            <span className={`h-1.5 w-1.5 rounded-full animate-pulse ${styles.dot}`} />
            {streaming ? t("chat.generating") : t("common.running")}
          </span>
        )}
      </button>
      <div className="px-3 py-2.5">{children}</div>
    </section>
  );
}

function ToolSummaryChip({
  chip,
  onSelectToolAction,
}: {
  chip: ToolChipSummary;
  onSelectToolAction: ((toolCallId: string) => void) | null;
}) {
  const t = useT();
  const tone =
    chip.status === "failed"
      ? "border-step-error/25 bg-step-error/8 text-step-error"
      : chip.status === "running"
        ? "border-border bg-surface text-foreground"
        : "border-border bg-surface text-muted";
  const dot =
    chip.status === "failed"
      ? "bg-step-error"
      : chip.status === "running"
        ? "bg-step-warning"
        : "bg-step-success";
  const content = (
    <>
      {!chip.overflow ? (
        <span
          className={[
            "h-1.5 w-1.5 shrink-0 rounded-full",
            dot,
            chip.status === "running" ? "animate-pulse" : "",
          ].join(" ")}
        />
      ) : null}
      <span className="truncate">{chip.label}</span>
      {!chip.overflow && chip.durationLabel ? (
        <span className="shrink-0 font-mono text-[10px] text-muted-light">
          {chip.durationLabel}
        </span>
      ) : null}
    </>
  );

  if (chip.overflow || !onSelectToolAction) {
    return (
      <span
        className={[
          "inline-flex max-w-full items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-medium",
          tone,
        ].join(" ")}
      >
        {content}
      </span>
    );
  }

  return (
    <button
      type="button"
      onClick={(event) => {
        event.stopPropagation();
        onSelectToolAction(chip.id);
      }}
      className={[
        "inline-flex max-w-full cursor-pointer items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-medium transition-colors duration-150 hover:bg-surface-subtle focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/20",
        tone,
      ].join(" ")}
      title={t("chat.viewStepDetails")}
    >
      {content}
    </button>
  );
}

function getStepCardTheme({
  hasToolCalls,
  isActive,
  isFinalAnswer,
  isFinalAnswerComplete,
  isThought,
  isCollaborationStep,
  isWaitingForUser,
}: {
  hasToolCalls: boolean;
  isActive: boolean;
  isFinalAnswer: boolean;
  isFinalAnswerComplete: boolean;
  isThought: boolean;
  isCollaborationStep?: boolean;
  isWaitingForUser?: boolean;
}) {
  if (isWaitingForUser || (isCollaborationStep && isActive)) {
    return {
      card: "border-border bg-surface",
      label: "text-foreground",
      statusPill: "border border-border bg-surface-subtle text-foreground",
      statusDot: "bg-foreground",
      caret: "bg-primary",
      glowVar: { ["--step-glow" as string]: "rgb(13 13 13 / 0.12)" },
    };
  }
  if (isCollaborationStep) {
    return {
      card: "border-border bg-surface",
      label: "text-muted",
      statusPill: "border border-border bg-surface-subtle text-muted",
      statusDot: "bg-muted",
      caret: "bg-primary",
      glowVar: undefined,
    };
  }
  if (isFinalAnswerComplete) {
    return {
      card: "border-border bg-surface",
      label: "text-foreground",
      statusPill: "border border-border bg-surface-subtle text-muted",
      statusDot: "bg-muted",
      caret: "bg-primary",
      glowVar: undefined,
    };
  }
  if (isFinalAnswer && isActive) {
    return {
      card: "border-border bg-surface",
      label: "text-foreground",
      statusPill: "border border-border bg-surface-subtle text-foreground",
      statusDot: "bg-foreground",
      caret: "bg-primary",
      glowVar: { ["--step-glow" as string]: "rgb(13 13 13 / 0.12)" },
    };
  }
  if (hasToolCalls && isActive) {
    return {
      card: "border-border bg-surface",
      label: "text-foreground",
      statusPill: "border border-border bg-surface-subtle text-foreground",
      statusDot: "bg-foreground",
      caret: "bg-primary",
      glowVar: { ["--step-glow" as string]: "rgb(13 13 13 / 0.1)" },
    };
  }
  if (hasToolCalls) {
    return {
      card: "border-border bg-surface",
      label: "text-muted",
      statusPill: "border border-border bg-surface-subtle text-muted",
      statusDot: "bg-muted-light",
      caret: "bg-primary",
      glowVar: undefined,
    };
  }
  if (isThought && isActive) {
    return {
      card: "border-border bg-surface",
      label: "text-foreground",
      statusPill: "border border-border bg-surface-subtle text-foreground",
      statusDot: "bg-foreground",
      caret: "bg-primary",
      glowVar: { ["--step-glow" as string]: "rgb(13 13 13 / 0.1)" },
    };
  }
  if (isThought) {
    return {
      card: "border-border bg-surface",
      label: "text-muted",
      statusPill: "border border-border bg-surface-subtle text-muted",
      statusDot: "bg-muted-light",
      caret: "bg-primary",
      glowVar: undefined,
    };
  }
  return {
    card: "border-border bg-surface-subtle/80",
    label: "text-muted-light",
    statusPill: "bg-surface-subtle text-muted",
    statusDot: "bg-muted-light",
    caret: "bg-muted",
    glowVar: undefined,
  };
}

function StepChevron({ expanded }: { expanded: boolean }) {
  return (
    <span
      data-expanded={expanded}
      className="step-chevron flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-muted-light"
      aria-hidden
    >
      <svg viewBox="0 0 20 20" className="h-4 w-4" fill="currentColor">
        <path
          fillRule="evenodd"
          d="M5.23 7.21a.75.75 0 0 1 1.06.02L10 10.94l3.71-3.71a.75.75 0 1 1 1.06 1.06l-4.24 4.25a.75.75 0 0 1-1.06 0L5.21 8.29a.75.75 0 0 1 .02-1.08Z"
          clipRule="evenodd"
        />
      </svg>
    </span>
  );
}

function ChatAssistantLoadingRow() {
  return (
    <div
      data-copilotkit
      className="copilotKitMessage copilotKitAssistantMessage mb-4 flex items-center gap-2.5"
      role="status"
      aria-live="polite"
      aria-label="Agent thinking"
    >
      <StepBadge
        stepNumber={0}
        isFinalAnswer={false}
        isStreamingAnswer={false}
        isActive={true}
        isThought={true}
      />
      <span className="chat-assistant-loading-pill inline-flex items-center gap-1 rounded-full px-3 py-1.5 text-sm font-medium">
        <span className="chat-assistant-loading-dot h-1.5 w-1.5 rounded-full" />
        <span className="chat-assistant-loading-dot h-1.5 w-1.5 rounded-full" />
        <span className="chat-assistant-loading-dot h-1.5 w-1.5 rounded-full" />
      </span>
    </div>
  );
}

function StepBadge({
  stepNumber,
  isFinalAnswer,
  isStreamingAnswer,
  isActive,
  isThought,
  isCollaboration,
  isWaitingForUser,
}: {
  stepNumber: number;
  isFinalAnswer: boolean;
  isStreamingAnswer: boolean;
  isActive: boolean;
  isThought?: boolean;
  isCollaboration?: boolean;
  isWaitingForUser?: boolean;
}) {
  const presentation = resolveStepBadgePresentation({
    stepNumber,
    isFinalAnswer,
    isStreamingAnswer,
    isActive,
    isThought,
    isCollaboration,
    isWaitingForUser,
  });

  if (presentation.kind === "waiting") {
    return (
      <span className="relative flex h-5 w-5 items-center justify-center rounded-full bg-foreground">
        <span className="absolute inset-0 rounded-full bg-muted-light/45 animate-ping" />
        <span className="relative text-[9px] font-bold text-white">?</span>
      </span>
    );
  }
  if (presentation.kind === "collaboration") {
    return (
      <span className="flex h-5 w-5 items-center justify-center rounded-full border border-border bg-surface-subtle text-muted">
        <svg viewBox="0 0 20 20" className="h-3 w-3" fill="currentColor" aria-hidden>
          <path d="M10 9a3 3 0 1 0 0-6 3 3 0 0 0 0 6ZM4 17v-1a4 4 0 0 1 4-4h4a4 4 0 0 1 4 4v1H4Z" />
        </svg>
      </span>
    );
  }
  if (presentation.kind === "final") {
    return (
      <span className="flex h-5 w-5 items-center justify-center rounded-full border border-border bg-surface-subtle text-muted">
        <svg viewBox="0 0 20 20" className="h-3 w-3" fill="currentColor" aria-hidden>
          <path
            fillRule="evenodd"
            d="M16.7 5.3a1 1 0 0 1 0 1.4l-7.5 7.5a1 1 0 0 1-1.4 0L3.3 9.7a1 1 0 1 1 1.4-1.4l3.1 3.1 6.8-6.8a1 1 0 0 1 1.4 0Z"
            clipRule="evenodd"
          />
        </svg>
      </span>
    );
  }
  if (presentation.kind === "streaming") {
    return (
      <span className="relative flex h-5 w-5 items-center justify-center rounded-full bg-foreground">
        <span className="absolute inset-0 rounded-full bg-muted-light/60 animate-ping" />
        <span className="relative h-1.5 w-1.5 rounded-full bg-white" />
      </span>
    );
  }
  if (presentation.kind === "number") {
    return (
      <span
        className={`relative flex h-5 w-5 items-center justify-center rounded-full text-[10px] font-bold ${
          isActive
            ? "bg-foreground text-white"
            : "border border-border bg-surface text-muted"
        }`}
      >
        {isActive && (
          <span className="absolute inset-0 rounded-full bg-muted-light/60 animate-ping" />
        )}
        <span className="relative">{presentation.value}</span>
      </span>
    );
  }
  if (presentation.kind === "thought") {
    return (
      <span
        className={`relative flex h-5 w-5 items-center justify-center rounded-full ${
          isActive ? "bg-foreground" : "border border-border bg-surface"
        }`}
      >
        {isActive && (
          <span className="absolute inset-0 rounded-full bg-muted-light/60 animate-ping" />
        )}
        <span
          className={`relative h-1.5 w-1.5 rounded-full ${isActive ? "bg-white" : "bg-muted"}`}
        />
      </span>
    );
  }
  return (
    <span className="flex h-5 w-5 items-center justify-center">
      <span className="h-1.5 w-1.5 rounded-full bg-muted-light" />
    </span>
  );
}

function ChevronIcon({ direction }: { direction: "left" | "right" }) {
  return (
    <svg
      viewBox="0 0 20 20"
      className="h-4 w-4"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      {direction === "left" ? (
        <path d="M12.5 5 7.5 10l5 5" />
      ) : (
        <path d="M7.5 5l5 5-5 5" />
      )}
    </svg>
  );
}

function SidebarToggleIcon() {
  return (
    <svg
      viewBox="0 0 20 20"
      className="h-[18px] w-[18px]"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <rect x="2.75" y="3.75" width="14.5" height="12.5" rx="2.5" />
      <line x1="7.75" y1="3.75" x2="7.75" y2="16.25" />
    </svg>
  );
}

function WorkspaceFilesGlyph() {
  return (
    <svg
      viewBox="0 0 20 20"
      className="h-[18px] w-[18px]"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="M11 2.75H5.5A1.75 1.75 0 0 0 3.75 4.5v11A1.75 1.75 0 0 0 5.5 17.25h9a1.75 1.75 0 0 0 1.75-1.75V8z" />
      <path d="M11 2.75V8h5.25" />
    </svg>
  );
}

function SessionBubbleIcon() {
  return (
    <svg
      viewBox="0 0 20 20"
      className="h-4 w-4 shrink-0 text-muted-light"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      aria-hidden
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M4 5.5A2.5 2.5 0 0 1 6.5 3h7A2.5 2.5 0 0 1 16 5.5v5A2.5 2.5 0 0 1 13.5 13H9l-3.5 2.5V13H6.5A2.5 2.5 0 0 1 4 10.5v-5Z"
      />
    </svg>
  );
}

function SessionRunningIcon() {
  const t = useT();
  return (
    <span
      className="relative inline-flex h-4 w-4 shrink-0 items-center justify-center"
      aria-label={t("common.running")}
      title={t("common.running")}
    >
      <svg
        viewBox="0 0 20 20"
        className="session-sidebar-running-icon h-4 w-4"
        fill="none"
        aria-hidden
      >
        <g className="session-sidebar-running-orbit">
          <circle
            cx="10"
            cy="10"
            r="7.25"
            stroke="currentColor"
            strokeWidth="1.15"
            strokeLinecap="round"
            strokeDasharray="10 36"
            opacity="0.45"
          />
        </g>
        <g className="session-sidebar-running-orbit-inner">
          <circle
            cx="10"
            cy="10"
            r="5.75"
            stroke="currentColor"
            strokeWidth="1"
            strokeLinecap="round"
            strokeDasharray="5 28"
            opacity="0.3"
          />
        </g>
      </svg>
    </span>
  );
}

function MoreHorizontalIcon() {
  return (
    <svg viewBox="0 0 20 20" className="h-4 w-4" fill="currentColor" aria-hidden>
      <circle cx="4.5" cy="10" r="1.2" />
      <circle cx="10" cy="10" r="1.2" />
      <circle cx="15.5" cy="10" r="1.2" />
    </svg>
  );
}

function PinMenuIcon() {
  return (
    <svg
      viewBox="0 0 20 20"
      className="h-4 w-4"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      aria-hidden
    >
      <path strokeLinecap="round" strokeLinejoin="round" d="M8.5 3.5h3l.5 3 2.5 2.5-1.5 1.5-2-1V16l-2-1.5V9.5l-2 1-1.5-1.5L7 6.5l.5-3Z" />
    </svg>
  );
}

function ShareMenuIcon() {
  return (
    <svg
      viewBox="0 0 20 20"
      className="h-4 w-4"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      aria-hidden
    >
      <path strokeLinecap="round" strokeLinejoin="round" d="M12 4.5 16 8.5 12 12.5M16 8.5H8a3.5 3.5 0 1 0 0 7" />
    </svg>
  );
}

function PencilMenuIcon() {
  return (
    <svg
      viewBox="0 0 20 20"
      className="h-4 w-4"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      aria-hidden
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="m12.2 4.8 3 3-7.8 7.8H4.4v-3l7.8-7.8Z"
      />
    </svg>
  );
}

function TrashMenuIcon() {
  return (
    <svg
      viewBox="0 0 20 20"
      className="h-4 w-4"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      aria-hidden
    >
      <path strokeLinecap="round" strokeLinejoin="round" d="M5 6.5h10M8 6.5V5h4v1.5M7 6.5v8.5h6V6.5" />
    </svg>
  );
}

function SessionActionMenu({
  session,
  forceVisible,
  onRename,
  onDelete,
  onTogglePin,
}: {
  session: ChatSession;
  forceVisible?: boolean;
  onRename: () => void;
  onDelete: () => void;
  onTogglePin: () => void;
}) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: globalThis.MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  const runMenuAction = (action: () => void) => {
    action();
    setOpen(false);
  };

  const menuItemClass =
    "flex w-full cursor-pointer items-center gap-2.5 rounded-md px-2.5 py-2 text-left text-sm text-muted transition-colors duration-150 hover:bg-surface-subtle hover:text-foreground";

  return (
    <div ref={rootRef} className="relative shrink-0">
      <button
        type="button"
        aria-label={t("sidebar.sessionActions", { title: session.title })}
        aria-expanded={open}
        aria-haspopup="menu"
        onClick={(event) => {
          event.stopPropagation();
          setOpen((value) => !value);
        }}
        className={[
          "flex h-7 w-7 cursor-pointer items-center justify-center rounded-md text-muted-light transition-colors duration-150 hover:bg-surface-subtle hover:text-foreground",
          open || forceVisible
            ? "bg-surface-subtle text-foreground opacity-100"
            : "opacity-0 group-hover:opacity-100 group-focus-within:opacity-100",
        ].join(" ")}
      >
        <MoreHorizontalIcon />
      </button>
      {open ? (
        <div
          role="menu"
          className="absolute right-0 top-full z-20 mt-1 min-w-[148px] rounded-xl border border-border bg-surface p-1.5 shadow-[var(--shadow-card-hover)]"
        >
          <button
            type="button"
            role="menuitem"
            className={menuItemClass}
            onMouseDown={(event) => {
              event.preventDefault();
              event.stopPropagation();
            }}
            onClick={(event) => {
              event.stopPropagation();
              runMenuAction(onTogglePin);
            }}
          >
            <PinMenuIcon />
            {session.pinned ? t("session.unpin") : t("session.pin")}
          </button>
          <button
            type="button"
            role="menuitem"
            disabled
            className="flex w-full cursor-not-allowed items-center gap-2.5 rounded-md px-2.5 py-2 text-left text-sm text-muted-light opacity-50"
            title={t("session.sharingDisabled")}
          >
            <ShareMenuIcon />
            {t("session.share")}
          </button>
          <button
            type="button"
            role="menuitem"
            className={menuItemClass}
            onMouseDown={(event) => {
              event.preventDefault();
              event.stopPropagation();
            }}
            onClick={(event) => {
              event.stopPropagation();
              runMenuAction(onRename);
            }}
          >
            <PencilMenuIcon />
            {t("session.rename")}
          </button>
          <button
            type="button"
            role="menuitem"
            className="flex w-full cursor-pointer items-center gap-2.5 rounded-md px-2.5 py-2 text-left text-sm text-step-error transition-colors duration-150 hover:bg-step-error/8"
            onMouseDown={(event) => {
              event.preventDefault();
              event.stopPropagation();
            }}
            onClick={(event) => {
              event.stopPropagation();
              runMenuAction(onDelete);
            }}
          >
            <TrashMenuIcon />
            {t("session.delete")}
          </button>
        </div>
      ) : null}
    </div>
  );
}

function SessionListItem({
  session,
  active,
  running,
  onSelect,
  onRename,
  onDelete,
  onTogglePin,
}: {
  session: ChatSession;
  active: boolean;
  running: boolean;
  onSelect: () => void;
  onRename: (title: string) => void;
  onDelete: () => void;
  onTogglePin: () => void;
}) {
  const t = useT();
  const [editing, setEditing] = useState(false);
  const [draftTitle, setDraftTitle] = useState(session.title);

  useEffect(() => {
    if (!editing) setDraftTitle(session.title);
  }, [editing, session.title]);

  const commitRename = () => {
    onRename(draftTitle);
    setEditing(false);
  };
  const iconSlots = getSessionListItemIconSlots({
    pinned: Boolean(session.pinned),
    running,
  });

  if (editing) {
    return (
      <div className="rounded-lg bg-surface px-2 py-1.5 shadow-[var(--shadow-card)]">
        <input
          autoFocus
          value={draftTitle}
          onChange={(event) => setDraftTitle(event.target.value)}
          onBlur={commitRename}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              commitRename();
            }
            if (event.key === "Escape") {
              event.preventDefault();
              setDraftTitle(session.title);
              setEditing(false);
            }
          }}
          className="h-8 w-full rounded-md border border-border bg-surface px-2.5 text-sm text-foreground outline-none transition-colors duration-150 focus:border-muted-light"
        />
      </div>
    );
  }

  return (
    <div
      id={`session-item-${session.id}`}
      role="button"
      tabIndex={0}
      data-testid={`session-item-${session.id}`}
      onClick={onSelect}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onSelect();
        }
      }}
      title={session.title}
      className={[
        "group flex cursor-pointer items-center gap-2 rounded-lg px-2 py-1.5 transition-colors duration-150",
        active ? "bg-surface shadow-[var(--shadow-card)]" : "hover:bg-surface",
      ].join(" ")}
    >
      {iconSlots.leading === "running" ? (
        <SessionRunningIcon />
      ) : iconSlots.leading === "session" ? (
        <SessionBubbleIcon />
      ) : null}
      <span
        className={[
          "min-w-0 flex-1 truncate text-left text-sm transition",
          active ? "font-medium text-foreground" : "text-muted group-hover:text-foreground",
        ].join(" ")}
      >
        {session.title}
      </span>
      {iconSlots.trailing === "pin" && (
        <span
          className="shrink-0 text-muted"
          title={t("common.pinned")}
          aria-label={t("common.pinned")}
        >
          <PinMenuIcon />
        </span>
      )}
      <SessionActionMenu
        session={session}
        forceVisible={active}
        onRename={() => setEditing(true)}
        onDelete={onDelete}
        onTogglePin={onTogglePin}
      />
    </div>
  );
}

type SessionPaneProps = {
  activeSessionId: string | null;
  activeConfigPanel: WorkspaceConfigPanelKey | null;
  activeDataLinkPanel: boolean;
  activeFilesPanel: boolean;
  collapsed: boolean;
  leftPanelWidth: number;
  isLeftPanelResizing: boolean;
  onLeftPanelResizeStart: (event: ReactPointerEvent<HTMLElement>) => void;
  onResetLeftPanelWidth: () => void;
  filteredSessions: ChatSession[];
  query: string;
  sessionCount: number;
  runningThreadIds: ReadonlySet<string>;
  workspaceFileCount: number;
  workspaceConfig: WorkspaceConfigStore;
  quickStartGuide?: ReactNode;
  capabilitiesReady: boolean;
  onCreateSession: () => void;
  onOpenConfigPanel: (panel: WorkspaceConfigPanelKey) => void;
  onOpenDataLinkPanel: () => void;
  onOpenFilesPanel: () => void;
  onQueryChange: (value: string) => void;
  onToggleCollapse: () => void;
  onSelectSession: (sessionId: string) => void;
  onRenameSession: (sessionId: string, title: string) => void;
  onDeleteSession: (sessionId: string) => void;
  onTogglePinSession: (sessionId: string) => void;
};

function SessionPane({
  activeSessionId,
  activeConfigPanel,
  activeDataLinkPanel,
  activeFilesPanel,
  collapsed,
  leftPanelWidth,
  isLeftPanelResizing,
  onLeftPanelResizeStart,
  onResetLeftPanelWidth,
  filteredSessions,
  query,
  sessionCount,
  runningThreadIds,
  workspaceFileCount,
  workspaceConfig,
  quickStartGuide,
  capabilitiesReady,
  onCreateSession,
  onOpenConfigPanel,
  onOpenDataLinkPanel,
  onOpenFilesPanel,
  onQueryChange,
  onToggleCollapse,
  onSelectSession,
  onRenameSession,
  onDeleteSession,
  onTogglePinSession,
}: SessionPaneProps) {
  const t = useT();
  const collapsedRailCopy = getCollapsedWorkspaceRailCopy(t);
  const previewClassNames = getCollapsedWorkspacePreviewClassNames();

  if (collapsed) {
    return (
      <aside
        aria-label={collapsedRailCopy.railLabel}
        className="relative z-30 flex h-full min-h-0 w-14 min-w-14 max-w-14 shrink-0 flex-col items-center border-r border-border bg-surface-subtle py-3"
      >
        <div className="group relative">
          <button
            type="button"
            onClick={onToggleCollapse}
            title={collapsedRailCopy.expandLabel}
            aria-label={collapsedRailCopy.expandLabel}
            className="flex h-9 w-9 cursor-pointer items-center justify-center rounded-lg border border-border bg-surface text-foreground shadow-[var(--shadow-card)] transition-colors duration-200 hover:bg-surface-subtle"
          >
            <SidebarToggleIcon />
          </button>
          <div className={previewClassNames.panel} aria-label="Workspace sidebar preview">
            <SessionPaneContent
              activeSessionId={activeSessionId}
              activeConfigPanel={activeConfigPanel}
              activeDataLinkPanel={activeDataLinkPanel}
              activeFilesPanel={activeFilesPanel}
              filteredSessions={filteredSessions}
              query={query}
              sessionCount={sessionCount}
              runningThreadIds={runningThreadIds}
              workspaceFileCount={workspaceFileCount}
              workspaceConfig={workspaceConfig}
              quickStartGuide={quickStartGuide}
              capabilitiesReady={capabilitiesReady}
              onCreateSession={onCreateSession}
              onOpenConfigPanel={onOpenConfigPanel}
              onOpenDataLinkPanel={onOpenDataLinkPanel}
              onOpenFilesPanel={onOpenFilesPanel}
              onQueryChange={onQueryChange}
              onToggleCollapse={onToggleCollapse}
              onSelectSession={onSelectSession}
              onRenameSession={onRenameSession}
              onDeleteSession={onDeleteSession}
              onTogglePinSession={onTogglePinSession}
              preview
            />
          </div>
        </div>
        <DataTaskUserBar
          compact
          quickStartGuide={quickStartGuide}
          onOpenSettings={() => {
            onToggleCollapse();
            onOpenConfigPanel("llm");
          }}
        />
      </aside>
    );
  }

  return (
    <aside
      className="relative z-30 flex h-full min-h-0 shrink-0 flex-col overflow-hidden border-r border-border bg-surface-subtle"
      style={{
        width: leftPanelWidth,
        minWidth: leftPanelWidth,
        maxWidth: leftPanelWidth,
      }}
    >
      <PanelResizeHandle
        edge="right"
        width={leftPanelWidth}
        minWidth={LEFT_PANEL_MIN_WIDTH}
        maxWidth={LEFT_PANEL_MAX_WIDTH}
        label={t("sidebar.resizeSidebar")}
        isResizing={isLeftPanelResizing}
        onResizeStart={onLeftPanelResizeStart}
        onReset={onResetLeftPanelWidth}
      />
      <SessionPaneContent
        activeSessionId={activeSessionId}
        activeConfigPanel={activeConfigPanel}
        activeDataLinkPanel={activeDataLinkPanel}
        activeFilesPanel={activeFilesPanel}
        filteredSessions={filteredSessions}
        query={query}
        sessionCount={sessionCount}
        runningThreadIds={runningThreadIds}
        workspaceFileCount={workspaceFileCount}
        workspaceConfig={workspaceConfig}
        quickStartGuide={quickStartGuide}
        capabilitiesReady={capabilitiesReady}
        onCreateSession={onCreateSession}
        onOpenConfigPanel={onOpenConfigPanel}
        onOpenDataLinkPanel={onOpenDataLinkPanel}
        onOpenFilesPanel={onOpenFilesPanel}
        onQueryChange={onQueryChange}
        onToggleCollapse={onToggleCollapse}
        onSelectSession={onSelectSession}
        onRenameSession={onRenameSession}
        onDeleteSession={onDeleteSession}
        onTogglePinSession={onTogglePinSession}
      />
    </aside>
  );
}

type SessionPaneContentProps = Omit<
  SessionPaneProps,
  | "collapsed"
  | "leftPanelWidth"
  | "isLeftPanelResizing"
  | "onLeftPanelResizeStart"
  | "onResetLeftPanelWidth"
> & {
  preview?: boolean;
};

function SessionPaneContent({
  activeSessionId,
  activeConfigPanel,
  activeDataLinkPanel,
  activeFilesPanel,
  filteredSessions,
  query,
  sessionCount,
  runningThreadIds,
  workspaceFileCount,
  workspaceConfig,
  quickStartGuide,
  capabilitiesReady,
  onCreateSession,
  onOpenConfigPanel,
  onOpenDataLinkPanel,
  onOpenFilesPanel,
  onQueryChange,
  onToggleCollapse,
  onSelectSession,
  onRenameSession,
  onDeleteSession,
  onTogglePinSession,
  preview = false,
}: SessionPaneContentProps) {
  const t = useT();
  const previewClassNames = getCollapsedWorkspacePreviewClassNames();
  const resourceNavGroups = getWorkspaceResourceNavGroups({
    t,
    workspaceConfig,
    workspaceFileCount,
    activeConfigPanel,
    activeDataLinkPanel,
    activeFilesPanel,
    capabilitiesReady,
    supportsFiles: hasCapability("files"),
    supportsKnowledge: isResourcePanelSupported("kb"),
    supportsMcp: isResourcePanelSupported("mcp"),
    supportsSkills: isResourcePanelSupported("skill"),
  });

  const handleResourceAction = (action: WorkspaceResourceNavAction) => {
    if (action.type === "assets") {
      onOpenFilesPanel();
      return;
    }
    if (action.type === "datalink") {
      onOpenDataLinkPanel();
      return;
    }
    onOpenConfigPanel(action.panel);
  };

  return (
    <div
      className={
        preview
          ? previewClassNames.content
          : "flex h-full min-h-0 w-full flex-col overflow-hidden"
      }
    >
      <div className="flex h-16 items-center gap-3 border-b border-border px-4">
        <div className="flex h-9 w-9 items-center justify-center rounded-lg border border-border bg-surface text-sm font-semibold text-foreground shadow-[var(--shadow-card)]">
          D
        </div>
        <div className="min-w-0 flex-1">
          <h1 className="truncate text-sm font-semibold text-foreground">{t("sidebar.brand")}</h1>
          <p className="text-xs text-muted-light">{t("sidebar.sessionCount", { count: sessionCount })}</p>
        </div>
        <button
          type="button"
          onClick={onToggleCollapse}
          title={preview ? t("sidebar.expandToSidebar") : t("sidebar.collapseToRail")}
          aria-label={preview ? t("sidebar.expandToSidebar") : t("sidebar.collapseToRail")}
          className="flex h-8 w-8 shrink-0 cursor-pointer items-center justify-center rounded-lg text-muted-light transition-colors duration-200 hover:bg-surface-subtle hover:text-foreground"
        >
          <SidebarToggleIcon />
        </button>
      </div>

      <div
        data-guide-id="workspace-resources"
        className="border-b border-border px-2.5 pt-1.5 pb-1"
      >
        <div className="mb-0.5 px-0.5">
          <span className={sectionLabelClass}>{t("sidebar.workspaceResources")}</span>
        </div>
        <div className="flex flex-col gap-px">
          {resourceNavGroups.map((group) => (
            <ResourceNavCard
              key={group.id}
              group={group}
              onAction={handleResourceAction}
            />
          ))}
        </div>
      </div>

      <div className="border-b border-border px-2.5 py-2">
        <button
          type="button"
          onClick={onCreateSession}
          className="h-9 w-full cursor-pointer rounded-lg bg-primary text-sm font-semibold text-white transition-colors duration-200 hover:bg-primary-light focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/25"
        >
          {t("sidebar.newTask")}
        </button>
        <label className="mt-2 block">
          <span className="sr-only">{t("sidebar.searchConversations")}</span>
          <input
            value={query}
            onChange={(event) => onQueryChange(event.target.value)}
            className="h-9 w-full rounded-lg border border-border bg-surface px-3 text-sm text-foreground outline-none transition-colors duration-200 placeholder:text-muted-light focus:border-muted-light focus:bg-surface"
            placeholder={t("sidebar.searchConversations")}
          />
        </label>
      </div>

      <div
        className={
          preview
            ? previewClassNames.sessionList
            : "min-h-0 flex-1 overflow-y-auto p-2"
        }
      >
        <div className="px-2 pb-2 text-xs font-semibold text-muted-light">
          {preview ? t("sidebar.history") : t("sidebar.sessions")}
        </div>
        <div className="flex flex-col gap-0.5">
          {filteredSessions.length === 0 ? (
            <p className="px-2 py-3 text-xs text-muted-light">{t("sidebar.noMatchingSessions")}</p>
          ) : (
            filteredSessions.map((session) => (
              <SessionListItem
                key={session.id}
                session={session}
                active={session.id === activeSessionId}
                running={runningThreadIds.has(session.threadId)}
                onSelect={() => onSelectSession(session.id)}
                onRename={(title) => onRenameSession(session.id, title)}
                onDelete={() => onDeleteSession(session.id)}
                onTogglePin={() => onTogglePinSession(session.id)}
              />
            ))
          )}
        </div>
      </div>
      {!preview ? (
        <DataTaskUserBar
          onOpenSettings={() => onOpenConfigPanel("llm")}
          quickStartGuide={quickStartGuide}
        />
      ) : null}
    </div>
  );
}

function WorkspaceFilesLibraryPanel({
  sessionId,
  onBack,
  onFilesChange,
}: {
  sessionId?: string | null;
  onBack: () => void;
  onFilesChange: (files: FileAssetRefDto[]) => void;
}) {
  const t = useT();
  return (
    <div className="flex min-h-0 min-w-0 flex-col overflow-hidden bg-surface">
      <div className="flex h-16 items-center gap-3 border-b border-border px-4">
        <button
          type="button"
          onClick={onBack}
          className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-muted-light transition hover:bg-surface-subtle hover:text-foreground"
          aria-label={t("common.backToWorkspace")}
          title={t("common.backToWorkspace")}
        >
          <ChevronIcon direction="left" />
        </button>
        <div className="min-w-0">
          <h2 className={panelTitleClass}>{t("assets.title")}</h2>
          <p className="text-xs text-muted-light">{t("assets.description")}</p>
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        <WorkspaceFileAssetsPanel sessionId={sessionId} onFilesChange={onFilesChange} />
      </div>
    </div>
  );
}

function WorkspaceConfigPanel({
  panel,
  items,
  workspaceConfig,
  datasourceTypes,
  loading,
  onAdd,
  onBack,
  onSwitchPanel,
  onSaveItem,
  onDeleteItem,
  onTestItem,
  onIntrospect,
  onReindex,
  onUploadKnowledgeFile,
  onListKnowledgeFiles,
  onDeleteKnowledgeFile,
  onRetryKnowledgeFile,
  onReplaceSkill,
  onValidateSkill,
}: {
  panel: WorkspaceConfigPanelKey;
  items: WorkspaceConfigItem[];
  workspaceConfig: WorkspaceConfigStore;
  datasourceTypes: DatasourceTypeDto[];
  loading?: boolean;
  onAdd: (
    payload: {
      name: string;
      description: string;
      enabled?: boolean;
      settings?: Record<string, string>;
    },
    skillFile?: File,
  ) => Promise<string>;
  onBack: () => void;
  onSwitchPanel?: (panel: WorkspaceConfigPanelKey) => void;
  onSaveItem: (item: WorkspaceConfigItem) => Promise<WorkspaceConfigItem>;
  onDeleteItem: (itemId: string) => Promise<void>;
  onTestItem: (itemId: string) => Promise<Record<string, unknown>>;
  onIntrospect?: (itemId: string) => Promise<void>;
  onReindex?: (itemId: string) => Promise<void>;
  onUploadKnowledgeFile?: (itemId: string, file: File) => Promise<void>;
  onListKnowledgeFiles?: (itemId: string) => Promise<{ documents: KnowledgeDocumentDto[] }>;
  onDeleteKnowledgeFile?: (itemId: string, documentId: string) => Promise<void>;
  onRetryKnowledgeFile?: (itemId: string, documentId: string) => Promise<void>;
  onReplaceSkill?: (itemId: string, file: File) => Promise<void>;
  onValidateSkill?: (itemId: string) => Promise<void>;
}) {
  const t = useT();
  const [detailItemId, setDetailItemId] = useState<string | null>(null);
  const [draftItem, setDraftItem] = useState<WorkspaceConfigItem | null>(null);
  const [editDraftItem, setEditDraftItem] = useState<WorkspaceConfigItem | null>(null);
  const [pendingSkillFile, setPendingSkillFile] = useState<File | null>(null);
  const [panelError, setPanelError] = useState<string | null>(null);
  const [actionBusy, setActionBusy] = useState(false);
  const [testResult, setTestResult] = useState<ConfigTestPresentation | null>(null);
  const [dbGalleryOpen, setDbGalleryOpen] = useState(false);
  const [explorerItemId, setExplorerItemId] = useState<string | null>(null);
  const [kbDocumentsItemId, setKbDocumentsItemId] = useState<string | null>(null);

  useEffect(() => {
    setDetailItemId(null);
    setDraftItem(null);
    setEditDraftItem(null);
    setPendingSkillFile(null);
    setPanelError(null);
    setTestResult(null);
    setDbGalleryOpen(false);
    setExplorerItemId(null);
    setKbDocumentsItemId(null);
  }, [panel]);

  useEffect(() => {
    if (!detailItemId || detailItemId === NEW_CONFIG_ITEM_ID) {
      setEditDraftItem(null);
      return;
    }
    const saved = items.find((entry) => entry.id === detailItemId);
    if (!saved) return;
    setEditDraftItem((current) => {
      if (!current || current.id !== detailItemId) {
        return saved;
      }
      if (workspaceConfigItemDraftEquals(current, saved)) {
        return saved;
      }
      return current;
    });
  }, [detailItemId, items]);

  const isCreating = detailItemId === NEW_CONFIG_ITEM_ID;
  const savedItem =
    !isCreating && detailItemId
      ? items.find((item) => item.id === detailItemId) ?? null
      : null;
  const detailItem = isCreating ? draftItem : editDraftItem ?? savedItem;
  const explorerItem =
    panel === "db" && explorerItemId
      ? items.find((entry) => entry.id === explorerItemId) ?? null
      : null;
  const kbDocumentsItem =
    panel === "kb" && kbDocumentsItemId
      ? items.find((entry) => entry.id === kbDocumentsItemId) ?? null
      : null;

  const titles: Record<typeof panel, string> = {
    db: t("configPanel.titles.db"),
    kb: t("configPanel.titles.kb"),
    mcp: t("configPanel.titles.mcp"),
    skill: t("configPanel.titles.skill"),
    llm: t("configPanel.titles.llm"),
  };

  const detailTitles: Record<typeof panel, string> = {
    db: t("configPanel.detailTitles.db"),
    kb: t("configPanel.detailTitles.kb"),
    mcp: t("configPanel.detailTitles.mcp"),
    skill: t("configPanel.detailTitles.skill"),
    llm: t("configPanel.detailTitles.llm"),
  };

  const descriptions: Record<typeof panel, string> = {
    db: t("configPanel.descriptions.db"),
    kb: t("configPanel.descriptions.kb"),
    mcp: t("configPanel.descriptions.mcp"),
    skill: t("configPanel.descriptions.skill"),
    llm: t("configPanel.descriptions.llm"),
  };

  const addLabels: Record<typeof panel, string> = {
    db: t("configPanel.addLabels.db"),
    kb: t("configPanel.addLabels.kb"),
    mcp: t("configPanel.addLabels.mcp"),
    skill: t("configPanel.addLabels.skill"),
    llm: t("configPanel.addLabels.llm"),
  };

  const openCreate = () => {
    if (panel === "db") {
      setDbGalleryOpen(true);
      setExplorerItemId(null);
      return;
    }
    if (panel === "kb") {
      setKbDocumentsItemId(null);
    }
    setDraftItem({
      id: NEW_CONFIG_ITEM_ID,
      name: "",
      description: "",
      enabled: true,
      settings: defaultSettingsForKind(panel),
    });
    setDetailItemId(NEW_CONFIG_ITEM_ID);
  };

  const openDbCreateFromType = (type: DatasourceTypeDto) => {
    setDraftItem({
      id: NEW_CONFIG_ITEM_ID,
      name: t("configPanel.datasourceSuffix", { label: type.label }),
      description: type.description ?? t("configPanel.customDataSource"),
      enabled: true,
      settings: buildDatasourceSettingsForType(type),
    });
    setDbGalleryOpen(false);
    setExplorerItemId(null);
    setDetailItemId(NEW_CONFIG_ITEM_ID);
  };

  const handleHeaderBack = () => {
    if (detailItem) {
      if (panel === "db") {
        onBack();
        return;
      }
      setDetailItemId(null);
      setDraftItem(null);
      setEditDraftItem(null);
      setTestResult(null);
      return;
    }
    if (dbGalleryOpen) {
      setDbGalleryOpen(false);
      return;
    }
    if (explorerItem) {
      setExplorerItemId(null);
      return;
    }
    if (kbDocumentsItem) {
      setKbDocumentsItemId(null);
      return;
    }
    onBack();
  };

  const handleSaveEdit = async () => {
    if (!editDraftItem) return;
    setActionBusy(true);
    setPanelError(null);
    try {
      const saved = await onSaveItem(editDraftItem);
      setEditDraftItem(saved);
    } catch (error) {
      setPanelError(error instanceof Error ? error.message : t("configPanel.saveFailed"));
    } finally {
      setActionBusy(false);
    }
  };

  const handleCancelEdit = () => {
    if (savedItem) {
      setEditDraftItem(savedItem);
    }
    setPanelError(null);
  };

  const handleCreate = async () => {
    if (!draftItem) return;
    const name = draftItem.name.trim();
    if (!name) return;
    setActionBusy(true);
    setPanelError(null);
    try {
      const createdId = await onAdd(
        {
          name,
          description: draftItem.description.trim() || t("configPanel.customConfigurationItem"),
          enabled: draftItem.enabled,
          settings: draftItem.settings,
        },
        panel === "skill" ? pendingSkillFile ?? undefined : undefined,
      );
      setDetailItemId(createdId);
      setDraftItem(null);
      setPendingSkillFile(null);
    } catch (error) {
      setPanelError(error instanceof Error ? error.message : t("configPanel.createFailed"));
    } finally {
      setActionBusy(false);
    }
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <header className="flex h-16 shrink-0 items-center gap-3 border-b border-border px-6">
        <button
          type="button"
          onClick={handleHeaderBack}
          aria-label={detailItem || dbGalleryOpen || explorerItem || kbDocumentsItem ? t("configPanel.backToList", { panel: titles[panel] }) : t("common.backToWorkspace")}
          className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-slate-500 transition hover:bg-slate-100 hover:text-slate-700"
        >
          <ChevronIcon direction="left" />
        </button>
        <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl border border-border bg-surface-subtle text-muted-light">
          <WorkspaceResourceIcon
            icon={
              panel === "kb"
                ? "book"
                : panel === "llm"
                  ? "models"
                  : panel === "db"
                    ? "database"
                    : "tools"
            }
          />
        </span>
        <div className="min-w-0 flex-1">
          <h2 className="truncate text-base font-semibold text-slate-950">
            {detailItem
              ? isCreating
                ? addLabels[panel]
                : detailItem.name || titles[panel]
              : dbGalleryOpen
                ? t("configPanel.chooseDataSource")
                : explorerItem
                  ? explorerItem.name || t("configPanel.dataSourceBrowser")
                : kbDocumentsItem
                  ? kbDocumentsItem.name || t("configPanel.knowledgeDocuments")
              : titles[panel]}
          </h2>
          <p className="text-xs text-slate-500">
            {detailItem
              ? detailTitles[panel]
              : dbGalleryOpen
                ? t("configPanel.pickAdapter")
                : explorerItem
                  ? t("configPanel.browseSchema")
                  : kbDocumentsItem
                    ? t("configPanel.browseKnowledgeDocuments")
                  : t("configPanel.workspaceConfiguration")}
          </p>
        </div>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto px-6 py-5">
        <div className="w-full space-y-4">
          {panelError ? (
            <div className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-800">
              {panelError}
            </div>
          ) : null}
          {loading ? (
            <p className="text-sm text-slate-500">{t("configPanel.loading")}</p>
          ) : null}
          {(panel === "mcp" || panel === "skill") && !detailItem ? (
            <AgentToolsTabs
              activePanel={panel}
              mcpCount={workspaceConfig.mcp.length}
              skillCount={workspaceConfig.skill.length}
              onSelect={(nextPanel) => {
                if (nextPanel !== panel) onSwitchPanel?.(nextPanel);
              }}
            />
          ) : null}

          {dbGalleryOpen ? (
            <DatasourceTypeGallery
              types={datasourceTypes}
              onSelect={openDbCreateFromType}
            />
          ) : explorerItem ? (
            <DatasourceExplorerPanel
              item={explorerItem}
              onBack={() => setExplorerItemId(null)}
              onEdit={() => {
                setExplorerItemId(null);
                setDetailItemId(explorerItem.id);
              }}
              onTest={async () => {
                setActionBusy(true);
                setPanelError(null);
                try {
                  await onTestItem(explorerItem.id);
                } catch (error) {
                  setPanelError(formatConfigTestError(error, t).details[0] ?? t("configPanel.testFailed"));
                } finally {
                  setActionBusy(false);
                }
              }}
              onIntrospect={
                onIntrospect
                  ? async () => {
                      setActionBusy(true);
                      setPanelError(null);
                      try {
                        await onIntrospect(explorerItem.id);
                      } catch (error) {
                        setPanelError(error instanceof Error ? error.message : t("configPanel.schemaSyncFailed"));
                      } finally {
                        setActionBusy(false);
                      }
                    }
                  : undefined
              }
            />
          ) : kbDocumentsItem && onUploadKnowledgeFile && onListKnowledgeFiles ? (
            <KnowledgeDocumentsPanel
              item={kbDocumentsItem}
              onBack={() => setKbDocumentsItemId(null)}
              onEdit={() => {
                setKbDocumentsItemId(null);
                setDetailItemId(kbDocumentsItem.id);
              }}
              onUpload={(file) => onUploadKnowledgeFile(kbDocumentsItem.id, file)}
              onList={() => onListKnowledgeFiles(kbDocumentsItem.id)}
              onReindex={
                onReindex
                  ? async () => {
                      await onReindex(kbDocumentsItem.id);
                    }
                  : undefined
              }
              onDeleteDocument={
                onDeleteKnowledgeFile
                  ? (documentId) => onDeleteKnowledgeFile(kbDocumentsItem.id, documentId)
                  : undefined
              }
              onRetryDocument={
                onRetryKnowledgeFile
                  ? (documentId) => onRetryKnowledgeFile(kbDocumentsItem.id, documentId)
                  : undefined
              }
            />
          ) : detailItem ? (
            <ConfigItemDetailView
              item={detailItem}
              savedItem={savedItem}
              mode={isCreating ? "create" : "edit"}
              panel={panel}
              workspaceConfig={workspaceConfig}
              onCreate={() => {
                void handleCreate();
              }}
              createDisabled={
                actionBusy || (panel === "skill" && isCreating && !pendingSkillFile)
              }
              onUpdate={(patch) => {
                if (isCreating) {
                  setDraftItem((current) =>
                    current
                      ? {
                          ...current,
                          ...patch,
                          settings: patch.settings
                            ? { ...current.settings, ...patch.settings }
                            : current.settings,
                        }
                      : current,
                  );
                  return;
                }
                setEditDraftItem((current) =>
                  current
                    ? {
                        ...current,
                        ...patch,
                        settings: patch.settings
                          ? { ...current.settings, ...patch.settings }
                          : current.settings,
                      }
                    : current,
                );
              }}
              onSave={
                !isCreating
                  ? () => {
                      void handleSaveEdit();
                    }
                  : undefined
              }
              onCancel={!isCreating ? handleCancelEdit : undefined}
              saveBusy={actionBusy}
              onDelete={
                !isCreating && !(panel === "skill" && detailItem.builtin)
                  ? async () => {
                      setActionBusy(true);
                      try {
                        await onDeleteItem(detailItem.id);
                        setDetailItemId(null);
                      } catch (error) {
                        setPanelError(
                          error instanceof Error ? error.message : t("configPanel.deleteFailed"),
                        );
                      } finally {
                        setActionBusy(false);
                      }
                    }
                  : undefined
              }
              onTest={
                !isCreating
                  ? async () => {
                      setActionBusy(true);
                      setPanelError(null);
                      setTestResult(null);
                      try {
                        const result = await onTestItem(detailItem.id);
                        setTestResult(formatConfigTestResult(panel, result, t));
                      } catch (error) {
                        setTestResult(formatConfigTestError(error, t));
                      } finally {
                        setActionBusy(false);
                      }
                    }
                  : undefined
              }
              testBusy={actionBusy}
              testResult={testResult}
              onIntrospect={
                !isCreating && onIntrospect
                  ? async () => {
                      setActionBusy(true);
                      try {
                        await onIntrospect(detailItem.id);
                      } catch (error) {
                        setPanelError(
                          error instanceof Error ? error.message : t("configPanel.schemaSyncFailed"),
                        );
                      } finally {
                        setActionBusy(false);
                      }
                    }
                  : undefined
              }
              onReindex={
                !isCreating && onReindex
                  ? async () => {
                      setActionBusy(true);
                      try {
                        await onReindex(detailItem.id);
                      } catch (error) {
                        setPanelError(
                          error instanceof Error ? error.message : t("configPanel.reindexFailed"),
                        );
                      } finally {
                        setActionBusy(false);
                      }
                    }
                  : undefined
              }
              onUploadKnowledgeFile={
                !isCreating && onUploadKnowledgeFile
                  ? async (file) => {
                      setActionBusy(true);
                      setPanelError(null);
                      try {
                        await onUploadKnowledgeFile(detailItem.id, file);
                      } catch (error) {
                        setPanelError(
                          error instanceof Error ? error.message : t("configPanel.uploadFailed"),
                        );
                        throw error;
                      } finally {
                        setActionBusy(false);
                      }
                    }
                  : undefined
              }
              onReplaceSkill={
                !isCreating && onReplaceSkill
                  ? async (file) => {
                      setActionBusy(true);
                      try {
                        await onReplaceSkill(detailItem.id, file);
                      } catch (error) {
                        setPanelError(
                          error instanceof Error ? error.message : t("configPanel.replaceSkillFailed"),
                        );
                      } finally {
                        setActionBusy(false);
                      }
                    }
                  : undefined
              }
              onValidateSkill={
                !isCreating && onValidateSkill
                  ? async () => {
                      setActionBusy(true);
                      try {
                        await onValidateSkill(detailItem.id);
                      } catch (error) {
                        setPanelError(
                          error instanceof Error ? error.message : t("configPanel.validationFailed"),
                        );
                      } finally {
                        setActionBusy(false);
                      }
                    }
                  : undefined
              }
              onSelectSkillFile={
                isCreating && panel === "skill"
                  ? (file) => setPendingSkillFile(file)
                  : undefined
              }
            />
          ) : (
            <>
              <p className="text-sm leading-6 text-slate-500">{descriptions[panel]}</p>
              {panel === "db" ? (
                <DatasourceConfigList
                  items={items}
                  datasourceTypes={datasourceTypes}
                  testBusy={actionBusy}
                  onAdd={openCreate}
                  onBrowse={(itemId) => setExplorerItemId(itemId)}
                  onEdit={(itemId) => setDetailItemId(itemId)}
                  onTest={(itemId) => {
                    if (actionBusy) return;
                    setActionBusy(true);
                    setPanelError(null);
                    void onTestItem(itemId)
                      .catch((error) => {
                        setPanelError(
                          formatConfigTestError(error, t).details[0] ?? t("configPanel.testFailed"),
                        );
                      })
                      .finally(() => {
                        setActionBusy(false);
                      });
                  }}
                />
              ) : panel === "kb" ? (
                <KnowledgeConfigList
                  items={items}
                  uploadBusy={actionBusy}
                  onAdd={openCreate}
                  onBrowse={(itemId) => setKbDocumentsItemId(itemId)}
                  onEdit={(itemId) => setDetailItemId(itemId)}
                  onUpload={
                    onUploadKnowledgeFile
                      ? async (itemId, file) => {
                          setActionBusy(true);
                          setPanelError(null);
                          try {
                            await onUploadKnowledgeFile(itemId, file);
                          } catch (error) {
                            setPanelError(
                              error instanceof Error ? error.message : t("configPanel.uploadFailed"),
                            );
                            throw error;
                          } finally {
                            setActionBusy(false);
                          }
                        }
                      : undefined
                  }
                  onReindex={
                    onReindex
                      ? async (itemId) => {
                          setActionBusy(true);
                          setPanelError(null);
                          try {
                            await onReindex(itemId);
                          } catch (error) {
                            setPanelError(
                              error instanceof Error ? error.message : t("configPanel.reindexFailed"),
                            );
                          } finally {
                            setActionBusy(false);
                          }
                        }
                      : undefined
                  }
                />
              ) : panel === "mcp" ? (
                <McpConfigList
                  items={items}
                  testBusy={actionBusy}
                  onAdd={openCreate}
                  onEdit={(itemId) => setDetailItemId(itemId)}
                  onTest={(itemId) => {
                    if (actionBusy) return;
                    setActionBusy(true);
                    setPanelError(null);
                    void onTestItem(itemId)
                      .catch((error) => {
                        setPanelError(
                          formatConfigTestError(error, t).details[0] ?? t("configPanel.testFailed"),
                        );
                      })
                      .finally(() => {
                        setActionBusy(false);
                      });
                  }}
                />
              ) : panel === "skill" ? (
                <SkillConfigList
                  items={items}
                  actionBusy={actionBusy}
                  onAdd={openCreate}
                  onEdit={(itemId) => setDetailItemId(itemId)}
                  onValidate={
                    onValidateSkill
                      ? async (itemId) => {
                          setActionBusy(true);
                          setPanelError(null);
                          try {
                            await onValidateSkill(itemId);
                          } catch (error) {
                            setPanelError(
                              error instanceof Error
                                ? error.message
                                : t("configPanel.validationFailed"),
                            );
                          } finally {
                            setActionBusy(false);
                          }
                        }
                      : undefined
                  }
                />
              ) : (
                <ModelConfigList
                  items={items}
                  testBusy={actionBusy}
                  onAdd={openCreate}
                  onEdit={(itemId) => setDetailItemId(itemId)}
                  onTest={(itemId) => {
                    if (actionBusy) return;
                    setActionBusy(true);
                    setPanelError(null);
                    void onTestItem(itemId)
                      .catch((error) => {
                        setPanelError(
                          formatConfigTestError(error, t).details[0] ?? t("configPanel.testFailed"),
                        );
                      })
                      .finally(() => {
                        setActionBusy(false);
                      });
                  }}
                />
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function AgentToolsTabs({
  activePanel,
  mcpCount,
  skillCount,
  onSelect,
}: {
  activePanel: "mcp" | "skill";
  mcpCount: number;
  skillCount: number;
  onSelect: (panel: "mcp" | "skill") => void;
}) {
  const t = useT();
  return (
    <div className="inline-flex rounded-xl border border-border bg-slate-50 p-1">
      {([
        ["mcp", t("configPanel.tabMcp"), mcpCount],
        ["skill", t("configPanel.tabSkills"), skillCount],
      ] as const).map(([panel, label, count]) => (
        <button
          key={panel}
          type="button"
          onClick={() => onSelect(panel)}
          className={[
            "cursor-pointer rounded-lg px-3 py-1.5 text-xs font-semibold transition-colors duration-200",
            activePanel === panel
              ? "bg-white text-slate-950 shadow-sm"
              : "text-slate-500 hover:text-slate-800",
          ].join(" ")}
        >
          {label}
          <span className="ml-1.5 rounded-full border border-border bg-slate-50 px-1.5 py-px text-[10px] text-slate-500">
            {count}
          </span>
        </button>
      ))}
    </div>
  );
}

function DatasourceConfigList({
  items,
  datasourceTypes,
  onAdd,
  onBrowse,
  onEdit,
  onTest,
  testBusy = false,
}: {
  items: WorkspaceConfigItem[];
  datasourceTypes: DatasourceTypeDto[];
  onAdd: () => void;
  onBrowse: (itemId: string) => void;
  onEdit: (itemId: string) => void;
  onTest: (itemId: string) => void;
  testBusy?: boolean;
}) {
  const t = useT();
  const typeLabelByName = new Map(datasourceTypes.map((type) => [type.name, type.label]));

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-border bg-surface-subtle px-4 py-3">
        <div>
          <h3 className="text-sm font-semibold text-slate-950">{t("configPanel.configuredDataSources")}</h3>
          <p className="mt-1 text-xs text-slate-600">
            {t("configPanel.configuredDataSourcesHelp")}
          </p>
        </div>
        <button
          type="button"
          onClick={onAdd}
          className="h-9 cursor-pointer rounded-lg bg-slate-950 px-4 text-sm font-semibold text-white transition-colors duration-200 hover:bg-slate-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-300"
        >
          {t("configPanel.addDataSource")}
        </button>
      </div>

      {items.length === 0 ? (
        <div className="rounded-xl border border-dashed border-slate-300 bg-slate-50 p-6 text-center">
          <p className="text-sm font-medium text-slate-700">{t("configPanel.noDataSources")}</p>
          <p className="mt-1 text-xs text-slate-500">{t("configPanel.startFromGallery")}</p>
        </div>
      ) : (
        <div className={DATASOURCE_CARD_GRID_CLASS}>
          {items.map((item) => {
            const type = item.settings?.type ?? "unknown";
            const typeLabel = typeLabelByName.get(type) ?? type;
            const status = workspaceConfigItemStatusBadge(item, t);
            const connection = summarizeDatasourceConnection(item);
            return (
              <article
                key={item.id}
                className="group rounded-2xl border border-border bg-white p-4 transition-colors duration-200 hover:border-primary-light/30 hover:bg-primary-light/5"
              >
                <div className="flex items-start gap-3">
                  <DatasourceTypeIcon
                    typeName={type}
                    className="flex h-12 w-12 shrink-0 items-center justify-center rounded-2xl border"
                    iconClassName="h-8 w-8 object-contain"
                  />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <h4 className="truncate text-sm font-semibold text-slate-950">{item.name}</h4>
                      {status ? (
                        <span className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] font-medium ${status.className}`}>
                          {status.label}
                        </span>
                      ) : null}
                    </div>
                    <p className="mt-0.5 truncate font-mono text-[10px] text-slate-400">{item.id}</p>
                    <p className="mt-2 truncate text-xs text-slate-600">
                      <span className="font-medium text-slate-800">{typeLabel}</span>
                      {connection ? ` · ${connection}` : ""}
                    </p>
                    <div className="mt-3 flex flex-wrap gap-1.5 text-[10px] text-slate-500">
                      <span className="rounded-full border border-border bg-slate-50 px-2 py-0.5">
                        {item.enabled ? t("configPanel.workspaceDefault") : t("configPanel.disabledByDefault")}
                      </span>
                      {item.hasSecret ? (
                        <span className="rounded-full border border-emerald-100 bg-emerald-50 px-2 py-0.5 text-emerald-700">
                          {t("configPanel.secretSaved")}
                        </span>
                      ) : null}
                      <span className="rounded-full border border-border bg-slate-50 px-2 py-0.5">
                        {t("configPanel.browseSchemaAndData")}
                      </span>
                    </div>
                  </div>
                </div>
                <div className="mt-4 flex flex-wrap justify-end gap-2">
                  <button
                    type="button"
                    disabled={testBusy}
                    onClick={() => onTest(item.id)}
                    className={`${btnSecondaryClass} disabled:opacity-50`}
                  >
                    {t("common.test")}
                  </button>
                  <button type="button" onClick={() => onEdit(item.id)} className={btnSecondaryClass}>
                    {t("common.edit")}
                  </button>
                  <button
                    type="button"
                    onClick={() => onBrowse(item.id)}
                    className={CONFIG_CARD_PRIMARY_BUTTON_CLASS}
                  >
                    {t("common.browse")}
                  </button>
                </div>
              </article>
            );
          })}
        </div>
      )}
    </div>
  );
}

function KnowledgeConfigList({
  items,
  onAdd,
  onBrowse,
  onEdit,
  onUpload,
  onReindex,
  uploadBusy = false,
}: {
  items: WorkspaceConfigItem[];
  onAdd: () => void;
  onBrowse: (itemId: string) => void;
  onEdit: (itemId: string) => void;
  onUpload?: (itemId: string, file: File) => Promise<void>;
  onReindex?: (itemId: string) => Promise<void>;
  uploadBusy?: boolean;
}) {
  const t = useT();
  const inputRefs = useRef<Record<string, HTMLInputElement | null>>({});
  const [uploadingId, setUploadingId] = useState<string | null>(null);

  const handleUpload = async (itemId: string, file: File | undefined) => {
    if (!file || !onUpload) return;
    setUploadingId(itemId);
    try {
      await onUpload(itemId, file);
    } finally {
      setUploadingId(null);
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-border bg-surface-subtle px-4 py-3">
        <div>
          <h3 className="text-sm font-semibold text-slate-950">{t("configPanel.configuredKnowledgeBases")}</h3>
          <p className="mt-1 text-xs text-slate-600">
            {t("configPanel.configuredKnowledgeBasesHelp")}
          </p>
        </div>
        <button
          type="button"
          onClick={onAdd}
          className="h-9 cursor-pointer rounded-lg bg-slate-950 px-4 text-sm font-semibold text-white transition-colors duration-200 hover:bg-slate-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-300"
        >
          {t("configPanel.addKnowledgeBase")}
        </button>
      </div>

      {items.length === 0 ? (
        <div className="rounded-xl border border-dashed border-slate-300 bg-slate-50 p-6 text-center">
          <p className="text-sm font-medium text-slate-700">{t("configPanel.noKnowledgeBases")}</p>
          <p className="mt-1 text-xs text-slate-500">{t("configPanel.startKnowledgeBase")}</p>
        </div>
      ) : (
        <div className={DATASOURCE_CARD_GRID_CLASS}>
          {items.map((item) => {
            const status = workspaceConfigItemStatusBadge(item, t);
            const indexStatus = item.settings?.indexStatus || item.status || "empty";
            const provider = item.settings?.embeddingProvider || "—";
            const model = item.settings?.embeddingModel || "—";
            const busy = uploadBusy || uploadingId === item.id;
            return (
              <article
                key={item.id}
                className="group rounded-2xl border border-border bg-white p-4 transition-colors duration-200 hover:border-primary-light/30 hover:bg-primary-light/5"
              >
                <div className="flex items-start gap-3">
                  <span className="flex h-12 w-12 shrink-0 items-center justify-center rounded-2xl border border-border bg-surface-subtle text-muted-light">
                    <WorkspaceResourceIcon icon="book" />
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <h4 className="truncate text-sm font-semibold text-slate-950">{item.name}</h4>
                      {status ? (
                        <span className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] font-medium ${status.className}`}>
                          {status.label}
                        </span>
                      ) : null}
                    </div>
                    <p className="mt-0.5 truncate font-mono text-[10px] text-slate-400">{item.id}</p>
                    <p className="mt-2 truncate text-xs text-slate-600">
                      <span className="font-medium text-slate-800">{provider}</span>
                      {` · ${model}`}
                    </p>
                    <div className="mt-3 flex flex-wrap gap-1.5 text-[10px] text-slate-500">
                      <span className="rounded-full border border-border bg-slate-50 px-2 py-0.5">
                        {item.enabled ? t("configPanel.workspaceDefault") : t("configPanel.disabledByDefault")}
                      </span>
                      {item.hasSecret ? (
                        <span className="rounded-full border border-emerald-100 bg-emerald-50 px-2 py-0.5 text-emerald-700">
                          {t("configPanel.secretSaved")}
                        </span>
                      ) : null}
                      <span className="rounded-full border border-border bg-slate-50 px-2 py-0.5">
                        {t("configPanel.indexStatusLabel", { status: indexStatus })}
                      </span>
                    </div>
                  </div>
                </div>
                <div className="mt-4 flex flex-wrap justify-end gap-2">
                  {onReindex ? (
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => void onReindex(item.id)}
                      className={`${btnSecondaryClass} disabled:opacity-50`}
                    >
                      {t("configPanel.reindex")}
                    </button>
                  ) : null}
                  <button type="button" onClick={() => onEdit(item.id)} className={btnSecondaryClass}>
                    {t("common.edit")}
                  </button>
                  {onUpload ? (
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => inputRefs.current[item.id]?.click()}
                      className={`${btnSecondaryClass} disabled:opacity-50`}
                    >
                      {uploadingId === item.id
                        ? t("configPanel.uploading")
                        : t("configPanel.uploadAndVectorize")}
                    </button>
                  ) : null}
                  <button
                    type="button"
                    onClick={() => onBrowse(item.id)}
                    className={CONFIG_CARD_PRIMARY_BUTTON_CLASS}
                  >
                    {t("configPanel.viewDocuments")}
                  </button>
                  {onUpload ? (
                    <input
                      ref={(node) => {
                        inputRefs.current[item.id] = node;
                      }}
                      type="file"
                      accept=".txt,.md,.csv,.tsv,.json,.yaml,.yml,.pdf,text/plain,text/markdown,text/csv,application/json,application/pdf"
                      className="hidden"
                      onChange={(event) => {
                        void handleUpload(item.id, event.target.files?.[0]).catch(() => undefined);
                        event.target.value = "";
                      }}
                    />
                  ) : null}
                </div>
              </article>
            );
          })}
        </div>
      )}
    </div>
  );
}

function summarizeMcpConnection(item: WorkspaceConfigItem): string {
  const settings = normalizeMcpSettings(item.settings);
  if (settings.transport === "stdio") {
    return [settings.transport, settings.command].filter(Boolean).join(" · ");
  }
  return [settings.transport, settings.serverUrl || settings.apiUrl].filter(Boolean).join(" · ");
}

function summarizeSkillPackage(item: WorkspaceConfigItem, builtinLabel: string): string {
  const settings = normalizeSkillSettings(item.settings);
  if (item.builtin || settings.packageSource?.startsWith("builtin://")) {
    return builtinLabel;
  }
  return [settings.packageFormat, settings.packageFileName || settings.packageSource || item.description]
    .filter(Boolean)
    .join(" · ");
}

function summarizeModelProfile(item: WorkspaceConfigItem, serverEnvLabel: string): string {
  if (item.builtin) return serverEnvLabel;
  const settings = normalizeLlmSettings(item.settings);
  return [settings.provider, settings.modelName].filter(Boolean).join(" · ");
}

function McpConfigList({
  items,
  onAdd,
  onEdit,
  onTest,
  testBusy = false,
}: {
  items: WorkspaceConfigItem[];
  onAdd: () => void;
  onEdit: (itemId: string) => void;
  onTest: (itemId: string) => void;
  testBusy?: boolean;
}) {
  const t = useT();

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-border bg-surface-subtle px-4 py-3">
        <div>
          <h3 className="text-sm font-semibold text-slate-950">{t("configPanel.configuredMcpServers")}</h3>
          <p className="mt-1 text-xs text-slate-600">{t("configPanel.configuredMcpServersHelp")}</p>
        </div>
        <button
          type="button"
          onClick={onAdd}
          className="h-9 cursor-pointer rounded-lg bg-slate-950 px-4 text-sm font-semibold text-white transition-colors duration-200 hover:bg-slate-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-300"
        >
          {t("configPanel.addMcpServer")}
        </button>
      </div>

      {items.length === 0 ? (
        <div className="rounded-xl border border-dashed border-slate-300 bg-slate-50 p-6 text-center">
          <p className="text-sm font-medium text-slate-700">{t("configPanel.noMcpServers")}</p>
          <p className="mt-1 text-xs text-slate-500">{t("configPanel.startMcpServer")}</p>
        </div>
      ) : (
        <div className={DATASOURCE_CARD_GRID_CLASS}>
          {items.map((item) => {
            const status = workspaceConfigItemStatusBadge(item, t);
            const connection = summarizeMcpConnection(item);
            return (
              <article
                key={item.id}
                className="group rounded-2xl border border-border bg-white p-4 transition-colors duration-200 hover:border-primary-light/30 hover:bg-primary-light/5"
              >
                <div className="flex items-start gap-3">
                  <span className="flex h-12 w-12 shrink-0 items-center justify-center rounded-2xl border border-border bg-surface-subtle text-muted-light">
                    <WorkspaceResourceIcon icon="tools" />
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <h4 className="truncate text-sm font-semibold text-slate-950">{item.name}</h4>
                      {status ? (
                        <span className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] font-medium ${status.className}`}>
                          {status.label}
                        </span>
                      ) : null}
                    </div>
                    <p className="mt-0.5 truncate font-mono text-[10px] text-slate-400">{item.id}</p>
                    <p className="mt-2 truncate text-xs text-slate-600">{connection || item.description}</p>
                    <div className="mt-3 flex flex-wrap gap-1.5 text-[10px] text-slate-500">
                      <span className="rounded-full border border-border bg-slate-50 px-2 py-0.5">
                        {item.enabled ? t("configPanel.workspaceDefault") : t("configPanel.disabledByDefault")}
                      </span>
                      {item.hasSecret ? (
                        <span className="rounded-full border border-emerald-100 bg-emerald-50 px-2 py-0.5 text-emerald-700">
                          {t("configPanel.secretSaved")}
                        </span>
                      ) : null}
                    </div>
                  </div>
                </div>
                <div className="mt-4 flex flex-wrap justify-end gap-2">
                  <button
                    type="button"
                    disabled={testBusy}
                    onClick={() => onTest(item.id)}
                    className={`${btnSecondaryClass} disabled:opacity-50`}
                  >
                    {t("common.test")}
                  </button>
                  <button type="button" onClick={() => onEdit(item.id)} className={CONFIG_CARD_PRIMARY_BUTTON_CLASS}>
                    {t("common.edit")}
                  </button>
                </div>
              </article>
            );
          })}
        </div>
      )}
    </div>
  );
}

function SkillConfigList({
  items,
  onAdd,
  onEdit,
  onValidate,
  actionBusy = false,
}: {
  items: WorkspaceConfigItem[];
  onAdd: () => void;
  onEdit: (itemId: string) => void;
  onValidate?: (itemId: string) => Promise<void>;
  actionBusy?: boolean;
}) {
  const t = useT();
  const builtinLabel = t("configPanel.builtinBadge");

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-border bg-surface-subtle px-4 py-3">
        <div>
          <h3 className="text-sm font-semibold text-slate-950">{t("configPanel.configuredSkills")}</h3>
          <p className="mt-1 text-xs text-slate-600">{t("configPanel.configuredSkillsHelp")}</p>
        </div>
        <button
          type="button"
          onClick={onAdd}
          className="h-9 cursor-pointer rounded-lg bg-slate-950 px-4 text-sm font-semibold text-white transition-colors duration-200 hover:bg-slate-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-300"
        >
          {t("configPanel.addSkill")}
        </button>
      </div>

      {items.length === 0 ? (
        <div className="rounded-xl border border-dashed border-slate-300 bg-slate-50 p-6 text-center">
          <p className="text-sm font-medium text-slate-700">{t("configPanel.noSkills")}</p>
          <p className="mt-1 text-xs text-slate-500">{t("configPanel.startSkill")}</p>
        </div>
      ) : (
        <div className={DATASOURCE_CARD_GRID_CLASS}>
          {items.map((item) => {
            const status = workspaceConfigItemStatusBadge(item, t);
            const summary = summarizeSkillPackage(item, builtinLabel);
            return (
              <article
                key={item.id}
                className="group rounded-2xl border border-border bg-white p-4 transition-colors duration-200 hover:border-primary-light/30 hover:bg-primary-light/5"
              >
                <div className="flex items-start gap-3">
                  <span className="flex h-12 w-12 shrink-0 items-center justify-center rounded-2xl border border-border bg-surface-subtle text-muted-light">
                    <WorkspaceResourceIcon icon="tools" />
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <h4 className="truncate text-sm font-semibold text-slate-950">{item.name}</h4>
                      {status ? (
                        <span className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] font-medium ${status.className}`}>
                          {status.label}
                        </span>
                      ) : null}
                    </div>
                    <p className="mt-0.5 truncate font-mono text-[10px] text-slate-400">{item.id}</p>
                    <p className="mt-2 truncate text-xs text-slate-600">{summary || item.description}</p>
                    <div className="mt-3 flex flex-wrap gap-1.5 text-[10px] text-slate-500">
                      <span className="rounded-full border border-border bg-slate-50 px-2 py-0.5">
                        {item.enabled ? t("configPanel.workspaceDefault") : t("configPanel.disabledByDefault")}
                      </span>
                      {item.builtin ? (
                        <span className="rounded-full border border-border bg-slate-50 px-2 py-0.5">
                          {builtinLabel}
                        </span>
                      ) : null}
                    </div>
                  </div>
                </div>
                <div className="mt-4 flex flex-wrap justify-end gap-2">
                  {onValidate ? (
                    <button
                      type="button"
                      disabled={actionBusy}
                      onClick={() => void onValidate(item.id)}
                      className={`${btnSecondaryClass} disabled:opacity-50`}
                    >
                      {t("configPanel.validateSemantics")}
                    </button>
                  ) : null}
                  <button type="button" onClick={() => onEdit(item.id)} className={CONFIG_CARD_PRIMARY_BUTTON_CLASS}>
                    {t("common.edit")}
                  </button>
                </div>
              </article>
            );
          })}
        </div>
      )}
    </div>
  );
}

function ModelConfigList({
  items,
  onAdd,
  onEdit,
  onTest,
  testBusy = false,
}: {
  items: WorkspaceConfigItem[];
  onAdd: () => void;
  onEdit: (itemId: string) => void;
  onTest: (itemId: string) => void;
  testBusy?: boolean;
}) {
  const t = useT();
  const serverEnvLabel = t("configPanel.serverEnvVars");

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-border bg-surface-subtle px-4 py-3">
        <div>
          <h3 className="text-sm font-semibold text-slate-950">{t("configPanel.configuredModels")}</h3>
          <p className="mt-1 text-xs text-slate-600">{t("configPanel.configuredModelsHelp")}</p>
        </div>
        <button
          type="button"
          onClick={onAdd}
          className="h-9 cursor-pointer rounded-lg bg-slate-950 px-4 text-sm font-semibold text-white transition-colors duration-200 hover:bg-slate-800 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-slate-300"
        >
          {t("configPanel.addModel")}
        </button>
      </div>

      {items.length === 0 ? (
        <div className="rounded-xl border border-dashed border-slate-300 bg-slate-50 p-6 text-center">
          <p className="text-sm font-medium text-slate-700">{t("configPanel.noModels")}</p>
          <p className="mt-1 text-xs text-slate-500">{t("configPanel.startModel")}</p>
        </div>
      ) : (
        <div className={DATASOURCE_CARD_GRID_CLASS}>
          {items.map((item) => {
            const status = workspaceConfigItemStatusBadge(item, t);
            const summary = summarizeModelProfile(item, serverEnvLabel);
            return (
              <article
                key={item.id}
                className="group rounded-2xl border border-border bg-white p-4 transition-colors duration-200 hover:border-primary-light/30 hover:bg-primary-light/5"
              >
                <div className="flex items-start gap-3">
                  <span className="flex h-12 w-12 shrink-0 items-center justify-center rounded-2xl border border-border bg-surface-subtle text-muted-light">
                    <WorkspaceResourceIcon icon="models" />
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <h4 className="truncate text-sm font-semibold text-slate-950">{item.name}</h4>
                      {status ? (
                        <span className={`shrink-0 rounded-full px-2 py-0.5 text-[10px] font-medium ${status.className}`}>
                          {status.label}
                        </span>
                      ) : null}
                    </div>
                    <p className="mt-0.5 truncate font-mono text-[10px] text-slate-400">{item.id}</p>
                    <p className="mt-2 truncate text-xs text-slate-600">{summary || item.description}</p>
                    <div className="mt-3 flex flex-wrap gap-1.5 text-[10px] text-slate-500">
                      <span className="rounded-full border border-border bg-slate-50 px-2 py-0.5">
                        {item.enabled ? t("configPanel.workspaceDefault") : t("configPanel.disabledByDefault")}
                      </span>
                      {item.builtin ? (
                        <span className="rounded-full border border-border bg-slate-50 px-2 py-0.5">
                          {t("configPanel.builtinBadge")}
                        </span>
                      ) : null}
                      {item.hasSecret ? (
                        <span className="rounded-full border border-emerald-100 bg-emerald-50 px-2 py-0.5 text-emerald-700">
                          {t("configPanel.secretSaved")}
                        </span>
                      ) : null}
                    </div>
                  </div>
                </div>
                <div className="mt-4 flex flex-wrap justify-end gap-2">
                  <button
                    type="button"
                    disabled={testBusy}
                    onClick={() => onTest(item.id)}
                    className={`${btnSecondaryClass} disabled:opacity-50`}
                  >
                    {t("common.test")}
                  </button>
                  <button type="button" onClick={() => onEdit(item.id)} className={CONFIG_CARD_PRIMARY_BUTTON_CLASS}>
                    {t("common.edit")}
                  </button>
                </div>
              </article>
            );
          })}
        </div>
      )}
    </div>
  );
}

function ConfigItemDetailView({
  item,
  savedItem,
  mode,
  panel,
  workspaceConfig,
  onCreate,
  createDisabled,
  onUpdate,
  onSave,
  onCancel,
  saveBusy = false,
  onDelete,
  onTest,
  onIntrospect,
  onReindex,
  onUploadKnowledgeFile,
  onReplaceSkill,
  onValidateSkill,
  onSelectSkillFile,
  testBusy = false,
  testResult,
}: {
  item: WorkspaceConfigItem;
  savedItem?: WorkspaceConfigItem | null;
  mode: "create" | "edit";
  panel: WorkspaceConfigPanelKey;
  workspaceConfig: WorkspaceConfigStore;
  onCreate: () => void;
  createDisabled?: boolean;
  onUpdate: (
    patch: Partial<
      Pick<WorkspaceConfigItem, "name" | "description" | "enabled" | "settings">
    >,
  ) => void;
  onSave?: () => void;
  onCancel?: () => void;
  saveBusy?: boolean;
  onDelete?: () => void | Promise<void>;
  onTest?: () => void | Promise<void>;
  onIntrospect?: () => void | Promise<void>;
  onReindex?: () => void | Promise<void>;
  onUploadKnowledgeFile?: (file: File) => void | Promise<void>;
  onReplaceSkill?: (file: File) => void | Promise<void>;
  onValidateSkill?: () => void | Promise<void>;
  onSelectSkillFile?: (file: File) => void;
  testBusy?: boolean;
  testResult?: ConfigTestPresentation | null;
}) {
  const t = useT();
  const [mcpTools, setMcpTools] = useState<Array<Record<string, unknown>> | null>(null);
  const [mcpToolsError, setMcpToolsError] = useState<string | null>(null);

  useEffect(() => {
    if (mode !== "edit" || panel !== "mcp") {
      setMcpTools(null);
      setMcpToolsError(null);
      return;
    }
    let cancelled = false;
    void configApi
      .getMcpTools(item.id)
      .then((tools) => {
        if (!cancelled) {
          setMcpTools(tools);
          setMcpToolsError(null);
        }
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setMcpTools(null);
          setMcpToolsError(error instanceof Error ? error.message : t("configPanel.loadToolsFailed"));
        }
      });
    return () => {
      cancelled = true;
    };
  }, [mode, panel, item.id]);

  const settings: Record<string, string> =
    panel === "llm"
      ? normalizeLlmSettingsExtended(
          item.settings ?? defaultSettingsForKind(panel, item.name),
        )
      : panel === "mcp"
        ? normalizeMcpSettings(item.settings ?? defaultSettingsForKind(panel, item.name))
        : panel === "kb"
          ? normalizeKbSettings(item.settings ?? defaultSettingsForKind(panel, item.name))
          : panel === "skill"
            ? normalizeSkillSettings(
                item.settings ?? defaultSettingsForKind(panel, item.name),
              )
            : (item.settings ?? defaultSettingsForKind(panel, item.name));
  const fields = renderableConfigFields(panel, settings);
  const fieldOptionsContext = {
    workspaceConfig,
    currentItemId: item.id,
  };
  const nameReadOnly = mode === "edit" && panel === "skill" && !!item.builtin;
  const isBuiltinSkill = panel === "skill" && !!item.builtin;
  const hasUploadedSkillPackage =
    panel === "skill" &&
    (settings.packageSource?.startsWith("builtin://") ||
      settings.hasPackageContent === "true" ||
      settings.packageContent.trim().length > 0);

  const configKindLabel =
    panel === "db"
      ? t("configPanel.kindDb")
      : panel === "kb"
        ? t("configPanel.kindKb")
        : panel === "mcp"
          ? t("configPanel.kindMcp")
          : panel === "llm"
            ? t("configPanel.kindModel")
            : t("configPanel.kindSkill");

  const notes: Record<WorkspaceConfigPanelKey, string> = {
    db: t("configPanel.notesDb"),
    kb: t("configPanel.notesKb"),
    mcp: t("configPanel.notesMcp"),
    llm: t("configPanel.notesLlm"),
    skill: isBuiltinSkill
      ? t("configPanel.notesSkillBuiltin")
      : t("configPanel.notesSkillCustom"),
  };

  const createDisabledFinal =
    Boolean(createDisabled) ||
    !isWorkspaceConfigItemValid(panel, item, settings);
  const createLabel = panel === "skill" ? t("configPanel.importSkill") : t("configPanel.createItem");
  const isDirty =
    mode === "edit" &&
    savedItem != null &&
    !workspaceConfigItemDraftEquals(item, savedItem);
  const saveDisabledFinal =
    !isDirty ||
    saveBusy ||
    !isWorkspaceConfigItemValid(panel, item, settings);

  return (
    <div className={`space-y-4 ${CONFIG_DETAIL_MAX_WIDTH_CLASS}`}>
      {panel === "skill" && <SkillConfigProtocolHint builtin={isBuiltinSkill} />}

      {!isBuiltinSkill && panel === "skill" && (
        <SkillPackageUpload
          fileName={settings.packageFileName}
          onImport={(pkg) => {
            onUpdate({
              name: pkg.name,
              description: pkg.description,
              settings: skillSettingsFromPackage(pkg),
            });
          }}
          onSelectFile={onSelectSkillFile ?? onReplaceSkill}
        />
      )}

      {panel === "kb" && mode === "edit" && onUploadKnowledgeFile ? (
        <KnowledgeFileUpload onUpload={onUploadKnowledgeFile} />
      ) : null}

      {mode === "edit" && (onTest || onIntrospect || onReindex || onValidateSkill || onDelete) ? (
        <section className="flex flex-wrap gap-2 rounded-xl border border-border bg-white px-5 py-4">
          {onTest ? (
            <ActionButton
              label={testBusy ? t("configPanel.testing") : t("configPanel.testConnection")}
              disabled={testBusy}
              onClick={() => void onTest()}
            />
          ) : null}
          {onIntrospect ? (
            <ActionButton label={t("configPanel.syncSchema")} onClick={() => void onIntrospect()} />
          ) : null}
          {onReindex ? (
            <ActionButton label={t("configPanel.reindex")} onClick={() => void onReindex()} />
          ) : null}
          {onValidateSkill ? (
            <ActionButton label={t("configPanel.validateSemantics")} onClick={() => void onValidateSkill()} />
          ) : null}
          {onDelete ? (
            <ActionButton label={t("common.delete")} tone="danger" onClick={() => void onDelete()} />
          ) : null}
        </section>
      ) : null}

      {testResult ? <ConfigTestResultCard result={testResult} /> : null}

      <div className="rounded-xl border border-border bg-white px-5 py-4">
        <div className="space-y-3">
          <EditableField
            label={t("common.name")}
            value={item.name}
            readOnly={nameReadOnly}
            required
            placeholder={t("configPanel.enterName", { kind: configKindLabel })}
            onChange={(name) => onUpdate({ name })}
          />
          <EditableField
            label={t("common.description")}
            value={item.description}
            multiline
            placeholder={t("configPanel.shortDescription")}
            onChange={(description) => onUpdate({ description })}
          />
        </div>
      </div>

      {mode === "edit" && (
        <DetailField label={t("configPanel.configurationId")} value={item.id} />
      )}

      {(panel !== "skill" || hasUploadedSkillPackage) && (
        <section className="space-y-3 rounded-xl border border-border bg-slate-50 px-5 py-4">
          <h4 className="text-xs font-semibold uppercase tracking-[0.06em] text-slate-400">
            {panel === "skill" ? t("configPanel.packageInformation") : t("configPanel.configurationDetails")}
          </h4>
          {panel === "llm" && (
            <LlmConfigProtocolHint builtin={!!item.builtin && mode === "edit"} />
          )}
          {panel === "mcp" && <McpConfigProtocolHint />}
          {panel === "mcp" && mode === "edit" ? (
            <McpToolsManifest tools={mcpTools} error={mcpToolsError} />
          ) : null}
          <div className="grid gap-3 sm:grid-cols-2">
            {fields.map((field) => {
              const isSecretField =
                field.inputType === "password" ||
                field.key === "apiKey" ||
                field.key === "embeddingApiKey";
              const secretPlaceholder =
                mode === "edit" &&
                item.hasSecret &&
                isSecretField &&
                !(settings[field.key] ?? "").trim()
                  ? t("configPanel.secretKeepPlaceholder")
                  : undefined;
              const lockField = panel === "skill";
              const pending = lockField && isFieldPending(field, settings);
              const disabled = lockField ? isFieldDisabled(field, item, settings) : false;
              const options = translateConfigFieldOptions(
                panel,
                field,
                resolveConfigFieldOptions(field, fieldOptionsContext),
                t,
              );
              const fieldCopy = translateConfigField(panel, field, t);
              if (panel === "db" && field.key === "filePath") {
                return (
                  <DatasourceFilePathField
                    key={field.key}
                    label={fieldCopy.label}
                    value={settings.filePath ?? ""}
                    uploadedFileName={settings.uploadedFileName ?? ""}
                    placeholder={secretPlaceholder ?? fieldCopy.placeholder}
                    helpText={fieldCopy.helpText}
                    required={field.required && !pending}
                    datasourceType={settings.type ?? "duckdb"}
                    onChange={(value) => onUpdate({ settings: { filePath: value } })}
                    onUploaded={(result) =>
                      onUpdate({
                        settings: {
                          filePath: result.path,
                          uploadedFileName: result.originalName,
                        },
                      })
                    }
                  />
                );
              }
              return (
              <EditableField
                key={field.key}
                label={fieldCopy.label}
                value={settings[field.key] ?? ""}
                placeholder={secretPlaceholder ?? fieldCopy.placeholder}
                helpText={fieldCopy.helpText}
                inputType={field.inputType}
                options={options}
                isOptionPending={
                  lockField ? (value) => isSelectOptionPending(field, value) : undefined
                }
                fullWidth={field.fullWidth}
                required={field.required && !pending}
                readOnly={lockField ? (field.readOnly?.(item) ?? false) : false}
                disabled={disabled}
                pending={pending}
                onChange={(value) =>
                  onUpdate({ settings: { [field.key]: value } })
                }
              />
              );
            })}
          </div>
        </section>
      )}

      <section className="rounded-xl border border-border bg-white px-5 py-4">
        <h4 className="text-xs font-semibold uppercase tracking-[0.06em] text-slate-400">
          {t("common.notes")}
        </h4>
        <p className="mt-2 text-sm leading-6 text-slate-600">{notes[panel]}</p>
        {mode === "edit" && (
          <p className="mt-2 text-xs text-slate-400">
            {item.builtin ? t("configPanel.builtinItem") : t("configPanel.customItem")}
            {" · "}
            {t("configPanel.availableByDefault")}
          </p>
        )}
      </section>

      {mode === "create" && (
        <div className="flex justify-end">
          <button
            type="button"
            disabled={createDisabledFinal}
            onClick={onCreate}
            className="h-9 rounded-lg bg-code-bg px-4 text-sm font-medium text-white transition hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {createLabel}
          </button>
        </div>
      )}

      {mode === "edit" && onSave && onCancel ? (
        <div className="flex flex-wrap items-center justify-end gap-2 border-t border-border pt-4">
          {isDirty ? (
            <span className="mr-auto text-xs text-amber-700">{t("configPanel.unsavedChanges")}</span>
          ) : null}
          <ActionButton
            label={t("common.cancel")}
            disabled={!isDirty || saveBusy}
            onClick={onCancel}
          />
          <button
            type="button"
            disabled={saveDisabledFinal}
            onClick={onSave}
            className="h-9 rounded-lg bg-code-bg px-4 text-sm font-medium text-white transition hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {saveBusy ? t("common.saving") : t("common.save")}
          </button>
        </div>
      ) : null}
    </div>
  );
}

function ActionButton({
  label,
  tone = "default",
  onClick,
  disabled = false,
}: {
  label: string;
  tone?: "default" | "danger";
  onClick: () => void;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={[
        "h-8 rounded-lg px-3 text-xs font-medium transition disabled:cursor-not-allowed disabled:opacity-50",
        tone === "danger"
          ? "border border-rose-200 bg-rose-50 text-rose-800 hover:bg-rose-100"
          : "border border-border bg-white text-slate-700 hover:bg-slate-50",
      ].join(" ")}
    >
      {label}
    </button>
  );
}

function ConfigTestResultCard({ result }: { result: ConfigTestPresentation }) {
  const success = result.tone === "success";
  return (
    <section
      aria-live="polite"
      className={[
        "rounded-xl border px-4 py-3 text-sm",
        success
          ? "border-emerald-200 bg-emerald-50 text-emerald-900"
          : "border-rose-200 bg-rose-50 text-rose-900",
      ].join(" ")}
    >
      <h4 className="font-medium">{result.title}</h4>
      <ul className="mt-1 space-y-0.5 text-xs opacity-90">
        {result.details.map((detail) => (
          <li key={detail}>{detail}</li>
        ))}
      </ul>
    </section>
  );
}

function KnowledgeFileUpload({
  onUpload,
}: {
  onUpload: (file: File) => void | Promise<void>;
}) {
  const t = useT();
  const inputRef = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const accept =
    ".txt,.md,.csv,.tsv,.json,.yaml,.yml,.pdf,text/plain,text/markdown,text/csv,text/tab-separated-values,application/json,application/x-yaml,text/yaml,application/pdf";

  const handleFile = async (file: File | undefined) => {
    if (!file) return;
    setUploading(true);
    setError(null);
    try {
      await onUpload(file);
    } catch (uploadError) {
      setError(
        uploadError instanceof Error ? uploadError.message : t("configPanel.uploadKnowledgeFailed"),
      );
    } finally {
      setUploading(false);
    }
  };

  return (
    <section className="rounded-xl border border-dashed border-slate-300 bg-white px-5 py-4">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h4 className="text-sm font-medium text-slate-900">{t("configPanel.uploadDocument")}</h4>
          <p className="mt-1 text-xs text-slate-500">
            {t("configPanel.uploadDocumentHelp")}
          </p>
        </div>
        <button
          type="button"
          disabled={uploading}
          onClick={() => inputRef.current?.click()}
          className="h-9 rounded-lg border border-border bg-white px-4 text-sm font-medium text-slate-700 transition hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {uploading ? t("configPanel.uploading") : t("configPanel.chooseFile")}
        </button>
      </div>
      {error ? (
        <p className="mt-3 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-800">
          {error}
        </p>
      ) : null}
      <input
        ref={inputRef}
        type="file"
        accept={accept}
        className="hidden"
        onChange={(event) => {
          void handleFile(event.target.files?.[0]);
          event.target.value = "";
        }}
      />
    </section>
  );
}

const DATASOURCE_FILE_ACCEPT: Record<string, string> = {
  access: ".mdb,.accdb",
  csv: ".csv,.tsv,text/csv,text/tab-separated-values",
  duckdb: ".duckdb,.db",
  sqlite: ".sqlite,.sqlite3,.db",
  xlsx: ".xlsx,.xls,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/vnd.ms-excel",
};

function DatasourceFilePathField({
  label,
  value,
  uploadedFileName,
  placeholder,
  helpText,
  required,
  datasourceType,
  onChange,
  onUploaded,
}: {
  label: string;
  value: string;
  uploadedFileName: string;
  placeholder?: string;
  helpText?: string;
  required?: boolean;
  datasourceType: string;
  onChange: (value: string) => void;
  onUploaded: (result: { path: string; originalName: string }) => void;
}) {
  const t = useT();
  const inputRef = useRef<HTMLInputElement>(null);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const accept =
    DATASOURCE_FILE_ACCEPT[datasourceType] ??
    ".duckdb,.db,.sqlite,.sqlite3,.csv,.tsv,.xlsx,.xls,.mdb,.accdb,.parquet";

  const handleFile = async (file: File | undefined) => {
    if (!file) return;
    setUploading(true);
    setError(null);
    try {
      const result = await configApi.uploadDatasourceFile(file);
      onUploaded({ path: result.path, originalName: result.originalName });
    } catch (uploadError) {
      setError(
        uploadError instanceof Error ? uploadError.message : t("configPanel.uploadDatasourceFailed"),
      );
    } finally {
      setUploading(false);
    }
  };

  return (
    <label className="block sm:col-span-2">
      <span className="mb-1 flex items-center gap-2 text-xs font-medium text-slate-600">
        <span>
          {label}
          {required && <span className="ml-0.5 text-rose-500">*</span>}
        </span>
      </span>
      <div className="flex flex-wrap gap-2">
        <input
          type="text"
          value={value}
          placeholder={placeholder}
          onChange={(event) => onChange(event.target.value)}
          className="h-9 min-w-0 flex-1 rounded-lg border border-border bg-white px-3 py-2 text-sm text-slate-900 outline-none transition focus:border-slate-400"
        />
        <button
          type="button"
          disabled={uploading}
          onClick={() => inputRef.current?.click()}
          className="h-9 shrink-0 rounded-lg border border-border bg-white px-3 text-xs font-medium text-slate-700 transition hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {uploading ? t("common.uploading") : t("configPanel.chooseLocalFile")}
        </button>
      </div>
      <input
        ref={inputRef}
        type="file"
        accept={accept}
        className="hidden"
        onChange={(event) => {
          void handleFile(event.target.files?.[0]);
          event.target.value = "";
        }}
      />
      {uploadedFileName ? (
        <span className="mt-1 block text-[11px] leading-4 text-slate-500">
          {t("configPanel.uploadedFile", { name: uploadedFileName })}
        </span>
      ) : null}
      {error ? (
        <span className="mt-1 block text-[11px] leading-4 text-rose-700">{error}</span>
      ) : null}
      {helpText ? (
        <span className="mt-1 block text-[11px] leading-4 text-slate-400">{helpText}</span>
      ) : null}
    </label>
  );
}

function SkillPackageUpload({
  fileName,
  onImport,
  onSelectFile,
}: {
  fileName: string;
  onImport: (pkg: ParsedSkillPackage) => void;
  onSelectFile?: (file: File) => void;
}) {
  const t = useT();
  const inputRef = useRef<HTMLInputElement>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const handleFile = async (file: File | undefined) => {
    if (!file) return;
    setLoading(true);
    setError(null);
    const result = await parseSkillPackageFile(file);
    setLoading(false);
    if ("error" in result) {
      setError(result.error);
      return;
    }
    onSelectFile?.(file);
    onImport(result);
  };

  return (
    <section className="space-y-3 rounded-xl border border-dashed border-slate-300 bg-white px-5 py-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h4 className="text-sm font-medium text-slate-900">{t("configPanel.uploadSkillPackage")}</h4>
          <p className="mt-1 text-xs leading-5 text-slate-500">
            {t("configPanel.uploadSkillPackageHelp")}
          </p>
        </div>
        <button
          type="button"
          disabled={loading}
          onClick={() => inputRef.current?.click()}
          className="h-9 rounded-lg border border-border bg-white px-4 text-sm font-medium text-slate-700 transition hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {loading ? t("common.parsing") : fileName ? t("configPanel.chooseAnotherFile") : t("configPanel.chooseFile")}
        </button>
      </div>
      {fileName && (
        <p className="text-xs text-slate-600">
          {t("configPanel.selectedFile", { name: fileName })}
        </p>
      )}
      {error && (
        <p className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-800">
          {error}
        </p>
      )}
      <input
        ref={inputRef}
        type="file"
        accept=".md,.zip,application/zip"
        className="hidden"
        onChange={(event) => {
          void handleFile(event.target.files?.[0]);
          event.target.value = "";
        }}
      />
    </section>
  );
}

function SkillConfigProtocolHint({ builtin }: { builtin: boolean }) {
  const t = useT();
  return (
    <div className="rounded-lg border border-violet-200 bg-violet-50 px-3 py-2.5 text-xs leading-5 text-violet-900">
      <p className="font-medium">{t("configPanel.skillNotesTitle")}</p>
      <ul className="mt-1 list-inside list-disc space-y-0.5 text-violet-800/90">
        <li>{t("configPanel.skillNotesStructure")}</li>
        <li>{t("configPanel.skillNotesContract")}</li>
        {builtin ? (
          <li>{t("configPanel.skillNotesBuiltin")}</li>
        ) : (
          <li>{t("configPanel.skillNotesCustom")}</li>
        )}
      </ul>
    </div>
  );
}

function McpConfigProtocolHint() {
  const t = useT();
  return (
    <div className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2.5 text-xs leading-5 text-rose-900">
      <p className="font-medium">{t("configPanel.mcpNotesTitle")}</p>
      <ul className="mt-1 list-inside list-disc space-y-0.5 text-rose-800/90">
        <li>{t("configPanel.mcpNotesRemote")}</li>
        <li>{t("configPanel.mcpNotesPersist")}</li>
        <li>{t("configPanel.mcpNotesRuns")}</li>
      </ul>
    </div>
  );
}

function LlmConfigProtocolHint({ builtin }: { builtin: boolean }) {
  const t = useT();
  return (
    <div className="rounded-lg border border-blue-200 bg-blue-50 px-3 py-2.5 text-xs leading-5 text-blue-900">
      <p className="font-medium">{t("configPanel.llmNotesTitle")}</p>
      <ul className="mt-1 list-inside list-disc space-y-0.5 text-blue-800/90">
        <li>{t("configPanel.llmNotesEnv")}</li>
        <li>{t("configPanel.llmNotesPersist")}</li>
        <li>{t("configPanel.llmNotesRuns")}</li>
        {builtin ? <li>{t("configPanel.llmNotesBuiltin")}</li> : null}
      </ul>
    </div>
  );
}

function McpToolsManifest({
  tools,
  error,
}: {
  tools: Array<Record<string, unknown>> | null;
  error: string | null;
}) {
  const t = useT();
  return (
    <div className="rounded-lg border border-slate-200 bg-white px-3 py-2.5 sm:col-span-2">
      <h5 className="text-xs font-semibold text-slate-700">{t("configPanel.toolsManifest")}</h5>
      {error ? (
        <p className="mt-1 text-xs text-rose-600">{error}</p>
      ) : tools === null ? (
        <p className="mt-1 text-xs text-slate-400">{t("common.loading")}</p>
      ) : tools.length === 0 ? (
        <p className="mt-1 text-xs text-slate-500">{t("configPanel.noToolsYet")}</p>
      ) : (
        <ul className="mt-2 max-h-40 space-y-1 overflow-y-auto text-xs text-slate-600">
          {tools.map((tool, index) => {
            const name = typeof tool.name === "string" ? tool.name : `tool-${index + 1}`;
            const description =
              typeof tool.description === "string" ? tool.description : "";
            return (
              <li key={`${name}-${index}`} className="rounded-md bg-slate-50 px-2 py-1">
                <span className="font-medium text-slate-800">{name}</span>
                {description ? (
                  <span className="ml-2 text-slate-500">{description}</span>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

function EditableField({
  label,
  value,
  placeholder,
  helpText,
  readOnly,
  disabled = false,
  pending = false,
  required,
  multiline,
  inputType = "text",
  options,
  isOptionPending,
  fullWidth,
  onChange,
}: {
  label: string;
  value: string;
  placeholder?: string;
  helpText?: string;
  readOnly?: boolean;
  disabled?: boolean;
  pending?: boolean;
  required?: boolean;
  multiline?: boolean;
  inputType?: "text" | "password" | "url" | "select" | "number" | "boolean" | "textarea";
  options?: Array<{ value: string; label: string }>;
  isOptionPending?: (value: string) => boolean;
  fullWidth?: boolean;
  onChange: (value: string) => void;
}) {
  const isLocked = readOnly || disabled;
  const className =
    "w-full rounded-lg border border-border bg-white px-3 py-2 text-sm text-slate-900 outline-none transition read-only:bg-slate-50 read-only:text-slate-500 focus:border-slate-400 disabled:cursor-not-allowed disabled:bg-slate-50 disabled:text-slate-400";
  const wrapperClass = fullWidth ? "sm:col-span-2" : "";

  return (
    <label className={`block ${wrapperClass}`}>
      <span className="mb-1 flex items-center gap-2 text-xs font-medium text-slate-600">
        <span>
          {label}
          {required && <span className="ml-0.5 text-rose-500">*</span>}
        </span>
        {pending ? (
          <span className="rounded-full bg-amber-50 px-2 py-0.5 text-[10px] font-semibold text-amber-700">
            Pending backend
          </span>
        ) : null}
      </span>
      {inputType === "boolean" ? (
        <select
          value={value === "true" ? "true" : "false"}
          disabled={isLocked}
          onChange={(event) => onChange(event.target.value)}
          className={`${className} h-9`}
        >
          <option value="false">No</option>
          <option value="true">Yes</option>
        </select>
      ) : inputType === "select" && options ? (
        <select
          value={value}
          disabled={isLocked}
          onChange={(event) => onChange(event.target.value)}
          className={`${className} h-9`}
        >
          <option value="">Select...</option>
          {options.map((option) => {
            const optionPending = isOptionPending?.(option.value) ?? false;
            return (
              <option
                key={option.value}
                value={option.value}
                disabled={optionPending}
              >
                {option.label}
                {optionPending ? "（Pending backend）" : ""}
              </option>
            );
          })}
        </select>
      ) : multiline ? (
        <textarea
          value={value}
          readOnly={isLocked}
          disabled={disabled}
          placeholder={placeholder}
          rows={3}
          onChange={(event) => onChange(event.target.value)}
          className={`${className} resize-y min-h-[72px]`}
        />
      ) : (
        <input
          type={
            inputType === "password"
              ? "password"
              : inputType === "number"
                ? "number"
                : "text"
          }
          value={value}
          readOnly={readOnly}
          disabled={disabled}
          placeholder={placeholder}
          autoComplete={inputType === "password" ? "off" : undefined}
          onChange={(event) => onChange(event.target.value)}
          className={`${className} h-9`}
        />
      )}
      {helpText && (
        <span className="mt-1 block text-[11px] leading-4 text-slate-400">
          {helpText}
        </span>
      )}
    </label>
  );
}

function DetailField({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-border bg-slate-50 px-3 py-2.5">
      <dt className="text-[10px] font-semibold uppercase tracking-[0.06em] text-slate-400">
        {label}
      </dt>
      <dd className="mt-0.5 break-all text-sm text-slate-800">{value}</dd>
    </div>
  );
}

function ResourceNavCard({
  group,
  onAction,
}: {
  group: WorkspaceResourceNavGroup;
  onAction: (action: WorkspaceResourceNavAction) => void;
}) {
  return (
    <button
      type="button"
      data-guide-id={
        group.action.type === "config" && group.action.panel === "db"
          ? "datasource-config"
          : undefined
      }
      onClick={() => onAction(group.action)}
      className={[
        "group flex min-h-8 w-full cursor-pointer items-center gap-1.5 rounded-md px-1.5 py-1 text-left transition-colors duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/20",
        group.active ? "bg-surface shadow-[var(--shadow-card)]" : "hover:bg-surface/80",
      ].join(" ")}
      aria-current={group.active ? "page" : undefined}
    >
      <span
        className={[
          "flex h-6 w-6 shrink-0 items-center justify-center rounded-md border transition-colors duration-200",
          group.active
            ? "border-primary-light/30 bg-primary-light/10 text-primary"
            : "border-border bg-surface-subtle text-muted-light group-hover:text-foreground",
        ].join(" ")}
        aria-hidden
      >
        <WorkspaceResourceIcon icon={group.icon} />
      </span>
      <span className="min-w-0 flex-1 truncate text-[13px] font-semibold leading-snug text-foreground">
        {group.title}
      </span>
      {group.statusLabel ? (
        <span className="shrink-0 rounded-full border border-border bg-surface-subtle px-1 py-px text-[9px] font-medium leading-snug text-muted-light">
          {group.statusLabel}
        </span>
      ) : null}
      <span className="min-w-12 shrink-0 text-right tabular text-[10px] leading-snug text-muted-light">
        {group.summary}
      </span>
      <span className="shrink-0 text-muted-light transition-colors duration-200 group-hover:text-foreground">
        <ChevronIcon direction="right" />
      </span>
    </button>
  );
}

function WorkspaceResourceIcon({
  icon,
}: {
  icon: WorkspaceResourceNavGroup["icon"] | "models";
}) {
  if (icon === "database") {
    return (
      <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
        <ellipse cx="12" cy="5" rx="7" ry="3" />
        <path d="M5 5v6c0 1.7 3.1 3 7 3s7-1.3 7-3V5" />
        <path d="M5 11v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6" />
      </svg>
    );
  }
  if (icon === "assets") {
    return (
      <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
        <path d="M4 7.5A2.5 2.5 0 0 1 6.5 5h11A2.5 2.5 0 0 1 20 7.5v9A2.5 2.5 0 0 1 17.5 19h-11A2.5 2.5 0 0 1 4 16.5v-9Z" />
        <path d="M8 10h8M8 14h5" />
      </svg>
    );
  }
  if (icon === "book") {
    return (
      <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
        <path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H20v16H7a3 3 0 0 0-3 3V5.5Z" />
        <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" />
      </svg>
    );
  }
  if (icon === "graph") {
    return (
      <svg
        viewBox="0 0 24 24"
        className="h-3.5 w-3.5"
        fill="none"
        stroke="currentColor"
        strokeWidth={1.8}
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <circle cx="6" cy="7" r="2.5" />
        <circle cx="18" cy="6" r="2.5" />
        <circle cx="16" cy="18" r="2.5" />
        <path d="m8.4 6.8 7.2-.6M7.7 9.1l6.7 7.1M17.5 8.4 16.4 15.5" />
      </svg>
    );
  }
  if (icon === "tools") {
    return (
      <svg viewBox="0 0 24 24" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
        <path d="M14.5 6.5 17 4l3 3-2.5 2.5" />
        <path d="m3 21 8.5-8.5" />
        <path d="M12 7a5 5 0 0 0 5 5" />
        <path d="M4 4h5v5H4z" />
        <path d="M16 16h4v4h-4z" />
      </svg>
    );
  }
  return (
    <svg viewBox="0 0 24 24" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
      <path d="M5 7.5A3.5 3.5 0 0 1 8.5 4h7A3.5 3.5 0 0 1 19 7.5v9a3.5 3.5 0 0 1-3.5 3.5h-7A3.5 3.5 0 0 1 5 16.5v-9Z" />
      <path d="M9 8h6" />
      <path d="M9 12h6" />
      <path d="M9 16h3" />
    </svg>
  );
}

function ProcessToolGroupSync({
  liveRun,
  onToolGroupsChange,
}: {
  liveRun: LiveRun;
  onToolGroupsChange: (groups: ProcessToolGroup[]) => void;
}) {
  const chatConfig = useCopilotChatConfiguration();
  const { agent } = useAgent({ agentId: chatConfig?.agentId ?? runtimeAgentId });
  const messages = agent.messages ?? [];
  const groups = useMemo(
    () => buildProcessToolGroups(messages, liveRun),
    [liveRun, messages],
  );

  useEffect(() => {
    onToolGroupsChange(groups);
  }, [groups, onToolGroupsChange]);

  return null;
}

function ChatWelcomeOverlay({
  liveRunStatus,
  onUseExamplePrompt,
}: {
  liveRunStatus: LiveRun["runStatus"];
  onUseExamplePrompt: (prompt: string) => void;
}) {
  const t = useT();
  const chatConfig = useCopilotChatConfiguration();
  const threadId = chatConfig?.threadId;
  const { agent } = useAgent({ agentId: chatConfig?.agentId ?? runtimeAgentId });
  const { isThreadRestoring, isThreadRestored } = useConversationRestoreGate();
  const isRestoringConversation = isThreadRestoring(threadId);
  const hasVisibleMessages = (agent.messages?.length ?? 0) > 0 || liveRunStatus !== "idle";
  // Until the first restore cycle finishes, keep loading — never flash welcome.
  const awaitingFirstRestore = Boolean(threadId) && !isThreadRestored(threadId);

  if (isRestoringConversation || awaitingFirstRestore) {
    return (
      <div
        data-testid="conversation-restore-loading"
        className="pointer-events-none absolute inset-x-0 top-0 bottom-32 z-10 flex items-center justify-center"
      >
        <div className="rounded-lg bg-surface/80 px-3 py-2 text-sm text-muted shadow-[var(--shadow-card)]">
          {t("common.loading")}
        </div>
      </div>
    );
  }

  if (hasVisibleMessages) return null;

  return (
    <div className="pointer-events-none absolute inset-x-0 top-0 bottom-32 z-10 overflow-y-auto">
      <div className="pointer-events-auto min-h-full">
        <DataTaskWelcomeScreen onUsePrompt={onUseExamplePrompt} />
      </div>
    </div>
  );
}

function PendingBranchRunDispatcher({
  pending,
  getRunForwardedProps,
  onDispatched,
  onUserMessageSubmitted,
}: {
  pending: PendingBranchRun | null;
  getRunForwardedProps: () => ReturnType<typeof buildRunForwardedProps>;
  onDispatched: () => void;
  onUserMessageSubmitted: (text: string) => void;
}) {
  const chatConfig = useCopilotChatConfiguration();
  const { agent } = useAgent({ agentId: chatConfig?.agentId ?? runtimeAgentId });
  const { copilotkit } = useCopilotKit();
  const restoredConversation = useConversationBranchSnapshot(chatConfig?.threadId);
  const { isThreadRestoring } = useConversationRestoreGate();
  const isRestoringConversation = isThreadRestoring(chatConfig?.threadId);

  useEffect(() => {
    if (!pending || chatConfig?.threadId !== pending.sessionId) {
      return;
    }
    if (!restoredConversation || agent.isRunning || isRestoringConversation) {
      return;
    }
    const timeout = window.setTimeout(() => {
      const forwardedProps = getRunForwardedProps();
      onUserMessageSubmitted(pending.text);
      agent.setState(buildAgentRunStatePatch(forwardedProps, agent.state));
      agent.addMessage({ id: createClientId("msg"), role: "user", content: pending.text });
      onDispatched();
      void copilotkit.runAgent({ agent, forwardedProps }).catch((error) => {
        reportDataFoundryRunError(error, chatConfig?.threadId);
      });
    }, 0);
    return () => window.clearTimeout(timeout);
  }, [
    agent,
    chatConfig?.threadId,
    copilotkit,
    getRunForwardedProps,
    isRestoringConversation,
    onDispatched,
    onUserMessageSubmitted,
    pending,
    restoredConversation,
  ]);

  return null;
}

function HiddenSessionChatInput() {
  return null;
}

function SessionChatRuntime({
  threadId,
  isActive,
  chatInput: ChatInput,
  capabilitiesReady,
  pendingBranchRun,
  onPendingBranchRunDispatched,
  getRunForwardedProps,
  onUserMessageSubmitted,
  onToolGroupsChange,
  onUseExamplePrompt,
}: {
  threadId: string;
  isActive: boolean;
  chatInput: ComponentType<ComponentProps<typeof DataTaskChatInput>>;
  capabilitiesReady: boolean;
  pendingBranchRun: PendingBranchRun | null;
  onPendingBranchRunDispatched: () => void;
  getRunForwardedProps: () => ReturnType<typeof buildRunForwardedProps>;
  onUserMessageSubmitted: (text: string) => void;
  onToolGroupsChange: (groups: ProcessToolGroup[]) => void;
  onUseExamplePrompt: (prompt: string) => void;
}) {
  const agentId = dataTaskSessionAgentId(threadId);
  const { liveRun } = useLiveRun(threadId);
  const liveRunStatus = liveRun.runStatus;
  const autoScrollMode = useChatAutoScrollMode();

  return (
    <ChatRunStatusContext.Provider value={liveRunStatus}>
    <ChatLiveRunContext.Provider value={liveRun}>
    <CopilotChatConfigurationProvider
      agentId={agentId}
      threadId={threadId}
      hasExplicitThreadId
    >
      <LiveRunEventSubscriber agentId={agentId} threadId={threadId} />
      <SessionConversationRestore
        agentId={agentId}
        capabilitiesReady={capabilitiesReady}
      />
      {isActive ? <SessionConversationScrollRestore agentId={agentId} /> : null}
      {isActive ? (
        <PendingBranchRunDispatcher
          pending={pendingBranchRun}
          getRunForwardedProps={getRunForwardedProps}
          onDispatched={onPendingBranchRunDispatched}
          onUserMessageSubmitted={onUserMessageSubmitted}
        />
      ) : null}
      <SessionArtifactsRestore
        capabilitiesReady={capabilitiesReady}
        threadId={threadId}
      />
      {isActive ? (
        <AgentMessageRenderSync agentId={agentId} runStatus={liveRunStatus} />
      ) : null}
      {isActive ? (
        <ProcessToolGroupSync
          liveRun={liveRun}
          onToolGroupsChange={onToolGroupsChange}
        />
      ) : null}
      <CollaborationInterruptHandler
        key={`collaboration-interrupt-${threadId}`}
        agentId={agentId}
        threadId={threadId}
      />
      <RestoredInterruptHandler
        key={`restored-${threadId}`}
        agentId={agentId}
        threadId={threadId}
        capabilitiesReady={capabilitiesReady}
      />
      <div className="relative flex min-h-0 flex-1">
        {isActive ? (
          <ChatWelcomeOverlay
            liveRunStatus={liveRunStatus}
            onUseExamplePrompt={onUseExamplePrompt}
          />
        ) : null}
        <CopilotChat
          agentId={agentId}
          threadId={threadId}
          key={`copilot-chat-${threadId}`}
          welcomeScreen={false}
          autoScroll={autoScrollMode}
          intelligenceIndicator={{ className: "chat-intelligence-indicator" }}
          messageView={{
            userMessage: StepUserMessage as typeof CopilotChatUserMessage,
            assistantMessage:
              StepAssistantMessage as unknown as typeof CopilotChatAssistantMessage,
            reasoningMessage:
              StepReasoningMessage as unknown as typeof CopilotChatReasoningMessage,
            cursor: { className: "chat-stream-cursor" },
          }}
          input={(isActive ? ChatInput : HiddenSessionChatInput) as typeof CopilotChatInput}
          className={chatPaneClassName}
        />
      </div>
    </CopilotChatConfigurationProvider>
    </ChatLiveRunContext.Provider>
    </ChatRunStatusContext.Provider>
  );
}

function ChatPane({
  activeThreadId,
  sessionAgentsReady,
  sessions,
  title,
  workspaceConfig,
  activeSession,
  liveRunStatus,
  liveRun,
  runningThreadIds,
  chatInput: ChatInput,
  rightPanelOpen,
  onOpenRightPanel,
  onChatColumnWidthChange,
  onToolGroupsChange,
  onUseExamplePrompt,
  capabilitiesReady,
  pendingBranchRun,
  onPendingBranchRunDispatched,
  getRunForwardedProps,
  onUserMessageSubmitted,
}: {
  activeThreadId?: string;
  sessionAgentsReady: boolean;
  sessions: ChatSession[];
  title: string;
  workspaceConfig: WorkspaceConfigStore;
  activeSession: ChatSession | null;
  liveRunStatus: LiveRun["runStatus"];
  liveRun: LiveRun;
  runningThreadIds: ReadonlySet<string>;
  chatInput: ComponentType<ComponentProps<typeof DataTaskChatInput>>;
  rightPanelOpen: boolean;
  onOpenRightPanel: () => void;
  onChatColumnWidthChange: (width: number) => void;
  onToolGroupsChange: (groups: ProcessToolGroup[]) => void;
  onUseExamplePrompt: (prompt: string) => void;
  capabilitiesReady: boolean;
  pendingBranchRun: PendingBranchRun | null;
  onPendingBranchRunDispatched: () => void;
  getRunForwardedProps: () => ReturnType<typeof buildRunForwardedProps>;
  onUserMessageSubmitted: (text: string) => void;
}) {
  const { containerRef, chatColumnWidth } = useChatColumnWidth();
  const [processTimelineCollapsed, setProcessTimelineCollapsed] = useState(false);
  const [schemaPreviewDatasourceId, setSchemaPreviewDatasourceId] = useState<string | null>(null);
  const [mountedThreadIds, setMountedThreadIds] = useState<string[]>([]);
  const schemaPreviewRootRef = useRef<HTMLDivElement>(null);
  const sessionByThreadId = useMemo(
    () => new Map(sessions.map((session) => [session.threadId, session])),
    [sessions],
  );
  const chatThreadIds = useMemo(() => {
    const next: string[] = [];
    const add = (threadId: string | undefined) => {
      if (!threadId || !sessionByThreadId.has(threadId) || next.includes(threadId)) {
        return;
      }
      next.push(threadId);
    };
    mountedThreadIds.forEach(add);
    runningThreadIds.forEach(add);
    add(activeThreadId);
    return next;
  }, [activeThreadId, mountedThreadIds, runningThreadIds, sessionByThreadId]);
  const schemaPreviewDatasource = useMemo(
    () =>
      schemaPreviewDatasourceId
        ? workspaceConfig.db.find((item) => item.id === schemaPreviewDatasourceId) ?? null
        : null,
    [schemaPreviewDatasourceId, workspaceConfig.db],
  );
  const processTimelineCollapse = useMemo(
    () => ({
      collapsed: processTimelineCollapsed,
      toggle: () => setProcessTimelineCollapsed((value) => !value),
    }),
    [processTimelineCollapsed],
  );

  useEffect(() => {
    if (chatColumnWidth > 0) {
      onChatColumnWidthChange(chatColumnWidth);
    }
  }, [chatColumnWidth, onChatColumnWidthChange]);

  useEffect(() => {
    if (!activeThreadId) {
      return;
    }
    setMountedThreadIds((current) =>
      current.includes(activeThreadId) ? current : [...current, activeThreadId],
    );
  }, [activeThreadId]);

  useEffect(() => {
    setMountedThreadIds((current) => current.filter((threadId) => sessionByThreadId.has(threadId)));
  }, [sessionByThreadId]);

  useEffect(() => {
    if (!schemaPreviewDatasourceId) return;
    const handlePointerDown = (event: globalThis.MouseEvent) => {
      if (!schemaPreviewRootRef.current?.contains(event.target as Node)) {
        setSchemaPreviewDatasourceId(null);
      }
    };
    document.addEventListener("mousedown", handlePointerDown);
    return () => document.removeEventListener("mousedown", handlePointerDown);
  }, [schemaPreviewDatasourceId]);

  return (
    <main
      data-chat-column
      className="flex h-full min-h-0 min-w-0 flex-col overflow-hidden bg-surface"
    >
      <ChatRunStatusContext.Provider value={liveRunStatus}>
      <ChatLiveRunContext.Provider value={liveRun}>
      <ProcessTimelineCollapseContext.Provider value={processTimelineCollapse}>
      <header className="flex h-16 items-center justify-between gap-3 border-b border-border bg-surface px-5">
        <div ref={schemaPreviewRootRef} className="relative min-w-0">
          <h2 className="truncate text-base font-semibold text-foreground">
            {title}
          </h2>
          <div className="mt-0.5">
            <SessionHeaderResourceChips
              workspaceConfig={workspaceConfig}
              session={activeSession}
              onPreviewDatasource={(itemId) =>
                setSchemaPreviewDatasourceId((current) => (current === itemId ? null : itemId))
              }
            />
          </div>
          {schemaPreviewDatasource ? (
            <DatasourceSchemaPreviewPopover
              datasourceId={schemaPreviewDatasource.id}
              datasourceName={schemaPreviewDatasource.name || schemaPreviewDatasource.id}
              onClose={() => setSchemaPreviewDatasourceId(null)}
            />
          ) : null}
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <RunStatusPill status={liveRunStatus} />
          {!rightPanelOpen ? (
            <ChatOpenConsoleButton onOpenRightPanel={onOpenRightPanel} />
          ) : null}
        </div>
      </header>
      <div
        ref={containerRef}
        className="flex min-h-0 flex-1 flex-col overflow-hidden"
      >
        <CollaborationResponseBridge />
        {activeThreadId && sessionAgentsReady ? (
          <div className="relative flex min-h-0 flex-1 overflow-hidden">
            {chatThreadIds.map((threadId) => {
              const isActive = threadId === activeThreadId;
              return (
                <div
                  key={threadId}
                  className={[
                    "absolute inset-0 flex min-h-0 min-w-0 flex-col",
                    isActive ? "z-10" : "pointer-events-none z-0 hidden",
                  ].join(" ")}
                  aria-hidden={!isActive}
                >
                  <SessionChatRuntime
                    threadId={threadId}
                    isActive={isActive}
                    chatInput={ChatInput}
                    capabilitiesReady={capabilitiesReady}
                    pendingBranchRun={pendingBranchRun}
                    onPendingBranchRunDispatched={onPendingBranchRunDispatched}
                    getRunForwardedProps={getRunForwardedProps}
                    onUserMessageSubmitted={onUserMessageSubmitted}
                    onToolGroupsChange={onToolGroupsChange}
                    onUseExamplePrompt={onUseExamplePrompt}
                  />
                </div>
              );
            })}
          </div>
        ) : (
          <ChatInitializingState />
        )}
      </div>
      </ProcessTimelineCollapseContext.Provider>
      </ChatLiveRunContext.Provider>
      </ChatRunStatusContext.Provider>
    </main>
  );
}

function formatPayload(value: unknown): string {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function parseJson<T>(value: unknown): T | null {
  if (value && typeof value === "object") return value as T;
  if (typeof value !== "string") return null;
  try {
    const parsed = JSON.parse(value);
    return parsed && typeof parsed === "object" ? (parsed as T) : null;
  } catch {
    return null;
  }
}

function ChatOpenConsoleButton({ onOpenRightPanel }: { onOpenRightPanel: () => void }) {
  const t = useT();
  return (
    <button
      type="button"
      onClick={onOpenRightPanel}
      className="h-8 cursor-pointer rounded-lg bg-foreground px-3 py-1.5 text-xs font-semibold text-white transition-colors duration-200 hover:bg-foreground/90 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-foreground/25"
    >
      {t("chat.openConsole")}
    </button>
  );
}

function RunStatusPill({
  status,
}: {
  status: LiveRun["runStatus"];
}) {
  const t = useT();
  const label =
    status === "completed"
      ? t("common.completed")
      : status === "running"
        ? t("common.running")
        : status === "suspended"
          ? t("common.waiting")
          : status === "failed"
            ? t("common.failed")
            : status === "canceled"
              ? t("common.canceled")
              : t("common.ready");
  const className =
    status === "completed"
      ? "border border-step-success/20 bg-step-success/8 text-step-success"
      : status === "running"
        ? "border border-border bg-surface-subtle text-foreground"
        : status === "suspended"
          ? "border border-border bg-surface-subtle text-foreground"
          : status === "failed"
            ? "border border-step-error/20 bg-step-error/8 text-step-error"
            : status === "canceled"
              ? "border border-border bg-surface-subtle text-muted"
              : "border border-border bg-surface-subtle text-muted";

  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium ${className}`}
    >
      {status === "running" ? (
        <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-foreground" />
      ) : null}
      {label}
    </span>
  );
}
