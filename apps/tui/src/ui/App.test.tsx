import assert from 'node:assert/strict';
import { afterEach, it } from 'node:test';
import { PassThrough } from 'node:stream';
import { setTimeout as delay } from 'node:timers/promises';
import { stripVTControlCharacters } from 'node:util';
import React from 'react';
import { render } from 'ink';
import { ConfigClient } from '../config/index.js';
import { CopilotKitClient } from '../protocol/copilotkit-client.js';
import { TuiAuthClient, TuiCookieJar, AuthenticatedTransport } from '../auth/index.js';
import { store } from '../state/store.js';
import { getMessageTextContent } from '../state/message-history.js';
import { App } from './App.js';
import { textWidth } from './text-width.js';
import { createInitialLiveRun, reduceLiveRunEvent } from '../state/live-run-state.js';
import { formatRunDuration } from './transcript-lines.js';
import { installFullscreenCursorCorrection } from './fullscreen-cursor.js';

let cleanup = () => {};
afterEach(() => { cleanup(); store.reset(); });

async function until(check: () => boolean, description: string, timeout = 15000) {
  const deadline = Date.now() + timeout;
  while (!check()) {
    if (Date.now() > deadline) throw new Error(`Timed out: ${description}`);
    await delay(25);
  }
}

function mount(fetchImpl: typeof fetch, baseUrl = 'http://localhost', options: { interactive?: boolean; columns?: number; rows?: number } = {}) {
  store.reset();
  store.setThreadId('tui-smoke-thread');
  const stdin = Object.assign(new PassThrough(), {
    isTTY: true, isRaw: false, setRawMode(mode: boolean) { this.isRaw = mode; },
    ref() { return this; }, unref() { return this; },
  });
  const stdout = Object.assign(new PassThrough(), { columns: options.columns ?? 80, rows: options.rows ?? 24, isTTY: true });
  const frames: string[] = [];
  const raw: string[] = [];
  stdout.on('data', (data: Buffer) => { raw.push(data.toString()); frames.push(stripVTControlCharacters(data.toString())); });
  const client = new CopilotKitClient({ runtimeUrl: `${baseUrl}/api/copilotkit`, agent: 'dataFoundry', fetchImpl });
  const view = render(<App client={client} configClient={new ConfigClient({ baseUrl, fetchImpl })} datasourceId={undefined} />, {
    stdin: stdin as unknown as NodeJS.ReadStream, stdout: stdout as unknown as NodeJS.WriteStream,
    stderr: stdout as unknown as NodeJS.WriteStream, debug: !options.interactive, interactive: true,
    incrementalRendering: true,
    exitOnCtrlC: false, patchConsole: false,
  });
  cleanup = () => { view.unmount(); view.cleanup(); client.dispose(); stdin.destroy(); stdout.destroy(); };
  return {
    frames, raw, stdin, stdout,
    async submit(text: string) { stdin.write(text); await delay(80); stdin.write('\r'); },
    async ready() { await until(() => frames.some(f => f.includes('Ask a question')), 'backend configuration rendered'); },
  };
}

// Replay actual ANSI output, including cursor movement and terminal-edge
// clamping. String snapshots cannot detect a cursor painted on the wrong row.
function terminalScreen(output: string, columns: number, rows: number) {
  const blank = () => Array<string>(columns).fill(' ');
  const lines = Array.from({ length: rows }, blank);
  let x = 0;
  let y = 0;
  let wrapPending = false;
  const nextLine = () => {
    y++;
    if (y >= rows) { lines.shift(); lines.push(blank()); y = rows - 1; }
  };
  for (const match of output.matchAll(/\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07)|[\s\S]/gu)) {
    const token = match[0];
    if (token.startsWith('\x1b[')) {
      const params = token.slice(2, -1);
      if (/[?<=>]/.test(params)) continue;
      const [a = 0, b = 0] = params.split(';').map(Number);
      switch (token.at(-1)) {
        case 'A': y = Math.max(0, y - (a || 1)); break;
        case 'B': y = Math.min(rows - 1, y + (a || 1)); break;
        case 'C': x = Math.min(columns - 1, x + (a || 1)); break;
        case 'D': x = Math.max(0, x - (a || 1)); break;
        case 'E': y = Math.min(rows - 1, y + (a || 1)); x = 0; break;
        case 'G': x = Math.min(columns - 1, Math.max(0, (a || 1) - 1)); break;
        case 'H': case 'f': y = Math.min(rows - 1, (a || 1) - 1); x = Math.min(columns - 1, (b || 1) - 1); break;
        case 'J': if (a === 2) lines.forEach(line => line.fill(' ')); break;
        case 'K': lines[y]!.fill(' ', a === 2 ? 0 : x); break;
        default: continue;
      }
      wrapPending = false;
    } else if (token.startsWith('\x1b')) {
      continue;
    } else if (token === '\n') {
      nextLine(); x = 0; wrapPending = false;
    } else if (token === '\r') {
      x = 0; wrapPending = false;
    } else {
      const width = textWidth(token);
      if (!width) continue;
      if (wrapPending || x + width > columns) { nextLine(); x = 0; }
      lines[y]![x] = token;
      if (width === 2) lines[y]![x + 1] = '';
      x += width;
      wrapPending = x >= columns;
      if (wrapPending) x = columns - 1;
    }
  }
  return { x, y, lines };
}

const common = { name: 'resource', description: '', defaultEnabled: true, builtin: false, revision: 1, createdAt: '', updatedAt: '' };

it('corrects only the fullscreen cursor suffix and restores the terminal writer', () => {
  const stdout = Object.assign(new PassThrough(), { isTTY: true });
  const chunks: string[] = [];
  stdout.on('data', chunk => chunks.push(chunk.toString()));
  const original = stdout.write;
  const restore = installFullscreenCursorCorrection(stdout as unknown as NodeJS.WriteStream);
  stdout.write('frame\x1b[3A\x1b[6G\x1b[?25h');
  assert.equal(chunks.at(-1), 'frame\x1b[2A\x1b[6G\x1b[?25h');
  stdout.write('\x1b[1A\x1b[6G\x1b[?25h');
  assert.equal(chunks.at(-1), '\x1b[6G\x1b[?25h');
  stdout.write('\x1b[2B\x1b[1Gordinary output');
  assert.equal(chunks.at(-1), '\x1b[2B\x1b[1Gordinary output');
  restore();
  assert.equal(stdout.write, original);
  stdout.destroy();
});
const skill = { ...common, id: 'skill-1', packageFormat: 'skill-md' };
const configs: Record<string, unknown> = {
  '/api/v1/capabilities': { skills: true, mcp: true, 'runtime.dataTools': false, 'conversation.memory': false, 'artifact.export': false },
  '/api/v1/run-defaults': { enabledDatasourceIds: [], enabledKnowledgeIds: [], enabledMcpServerIds: ['mcp-1'], enabledSkillIds: ['skill-1'], activeSkillId: 'skill-1', activeLlmProfileId: 'model-1' },
  '/api/v1/workspace-config': { datasources: [], knowledgeBases: [], skills: [skill],
    modelProfiles: [{ ...common, id: 'model-1', modelName: 'server-model', secretRef: null }],
    mcpServers: [{ ...common, id: 'mcp-1', transport: 'streamable-http', serverUrl: 'http://localhost/mcp', secretRef: null }],
  },
  '/api/v1/skills': [skill],
};

function fakeBackend(mode: 'success' | 'EOF' | 'error', finishGate?: Promise<void>) {
  const calls: string[] = [];
  const inputs: Array<Record<string, any>> = [];
  const fetchImpl: typeof fetch = async (url, init) => {
    const path = new URL(String(url)).pathname;
    calls.push(path);
    if (path === '/healthz') return Response.json({ ok: true });
    if (path in configs) return Response.json({ success: true, data: configs[path] });
    assert.equal(path, '/api/copilotkit', `unexpected API: ${path}`);
    const input = JSON.parse(String(init?.body));
    inputs.push(input);
    const events = [
      { type: 'RUN_STARTED', threadId: input.threadId, runId: input.runId },
      { type: 'TOOL_CALL_START', toolCallId: 'tool-1', toolCallName: 'read_workspace_file', parentMessageId: 'a-1' },
      { type: 'TOOL_CALL_ARGS', toolCallId: 'tool-1', delta: '{"path":"smoke.txt"}' },
      { type: 'TOOL_CALL_END', toolCallId: 'tool-1' },
      { type: 'TOOL_CALL_RESULT', toolCallId: 'tool-1', messageId: 'result-1', content: '{"content":"TUI_SMOKE_OK"}', role: 'tool' },
      { type: 'TEXT_MESSAGE_START', messageId: 'a-2', role: 'assistant' },
      { type: 'TEXT_MESSAGE_CONTENT', messageId: 'a-2', delta: 'ha' },
      { type: 'TEXT_MESSAGE_CONTENT', messageId: 'a-2', delta: 'ha TUI_SMOKE_OK' },
      ...(mode === 'success' ? [{ type: 'TEXT_MESSAGE_END', messageId: 'a-2' }, { type: 'RUN_FINISHED', threadId: input.threadId, runId: input.runId }] : []),
      ...(mode === 'error' ? [{ type: 'RUN_ERROR', code: 'RUN_TIMEOUT', message: 'Agent run exceeded the configured timeout of 1000 ms.' }] : []),
    ];
    if (finishGate) {
      const encoder = new TextEncoder();
      return new Response(new ReadableStream({ async start(controller) {
        for (const event of events.slice(0, -1)) controller.enqueue(encoder.encode(`data: ${JSON.stringify(event)}\n\n`));
        await finishGate;
        controller.enqueue(encoder.encode(`data: ${JSON.stringify(events.at(-1))}\n\n`));
        controller.close();
      } }), { headers: { 'content-type': 'text/event-stream' } });
    }
    return new Response(events.map(e => `data: ${JSON.stringify(e)}\n\n`).join(''), { headers: { 'content-type': 'text/event-stream' } });
  };
  return { fetchImpl, calls, inputs };
}

for (const columns of [60, 80, 140]) {
  it(`keeps the terminal cursor on the actual draft in a ${columns}x24 app through edits and home/chat transitions`, async () => {
    const backend = fakeBackend('success');
    const view = mount(backend.fetchImpl, 'http://localhost', { interactive: true, columns, rows: 24 });
    await view.ready();
    const assertCursorAfter = async (text: string) => {
      await delay(120);
      const screen = terminalScreen(view.raw.join(''), view.stdout.columns, view.stdout.rows);
      assert.ok(screen.lines[screen.y]!.slice(0, screen.x).join('').endsWith(text),
        `cursor (${screen.x}, ${screen.y}) must follow ${text}:\n${screen.lines.map(l => l.join('')).join('\n')}`);
      const modelRows = screen.lines.map(l => l.join('')).flatMap((line, index) => line.includes('model: server-model') ? [index] : []);
      assert.deepEqual(modelRows, [screen.y + 2], 'model sits directly below the composer separator, only once');
      assert.equal(modelRows[0], view.stdout.rows - 1, 'model occupies the final terminal row without blank padding');
      return screen.y;
    };
    view.stdin.write('中文A');
    const firstRow = await assertCursorAfter('中文A');
    view.stdin.write('B');
    await assertCursorAfter('中文AB');
    view.stdin.write('\x1b[13;2u');
    await delay(80);
    view.stdin.write('第二行');
    assert.equal(await assertCursorAfter('第二行'), firstRow, 'bottom stays anchored while composer grows upward');
    view.stdin.write('\r');
    await until(() => store.getState().runStatus === 'completed', 'chat mode');
    view.stdin.write('后续问题');
    assert.equal(await assertCursorAfter('后续问题'), view.stdout.rows - 3,
      'chat leaves only its separator and model row below input');
    view.stdin.write('\x7f');
    await assertCursorAfter('后续问');
    view.raw.length = 0;
    view.stdout.columns = columns + 10;
    view.stdout.rows = 28;
    view.stdout.emit('resize');
    await assertCursorAfter('后续问');
  });
}

it('hydrates real selections, renders tool/text events, and gates unsupported commands', async () => {
  const backend = fakeBackend('success');
  const view = mount(backend.fetchImpl);
  await view.ready();
  assert.deepEqual(store.getState().workspaceConfig.llm.map(r => r.id), ['model-1']);
  await view.submit('Read the fixture');
  await until(() => store.getState().runStatus === 'completed', 'completed run');
  await until(() => view.frames.some(f => f.includes('haha TUI_SMOKE_OK')), 'Ink text');
  assert.equal(store.getState().toolCalls[0]?.name, 'read_workspace_file');
  assert.ok(view.frames.some(f => f.includes('read_workspace_file')));
  assert.equal(backend.inputs[0]?.forwardedProps.run_config.activeLlmProfileId, 'model-1');
  assert.deepEqual(backend.inputs[0]?.forwardedProps.run_config.enabledMcpServerIds, ['mcp-1']);
  assert.deepEqual(backend.inputs[0]?.forwardedProps.run_config.enabledSkillIds, ['skill-1']);
  await view.submit('Second question');
  await until(() => backend.inputs.length === 2 && store.getState().runStatus === 'completed', 'second run');
  const summaries = store.getState().messages.filter(m => m.runSummary);
  assert.equal(summaries.length, 2);
  assert.notEqual(summaries[0]?.id, summaries[1]?.id);
  assert.ok(summaries.every(m => m.runSummary?.status === 'completed'));
  assert.equal(backend.inputs[1]?.messages.length, 1);
  assert.equal(backend.inputs[1]?.messages[0].content, 'Second question');
  await view.submit('/resume');
  await until(() => view.frames.some(f => f.includes('/resume is not supported')), 'resume gate');
  await view.submit('/datasource');
  await until(() => view.frames.some(f => f.includes('/datasource is not supported')), 'datasource gate');
  assert.ok(!backend.calls.some(path => /sessions|datasources|artifacts/.test(path)));
});

it('keeps partial output, marks EOF failed, and never resubmits the run', async () => {
  const backend = fakeBackend('EOF');
  const view = mount(backend.fetchImpl);
  await view.ready();
  await view.submit('Read the fixture');
  await until(() => store.getState().runStatus === 'failed', 'incomplete run');
  assert.equal(backend.inputs.length, 1);
  assert.ok(store.getState().messages.some(m => getMessageTextContent(m).includes('haha TUI_SMOKE_OK')));
  assert.match(store.getState().errorMessage ?? '', /without a terminal event/);
  assert.equal(store.getState().messages.at(-1)?.runSummary?.status, 'interrupted');
  assert.ok(!view.frames.some(f => f.includes('Run completed')));
});

it('keeps progress in chat until RUN_FINISHED, then records completion once after final text', async () => {
  let finish!: () => void;
  const gate = new Promise<void>(resolve => { finish = resolve; });
  const backend = fakeBackend('success', gate);
  const view = mount(backend.fetchImpl);
  try {
    await view.ready();
    await view.submit('Read the fixture');
    await until(() => view.frames.some(f => f.includes('Running.') && f.includes('haha TUI_SMOKE_OK')), 'running feedback');
    assert.equal(store.getState().runStatus, 'running');
    assert.ok(!store.getState().messages.some(m => m.runSummary));
    finish();
    await until(() => view.frames.some(f => /Run completed · [\d.]+s/.test(f)), 'completion feedback');
    const summary = store.getState().messages.at(-1)!;
    assert.equal(summary.runSummary?.status, 'completed');
    assert.ok(summary.runSummary!.durationMs >= 0);
    store.recordRunSummary('completed');
    assert.equal(store.getState().messages.filter(m => m.runSummary).length, 1);
    assert.equal(store.getState().messages.at(-1), summary);
  } finally { finish(); }
});

it('shows an English failure summary for an explicit backend error', async () => {
  const backend = fakeBackend('error');
  const view = mount(backend.fetchImpl);
  await view.ready();
  await view.submit('Read the fixture');
  await until(() => view.frames.some(f => f.includes('Run failed ·')), 'failure feedback');
  assert.equal(store.getState().messages.at(-1)?.runSummary?.status, 'failed');
  assert.ok(view.frames.some(f => f.includes('RUN_TIMEOUT') && f.includes('configured timeout of 1000 ms.')));
  assert.ok(!view.frames.some(f => f.includes('Network connection failed') || f.includes('INCOMPLETE_STREAM')));
  assert.ok(!view.frames.some(f => f.includes('Run completed')));
});

it('times dispatch through terminal event, ignoring repeated starts and premature snapshots', t => {
  let now = 1000;
  t.mock.method(Date, 'now', () => now);
  let run = reduceLiveRunEvent(createInitialLiveRun(), { type: 'RUN_STARTED', runId: 'client' });
  now = 2500;
  run = reduceLiveRunEvent(run, { type: 'RUN_STARTED', runId: 'server' });
  assert.equal(run.runStartedAt, 1000);
  run = reduceLiveRunEvent(run, { type: 'STATE_SNAPSHOT', snapshot: { runStatus: 'completed' } });
  assert.equal(run.runStatus, 'running');
  assert.equal(run.runFinishedAt, undefined);
  now = 13400;
  run = reduceLiveRunEvent(run, { type: 'RUN_FINISHED' });
  assert.equal(formatRunDuration(run.runFinishedAt! - run.runStartedAt!), '12.4s');
  now = 20000;
  assert.equal(reduceLiveRunEvent(run, { type: 'RUN_FINISHED' }).runFinishedAt, 13400);
  const next = reduceLiveRunEvent(run, { type: 'RUN_STARTED', runId: 'next' });
  assert.equal(next.runStartedAt, 20000);
  assert.equal(next.runFinishedAt, undefined);
  assert.equal(formatRunDuration(125000), '2m 5s');
});

it('renders a real Python DataAgent tool round trip and retains backend conversation history', { skip: !process.env.TUI_SMOKE_API_URL }, async () => {
  const baseUrl = process.env.TUI_SMOKE_API_URL!;
  const jar = new TuiCookieJar();
  const auth = new TuiAuthClient({ apiBaseUrl: baseUrl, cookieJar: jar });
  if (process.env.TUI_SMOKE_LOCAL_TOKEN) {
    jar.replace({ df_session: process.env.TUI_SMOKE_LOCAL_TOKEN, df_csrf: process.env.TUI_SMOKE_LOCAL_CSRF! });
    assert.equal((await auth.me()).id, 'local-tui');
  } else {
    await auth.login('tui@example.test', 'smoke-password');
  }
  const transport = new AuthenticatedTransport({ cookieJar: jar, refreshCsrf: () => auth.refreshCsrf(), onSessionInvalid: async () => { throw new Error('Unexpected auth failure'); } });
  const view = mount(transport.fetch.bind(transport), baseUrl);
  await view.ready();
  await view.submit('Read smoke.txt from the shared workspace');
  await until(() => ['completed', 'failed'].includes(store.getState().runStatus), 'Python run', 30000);
  assert.equal(store.getState().runStatus, 'completed', store.getState().errorMessage);
  assert.equal(store.getState().toolCalls[0]?.name, 'read_workspace_file');
  await until(() => view.frames.some(f => f.includes('TUI_SMOKE_OK')), 'real Ink output');
  await view.submit('What was the previous result?');
  await until(() => store.getState().messages.some(m => getMessageTextContent(m).includes('HISTORY_OK')), 'checkpoint history', 30000);
  assert.equal(store.getState().runStatus, 'completed', store.getState().errorMessage);
});
