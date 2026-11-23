import assert from 'node:assert/strict';
import { it } from 'node:test';
import { createInitialLiveRun, reduceLiveRunEvent } from './live-run-state.js';

it('duplicate starts retain dispatch time and all in-progress tool state', () => {
  let state = reduceLiveRunEvent(createInitialLiveRun(), { type: 'RUN_STARTED', runId: 'root' });
  state = { ...state, runStartedAt: 123 };
  state = reduceLiveRunEvent(state, { type: 'TOOL_CALL_START', toolCallId: 't', toolCallName: 'read_file' });
  assert.equal(reduceLiveRunEvent(state, { type: 'RUN_STARTED', runId: 'root' }), state);
});

it('snapshots and deltas cannot finish an active root run', () => {
  let state = reduceLiveRunEvent(createInitialLiveRun(), { type: 'RUN_STARTED', runId: 'root' });
  for (const event of [
    { type: 'STATE_SNAPSHOT', snapshot: { runStatus: 'completed' } },
    { type: 'STATE_DELTA', delta: [{ op: 'replace', path: '/runStatus', value: 'failed' }] },
    { type: 'STATE_SNAPSHOT', snapshot: { runStatus: 'idle' } },
  ]) {
    state = reduceLiveRunEvent(state, event);
    assert.equal(state.runStatus, 'running');
    assert.equal(state.runFinishedAt, undefined);
  }
  assert.equal(reduceLiveRunEvent(state, { type: 'RUN_FINISHED' }).runStatus, 'completed');
});

it('retains streamed AG-UI tool argument fragments for the displayed target and details', () => {
  let state = reduceLiveRunEvent(createInitialLiveRun(), { type: 'RUN_STARTED', runId: 'root' });
  state = reduceLiveRunEvent(state, { type: 'TOOL_CALL_START', toolCallId: 'read', toolCallName: 'read_file' });
  state = reduceLiveRunEvent(state, { type: 'TOOL_CALL_START', toolCallId: 'other', toolCallName: 'ls' });
  for (const delta of ['{"file_path":"/工作空间/', '哈哈', '哈哈', '.md"}']) {
    state = reduceLiveRunEvent(state, { type: 'TOOL_CALL_ARGS', toolCallId: 'read', delta });
  }
  state = reduceLiveRunEvent(state, { type: 'TOOL_CALL_END', toolCallId: 'read' });
  state = reduceLiveRunEvent(state, { type: 'TOOL_CALL_RESULT', toolCallId: 'read', content: 'done' });
  assert.deepEqual(JSON.parse(state.toolCalls.find((call) => call.id === 'read')!.args as string), {
    file_path: '/工作空间/哈哈哈哈.md',
  });
  assert.equal(state.toolCalls.find((call) => call.id === 'other')!.args, undefined);
});

it('full legacy arguments remain authoritative when supplied with a tool event', () => {
  let state = reduceLiveRunEvent(createInitialLiveRun(), { type: 'TOOL_CALL_START', toolCallId: 'read', toolCallName: 'read_file' });
  state = reduceLiveRunEvent(state, { type: 'TOOL_CALL_ARGS', toolCallId: 'read', delta: '{"file_path":' });
  state = reduceLiveRunEvent(state, { type: 'TOOL_CALL_ARGS', toolCallId: 'read', args: { file_path: '/complete.md' } });
  assert.deepEqual(state.toolCalls[0]!.args, { file_path: '/complete.md' });
});

const childStart = (toolCallId: string, extra: Record<string, unknown> = {}) => ({
  type: 'SUBAGENT_STARTED', subagentRunId: `child-${toolCallId}`, parentToolCallId: toolCallId,
  name: 'nl2sql', ...extra,
});
const step = (id: string, type = 'STEP_STARTED', stepName = 'generator') => ({
  type, subagentRunId: `child-${id}`, stepName,
});

it('native subagent events precede a late tool start, stay scoped and preserve retries', (t) => {
  let now = 0;
  t.mock.method(performance, 'now', () => now);
  let state = reduceLiveRunEvent(createInitialLiveRun(), { type: 'RUN_STARTED', runId: 'root' });
  for (const id of ['a', 'b']) {
    state = reduceLiveRunEvent(state, childStart(id));
    state = reduceLiveRunEvent(state, step(id));
  }
  const before = state;
  assert.equal(reduceLiveRunEvent(state, childStart('a')), before, 'duplicate start is ignored');
  assert.equal(reduceLiveRunEvent(state, step('a')), before, 'duplicate active step is ignored');
  assert.equal(reduceLiveRunEvent(state, step('unknown')), before, 'unlinked events are ignored');
  assert.equal(reduceLiveRunEvent(state, childStart('unknown', { parentToolCallId: undefined })), before);
  state = reduceLiveRunEvent(state, { type: 'TOOL_CALL_START', toolCallId: 'a', toolCallName: 'task' });
  state = reduceLiveRunEvent(state, { type: 'TOOL_CALL_ARGS', toolCallId: 'a', delta: '{"subagent_type":"nl2sql"}' });
  now = 42;
  state = reduceLiveRunEvent(state, step('a', 'STEP_FINISHED'));
  assert.equal(reduceLiveRunEvent(state, step('a', 'STEP_FINISHED')), state, 'duplicate finish is ignored');
  state = reduceLiveRunEvent(state, step('a'));
  assert.equal(state.toolCalls.length, 2);
  assert.deepEqual(state.toolCalls[0]!.progress?.steps.map((item) => item.status), ['completed', 'running']);
  assert.equal(state.toolCalls[0]!.progress?.steps[0]?.durationMs, 42);
  assert.equal(state.toolCalls[1]!.progress?.steps.length, 1);
  assert.equal(state.toolCalls[1]!.progress?.steps[0]?.status, 'running');
  assert.deepEqual(JSON.parse(state.toolCalls[0]!.args as string), { subagent_type: 'nl2sql' });
  state = reduceLiveRunEvent(state, { type: 'SUBAGENT_FINISHED', subagentRunId: 'child-a' });
  assert.equal(state.toolCalls[0]!.status, 'running', 'child finish is not a task result');
  assert.equal(state.runStatus, 'running');
  state = reduceLiveRunEvent(state, { type: 'TOOL_CALL_RESULT', toolCallId: 'a', content: '{"error":"Query failed"}' });
  assert.equal(state.toolCalls[0]!.status, 'failed');
  assert.equal(reduceLiveRunEvent(state, step('a')), state, 'late progress cannot revive a finished task');
  state = reduceLiveRunEvent(state, { type: 'RUN_FINISHED' });
  assert.equal(state.toolCalls[0]!.status, 'failed', 'root success cannot erase a failed task');
});

it('nested children stay under their exact parent and cannot close sibling or root work', () => {
  let state = reduceLiveRunEvent(createInitialLiveRun(), { type: 'RUN_STARTED', runId: 'root' });
  state = reduceLiveRunEvent(state, childStart('a'));
  state = reduceLiveRunEvent(state, step('a'));
  assert.equal(reduceLiveRunEvent(state, childStart('orphan', { parentSubagentRunId: 'unknown' })), state);
  state = reduceLiveRunEvent(state, childStart('nested', { parentSubagentRunId: 'child-a', name: 'checker' }));
  state = reduceLiveRunEvent(state, step('nested'));
  assert.equal(state.toolCalls.length, 1);
  assert.equal(state.toolCalls[0]!.progress?.runs.length, 2);
  for (const event of [
    { type: 'TOOL_CALL_START', toolCallId: 'inner-tool', toolCallName: 'read_file' },
    { type: 'RUN_FINISHED' },
    { type: 'STATE_SNAPSHOT', snapshot: { runStatus: 'completed' } },
  ]) assert.equal(reduceLiveRunEvent(state, { ...event, subagentRunId: 'child-nested' }), state);
  state = reduceLiveRunEvent(state, { type: 'SUBAGENT_ERROR', subagentRunId: 'child-nested', message: 'bad', code: 'QueryError' });
  assert.deepEqual(state.toolCalls[0]!.progress?.steps.map((item) => item.status), ['running', 'failed']);
  assert.equal(state.toolCalls[0]!.progress?.runs[1]?.errorMessage, 'bad (QueryError)');
  assert.equal(state.toolCalls[0]!.status, 'running');
  assert.equal(state.runStatus, 'running');
  state = reduceLiveRunEvent(state, { type: 'SUBAGENT_FINISHED', subagentRunId: 'child-a' });
  assert.equal(state.toolCalls[0]!.progress?.status, 'failed', 'nested error stays in history');
});

it('root errors close pending subagent steps and prevent later progress from reviving them', () => {
  let state = reduceLiveRunEvent(createInitialLiveRun(), { type: 'RUN_STARTED', runId: 'root' });
  state = reduceLiveRunEvent(state, childStart('a'));
  state = reduceLiveRunEvent(state, step('a'));
  state = reduceLiveRunEvent(state, { type: 'RUN_ERROR', message: 'TimeoutError' });
  assert.equal(state.toolCalls[0]!.progress?.steps[0]?.status, 'failed');
  assert.ok(state.toolCalls[0]!.progress?.steps[0]?.durationMs !== undefined);
  assert.equal(reduceLiveRunEvent(state, step('a')), state);
});

it('a suspended native child is never marked successful', () => {
  let state = reduceLiveRunEvent(createInitialLiveRun(), { type: 'RUN_STARTED', runId: 'root' });
  state = reduceLiveRunEvent(state, childStart('a'));
  state = reduceLiveRunEvent(state, step('a'));
  state = reduceLiveRunEvent(state, {
    type: 'SUBAGENT_FINISHED', subagentRunId: 'child-a', outcome: { type: 'suspended', interruptIds: ['interrupt'] },
  });
  assert.equal(state.toolCalls[0]!.progress?.status, 'suspended');
  assert.equal(state.toolCalls[0]!.progress?.steps[0]?.status, 'suspended');
  assert.equal(state.toolCalls[0]!.status, 'running');
});

it('a server-confirmed cancel reads as a stop, not a failure message', () => {
  let state = reduceLiveRunEvent(createInitialLiveRun(), { type: 'RUN_STARTED', runId: 'root' });
  state = reduceLiveRunEvent(state, { type: 'RUN_ERROR', code: 'CANCELLED', message: 'Run was cancelled.' });
  assert.equal(state.runStatus, 'failed');
  assert.equal(state.errorMessage, 'Stopped');
});

it('links an artifact only to its recorded tool call', () => {
  let state = reduceLiveRunEvent(createInitialLiveRun(), { type: 'RUN_STARTED', runId: 'root' });
  state = reduceLiveRunEvent(state, { type: 'TOOL_CALL_START', toolCallId: 'sql', toolCallName: 'run_sql_readonly' });
  state = reduceLiveRunEvent(state, { type: 'TOOL_CALL_RESULT', toolCallId: 'sql', content: '{"row_count":1}' });
  for (const value of [
    { id: 'standalone', type: 'table', preview_json: { row_count: 1 } },
    { id: 'linked', type: 'table', tool_call_id: 'sql' },
  ]) state = reduceLiveRunEvent(state, { type: 'CUSTOM', name: 'artifact', value });
  assert.equal(state.artifacts.find((item) => item.id === 'standalone')?.createdByEventId, undefined);
  assert.equal(state.artifacts.find((item) => item.id === 'linked')?.createdByEventId, 'sql');
});
