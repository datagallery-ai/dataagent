import assert from 'node:assert/strict';
import { it } from 'node:test';
import { CopilotKitClient } from './copilotkit-client.js';
import { AssistantTextStreamBuffer } from '../ui/assistant-stream-buffer.js';

const input = { threadId: 'thread', runId: 'run', messages: [], tools: [], context: [], state: {}, forwardedProps: {} };
const frame = (event: unknown) => `data: ${JSON.stringify(event)}\r\n\r\n`;

it('parses fragmented UTF-8 SSE and preserves repeated text deltas', async () => {
  const events = [
    { type: 'RUN_STARTED', threadId: 'thread', runId: 'run' },
    { type: 'TEXT_MESSAGE_CONTENT', messageId: 'm', delta: '哈哈' },
    { type: 'TEXT_MESSAGE_CONTENT', messageId: 'm', delta: '哈哈' },
    { type: 'RUN_FINISHED', threadId: 'thread', runId: 'run' },
  ];
  const bytes = new TextEncoder().encode(events.map(frame).join(''));
  const client = new CopilotKitClient({ runtimeUrl: 'http://localhost/api/copilotkit', agent: 'dataFoundry',
    fetchImpl: async () => new Response(new ReadableStream({ start(controller) {
      for (const byte of bytes) controller.enqueue(new Uint8Array([byte]));
      controller.close();
    } }), { headers: { 'content-type': 'text/event-stream' } }),
  });
  const received = [];
  for await (const event of client.runAgent(input)) received.push(event);
  assert.deepEqual(received, events);
  const buffer = new AssistantTextStreamBuffer();
  buffer.append('哈哈'); buffer.append('哈哈');
  assert.deepEqual(buffer.flush(false), { type: 'text', content: '哈哈哈哈', isStreaming: false });
});

for (const [name, body, message] of [
  ['EOF', frame({ type: 'RUN_STARTED' }), /without a terminal event/],
  ['invalid JSON', 'data: {broken\n\n', /Invalid JSON/],
  ['interrupt', frame({ type: 'CUSTOM', name: 'on_interrupt', value: '{}' }) + frame({ type: 'RUN_FINISHED' }), /cannot resume/],
] as const) {
  it(`does not report success or replay after ${name}`, async () => {
    let calls = 0;
    const client = new CopilotKitClient({ runtimeUrl: 'http://localhost/api/copilotkit', agent: 'dataFoundry', maxRetries: 3,
      fetchImpl: async () => { calls++; return new Response(body, { headers: { 'content-type': 'text/event-stream' } }); },
    });
    await assert.rejects(async () => { for await (const event of client.runAgent(input)) assert.notEqual(event.type, 'RUN_FINISHED'); }, message);
    assert.equal(calls, 1);
  });
}

it('does not replay a failed POST that may already have executed tools', async () => {
  let calls = 0;
  const client = new CopilotKitClient({ runtimeUrl: 'http://localhost/api/copilotkit', agent: 'dataFoundry',
    fetchImpl: async () => { calls++; throw new TypeError('network failure'); },
  });
  await assert.rejects(async () => { for await (const _ of client.runAgent(input)) { /* drain */ } });
  assert.equal(calls, 1);
});

it('disposes an in-flight stream when TUI exits', async () => {
  let signal: AbortSignal | null | undefined;
  const client = new CopilotKitClient({ runtimeUrl: 'http://localhost/api/copilotkit', agent: 'dataFoundry',
    fetchImpl: async (_url, init) => {
      signal = init?.signal;
      return new Response(new ReadableStream({ start(controller) {
        controller.enqueue(new TextEncoder().encode(frame({ type: 'RUN_STARTED' })));
        signal?.addEventListener('abort', () => controller.error(new Error('aborted')), { once: true });
      } }), { headers: { 'content-type': 'text/event-stream' } });
    },
  });
  const events = client.runAgent(input);
  await events.next();
  const pending = events.next();
  client.dispose();
  await assert.rejects(pending, /aborted/);
  assert.equal(signal?.aborted, true);
});
