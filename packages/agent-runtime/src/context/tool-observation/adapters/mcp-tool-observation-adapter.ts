import type { ContextBudget } from "../../inventory/context-budget.js";
import type { ContextTruncation } from "../../inventory/context-package.js";
import type { ContextItem } from "../../inventory/context-item.js";
import { truncateString } from "../../inventory/context-text.js";
import {
  toolObservationProjectionToItems,
  type ToolObservationProjection
} from "../tool-observation-projection-items.js";
import { asRecord, BaseToolObservationAdapter } from "./base-tool-observation-adapter.js";

export class McpToolObservationAdapter extends BaseToolObservationAdapter {
  readonly resultType: string;

  constructor(readonly toolName: string) {
    super();
    this.resultType = `mcp-${toolName}`;
  }

  toContextItems(raw: unknown, budget: ContextBudget): ContextItem[] {
    return toolObservationProjectionToItems(
      projectMcpToolObservation(this.toolName, raw, resolveMcpMaxChars(budget)),
      this.resultType,
      10
    );
  }

  protected project(raw: unknown): unknown {
    return asRecord(raw);
  }
}

const projectMcpToolObservation = (
  toolName: string,
  raw: unknown,
  maxChars: number
): ToolObservationProjection => {
  const serialized = serializeMcpResult(raw);
  const truncated = truncateString(serialized, maxChars);
  const content = truncated?.value ?? serialized;
  const truncation: ContextTruncation[] = truncated?.truncated
    ? [{
        sourceId: toolName,
        truncated: true,
        reason: `MCP tool result exceeded ${maxChars} chars`,
        originalSize: serialized.length,
        returnedSize: content.length
      }]
    : [];
  const model = {
    tool_name: toolName,
    content,
    content_format: typeof raw === "string" ? "text" : "json",
    ...(truncation[0] ? { context: { truncation: truncation[0] } } : {})
  };
  return {
    model,
    activity: model,
    artifactRefs: [],
    auditRefs: [],
    truncation
  };
};

const resolveMcpMaxChars = (budget: ContextBudget): number => {
  const configured = budget.sourceLimits?.maxChars ?? budget.maxChars;
  return Math.max(1000, Math.min(32000, Math.floor(configured ?? 12000)));
};

const serializeMcpResult = (raw: unknown): string => {
  if (typeof raw === "string") {
    return raw;
  }
  try {
    return JSON.stringify(raw);
  } catch {
    return String(raw);
  }
};
