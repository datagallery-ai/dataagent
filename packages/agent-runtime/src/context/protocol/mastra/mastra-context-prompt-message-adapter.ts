import type { MastraDBMessage } from "@mastra/core/agent";

import type { ContextPromptMessage } from "../../projection/context-prompt-message.js";

export const toContextPromptMessage = (message: MastraDBMessage): ContextPromptMessage => ({
  ...(typeof message.id === "string" ? { id: message.id } : {}),
  role: message.role,
  ...(message.createdAt instanceof Date ? { createdAt: message.createdAt } : {}),
  content: message.content
});

export const toMastraDBMessage = (message: ContextPromptMessage): MastraDBMessage => ({
  ...(message.id ? { id: message.id } : {}),
  role: message.role,
  createdAt: message.createdAt ?? new Date(0),
  content: message.content
} as MastraDBMessage);

export const toMastraDBMessages = (messages: ContextPromptMessage[]): MastraDBMessage[] =>
  messages.map((message) => toMastraDBMessage(message));
