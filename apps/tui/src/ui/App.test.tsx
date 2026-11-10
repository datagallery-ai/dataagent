import React from 'react';
import assert from 'node:assert/strict';
import { PassThrough } from 'node:stream';
import { afterEach, it } from 'node:test';
import { stripVTControlCharacters } from 'node:util';
import { render } from 'ink';
import type { BaseEvent } from '@ag-ui/core';
import type { AgentEvent } from '../protocol/agui-events.js';
import { App } from './App.js';
import { store } from '../state/store.js';
import { getMessageTextContent } from '../state/message-history.js';
import { SessionClient } from '../protocol/session-client.js';
import type { AgentClient, RunAgentInput } from '../protocol/types.js';

const views: Array<ReturnType<typeof render>> = [];
const tick = () => new Promise<void>((resolve) => setImmediate(resolve));

function view(client: AgentClient, columns = 80, rows = 24, sessions?: SessionClient) {
  store.setWorkspaceConfig({ llm: [], db: [], skill: [], kb: [], mcp: [] });
  store.startNewSession('ui-test');
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
  const result = render(<App client={client} datasourceId={undefined} tui={{
    model: 'GLM-5.2', home: '/tmp/tui-home',
    sessions: sessions ?? new SessionClient('http://127.0.0.1:8790', async () => { throw new Error('Unexpected session call'); }),
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

it('Ink submits Chinese text, renders a streamed answer once and shows total duration', async () => {
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

it('Ink preserves partial output on stream failure and never retries', async () => {
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

it('Ink uses a final snapshot when the provider emits no text deltas', async () => {
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

it('Ink resumes native saved messages, rejects unsupported commands, and clears to a new thread', async () => {
  const requests: string[] = [];
  const sessions = new SessionClient('http://127.0.0.1:8790', async (url) => {
    requests.push(String(url));
    if (String(url).endsWith('/outputs')) {
      assert.equal(String(url), 'http://127.0.0.1:8790/sessions/saved-thread/outputs');
      return Response.json({ files: [{ path: 'restored.csv', size: 10, modifiedAt: '2026-09-23T00:00:00Z' }] });
    }
    return Response.json({
      threadId: 'saved-thread', title: 'Saved statistics', createdAt: '', updatedAt: '',
      status: 'interrupted', hasCheckpoint: true,
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
  await until(() => current.frames.join('').includes('not supported'));
  await submit('/clear');
  await until(() => store.getState().threadId !== 'saved-thread');
  assert.equal(store.getState().messages.length, 0);
});

it('Ink keeps the saved thread when the session has no checkpoint', async () => {
  const sessions = new SessionClient('http://127.0.0.1:8790', async () => Response.json({
    threadId: 'saved-thread', title: 'Unfinished', createdAt: '', updatedAt: '',
    status: 'interrupted', hasCheckpoint: false,
    notice: 'No checkpoint was saved. Continue this conversation.',
    messages: [{ id: 'kept-user', role: 'user', content: 'Kept question' }],
  }));
  const current = view({ async *runAgent() { throw new Error('Resume must not invoke the model'); } }, 80, 24, sessions);
  await tick();
  current.stdin.write('/resume saved-thread');
  await tick();
  current.stdin.write('\r');
  await until(() => store.getState().threadId === 'saved-thread');
  assert.match(getMessageTextContent(store.getState().messages[0]!), /Kept question/);
  assert.match(current.frames.join(''), /Continue this conversation/);
});

it('/stop cancels the active run and stays on this thread', async () => {
  let release: () => void = () => {};
  const hanging = new Promise<void>((resolve) => { release = resolve; });
  const cancels: string[] = [];
  const stopped: string[] = [];
  const sessions = new SessionClient('http://127.0.0.1:8790', async (url, init) => {
    cancels.push(`${init?.method ?? 'GET'} ${String(url)}`);
    if (String(url).includes('/cancel')) return new Response(null, { status: 204 });
    throw new Error(`Unexpected session call ${String(url)}`);
  });
  const current = view({
    cancelRun(runId) { stopped.push(runId); release(); },
    async *runAgent() {
      await hanging;
    },
  }, 80, 24, sessions);
  await tick();
  current.stdin.write('keep going');
  await tick();
  current.stdin.write('\r');
  await until(() => store.getState().runStatus === 'running');
  const threadId = store.getState().threadId;
  const runId = store.getState().runId;
  assert.ok(runId);
  current.stdin.write('/stop');
  await tick();
  current.stdin.write('\r');
  await until(() => store.getState().runStatus !== 'running');
  assert.deepEqual(stopped, [runId]);
  assert.equal(store.getState().threadId, threadId);
  assert.equal(cancels[0], `POST http://127.0.0.1:8790/runs/${runId}/cancel`);
  const text = store.getState().messages.map(getMessageTextContent).join('\n');
  assert.match(text, /keep going/);
  assert.doesNotMatch(text, /INCOMPLETE_STREAM/);
  assert.equal(store.getState().messages.at(-1)?.runSummary?.status, 'interrupted');
});

it('/stop leaves a queued run alone when its cancel returns after the original stream ends', async () => {
  let releaseOriginal!: () => void;
  const originalGate = new Promise<void>((resolve) => { releaseOriginal = resolve; });
  let cancelStarted!: () => void;
  const cancelSeen = new Promise<void>((resolve) => { cancelStarted = resolve; });
  let releaseCancel!: () => void;
  const cancelGate = new Promise<void>((resolve) => { releaseCancel = resolve; });
  let releaseQueued!: () => void;
  const queuedGate = new Promise<void>((resolve) => { releaseQueued = resolve; });
  const stopped: string[] = [];
  const runs: string[] = [];
  const sessions = new SessionClient('http://127.0.0.1:8790', async (url) => {
    if (String(url).includes('/cancel')) {
      cancelStarted();
      await cancelGate;
      return new Response(null, { status: 204 });
    }
    throw new Error(`Unexpected session call ${String(url)}`);
  });
  const current = view({
    cancelRun(runId) { stopped.push(runId); },
    async *runAgent(input) {
      runs.push(input.runId);
      if (runs.length === 1) {
        await originalGate;
        yield { type: 'RUN_FINISHED', threadId: input.threadId, runId: input.runId } as BaseEvent;
        return;
      }
      await queuedGate;
      yield { type: 'TEXT_MESSAGE_CONTENT', messageId: 'queued', delta: 'Queued answer' } as BaseEvent;
      yield { type: 'RUN_FINISHED', threadId: input.threadId, runId: input.runId } as BaseEvent;
    },
  }, 80, 24, sessions);
  await tick();
  await submit(current, 'first question');
  await until(() => store.getState().runStatus === 'running');
  const originalRunId = store.getState().runId;
  await submit(current, 'queued question');
  await submit(current, '/stop');
  await cancelSeen;
  releaseOriginal();
  await until(() => runs.length === 2 && store.getState().runId !== originalRunId && store.getState().runStatus === 'running');
  releaseCancel();
  await tick();
  assert.deepEqual(stopped, []);
  releaseQueued();
  await until(() => store.getState().runStatus === 'completed');
  const text = store.getState().messages.map(getMessageTextContent).join('\n');
  assert.match(text, /Queued answer/);
  assert.doesNotMatch(text, /CANCELLED|The run was cancelled/);
  assert.equal(store.getState().messages.at(-1)?.runSummary?.status, 'completed');
});

it('/resume list opens the picker and does not load a session id named list', async () => {
  const requests: string[] = [];
  const sessions = new SessionClient('http://127.0.0.1:8790', async (url) => {
    requests.push(String(url));
    const href = String(url);
    if (href.includes('/sessions?')) {
      return Response.json({ sessions: [{
        threadId: 'saved-thread', title: 'Saved statistics', createdAt: '', updatedAt: '',
        status: 'complete', hasCheckpoint: true,
      }] });
    }
    if (href.endsWith('/sessions/saved-thread')) {
      return Response.json({
        threadId: 'saved-thread', title: 'Saved statistics', createdAt: '', updatedAt: '',
        status: 'complete', hasCheckpoint: true, notice: null,
        messages: [{ id: 'saved-user', role: 'user', content: 'Previous question' }],
      });
    }
    return Response.json({ detail: 'missing' }, { status: 404 });
  });
  const current = view({ async *runAgent() { throw new Error('Commands must not invoke the model'); } }, 80, 24, sessions);
  const shown = () => current.frames.map(stripVTControlCharacters).filter((frame) =>
    frame.includes('Resume Session') || frame.includes('GLM-5.2') || frame.includes('Resume failed')).at(-1) ?? '';
  await tick();
  await submit(current, '/resume list');
  await until(() => shown().includes('Resume Session') && shown().includes('Saved statistics'));
  assert.equal(store.getState().threadId, 'ui-test');
  assert.ok(requests.some((url) => url.includes('/sessions?limit=50')));
  assert.ok(requests.every((url) => !url.includes('/sessions/list')));
  current.stdin.write('\x1b');
  await until(() => shown().includes('GLM-5.2') && !shown().includes('Resume Session'));
  requests.length = 0;
  await submit(current, '/resume');
  await until(() => shown().includes('Resume Session'));
  assert.ok(requests.some((url) => url.includes('/sessions?limit=50')));
  assert.equal(store.getState().threadId, 'ui-test');
  current.stdin.write('\x1b');
  await until(() => shown().includes('GLM-5.2') && !shown().includes('Resume Session'));
  requests.length = 0;
  await submit(current, '/resume missing-id');
  await until(() => shown().includes('Resume failed'));
  assert.ok(requests.some((url) => url.endsWith('/sessions/missing-id')));
  assert.equal(store.getState().threadId, 'ui-test');
  requests.length = 0;
  await submit(current, '/resume latest');
  await until(() => store.getState().threadId === 'saved-thread');
  assert.ok(requests.some((url) => url.includes('/sessions?limit=1')));
  assert.ok(requests.some((url) => url.endsWith('/sessions/saved-thread')));
});

it('Ink accepts Chinese multiline input and Shift+Enter does not submit', async () => {
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

it('Ink reconciles failed tool status from the authoritative final snapshot', async () => {
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

for (const error of [null, 'Query failed']) {
  it(`Ink keeps nl2sql outcome stable through result, snapshot and completion (error=${error})`, async () => {
    let releaseSnapshot!: () => void;
    let releaseFinish!: () => void;
    const snapshotGate = new Promise<void>((resolve) => { releaseSnapshot = resolve; });
    const finishGate = new Promise<void>((resolve) => { releaseFinish = resolve; });
    const content = JSON.stringify({ row_count: error ? 0 : 5, error });
    const expected = error ? 'failed' : 'success';
    const current = view({ async *runAgent(input) {
      yield { type: 'TOOL_CALL_START', toolCallId: 'nl2sql-call', toolCallName: 'task' } as BaseEvent;
      yield { type: 'TOOL_CALL_ARGS', toolCallId: 'nl2sql-call', delta: '{"subagent_type":"nl2sql"}' } as BaseEvent;
      yield { type: 'TOOL_CALL_END', toolCallId: 'nl2sql-call' } as BaseEvent;
      yield { type: 'TOOL_CALL_RESULT', toolCallId: 'nl2sql-call', messageId: 't', content } as BaseEvent;
      await snapshotGate;
      yield { type: 'MESSAGES_SNAPSHOT', messages: [
        ...input.messages,
        { id: 'a', role: 'assistant', toolCalls: [{ id: 'nl2sql-call', type: 'function', function: { name: 'task', arguments: '{"subagent_type":"nl2sql"}' } }] },
        { id: 't', role: 'tool', toolCallId: 'nl2sql-call', content },
        { id: 'answer', role: 'assistant', content: 'Query response' },
      ] } as BaseEvent;
      await finishGate;
      yield { type: 'RUN_FINISHED', threadId: input.threadId, runId: input.runId } as BaseEvent;
    } });
    const checkStatus = () => {
      assert.equal(store.getState().toolCalls[0]?.status, expected);
      const tool = store.getState().messages.flatMap((message) => message.elements)
        .find((element) => element.type === 'tool_call');
      assert.equal(tool?.toolCall?.status, expected, 'inline transcript badge');
    };
    await tick();
    try {
      await submit(current, '数据源有几个表');
      await until(() => store.getState().toolCalls[0]?.result === content);
      checkStatus();
      releaseSnapshot();
      await until(() => transcript().includes('Query response'));
      checkStatus();
      releaseFinish();
      await until(() => store.getState().runStatus === 'completed');
      checkStatus();
    } finally {
      releaseSnapshot();
      releaseFinish();
    }
  });
}

async function submit(current: ReturnType<typeof view>, text: string) {
  current.stdin.write(text);
  await tick();
  current.stdin.write('\r');
  await tick();
}
const transcript = () => store.getState().messages.map(getMessageTextContent).join('\n');
const summaries = () => store.getState().messages.filter((message) => message.runSummary);

it('shows native child progress before the task, expands steps and keeps child output out of the answer', async () => {
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  const current = view({ async *runAgent(input) {
    const events = [
      { type: 'SUBAGENT_STARTED', subagentRunId: 'child', parentToolCallId: 'delegate', name: 'nl2sql' },
      { type: 'STEP_STARTED', subagentRunId: 'child', stepName: 'perceptor' },
      { type: 'STEP_FINISHED', subagentRunId: 'child', stepName: 'perceptor' },
      { type: 'STEP_STARTED', subagentRunId: 'child', stepName: 'generator' },
      { type: 'TEXT_MESSAGE_CONTENT', subagentRunId: 'child', messageId: 'private', delta: 'child-private-answer' },
      { type: 'REASONING_MESSAGE_CONTENT', subagentRunId: 'child', messageId: 'private-reasoning', delta: 'child-private-reasoning' },
      { type: 'TOOL_CALL_START', subagentRunId: 'child', toolCallId: 'child-tool', toolCallName: 'read_file' },
    ];
    for (const event of events) yield event as AgentEvent;
    await gate;
    for (const event of [
      { type: 'SUBAGENT_FINISHED', subagentRunId: 'child' },
      { type: 'TOOL_CALL_START', toolCallId: 'delegate', toolCallName: 'task' },
      { type: 'TOOL_CALL_ARGS', toolCallId: 'delegate', delta: '{"subagent_type":"nl2sql"}' },
      { type: 'TOOL_CALL_RESULT', toolCallId: 'delegate', messageId: 'result', content: '{"error":"Query failed"}' },
      { type: 'TEXT_MESSAGE_CONTENT', messageId: 'root-answer', delta: 'Root answer' },
      { type: 'RUN_FINISHED', threadId: input.threadId, runId: input.runId },
    ]) yield event as AgentEvent;
  } }, 80, 34);
  await tick();
  try {
    await submit(current, 'How many tables?');
    await until(() => current.frames.join('').includes('generator'));
    assert.equal(store.getState().toolCalls.length, 1);
    assert.equal(store.getState().toolCalls[0]?.id, 'delegate');
    assert.equal(store.getState().runStatus, 'running');
    const before = store.getState().toolCalls[0]?.progress;
    store.handleLiveRunEvent({ type: 'SUBAGENT_ERROR', subagentRunId: 'child', message: 'stale', _clientRunId: 'old-run' });
    assert.equal(store.getState().toolCalls[0]?.progress, before, 'old stream cannot update current progress');
    current.stdin.write('draft');
    await tick();
    current.stdin.write('\x0f');
    await until(() => current.frames.join('').includes('perceptor'));
    assert.doesNotMatch(transcript(), /child-private/);
    assert.doesNotMatch(current.frames.join(''), /child-private|child-tool/);
    release();
    await until(() => store.getState().runStatus === 'completed');
    assert.equal(store.getState().toolCalls.length, 1);
    assert.equal(store.getState().toolCalls[0]?.status, 'failed');
    assert.equal(store.getState().toolCalls[0]?.progress?.steps.length, 2);
    assert.equal(summaries().length, 1);
    assert.match(transcript(), /Root answer/);
    assert.doesNotMatch(transcript(), /child-private/);
    await until(() => latestOutputFrame(current).includes('draft'));
  } finally {
    release();
  }
});

it('keeps repeated deltas and interleaved tools once across repeated snapshots', async () => {
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

it('renders streamed tool targets and Ctrl+O reveals the hidden output without another request', async () => {
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

it('slash menu, completion and help expose only supported commands including outputs', async () => {
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
  const sessions = new SessionClient('http://127.0.0.1:8790', async (url) => {
    calls.push(String(url));
    if (String(url).includes('/outputs/preview?')) {
      return Response.json({ type: 'file', path: '销售报告.md', content: '# Sales\n\nTotal: 42' });
    }
    assert.equal(String(url), 'http://127.0.0.1:8790/sessions/ui-test/outputs');
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

it('/outputs opens the empty view and q returns to the home composer', async () => {
  const sessions = new SessionClient('http://127.0.0.1:8790', async () => Response.json({ files: [] }));
  const current = view({ async *runAgent() { throw new Error('Must not call the model'); } }, 160, 24, sessions);
  const frame = () => latestOutputFrame(current);
  await tick();
  await submit(current, '/outputs');
  await until(() => frame().includes('Outputs (0)'));
  current.stdin.write('q');
  await until(() => frame().includes('GLM-5.2'));
  assert.doesNotMatch(frame(), /Outputs \(0\)|Ctrl\+G/);
});

it('/outputs reports backend failures instead of displaying an empty list', async () => {
  const sessions = new SessionClient('http://127.0.0.1:8790', async () =>
    Response.json({ detail: 'Unable to list session outputs' }, { status: 500 }));
  const current = view({ async *runAgent() { throw new Error('Must not call the model'); } }, 160, 24, sessions);
  await tick();
  await submit(current, '/outputs');
  await until(() => latestOutputFrame(current).includes('Unable to list session outputs'));
  assert.doesNotMatch(latestOutputFrame(current), /Outputs \(0\)/);
});

it('discards output responses from a session that has been cleared', async () => {
  let finish!: (response: Response) => void;
  const response = new Promise<Response>((resolve) => { finish = resolve; });
  const sessions = new SessionClient('http://127.0.0.1:8790', async () => response);
  const current = view({ async *runAgent() { throw new Error('Must not call the model'); } }, 160, 24, sessions);
  await tick();
  await submit(current, '/outputs');
  await until(() => latestOutputFrame(current).includes('Loading session outputs'));
  await submit(current, '/clear');
  await until(() => store.getState().threadId !== 'ui-test');
  finish(Response.json({ files: [{ path: 'old.txt', size: 4, modifiedAt: '2026-09-23T00:00:00Z' }] }));
  await tick();
  await tick();
  assert.doesNotMatch(latestOutputFrame(current), /Outputs \(1\)|old.txt/);
});

for (const status of [401, 403, 409, 422]) it(`HTTP ${status} produces one failure summary without retry or login UI`, async () => {
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
