#!/usr/bin/env node
/**
 * Verify the line-based chat viewport:
 * - text measurement is CJK-aware (wide chars count as two columns),
 * - row counts are exact (so they match what Ink renders),
 * - the scroll window is a deterministic, stable slice (no overlap/garble).
 *
 * Run after `npm run build` with: `npx tsx test-chat-viewport.ts`.
 */
import { buildChatLines, countChatLines, chatContentWidth } from './dist/ui/transcript-lines.js';
import { textWidth, wrapToWidth, truncateToWidth } from './dist/ui/text-width.js';
import {
  availableContentRows,
  estimateControlsRows,
  resolveMainPaneColumns,
} from './dist/ui/workspace-layout.js';
import { ENHANCED_INPUT_RESERVED_ROWS } from './dist/ui/components/EnhancedInputBox.js';
import { TextBuffer } from './dist/ui/components/text-buffer.js';
import {
  artifactDetailFromPreview,
  dataArtifactFromArtifactValue,
} from './dist/state/index.js';
import { artifactMarkdownContent } from './dist/ui/ArtifactCard.js';
import {
  createWheelScrollDecoder,
  wheelScrollDelta,
  wheelScrollDeltas,
  WHEEL_LINES_PER_TICK,
} from './dist/input/mouse-wheel.js';
import type { DisplayMessage, LiveToolCallRecord, MessageElement } from './dist/state/index.js';

let failures = 0;
function check(condition: boolean, message: string): void {
  if (condition) {
    console.log(`✓ ${message}`);
  } else {
    failures += 1;
    console.error(`✗ ${message}`);
  }
}

function eq<T>(actual: T, expected: T): boolean {
  return JSON.stringify(actual) === JSON.stringify(expected);
}

function visualLineText(line: { node: unknown }): string {
  return reactNodeText(line.node);
}

function unindentedLineText(line: { node: unknown }): string {
  return visualLineText(line).trimStart();
}

function reactNodeText(node: unknown): string {
  if (node === null || node === undefined || typeof node === 'boolean') {
    return '';
  }
  if (typeof node === 'string' || typeof node === 'number') {
    return String(node);
  }
  if (Array.isArray(node)) {
    return node.map(reactNodeText).join('');
  }
  if (typeof node === 'object') {
    const props = (node as {
      props?: {
        children?: unknown;
        segments?: Array<{ text?: unknown }>;
      };
    }).props;
    if (props?.segments) {
      return props.segments.map((segment) => String(segment.text ?? '')).join('');
    }
    return reactNodeText(props?.children);
  }
  return '';
}

function textMessage(id: string, content: string): DisplayMessage {
  return {
    id,
    role: 'assistant',
    timestamp: Date.now(),
    isStreaming: false,
    elements: [{ type: 'text', content, timestamp: Date.now() }],
  };
}

function elementMessage(id: string, elements: MessageElement[]): DisplayMessage {
  return {
    id,
    role: 'assistant',
    timestamp: Date.now(),
    isStreaming: false,
    elements,
  };
}

function textElement(content: string): MessageElement {
  return { type: 'text', content, timestamp: Date.now() };
}

function toolElement(toolCallId: string): MessageElement {
  return { type: 'tool_call', toolCallId, timestamp: Date.now() };
}

function toolRecord(id: string): LiveToolCallRecord {
  return { id, name: 'inspect_schema', status: 'success' };
}

// --- text width ---
check(textWidth('abc') === 3, 'ASCII width is 1 per char');
check(textWidth('中文') === 4, 'CJK width is 2 per char');
check(textWidth('a中b') === 4, 'mixed CJK/ASCII width adds up');
check(textWidth('e\u0301') === 1, 'combining mark is zero width');
check(textWidth('🌐') === 2, 'emoji width is 2');

// --- wrapping ---
check(eq(wrapToWidth('aaaa', 2), ['aa', 'aa']), 'wrap hard-breaks ASCII at width');
check(eq(wrapToWidth('中中中', 4), ['中中', '中']), 'wrap respects CJK display width');
check(eq(wrapToWidth('hello world', 5), ['hello', 'world']), 'wrap breaks at spaces with no empty rows');
check(wrapToWidth('', 10).length === 1, 'empty line still yields one row');
check(textWidth(truncateToWidth('中文测试内容很长', 5)) <= 5, 'truncate fits within width');

// --- input control characters ---
const inputBuffer = new TextBuffer('abc', 5, 10);
inputBuffer.insert('\x7f');
check(inputBuffer.text === 'abc', 'raw DEL is ignored by the text buffer instead of rendering as residue');

// --- layout helpers ---
check(chatContentWidth(120) === 115, 'content width is capped for wide terminals');
check(estimateControlsRows({ commandNotice: false, activeTab: 'chat' }) === 9, 'controls estimate reserves the fixed enhanced input height');
check(estimateControlsRows({ commandNotice: true, activeTab: 'chat' }) === 10, 'controls estimate includes command notice');
check(estimateControlsRows({ commandNotice: false, activeTab: 'chat', inputBoxRows: 12 }) === 12, 'controls estimate follows expanded input height');
check(estimateControlsRows({ commandNotice: true, activeTab: 'chat', inputBoxRows: 12 }) === 13, 'controls estimate combines notice and expanded input height');
check(ENHANCED_INPUT_RESERVED_ROWS === 9, 'reserved input height covers the bordered three-row input viewport');
check(
  estimateControlsRows({
    commandNotice: false,
    activeTab: 'chat',
    inputBoxRows: ENHANCED_INPUT_RESERVED_ROWS,
  }) === ENHANCED_INPUT_RESERVED_ROWS,
  'controls estimate follows the reserved input height',
);
check(estimateControlsRows({ commandNotice: false, activeTab: 'chat', homeScreen: true }) === 0, 'home screen keeps the composer inside the main content area');
check(availableContentRows(40, 5) === 35, 'content viewport subtracts measured controls rows');
check(availableContentRows(40, 9) === 31, 'content viewport shrinks when measured input grows');
check(availableContentRows(8, 12) === 0, 'content viewport can collapse instead of overlapping controls');

// --- output artifact markdown previews ---
const markdownFileArtifact = dataArtifactFromArtifactValue({
  id: 'artifact-md-file',
  type: 'file',
  name: 'report.md',
  mime_type: 'text/markdown',
  preview_json: { path: 'report.md' },
});
const markdownFileDetail = artifactDetailFromPreview(
  markdownFileArtifact,
  '# Report\n\nDone.',
);
check(
  eq(markdownFileDetail, {
    type: 'file',
    path: 'report.md',
    content: '# Report\n\nDone.',
  }),
  'raw markdown file previews become file detail content',
);
check(
  artifactMarkdownContent({
    ...markdownFileArtifact,
    detail: markdownFileDetail,
  }) === '# Report\n\nDone.',
  'markdown file detail exposes content for Outputs markdown rendering',
);
const markdownReportArtifact = dataArtifactFromArtifactValue({
  id: 'artifact-md-report',
  type: 'markdown',
  name: 'summary.md',
  preview_json: { markdown: '# Summary\n\nComplete.' },
});
check(
  markdownReportArtifact.detail?.type === 'report'
    && artifactMarkdownContent(markdownReportArtifact) === '# Summary\n\nComplete.',
  'markdown artifact preview_json markdown field renders as report markdown',
);
check(
  eq(resolveMainPaneColumns({ columns: 120 }), {
    chatColumns: 120,
    outputsColumns: 0,
    outputsVisible: false,
  }),
  'outputs sidebar stays hidden at the opencode-style breakpoint',
);
check(
  eq(resolveMainPaneColumns({ columns: 121 }), {
    chatColumns: 79,
    outputsColumns: 42,
    outputsVisible: true,
  }),
  'outputs sidebar appears above the opencode-style breakpoint with fixed width',
);
check(
  eq(resolveMainPaneColumns({ columns: 220 }), {
    chatColumns: 178,
    outputsColumns: 42,
    outputsVisible: true,
  }),
  'outputs sidebar keeps a fixed width on very wide terminals',
);
check(
  eq(resolveMainPaneColumns({ columns: 200 }), {
    chatColumns: 158,
    outputsColumns: 42,
    outputsVisible: true,
  }),
  'outputs sidebar stays visible on wide terminals even before outputs exist',
);

// --- mouse wheel parsing ---
const ESC = '\u001B';
const wheelBurst = `${ESC}[<64;10;2M${ESC}[<65;10;3M${ESC}[<64;10;4M`;
check(
  eq(wheelScrollDeltas(wheelBurst), [WHEEL_LINES_PER_TICK, -WHEEL_LINES_PER_TICK, WHEEL_LINES_PER_TICK]),
  'wheel parser returns one delta per SGR wheel event',
);
check(
  wheelScrollDelta(wheelBurst) === WHEEL_LINES_PER_TICK,
  'legacy wheel delta helper still returns the summed delta',
);
const wheelDecoder = createWheelScrollDecoder();
check(wheelDecoder.push(`${ESC}[<64;10`).length === 0, 'wheel decoder buffers split SGR sequences');
check(
  eq(wheelDecoder.push(`;2M${ESC}[<65;10;3M`), [WHEEL_LINES_PER_TICK, -WHEEL_LINES_PER_TICK]),
  'wheel decoder reassembles split SGR sequences before emitting deltas',
);

// --- startup banner ---
const startupLines = buildChatLines({
  messages: [],
  artifacts: [],
  columns: 80,
  startup: {
    threadId: '12345678-abcd',
    connectionStatus: 'connected',
    runStatus: 'idle',
    modelName: 'test-model',
    directory: '/tmp/datafoundry',
  },
});
const startupTexts = startupLines.map(visualLineText);
check(startupLines[0]?.key === 'startup:border:top', 'startup transcript begins with the banner border');
check(startupTexts.some((line) => line.includes('DataFoundry')), 'startup banner includes the DataFoundry wordmark');
check(
  startupTexts.every((line) => textWidth(line) <= chatContentWidth(80)),
  'startup banner rows fit within chat content width',
);

// --- exact line counts ---
const columns = 120;
check(
  countChatLines({ messages: [textMessage('m1', 'hello')], artifacts: [], columns }) === 5,
  'short message = top padding + header + 1 line + trailing blank (5 rows)',
);

// bodyWidth = 115 - 2 = 113 -> 56 CJK chars per row -> 60 chars wrap to 2 rows.
check(
  countChatLines({ messages: [textMessage('m2', '中'.repeat(60))], artifacts: [], columns }) === 6,
  '60 CJK chars wrap to 2 rows (top padding + header + 2 + blank = 6)',
);

// The previous length-based estimate would have counted this as a single row,
// which is exactly the drift that garbled scrolling. Guard against regressing.
check(
  countChatLines({ messages: [textMessage('m3', '中'.repeat(60))], artifacts: [], columns }) >
    countChatLines({ messages: [textMessage('m3b', 'x'.repeat(60))], artifacts: [], columns }),
  'CJK content is taller than equal-length ASCII content',
);

// --- markdown tables ---
const tableMarkdown = [
  '| Name | Amount | Note |',
  '| --- | ---: | :---: |',
  '| Alpha | 123 | ok |',
  '| Very long item name that should be clipped | 456789 | 中文 |',
].join('\n');
const tableBodyWidth = chatContentWidth(40) - 2;
const tableRows = buildChatLines({
  messages: [textMessage('tbl', tableMarkdown)],
  artifacts: [],
  columns: 40,
})
  .filter((line) => line.key.startsWith('m:tbl:e0:k0:'))
  .map(unindentedLineText);

check(tableRows[0]?.startsWith('┌') === true && tableRows[0]?.endsWith('┐') === true, 'markdown table has a closed top border');
check(tableRows[1]?.startsWith('│') === true && tableRows[1]?.endsWith('│') === true, 'markdown table header has side borders');
check(tableRows[2]?.startsWith('├') === true && tableRows[2]?.endsWith('┤') === true, 'markdown table has a closed header separator');
check(tableRows.at(-1)?.startsWith('└') === true && tableRows.at(-1)?.endsWith('┘') === true, 'markdown table has a closed bottom border');
check(tableRows.every((row) => textWidth(row) <= tableBodyWidth), 'markdown table rows fit within chat body width');

const tallTableMarkdown = [
  '| A | B |',
  '| --- | --- |',
  ...Array.from({ length: 14 }, (_, index) => `| row ${index + 1} | ${index + 1} |`),
].join('\n');
const tallTableRows = buildChatLines({
  messages: [textMessage('talltbl', tallTableMarkdown)],
  artifacts: [],
  columns,
})
  .filter((line) => line.key.startsWith('m:talltbl:e0:k0:'))
  .map(unindentedLineText);
check(
  tallTableRows.some((row) => row.startsWith('│') && row.endsWith('│') && row.includes('more rows')),
  'markdown table overflow notice stays inside table borders',
);

const wideTableMarkdown = [
  '| c1 | c2 | c3 | c4 | c5 | c6 |',
  '| --- | --- | --- | --- | --- | --- |',
  '| a | b | c | d | e | f |',
].join('\n');
const wideTableRows = buildChatLines({
  messages: [textMessage('widetbl', wideTableMarkdown)],
  artifacts: [],
  columns: 40,
})
  .filter((line) => line.key.startsWith('m:widetbl:e0:k0:'))
  .map(unindentedLineText);
check(
  wideTableRows.some((row) => row.startsWith('│') && row.endsWith('│') && row.includes('more column')),
  'markdown table hidden-column notice stays inside table borders',
);

// --- block spacing: exactly one blank row between text and tool calls ---
const toolCalls: LiveToolCallRecord[] = [toolRecord('tc1'), toolRecord('tc2')];

// top padding + header + "a" + 1 blank + tool line + trailing blank = 7 rows.
const paddedTextThenTool = elementMessage('s1', [textElement('a\n\n\n'), toolElement('tc1')]);
check(
  countChatLines({ messages: [paddedTextThenTool], artifacts: [], toolCalls, columns }) === 7,
  'text + tool = top padding + header + text + 1 blank + tool + trailing blank (7 rows)',
);

// Trailing newlines the model emits before a tool must not inflate the gap.
const tightTextThenTool = elementMessage('s2', [textElement('a'), toolElement('tc1')]);
check(
  countChatLines({ messages: [paddedTextThenTool], artifacts: [], toolCalls, columns }) ===
    countChatLines({ messages: [tightTextThenTool], artifacts: [], toolCalls, columns }),
  'trailing newlines before a tool add no extra rows',
);

// text -> tool -> text: a single blank between each of the three blocks.
// top padding + header + "a" + blank + tool + blank + "b" + trailing blank = 9 rows.
const textToolText = elementMessage('s3', [textElement('a'), toolElement('tc1'), textElement('b')]);
check(
  countChatLines({ messages: [textToolText], artifacts: [], toolCalls, columns }) === 9,
  'text/tool/text join with one blank between each block (9 rows)',
);

// A whitespace-only text element (e.g. the lone "\n\n" before a tool) is skipped
// entirely, so it adds neither a block nor a separator.
const emptyTextBetween = elementMessage('s4', [textElement('a'), textElement('\n\n\n'), toolElement('tc1')]);
check(
  countChatLines({ messages: [emptyTextBetween], artifacts: [], toolCalls, columns }) ===
    countChatLines({ messages: [tightTextThenTool], artifacts: [], toolCalls, columns }),
  'whitespace-only text elements are skipped (no phantom block or blank)',
);

// Two adjacent tool calls keep a single blank between them.
// top padding + header + "a" + blank + tool + blank + tool + trailing blank = 9 rows.
const textToolTool = elementMessage('s5', [textElement('a'), toolElement('tc1'), toolElement('tc2')]);
check(
  countChatLines({ messages: [textToolTool], artifacts: [], toolCalls, columns }) === 9,
  'adjacent tool calls keep exactly one blank between them (9 rows)',
);

// --- deterministic, stable slicing ---
const manyMessages: DisplayMessage[] = Array.from({ length: 8 }, (_, index) =>
  textMessage(`mm${index}`, `line ${index}`),
);
const lines = buildChatLines({ messages: manyMessages, artifacts: [], columns });
const total = lines.length;
const viewport = 5;

function windowKeys(scrollback: number): string[] {
  const maxScroll = Math.max(0, total - viewport);
  const safe = Math.max(0, Math.min(scrollback, maxScroll));
  const top = Math.max(0, total - viewport - safe);
  return lines.slice(top, top + viewport).map((line) => line.key);
}

check(windowKeys(0).length === viewport, 'bottom window fills the viewport');
check(eq(windowKeys(0), lines.slice(total - viewport).map((line) => line.key)), 'scrollback 0 shows the newest rows');
check(
  eq(windowKeys(3), lines.slice(total - viewport - 3, total - 3).map((line) => line.key)),
  'scrollback 3 is an exact shifted slice',
);
check(new Set(lines.map((line) => line.key)).size === total, 'all line keys are unique (no duplicate rows)');

console.log();
if (failures > 0) {
  console.error(`${failures} check(s) failed.`);
  process.exit(1);
}
console.log('✓ all chat viewport checks passed');
