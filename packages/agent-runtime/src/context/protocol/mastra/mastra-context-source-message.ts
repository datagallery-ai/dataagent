import type { MastraDBMessage } from "@mastra/core/agent";

export type MastraContextSourceMessageClassification = {
  atomic: boolean;
  exclusivityKey: string;
  groupId: string;
  messageKind: "message" | "source-message";
  priority: number;
  sourceOwner: string;
  sourceType: string;
};

export const classifyMastraContextSourceMessage = (
  message: MastraDBMessage
): MastraContextSourceMessageClassification | undefined => {
  if (typeof message.id !== "string") {
    return undefined;
  }
  if (message.id.startsWith("memory-summary:")) {
    return {
      atomic: false,
      exclusivityKey: "compact-conversation-memory",
      groupId: "compact-conversation-memory",
      messageKind: "source-message",
      priority: 45,
      sourceOwner: "metadata-summary",
      sourceType: "compact-conversation-memory"
    };
  }
  if (message.id === "context:long-term-memory") {
    return {
      atomic: false,
      exclusivityKey: "long-term-memory:metadata-ltm",
      groupId: "long-term-memory",
      messageKind: "source-message",
      priority: 35,
      sourceOwner: "metadata-ltm",
      sourceType: "long-term-memory"
    };
  }
  return undefined;
};
