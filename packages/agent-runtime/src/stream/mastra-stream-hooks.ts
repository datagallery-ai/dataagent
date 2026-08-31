import { createCustomEvent } from "../events.js";
import type { AgUiEventEmitter } from "../types.js";
import type { MastraStreamChunk, MastraStreamNormalizerHooks } from "./mastra-stream-normalizer.js";

const SANDBOX_DATA_TYPES = new Set([
  "data-sandbox-stdout",
  "data-sandbox-stderr",
  "data-sandbox-exit",
  "data-sandbox-command",
]);

const STEP_END_CHUNK_TYPES = new Set(["step-finish", "finish-step"]);

/** Map Mastra data-* stream chunks to AG-UI CUSTOM events for persistence and UI. */
export const createMastraStreamNormalizerHooks = (
  emitter: AgUiEventEmitter,
  options: {
    onWorkspaceMetadata?: (metadata: unknown) => Promise<void> | void;
  } = {},
): MastraStreamNormalizerHooks => {
  const emittedStepUsageKeys = new Set<string>();
  let completedSteps = 0;

  return {
    onChunk(chunk: MastraStreamChunk) {
      const type = typeof chunk.type === "string" ? chunk.type : undefined;
      if (!type || !STEP_END_CHUNK_TYPES.has(type)) {
        return;
      }

      const usageEvent = tokenUsageEventFromChunk(chunk, {});
      if (!usageEvent) {
        if (type === "step-finish") {
          completedSteps += 1;
        }
        return;
      }

      const toolCallId = stringValue(usageEvent.tool_call_id);
      const toolName = stringValue(usageEvent.tool_name);
      const dedupeKey = [
        toolCallId ?? "",
        toolName ?? "",
        String(usageEvent.input_tokens ?? 0),
        String(usageEvent.output_tokens ?? 0),
        stringValue(usageEvent.model) ?? "",
      ].join("|");
      if (emittedStepUsageKeys.has(dedupeKey)) {
        return;
      }
      emittedStepUsageKeys.add(dedupeKey);

      completedSteps += 1;
      usageEvent.step_number = completedSteps;
      emitter.emit(createCustomEvent("token_usage", usageEvent));
    },
    onDataChunk(chunk: MastraStreamChunk) {
      const type = typeof chunk.type === "string" ? chunk.type : undefined;
      if (!type?.startsWith("data-")) {
        return;
      }

      if (type === "data-workspace-metadata") {
        emitter.emit(createCustomEvent("workspace.metadata", chunk.data));
        void Promise.resolve(options.onWorkspaceMetadata?.(chunk.data)).catch((error) => {
          console.warn("[data-foundry] workspace_metadata_hook_failed", error);
        });
        return;
      }

      if (SANDBOX_DATA_TYPES.has(type)) {
        const kind = type.slice("data-sandbox-".length);
        const data = isRecord(chunk.data) ? chunk.data : { value: chunk.data };
        emitter.emit(createCustomEvent("sandbox.output", { kind, ...data }));
      }
    },
  };
};

export const tokenUsageEventFromChunk = (
  chunk: MastraStreamChunk,
  context: { stepNumber?: number } = {},
): Record<string, unknown> | undefined => {
  const payload = isRecord(chunk.payload) ? chunk.payload : undefined;
  const data = isRecord(chunk.data) ? chunk.data : undefined;
  const output = isRecord(payload?.output) ? payload.output : undefined;
  const usage =
    usageRecord(chunk.usage) ??
    usageRecord(payload?.usage) ??
    usageRecord(output?.usage) ??
    usageRecord(data?.usage);
  if (!usage) {
    return undefined;
  }
  const inputTokens = tokenCount(usage.inputTokens) ?? tokenCount(usage.promptTokens);
  const outputTokens = tokenCount(usage.outputTokens) ?? tokenCount(usage.completionTokens);
  if (inputTokens === undefined && outputTokens === undefined) {
    return undefined;
  }

  const toolCalls = arrayValue(output?.toolCalls);
  const lastToolCall = toolCalls.at(-1);
  const toolCallRecord = isRecord(lastToolCall) ? lastToolCall : undefined;
  const toolCallId =
    stringValue(toolCallRecord?.toolCallId) ?? stringValue(toolCallRecord?.id);
  const toolName = stringValue(toolCallRecord?.toolName) ?? stringValue(toolCallRecord?.name);
  const modelInfo = isRecord(payload?.model) ? payload.model : undefined;
  const stepNumber =
    context.stepNumber ??
    numericValue(chunk.stepNumber) ??
    numericValue(payload?.stepNumber);

  return {
    input_tokens: inputTokens ?? 0,
    output_tokens: outputTokens ?? 0,
    prompt_tokens: inputTokens ?? 0,
    completion_tokens: outputTokens ?? 0,
    ...(tokenCount(usage.totalTokens) !== undefined
      ? { total_tokens: tokenCount(usage.totalTokens) }
      : {}),
    ...(stepNumber !== undefined ? { step_number: stepNumber } : {}),
    ...(toolCallId ? { tool_call_id: toolCallId } : {}),
    ...(toolName ? { tool_name: toolName } : {}),
    ...(stringValue(modelInfo?.modelId) ? { model: stringValue(modelInfo?.modelId) } : {}),
    ...(typeof chunk.runId === "string" ? { run_id: chunk.runId } : {}),
    ...(typeof chunk.from === "string" ? { source: chunk.from } : {}),
  };
};

const usageRecord = (value: unknown): Record<string, unknown> | undefined =>
  isRecord(value) ? value : undefined;

const tokenCount = (value: unknown): number | undefined => {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  if (isRecord(value)) {
    return numericValue(value.total) ?? numericValue(value.text);
  }
  return undefined;
};

const arrayValue = (value: unknown): unknown[] => (Array.isArray(value) ? value : []);

const stringValue = (value: unknown): string | undefined =>
  typeof value === "string" && value.length > 0 ? value : undefined;

const numericValue = (value: unknown): number | undefined =>
  typeof value === "number" && Number.isFinite(value) ? value : undefined;

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
