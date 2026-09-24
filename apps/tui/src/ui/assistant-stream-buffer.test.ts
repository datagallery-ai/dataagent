import assert from 'node:assert/strict';
import { it } from 'node:test';
import { AssistantTextStreamBuffer } from './assistant-stream-buffer.js';

it('appends identical and overlapping deltas without guessing cumulative text', () => {
  for (const [chunks, expected] of [
    [['哈哈', '哈哈'], '哈哈哈哈'],
    [['ha', 'ha!'], 'haha!'],
    [['a', 'abc'], 'aabc'],
  ] as const) {
    const buffer = new AssistantTextStreamBuffer();
    for (const chunk of chunks) buffer.append(chunk);
    assert.deepEqual(buffer.flush(false), { type: 'text', content: expected, isStreaming: false });
  }
});

it('retains internal context filtering and text boundaries around tools', () => {
  const buffer = new AssistantTextStreamBuffer();
  buffer.append('Before<working_');
  assert.deepEqual(buffer.flush(), { type: 'text', content: 'Before', isStreaming: true });
  buffer.append('memory_data>secret</working_memory_data>');
  assert.equal(buffer.flush(), null);
  buffer.markSegmentBoundary();
  buffer.append('After');
  assert.deepEqual(buffer.flush(false), { type: 'text', content: 'After', isStreaming: false });
});
