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
