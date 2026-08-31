import type { ContextItem } from "../inventory/context-item.js";
import type { ContextPromptMessage, ContextPromptRole } from "./context-prompt-message.js";

export type ContextSourcePromptMaterializationInput = {
  groupId: string;
  items: ContextItem[];
};

export interface ContextSourcePromptMaterializer {
  readonly id: string;
  materialize(input: ContextSourcePromptMaterializationInput): ContextPromptMessage | undefined;
}

export type ContextDefaultSourcePromptMaterializerOptions = {
  role?: ContextPromptRole;
};

export class ContextDefaultSourcePromptMaterializer implements ContextSourcePromptMaterializer {
  readonly id = "default-source-prompt-materializer";
  private readonly role: ContextPromptRole;

  constructor(options: ContextDefaultSourcePromptMaterializerOptions = {}) {
    this.role = options.role ?? "user";
  }

  materialize(input: ContextSourcePromptMaterializationInput): ContextPromptMessage | undefined {
    const contents = input.items.map((item) => materializeSourceItem(item)).filter(Boolean);
    if (contents.length === 0) {
      return undefined;
    }

    return {
      id: `context:${input.groupId}`,
      role: this.role,
      createdAt: new Date(0),
      content: {
        format: 2,
        parts: [{ type: "text", text: contents.join("\n\n") }]
      }
    };
  }
}

const materializeSourceItem = (item: ContextItem): string => {
  if (typeof item.content === "string") {
    return item.content;
  }

  try {
    return JSON.stringify(item.content);
  } catch {
    return String(item.content);
  }
};
