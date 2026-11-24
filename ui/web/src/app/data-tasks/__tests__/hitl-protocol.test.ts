import { describe, expect, it } from 'vitest';
import { toStreamRunInput } from '../../../lib/stream-run';
import { getHitlPending, parseHitlPending, setHitlPending } from '../../../lib/hitl-state';
import { createInitialLiveRun, reduceLiveRunEvent } from '../live-run-state';
import { liveRunFromNativeMessages } from '../conversation-restore';

describe('native HITL protocol', () => {
  it('transports answers without replaying user messages or client state', () => {
    const body = toStreamRunInput({ threadId: 'thread', runId: 'reply', messages: [],
      tools: [], context: [], state: { unsafe: true }, forwardedProps: { hitlAnswers: { ask: 'user_id' } } });
    expect(body.answers).toEqual({ ask: 'user_id' });
    expect(body.messages).toEqual([]);
    expect(body.state).toEqual({});
    expect(body.forwardedProps).toEqual({});
  });
  it('keeps pending questions isolated by thread', () => {
    const value = parseHitlPending([{ type: 'human_feedback', interruptId: 'ask', message: 'Which ID?' }]);
    setHitlPending('first', value);
    expect(getHitlPending('first')).toEqual(value);
    expect(getHitlPending('second')).toEqual([]);
    setHitlPending('first', []);
    expect(getHitlPending('first')).toEqual([]);
  });
  it('does not mark a waiting task completed', () => {
    const result = reduceLiveRunEvent(createInitialLiveRun(), { type: 'RUN_FINISHED', metadata: { status: 'waiting_for_input' } });
    expect(result.runStatus).toBe('suspended');
  });
  it('restores a waiting session even before the first model response', () => {
    expect(liveRunFromNativeMessages([{ id: 'user', role: 'user', content: 'SQL' }], 'waiting_for_input').runStatus).toBe('suspended');
  });
});
