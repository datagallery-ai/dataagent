import assert from "node:assert/strict";
import { describe, it } from "node:test";
import type { BaseEvent } from "@ag-ui/core";
import { AguiClient } from "./agui-client.js";
import type { RunAgentInput } from "./types.js";

const input: RunAgentInput = {
  threadId: "thread", runId: "run", messages: [
    { id: "old", role: "user", content: "old" },
    { id: "answer", role: "assistant", content: "old answer" },
    { id: "new", role: "user", content: "中文输入" },
  ], state: { injected: true }, context: [{ description: "ignored", value: "ignored" }],
};
const started = { type: "RUN_STARTED", threadId: "thread", runId: "run" };
const finished = { type: "RUN_FINISHED", threadId: "thread", runId: "run" };

function stream(events: object[], byteByByte = false) {
  const bytes = new TextEncoder().encode(events.map((event) => `data: ${JSON.stringify(event)}\r\n\r\n`).join(""));
  return new Response(new ReadableStream({
    start(controller) {
      if (byteByByte) for (const byte of bytes) controller.enqueue(Uint8Array.of(byte));
      else controller.enqueue(bytes);
      controller.close();
    },
  }), { headers: { "content-type": "text/event-stream" } });
}

describe("V2 direct AG-UI transport", () => {
  it("sends only the newest user message and handles split UTF-8 / CRLF", async () => {
    let payload: unknown;
    const client = new AguiClient("http://127.0.0.1:8790/dataagent/stream", async (_url, init) => {
      payload = JSON.parse(String(init?.body));
      return stream([started,
        { type: "TEXT_MESSAGE_START", messageId: "a", role: "assistant" },
        { type: "TEXT_MESSAGE_CONTENT", messageId: "a", delta: "你好" },
        { type: "TEXT_MESSAGE_END", messageId: "a" }, finished], true);
    });
    const events = [];
    for await (const event of client.runAgent(input)) events.push(event);
    assert.deepEqual(payload, { threadId: "thread", runId: "run", messages: [input.messages[2]], tools: [], context: [], state: {}, forwardedProps: {} });
    assert.equal(events[2]?.type, "TEXT_MESSAGE_CONTENT");
    assert.equal(events.at(-1)?.type, "RUN_FINISHED");
  });

  it("does not retry incomplete streams and preserves yielded text", async () => {
    let requests = 0;
    const client = new AguiClient("http://127.0.0.1:8790/dataagent/stream", async () => {
      requests++;
      return stream([started, { type: "TEXT_MESSAGE_CONTENT", messageId: "a", delta: "partial" }]);
    });
    const seen = [];
    await assert.rejects(async () => {
      for await (const event of client.runAgent(input)) seen.push(event);
    }, /INCOMPLETE_STREAM/);
    assert.equal(requests, 1);
    assert.equal(seen.length, 2);
  });

  it("delivers a real RUN_ERROR, without adding RUN_FINISHED", async () => {
    const client = new AguiClient("http://127.0.0.1:8790/dataagent/stream", async () => stream([
      started, { type: "RUN_ERROR", code: "ProviderError", message: "Model unavailable" },
    ]));
    const events = [];
    for await (const event of client.runAgent(input)) events.push(event);
    assert.deepEqual(events.map((event) => event.type), ["RUN_STARTED", "RUN_ERROR"]);
  });

  it("rejects duplicate terminal events before reporting success", async () => {
    const client = new AguiClient("http://127.0.0.1:8790/dataagent/stream", async () => stream([started, finished, finished]));
    const events: BaseEvent[] = [];
    await assert.rejects(async () => {
      for await (const event of client.runAgent(input)) events.push(event);
    }, /after the terminal/);
    assert.deepEqual(events.map((event) => event.type), ["RUN_STARTED"]);
  });
});

it('retains identical Chinese deltas through byte-by-byte UTF-8 transport', async () => {
  let requests = 0;
  const client = new AguiClient('http://127.0.0.1/stream', async () => {
    requests++;
    return stream([started,
      { type: 'TEXT_MESSAGE_CONTENT', messageId: 'a', delta: '哈哈' },
      { type: 'TEXT_MESSAGE_CONTENT', messageId: 'a', delta: '哈哈' }, finished], true);
  });
  const events = [];
  for await (const event of client.runAgent(input)) events.push(event);
  assert.equal(events.filter((event) => event.type === 'TEXT_MESSAGE_CONTENT').map((event) => event.delta).join(''), '哈哈哈哈');
  assert.equal(requests, 1);
});

for (const [name, data, code] of [
  ['invalid JSON', 'data: {bad}\n\n', 'STREAM_ERROR'],
  ['partial trailing frame', `data: ${JSON.stringify(finished)}\n\ndata: {`, 'INCOMPLETE_STREAM'],
  ['event after terminal', `data: ${JSON.stringify(finished)}\n\ndata: ${JSON.stringify(started)}\n\n`, 'STREAM_ERROR'],
] as const) it(`rejects ${name} without yielding success`, async () => {
  let attempts = 0;
  const client = new AguiClient('http://127.0.0.1/stream', async () => {
    attempts++;
    return new Response(data, { headers: { 'content-type': 'text/event-stream' } });
  });
  const events = [];
  await assert.rejects(async () => { for await (const event of client.runAgent(input)) events.push(event); }, { code });
  assert.equal(events.length, 0);
  assert.equal(attempts, 1);
});

for (const status of [401, 403, 409, 422]) it(`preserves HTTP ${status} without retry`, async () => {
  let attempts = 0;
  const client = new AguiClient('http://127.0.0.1/stream', async () => {
    attempts++;
    return Response.json({ detail: 'Rejected run' }, { status });
  });
  await assert.rejects(async () => { for await (const _event of client.runAgent(input)) {} }, {
    code: 'HTTP_ERROR', statusCode: status, message: 'Rejected run',
  });
  assert.equal(attempts, 1);
});

for (const dispose of [false, true]) it(`cleans reader and timer on ${dispose ? 'dispose' : 'timeout'}`, async () => {
  let signal: AbortSignal | undefined;
  let body: ReadableStream<Uint8Array> | undefined;
  let readStarted!: () => void;
  const reading = new Promise<void>((resolve) => { readStarted = resolve; });
  const client = new AguiClient('http://127.0.0.1/stream', async (_url, options) => {
    signal = options?.signal ?? undefined;
    body = new ReadableStream({ start(controller) {
      signal?.addEventListener('abort', () => controller.error(new DOMException('Aborted', 'AbortError')));
      readStarted();
    } });
    return new Response(body, { headers: { 'content-type': 'text/event-stream' } });
  }, dispose ? 5000 : 20);
  const consuming = (async () => { for await (const _event of client.runAgent(input)) {} })();
  await reading;
  if (dispose) client.dispose();
  await assert.rejects(consuming, { code: dispose ? 'CANCELLED' : 'TIMEOUT' });
  assert.equal(signal?.aborted, true);
  assert.equal(body?.locked, false);
});

it('cancels and releases the reader when its consumer stops early', async () => {
  let cancelled = false;
  const body = new ReadableStream<Uint8Array>({
    start(controller) { controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(started)}\n\n`)); },
    cancel() { cancelled = true; },
  });
  const client = new AguiClient('http://127.0.0.1/stream', async () => new Response(body, {
    headers: { 'content-type': 'text/event-stream' },
  }));
  for await (const _event of client.runAgent(input)) break;
  assert.equal(cancelled, true);
  assert.equal(body.locked, false);
});
