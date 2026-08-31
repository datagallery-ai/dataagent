type MessageLike = {
  id?: string;
  role?: string;
  content?: unknown;
  toolCalls?: unknown[];
};

function messageToolCallCount(message: MessageLike): number {
  return Array.isArray(message.toolCalls) ? message.toolCalls.length : 0;
}

function getRunMessageSlice(messages: MessageLike[], messageIndex: number): MessageLike[] {
  if (messageIndex < 0) return messages;
  const lastUserIndex =
    messageIndex >= 0
      ? (messages
          .slice(0, messageIndex + 1)
          .map((item, index) => ({ item, index }))
          .filter(({ item }) => item.role === "user")
          .at(-1)?.index ?? -1)
      : -1;
  const nextUserIndex =
    messageIndex >= 0
      ? messages.findIndex((item, index) => index > messageIndex && item.role === "user")
      : -1;
  return messages.slice(lastUserIndex + 1, nextUserIndex > -1 ? nextUserIndex : undefined);
}

const MEANINGFUL_TEXT_PATTERN = /[\p{L}\p{N}]/u;

/**
 * True when text carries at least one letter or digit (incl. CJK). Punctuation /
 * whitespace-only fragments like "." or "。" are noise some models emit between
 * tool steps and must not render as thinking or count as a process step.
 */
export function hasMeaningfulText(text: string): boolean {
  return MEANINGFUL_TEXT_PATTERN.test(text);
}

function stripTrivialText(text: string): string {
  return hasMeaningfulText(text) ? text : "";
}

export function messageTextContent(content: unknown): string {
  if (typeof content === "string") return content.trim();
  if (!Array.isArray(content)) return "";
  return content
    .map((part) => {
      if (!part || typeof part !== "object" || !("type" in part)) return "";
      const typed = part as { type?: unknown; text?: unknown };
      if (typed.type === "text" && typeof typed.text === "string") return typed.text;
      if (typed.type === "reasoning" && typeof typed.text === "string") return typed.text;
      return "";
    })
    .join("")
    .trim();
}

/** Reasoning-only text from CopilotKit / restored content parts. */
export function messageReasoningContent(content: unknown): string {
  if (!Array.isArray(content)) return "";
  return content
    .map((part) => {
      if (!part || typeof part !== "object" || !("type" in part)) return "";
      const typed = part as { type?: unknown; text?: unknown };
      if (typed.type === "reasoning" && typeof typed.text === "string") return typed.text;
      return "";
    })
    .filter((text) => text.trim().length > 0)
    .join("\n\n")
    .trim();
}

/** Visible assistant text parts only (excludes reasoning). */
export function messageVisibleTextContent(content: unknown): string {
  if (typeof content === "string") return content.trim();
  if (!Array.isArray(content)) return "";
  const hasTypedParts = content.some(
    (part) => part && typeof part === "object" && "type" in part,
  );
  if (!hasTypedParts) return "";
  return content
    .map((part) => {
      if (!part || typeof part !== "object" || !("type" in part)) return "";
      const typed = part as { type?: unknown; text?: unknown };
      if (typed.type === "text" && typeof typed.text === "string") return typed.text;
      return "";
    })
    .join("")
    .trim();
}

/** Some thinking models emit the same block twice in one assistant message. */
export function dedupeRepeatedText(text: string): string {
  const trimmed = text.trim();
  if (trimmed.length < 2 || trimmed.length % 2 !== 0) return trimmed;

  const half = trimmed.length / 2;
  if (trimmed.slice(0, half) === trimmed.slice(half)) {
    return trimmed.slice(0, half);
  }

  return trimmed;
}

function reasoningPartLength(content: unknown): number {
  if (!Array.isArray(content)) return 0;
  return content.reduce((sum, part) => {
    if (!part || typeof part !== "object" || !("type" in part)) return sum;
    const typed = part as { type?: unknown; text?: unknown };
    if (typed.type !== "reasoning" || typeof typed.text !== "string") return sum;
    return sum + typed.text.trim().length;
  }, 0);
}

function pickRicherAssistantMessage(
  agentMessage: MessageLike,
  renderMessage: MessageLike,
): MessageLike {
  const agentReasoning = reasoningPartLength(agentMessage.content);
  const renderReasoning = reasoningPartLength(renderMessage.content);
  if (renderReasoning > agentReasoning) {
    return { ...agentMessage, content: renderMessage.content };
  }
  const agentText = messageTextContent(agentMessage.content);
  const renderText = messageTextContent(renderMessage.content);
  if (renderText.length > agentText.length) {
    return { ...agentMessage, content: renderMessage.content };
  }
  return agentMessage;
}

/**
 * CopilotKit keeps reasoning turns in render messages, but agent.messages may
 * omit them. Merge both sources so tool steps can still show Thinking content.
 */
export function mergeMessagesForStepContext(
  agentMessages: MessageLike[],
  renderMessages: MessageLike[],
): MessageLike[] {
  if (agentMessages.length === 0) return renderMessages;
  if (renderMessages.length === 0) return agentMessages;

  const mergedById = new Map<string, MessageLike>();
  for (const agentMessage of agentMessages) {
    if (!agentMessage.id) continue;
    const renderMessage = renderMessages.find((item) => item.id === agentMessage.id);
    mergedById.set(
      agentMessage.id,
      renderMessage
        ? pickRicherAssistantMessage(agentMessage, renderMessage)
        : agentMessage,
    );
  }
  for (const renderMessage of renderMessages) {
    if (!renderMessage.id || mergedById.has(renderMessage.id)) continue;
    mergedById.set(renderMessage.id, renderMessage);
  }

  const result: MessageLike[] = [];
  const seenIds = new Set<string>();

  const pushMerged = (item: MessageLike) => {
    if (!item.id) {
      result.push(item);
      return;
    }
    if (seenIds.has(item.id)) return;
    result.push(mergedById.get(item.id) ?? item);
    seenIds.add(item.id);
  };

  for (const item of renderMessages) {
    if (item.role === "reasoning") {
      result.push(item);
      continue;
    }
    pushMerged(item);
  }

  for (const agentMessage of agentMessages) {
    if (agentMessage.id && seenIds.has(agentMessage.id)) continue;
    pushMerged(agentMessage);
  }

  return result;
}

function resolveMessageFromTimeline(
  message: MessageLike,
  messages: MessageLike[],
): MessageLike {
  if (!message.id) return message;
  return messages.find((item) => item.id === message.id) ?? message;
}

function dedupeThoughtParts(parts: string[]): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  for (const part of parts) {
    const trimmed = part.trim();
    if (!trimmed || seen.has(trimmed)) continue;
    seen.add(trimmed);
    result.push(trimmed);
  }
  return result;
}

function collectReasoningTextsInRunSlice(
  runSlice: MessageLike[],
  runIndex: number,
  direction: "backward" | "forward",
): string[] {
  const reasoningTexts: string[] = [];
  if (direction === "backward") {
    for (let index = runIndex - 1; index >= 0; index -= 1) {
      const item = runSlice[index];
      if (item?.role === "reasoning") {
        const text = messageTextContent(item.content);
        if (text) reasoningTexts.unshift(text);
        continue;
      }
      if (item?.role === "assistant" && messageToolCallCount(item) === 0) {
        continue;
      }
      if (item?.role === "assistant" || item?.role === "user") break;
    }
    return reasoningTexts;
  }

  for (let index = runIndex + 1; index < runSlice.length; index += 1) {
    const item = runSlice[index];
    if (item?.role === "reasoning") {
      const text = messageTextContent(item.content);
      if (text) reasoningTexts.push(text);
      continue;
    }
    if (item?.role === "assistant" || item?.role === "user") break;
  }
  return reasoningTexts;
}

export function reasoningMessageAbsorbedByFollowingToolStep(
  reasoningMessage: MessageLike,
  messages: MessageLike[],
): boolean {
  if (reasoningMessage.role !== "reasoning") return false;
  const messageIndex = messages.findIndex((item) => item.id === reasoningMessage.id);
  if (messageIndex < 0) return false;

  const runSlice = getRunMessageSlice(messages, messageIndex);
  const runIndex = runSlice.findIndex((item) => item.id === reasoningMessage.id);
  if (runIndex < 0) return false;

  for (let index = runIndex + 1; index < runSlice.length; index += 1) {
    const next = runSlice[index];
    if (next?.role === "user") return false;
    if (next?.role === "tool") continue;
    if (next?.role === "assistant" && messageToolCallCount(next) > 0) return true;
    if (next?.role === "assistant") return false;
    if (next?.role === "reasoning") return false;
  }
  return false;
}

export function resolveAssistantThoughtContent(
  message: MessageLike,
  messages: MessageLike[],
): string {
  const foldedReasoning = messageReasoningContent(message.content);
  if (foldedReasoning && hasMeaningfulText(foldedReasoning)) {
    return foldedReasoning;
  }

  const assistantText = messageTextContent(message.content);
  const messageIndex = messages.findIndex((item) => item.id === message.id);

  const reasoningTexts: string[] = [];
  if (messageIndex >= 0) {
    const runSlice = getRunMessageSlice(messages, messageIndex);
    const runIndex = runSlice.findIndex((item) => item.id === message.id);
    if (runIndex >= 0) {
      reasoningTexts.push(
        ...collectReasoningTextsInRunSlice(runSlice, runIndex, "backward"),
      );
      if (reasoningTexts.length === 0 && messageToolCallCount(message) > 0) {
        reasoningTexts.push(
          ...collectReasoningTextsInRunSlice(runSlice, runIndex, "forward"),
        );
      }
    }
  }

  const reasoningText = reasoningTexts.join("\n\n").trim();
  if (reasoningText && hasMeaningfulText(reasoningText)) return reasoningText;

  return stripTrivialText(dedupeRepeatedText(assistantText));
}

/** True when a text-only assistant turn should fold into the next tool step card. */
export function isOrphanPreambleMergedIntoFollowingToolStep(
  message: MessageLike,
  messages: MessageLike[],
): boolean {
  if (message.role !== "assistant") return false;
  if (messageToolCallCount(message) > 0) return false;
  if (!messageTextContent(message.content)) return false;

  const messageIndex = messages.findIndex((item) => item.id === message.id);
  if (messageIndex < 0) return false;

  const runSlice = getRunMessageSlice(messages, messageIndex);
  const runIndex = runSlice.findIndex((item) => item.id === message.id);
  if (runIndex < 0) return false;
  if (!runSlice.slice(runIndex + 1).some((item) => item.role === "assistant")) {
    return false;
  }

  for (let index = runIndex + 1; index < runSlice.length; index += 1) {
    const next = runSlice[index];
    if (next?.role !== "assistant") continue;
    if (messageToolCallCount(next) > 0) {
      const between = runSlice.slice(runIndex + 1, index);
      return between.every((item) => {
        if (item.role !== "assistant") return true;
        return messageToolCallCount(item) === 0;
      });
    }
    if (messageTextContent(next?.content)) {
      return false;
    }
  }

  return false;
}

/**
 * Thought body for a tool step — includes reasoning blocks and any immediately
 * preceding text-only assistant preambles that belong to the same ReAct step.
 */
export function resolveToolStepThoughtContent(
  message: MessageLike,
  messages: MessageLike[],
): string {
  const source = resolveMessageFromTimeline(message, messages);
  const foldedReasoning = messageReasoningContent(source.content);
  const textOnly = messageVisibleTextContent(source.content);
  const inline = stripTrivialText(
    dedupeRepeatedText(
      foldedReasoning || textOnly || messageTextContent(source.content),
    ),
  );
  if (messageToolCallCount(source) === 0) {
    return resolveAssistantThoughtContent(source, messages) || inline;
  }

  const messageIndex = messages.findIndex((item) => item.id === source.id);
  if (messageIndex < 0) return inline;

  const runSlice = getRunMessageSlice(messages, messageIndex);
  const runIndex = runSlice.findIndex((item) => item.id === source.id);
  if (runIndex < 0) return inline;

  const parts: string[] = [];

  for (let index = runIndex - 1; index >= 0; index -= 1) {
    const candidate = runSlice[index];
    if (candidate?.role === "user") break;
    if (candidate?.role === "reasoning") {
      const text = messageTextContent(candidate.content);
      if (text && hasMeaningfulText(text)) parts.unshift(text);
      continue;
    }
    if (candidate?.role === "assistant") {
      if (messageToolCallCount(candidate) > 0) break;
      const text = dedupeRepeatedText(
        messageReasoningContent(candidate.content) ||
          messageTextContent(candidate.content),
      );
      if (text && hasMeaningfulText(text)) parts.unshift(text);
      continue;
    }
  }

  parts.push(
    ...collectReasoningTextsInRunSlice(runSlice, runIndex, "forward").filter(
      hasMeaningfulText,
    ),
  );
  if (foldedReasoning && hasMeaningfulText(foldedReasoning)) {
    parts.push(foldedReasoning);
  } else if (inline) {
    parts.push(inline);
  }

  return dedupeThoughtParts(parts).join("\n\n").trim();
}
