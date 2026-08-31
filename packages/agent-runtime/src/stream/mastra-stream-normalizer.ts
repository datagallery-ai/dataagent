import { randomUUID } from "node:crypto";

export type MastraStreamChunk = {
  type?: string;
  payload?: unknown;
  data?: unknown;
  runId?: string;
  from?: string;
  [key: string]: unknown;
};

export type MastraStreamNormalizerHooks = {
  onChunk?: (chunk: MastraStreamChunk) => void;
  onDataChunk?: (chunk: MastraStreamChunk) => void;
  onQuarantine?: (chunk: MastraStreamChunk) => void;
};

export type MastraAgentForAgUiOptions = {
  abortSignal?: AbortSignal | undefined;
};

const INTERNAL_CHUNK_TYPES = new Set([
  "abort",
  "text-start",
  "text-end",
  "tool-call-input-streaming-start",
  "tool-call-input-streaming-end",
  "tool-call-delta"
]);

/** Normalize Mastra fullStream chunks before @ag-ui/mastra consumes them. */
export async function* normalizeMastraFullStream(
  stream: AsyncIterable<MastraStreamChunk>,
  hooks: MastraStreamNormalizerHooks = {}
): AsyncGenerator<MastraStreamChunk> {
  const quarantined = new Set<string>();
  // Per-step assistant message segmentation. @mastra/core (1.49.x) emits every
  // `step-start` of a multi-step loop with the SAME payload.messageId, and
  // @ag-ui/mastra (1.1.x) blindly calls onMessageId() with it — so the current
  // assistant message id never advances and all iterations (reasoning + tool
  // calls) collapse into a single AG-UI message, i.e. one "step" in the UI.
  // We force a distinct id whenever a step-start reuses the previous one so the
  // bridge opens a new assistant message per iteration. Runs that already emit
  // distinct ids (e.g. durable loops) are left untouched.
  let previousStepStartMessageId: string | undefined;
  let reusedStepStartCount = 0;

  for await (const chunk of stream) {
    if (!chunk || typeof chunk !== "object") {
      continue;
    }
    hooks.onChunk?.(chunk);

    const type = typeof chunk.type === "string" ? chunk.type : undefined;

    if (type === "step-start" && isRecord(chunk.payload)) {
      const payload = chunk.payload;
      const messageId = typeof payload.messageId === "string" ? payload.messageId : undefined;
      if (messageId !== undefined) {
        if (messageId === previousStepStartMessageId) {
          reusedStepStartCount += 1;
          // Use a fresh UUID (not a derived suffix) so downstream consumers that
          // parse, dedupe, or prefix-match message ids treat this as a wholly new
          // assistant message with no relationship to the reused id.
          yield {
            ...chunk,
            payload: { ...payload, messageId: randomUUID() }
          };
          continue;
        }
        previousStepStartMessageId = messageId;
      }
    }

    // Mastra workspace tools emit data-* chunks (e.g. data-workspace-metadata) via
    // writer.custom() with a `data` field and no `payload`. @ag-ui/mastra rejects
    // chunks without payload, so route all data-* chunks out-of-band before AG-UI.
    if (type?.startsWith("data-")) {
      hooks.onDataChunk?.(chunk);
      continue;
    }

    if (type === "finish-step") {
      continue;
    }

    if (type && INTERNAL_CHUNK_TYPES.has(type)) {
      continue;
    }

    if (type === "tool-error" && isRecord(chunk.payload)) {
      const payload = chunk.payload;
      yield {
        ...chunk,
        type: "tool-result",
        payload: {
          toolCallId: payload.toolCallId,
          toolName: payload.toolName,
          result: {
            error: stringifyStreamError(payload.error),
            isError: true
          }
        }
      };
      continue;
    }

    if (chunk.payload === undefined) {
      const key = type ?? "unknown";
      if (!quarantined.has(key)) {
        quarantined.add(key);
        hooks.onQuarantine?.(chunk);
      }
      continue;
    }

    yield chunk;
  }
}

/** Wrap a local Mastra Agent so only fullStream is normalized for AG-UI consumption. */
export function wrapAgentForAgUi<TAgent extends object>(
  agent: TAgent,
  hooks: MastraStreamNormalizerHooks = {},
  options: MastraAgentForAgUiOptions = {}
): TAgent {
  return new Proxy(agent, {
    get(target, prop, receiver) {
      const value = Reflect.get(target, prop, receiver);

      if ((prop === "stream" || prop === "resumeStream") && typeof value === "function") {
        return async (...args: unknown[]) => {
          const response = await value.apply(target, withAbortSignal(args, options.abortSignal));
          if (!response || typeof response !== "object") {
            return response;
          }

          return new Proxy(response as object, {
            get(responseTarget, responseProp, responseReceiver) {
              if (responseProp === "fullStream") {
                const fullStream = Reflect.get(responseTarget, responseProp, responseReceiver);
                if (fullStream && typeof (fullStream as AsyncIterable<MastraStreamChunk>)[Symbol.asyncIterator] === "function") {
                  return normalizeMastraFullStream(fullStream as AsyncIterable<MastraStreamChunk>, hooks);
                }
                return fullStream;
              }
              return Reflect.get(responseTarget, responseProp, responseReceiver);
            }
          });
        };
      }

      if (typeof value === "function") {
        return value.bind(target);
      }

      return value;
    }
  }) as TAgent;
}

const withAbortSignal = (args: unknown[], abortSignal?: AbortSignal | undefined): unknown[] => {
  if (!abortSignal) {
    return args;
  }
  const nextArgs = [...args];
  const streamOptions = isRecord(nextArgs[1]) ? nextArgs[1] : {};
  nextArgs[1] = {
    ...streamOptions,
    abortSignal: mergeAbortSignals(streamOptions.abortSignal, abortSignal)
  };
  return nextArgs;
};

const mergeAbortSignals = (
  first: unknown,
  second: AbortSignal
): AbortSignal => {
  if (!(first instanceof AbortSignal)) {
    return second;
  }
  if (first === second) {
    return second;
  }
  if (typeof AbortSignal.any === "function") {
    return AbortSignal.any([first, second]);
  }
  if (first.aborted) {
    return AbortSignal.abort(first.reason);
  }
  if (second.aborted) {
    return AbortSignal.abort(second.reason);
  }
  const controller = new AbortController();
  const abort = (signal: AbortSignal): void => controller.abort(signal.reason);
  first.addEventListener("abort", () => abort(first), { once: true });
  second.addEventListener("abort", () => abort(second), { once: true });
  return controller.signal;
};

const stringifyStreamError = (error: unknown): string => {
  if (error instanceof Error) {
    return error.message;
  }
  if (typeof error === "string") {
    return error;
  }
  try {
    return JSON.stringify(error);
  } catch {
    return String(error);
  }
};

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
