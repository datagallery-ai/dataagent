import type { MastraDBMessage } from "@mastra/core/agent";
import type { ProcessInputStepArgs } from "@mastra/core/processors";

export type MastraPromptSnapshot = {
  messages: MastraDBMessage[];
};

type MastraMessageList = NonNullable<ProcessInputStepArgs["messageList"]>;

// Mastra applies returned message arrays with per-message remove/add. Context compilation
// produces a complete prompt snapshot, so apply it atomically to avoid assistant-message merging.
export const applyMastraPromptSnapshot = (
  messageList: MastraMessageList,
  snapshot: MastraPromptSnapshot
): void => {
  const sourceCheck = messageList.makeMessageSourceChecker();
  const currentIds = messageList.get.all.db().map((message) => message.id).filter(isString);

  if (currentIds.length > 0) {
    messageList.removeByIds(currentIds);
  }

  for (const message of snapshot.messages) {
    messageList.add(message, sourceCheck.getSource(message) ?? "input");
  }
};

const isString = (value: unknown): value is string => typeof value === "string";
