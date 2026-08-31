import type { ContextItem, ContextRetention } from "../inventory/context-item.js";
import type { ContextPackage } from "../inventory/context-package.js";
import type { PromptTokenReport } from "../inventory/context-token-report.js";
import { isContextPromptMessage, type ContextPromptMessage } from "./context-prompt-message.js";
import type { ContextPromptView } from "./context-prompt-view.js";
import {
  ContextDefaultSourcePromptMaterializer,
  type ContextSourcePromptMaterializer
} from "./context-source-prompt-materializer.js";

export type ContextPromptGroup = {
  id: string;
  retention: ContextRetention;
  mandatory: boolean;
  order: number;
  messages: ContextPromptMessage[];
};

export type ContextPromptGroupPlan = {
  groups: ContextPromptGroup[];
  systemMessages: unknown[];
};

export type CreateContextPromptGroupsInput = {
  contextPackage: ContextPackage;
  sourceItemIds?: ReadonlySet<string>;
};

export type MaterializeContextPromptViewInput = {
  groups: ContextPromptGroup[];
  selectedGroupIds: ReadonlySet<string>;
  systemMessages: unknown[];
  tokenReport: PromptTokenReport;
};

export type ContextPromptMaterializerOptions = {
  sourceMaterializer?: ContextSourcePromptMaterializer;
};

export class ContextPromptMaterializer {
  private readonly sourceMaterializer: ContextSourcePromptMaterializer;

  constructor(options: ContextPromptMaterializerOptions = {}) {
    this.sourceMaterializer = options.sourceMaterializer ?? new ContextDefaultSourcePromptMaterializer();
  }

  createGroups(input: CreateContextPromptGroupsInput): ContextPromptGroup[] {
    return this.createGroupPlan(input).groups;
  }

  createGroupPlan(input: CreateContextPromptGroupsInput): ContextPromptGroupPlan {
    const sourcePlan = this.createSourceGroups(input);
    const sourceGroups = sourcePlan.groups;
    const messageGroups = this.createMessageGroups(input, sourceGroups.length);
    return {
      groups: [...sourceGroups, ...messageGroups],
      systemMessages: this.createSystemMessages(input)
    };
  }

  materializePromptView(input: MaterializeContextPromptViewInput): ContextPromptView {
    const messages = input.groups
      .filter((group) => input.selectedGroupIds.has(group.id))
      .flatMap((group) => group.messages);

    return {
      messages,
      systemMessages: input.systemMessages,
      tokenReport: input.tokenReport
    };
  }

  private createSourceGroups(input: CreateContextPromptGroupsInput): { groups: ContextPromptGroup[] } {
    const sourceGroups = input.contextPackage.groups.filter((group) => group.kind === "source");
    const groups = sourceGroups
      .map((group, index) => {
        const items = this.sourceGroupItems(input.contextPackage, group.itemIds)
          .filter((item) => !input.sourceItemIds || input.sourceItemIds.has(item.id));
        const message = this.sourceMaterializer.materialize({
          groupId: group.id,
          items
        });
        return {
          id: group.id,
          order: index,
          retention: resolveGroupRetention(items),
          mandatory: items.some((item) => item.retention === "mandatory" || item.metadata.mandatory === true),
          messages: message ? [message] : []
        };
      })
      .filter((group) => group.messages.length > 0);
    return { groups };
  }

  private createSystemMessages(input: CreateContextPromptGroupsInput): unknown[] {
    return input.contextPackage.groups
      .filter((group) => group.kind === "system")
      .sort((left, right) =>
        groupOrder(input.contextPackage, left.itemIds) - groupOrder(input.contextPackage, right.itemIds)
      )
      .flatMap((group) =>
        group.itemIds
          .map((itemId) => input.contextPackage.items.find((item) => item.id === itemId))
          .filter((item): item is ContextItem => Boolean(item))
          .filter((item) => item.visibility === "model" && item.metadata.messageKind === "system")
          .map((item) => item.content)
      );
  }

  private sourceGroupItems(contextPackage: ContextPackage, itemIds: string[]): ContextItem[] {
    return contextPackage.items.filter((item) =>
      itemIds.includes(item.id)
      && item.visibility === "model"
      && item.metadata.messageKind !== "message"
    );
  }

  private createMessageGroups(
    input: CreateContextPromptGroupsInput,
    orderOffset: number
  ): ContextPromptGroup[] {
    return input.contextPackage.groups
      .filter((group) => group.kind === "turn")
      .map((group) => {
        const items = group.itemIds
          .map((itemId) => input.contextPackage.items.find((item) => item.id === itemId))
          .filter((item): item is ContextItem => Boolean(item))
          .filter((item) => item.visibility === "model" && item.metadata.messageKind === "message");
        const messages = items.map((item) => item.content).filter(isContextPromptMessage);
        return {
          id: group.id,
          order: orderOffset + groupOrder(input.contextPackage, group.itemIds),
          retention: resolveGroupRetention(items),
          mandatory: items.some((item) => item.metadata.mandatory === true || item.retention === "mandatory"),
          messages
        };
      })
      .filter((group) => group.messages.length > 0);
  }
}

const groupOrder = (contextPackage: ContextPackage, itemIds: string[]): number => {
  const indexes = itemIds
    .map((itemId) => contextPackage.items.findIndex((item) => item.id === itemId))
    .filter((index) => index >= 0);
  return indexes.length > 0 ? Math.min(...indexes) : Number.MAX_SAFE_INTEGER;
};

const resolveGroupRetention = (items: ContextItem[]): ContextRetention => {
  if (items.some((item) => item.retention === "mandatory")) {
    return "mandatory";
  }
  if (items.some((item) => item.retention === "active")) {
    return "active";
  }
  if (items.some((item) => item.retention === "supporting")) {
    return "supporting";
  }
  if (items.some((item) => item.retention === "historical")) {
    return "historical";
  }
  return "reference";
};
