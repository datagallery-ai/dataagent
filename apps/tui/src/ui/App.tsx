import React, { useState, useEffect, useLayoutEffect, useMemo, useRef, useCallback } from 'react';
import { Box, Text, useApp, useInput, useStdin, useStdout, measureElement, type DOMElement } from 'ink';
import { randomUUID } from 'node:crypto';
import { ChatArea, type ChatAreaRef } from './ChatArea.js';
import { OutputsScreen, OutputsSidebar } from './OutputsView.js';
import { ActivityPanel } from './ActivityPanel.js';
import { EnhancedInputBox, ENHANCED_INPUT_RESERVED_ROWS } from './components/EnhancedInputBox.js';
import { QueuedPromptDisplay } from './components/QueuedPromptDisplay.js';
import {
  WorkspaceFrame,
  availableContentRows,
  estimateControlsRows,
  resolveMainPaneColumns,
} from './workspace-layout.js';
import { useTerminalSize } from './use-terminal-size.js';
import { SessionPicker } from './SessionPicker.js';
import { ResourcePicker, type ResourcePickerItem } from './ResourcePicker.js';
import { HomeSplash } from './HomeSplash.js';
import { StatusBar } from './StatusBar.js';
import { CommandHistory, DEFAULT_COMMANDS } from './keybindings.js';
import { AssistantTextStreamBuffer, type AssistantTextFlush } from './assistant-stream-buffer.js';
import { createWheelScrollDecoder } from '../input/mouse-wheel.js';
import { inkColors } from './theme.js';
import {
  restoreSessionConversation,
  store,
  type TuiAppState,
} from '../state/index.js';
import { runIdFromEvent } from '../state/live-run-state.js';
import {
  persistWorkspaceConfig,
  type WorkspaceConfigItem,
} from '../state/data-task-state.js';
import { getMessageTextContent } from '../state/message-history.js';
import type { AgentClient, AgentMessage, RunAgentInput } from '../protocol/types.js';
import { classifyError, formatErrorMessage, errorLogger } from '../protocol/error-handler.js';
import { commandProcessor } from '../commands/index.js';
import type { CommandContext, CommandResult } from '../commands/types.js';
import { ConfigClientError, type ConfigClient, type Datasource, type SessionListItem, type Skill } from '../config/index.js';
import type { AppExitReason, AuthCommandController } from '../auth/types.js';

interface AppProps {
  client: AgentClient;
  configClient?: ConfigClient | undefined;
  authController?: AuthCommandController | undefined;
  onExit?: ((reason: AppExitReason) => void) | undefined;
  datasourceId: string | undefined;
  initialDatasourceId?: string | undefined;
  initialResume?: {
    enabled: boolean;
    sessionId?: string | undefined;
  } | undefined;
}

type CommandNotice = {
  message: string;
  kind: 'info' | 'error';
};
type RuntimeEvent = { type?: string; [key: string]: unknown };
const REASONING_START_EVENTS = new Set([
  'REASONING_START',
  'REASONING_MESSAGE_START',
  'THINKING_START',
  'THINKING_TEXT_MESSAGE_START',
]);
const REASONING_CONTENT_EVENTS = new Set([
  'REASONING_MESSAGE_CONTENT',
  'REASONING_MESSAGE_CHUNK',
  'THINKING_TEXT_MESSAGE_CONTENT',
  'THINKING_TEXT_MESSAGE_CHUNK',
]);
const REASONING_END_EVENTS = new Set([
  'REASONING_END',
  'REASONING_MESSAGE_END',
  'THINKING_END',
  'THINKING_TEXT_MESSAGE_END',
]);
const SCROLL_FRAME_MS = 33;
const SCROLL_ROWS_PER_FRAME = 9;
const MAX_PENDING_SCROLL_ROWS = 120;
const CTRL_EXIT_PROMPT_DURATION_MS = 1000;
const CLEAR_TERMINAL = '\u001B[2J\u001B[H';
type ClearInputDraft = () => boolean;

function eventType(event: RuntimeEvent): string {
  return typeof event.type === 'string' ? event.type : '';
}

function isReasoningStartEvent(event: RuntimeEvent): boolean {
  return REASONING_START_EVENTS.has(eventType(event));
}

function isReasoningContentEvent(event: RuntimeEvent): boolean {
  return REASONING_CONTENT_EVENTS.has(eventType(event));
}

function isReasoningEndEvent(event: RuntimeEvent): boolean {
  return REASONING_END_EVENTS.has(eventType(event));
}

function isToolCallEvent(event: RuntimeEvent): boolean {
  return eventType(event).startsWith('TOOL_CALL_');
}

function reasoningDeltaFromEvent(event: RuntimeEvent): string | undefined {
  const delta = event.delta ?? event.content ?? event.text ?? event.message;
  return typeof delta === 'string' ? delta : undefined;
}

const createThreadId = (): string => {
  try {
    return randomUUID();
  } catch {
    return `thread-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }
};

const formatDirectory = (directory: string): string => {
  const homeDirectory = process.env.HOME;
  if (!homeDirectory) return directory;
  if (directory === homeDirectory) return '~';
  if (directory.startsWith(`${homeDirectory}/`)) {
    return `~/${directory.slice(homeDirectory.length + 1)}`;
  }
  return directory;
};

const resolveModelName = (state: TuiAppState): string => {
  const model = state.workspaceConfig.llm.find((item) => item.enabled) ?? state.workspaceConfig.llm[0];
  const modelName = model?.settings?.modelName;
  const provider = model?.settings?.provider;

  if (modelName && provider && !provider.includes('服务端')) {
    return `${modelName} (${provider})`;
  }

  return modelName || model?.name || 'server default';
};

const datasourceIdForItem = (item: WorkspaceConfigItem): string => {
  return item.settings?.datasourceId?.trim() || item.id;
};

const firstEnabledDatasourceId = (state: TuiAppState): string | undefined => {
  const item = state.workspaceConfig.db.find((candidate) => candidate.enabled)
    ?? state.workspaceConfig.db[0];
  return item ? datasourceIdForItem(item) : undefined;
};

const firstEnabledSkillId = (state: TuiAppState): string | undefined => {
  return state.workspaceConfig.skill.find((item) => item.enabled)?.id
    ?? state.workspaceConfig.skill[0]?.id;
};

const uniqueStrings = (values: Array<string | undefined>): string[] => {
  return [...new Set(values.filter((value): value is string => !!value))];
};

const enabledItemIds = (items: WorkspaceConfigItem[]): string[] => {
  return items.filter((item) => item.enabled).map((item) => item.id);
};

const buildTuiRunConfig = (
  state: TuiAppState,
  activeDatasourceId: string | undefined,
  activeSkillId: string | undefined,
): Record<string, unknown> => {
  const enabledDatasourceIds = activeDatasourceId
    ? [activeDatasourceId]
    : uniqueStrings(state.workspaceConfig.db
        .filter((item) => item.enabled)
        .map((item) => datasourceIdForItem(item)));
  const enabledSkillIds = uniqueStrings([
    ...(activeSkillId ? [activeSkillId] : []),
    ...enabledItemIds(state.workspaceConfig.skill),
  ]);
  const datasourceSelection = enabledDatasourceIds.length > 0
    ? {
        enabledDatasourceIds,
        ...(activeDatasourceId ? { activeDatasourceId } : {}),
      }
    : {};

  return {
    ...datasourceSelection,
    enabledKnowledgeIds: enabledItemIds(state.workspaceConfig.kb),
    enabledMcpServerIds: enabledItemIds(state.workspaceConfig.mcp),
    enabledSkillIds,
    ...(activeSkillId ? { activeSkillId } : {}),
    mentioned: {
      db: activeDatasourceId ? [activeDatasourceId] : [],
      kb: [],
      mcp: [],
      skill: activeSkillId ? [activeSkillId] : [],
    },
  };
};

const formatSessionApiError = (error: unknown): string => {
  if (
    error instanceof ConfigClientError &&
    error.statusCode === 404 &&
    error.message.includes('Unknown API resource: sessions')
  ) {
    return 'The connected API process does not support /api/v1/sessions. Rebuild/restart the API server, then retry /resume.';
  }
  return error instanceof Error ? error.message : String(error);
};

const formatSkillApiError = (error: unknown): string => {
  return error instanceof Error ? error.message : String(error);
};

const formatDatasourceApiError = (error: unknown): string => {
  return error instanceof Error ? error.message : String(error);
};

const localDatasourcePickerItems = (
  items: WorkspaceConfigItem[],
  activeDatasourceId?: string | undefined,
): ResourcePickerItem[] => {
  return items.map((item) => {
    const id = datasourceIdForItem(item);
    const type = item.settings?.type ?? 'unknown';
    const status = item.status ?? (item.enabled ? 'enabled' : 'disabled');
    const detailParts = [
      `type=${type}`,
      `status=${status}`,
      item.builtin ? 'builtin' : undefined,
    ].filter(Boolean);

    return {
      id,
      name: item.name,
      description: item.description,
      detail: detailParts.join(', '),
      enabled: item.enabled,
      active: id === activeDatasourceId,
    };
  });
};

const apiDatasourcePickerItems = (
  datasources: Datasource[],
  activeDatasourceId?: string | undefined,
): ResourcePickerItem[] => {
  return datasources.map((datasource) => {
    const enabled = datasource.defaultEnabled !== false;
    const detailParts = [
      `type=${datasource.type}`,
      datasource.connectionStatus ? `status=${datasource.connectionStatus}` : undefined,
      datasource.builtin ? 'builtin' : undefined,
    ].filter(Boolean);

    return {
      id: datasource.id,
      name: datasource.name,
      description: datasource.description,
      detail: detailParts.join(', '),
      enabled,
      active: datasource.id === activeDatasourceId,
    };
  });
};

const localSkillPickerItems = (
  items: WorkspaceConfigItem[],
  activeSkillId?: string | undefined,
): ResourcePickerItem[] => {
  return items.map((item) => {
    const format = item.settings?.packageFormat ?? 'unknown';
    const detailParts = [
      `format=${format}`,
      item.enabled ? 'enabled' : 'disabled',
      item.builtin ? 'builtin' : undefined,
    ].filter(Boolean);

    return {
      id: item.id,
      name: item.name,
      description: item.description,
      detail: detailParts.join(', '),
      enabled: item.enabled,
      active: item.id === activeSkillId,
    };
  });
};

const apiSkillPickerItems = (
  skills: Skill[],
  activeSkillId?: string | undefined,
): ResourcePickerItem[] => {
  return skills.map((skill) => {
    const enabled = skill.defaultEnabled !== false;
    const detailParts = [
      `format=${skill.packageFormat}`,
      skill.validationStatus ? `validation=${skill.validationStatus}` : undefined,
      skill.builtin ? 'builtin' : undefined,
    ].filter(Boolean);

    return {
      id: skill.id,
      name: skill.name,
      description: skill.description,
      detail: detailParts.join(', '),
      enabled,
      active: skill.id === activeSkillId,
    };
  });
};

const findSkillPickerItem = (
  items: ResourcePickerItem[],
  requestedId: string,
): ResourcePickerItem | undefined => {
  return items.find((item) => item.id === requestedId)
    ?? items.find((item) => item.name === requestedId)
    ?? items.find((item) => item.id.toLowerCase() === requestedId.toLowerCase());
};

export const App: React.FC<AppProps> = ({
  client,
  configClient,
  authController,
  onExit,
  datasourceId,
  initialDatasourceId,
  initialResume,
}) => {
  const { exit } = useApp();
  const { stdin } = useStdin();
  const { write: writeToStdout } = useStdout();
  const { columns: terminalColumns, rows: terminalRows } = useTerminalSize();
  // Ink 7 renders an exact-height frame without a trailing newline, so the app
  // can safely use the full viewport and keep the status bar on the bottom row.
  const appRows = Math.max(1, terminalRows);
  const workspaceRows = Math.max(0, appRows - 1);
  const [state, setState] = useState<TuiAppState>(store.getState());
  const [inputFocused, setInputFocused] = useState(false);
  const [commandNotice, setCommandNotice] = useState<CommandNotice | null>(null);
  const [logoutConfirm, setLogoutConfirm] = useState<
    | {
        kind: "remote-failed";
        clearLocalOnly: () => Promise<void>;
      }
    | {
        kind: "local-cleanup-failed";
        storePath: string;
        error: string;
        retry: () => Promise<void>;
      }
    | null
  >(null);
  const [activeDatasourceId, setActiveDatasourceId] = useState<string | undefined>(
    () => datasourceId
      ?? initialDatasourceId
      ?? (configClient ? undefined : firstEnabledDatasourceId(store.getState())),
  );
  const [activeSkillId, setActiveSkillId] = useState<string | undefined>(
    () => firstEnabledSkillId(store.getState()),
  );
  const [sessionPickerOpen, setSessionPickerOpen] = useState(false);
  const [pickerSessions, setPickerSessions] = useState<SessionListItem[]>([]);
  const [pickerLoading, setPickerLoading] = useState(false);
  const [pickerError, setPickerError] = useState<string | undefined>(undefined);
  const [resumeLoadingSessionId, setResumeLoadingSessionId] = useState<string | null>(null);
  const [outputsOpen, setOutputsOpen] = useState(false);
  const [datasourcePickerOpen, setDatasourcePickerOpen] = useState(false);
  const [datasourcePickerItems, setDatasourcePickerItems] = useState<ResourcePickerItem[]>([]);
  const [datasourcePickerLoading, setDatasourcePickerLoading] = useState(false);
  const [datasourcePickerError, setDatasourcePickerError] = useState<string | undefined>(undefined);
  const [datasourcePickerWarning, setDatasourcePickerWarning] = useState<string | undefined>(undefined);
  const [skillPickerOpen, setSkillPickerOpen] = useState(false);
  const [skillPickerItems, setSkillPickerItems] = useState<ResourcePickerItem[]>([]);
  const [skillShortcutItems, setSkillShortcutItems] = useState<ResourcePickerItem[]>(
    () => localSkillPickerItems(store.getState().workspaceConfig.skill, activeSkillId),
  );
  const [skillPickerLoading, setSkillPickerLoading] = useState(false);
  const [skillPickerError, setSkillPickerError] = useState<string | undefined>(undefined);
  const [skillPickerWarning, setSkillPickerWarning] = useState<string | undefined>(undefined);
  const [retryCount, setRetryCount] = useState(0);
  const [controlsHeight, setControlsHeight] = useState(0);
  const [reportedInputBoxRows, setReportedInputBoxRows] = useState<number | null>(
    ENHANCED_INPUT_RESERVED_ROWS,
  );
  const [ctrlCPressedOnce, setCtrlCPressedOnce] = useState(false);
  const [compactMode, setCompactMode] = useState(true);
  const [thoughtExpanded, setThoughtExpanded] = useState(false);
  // The home composer is replaced by the chat composer after the first
  // submit, so the history must live above both component instances.
  const inputHistoryRef = useRef(new CommandHistory());
  const chatAreaRef = useRef<ChatAreaRef>(null);
  const mainControlsRef = useRef<DOMElement | null>(null);
  const ctrlCTimerRef = useRef<NodeJS.Timeout | null>(null);
  const applyChatScrollDeltaRef = useRef<(delta: number) => void>(() => {});
  const startupResumeAttempted = useRef(false);
  const activeDatasourceIdRef = useRef(activeDatasourceId);
  const fullRedrawSignatureRef = useRef<string | null>(null);
  const datasourceSelectionLockedRef = useRef(Boolean(datasourceId));
  const defaultDatasourcePromiseRef = useRef<Promise<string | undefined> | null>(null);
  const queuedPromptsRef = useRef<string[]>([]);
  const drainingQueuedPromptRef = useRef(false);
  const handleAgentQueryRef = useRef<((input: string) => Promise<void>) | null>(null);
  const [queuedPrompts, setQueuedPrompts] = useState<string[]>([]);
  const modelName = resolveModelName(state);
  const directory = formatDirectory(process.env.PWD || process.cwd());
  const startup = {
    threadId: state.threadId,
    connectionStatus: state.connectionStatus,
    runStatus: state.runStatus,
    modelName,
    directory,
    datasourceId: activeDatasourceId,
  };
  const visibleMessages = state.messages;
  const isRestoringSession = resumeLoadingSessionId !== null;
  const showLiveActivity = false;
  const isHomeScreen = visibleMessages.length === 0
    && state.messages.length === 0
    && !commandNotice
    && queuedPrompts.length === 0
    && !isRestoringSession
    && !showLiveActivity;
  const transcriptStartup = state.messages.length === 0
    && !commandNotice
    && queuedPrompts.length === 0
    && !isRestoringSession
    ? startup
    : undefined;
  const pickerOpen = sessionPickerOpen || datasourcePickerOpen || skillPickerOpen || outputsOpen;
  const mainPaneColumns = resolveMainPaneColumns({
    columns: terminalColumns,
  });
  const showResumeLoading = isRestoringSession && state.messages.length === 0;
  const showOutputsSidebar = mainPaneColumns.outputsVisible
    && !showLiveActivity
    && !isHomeScreen
    && !showResumeLoading;
  const fullRedrawSignature = [
    isHomeScreen ? 'home' : 'workspace',
    pickerOpen ? 'picker-open' : 'picker-closed',
    showOutputsSidebar ? 'outputs-sidebar' : 'main-only',
    terminalColumns,
    workspaceRows,
    isHomeScreen ? state.connectionStatus : '',
    isHomeScreen ? activeDatasourceId ?? 'no-datasource' : '',
    isHomeScreen ? activeSkillId ?? 'no-skill' : '',
  ].join('\u0000');
  const controlsLayoutKey = [
    'chat',
    isHomeScreen ? 'home' : 'workspace',
    pickerOpen ? 'picker-open' : 'picker-closed',
    commandNotice ? `${commandNotice.kind}:${commandNotice.message}` : 'clean',
    terminalColumns,
    workspaceRows,
    reportedInputBoxRows ?? 'input-unknown',
    queuedPrompts.length,
    state.connectionStatus,
    state.runStatus,
    modelName,
    directory,
    activeDatasourceId ?? 'no-datasource',
    activeSkillId ?? 'no-skill',
  ].join('\u0000');
  const estimatedControlsRowCount = estimateControlsRows({
    commandNotice: Boolean(commandNotice),
    queuedPromptCount: queuedPrompts.length,
    activeTab: 'chat',
    homeScreen: isHomeScreen,
    inputBoxRows: reportedInputBoxRows ?? undefined,
  });
  const controlsRowCountForViewport = Math.max(estimatedControlsRowCount, controlsHeight);
  const scrollableRowCount = availableContentRows(workspaceRows, controlsRowCountForViewport);
  const chatViewportRowCount = scrollableRowCount;
  const inputDisabled = isRestoringSession;
  const inputCommands = useMemo(
    () => uniqueStrings([
      ...DEFAULT_COMMANDS,
      ...skillShortcutItems.map((item) => `/${item.id}`),
    ]),
    [skillShortcutItems],
  );

  const resolveDefaultDatasourceId = useCallback(async (): Promise<string | undefined> => {
    if (!configClient || datasourceSelectionLockedRef.current || activeDatasourceIdRef.current) {
      return activeDatasourceIdRef.current;
    }

    if (!defaultDatasourcePromiseRef.current) {
      defaultDatasourcePromiseRef.current = configClient.getRunDefaults()
        .then((defaults) => defaults.activeDatasourceId ?? defaults.enabledDatasourceIds[0])
        .then((defaultDatasourceId) => {
          if (
            defaultDatasourceId
            && !datasourceSelectionLockedRef.current
            && !activeDatasourceIdRef.current
          ) {
            activeDatasourceIdRef.current = defaultDatasourceId;
            setActiveDatasourceId(defaultDatasourceId);
          }
          return defaultDatasourceId;
        })
        .catch(() => {
          defaultDatasourcePromiseRef.current = null;
          return undefined;
        });
    }

    await defaultDatasourcePromiseRef.current;
    return activeDatasourceIdRef.current;
  }, [configClient]);

  const requestControlsMeasurement = useCallback((inputBoxRows?: number) => {
    if (typeof inputBoxRows === 'number') {
      const nextRows = Math.max(0, Math.ceil(inputBoxRows));
      setReportedInputBoxRows((current) => (
        current === nextRows ? current : nextRows
      ));
    }
  }, []);

  const clearCtrlCExitTimer = useCallback(() => {
    if (ctrlCTimerRef.current) {
      clearTimeout(ctrlCTimerRef.current);
      ctrlCTimerRef.current = null;
    }
  }, []);

  const armCtrlCExit = useCallback(() => {
    clearCtrlCExitTimer();
    setCtrlCPressedOnce(true);
    ctrlCTimerRef.current = setTimeout(() => {
      setCtrlCPressedOnce(false);
      ctrlCTimerRef.current = null;
    }, CTRL_EXIT_PROMPT_DURATION_MS);
  }, [clearCtrlCExitTimer]);

  const exitApplication = useCallback((reason: AppExitReason = 'exit') => {
    clearCtrlCExitTimer();
    setCtrlCPressedOnce(false);
    if ('dispose' in client && typeof client.dispose === 'function') {
      client.dispose();
    }
    onExit?.(reason);
    exit();
  }, [clearCtrlCExitTimer, client, exit, onExit]);

  const handleLogoutAction = useCallback(async () => {
    if (!authController) {
      setCommandNotice({
        message: 'Logout is unavailable because no auth controller was provided.',
        kind: 'error',
      });
      return;
    }
    const result = await authController.logout();
    if (result.kind === 'complete') {
      exitApplication('logout');
      return;
    }
    if (result.kind === 'local-cleanup-failed') {
      setLogoutConfirm({
        kind: 'local-cleanup-failed',
        storePath: result.storePath,
        error: result.error,
        retry: result.retry,
      });
      setCommandNotice({
        message: `Signed out remotely, but local session cleanup failed: ${result.error}`,
        kind: 'error',
      });
      return;
    }
    setLogoutConfirm({ kind: 'remote-failed', clearLocalOnly: result.clearLocalOnly });
    setCommandNotice({
      message: 'Unable to reach the server. The remote session may still be valid.',
      kind: 'error',
    });
  }, [authController, exitApplication]);

  const clearQueuedPrompts = useCallback((): void => {
    queuedPromptsRef.current = [];
    setQueuedPrompts([]);
  }, []);

  const popAllQueuedPrompts = useCallback((): string | null => {
    const currentPrompts = queuedPromptsRef.current;
    if (currentPrompts.length === 0) return null;
    queuedPromptsRef.current = [];
    setQueuedPrompts([]);
    setCommandNotice(null);
    return currentPrompts.join('\n\n');
  }, []);

  const enqueueAgentQuery = useCallback((input: string): void => {
    const nextPrompts = [...queuedPromptsRef.current, input];
    queuedPromptsRef.current = nextPrompts;
    setQueuedPrompts(nextPrompts);
    store.clearInputBuffer();
    setCommandNotice(null);
  }, []);

  const requestCtrlCExit = useCallback((clearInputDraft?: ClearInputDraft) => {
    if (ctrlCPressedOnce) {
      exitApplication();
      return;
    }

    armCtrlCExit();

    if (sessionPickerOpen) {
      setSessionPickerOpen(false);
      return;
    }

    if (datasourcePickerOpen) {
      setDatasourcePickerOpen(false);
      return;
    }

    if (skillPickerOpen) {
      setSkillPickerOpen(false);
      return;
    }

    if (clearInputDraft?.()) {
      return;
    }
  }, [armCtrlCExit, ctrlCPressedOnce, datasourcePickerOpen, exitApplication, sessionPickerOpen, skillPickerOpen]);

  useLayoutEffect(() => {
    const controlsNode = mainControlsRef.current;
    if (!controlsNode) {
      setControlsHeight((current) => (current === 0 ? current : 0));
      return;
    }

    const nextRows = Math.max(0, Math.ceil(measureElement(controlsNode).height));
    setControlsHeight((current) => (current === nextRows ? current : nextRows));
  }, [controlsLayoutKey]);

  useEffect(() => {
    const previousSignature = fullRedrawSignatureRef.current;
    fullRedrawSignatureRef.current = fullRedrawSignature;

    if (previousSignature !== null && previousSignature !== fullRedrawSignature) {
      writeToStdout(CLEAR_TERMINAL);
    }
  }, [fullRedrawSignature, writeToStdout]);

  useEffect(() => {
    activeDatasourceIdRef.current = activeDatasourceId;
  }, [activeDatasourceId]);

  useEffect(() => {
    void resolveDefaultDatasourceId();
  }, [resolveDefaultDatasourceId]);

  useEffect(() => {
    if (!activeDatasourceId && !configClient) {
      setActiveDatasourceId(firstEnabledDatasourceId(state));
    }
    if (!activeSkillId) {
      setActiveSkillId(firstEnabledSkillId(state));
    }
  }, [activeDatasourceId, activeSkillId, configClient, state.workspaceConfig]);

  const scrollChatBy = (delta: number): void => {
    if (delta === 0) return;
    chatAreaRef.current?.scrollBy(delta);
  };

  const jumpToLatest = (): void => {
    chatAreaRef.current?.scrollToBottom();
  };

  applyChatScrollDeltaRef.current = (delta: number): void => {
    if (delta === 0) return;
    chatAreaRef.current?.scrollBy(delta);
  };

  // Subscribe to state changes
  useEffect(() => {
    const unsubscribe = store.subscribe((newState) => {
      setState(newState);
    });
    return unsubscribe;
  }, []);

  useEffect(() => {
    let cancelled = false;
    const localItems = localSkillPickerItems(state.workspaceConfig.skill, activeSkillId);
    setSkillShortcutItems(localItems);

    if (!configClient) {
      return () => {
        cancelled = true;
      };
    }

    configClient.listSkills()
      .then((skills) => {
        if (!cancelled) {
          setSkillShortcutItems(apiSkillPickerItems(skills, activeSkillId));
        }
      })
      .catch(() => {
        if (!cancelled) {
          setSkillShortcutItems(localItems);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [configClient, state.workspaceConfig.skill, activeSkillId]);

  useEffect(() => {
    const decoder = createWheelScrollDecoder();
    let pendingRows = 0;
    let timer: ReturnType<typeof setTimeout> | null = null;

    function schedule(): void {
      if (timer !== null) return;
      timer = setTimeout(flush, SCROLL_FRAME_MS);
    }

    function flush(): void {
      timer = null;

      if (pendingRows === 0) return;
      const direction = Math.sign(pendingRows);
      const rows = Math.min(Math.abs(pendingRows), SCROLL_ROWS_PER_FRAME);
      const delta = direction * rows;

      pendingRows -= delta;
      applyChatScrollDeltaRef.current(delta);

      if (pendingRows !== 0) {
        schedule();
      }
    }

    const onData = (chunk: Buffer | string) => {
      const deltas = decoder.push(chunk.toString());
      if (deltas.length === 0) return;

      for (const delta of deltas) {
        pendingRows = Math.max(
          -MAX_PENDING_SCROLL_ROWS,
          Math.min(MAX_PENDING_SCROLL_ROWS, pendingRows + delta),
        );
      }

      schedule();
    };

    stdin.prependListener('data', onData);
    return () => {
      stdin.off('data', onData);
      decoder.reset();
      if (timer !== null) {
        clearTimeout(timer);
      }
    };
  }, [stdin]);

  // Initialize connection monitoring if client supports it
  useEffect(() => {
    if ('startConnectionMonitoring' in client && typeof client.startConnectionMonitoring === 'function') {
      client.startConnectionMonitoring();
    }

    return () => {
      if ('stopConnectionMonitoring' in client && typeof client.stopConnectionMonitoring === 'function') {
        client.stopConnectionMonitoring();
      }
    };
  }, [client]);

  useEffect(() => {
    if (!initialResume?.enabled || startupResumeAttempted.current) return;
    if (state.connectionStatus !== 'connected') return;
    startupResumeAttempted.current = true;
    void restoreHistoricalSession(initialResume.sessionId, true);
  }, [initialResume?.enabled, initialResume?.sessionId, state.connectionStatus]);

  useEffect(() => () => {
    clearCtrlCExitTimer();
  }, [clearCtrlCExitTimer]);

  async function restoreHistoricalSession(
    requestedSessionId?: string | undefined,
    startup = false,
  ): Promise<void> {
    if (store.getState().runStatus === 'running') {
      setResumeLoadingSessionId(null);
      setCommandNotice({
        kind: 'error',
        message: 'Cannot resume another session while a run is active.',
      });
      return;
    }

    if (!configClient) {
      setResumeLoadingSessionId(null);
      setCommandNotice({
        kind: 'error',
        message: 'Session resume requires a live backend; it is not available in demo mode.',
      });
      return;
    }

    setResumeLoadingSessionId(requestedSessionId ?? 'latest');
    setCommandNotice({
      kind: 'info',
      message: requestedSessionId
        ? `Loading session ${requestedSessionId}...`
        : 'Loading latest session...',
    });

    try {
      let sessionId = requestedSessionId;
      if (!sessionId) {
        const response = await configClient.listSessions({ limit: 1 });
        sessionId = response.sessions[0]?.threadId ?? response.sessions[0]?.id;
        if (!sessionId) {
          setCommandNotice({
            kind: startup ? 'info' : 'error',
            message: 'No server sessions found.',
          });
          return;
        }
      }

      const [conversation, artifactList] = await Promise.all([
        configClient.getSessionConversation(sessionId),
        configClient.listSessionArtifacts(sessionId),
      ]);
      const restored = restoreSessionConversation(conversation, artifactList.artifacts);
      clearQueuedPrompts();
      store.restoreSession(restored);
      chatAreaRef.current?.reset();
      setCommandNotice({
        kind: 'info',
        message: `↺ 已恢复会话${restored.title ? `：${restored.title}` : ''}`,
      });
    } catch (error) {
      setCommandNotice({
        kind: 'error',
        message: `Resume failed: ${formatSessionApiError(error)}`,
      });
    } finally {
      setResumeLoadingSessionId(null);
    }
  }

  async function openSessionPicker(): Promise<void> {
    if (store.getState().runStatus === 'running') {
      setCommandNotice({
        kind: 'error',
        message: 'Cannot resume another session while a run is active.',
      });
      return;
    }

    if (!configClient) {
      setCommandNotice({
        kind: 'error',
        message: 'Session resume requires a live backend; it is not available in demo mode.',
      });
      return;
    }

    setCommandNotice(null);
    setSessionPickerOpen(true);
    setDatasourcePickerOpen(false);
    setSkillPickerOpen(false);
    setOutputsOpen(false);
    setPickerLoading(true);
    setPickerError(undefined);
    setPickerSessions([]);

    try {
      const response = await configClient.listSessions({ limit: 50 });
      setPickerSessions(response.sessions);
    } catch (error) {
      setPickerError(formatSessionApiError(error));
    } finally {
      setPickerLoading(false);
    }
  }

  async function openDatasourcePicker(): Promise<void> {
    setCommandNotice(null);
    setDatasourcePickerOpen(true);
    setSessionPickerOpen(false);
    setSkillPickerOpen(false);
    setOutputsOpen(false);
    setDatasourcePickerLoading(true);
    setDatasourcePickerError(undefined);
    setDatasourcePickerWarning(undefined);
    setDatasourcePickerItems([]);

    const localItems = localDatasourcePickerItems(
      store.getState().workspaceConfig.db,
      activeDatasourceId,
    );

    if (!configClient) {
      setDatasourcePickerItems(localItems);
      setDatasourcePickerLoading(false);
      return;
    }

    try {
      const datasources = await configClient.listDatasources();
      setDatasourcePickerItems(apiDatasourcePickerItems(datasources, activeDatasourceId));
    } catch (error) {
      setDatasourcePickerItems(localItems);
      if (localItems.length > 0) {
        setDatasourcePickerWarning(`Backend datasource list failed: ${formatDatasourceApiError(error)}`);
      } else {
        setDatasourcePickerError(`Failed to load data sources: ${formatDatasourceApiError(error)}`);
      }
    } finally {
      setDatasourcePickerLoading(false);
    }
  }

  async function openSkillPicker(): Promise<void> {
    setCommandNotice(null);
    setSkillPickerOpen(true);
    setSessionPickerOpen(false);
    setDatasourcePickerOpen(false);
    setOutputsOpen(false);
    setSkillPickerLoading(true);
    setSkillPickerError(undefined);
    setSkillPickerWarning(undefined);
    setSkillPickerItems([]);

    const localItems = localSkillPickerItems(
      store.getState().workspaceConfig.skill,
      activeSkillId,
    );

    if (!configClient) {
      setSkillPickerItems(localItems);
      setSkillPickerLoading(false);
      return;
    }

    try {
      const skills = await configClient.listSkills();
      const items = apiSkillPickerItems(skills, activeSkillId);
      setSkillPickerItems(items);
      setSkillShortcutItems(items);
    } catch (error) {
      setSkillPickerItems(localItems);
      setSkillShortcutItems(localItems);
      if (localItems.length > 0) {
        setSkillPickerWarning(`Backend skill list failed: ${formatSkillApiError(error)}`);
      } else {
        setSkillPickerError(`Failed to load skills: ${formatSkillApiError(error)}`);
      }
    } finally {
      setSkillPickerLoading(false);
    }
  }

  async function resolveSkillShortcut(commandName: string): Promise<ResourcePickerItem | undefined> {
    const cachedChoice = findSkillPickerItem(skillShortcutItems, commandName);
    if (cachedChoice) {
      return cachedChoice;
    }

    const localItems = localSkillPickerItems(
      store.getState().workspaceConfig.skill,
      activeSkillId,
    );
    const localChoice = findSkillPickerItem(localItems, commandName);
    if (localChoice) {
      return localChoice;
    }

    if (!configClient) {
      return undefined;
    }

    try {
      const skills = await configClient.listSkills();
      const items = apiSkillPickerItems(skills, activeSkillId);
      setSkillShortcutItems(items);
      return findSkillPickerItem(items, commandName);
    } catch {
      setSkillShortcutItems(localItems);
      return undefined;
    }
  }

  async function executeCommandOrSkillShortcut(
    input: string,
    commandContext: CommandContext,
  ): Promise<CommandResult> {
    const { commandName } = commandProcessor.parseCommand(input);
    if (commandName && !commandProcessor.hasCommand(commandName)) {
      const skill = await resolveSkillShortcut(commandName);
      if (skill) {
        return {
          success: true,
          message: `Skill selected: ${skill.name} (${skill.id})`,
          data: {
            action: 'select_skill',
            skillId: skill.id,
            label: skill.name,
          },
        };
      }
    }

    return commandProcessor.executeCommand(input, commandContext);
  }

  function selectDatasourceForSession(nextDatasourceId: string): void {
    datasourceSelectionLockedRef.current = true;
    activeDatasourceIdRef.current = nextDatasourceId;
    setActiveDatasourceId(nextDatasourceId);

    const currentConfig = store.getState().workspaceConfig;
    let found = false;
    const nextDb = currentConfig.db.map((item) => {
      const itemDatasourceId = datasourceIdForItem(item);
      const selected = item.id === nextDatasourceId || itemDatasourceId === nextDatasourceId;
      if (selected) {
        found = true;
      }
      return { ...item, enabled: selected };
    });

    if (found) {
      const nextConfig = { ...currentConfig, db: nextDb };
      store.setWorkspaceConfig(nextConfig);
      persistWorkspaceConfig(nextConfig);
    }
  }

  function selectSkillForSession(nextSkillId: string): void {
    setActiveSkillId(nextSkillId);

    const currentConfig = store.getState().workspaceConfig;
    let found = false;
    const nextSkills = currentConfig.skill.map((item) => {
      const selected = item.id === nextSkillId;
      if (selected) {
        found = true;
      }
      return { ...item, enabled: selected };
    });

    if (found) {
      const nextConfig = { ...currentConfig, skill: nextSkills };
      store.setWorkspaceConfig(nextConfig);
      persistWorkspaceConfig(nextConfig);
    }
  }

  const clearScreen = (): void => {
    store.clearMessages();
    chatAreaRef.current?.reset();
  };

  const startNewSession = (): void => {
    clearQueuedPrompts();
    store.startNewSession(createThreadId());
    chatAreaRef.current?.reset();
  };

  // Handle global keyboard shortcuts
  useInput((input, key) => {
    if (logoutConfirm) {
      if (input === '1') {
        const action = logoutConfirm.kind === 'remote-failed'
          ? logoutConfirm.clearLocalOnly
          : logoutConfirm.retry;
        setLogoutConfirm(null);
        void action()
          .then(() => exitApplication('logout'))
          .catch((error: unknown) => {
            const message = error instanceof Error ? error.message : String(error);
            setCommandNotice({
              message: `Local session cleanup still failing: ${message}`,
              kind: 'error',
            });
          });
        return;
      }
      if (input === '2' || key.escape) {
        setLogoutConfirm(null);
        setCommandNotice(null);
        return;
      }
      return;
    }

    if (key.ctrl && input === 'c') {
      if (pickerOpen || !inputFocused) {
        requestCtrlCExit();
      }
      return;
    }

    if (!pickerOpen && key.ctrl && input === 'o') {
      setCompactMode((value) => !value);
      return;
    }

    if (!pickerOpen && key.meta && input.toLowerCase() === 't') {
      setThoughtExpanded((value) => !value);
      return;
    }

    // Ignore input when typing in the input box
    if (pickerOpen || inputFocused) return;

    if (key.pageUp) {
      scrollChatBy(Math.max(3, Math.floor(chatViewportRowCount * 0.8)));
      return;
    }

    if (key.pageDown) {
      scrollChatBy(-Math.max(3, Math.floor(chatViewportRowCount * 0.8)));
      return;
    }

    if (key.home) {
      chatAreaRef.current?.scrollToTop();
      return;
    }

    if (key.end) {
      jumpToLatest();
      return;
    }

    // Ctrl+L - Clear screen (reset chat messages)
    if (key.ctrl && input === 'l') {
      clearScreen();
      return;
    }

    // Ctrl+N - New session (reset and create new thread)
    if (key.ctrl && input === 'n') {
      startNewSession();
      return;
    }

  });

  // Handle user input submission
  const handleSubmit = async (input: string) => {
    if (!input.trim()) return;

    const trimmedInput = input.trim();

    // Check if input is a command (starts with /)
    if (commandProcessor.isCommand(trimmedInput)) {
      // Handle command execution
      await handleCommandExecution(trimmedInput);
      return;
    }

    if (store.getState().runStatus === 'running' || drainingQueuedPromptRef.current) {
      enqueueAgentQuery(trimmedInput);
      return;
    }

    // Handle as natural language query to agent
    await handleAgentQuery(trimmedInput);
  };

  // Handle command execution
  const handleCommandExecution = async (input: string) => {
    store.clearInputBuffer();
    setCommandNotice(null);

    try {
      // Prepare command context
      const currentState = store.getState();
      const commandContext: CommandContext = {
        client,
        ...(configClient ? { configClient } : {}),
        ...(authController ? { authController } : {}),
        ...(activeDatasourceId ? { datasourceId: activeDatasourceId } : {}),
        ...(activeSkillId ? { activeSkillId } : {}),
        workspaceConfig: currentState.workspaceConfig,
        state: {
          ...(currentState.threadId !== undefined && { threadId: currentState.threadId }),
          messages: currentState.messages,
        },
      };

      // Execute command
      const result = await executeCommandOrSkillShortcut(input, commandContext);

      // Handle command result
      if (result.success) {
        // Check for special actions
        if (result.data && typeof result.data === 'object') {
          const commandData = result.data as {
            action?: string;
            sessionId?: unknown;
            datasourceId?: unknown;
            skillId?: unknown;
          };
          const action = commandData.action;

          if (action === 'clear_history') {
            store.clearMessages();
            chatAreaRef.current?.reset();
          } else if (action === 'reset_session') {
            clearQueuedPrompts();
            store.startNewSession(createThreadId());
            chatAreaRef.current?.reset();
          } else if (action === 'logout') {
            await handleLogoutAction();
            return;
          } else if (action === 'exit_application') {
            exitApplication('exit');
            return;
          } else if (action === 'open_outputs') {
            setOutputsOpen(true);
            return;
          } else if (action === 'resume_session') {
            const sessionId = typeof commandData.sessionId === 'string'
              ? commandData.sessionId
              : undefined;
            await restoreHistoricalSession(sessionId);
            return;
          } else if (action === 'open_picker' || action === 'list_sessions') {
            await openSessionPicker();
            return;
          } else if (action === 'open_skill_picker') {
            await openSkillPicker();
            return;
          } else if (action === 'open_datasource_picker') {
            await openDatasourcePicker();
            return;
          } else if (action === 'select_datasource') {
            if (typeof commandData.datasourceId === 'string') {
              selectDatasourceForSession(commandData.datasourceId);
            }
          } else if (action === 'select_skill') {
            if (typeof commandData.skillId === 'string') {
              selectSkillForSession(commandData.skillId);
            }
          }
        }

        setCommandNotice({ message: result.message, kind: 'info' });
      } else {
        setCommandNotice({
          message: `Command error: ${result.message}`,
          kind: 'error',
        });
      }
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : String(error);
      setCommandNotice({
        message: `Command execution failed: ${errorMessage}`,
        kind: 'error',
      });
    }
  };

  // Handle agent query execution
  const handleAgentQuery = async (input: string) => {
    setCommandNotice(null);
    const resolvedActiveDatasourceId = activeDatasourceId ?? await resolveDefaultDatasourceId();
    store.addUserMessage(input);
    store.clearInputBuffer();
    // Reset after the message enters history so the startup banner no longer
    // contributes to the viewport slice.
    chatAreaRef.current?.scrollToBottom();

    // Prepare stable run input material. Retry attempts should not append
    // duplicate chat messages or send retry UI text back as model history.
    const currentState = store.getState();
    const threadId = currentState.threadId || createThreadId();
    if (!currentState.threadId) {
      store.setThreadId(threadId);
    }

    const messages: AgentMessage[] = currentState.messages.map(msg => ({
      id: msg.id,
      role: msg.role,
      content: getMessageTextContent(msg),  // Extract text from elements
    }));

    const createRunInput = (): RunAgentInput => {
      const runId = `run-${Date.now()}-${Math.random().toString(36).substring(7)}`;
      const runConfig = buildTuiRunConfig(currentState, resolvedActiveDatasourceId, activeSkillId);
      const context: NonNullable<RunAgentInput['context']> = [
        ...(resolvedActiveDatasourceId
          ? [
              {
                description: 'datasource_id',
                value: resolvedActiveDatasourceId,
              },
            ]
          : []),
        {
          description: 'run_config',
          value: JSON.stringify(runConfig),
        },
      ];

      return {
        threadId,
        runId,
        messages,
        tools: [],
        context,
        state: {
          ...(resolvedActiveDatasourceId
            ? {
                datasourceId: resolvedActiveDatasourceId,
                selectedDatasourceId: resolvedActiveDatasourceId,
              }
            : {}),
          runId,
          runStatus: 'running',
          sessionId: threadId,
          run_config: runConfig,
        },
        forwardedProps: {
          ...(resolvedActiveDatasourceId ? { datasourceId: resolvedActiveDatasourceId } : {}),
          run_config: runConfig,
        },
      };
    };

    const runAttempt = async (attempt: number): Promise<void> => {
      const runInput = createRunInput();
      const runId = runInput.runId;
      const acceptedRunIds = new Set<string>([runId]);

      // Set run status to running
      store.handleLiveRunEvent({ type: 'RUN_STARTED', runId });

      const isCurrentRun = () => {
        const currentRunId = store.getState().runId;
        return currentRunId !== undefined && acceptedRunIds.has(currentRunId);
      };
      const currentAttemptRunId = () => {
        const currentRunId = store.getState().runId;
        return currentRunId !== undefined && acceptedRunIds.has(currentRunId)
          ? currentRunId
          : runId;
      };
      const shouldHandleRunEvent = (event?: RuntimeEvent) => {
        if (!isCurrentRun()) return false;

        const eventRunId = event ? runIdFromEvent(event) : undefined;
        if (!eventRunId) return true;
        if (acceptedRunIds.has(eventRunId)) return true;

        // A backend may normalize the run id for resume/canonical identity.
        // While this attempt is still current, treat the first new run id from
        // this stream as an alias so later terminal events are not dropped.
        acceptedRunIds.add(eventRunId);
        return true;
      };
      const storeCurrentRunEvent = (event: RuntimeEvent) => {
        store.handleLiveRunEvent({ ...event, _clientRunId: currentAttemptRunId() });
      };
      const handleCurrentRunEvent = (event: RuntimeEvent) => {
        if (shouldHandleRunEvent(event)) {
          storeCurrentRunEvent(event);
        }
      };

      if (attempt === 0) {
        store.addAssistantMessage('', true);
      }

      setRetryCount(attempt);
      let receivedText = false;
      const textBuffer = new AssistantTextStreamBuffer();
      let textFlushTimer: ReturnType<typeof setTimeout> | undefined;

      const applyTextFlush = (flush: AssistantTextFlush | null) => {
        if (!flush || !isCurrentRun()) return;
        if (flush.type === 'text') {
          store.updateAssistantMessage(flush.content, flush.isStreaming);
        } else {
          store.finalizeAssistantMessage();
        }
      };

      const flushTextBuffer = (isStreaming = true) => {
        if (textFlushTimer) {
          clearTimeout(textFlushTimer);
          textFlushTimer = undefined;
        }

        applyTextFlush(textBuffer.flush(isStreaming));
      };

      const startNextTextSegment = () => {
        flushTextBuffer();
        textBuffer.markSegmentBoundary();
      };

      const scheduleTextFlush = () => {
        if (textFlushTimer) return;
        textFlushTimer = setTimeout(() => {
          textFlushTimer = undefined;
          flushTextBuffer();
        }, 60);
      };

      try {
        // Stream events from the agent
        for await (const event of client.runAgent(runInput)) {
          const runtimeEvent = event as RuntimeEvent;
          if (!shouldHandleRunEvent(runtimeEvent)) {
            continue;
          }

          // Handle specific event types for message streaming
          if (isReasoningStartEvent(runtimeEvent)) {
            flushTextBuffer();
            store.appendReasoningMessage('', true);
          } else if (isReasoningContentEvent(runtimeEvent)) {
            flushTextBuffer();
            const delta = reasoningDeltaFromEvent(runtimeEvent);
            if (delta !== undefined) {
              store.appendReasoningMessage(delta, true);
            }
          } else if (isReasoningEndEvent(runtimeEvent)) {
            flushTextBuffer();
            store.finalizeReasoningMessage();
          } else if (event.type === 'TEXT_MESSAGE_CONTENT' || event.type === 'TEXT_MESSAGE_CHUNK') {
            const delta = (event as { delta?: unknown }).delta;
            if (typeof delta === 'string') {
              if (!textBuffer.append(delta)) {
                continue;
              }

              if (!receivedText) {
                store.updateAssistantMessage('', true);
                receivedText = true;
              }
              scheduleTextFlush();
            }
          } else if (event.type === 'TEXT_MESSAGE_END') {
            flushTextBuffer(false);
          } else if (event.type === 'RUN_FINISHED') {
            flushTextBuffer(false);
            store.finalizeReasoningMessage();
            storeCurrentRunEvent(runtimeEvent);
          } else if (event.type === 'RUN_ERROR') {
            flushTextBuffer();
            store.finalizeReasoningMessage();
            const message = (event as { message?: unknown }).message;
            const errorMessage = typeof message === 'string' ? message : 'Agent run failed';
            storeCurrentRunEvent(runtimeEvent);

            // Classify and log the error
            const classifiedError = classifyError(new Error(errorMessage));
            errorLogger.log(classifiedError, { threadId, runId });

            // Display user-friendly error message
            const friendlyMessage = formatErrorMessage(classifiedError);
            store.updateAssistantMessage(`Error: ${friendlyMessage}`, false);
          } else if (isToolCallEvent(runtimeEvent)) {
            startNextTextSegment();
            store.finalizeReasoningMessage();
            storeCurrentRunEvent(runtimeEvent);
          } else {
            // Feed non-text events into the state reducer. Text chunks are
            // rendered through the buffered assistant message path above.
            storeCurrentRunEvent(runtimeEvent);
          }
        }
        flushTextBuffer(false);
        store.finalizeReasoningMessage();
        if (!isCurrentRun()) {
          return;
        }

        // Ensure run is marked as finished
        const finalState = store.getState();
        if (finalState.runStatus === 'running') {
          handleCurrentRunEvent({ type: 'RUN_FINISHED', runId: currentAttemptRunId() });
        }

        // Clear any previous errors on success
        store.setConnectionStatus('connected');
        setRetryCount(0);
      } catch (error) {
        flushTextBuffer();
        store.finalizeReasoningMessage();
        if (!isCurrentRun()) {
          return;
        }

        // Classify the error
        const classifiedError = classifyError(error);

        // Log the error with context
        errorLogger.log(classifiedError, {
          threadId,
          runId,
          retryCount: attempt,
          activeDatasourceId,
        });

        // Update connection status
        if (classifiedError.category === 'network') {
          store.setConnectionStatus('error', classifiedError.userMessage);
        } else {
          store.setConnectionStatus('connected', classifiedError.userMessage);
        }

        // Format user-friendly error message
        const friendlyMessage = formatErrorMessage(classifiedError);

        // Handle retryable errors
        if (classifiedError.retryable && attempt < 3) {
          const nextAttempt = attempt + 1;
          setRetryCount(nextAttempt);

          // Show retry message in the existing assistant bubble.
          store.updateAssistantMessage(
            `${friendlyMessage}\n\nRetrying (${nextAttempt}/3)...`,
            true,
          );

          await new Promise(resolve => setTimeout(resolve, 2000 * nextAttempt));
          await runAttempt(nextAttempt);
        } else {
          // Non-retryable or max retries reached
          store.handleLiveRunEvent({
            type: 'RUN_ERROR',
            runId: currentAttemptRunId(),
            message: friendlyMessage,
            _clientRunId: currentAttemptRunId(),
          });

          // Add error message to chat with category indicator
          const categoryEmoji = {
            network: '🌐',
            config: '⚙️',
            api: '🔌',
            validation: '✏️',
            stream: '📡',
            unknown: '❓',
          }[classifiedError.category];

          store.updateAssistantMessage(
            `${categoryEmoji} ${friendlyMessage}`,
            false,
          );
          setRetryCount(0);
        }
      }
    };

    await runAttempt(0);
  };

  useEffect(() => {
    handleAgentQueryRef.current = handleAgentQuery;
  });

  useEffect(() => {
    if (drainingQueuedPromptRef.current) return;
    if (isRestoringSession || state.runStatus === 'running') return;

    const runQueuedPrompt = handleAgentQueryRef.current;
    if (!runQueuedPrompt) return;

    const [nextPrompt, ...remainingPrompts] = queuedPromptsRef.current;
    if (!nextPrompt) return;

    queuedPromptsRef.current = remainingPrompts;
    setQueuedPrompts(remainingPrompts);
    setCommandNotice(null);

    drainingQueuedPromptRef.current = true;
    void (async () => {
      try {
        await runQueuedPrompt(nextPrompt);
      } finally {
        drainingQueuedPromptRef.current = false;
        if (queuedPromptsRef.current.length > 0 && store.getState().runStatus !== 'running') {
          setQueuedPrompts([...queuedPromptsRef.current]);
        }
      }
    })();
  }, [isRestoringSession, queuedPrompts, state.runStatus]);

  // Handle input change (no-op - InputBox manages its own local state)
  const handleInputChange = (value: string) => {
    // InputBox manages its own state, we don't need to sync to store on every keystroke
    // This avoids unnecessary re-renders and flickering
  };

  const visibleArtifacts = state.artifacts;
  const liveActivity = state.runStatus === 'running'
    ? {
        plan: state.plan,
        toolCalls: state.toolCalls.slice(-2),
        events: [],
      }
    : {
        plan: [],
        toolCalls: [],
        events: [],
      };
  const chatPaneColumns = showOutputsSidebar
    ? mainPaneColumns.chatColumns
    : terminalColumns;

  if (logoutConfirm) {
    if (logoutConfirm.kind === 'local-cleanup-failed') {
      return (
        <Box flexDirection="column" height={appRows} width={terminalColumns} paddingX={1} paddingY={1}>
          <Text color={inkColors.error}>远端已注销，但本地 Session 清理失败。</Text>
          <Text>{`路径: ${logoutConfirm.storePath}`}</Text>
          <Text>{`错误: ${logoutConfirm.error}`}</Text>
          <Text>[1] 重试清理本地登录</Text>
          <Text>[2] 返回</Text>
        </Box>
      );
    }
    return (
      <Box flexDirection="column" height={appRows} width={terminalColumns} paddingX={1} paddingY={1}>
        <Text color={inkColors.error}>无法连接服务端，远端 Session 可能仍有效。</Text>
        <Text>[1] 仅清除此设备的登录</Text>
        <Text>[2] 返回</Text>
      </Box>
    );
  }

  return (
    <Box flexDirection="column" height={appRows} width={terminalColumns} overflowY="hidden">
      <Box flexDirection="column" height={workspaceRows} width={terminalColumns} overflowY="hidden">
        {sessionPickerOpen ? (
        <Box
          flexDirection="column"
          height={workspaceRows}
          width={terminalColumns}
          overflowY="hidden"
          paddingX={1}
        >
          <SessionPicker
            sessions={pickerSessions}
            loading={pickerLoading}
            error={pickerError}
            columns={Math.max(20, terminalColumns - 2)}
            rows={workspaceRows}
            onSelect={(sessionId) => {
              setResumeLoadingSessionId(sessionId);
              setSessionPickerOpen(false);
              void restoreHistoricalSession(sessionId);
            }}
            onCancel={() => {
              setSessionPickerOpen(false);
            }}
          />
        </Box>
      ) : datasourcePickerOpen ? (
        <Box
          flexDirection="column"
          height={workspaceRows}
          width={terminalColumns}
          overflowY="hidden"
          paddingX={1}
        >
          <ResourcePicker
            title="Select a data source"
            items={datasourcePickerItems}
            loading={datasourcePickerLoading}
            error={datasourcePickerError}
            warning={datasourcePickerWarning}
            columns={Math.max(20, terminalColumns - 2)}
            rows={workspaceRows}
            emptyMessage="No data sources configured."
            onSelect={(item) => {
              setDatasourcePickerOpen(false);
              selectDatasourceForSession(item.id);
              setCommandNotice({
                kind: 'info',
                message: `Datasource selected: ${item.name} (${item.id}).`,
              });
            }}
            onCancel={() => {
              setDatasourcePickerOpen(false);
            }}
          />
        </Box>
      ) : skillPickerOpen ? (
        <Box
          flexDirection="column"
          height={workspaceRows}
          width={terminalColumns}
          overflowY="hidden"
          paddingX={1}
        >
          <ResourcePicker
            title="Select a skill"
            items={skillPickerItems}
            loading={skillPickerLoading}
            error={skillPickerError}
            warning={skillPickerWarning}
            emptyMessage="No skills configured."
            onSelect={(item) => {
              setSkillPickerOpen(false);
              selectSkillForSession(item.id);
              setCommandNotice({
                kind: 'info',
                message: `Skill selected: ${item.name} (${item.id}).`,
              });
            }}
            onCancel={() => {
              setSkillPickerOpen(false);
            }}
          />
        </Box>
      ) : outputsOpen ? (
        <Box
          flexDirection="column"
          height={workspaceRows}
          width={terminalColumns}
          overflowY="hidden"
          paddingX={1}
        >
          <OutputsScreen
            artifacts={visibleArtifacts}
            events={state.events}
            columns={Math.max(20, terminalColumns - 2)}
            rows={workspaceRows}
            fetchArtifactPreview={
              configClient
                ? (artifactId) => configClient.getArtifactPreview(artifactId)
                : undefined
            }
            fetchArtifactContent={
              configClient
                ? (artifactId) => configClient.getArtifactContent(artifactId)
                : undefined
            }
            onCancel={() => {
              setOutputsOpen(false);
            }}
          />
        </Box>
      ) : (
        <WorkspaceFrame
          rows={workspaceRows}
          columns={terminalColumns}
          scrollableRows={scrollableRowCount}
          {...(showOutputsSidebar
            ? {
                rightColumns: mainPaneColumns.outputsColumns,
                right: (
                  <OutputsSidebar
                    artifacts={visibleArtifacts}
                    events={state.events}
                    columns={mainPaneColumns.outputsColumns}
                    rows={workspaceRows}
                  />
                ),
              }
            : {})}
          scrollable={
            <Box
              key={isHomeScreen ? 'home-scrollable' : 'chat-scrollable'}
              flexDirection="row"
              height={scrollableRowCount}
              width={chatPaneColumns}
              flexShrink={0}
              overflowY="hidden"
            >
              {isHomeScreen ? (
                  <Box
                    flexDirection="column"
                    height={scrollableRowCount}
                    width="100%"
                    flexShrink={0}
                    overflowY="hidden"
                  >
                    <HomeSplash
                      rows={scrollableRowCount}
                      columns={chatPaneColumns}
                      startup={startup}
                      canResume={Boolean(configClient)}
                      input={(promptWidth) => (
                        <EnhancedInputBox
                          onChange={handleInputChange}
                          onSubmit={handleSubmit}
                          onFocusChange={setInputFocused}
                          onClearScreen={clearScreen}
                          onNewSession={startNewSession}
                          onExitRequest={requestCtrlCExit}
                          onRestoreQueuedMessages={popAllQueuedPrompts}
                          ctrlCExitPending={ctrlCPressedOnce}
                          disabled={inputDisabled}
                          commands={inputCommands}
                          modelName={modelName}
                          datasourceId={activeDatasourceId}
                          skillId={activeSkillId}
                          inputWidth={promptWidth}
                          history={inputHistoryRef.current}
                          onShortcut={(shortcut) => {
                            if (shortcut === '1') {
                              void handleSubmit('Show tables');
                              return true;
                            }
                            if (shortcut === '2' && configClient) {
                              void handleCommandExecution('/resume latest');
                              return true;
                            }
                            return false;
                          }}
                        />
                      )}
                    />
                  </Box>
              ) : showResumeLoading ? (
                  <Box
                    flexDirection="column"
                    flexGrow={1}
                    paddingX={1}
                    overflowY="hidden"
                    justifyContent="center"
                  >
                    <Text color={inkColors.accent}>
                      {resumeLoadingSessionId === 'latest'
                        ? 'Loading latest session...'
                        : 'Loading selected session...'}
                    </Text>
                  </Box>
              ) : (
                  <>
                    <Box
                      flexDirection="column"
                      width={showLiveActivity ? '70%' : chatPaneColumns}
                      height={scrollableRowCount}
                      flexShrink={0}
                      paddingX={1}
                      overflowY="hidden"
                    >
                      <ChatArea
                        ref={chatAreaRef}
                        messages={visibleMessages}
                        artifacts={visibleArtifacts}
                        totalMessageCount={state.messages.length}
                        toolCalls={state.toolCalls}
                        viewportRows={chatViewportRowCount}
                        columns={chatPaneColumns}
                        startup={transcriptStartup}
                        compactMode={compactMode}
                        thoughtExpanded={thoughtExpanded}
                      />
                    </Box>

                    {showLiveActivity && (
                      <Box
                        flexDirection="column"
                        width="30%"
                        borderStyle="single"
                        borderColor={inkColors.border}
                        paddingX={1}
                      >
                        <ActivityPanel
                          plan={liveActivity.plan}
                          toolCalls={liveActivity.toolCalls}
                          events={liveActivity.events}
                        />
                      </Box>
                    )}
                  </>
              )}
            </Box>
          }
          bottom={
            <Box ref={mainControlsRef} flexDirection="column" flexShrink={0}>
              {commandNotice && (
                <Box paddingX={1} flexShrink={0}>
                  <Text color={commandNotice.kind === 'error' ? inkColors.error : inkColors.accent}>
                    {commandNotice.message}
                  </Text>
                </Box>
              )}

              {queuedPrompts.length > 0 && (
                <QueuedPromptDisplay prompts={queuedPrompts} />
              )}

              {!isHomeScreen && (
                <EnhancedInputBox
                  onChange={handleInputChange}
                  onSubmit={handleSubmit}
                  onFocusChange={setInputFocused}
                  onClearScreen={clearScreen}
                  onNewSession={startNewSession}
                  onExitRequest={requestCtrlCExit}
                  onRestoreQueuedMessages={popAllQueuedPrompts}
                  ctrlCExitPending={ctrlCPressedOnce}
                  onLayoutChange={requestControlsMeasurement}
                  disabled={inputDisabled}
                  commands={inputCommands}
                  modelName={modelName}
                  datasourceId={activeDatasourceId}
                  skillId={activeSkillId}
                  inputWidth={chatPaneColumns}
                  outputCount={visibleArtifacts.length}
                  history={inputHistoryRef.current}
                />
              )}

            </Box>
          }
        />
        )}
      </Box>
      <StatusBar columns={terminalColumns} startup={startup} />
    </Box>
  );
};
