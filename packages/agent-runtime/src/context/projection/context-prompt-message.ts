export type ContextPromptRole = "system" | "user" | "assistant" | "tool" | string;

export type ContextPromptMessage = {
  id?: string;
  role: ContextPromptRole;
  createdAt?: Date;
  content: unknown;
};

export const isContextPromptMessage = (value: unknown): value is ContextPromptMessage =>
  typeof value === "object"
  && value !== null
  && "role" in value
  && "content" in value
  && typeof (value as { role?: unknown }).role === "string";
