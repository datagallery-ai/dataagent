import type { ContextBudget } from "../../inventory/context-budget.js";
import { createContextItem, type ContextItem } from "../../inventory/context-item.js";
import { createContextSourceMetadata } from "../../inventory/context-source-metadata.js";
import type { ToolObservationAdapter } from "../tool-observation-adapter.js";

export abstract class BaseToolObservationAdapter implements ToolObservationAdapter {
  abstract readonly resultType: string;
  abstract readonly toolName: string;
  readonly sourceType = "tool-observation";
  protected readonly modelGroupKind: string = "tool-exchange";
  protected readonly sourceKind: string = "tool-observation";
  protected readonly sourceOwner: string = "tool";

  /** Project one tool's raw result into bounded model and activity context. */
  toContextItems(raw: unknown, budget: ContextBudget): ContextItem[] {
    const projected = this.project(raw);
    const bounded = boundStructuredValue(projected, budget.maxChars ?? 12000);
    const groupId = `${this.resultType}-observation`;
    const dedupeKeys = this.createDedupeKeys(projected);
    const exclusivityKey = this.createExclusivityKey(projected);
    const overlapKeys = this.createOverlapKeys(projected);
    return [
      createContextItem({
        id: `${this.resultType}-model`,
        sourceType: this.resultType,
        sourceId: this.toolName,
        groupId,
        visibility: "model",
        trust: "tool",
        retention: "supporting",
        priority: 20,
        content: bounded,
        metadata: createContextSourceMetadata({
          dedupeKeys,
          exclusivityKey,
          overlapKeys,
          sourceKind: this.sourceKind,
          sourceOwner: this.sourceOwner
        }, { atomic: true, groupKind: this.modelGroupKind, toolName: this.toolName })
      }),
      createContextItem({
        id: `${this.resultType}-activity`,
        sourceType: this.resultType,
        sourceId: this.toolName,
        groupId,
        visibility: "activity",
        trust: "tool",
        retention: "reference",
        priority: 10,
        content: bounded,
        metadata: createContextSourceMetadata({
          dedupeKeys,
          exclusivityKey,
          overlapKeys,
          sourceKind: this.sourceKind,
          sourceOwner: this.sourceOwner
        }, { atomic: true, groupKind: "reference", toolName: this.toolName })
      })
    ];
  }

  protected abstract project(raw: unknown): unknown;

  protected createDedupeKeys(_projected: unknown): string[] {
    return [`tool-observation:${this.toolName}`];
  }

  protected createExclusivityKey(_projected: unknown): string {
    return `tool-observation:${this.toolName}`;
  }

  protected createOverlapKeys(_projected: unknown): string[] {
    return [];
  }
}

const boundStructuredValue = (value: unknown, maxChars: number): unknown => {
  const serialized = safeSerialize(value);
  if (serialized.length <= maxChars) {
    return value;
  }

  const reservedChars = 160;
  return {
    original_chars: serialized.length,
    preview: serialized.slice(0, Math.max(maxChars - reservedChars, 0)),
    truncated: true
  };
};

const safeSerialize = (value: unknown): string => {
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
};

export const asRecord = (value: unknown): Record<string, unknown> =>
  typeof value === "object" && value !== null ? value as Record<string, unknown> : { value };

export const pickFields = (value: unknown, fields: string[]): Record<string, unknown> => {
  const record = asRecord(value);
  return Object.fromEntries(
    fields.filter((field) => record[field] !== undefined).map((field) => [field, record[field]])
  );
};
