import type { ContextItem } from "../inventory/context-item.js";
import { createContextItem } from "../inventory/context-item.js";
import { createContextSourceMetadata } from "../inventory/context-source-metadata.js";
import type { RuntimeContextSource, RuntimeContextSourceInput } from "./runtime-context-source.js";

export interface WorkingMemoryProjectionReader {
  getWorkingMemory(input: { resourceId: string; threadId: string }): Promise<null | string | undefined>;
}

export type WorkingMemoryProjectionContextSourceOptions = {
  memory: WorkingMemoryProjectionReader;
};

export class WorkingMemoryProjectionContextSource implements RuntimeContextSource {
  readonly sourceType = "compact-conversation-memory";

  constructor(private readonly options: WorkingMemoryProjectionContextSourceOptions) {}

  async collect(input: RuntimeContextSourceInput): Promise<ContextItem[]> {
    const workingMemory = await this.options.memory.getWorkingMemory({
      resourceId: input.userId,
      threadId: input.sessionId
    });
    if (!workingMemory?.trim()) {
      return [];
    }

    return [
      createContextItem({
        id: "working-memory-projection",
        sourceType: this.sourceType,
        sourceId: "mastra-working-memory",
        groupId: "compact-conversation-memory",
        visibility: "model",
        trust: "memory",
        retention: "supporting",
        priority: 30,
        content: boundText(workingMemory, input.budget.maxChars ?? 4000),
        metadata: createContextSourceMetadata({
          dedupeKeys: ["compact-conversation-memory:mastra-working-memory"],
          exclusivityKey: "compact-conversation-memory",
          shadow: true,
          scope: {
            sessionId: input.sessionId,
            userId: input.userId
          },
          sourceKind: "compact-conversation-memory",
          sourceOwner: "mastra-working-memory"
        }, { atomic: false, groupKind: "source" })
      })
    ];
  }
}

const boundText = (text: string, maxChars: number): string => {
  if (text.length <= maxChars) {
    return text;
  }
  return `${text.slice(0, Math.max(maxChars - 80, 0))}\n[working memory truncated: original_chars=${text.length}]`;
};
