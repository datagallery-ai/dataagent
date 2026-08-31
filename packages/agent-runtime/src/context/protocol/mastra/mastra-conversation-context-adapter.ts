import type { MastraDBMessage } from "@mastra/core/agent";

import { createContextItem, type ContextItem } from "../../inventory/context-item.js";
import { createContextSourceMetadata } from "../../inventory/context-source-metadata.js";
import { toContextPromptMessage } from "./mastra-context-prompt-message-adapter.js";
import { classifyMastraContextSourceMessage } from "./mastra-context-source-message.js";
import { groupMessagesByTurn, isToolObservationMessage } from "./mastra-message-utils.js";

export type MastraConversationContextAdapterOptions = {
  messages: MastraDBMessage[];
  systemMessages: unknown[];
};

export class MastraConversationContextAdapter {
  constructor(private readonly options: MastraConversationContextAdapterOptions) {}

  collect(): ContextItem[] {
    return createMastraConversationContextItems(this.options.messages, this.options.systemMessages);
  }
}

export const createMastraConversationContextItems = (
  messages: MastraDBMessage[],
  systemMessages: unknown[]
): ContextItem[] => {
  const items = systemMessages.map((message, index) => createContextItem({
    id: `system-${index}`,
    sourceType: "system",
    sourceId: `system-${index}`,
    groupId: `system-${index}`,
    visibility: "model",
    trust: "runtime",
    retention: "mandatory",
    priority: 100,
    content: message,
    metadata: createContextSourceMetadata({
      dedupeKeys: [`system:${index}`],
      exclusivityKey: `system:${index}`,
      sourceKind: "runtime-system",
      sourceOwner: "agent-runtime"
    }, { atomic: true, groupKind: "system", messageKind: "system" })
  }));

  groupMessagesByTurn(messages).forEach((group) => {
    group.members.forEach((member) => {
      const sourceMessage = classifyMastraContextSourceMessage(member.message);
      const toolObservation = isToolObservationMessage(member.message);
      items.push(createContextItem({
        id: `message-${member.id}`,
        sourceType: sourceMessage?.sourceType ?? (toolObservation ? "tool-observation" : "conversation"),
        sourceId: member.id,
        groupId: sourceMessage?.groupId ?? group.id,
        visibility: "model",
        trust: sourceMessage ? "memory" : "untrusted-client",
        retention: sourceMessage ? "supporting" : group.retention,
        priority: sourceMessage?.priority ?? (group.isCurrent ? 80 : 40),
        content: toContextPromptMessage(member.message),
        metadata: createContextSourceMetadata({
          dedupeKeys: [`message:${member.id}`],
          exclusivityKey: sourceMessage?.exclusivityKey
            ?? (toolObservation ? `tool-observation-message:${member.id}` : `conversation-turn:${group.id}`),
          sourceKind: sourceMessage?.sourceType ?? (toolObservation ? "tool-observation" : "conversation"),
          sourceOwner: sourceMessage?.sourceOwner ?? (toolObservation ? "mastra-tool-message" : "conversation-history")
        }, {
          atomic: sourceMessage?.atomic ?? true,
          groupKind: sourceMessage ? "source" : "turn",
          mandatory: group.mandatory,
          messageKind: sourceMessage?.messageKind ?? "message",
          role: member.message.role
        })
      }));
    });
  });

  return items;
};
