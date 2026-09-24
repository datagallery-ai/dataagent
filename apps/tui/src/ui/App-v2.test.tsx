import React from 'react';
import assert from 'node:assert/strict';
import { PassThrough } from 'node:stream';
import { afterEach, it } from 'node:test';
import { stripVTControlCharacters } from 'node:util';
import { render } from 'ink';
import type { BaseEvent } from '@ag-ui/core';
import { App } from './App.js';
import { store } from '../state/store.js';
import { getMessageTextContent } from '../state/message-history.js';
import { V2SessionClient } from '../protocol/v2-session-client.js';
import type { AgentClient, RunAgentInput } from '../protocol/types.js';

const views: Array<ReturnType<typeof render>> = [];
const tick = () => new Promise<void>((resolve) => setImmediate(resolve));

function view(client: AgentClient, columns = 80, rows = 24, sessions?: V2SessionClient) {
  store.setWorkspaceConfig({ llm: [], db: [], skill: [], kb: [], mcp: [] });
  store.startNewSession('v2-ui-test');
  store.setConnectionStatus('connected');
  const stdin = Object.assign(new PassThrough(), {
    isTTY: true, isRaw: false,
    setRawMode(mode: boolean) { this.isRaw = mode; },
    ref() { return this; }, unref() { return this; },
  });
  const stdout = Object.assign(new PassThrough(), { isTTY: true, columns, rows });
  const stderr = Object.assign(new PassThrough(), { isTTY: true, columns, rows });
  const frames: string[] = [];
  stdout.on('data', (chunk) => frames.push(String(chunk)));
  const result = render(<App client={client} datasourceId={undefined} v2={{
    model: 'GLM-5.2', home: '/tmp/v2-home',
    sessions: sessions ?? new V2SessionClient('http://127.0.0.1:8790', async () => { throw new Error('Unexpected session call'); }),
  }} />, {
    stdin: stdin as unknown as NodeJS.ReadStream,
    stdout: stdout as unknown as NodeJS.WriteStream,
    stderr: stderr as unknown as NodeJS.WriteStream,
    debug: true, interactive: true, exitOnCtrlC: false, patchConsole: false,
  });
  views.push(result);
  return { ...result, stdin, stdout, frames };
}

async function until(predicate: () => boolean) {
  const deadline = Date.now() + 3000;
  while (!predicate() && Date.now() < deadline) await new Promise((resolve) => setTimeout(resolve, 10));
  assert.ok(predicate(), 'Expected UI state before timeout');
}

afterEach(() => {
  for (const current of views.splice(0)) { current.unmount(); current.cleanup(); }
});

it('Ink V2 submits Chinese text, renders a streamed answer once and shows total duration', async () => {
  const requests: RunAgentInput[] = [];
  const client: AgentClient = {
    async *runAgent(input) {
      requests.push(input);
      const events = [
        { type: 'RUN_STARTED', threadId: input.threadId, runId: input.runId },
        { type: 'TEXT_MESSAGE_CONTENT', messageId: 'a', delta: '平均值是 2。' },
        { type: 'TEXT_MESSAGE_END', messageId: 'a' },
        { type: 'MESSAGES_SNAPSHOT', messages: [{ id: 'a', role: 'assistant', content: '平均值是 2。' }] },
        { type: 'RUN_FINISHED', threadId: input.threadId, runId: input.runId, metadata: { durationMs: 1250 } },
      ];
      for (const event of events) yield event as BaseEvent;
    },
  };
  const current = view(client);
  await tick();
  current.stdin.write('请统计这些数字');
  await tick();
  current.stdin.write('\r');
  await until(() => store.getState().runStatus === 'completed');
  assert.equal(requests.length, 1);
  assert.equal(requests[0]?.messages.length, 1);
  assert.equal(requests[0]?.messages[0]?.content, '请统计这些数字');
  assert.deepEqual(requests[0]?.state, {});
  const text = store.getState().messages.map(getMessageTextContent).join('\n');
  assert.equal(text.match(/平均值是 2。/g)?.length, 1);
  assert.doesNotMatch(text, /Completed in/);
  assert.equal(store.getState().messages.at(-1)?.runSummary?.status, 'completed');
  assert.ok(store.getState().messages.at(-1)!.runSummary!.durationMs < 1250);
  await until(() => current.frames.join('').includes('Run completed'));
  assert.doesNotMatch(current.frames.join(''), /Choose a datasource to get started/);
  assert.doesNotMatch(current.frames.join(''), /no datasource/);
});

it('Ink V2 preserves partial output on stream failure and never retries', async () => {
  let attempts = 0;
  const current = view({
    async *runAgent() {
      attempts++;
      yield { type: 'TEXT_MESSAGE_CONTENT', messageId: 'a', delta: 'Partial answer' } as BaseEvent;
      throw new Error('INCOMPLETE_STREAM: connection ended');
    },
  }, 60, 24);
  await tick();
  current.stdin.write('Hello');
  await tick();
  current.stdin.write('\r');
  await until(() => store.getState().runStatus === 'failed');
  const text = store.getState().messages.map(getMessageTextContent).join('\n');
  assert.match(text, /Partial answer/);
  assert.match(text, /INCOMPLETE_STREAM/);
  assert.doesNotMatch(text, /Completed in|Retrying/);
  assert.equal(attempts, 1);
  current.stdout.columns = 80;
  current.stdout.emit('resize');
  await tick();
  assert.equal(store.getState().runStatus, 'failed');
});

it('Ink V2 uses a final snapshot when the provider emits no text deltas', async () => {
  const current = view({
    async *runAgent(input) {
      yield { type: 'MESSAGES_SNAPSHOT', messages: [...input.messages, { id: 'a', role: 'assistant', content: 'Snapshot answer' }] } as BaseEvent;
      yield { type: 'RUN_FINISHED', threadId: input.threadId, runId: input.runId } as BaseEvent;
    },
  });
  await tick();
  current.stdin.write('Hello');
  await tick();
  current.stdin.write('\r');
  await until(() => store.getState().runStatus === 'completed');
  assert.match(store.getState().messages.map(getMessageTextContent).join('\n'), /Snapshot answer/);
});

it('Ink V2 resumes native saved messages, rejects unsupported commands, and clears to a new thread', async () => {
  const requests: string[] = [];
  const sessions = new V2SessionClient('http://127.0.0.1:8790', async (url) => {
    requests.push(String(url));
    if (String(url).endsWith('/outputs')) {
      assert.equal(String(url), 'http://127.0.0.1:8790/sessions/saved-thread/outputs');
      return Response.json({ files: [{ path: 'restored.csv', size: 10, modifiedAt: '2026-09-23T00:00:00Z' }] });
    }
    return Response.json({
      threadId: 'saved-thread', title: 'Saved statistics', createdAt: '', updatedAt: '',
      status: 'interrupted', hasCheckpoint: true, newThreadRequired: false,
      notice: 'Restored the last successful checkpoint; no work was replayed.',
      messages: [
        { id: 'saved-user', role: 'user', content: 'Previous question' },
        { id: 'saved-answer', role: 'assistant', content: 'Previous answer' },
      ],
    });
  });
  const current = view({ async *runAgent() { throw new Error('Commands must not invoke the model'); } }, 80, 24, sessions);
  const submit = async (text: string) => {
    current.stdin.write(text);
    await tick();
    current.stdin.write('\r');
    await tick();
  };
  await tick();
  await submit('/resume saved-thread');
  await until(() => store.getState().threadId === 'saved-thread');
  assert.equal(store.getState().messages.length, 2);
  assert.match(getMessageTextContent(store.getState().messages.at(-1)!), /Previous answer/);
  assert.deepEqual(requests, ['http://127.0.0.1:8790/sessions/saved-thread']);
  await until(() => current.frames.join('').includes('no work was replayed'));
  await submit('/outputs');
  await until(() => latestOutputFrame(current).includes('restored.csv'));
  assert.equal(store.getState().artifacts.length, 0);
  current.stdin.write('\x1b');
  await until(() => latestOutputFrame(current).includes('GLM-5.2'));
  await submit('/model');
  await until(() => current.frames.join('').includes('not supported in V2'));
  await submit('/clear');
  await until(() => store.getState().threadId !== 'saved-thread');
  assert.equal(store.getState().messages.length, 0);
});

it('Ink V2 accepts Chinese multiline input and Shift+Enter does not submit', async () => {
  const requests: RunAgentInput[] = [];
  const current = view({ async *runAgent(input) {
    requests.push(input);
    yield { type: 'RUN_FINISHED', threadId: input.threadId, runId: input.runId } as BaseEvent;
  } });
  await tick();
  current.stdin.write('你好');
  await tick();
  current.stdin.write('\x1b[13;2u');
  await tick();
  assert.equal(requests.length, 0);
  current.stdin.write('世界');
  await tick();
  current.stdin.write('\r');
  await until(() => store.getState().runStatus === 'completed');
  assert.equal(requests[0]?.messages[0]?.content, '你好\n世界');
});

it('Ink V2 reconciles failed tool status from the authoritative final snapshot', async () => {
  const current = view({ async *runAgent(input) {
    yield { type: 'TOOL_CALL_START', toolCallId: 'bad-call', toolCallName: 'common__summarize_numbers' } as BaseEvent;
    yield { type: 'TOOL_CALL_RESULT', toolCallId: 'bad-call', messageId: 't', content: 'Invalid input' } as BaseEvent;
    yield { type: 'MESSAGES_SNAPSHOT', messages: [
      ...input.messages,
      { id: 'a', role: 'assistant', toolCalls: [{ id: 'bad-call', type: 'function', function: { name: 'common__summarize_numbers', arguments: '{}' } }] },
      { id: 't', role: 'tool', toolCallId: 'bad-call', content: 'Invalid input', error: 'error' },
      { id: 'answer', role: 'assistant', content: 'Please provide a list of numbers.' },
    ] } as BaseEvent;
    yield { type: 'RUN_FINISHED', threadId: input.threadId, runId: input.runId } as BaseEvent;
  } });
  await tick();
  current.stdin.write('Test error');
  await tick();
  current.stdin.write('\r');
  await until(() => store.getState().runStatus === 'completed');
  assert.equal(store.getState().toolCalls[0]?.status, 'failed');
  assert.match(store.getState().messages.map(getMessageTextContent).join('\n'), /Please provide a list/);
});

async function submit(current: ReturnType<typeof view>, text: string) {
  current.stdin.write(text);
  await tick();
  current.stdin.write('\r');
  await tick();
}
const transcript = () => store.getState().messages.map(getMessageTextContent).join('\n');
const summaries = () => store.getState().messages.filter((message) => message.runSummary);

it('V2 keeps repeated deltas and interleaved tools once across repeated snapshots', async () => {
  const current = view({ async *runAgent(input) {
    yield { type: 'TEXT_MESSAGE_CONTENT', messageId: 'a', delta: '哈哈' } as BaseEvent;
    yield { type: 'TEXT_MESSAGE_CONTENT', messageId: 'a', delta: '哈哈' } as BaseEvent;
    yield { type: 'TOOL_CALL_START', toolCallId: 't', toolCallName: 'read_file' } as BaseEvent;
    yield { type: 'TOOL_CALL_RESULT', toolCallId: 't', messageId: 'tool', content: 'ok' } as BaseEvent;
    yield { type: 'TEXT_MESSAGE_CONTENT', messageId: 'b', delta: 'Done' } as BaseEvent;
    const snapshot = { type: 'MESSAGES_SNAPSHOT', messages: [...input.messages,
      { id: 'a', role: 'assistant', content: '哈哈哈哈' },
      { id: 'b', role: 'assistant', content: 'Done' },
      { id: 'c', role: 'assistant', content: 'Extra' },
      { id: 'd', role: 'assistant', content: 'Extra' },
    ] } as BaseEvent;
    yield snapshot;
    yield snapshot;
    yield { type: 'RUN_FINISHED', threadId: input.threadId, runId: input.runId } as BaseEvent;
  } });
  await tick();
  await submit(current, 'Test');
  await until(() => summaries().length === 1);
  assert.equal(transcript().match(/哈哈/g)?.length, 2);
  assert.equal(transcript().match(/Done/g)?.length, 1);
  assert.equal(transcript().match(/Extra/g)?.length, 2, 'different message IDs may have identical text');
  assert.equal(store.getState().toolCalls[0]?.id, 't');
  assert.ok(transcript().indexOf('Done') < transcript().indexOf('Extra'));
});

it('second-round snapshot fallback uses the submitted user ID and preserves every new assistant ID', async () => {
  let previous: unknown[] = [];
  let count = 0;
  const current = view({ async *runAgent(input) {
    count++;
    const answers = count === 1 ? [{ id: 'old-a', role: 'assistant', content: 'Old answer' }]
      : [{ id: 'new-a', role: 'assistant', content: 'New one' }, { id: 'new-b', role: 'assistant', content: 'New two' }];
    const messages = [...previous, ...input.messages, ...answers];
    yield { type: 'MESSAGES_SNAPSHOT', messages } as BaseEvent;
    yield { type: 'RUN_FINISHED', threadId: input.threadId, runId: input.runId } as BaseEvent;
    previous = messages;
  } });
  await tick();
  await submit(current, 'First');
  await until(() => summaries().length === 1);
  await submit(current, 'Second');
  await until(() => summaries().length === 2);
  for (const answer of ['Old answer', 'New one', 'New two']) assert.equal(transcript().split(answer).length, 2);
});

it('a missing snapshot user boundary cannot reuse a historical answer', async () => {
  const current = view({ async *runAgent(input) {
    yield { type: 'MESSAGES_SNAPSHOT', messages: [{ id: 'old', role: 'assistant', content: 'Stale answer' }] } as BaseEvent;
    yield { type: 'RUN_FINISHED', threadId: input.threadId, runId: input.runId } as BaseEvent;
  } });
  await tick();
  await submit(current, 'New question');
  await until(() => summaries().length === 1);
  assert.doesNotMatch(transcript(), /Stale answer/);
});

it('dispatch time survives delayed duplicate starts and early completion snapshots until a real terminal', async () => {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  const current = view({ async *runAgent(input) {
    yield { type: 'TOOL_CALL_START', toolCallId: 't', toolCallName: 'read_file' } as BaseEvent;
    await new Promise((resolve) => setTimeout(resolve, 50));
    yield { type: 'RUN_STARTED', runId: input.runId, threadId: input.threadId } as BaseEvent;
    yield { type: 'RUN_STARTED', runId: input.runId, threadId: input.threadId } as BaseEvent;
    yield { type: 'TEXT_MESSAGE_CONTENT', messageId: 'a', delta: 'Answer' } as BaseEvent;
    yield { type: 'TEXT_MESSAGE_END', messageId: 'a' } as BaseEvent;
    yield { type: 'STATE_SNAPSHOT', snapshot: { runStatus: 'completed' } } as BaseEvent;
    await gate;
    yield { type: 'RUN_FINISHED', runId: input.runId, threadId: input.threadId } as BaseEvent;
  } });
  await tick();
  await submit(current, 'Wait');
  const dispatchTime = store.getState().runStartedAt;
  try {
    await until(() => transcript().includes('Answer'));
    await new Promise((resolve) => setTimeout(resolve, 3000));
    assert.equal(store.getState().runStatus, 'running');
    assert.equal(store.getState().runStartedAt, dispatchTime);
    assert.equal(store.getState().toolCalls[0]?.id, 't');
    assert.equal(summaries().length, 0);
    assert.match(current.frames.join(''), /Running/);
    assert.doesNotMatch(current.frames.join(''), /Run completed|working\.\.\./);
  } finally { release(); }
  await until(() => summaries().length === 1);
  assert.ok(summaries()[0]!.runSummary!.durationMs >= 3000);
});

for (const partial of [false, true]) it(`RUN_ERROR preserves exact code/message (partial=${partial})`, async () => {
  const current = view({ async *runAgent() {
    if (partial) yield { type: 'TEXT_MESSAGE_CONTENT', messageId: 'a', delta: 'Partial text' } as BaseEvent;
    yield { type: 'RUN_ERROR', code: 'MODEL_QUOTA', message: 'Provider connection quota reached' } as BaseEvent;
  } });
  await tick();
  await submit(current, 'Test');
  await until(() => summaries().length === 1);
  assert.match(transcript(), /Error \[MODEL_QUOTA\]: Provider connection quota reached/);
  if (partial) assert.match(transcript(), /Partial text/);
  assert.equal(summaries()[0]?.runSummary?.status, 'failed');
});

it('queued rounds retain separate summaries and ignore stale root events', async () => {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  const inputs: RunAgentInput[] = [];
  const current = view({ async *runAgent(input) {
    inputs.push(input);
    if (inputs.length === 1) await gate;
    else yield { type: 'RUN_FINISHED', threadId: input.threadId, runId: inputs[0]!.runId } as BaseEvent;
    yield { type: 'TEXT_MESSAGE_CONTENT', messageId: input.runId, delta: `Answer ${inputs.length}` } as BaseEvent;
    yield { type: 'RUN_FINISHED', threadId: input.threadId, runId: input.runId } as BaseEvent;
  } });
  await tick();
  await submit(current, 'First');
  await submit(current, 'Second');
  assert.equal(inputs.length, 1);
  release();
  await until(() => summaries().length === 2);
  assert.equal(inputs.length, 2);
  assert.notEqual(summaries()[0]?.id, summaries()[1]?.id);
  assert.match(transcript(), /Answer 1[\s\S]*Answer 2/);
  assert.deepEqual(inputs.map((input) => input.messages.length), [1, 1]);
  const last = summaries()[1]!;
  store.addRunSummary(inputs[1]!.threadId, inputs[1]!.runId, 'completed', 1);
  assert.equal(summaries().length, 2);
  store.startNewSession('new-thread');
  store.addRunSummary(inputs[1]!.threadId, inputs[1]!.runId, 'completed', 1);
  assert.equal(summaries().length, 0);
  assert.ok(last.runSummary);
});

for (const [columns, rows] of [[60, 24], [80, 24], [160, 40]] as const) it(`home/chat composer stays at the floor at ${columns}x${rows}`, async () => {
  const { stripVTControlCharacters } = await import('node:util');
  const { textWidth } = await import('./text-width.js');
  const current = view({ async *runAgent(input) {
    yield { type: 'TEXT_MESSAGE_CONTENT', messageId: 'answer', delta: 'A short answer.' } as BaseEvent;
    yield { type: 'RUN_FINISHED', runId: input.runId, threadId: input.threadId } as BaseEvent;
  } }, columns, rows);
  const frame = () => stripVTControlCharacters(current.frames.filter((frame) => frame.includes('GLM-5.2')).at(-1) ?? '');
  const checkFrame = (width: number, height: number) => {
    const lines = frame().trimEnd().split('\n');
    assert.equal(lines.length, height);
    assert.match(lines.at(-1)!, /GLM-5.2/);
    assert.ok(lines.every((line) => textWidth(line) <= width), 'no horizontal overflow');
  };
  await tick(); await tick();
  checkFrame(columns, rows);
  assert.match(frame(), /Try statistics|Summarize the numbers/);
  current.stdin.write('\x1b[200~one\ntwo\nthree\nfour\nfive\nsix\x1b[201~');
  await tick(); await tick();
  checkFrame(columns, rows);
  assert.match(frame(), /Try statistics|Summarize the numbers/);
  current.stdin.write('\r');
  await until(() => summaries().length === 1);
  await tick();
  checkFrame(columns, rows);
  const composerColumns = columns > 120 ? columns - 42 : columns;
  const promptLine = frame().split('\n').find((line) => line.includes('› Ask a question'));
  assert.ok(promptLine?.startsWith('   › '), 'composer prompt shares the transcript inset');
  assert.ok(promptLine && promptLine.slice(0, composerColumns).includes('Ask a question'), 'composer fits the chat pane');
  current.stdin.write('\x1b[200~one\ntwo\nthree\nfour\nfive\nsix\x1b[201~');
  await tick(); await tick();
  checkFrame(columns, rows);
  current.stdout.columns = 60;
  current.stdout.rows = 24;
  current.stdout.emit('resize');
  await tick(); await tick();
  checkFrame(60, 24);
});

it('V2 renders streamed tool targets and Ctrl+O reveals the hidden output without another request', async () => {
  const { stripVTControlCharacters } = await import('node:util');
  let requests = 0;
  const current = view({ async *runAgent(input) {
    requests++;
    yield { type: 'RUN_STARTED', threadId: input.threadId, runId: input.runId } as BaseEvent;
    yield { type: 'TOOL_CALL_START', toolCallId: 'read', toolCallName: 'read_file' } as BaseEvent;
    for (const delta of ['{"file_path":', '"/tmp/数据.md"}']) {
      yield { type: 'TOOL_CALL_ARGS', toolCallId: 'read', delta } as BaseEvent;
    }
    yield { type: 'TOOL_CALL_END', toolCallId: 'read' } as BaseEvent;
    yield { type: 'TOOL_CALL_RESULT', toolCallId: 'read', messageId: 'result',
      content: Array.from({ length: 8 }, (_, i) => `Output line ${i + 1}`).join('\n') } as BaseEvent;
    yield { type: 'RUN_FINISHED', threadId: input.threadId, runId: input.runId } as BaseEvent;
  } });
  const frame = () => stripVTControlCharacters(current.frames.filter((value) => value.includes('GLM-5.2')).at(-1) ?? '');
  await tick();
  current.stdin.write('Read the file');
  await tick();
  current.stdin.write('\r');
  await until(() => frame().includes('Run completed'));
  assert.match(frame(), /Read \/tmp\/数据.md/);
  assert.match(frame(), /Ctrl\+O details/);
  assert.doesNotMatch(frame(), /Output line 8/);
  current.stdin.write('\x0f');
  await until(() => frame().includes('Output line 8'));
  current.stdin.write('\x0f');
  await until(() => !frame().includes('Output line 8'));
  assert.equal(requests, 1);
});

it('V2 slash menu, completion and help expose only supported commands including outputs', async () => {
  const current = view({ async *runAgent() { throw new Error('Must not call the model'); } });
  await tick();
  current.stdin.write('/');
  await tick(); await tick();
  const menu = current.frames.at(-1)!;
  for (const command of ['help', 'clear', 'resume', 'outputs', 'exit']) assert.match(menu, new RegExp(command));
  assert.doesNotMatch(menu, /\/model|\/datasource|\/skill|\/login/);
  current.stdin.write('mo');
  await tick();
  current.stdin.write('\t');
  await tick();
  assert.doesNotMatch(current.frames.at(-1)!, /\/model/);
});

function latestOutputFrame(current: ReturnType<typeof view>): string {
  return current.frames.map(stripVTControlCharacters).filter((frame) =>
    frame.includes('GLM-5.2') || frame.includes('Outputs (') || frame.includes('Outputs /')).at(-1) ?? '';
}

function outputClient(requests: RunAgentInput[]): AgentClient {
  return { async *runAgent(input) {
    requests.push(input);
    yield { type: 'TEXT_MESSAGE_CONTENT', messageId: 'answer', delta: 'Report ready.' } as BaseEvent;
    yield { type: 'RUN_FINISHED', threadId: input.threadId, runId: input.runId } as BaseEvent;
  } };
}

for (const columns of [60, 80, 160]) it(`/outputs opens and closes the full view without a sidebar at ${columns} columns`, async () => {
  const requests: RunAgentInput[] = [];
  const calls: string[] = [];
  const sessions = new V2SessionClient('http://127.0.0.1:8790', async (url) => {
    calls.push(String(url));
    if (String(url).includes('/outputs/preview?')) {
      return Response.json({ type: 'file', path: '销售报告.md', content: '# Sales\n\nTotal: 42' });
    }
    assert.equal(String(url), 'http://127.0.0.1:8790/sessions/v2-ui-test/outputs');
    return Response.json({ files: [{ path: '销售报告.md', size: 20, modifiedAt: '2026-09-23T00:00:00Z' }] });
  });
  const current = view(outputClient(requests), columns, 24, sessions);
  const frame = () => latestOutputFrame(current);
  await tick();
  assert.doesNotMatch(frame(), /Outputs 0|Ctrl\+G/);
  await submit(current, 'Generate a report');
  await until(() => frame().includes('Run completed'));
  assert.equal(store.getState().artifacts.length, 0); // Native writes have no artifact event.
  assert.doesNotMatch(frame(), /Outputs 1|Ctrl\+G/);
  await submit(current, '/outputs');
  await until(() => frame().includes('Outputs (1)'));
  assert.doesNotMatch(frame(), /GLM-5.2/);
  current.stdin.write('\r');
  await until(() => frame().includes('Total: 42'));
  current.stdin.write('\x1b');
  await until(() => frame().includes('Outputs (1)'));
  current.stdin.write('\x1b');
  await until(() => frame().includes('GLM-5.2'));
  assert.match(frame(), /Report ready/);
  assert.doesNotMatch(frame(), /Outputs 1|Ctrl\+G/);
  assert.equal(requests.length, 1);
  assert.equal(calls.length, 2);
  await submit(current, '/outputs');
  await until(() => frame().includes('Outputs (1)'));
  assert.equal(calls.length, 3); // Reopening refreshes files instead of caching the list.
});

it('V2 /outputs opens the empty view and q returns to the home composer', async () => {
  const sessions = new V2SessionClient('http://127.0.0.1:8790', async () => Response.json({ files: [] }));
  const current = view({ async *runAgent() { throw new Error('Must not call the model'); } }, 160, 24, sessions);
  const frame = () => latestOutputFrame(current);
  await tick();
  await submit(current, '/outputs');
  await until(() => frame().includes('Outputs (0)'));
  current.stdin.write('q');
  await until(() => frame().includes('GLM-5.2'));
  assert.doesNotMatch(frame(), /Outputs \(0\)|Ctrl\+G/);
});

it('V2 /outputs reports backend failures instead of displaying an empty list', async () => {
  const sessions = new V2SessionClient('http://127.0.0.1:8790', async () =>
    Response.json({ detail: 'Unable to list session outputs' }, { status: 500 }));
  const current = view({ async *runAgent() { throw new Error('Must not call the model'); } }, 160, 24, sessions);
  await tick();
  await submit(current, '/outputs');
  await until(() => latestOutputFrame(current).includes('Unable to list session outputs'));
  assert.doesNotMatch(latestOutputFrame(current), /Outputs \(0\)/);
});

it('V2 discards output responses from a session that has been cleared', async () => {
  let finish!: (response: Response) => void;
  const response = new Promise<Response>((resolve) => { finish = resolve; });
  const sessions = new V2SessionClient('http://127.0.0.1:8790', async () => response);
  const current = view({ async *runAgent() { throw new Error('Must not call the model'); } }, 160, 24, sessions);
  await tick();
  await submit(current, '/outputs');
  await until(() => latestOutputFrame(current).includes('Loading session outputs'));
  await submit(current, '/clear');
  await until(() => store.getState().threadId !== 'v2-ui-test');
  finish(Response.json({ files: [{ path: 'old.txt', size: 4, modifiedAt: '2026-09-23T00:00:00Z' }] }));
  await tick();
  await tick();
  assert.doesNotMatch(latestOutputFrame(current), /Outputs \(1\)|old.txt/);
});

for (const status of [401, 403, 409, 422]) it(`V2 HTTP ${status} produces one failure summary without retry or login UI`, async () => {
  const { AguiClientError } = await import('../protocol/agui-client.js');
  let attempts = 0;
  const current = view({ async *runAgent() { attempts++; throw new AguiClientError('Rejected request', 'HTTP_ERROR', status); } });
  await tick();
  await submit(current, 'Test rejection');
  await until(() => summaries().length === 1);
  assert.equal(summaries()[0]?.runSummary?.status, 'failed');
  assert.match(transcript(), /Error \[HTTP_ERROR\]: Rejected request/);
  assert.doesNotMatch(transcript(), /Retrying|register|login|Run completed/);
  assert.equal(attempts, 1);
});

it('low-height fallback remains usable at 60 columns and allows exit', async () => {
  const current = view({ async *runAgent() { throw new Error('Must not call the model'); } }, 60, 18);
  await tick();
  assert.match(current.frames.join(''), /Terminal too small/);
  assert.match(current.frames.join(''), /60x19/);
  current.stdin.write('\x03');
  await tick();
  current.stdin.write('\x03');
  await current.waitUntilExit();
});
