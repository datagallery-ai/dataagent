import { describe, expect, it } from "vitest";

import {
  createMastraStreamNormalizerHooks,
  tokenUsageEventFromChunk,
} from "../../../../../../packages/agent-runtime/src/stream/mastra-stream-hooks";
import {
  normalizeMastraFullStream,
  wrapAgentForAgUi,
} from "../../../../../../packages/agent-runtime/src/stream/mastra-stream-normalizer";

describe("tokenUsageEventFromChunk", () => {
  it("extracts usage, tool_call_id, and tool_name from step output", () => {
    const event = tokenUsageEventFromChunk({
      type: "step-finish",
      payload: {
        output: {
          usage: { inputTokens: 1200, outputTokens: 340, totalTokens: 1540 },
          toolCalls: [
            {
              toolCallId: "tool-sql-1",
              toolName: "run_sql_readonly",
            },
          ],
        },
        model: { modelId: "qwen-plus" },
      },
    });

    expect(event).toMatchObject({
      input_tokens: 1200,
      output_tokens: 340,
      total_tokens: 1540,
      tool_call_id: "tool-sql-1",
      tool_name: "run_sql_readonly",
      model: "qwen-plus",
    });
  });

  it("returns undefined when chunk has no usage", () => {
    expect(tokenUsageEventFromChunk({ type: "text-delta", payload: { text: "hi" } })).toBeUndefined();
  });
});

describe("createMastraStreamNormalizerHooks token emission", () => {
  it("emits token_usage only on step end chunks and dedupes finish-step pairs", () => {
    const events: Array<{ name?: string; value?: Record<string, unknown> }> = [];
    const emitter = {
      emit: (event: { name?: string; value?: unknown }) => {
        events.push({
          name: event.name,
          value: event.value as Record<string, unknown>,
        });
      },
    };

    const hooks = createMastraStreamNormalizerHooks(emitter);
    const usageChunk = {
      type: "step-finish",
      payload: {
        output: {
          usage: { inputTokens: 500, outputTokens: 100 },
          toolCalls: [{ toolCallId: "tool-1", toolName: "run_sql_readonly" }],
        },
      },
    };

    hooks.onChunk?.({ type: "text-delta", payload: { text: "partial" } });
    hooks.onChunk?.({ type: "finish-step", ...usageChunk });
    hooks.onChunk?.(usageChunk);

    expect(events).toHaveLength(1);
    expect(events[0]).toMatchObject({
      name: "token_usage",
      value: {
        input_tokens: 500,
        output_tokens: 100,
        step_number: 1,
        tool_call_id: "tool-1",
        tool_name: "run_sql_readonly",
      },
    });
  });
});

describe("normalizeMastraFullStream", () => {
  it("drops Mastra internal chunks before AG-UI processing", async () => {
    const chunks = normalizeMastraFullStream(streamFrom([
      { type: "abort", payload: { reason: "cancelled" } },
      { type: "text-start", payload: { id: "msg-1" } },
      { type: "finish", payload: {} },
    ]));

    await expect(collectStream(chunks)).resolves.toEqual([{ type: "finish", payload: {} }]);
  });
});

describe("wrapAgentForAgUi abort signal passthrough", () => {
  it("passes the run abort signal into stream options", async () => {
    const runAbortController = new AbortController();
    let receivedSignal: AbortSignal | undefined;
    const agent = wrapAgentForAgUi(
      {
        async stream(_messages: unknown, options?: { abortSignal?: AbortSignal; marker?: string }) {
          receivedSignal = options?.abortSignal;
          return { fullStream: emptyStream() };
        },
      },
      {},
      { abortSignal: runAbortController.signal },
    );

    await agent.stream([], { marker: "kept" });

    expect(receivedSignal).toBe(runAbortController.signal);
  });

  it("merges an existing stream abort signal with the run abort signal", async () => {
    const upstreamAbortController = new AbortController();
    const runAbortController = new AbortController();
    let receivedSignal: AbortSignal | undefined;
    const agent = wrapAgentForAgUi(
      {
        async stream(_messages: unknown, options?: { abortSignal?: AbortSignal }) {
          receivedSignal = options?.abortSignal;
          return { fullStream: emptyStream() };
        },
      },
      {},
      { abortSignal: runAbortController.signal },
    );

    await agent.stream([], { abortSignal: upstreamAbortController.signal });
    runAbortController.abort(new Error("run cancelled"));

    expect(receivedSignal).not.toBe(upstreamAbortController.signal);
    expect(receivedSignal?.aborted).toBe(true);
  });

  it("passes the run abort signal into resumeStream options", async () => {
    const runAbortController = new AbortController();
    let receivedSignal: AbortSignal | undefined;
    const agent = wrapAgentForAgUi(
      {
        async resumeStream(_resumeData: unknown, options?: { abortSignal?: AbortSignal }) {
          receivedSignal = options?.abortSignal;
          return { fullStream: emptyStream() };
        },
      },
      {},
      { abortSignal: runAbortController.signal },
    );

    await agent.resumeStream({ answer: "yes" }, {});

    expect(receivedSignal).toBe(runAbortController.signal);
  });
});

async function* emptyStream() {
  yield { type: "finish", payload: {} };
}

async function* streamFrom(chunks: Array<{ payload?: unknown; type: string }>) {
  for (const chunk of chunks) {
    yield chunk;
  }
}

async function collectStream<T>(stream: AsyncIterable<T>): Promise<T[]> {
  const chunks: T[] = [];
  for await (const chunk of stream) {
    chunks.push(chunk);
  }
  return chunks;
}
