import type { MastraDBMessage } from "@mastra/core/agent";

import type { ContextProtocolEventSink } from "../context-protocol-event-sink.js";
import type { ToolObservationDispatcher } from "../../tool-observation/tool-observation-dispatcher.js";
import { toolObservationModelFromPackage } from "../../tool-observation/tool-observation-projection-items.js";
import { filterModelVisibleMastraMessages } from "./mastra-message-utils.js";

export type MastraToolObservationRouterOptions = {
  dispatcher: ToolObservationDispatcher;
  eventSink: ContextProtocolEventSink;
};

type ToolObservation = {
  key: string;
  rawResult: unknown;
  replaceResult(result: unknown): unknown;
  toolName: string;
};

export class MastraToolObservationRouter {
  private readonly governedResults = new Map<string, unknown>();
  private readonly missingAdapters = new Set<string>();

  constructor(private readonly options: MastraToolObservationRouterOptions) {}

  /** Route completed Mastra tool observations through their registered adapters. */
  governMessages(messages: MastraDBMessage[]): MastraDBMessage[] {
    return filterModelVisibleMastraMessages(messages).map((message) => ({
      ...message,
      content: {
        ...message.content,
        parts: message.content.parts.map((part) => this.governPart(part))
      }
    })) as MastraDBMessage[];
  }

  private governPart(part: unknown): unknown {
    const observation = extractToolObservation(part);
    if (!observation || this.options.dispatcher.isGoverned(observation.rawResult)) {
      return part;
    }

    const cached = this.governedResults.get(observation.key);
    if (cached !== undefined) {
      return observation.replaceResult(cached);
    }

    try {
      const contextPackage = this.options.dispatcher.dispatch(observation.toolName, observation.rawResult);
      const modelResult = toolObservationModelFromPackage(contextPackage);
      this.governedResults.set(observation.key, modelResult);
      return observation.replaceResult(modelResult);
    } catch (error) {
      if (!(error instanceof Error) || !error.message.startsWith("CONTEXT_ADAPTER_REQUIRED:")) {
        throw error;
      }
      const fallback = createFallbackResult(observation.rawResult);
      this.governedResults.set(observation.key, fallback);
      this.emitMissingAdapter(observation.toolName);
      return observation.replaceResult(fallback);
    }
  }

  private emitMissingAdapter(toolName: string): void {
    if (this.missingAdapters.has(toolName)) {
      return;
    }
    this.missingAdapters.add(toolName);
    this.options.eventSink.emitContextEvent("context.adapter-missing", { tool_name: toolName });
  }
}

const extractToolObservation = (part: unknown): ToolObservation | undefined => {
  if (!isRecord(part) || typeof part.type !== "string") {
    return undefined;
  }
  if (part.type === "tool-invocation" && isRecord(part.toolInvocation)) {
    const invocation = part.toolInvocation;
    if (!isCompletedToolState(invocation.state) || typeof invocation.toolName !== "string") {
      return undefined;
    }
    const key = stringValue(invocation.toolCallId) ?? `${invocation.toolName}:${stableResultKey(invocation)}`;
    const rawResult = invocation.result ?? invocation.output ?? invocation.error;
    return {
      key,
      rawResult,
      toolName: invocation.toolName,
      replaceResult: (result) => ({ ...part, toolInvocation: { ...invocation, result } })
    };
  }
  if (part.type === "tool-result" && typeof part.toolName === "string") {
    const key = stringValue(part.toolCallId) ?? `${part.toolName}:${stableResultKey(part)}`;
    return {
      key,
      rawResult: part.result ?? part.output,
      toolName: part.toolName,
      replaceResult: (result) => ({ ...part, result })
    };
  }
  if (part.type.startsWith("tool-") && ("output" in part || "result" in part)) {
    const toolName = stringValue(part.toolName) ?? part.type.slice("tool-".length);
    const key = stringValue(part.toolCallId) ?? `${toolName}:${stableResultKey(part)}`;
    return {
      key,
      rawResult: part.output ?? part.result,
      toolName,
      replaceResult: (result) => ({ ...part, output: result })
    };
  }
  return undefined;
};

const createFallbackResult = (raw: unknown): Record<string, unknown> => {
  const serialized = safeSerialize(raw);
  return {
    adapter_missing: true,
    original_chars: serialized.length,
    preview: serialized.slice(0, 4000)
  };
};

const safeSerialize = (value: unknown): string => {
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
};

const stableResultKey = (value: unknown): string => safeSerialize(value).slice(0, 256);
const stringValue = (value: unknown): string | undefined => typeof value === "string" && value ? value : undefined;
const isCompletedToolState = (state: unknown): boolean =>
  state === "result" || state === "output-available" || state === "output-error" || state === "output-denied";
const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null;
