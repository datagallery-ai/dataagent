import type { Memory } from "@mastra/memory";

export const CONVERSATION_WORKING_MEMORY_TEMPLATE =
  "# Conversation Summary\n- Range:\n- Durable facts:\n- User constraints:\n- Open questions:";

export type ConversationMemoryProjection = {
  fromPosition: number;
  summaryText: string;
  toPosition: number;
};

export type ConversationMemoryBridge = {
  mirrorSummary(input: {
    projection: ConversationMemoryProjection;
    resourceId: string;
    threadId: string;
  }): Promise<void>;
};

export type MastraConversationMemoryBridgeInput = {
  memory: Memory;
};

export class MastraConversationMemoryBridge implements ConversationMemoryBridge {
  private readonly memory: Memory;

  constructor(input: MastraConversationMemoryBridgeInput) {
    this.memory = input.memory;
  }

  async mirrorSummary(input: {
    projection: ConversationMemoryProjection;
    resourceId: string;
    threadId: string;
  }): Promise<void> {
    const currentThread = await this.memory.getThreadById({
      threadId: input.threadId,
      resourceId: input.resourceId
    });
    if (!currentThread) {
      await this.memory.createThread({
        threadId: input.threadId,
        resourceId: input.resourceId,
        saveThread: true,
        memoryConfig: CONVERSATION_WORKING_MEMORY_CONFIG
      });
    }
    await this.memory.updateWorkingMemory({
      threadId: input.threadId,
      resourceId: input.resourceId,
      workingMemory: formatConversationProjection(input.projection),
      memoryConfig: CONVERSATION_WORKING_MEMORY_CONFIG
    });
  }
}

export const createMastraConversationMemoryBridge = (
  input: MastraConversationMemoryBridgeInput
): MastraConversationMemoryBridge => new MastraConversationMemoryBridge(input);

export const formatConversationProjection = (projection: ConversationMemoryProjection): string =>
  [
    "# Conversation Summary",
    `from_position: ${projection.fromPosition}`,
    `to_position: ${projection.toPosition}`,
    "",
    projection.summaryText.trim()
  ].join("\n");

export const CONVERSATION_WORKING_MEMORY_CONFIG = {
  readOnly: true,
  workingMemory: {
    enabled: true,
    scope: "thread" as const,
    template: CONVERSATION_WORKING_MEMORY_TEMPLATE
  }
};
