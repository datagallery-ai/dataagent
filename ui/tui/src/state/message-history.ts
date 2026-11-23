import type { DisplayMessage, TuiSessionState, MessageElement } from "./tui-state.js";
import type { LiveToolCallRecord } from "./live-run-state.js";

/**
 * Add a user message to the chat history
 */
export function addUserMessage(
  state: TuiSessionState,
  content: string,
): TuiSessionState {
  const message: DisplayMessage = {
    id: generateMessageId(),
    role: "user",
    timestamp: Date.now(),
    elements: [{
      type: 'text',
      content,
      timestamp: Date.now(),
    }],
  };

  return {
    ...state,
    messages: [...state.messages, message],
  };
}

/**
 * Add an assistant message to the chat history
 */
export function addAssistantMessage(
  state: TuiSessionState,
  content: string,
  isStreaming = false,
): TuiSessionState {
  const now = Date.now();
  const message: DisplayMessage = {
    id: generateMessageId(),
    role: "assistant",
    timestamp: now,
    isStreaming,
    elements: content ? [{
      type: 'text',
      content,
      timestamp: now,
    }] : [],
  };

  return {
    ...state,
    messages: [...state.messages, message],
  };
}

/**
 * Add a system message to the chat history
 */
export function addSystemMessage(
  state: TuiSessionState,
  content: string,
): TuiSessionState {
  const now = Date.now();
  const message: DisplayMessage = {
    id: generateMessageId(),
    role: "system",
    timestamp: now,
    elements: [{
      type: 'text',
      content,
      timestamp: now,
    }],
  };

  return {
    ...state,
    messages: [...state.messages, message],
  };
}

/**
 * Append text delta to the last assistant message
 */
export function appendToLastAssistantMessage(
  state: TuiSessionState,
  delta: string,
): TuiSessionState {
  const messages = [...state.messages];
  const lastIndex = messages.length - 1;

  if (lastIndex >= 0) {
    const lastMsg = messages[lastIndex];
    if (lastMsg && lastMsg.role === "assistant") {
      const now = Date.now();
      const elements = [...lastMsg.elements];

      // Append to the last text element or create a new one
      if (elements.length > 0 && elements[elements.length - 1].type === 'text') {
        const lastElement = elements[elements.length - 1];
        if (lastElement.type === 'text') {  // Type guard
          elements[elements.length - 1] = {
            type: 'text',
            content: lastElement.content + delta,
            timestamp: lastElement.timestamp,
          };
        }
      } else {
        elements.push({
          type: 'text',
          content: delta,
          timestamp: now,
        });
      }

      messages[lastIndex] = {
        ...lastMsg,
        elements,
        isStreaming: true,
      };
    } else {
      // No assistant message to append to, create a new one
      const now = Date.now();
      messages.push({
        id: generateMessageId(),
        role: "assistant",
        timestamp: now,
        isStreaming: true,
        elements: [{
          type: 'text',
          content: delta,
          timestamp: now,
        }],
      });
    }
  } else {
    // No assistant message to append to, create a new one
    const now = Date.now();
    messages.push({
      id: generateMessageId(),
      role: "assistant",
      timestamp: now,
      isStreaming: true,
      elements: [{
        type: 'text',
        content: delta,
        timestamp: now,
      }],
    });
  }

  return {
    ...state,
    messages,
  };
}

/**
 * Update the last assistant message with full content
 * This replaces the last text element with the full content
 */
export function updateLastAssistantMessage(
  state: TuiSessionState,
  content: string,
  isStreaming = false,
): TuiSessionState {
  const messages = [...state.messages];
  const lastIndex = messages.length - 1;

  if (lastIndex >= 0) {
    const lastMsg = messages[lastIndex];
    if (lastMsg && lastMsg.role === "assistant") {
      const now = Date.now();
      const elements = [...lastMsg.elements];

      // Replace or create the last text element
      if (elements.length > 0 && elements[elements.length - 1].type === 'text') {
        elements[elements.length - 1] = {
          type: 'text',
          content,
          timestamp: now,
        };
      } else {
        elements.push({
          type: 'text',
          content,
          timestamp: now,
        });
      }

      messages[lastIndex] = {
        ...lastMsg,
        elements,
        isStreaming,
      };
    } else {
      // No assistant message to update, create a new one
      const now = Date.now();
      messages.push({
        id: generateMessageId(),
        role: "assistant",
        timestamp: now,
        isStreaming,
        elements: [{
          type: 'text',
          content,
          timestamp: now,
        }],
      });
    }
  } else {
    // No assistant message to update, create a new one
    const now = Date.now();
    messages.push({
      id: generateMessageId(),
      role: "assistant",
      timestamp: now,
      isStreaming,
      elements: [{
        type: 'text',
        content,
        timestamp: now,
      }],
    });
  }

  return {
    ...state,
    messages,
  };
}

/**
 * Append reasoning delta to the last assistant message.
 */
export function appendReasoningToLastAssistantMessage(
  state: TuiSessionState,
  delta: string,
  isStreaming = true,
): TuiSessionState {
  const messages = [...state.messages];
  const lastIndex = messages.length - 1;

  const makeElement = (content: string, timestamp: number): MessageElement => ({
    type: 'reasoning',
    content,
    timestamp,
    ...(isStreaming ? { isStreaming: true } : {}),
  });

  if (lastIndex >= 0) {
    const lastMsg = messages[lastIndex];
    if (lastMsg && lastMsg.role === "assistant") {
      const now = Date.now();
      const elements = [...lastMsg.elements];
      const lastElement = elements[elements.length - 1];

      if (lastElement?.type === 'reasoning') {
        elements[elements.length - 1] = makeElement(
          lastElement.content + delta,
          lastElement.timestamp,
        );
      } else {
        elements.push(makeElement(delta, now));
      }

      messages[lastIndex] = {
        ...lastMsg,
        elements,
        isStreaming: true,
      };
    } else {
      const now = Date.now();
      messages.push({
        id: generateMessageId(),
        role: "assistant",
        timestamp: now,
        isStreaming: true,
        elements: [makeElement(delta, now)],
      });
    }
  } else {
    const now = Date.now();
    messages.push({
      id: generateMessageId(),
      role: "assistant",
      timestamp: now,
      isStreaming: true,
      elements: [makeElement(delta, now)],
    });
  }

  return {
    ...state,
    messages,
  };
}

/**
 * Insert a tool call element into the last assistant message
 */
export function insertToolCallIntoLastMessage(
  state: TuiSessionState,
  toolCallId: string,
  toolCall?: LiveToolCallRecord | undefined,
  runId?: string | undefined,
): TuiSessionState {
  const messages = [...state.messages];
  const lastIndex = messages.length - 1;
  const elementRunId = runId ?? state.runId;

  if (lastIndex >= 0) {
    const lastMsg = messages[lastIndex];
    if (lastMsg && lastMsg.role === "assistant") {
      if (lastMsg.elements.some((element) =>
        element.type === 'tool_call' &&
        element.toolCallId === toolCallId &&
        (elementRunId === undefined ||
          element.runId === undefined ||
          element.runId === elementRunId)
      )) {
        return state;
      }

      const now = Date.now();
      const scopedToolCall =
        toolCall && elementRunId && toolCall.runId !== elementRunId
          ? { ...toolCall, runId: elementRunId }
          : toolCall;
      const elements = [
        ...lastMsg.elements,
        {
          type: 'tool_call' as const,
          toolCallId,
          timestamp: now,
          ...(elementRunId ? { runId: elementRunId } : {}),
          ...(scopedToolCall ? { toolCall: scopedToolCall } : {}),
        },
      ];

      messages[lastIndex] = {
        ...lastMsg,
        elements,
      };
    }
  }

  return {
    ...state,
    messages,
  };
}

/**
 * Keep tool call snapshots embedded in assistant messages fresh so historical
 * messages can still render after the current LiveRun toolCalls array resets.
 */
export function updateToolCallInMessages(
  state: TuiSessionState,
  toolCall: LiveToolCallRecord,
  runId?: string | undefined,
  previousRunId?: string | undefined,
): TuiSessionState {
  let changed = false;
  const scopedToolCall =
    runId && toolCall.runId !== runId ? { ...toolCall, runId } : toolCall;
  const messages = state.messages.map((message) => {
    let messageChanged = false;
    const elements = message.elements.map((element): MessageElement => {
      if (
        element.type !== 'tool_call' ||
        element.toolCallId !== toolCall.id
      ) {
        return element;
      }

      const runIdMismatch =
        runId !== undefined &&
        element.runId !== undefined &&
        element.runId !== runId;
      if (
        runIdMismatch &&
        (previousRunId === undefined || element.runId !== previousRunId)
      ) {
        return element;
      }

      messageChanged = true;
      return {
        ...element,
        ...(runId ? { runId } : {}),
        toolCall: scopedToolCall,
      };
    });

    if (!messageChanged) {
      return message;
    }
    changed = true;
    return {
      ...message,
      elements,
    };
  });

  return changed ? { ...state, messages } : state;
}

/**
 * Mark the last assistant message as complete (streaming finished)
 */
export function finalizeLastAssistantMessage(
  state: TuiSessionState,
): TuiSessionState {
  const messages = [...state.messages];
  const lastIndex = messages.length - 1;

  if (lastIndex >= 0) {
    const lastMsg = messages[lastIndex];
    if (lastMsg && lastMsg.role === "assistant") {
      messages[lastIndex] = {
        ...lastMsg,
        isStreaming: false,
      };
    }
  }

  return {
    ...state,
    messages,
  };
}

/**
 * Mark the last reasoning element as complete without ending the assistant turn.
 */
export function finalizeLastReasoningElement(
  state: TuiSessionState,
): TuiSessionState {
  const messages = [...state.messages];
  const lastIndex = messages.length - 1;

  if (lastIndex < 0) {
    return state;
  }

  const lastMsg = messages[lastIndex];
  if (!lastMsg || lastMsg.role !== "assistant") {
    return state;
  }

  const reasoningIndex = [...lastMsg.elements]
    .reverse()
    .findIndex((element) => element.type === 'reasoning');
  if (reasoningIndex < 0) {
    return state;
  }

  const elementIndex = lastMsg.elements.length - 1 - reasoningIndex;
  const element = lastMsg.elements[elementIndex];
  if (!element || element.type !== 'reasoning') {
    return state;
  }

  const elements = [...lastMsg.elements];
  elements[elementIndex] = {
    type: 'reasoning',
    content: element.content,
    timestamp: element.timestamp,
  };

  messages[lastIndex] = {
    ...lastMsg,
    elements,
  };

  return {
    ...state,
    messages,
  };
}

/**
 * Clear all messages (start fresh session)
 */
export function clearMessages(state: TuiSessionState): TuiSessionState {
  return {
    ...state,
    messages: [],
  };
}

/**
 * Get the last N messages
 */
export function getRecentMessages(
  state: TuiSessionState,
  count: number,
): DisplayMessage[] {
  return state.messages.slice(-count);
}

/**
 * Get text content from a message (for sending to backend)
 */
export function getMessageTextContent(message: DisplayMessage): string {
  return message.elements
    .filter(e => e.type === 'text')
    .map(e => e.content)
    .join('');
}

/**
 * Generate a unique message ID
 */
function generateMessageId(): string {
  return `msg-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
}
