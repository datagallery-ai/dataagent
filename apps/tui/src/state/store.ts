import { createInitialTuiState, type TuiSessionState } from "./tui-state.js";
import {
  reduceLiveRunEvent,
  runIdFromEvent,
  type LiveRun,
  type LiveToolCallRecord,
  createInitialSessionUsage,
  deriveRunUsage,
  accumulateSessionUsage,
  deriveLiveSessionView,
  type SessionUsageStats,
} from "./live-run-state.js";
import {
  addUserMessage,
  addAssistantMessage,
  appendToLastAssistantMessage,
  appendReasoningToLastAssistantMessage,
  clearMessages,
  finalizeLastAssistantMessage,
  finalizeLastReasoningElement,
  updateLastAssistantMessage,
  insertToolCallIntoLastMessage,
  updateToolCallInMessages,
} from "./message-history.js";
import {
  updateConnectionStatus,
  updateInputBuffer,
  clearInputBuffer,
  setThreadId,
} from "./tui-state.js";
import type { DisplayMessage } from "./tui-state.js";
import {
  loadWorkspaceConfig,
  type DataArtifact,
  type WorkspaceConfigStore,
} from "./data-task-state.js";

/**
 * Extended TUI state with workspace configuration and session statistics
 */
export interface TuiAppState extends TuiSessionState {
  /** Workspace configuration (datasources, models, skills, mcp, kb) */
  workspaceConfig: WorkspaceConfigStore;
  /** Session-level cumulative statistics across all runs */
  sessionStats: SessionUsageStats;
}

/**
 * Selector function type for deriving state
 */
type Selector<T> = (state: TuiAppState) => T;

/**
 * Listener function that can optionally use selectors for optimization
 */
type StateListener = (state: TuiAppState) => void;

/**
 * Central state store for the TUI application with enhanced features:
 * - Workspace configuration management
 * - Session-level statistics accumulation
 * - Optimized listener notifications with selector support
 */
class StateStore {
  private state: TuiAppState;
  private listeners: Set<StateListener>;
  private selectorCache: Map<Selector<unknown>, unknown>;
  private notifyTimer: ReturnType<typeof setTimeout> | undefined;
  private readonly notifyFrameMs = 80;

  constructor(initialWorkspaceConfig: WorkspaceConfigStore) {
    this.state = {
      ...createInitialTuiState(),
      workspaceConfig: initialWorkspaceConfig,
      sessionStats: createInitialSessionUsage(),
    };
    this.listeners = new Set();
    this.selectorCache = new Map();
  }

  /**
   * Get current state (immutable reference)
   */
  getState(): TuiAppState {
    return this.state;
  }

  /**
   * Update state and notify listeners
   */
  private setState(newState: TuiAppState, immediate = false): void {
    this.state = newState;

    // Clear selector cache on state change
    this.selectorCache.clear();

    if (immediate) {
      this.notifyListeners();
      return;
    }

    this.scheduleNotifyListeners();
  }

  /**
   * Subscribe to state changes
   * @returns Unsubscribe function
   */
  subscribe(listener: StateListener): () => void {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  }

  /**
   * Notify all listeners of state change
   */
  private notifyListeners(): void {
    if (this.notifyTimer) {
      clearTimeout(this.notifyTimer);
      this.notifyTimer = undefined;
    }

    for (const listener of this.listeners) {
      listener(this.state);
    }
  }

  private scheduleNotifyListeners(): void {
    if (this.notifyTimer) return;

    this.notifyTimer = setTimeout(() => {
      this.notifyTimer = undefined;
      for (const listener of this.listeners) {
        listener(this.state);
      }
    }, this.notifyFrameMs);
  }

  /**
   * Evaluate a selector with caching
   */
  select<T>(selector: Selector<T>): T {
    if (this.selectorCache.has(selector)) {
      return this.selectorCache.get(selector) as T;
    }
    const result = selector(this.state);
    this.selectorCache.set(selector, result);
    return result;
  }

  /**
   * Handle LiveRun events from the protocol
   * Reduces the LiveRun part and merges with TUI-specific state
   */
  handleLiveRunEvent(event: { type?: string; [key: string]: unknown }): void {
    const eventRunId = runIdFromEvent(event);
    const previousRunId = this.state.runId;
    const clientRunId =
      typeof event._clientRunId === "string" && event._clientRunId.length > 0
        ? event._clientRunId
        : undefined;
    if (
      event.type !== "RUN_STARTED" &&
      clientRunId &&
      this.state.runId &&
      clientRunId !== this.state.runId
    ) {
      return;
    }

    if (
      event.type !== "RUN_STARTED" &&
      eventRunId &&
      this.state.runId &&
      eventRunId !== this.state.runId &&
      clientRunId !== this.state.runId
    ) {
      return;
    }

    // Apply the event to the LiveRun portion of the state
    const liveRun: LiveRun = {
      runId: this.state.runId,
      agentResponseComplete: this.state.agentResponseComplete,
      plan: this.state.plan,
      events: this.state.events,
      artifacts: this.state.artifacts,
      audits: this.state.audits,
      runStatus: this.state.runStatus,
      errorMessage: this.state.errorMessage ?? undefined,
      toolCalls: this.state.toolCalls,
      runStartedAt: this.state.runStartedAt,
      runFinishedAt: this.state.runFinishedAt,
      tokenUsage: this.state.tokenUsage,
    };

    const newLiveRun = reduceLiveRunEvent(liveRun, event);
    if (newLiveRun === liveRun) {
      return;
    }

    const previousToolCallsById = new Map(
      this.state.toolCalls.map((toolCall) => [toolCall.id, toolCall]),
    );
    const previousToolCallIds = new Set(previousToolCallsById.keys());
    const newToolCallIds = newLiveRun.toolCalls
      .filter((toolCall) => !previousToolCallIds.has(toolCall.id))
      .map((toolCall) => toolCall.id);
    const newToolCallsById = new Map(
      newLiveRun.toolCalls.map((toolCall) => [toolCall.id, toolCall]),
    );
    const hasNewRunningToolCall = newLiveRun.toolCalls.some((toolCall) => {
      const previous = previousToolCallsById.get(toolCall.id);
      return toolCall.status === "running" && previous?.status !== "running";
    });
    let stateWithNewToolCalls: TuiAppState = this.state;
    const liveRunId = newLiveRun.runId ?? this.state.runId;
    for (const toolCallId of newToolCallIds) {
      stateWithNewToolCalls = insertToolCallIntoLastMessage(
        stateWithNewToolCalls,
        toolCallId,
        newToolCallsById.get(toolCallId),
        liveRunId,
      ) as TuiAppState;
    }
    for (const toolCall of newLiveRun.toolCalls) {
      stateWithNewToolCalls = updateToolCallInMessages(
        stateWithNewToolCalls,
        toolCall,
        liveRunId,
        previousRunId,
      ) as TuiAppState;
    }

    // Detect run completion and accumulate stats
    let newSessionStats = this.state.sessionStats;
    const wasRunning = this.state.runStatus === "running";
    const isNowCompleted = newLiveRun.runStatus === "completed";
    const isNowFailed = newLiveRun.runStatus === "failed";

    if (wasRunning && (isNowCompleted || isNowFailed)) {
      const runSnapshot = deriveRunUsage(newLiveRun);
      newSessionStats = accumulateSessionUsage(
        this.state.sessionStats,
        runSnapshot,
        isNowCompleted ? "completed" : "failed"
      );
    }

    // Merge back with TUI-specific fields
    const newState: TuiAppState = {
      ...stateWithNewToolCalls,
      ...newLiveRun,
      sessionStats: newSessionStats,
    };

    const notifyImmediately =
      hasNewRunningToolCall ||
      event.type === "RUN_STARTED" ||
      event.type === "RUN_FINISHED" ||
      event.type === "RUN_ERROR" ||
      event.name === "run.response.completed";
    this.setState(newState, notifyImmediately);
  }

  /**
   * Add a user message
   */
  addUserMessage(content: string): void {
    const newState = addUserMessage(this.state, content);
    this.setState(newState as TuiAppState);
  }

  /**
   * Add an assistant message
   */
  addAssistantMessage(content: string, isStreaming = false): void {
    const newState = addAssistantMessage(this.state, content, isStreaming);
    this.setState(newState as TuiAppState, isStreaming);
  }

  /**
   * Append to the last assistant message (for streaming)
   */
  appendToAssistantMessage(delta: string): void {
    const newState = appendToLastAssistantMessage(this.state, delta);
    this.setState(newState as TuiAppState);
  }

  /**
   * Update the last assistant message
   */
  updateAssistantMessage(content: string, isStreaming = false): void {
    const newState = updateLastAssistantMessage(this.state, content, isStreaming);
    this.setState(newState as TuiAppState, !isStreaming);
  }

  /**
   * Append reasoning to the last assistant message.
   */
  appendReasoningMessage(delta: string, isStreaming = true): void {
    const newState = appendReasoningToLastAssistantMessage(this.state, delta, isStreaming);
    this.setState(newState as TuiAppState, !isStreaming);
  }

  /**
   * Finalize the last reasoning block without finalizing the whole message.
   */
  finalizeReasoningMessage(): void {
    const newState = finalizeLastReasoningElement(this.state);
    this.setState(newState as TuiAppState, true);
  }

  /**
   * Finalize the last assistant message
   */
  finalizeAssistantMessage(): void {
    const newState = finalizeLastAssistantMessage(this.state);
    this.setState(newState as TuiAppState, true);
  }

  /**
   * Update connection status
   */
  setConnectionStatus(status: TuiSessionState["connectionStatus"], error?: string): void {
    const newState = updateConnectionStatus(this.state, status, error);
    this.setState(newState as TuiAppState);
  }

  /**
   * Update input buffer
   */
  setInputBuffer(buffer: string): void {
    const newState = updateInputBuffer(this.state, buffer);
    this.setState(newState as TuiAppState, true);
  }

  /**
   * Clear input buffer
   */
  clearInputBuffer(): void {
    const newState = clearInputBuffer(this.state);
    this.setState(newState as TuiAppState, true);
  }

  /**
   * Set thread ID
   */
  setThreadId(threadId: string | null): void {
    const newState = setThreadId(this.state, threadId);
    this.setState(newState as TuiAppState);
  }

  /**
   * Clear chat messages while preserving the current session.
   */
  clearMessages(): void {
    const newState = clearMessages(this.state);
    this.setState(newState as TuiAppState, true);
  }

  /**
   * Start a fresh session with a known thread id.
   */
  startNewSession(threadId: string): void {
    this.setState({
      ...createInitialTuiState(),
      workspaceConfig: this.state.workspaceConfig,
      sessionStats: createInitialSessionUsage(),
      connectionStatus: this.state.connectionStatus,
      threadId,
    }, true);
  }

  /**
   * Replace visible state with a server-authoritative session conversation.
   */
  restoreSession(input: {
    threadId: string;
    messages: DisplayMessage[];
    toolCalls?: LiveToolCallRecord[] | undefined;
    artifacts?: DataArtifact[] | undefined;
  }): void {
    const initial = createInitialTuiState();
    this.setState({
      ...this.state,
      plan: initial.plan,
      events: initial.events,
      artifacts: input.artifacts ?? initial.artifacts,
      audits: initial.audits,
      runStatus: initial.runStatus,
      runId: undefined,
      agentResponseComplete: undefined,
      errorMessage: undefined,
      toolCalls: input.toolCalls ?? [],
      runStartedAt: undefined,
      runFinishedAt: undefined,
      tokenUsage: undefined,
      messages: input.messages,
      sessionStats: createInitialSessionUsage(),
      threadId: input.threadId,
    }, true);
  }

  /**
   * Update workspace configuration
   */
  setWorkspaceConfig(config: WorkspaceConfigStore): void {
    this.setState({
      ...this.state,
      workspaceConfig: config,
    });
  }

  /**
   * Update a specific configuration kind
   */
  updateConfigKind(
    kind: keyof WorkspaceConfigStore,
    items: WorkspaceConfigStore[keyof WorkspaceConfigStore]
  ): void {
    this.setState({
      ...this.state,
      workspaceConfig: {
        ...this.state.workspaceConfig,
        [kind]: items,
      },
    });
  }

  /**
   * Reset state to initial (preserves workspace config)
   */
  reset(): void {
    this.setState({
      ...createInitialTuiState(),
      workspaceConfig: this.state.workspaceConfig,
      sessionStats: createInitialSessionUsage(),
    });
  }

  /**
   * Reset only the current run (keeps session stats and config)
   */
  resetRun(): void {
    const initial = createInitialTuiState();
    this.setState({
      ...this.state,
      plan: initial.plan,
      events: initial.events,
      artifacts: initial.artifacts,
      audits: initial.audits,
      runStatus: initial.runStatus,
      runId: undefined,
      agentResponseComplete: undefined,
      errorMessage: undefined,
      toolCalls: initial.toolCalls,
      runStartedAt: undefined,
      runFinishedAt: undefined,
      tokenUsage: undefined,
    });
  }
}

// Export singleton instance
export const store = new StateStore(loadWorkspaceConfig());

// Export types (avoid duplicate export of TuiAppState)
export type { Selector };

// ============================================================================
// Derived state selectors (memoized)
// ============================================================================

/**
 * Get live session view including in-progress run stats
 */
export const selectLiveSessionView = (state: TuiAppState) => {
  const liveRun: LiveRun = {
    runId: state.runId,
    agentResponseComplete: state.agentResponseComplete,
    plan: state.plan,
    events: state.events,
    artifacts: state.artifacts,
    audits: state.audits,
    runStatus: state.runStatus,
    errorMessage: state.errorMessage,
    toolCalls: state.toolCalls,
    runStartedAt: state.runStartedAt,
    runFinishedAt: state.runFinishedAt,
    tokenUsage: state.tokenUsage,
  };
  return deriveLiveSessionView(state.sessionStats, liveRun);
};

/**
 * Get current run usage stats
 */
export const selectCurrentRunStats = (state: TuiAppState) => {
  const liveRun: LiveRun = {
    runId: state.runId,
    agentResponseComplete: state.agentResponseComplete,
    plan: state.plan,
    events: state.events,
    artifacts: state.artifacts,
    audits: state.audits,
    runStatus: state.runStatus,
    errorMessage: state.errorMessage,
    toolCalls: state.toolCalls,
    runStartedAt: state.runStartedAt,
    runFinishedAt: state.runFinishedAt,
    tokenUsage: state.tokenUsage,
  };
  return deriveRunUsage(liveRun);
};

/**
 * Get enabled datasources
 */
export const selectEnabledDatasources = (state: TuiAppState) => {
  return state.workspaceConfig.db.filter(item => item.enabled);
};

/**
 * Get enabled LLM configurations
 */
export const selectEnabledLlms = (state: TuiAppState) => {
  return state.workspaceConfig.llm.filter(item => item.enabled);
};

/**
 * Get enabled skills
 */
export const selectEnabledSkills = (state: TuiAppState) => {
  return state.workspaceConfig.skill.filter(item => item.enabled);
};

/**
 * Check if currently running
 */
export const selectIsRunning = (state: TuiAppState) => {
  return state.runStatus === "running";
};

/**
 * Check if connected to backend
 */
export const selectIsConnected = (state: TuiAppState) => {
  return state.connectionStatus === "connected";
};

/**
 * Get recent messages (last N)
 */
export const selectRecentMessages = (count: number) => (state: TuiAppState) => {
  return state.messages.slice(-count);
};

/**
 * Get tool call success rate
 */
export const selectToolCallSuccessRate = (state: TuiAppState) => {
  const stats = selectLiveSessionView(state);
  if (stats.toolCalls.total === 0) return 0;
  return stats.toolCalls.success / stats.toolCalls.total;
};

/**
 * Get SQL query success rate
 */
export const selectSqlSuccessRate = (state: TuiAppState) => {
  const stats = selectLiveSessionView(state);
  if (stats.sql.total === 0) return 0;
  return stats.sql.success / stats.sql.total;
};
