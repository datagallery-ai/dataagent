import React from 'react';
import assert from 'node:assert/strict';
import { PassThrough } from 'node:stream';
import { it } from 'node:test';
import { stripVTControlCharacters } from 'node:util';
import { Box, Text, render, renderToString } from 'ink';
import type { DataArtifact, DisplayMessage, LiveToolCallRecord } from '../state/index.js';
import { ChatArea } from './ChatArea.js';
import { buildChatLines, chatContentWidth } from './transcript-lines.js';
import { textWidth } from './text-width.js';
import { StatusBar } from './StatusBar.js';

const text = (id: string, role: 'user' | 'assistant', content: string): DisplayMessage => ({
  id, role, timestamp: 1, elements: [{ type: 'text', content, timestamp: 1 }],
});
const tool: LiveToolCallRecord = {
  id: 'read', name: 'read_file', status: 'success',
  args: { file_path: '/workspace/中文文件.md' },
  result: Array.from({ length: 10 }, (_, i) => `Output line ${i + 1}`).join('\n'),
  startedAtMs: 1, finishedAtMs: 121,
};
const action: DisplayMessage = {
  id: 'action', role: 'assistant', timestamp: 1,
  elements: [{ type: 'tool_call', toolCallId: tool.id, timestamp: 1 }],
};
const renderPlain = (node: React.ReactNode, columns: number) => stripVTControlCharacters(
  renderToString(<Box width={columns} flexDirection="column">{node}</Box>, { columns }),
);

for (const columns of [60, 80, 160]) for (const compactMode of [true, false]) it(`conversation keeps exact rows at ${columns} columns (compact: ${compactMode})`, () => {
  const messages = [
    text('user', 'user', '你好，**请保留原文**。' + '中e\u0301👩‍💻'.repeat(30)),
    text('answer', 'assistant', '我会先读取文件。\n\n- 保留中文、emoji 与组合字符。\n\n| 字段 | 值 |\n| --- | ---: |\n| 中文 | 42 |'),
    action,
  ];
  const lines = buildChatLines({ messages, toolCalls: [tool], artifacts: [], columns, compactMode });
  const width = chatContentWidth(columns);
  for (const line of lines) {
    const rendered = renderPlain(line.node, width);
    assert.equal(rendered.split('\n').length, 1, `visual row ${line.key} rewrapped`);
    assert.ok(textWidth(rendered) <= width, `visual row ${line.key} overflowed`);
  }
  const frame = renderPlain(lines.map((line) => line.node), width);
  assert.match(frame, / › 你好，\*\*请保留原文\*\*/);
  assert.match(frame, / • 我会先读取文件/);
  assert.match(frame, / • Read \/workspace\/中文文件.md/);
  assert.doesNotMatch(frame, /YOU|AGENT|SYSTEM|Parameters|Result|┃/);
  assert.equal(frame.split('\n').length, lines.length);
});

it('tool preview is bounded and the detail view retains the hidden output and arguments', () => {
  const frame = (compactMode: boolean) => renderPlain(buildChatLines({
    messages: [action], toolCalls: [tool], artifacts: [], columns: 80, compactMode,
  }).map((line) => line.node), 76);
  assert.match(frame(true), /└ Output line 1/);
  assert.match(frame(true), /Output line 2/);
  assert.doesNotMatch(frame(true), /Output line 3/);
  assert.match(frame(true), /Ctrl\+O details/);
  assert.match(frame(false), /└ Input/);
  assert.match(frame(false), /file path: \/workspace\/中文文件.md/);
  assert.match(frame(false), /Output line 10/);
});

it('failed tool state and reason remain visible in the compact monochrome transcript', () => {
  const failed: LiveToolCallRecord = { ...tool, status: 'failed', result: JSON.stringify({ error: 'Permission denied' }) };
  const frame = renderPlain(buildChatLines({
    messages: [action], toolCalls: [failed], artifacts: [], columns: 60, compactMode: true,
  }).map((line) => line.node), 56);
  assert.match(frame, /✗ Failed · Read/);
  assert.match(frame, /error: Permission denied/);
});

it('footer prioritizes model and connection failure without wrapping a long Unicode workspace', () => {
  for (const columns of [39, 56, 76, 114]) {
    const frame = renderPlain(<StatusBar columns={columns} startup={{
      modelName: 'GLM-5.2', directory: '~/工作空间/中文目录'.repeat(10),
      threadId: undefined, connectionStatus: 'disconnected', runStatus: 'idle',
    }} />, columns);
    assert.equal(frame.split('\n').length, 1);
    assert.ok(textWidth(frame) <= columns);
    assert.match(frame, /GLM-5.2/);
    assert.match(frame, /Disconnected/);
  }
});

for (const columns of [60, 80]) it(`running animation keeps the timer and composer anchored at ${columns} columns`, async (t) => {
  let now = 1000;
  t.mock.method(performance, 'now', () => now);
  t.mock.timers.enable({ apis: ['setInterval'] });
  const stdout = Object.assign(new PassThrough(), { isTTY: true, columns, rows: 24 });
  const frames: string[] = [];
  stdout.on('data', (chunk) => frames.push(stripVTControlCharacters(String(chunk))));
  const messages = [text('user', 'user', '请分析文件')];
  const scene = (running: boolean) => <Box width={columns} height={24} flexDirection="column">
    <ChatArea messages={messages} artifacts={[]} columns={columns} viewportRows={23}
      runStartedAt={running ? 1000 : undefined} />
    <Text>Composer anchor</Text>
  </Box>;
  const view = render(scene(true), {
    stdout: stdout as unknown as NodeJS.WriteStream,
    stdin: new PassThrough() as unknown as NodeJS.ReadStream,
    debug: true, interactive: true, exitOnCtrlC: false, patchConsole: false,
  });
  const flush = async () => {
    await new Promise<void>((resolve) => setImmediate(resolve));
    await view.waitUntilRenderFlush();
  };
  try {
    await flush();
    const progress: string[] = [];
    for (let frame = 0; frame < 40; frame += 1) {
      now += 80;
      t.mock.timers.tick(80);
      await flush();
      const rows = frames.at(-1)!.split('\n');
      assert.equal(rows.length, 24);
      assert.equal(rows[23], 'Composer anchor');
      const line = rows.find((row) => row.includes('Running'))!;
      assert.ok(line);
      assert.ok(textWidth(line) <= chatContentWidth(columns));
      progress.push(line);
    }
    assert.equal(new Set(progress.map((line) => line.indexOf('·'))).size, 1, 'timer separator moved');
    assert.equal(new Set(progress.map(textWidth)).size, 1, 'timer right edge moved');
    assert.ok(new Set(progress.map((line) => line.split('Running')[0])).size > 1, 'indicator did not animate');
    assert.ok(new Set(progress.map((line) => line.split('·')[1])).size > 1, 'duration did not advance');
    view.rerender(scene(false));
    await flush();
    now += 1000;
    t.mock.timers.tick(1000);
    await flush();
    assert.doesNotMatch(frames.at(-1)!, /Running/);
  } finally {
    view.unmount();
    view.cleanup();
  }
});

it('running duration keeps a fixed right edge across digit and minute transitions', (t) => {
  let now = 0;
  t.mock.method(performance, 'now', () => now);
  for (const columns of [60, 80, 160]) {
    const widths = new Set<number>();
    for (const elapsed of [9900, 10000, 59900, 60000, 69000, 70000, 599000, 600000, 3599000, 3600000]) {
      now = 1000 + elapsed;
      const progress = buildChatLines({
        messages: [text('user', 'user', 'Test')], artifacts: [], columns, runStartedAt: 1000,
      }).find((line) => line.key === 'run:progress')!;
      const frame = renderPlain(progress.node, chatContentWidth(columns));
      assert.equal(frame.split('\n').length, 1);
      assert.match(frame, /Running.*·\s+\d/);
      widths.add(textWidth(frame));
    }
    assert.equal(widths.size, 1, 'duration changed the status row width');
  }
});

it('outputs appear once below their source reply with unlinked outputs kept at session level', () => {
  const linked: DataArtifact = {
    id: 'linked', title: '销售数据.csv', kind: 'csv', summary: '2 rows', createdByEventId: tool.id,
  };
  const unlinked: DataArtifact = { id: 'unlinked', title: '报告\n' + '中文👩‍💻'.repeat(30), kind: 'file', summary: '' };
  for (const columns of [60, 80, 160]) {
    const lines = buildChatLines({
      messages: [action, text('later', 'assistant', 'Next reply')], toolCalls: [tool],
      artifacts: [linked, unlinked, linked], columns, compactMode: true,
    });
    assert.equal(lines.filter((line) => line.key === 'output:linked').length, 1);
    const linkedIndex = lines.findIndex((line) => line.key === 'output:linked');
    assert.ok(linkedIndex < lines.findIndex((line) => line.key === 'm:action:after'));
    assert.ok(lines.findIndex((line) => line.key === 'output:unlinked') > lines.findIndex((line) => line.key === 'm:later:after'));
    for (const line of lines.filter((line) => line.key.startsWith('output:'))) {
      const frame = renderPlain(line.node, chatContentWidth(columns));
      assert.equal(frame.split('\n').length, 1);
      assert.ok(textWidth(frame) <= chatContentWidth(columns));
      assert.match(frame, / · \/outputs$/);
    }
  }
});
