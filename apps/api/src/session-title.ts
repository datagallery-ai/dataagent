import { createCustomEvent } from "@datafoundry/agent-runtime";
import type { MetadataStore, SessionRecord } from "@datafoundry/metadata";
import type { BaseEvent } from "@ag-ui/client";

const TITLE_MAX_CHARS = 32;

export type SessionTitleTaskInput = {
  emit(event: BaseEvent): void;
  metadataStore: MetadataStore;
  model: unknown;
  modelTemperature?: number | undefined;
  sessionId: string;
  userId: string;
  userInput: string;
};

/** Start an asynchronous session title task without blocking the agent run. */
export const startSessionTitleTask = (input: SessionTitleTaskInput): void => {
  void generateAndPersistSessionTitle(input).catch(() => undefined);
};

const generateAndPersistSessionTitle = async (input: SessionTitleTaskInput): Promise<void> => {
  const current = input.metadataStore.sessions.get({
    user_id: input.userId,
    session_id: input.sessionId
  });
  // Only skip manual renames / already-finalized LLM titles. Empty or fallback
  // titles (and legacy rows with a prefilled title but no source) may still be replaced.
  if (current.title_source === "user" || current.title_source === "llm") {
    return;
  }

  const title = fallbackTitle(input.userInput);
  const updated = input.metadataStore.sessions.updateAutoTitleIfAllowed({
    user_id: input.userId,
    session_id: input.sessionId,
    title: title.title,
    title_source: "fallback"
  });
  if (!updated) {
    return;
  }
  input.emit(createCustomEvent("session.title", sessionTitleDto(updated)));
};

const fallbackTitle = (userInput: string): { source: "fallback"; title: string } => ({
  source: "fallback",
  title: sanitizeTitle(userInput) || "新会话"
});

const sanitizeTitle = (value: string): string => value
  .replace(/[`*_#>\[\](){}"“”'‘’]/gu, "")
  .replace(/\s+/gu, " ")
  .trim()
  .slice(0, TITLE_MAX_CHARS);

export const sessionTitleDto = (session: SessionRecord): Record<string, unknown> => ({
  sessionId: session.id,
  title: session.title ?? "",
  titleSource: session.title_source ?? "fallback",
  updatedAt: session.updated_at
});
