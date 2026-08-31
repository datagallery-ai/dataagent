import React, { useEffect, useState } from 'react';
import { Box, Text } from 'ink';
import type {
  ConnectionStatus,
  DataArtifact,
  DisplayMessage,
  LiveRunStatus,
  LiveToolCallRecord,
} from '../state/index.js';
import { InlineToolCall } from './InlineToolCall.js';
import {
  graphemeWidth,
  graphemes,
  textWidth,
  truncateToWidth,
  wrapStyledRuns,
  wrapToWidth,
  type StyledRun,
  type StyledSegment,
} from './text-width.js';
import {
  boundCodeLines,
  parseInlineRuns,
  parseMarkdownLines,
  type TableAlignment,
} from './markdown.js';
import { inkColors } from './theme.js';

/**
 * A single terminal row. The chat viewport renders these one-per-row and slices
 * them to the visible window, so every node here MUST occupy exactly one row and
 * never exceed the content width (otherwise Ink re-wraps and the slice math, and
 * therefore scrolling, drifts). Strings are pre-wrapped/truncated to guarantee
 * this; keys are content-stable so streaming appends don't re-key earlier rows.
 */
export interface VisualLine {
  key: string;
  node: React.ReactNode;
}

export interface StartupInfo {
  threadId: string | undefined;
  connectionStatus: ConnectionStatus;
  runStatus: LiveRunStatus;
  modelName: string;
  directory: string;
  datasourceId?: string | undefined;
}

export interface BuildChatLinesInput {
  messages: DisplayMessage[];
  artifacts: DataArtifact[];
  toolCalls?: LiveToolCallRecord[] | undefined;
  totalMessageCount?: number | undefined;
  maxMessageContentLength?: number | undefined;
  columns?: number | undefined;
  startup?: StartupInfo | undefined;
  compactMode?: boolean | undefined;
  thoughtExpanded?: boolean | undefined;
}

type ToolCallElement = Extract<DisplayMessage["elements"][number], { type: "tool_call" }>;
type ReasoningElement = Extract<DisplayMessage["elements"][number], { type: "reasoning" }>;

const INDENT = '  ';
const INDENT_WIDTH = 2;
const TOOL_DETAIL_INDENT = '  ';
const USER_MESSAGE_BORDER = '┃ ';
const USER_MESSAGE_BORDER_WIDTH = textWidth(USER_MESSAGE_BORDER);
const MAX_ELEMENT_LINES = 1000;
const MAX_REASONING_LINES = 120;
const MAX_TOOL_PAYLOAD_LINES = 28;
const MAX_TABLE_ROWS = 12;
const MIN_TABLE_CELL_WIDTH = 3;
const TOOL_DETAIL_INDENT_WIDTH = textWidth(TOOL_DETAIL_INDENT);
const STARTUP_BANNER_ART = [
  ' ____        _        _____                     _            ',
  '|  _ \\  __ _| |_ __ _|  ___|__  _   _ _ __   __| |_ __ _   _ ',
  '| | | |/ _` | __/ _` | |_ / _ \\| | | | `_ \\ / _` | `__| | | |',
  '| |_| | (_| | || (_| |  _| (_) | |_| | | | | (_| | |  | |_| |',
  '|____/ \\__,_|\\__\\__,_|_|  \\___/ \\__,_|_| |_|\\__,_|_|   \\__, |',
  '                                                        |___/ ',
];
const STARTUP_BANNER_ART_WIDTH = Math.max(
  ...STARTUP_BANNER_ART.map((line) => textWidth(line)),
);
const STARTUP_BANNER_MAX_WIDTH = 72;

/**
 * Total display-column budget for a single chat row.
 *
 * 优化说明：
 * - 限制最大宽度为 115 格，避免宽屏时文本过长难以阅读
 * - 窄屏时使用全宽（减去 padding），保持响应式
 * - 宽屏时自然留白，提升阅读舒适度
 */
export function chatContentWidth(columns: number): number {
  const minWidth = 20;
  const maxWidth = 115; // 限制最大宽度，优化宽屏阅读体验
  const padding = 4;

  const availableWidth = columns - padding;
  return Math.max(minWidth, Math.min(maxWidth, availableWidth));
}

/**
 * Build the entire chat transcript as a flat list of single-row lines. Pure and
 * deterministic: the same inputs always produce the same lines, which is what
 * lets the viewport and the scroll bookkeeping agree on an exact row count.
 */
export function buildChatLines(input: BuildChatLinesInput): VisualLine[] {
  const columns = input.columns ?? 100;
  const contentWidth = chatContentWidth(columns);
  const bodyWidth = Math.max(1, contentWidth - INDENT_WIDTH);
  const toolCalls = input.toolCalls ?? [];
  const messageCount = input.totalMessageCount ?? input.messages.length;
  const compactMode = input.compactMode ?? false;
  const thoughtExpanded = input.thoughtExpanded ?? false;

  const lines: VisualLine[] = [];
  const push = (key: string, node: React.ReactNode) => {
    lines.push({ key, node });
  };

  if (input.startup) {
    pushStartupLines(input.startup, contentWidth, push);
  }

  const hasMessages = input.messages.length > 0;
  if (!hasMessages && messageCount === 0) {
    if (!input.startup) {
      push('empty:0', <Text key="empty:0" dimColor>No messages yet. Start typing to begin...</Text>);
      push('empty:1', <Text key="empty:1" dimColor>Type your question and press Enter to send.</Text>);
    }
    return lines;
  }

  // Keep the first message clear of the viewport edge when it replaces the
  // startup banner.
  if (hasMessages) {
    push('spacer:top:0', blankNode('spacer:top:0'));
    push('spacer:top:1', blankNode('spacer:top:1'));
  }

  for (const message of input.messages) {
    pushMessageLines(message, toolCalls, bodyWidth, push, compactMode, thoughtExpanded);
    push(`m:${message.id}:after`, blankNode(`m:${message.id}:after`));
  }

  // Restored sessions can report a count without hydrated messages yet.
  if (!hasMessages && messageCount > 0) {
    push('spacer:0', blankNode('spacer:0'));
  }

  return lines;
}

/** Convenience for callers that only need the row count (e.g. scroll clamps). */
export function countChatLines(input: BuildChatLinesInput): number {
  return buildChatLines(input).length;
}

/**
 * Collapse the blank-line padding models tend to emit around tool calls so
 * blocks join with a single separator row (DataDock-style spacing). Leading and
 * trailing blank lines are dropped, and any internal run of blanks collapses to
 * one. A whitespace-only element returns '' so the caller can skip it entirely
 * (e.g. the lone "\n\n" a model streams right before invoking a tool).
 */
function normalizeBlankLines(content: string): string {
  const lines = content.split('\n');
  while (lines.length > 0 && (lines[0] ?? '').trim() === '') {
    lines.shift();
  }
  while (lines.length > 0 && (lines[lines.length - 1] ?? '').trim() === '') {
    lines.pop();
  }

  const collapsed: string[] = [];
  let previousBlank = false;
  for (const line of lines) {
    const isBlank = line.trim() === '';
    if (isBlank && previousBlank) {
      continue;
    }
    collapsed.push(line);
    previousBlank = isBlank;
  }
  return collapsed.join('\n');
}

function pushMessageLines(
  message: DisplayMessage,
  toolCalls: LiveToolCallRecord[],
  bodyWidth: number,
  push: (key: string, node: React.ReactNode) => void,
  compactMode: boolean,
  thoughtExpanded: boolean,
): void {
  const isUser = message.role === 'user';
  const headerKey = `m:${message.id}:h`;
  const messageBodyWidth = isUser
    ? Math.max(1, bodyWidth - USER_MESSAGE_BORDER_WIDTH)
    : bodyWidth;

  // pushLine wraps content with visual decorations to maintain alignment:
  // - User messages: blue border (┃ ) is added to the left of all content
  // - Agent messages: equivalent whitespace padding is added to align with User messages
  // This ensures both message types start at the same column position.
  const pushLine = (key: string, node: React.ReactNode) => {
    if (isUser) {
      push(
        key,
        <Box key={`box-${key}`}>
          <Text color={inkColors.accent}>{USER_MESSAGE_BORDER}</Text>
          {node}
        </Box>,
      );
    } else {
      // Agent messages need left padding to align with User messages that have border
      push(
        key,
        <Box key={`box-${key}`}>
          <Text>{' '.repeat(USER_MESSAGE_BORDER_WIDTH)}</Text>
          {node}
        </Box>,
      );
    }
  };

  pushLine(headerKey, <MessageHeader key={headerKey} message={message} />);

  if (message.elements.length === 0 && message.isStreaming) {
    const key = `m:${message.id}:thinking`;
    pushLine(key, <ThinkingLine key={key} />);
    return;
  }

  let blocksEmitted = 0;
  let emittedLines = 0;

  // Exactly one blank row between consecutive blocks (text/tool); none before
  // the first so the header stays glued to its content. Mirrors DataDock's
  // uniform marginTop={1} spacing within the flat single-row line model.
  const separate = (key: string): void => {
    if (blocksEmitted > 0) {
      pushLine(key, blankNode(key));
    }
  };
  const pushContentLine = (key: string, node: React.ReactNode): void => {
    if (emittedLines >= MAX_ELEMENT_LINES) {
      return;
    }
    emittedLines += 1;
    pushLine(key, node);
  };

  message.elements.forEach((element, elementIndex) => {
    const keyBase = `m:${message.id}:e${elementIndex}`;

    if (element.type === 'text') {
      const normalized = normalizeBlankLines(element.content);
      if (normalized === '') {
        return;
      }
      separate(`${keyBase}:gap`);
      blocksEmitted += 1;
      pushMarkdownLines(normalized, messageBodyWidth, keyBase, pushContentLine);
      return;
    }

    if (element.type === 'reasoning') {
      const normalized = normalizeBlankLines(element.content);
      if (compactMode) {
        if (!element.isStreaming) {
          return;
        }
        separate(`${keyBase}:gap`);
        blocksEmitted += 1;
        pushContentLine(`${keyBase}:thinking`, <ThinkingLine key={`${keyBase}:thinking`} />);
        return;
      }

      if (normalized === '' && !element.isStreaming) {
        return;
      }

      separate(`${keyBase}:gap`);
      blocksEmitted += 1;
      pushReasoningLines(
        element,
        messageBodyWidth,
        keyBase,
        thoughtExpanded,
        pushContentLine,
      );
      return;
    }

    // tool_call element
    const toolCall = resolveToolCallForElement(element, toolCalls);
    if (!toolCall) {
      return;
    }
    const key = `${keyBase}:tool`;
    separate(`${key}:gap`);
    blocksEmitted += 1;
    pushToolCallLines(toolCall, messageBodyWidth, key, compactMode, pushContentLine);
  });

  if (message.isStreaming && message.elements.length > 0) {
    const key = `m:${message.id}:cursor`;
    pushLine(key, <Text key={key} dimColor>{`${INDENT}▊`}</Text>);
  }
}

function resolveToolCallForElement(
  element: ToolCallElement,
  toolCalls: LiveToolCallRecord[],
): LiveToolCallRecord | undefined {
  if (element.runId) {
    return toolCalls.find(
      (candidate) =>
        candidate.id === element.toolCallId &&
        candidate.runId === element.runId,
    ) ?? element.toolCall;
  }

  if (element.toolCall) {
    return element.toolCall;
  }

  return toolCalls.find((candidate) => candidate.id === element.toolCallId);
}

function pushReasoningLines(
  element: ReasoningElement,
  bodyWidth: number,
  keyBase: string,
  thoughtExpanded: boolean,
  push: (key: string, node: React.ReactNode) => void,
): void {
  const normalized = normalizeBlankLines(element.content);
  const firstLine = normalized
    .split('\n')
    .map((line) => line.trim())
    .find((line) => line.length > 0);
  const header = thoughtExpanded
    ? element.isStreaming ? 'Thinking...' : 'Thinking'
    : firstLine
      ? `Thinking: ${firstLine}`
      : element.isStreaming ? 'Thinking...' : 'Thinking';

  push(
    `${keyBase}:reasoning:h`,
    <StyledLine
      key={`${keyBase}:reasoning:h`}
      segments={[{ text: truncateToWidth(header, bodyWidth), dimColor: true }]}
    />,
  );

  if (!thoughtExpanded || normalized === '') {
    return;
  }

  const rows = textRows(normalized, bodyWidth);
  const visibleRows = rows.slice(0, MAX_REASONING_LINES);
  visibleRows.forEach((row, rowIndex) => {
    const key = `${keyBase}:reasoning:r${rowIndex}`;
    push(
      key,
      <StyledLine key={key} segments={[{ text: row, dimColor: true }]} />,
    );
  });

  if (rows.length > visibleRows.length) {
    const hidden = rows.length - visibleRows.length;
    const key = `${keyBase}:reasoning:more`;
    push(
      key,
      <StyledLine
        key={key}
        segments={[{ text: `... ${hidden} more thinking lines hidden ...`, dimColor: true }]}
      />,
    );
  }
}

function pushToolCallLines(
  toolCall: LiveToolCallRecord,
  bodyWidth: number,
  keyBase: string,
  compactMode: boolean,
  push: (key: string, node: React.ReactNode) => void,
): void {
  const blockWidth = toolBlockBackgroundWidth(bodyWidth);

  push(
    keyBase,
    <Box key={keyBase} width={bodyWidth}>
      <Text>{TOOL_DETAIL_INDENT}</Text>
      <Box width={blockWidth}>
        <InlineToolCall toolCall={toolCall} showName maxWidth={blockWidth} />
      </Box>
    </Box>,
  );

  if (compactMode) {
    return;
  }

  const parameterRows = toolParameterRows(toolCall);
  if (parameterRows.length > 0) {
    pushPayloadBlock('Parameters', parameterRows, bodyWidth, `${keyBase}:args`, push);
  }

  const resultRows = toolResultRows(toolCall);
  if (resultRows.length > 0) {
    pushPayloadBlock('Result', resultRows, bodyWidth, `${keyBase}:result`, push);
  }
}

function pushPayloadBlock(
  label: string,
  rows: string[],
  bodyWidth: number,
  keyBase: string,
  push: (key: string, node: React.ReactNode) => void,
): void {
  const blockWidth = toolBlockBackgroundWidth(bodyWidth);
  const key = `${keyBase}:label`;
  push(
    key,
    <ToolBlockLine
      key={key}
      width={blockWidth}
      segments={[
        { text: label, color: inkColors.accent, bold: true },
      ]}
    />,
  );

  const detailIndent = TOOL_DETAIL_INDENT;
  const detailWidth = Math.max(1, blockWidth - textWidth(detailIndent));
  const wrappedRows = rows.flatMap((row) => textRows(row, detailWidth));
  const visibleRows = wrappedRows.slice(0, MAX_TOOL_PAYLOAD_LINES);
  visibleRows.forEach((row, rowIndex) => {
    const rowKey = `${keyBase}:r${rowIndex}`;
    push(
      rowKey,
      <ToolBlockLine
        key={rowKey}
        width={blockWidth}
        segments={detailRowSegments(detailIndent, row)}
      />,
    );
  });

  if (wrappedRows.length > visibleRows.length) {
    const hidden = wrappedRows.length - visibleRows.length;
    const moreKey = `${keyBase}:more`;
    push(
      moreKey,
      <ToolBlockLine
        key={moreKey}
        width={blockWidth}
        segments={[
          {
            text: `${detailIndent}... ${hidden} more lines hidden ...`,
            color: inkColors.muted,
          },
        ]}
      />,
    );
  }
}

function toolBlockBackgroundWidth(bodyWidth: number): number {
  return Math.max(1, bodyWidth - TOOL_DETAIL_INDENT_WIDTH);
}

function detailRowSegments(indent: string, row: string): StyledSegment[] {
  const separatorIndex = row.indexOf(': ');
  if (separatorIndex <= 0) {
    return [
      { text: indent, color: inkColors.muted },
      { text: row, color: inkColors.text },
    ];
  }

  const field = row.slice(0, separatorIndex + 1);
  const value = row.slice(separatorIndex + 2);
  const fieldName = row.slice(0, separatorIndex).toLowerCase();
  const valueColor = fieldName === 'error' ? inkColors.error : inkColors.text;

  return [
    { text: indent, color: inkColors.muted },
    { text: field, color: inkColors.muted, bold: true },
    { text: ' ', color: inkColors.muted },
    { text: value, color: valueColor },
  ];
}

function toolParameterRows(toolCall: LiveToolCallRecord): string[] {
  if (toolCall.args === undefined) {
    return [];
  }

  const value = parsePayloadValue(toolCall.args);
  if (!isRecord(value)) {
    return compactTextRows('input', String(value));
  }

  const rows: string[] = [];
  const sql = stringField(value, 'sql') ?? stringField(value, 'query');
  if (sql) {
    rows.push(`sql: ${compactWhitespace(sql)}`);
  }

  const datasource =
    stringField(value, 'datasource_id') ??
    stringField(value, 'datasourceId') ??
    stringField(value, 'source');
  if (datasource) {
    rows.push(`datasource: ${datasource}`);
  }

  const tables = arrayField(value, 'table_names') ?? arrayField(value, 'tables');
  if (tables) {
    rows.push(`tables: ${tables.map(formatValueSummary).join(', ')}`);
  }

  appendRecordSummaryRows(rows, value, new Set([
    'sql',
    'query',
    'datasource_id',
    'datasourceId',
    'source',
    'table_names',
    'tables',
  ]));

  return rows.length > 0 ? rows : compactObjectRows(value);
}

function toolResultRows(toolCall: LiveToolCallRecord): string[] {
  const rawResult = toolCall.result ?? toolCall.resultPreview;
  if (rawResult === undefined) {
    return [];
  }

  const value = parsePayloadValue(rawResult);
  if (!isRecord(value)) {
    return compactTextRows('output', String(value));
  }

  const rows: string[] = [];
  const error =
    stringField(value, 'error') ??
    stringField(value, 'message');
  const isError = value.isError === true || value.error === true;
  const hasExplicitErrorField = value.error !== undefined && value.error !== false;
  if ((isError || hasExplicitErrorField || toolCall.status === 'failed') && error) {
    rows.push(`error: ${compactWhitespace(error)}`);
  }

  const rowCount =
    numberField(value, 'row_count') ??
    numberField(value, 'rowCount') ??
    numberField(value, 'rows_scanned');
  if (rowCount !== undefined) {
    rows.push(`rows: ${rowCount.toLocaleString('en-US')}`);
  }

  const elapsedMs =
    numberField(value, 'elapsed_ms') ??
    numberField(value, 'elapsedMs') ??
    numberField(value, 'duration_ms');
  if (elapsedMs !== undefined) {
    rows.push(`elapsed: ${formatDurationMs(elapsedMs)}`);
  }

  const tables = arrayField(value, 'tables');
  if (tables) {
    rows.push(...schemaTableRows(tables));
  }

  appendRecordSummaryRows(rows, value, new Set([
    'error',
    ...(error ? ['message'] : []),
    'isError',
    'row_count',
    'rowCount',
    'rows_scanned',
    'elapsed_ms',
    'elapsedMs',
    'duration_ms',
    'tables',
  ]));

  return rows.length > 0 ? rows : compactObjectRows(value);
}

function parsePayloadValue(payload: unknown): unknown {
  if (typeof payload !== 'string') {
    return payload;
  }

  const trimmed = payload.trim();
  if (!trimmed) {
    return '';
  }
  if (!trimmed.startsWith('{') && !trimmed.startsWith('[')) {
    return payload;
  }

  try {
    return JSON.parse(trimmed);
  } catch {
    return payload;
  }
}

function compactTextRows(label: string, text: string): string[] {
  return text
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
    .slice(0, 6)
    .map((line, index) => `${index === 0 ? label : 'line'}: ${compactWhitespace(line)}`);
}

function compactObjectRows(record: Record<string, unknown>): string[] {
  const rows: string[] = [];
  appendRecordSummaryRows(rows, record, new Set());
  return rows.length > 0 ? rows : ['value: {}'];
}

function appendRecordSummaryRows(
  rows: string[],
  record: Record<string, unknown>,
  excludedKeys: Set<string>,
): void {
  for (const [key, value] of Object.entries(record)) {
    if (rows.length >= 8) {
      break;
    }
    if (excludedKeys.has(key) || value === undefined || value === null) {
      continue;
    }
    if (isRecord(value) && Object.keys(value).length === 0) {
      continue;
    }
    if (Array.isArray(value) && value.length === 0) {
      continue;
    }
    rows.push(`${humanizeKey(key)}: ${formatValueSummary(value)}`);
  }
}

function schemaTableRows(tables: unknown[]): string[] {
  const rows: string[] = [];
  const visibleTables = tables.slice(0, 5);
  for (const table of visibleTables) {
    if (!isRecord(table)) {
      rows.push(`table: ${formatValueSummary(table)}`);
      continue;
    }
    const name = stringField(table, 'name') ?? stringField(table, 'table_name') ?? 'table';
    const columns = arrayField(table, 'columns');
    if (!columns) {
      rows.push(`table: ${name}`);
      continue;
    }
    const columnNames = columns
      .slice(0, 4)
      .map((column) => isRecord(column)
        ? stringField(column, 'name') ?? stringField(column, 'column_name') ?? ''
        : String(column))
      .filter(Boolean);
    const suffix = columnNames.length > 0 ? ` (${columnNames.join(', ')})` : '';
    rows.push(`table: ${name} · ${columns.length} columns${suffix}`);
  }
  if (tables.length > visibleTables.length) {
    rows.push(`more: ${tables.length - visibleTables.length} tables hidden`);
  }
  return rows;
}

function formatValueSummary(value: unknown): string {
  if (value === undefined) return 'undefined';
  if (value === null) return 'null';
  if (typeof value === 'string') return compactWhitespace(value);
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (Array.isArray(value)) {
    if (value.length === 0) return '[]';
    const preview = value.slice(0, 4).map(formatValueSummary).join(', ');
    return value.length > 4 ? `[${preview}, ... +${value.length - 4}]` : `[${preview}]`;
  }
  if (isRecord(value)) {
    const entries = Object.entries(value).filter(([, item]) => item !== undefined && item !== null);
    if (entries.length === 0) return '{}';
    const preview = entries
      .slice(0, 3)
      .map(([key, item]) => `${humanizeKey(key)}=${formatValueSummary(item)}`)
      .join(', ');
    return entries.length > 3 ? `{${preview}, ...}` : `{${preview}}`;
  }
  return String(value);
}

function compactWhitespace(value: string): string {
  return value.replace(/\s+/g, ' ').trim();
}

function humanizeKey(key: string): string {
  return key
    .replace(/_/g, ' ')
    .replace(/([a-z])([A-Z])/g, '$1 $2')
    .toLowerCase();
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function stringField(record: Record<string, unknown>, key: string): string | undefined {
  const value = record[key];
  return typeof value === 'string' && value.trim() ? value : undefined;
}

function numberField(record: Record<string, unknown>, key: string): number | undefined {
  const value = record[key];
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined;
}

function arrayField(record: Record<string, unknown>, key: string): unknown[] | undefined {
  const value = record[key];
  return Array.isArray(value) ? value : undefined;
}

function formatDurationMs(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`;
  const minutes = Math.floor(ms / 60000);
  const seconds = Math.floor((ms % 60000) / 1000);
  return `${minutes}m ${seconds}s`;
}

function textRows(content: string, width: number): string[] {
  const rows: string[] = [];
  for (const rawLine of content.split('\n')) {
    rows.push(...wrapToWidth(rawLine.length === 0 ? ' ' : rawLine, width));
  }
  return rows.length > 0 ? rows : [' '];
}

/**
 * Render one text element as Markdown into single-row lines. Block structure
 * (headings, lists, quotes, fenced code, tables) is parsed once, then each
 * parsed line is emitted as one or more pre-wrapped rows so the viewport slice
 * math stays exact. Inline `code` and **bold** are styled; markup never leaks as
 * literal text. Keys are content-stable (line index + row index) so streaming
 * appends don't re-key earlier rows.
 */
function pushMarkdownLines(
  content: string,
  bodyWidth: number,
  keyBase: string,
  push: (key: string, node: React.ReactNode) => void,
): void {
  const parsed = boundCodeLines(parseMarkdownLines(content));
  let codeLang = '';

  parsed.forEach((line, lineIndex) => {
    const lineKey = `${keyBase}:k${lineIndex}`;
    switch (line.kind) {
      case 'blank':
        push(lineKey, blankNode(lineKey));
        return;
      case 'codeFence':
        codeLang = line.open ? line.lang : '';
        pushFenceLine(line.open, line.lang, bodyWidth, lineKey, push);
        return;
      case 'code':
        pushCodeLines(line.text, codeLang, bodyWidth, lineKey, push);
        return;
      case 'heading':
        pushStyledRows(parseInlineRuns(line.text), bodyWidth, lineKey, push, {
          bold: true,
          blockColor: inkColors.accent,
        });
        return;
      case 'paragraph':
        pushStyledRows(parseInlineRuns(line.text), bodyWidth, lineKey, push, {});
        return;
      case 'bullet':
        pushStyledRows(parseInlineRuns(line.text), bodyWidth, lineKey, push, {
          firstPrefix: { text: '- ', color: inkColors.muted },
          contPrefix: { text: '  ' },
        });
        return;
      case 'ordered': {
        const marker = `${line.index} `;
        pushStyledRows(parseInlineRuns(line.text), bodyWidth, lineKey, push, {
          firstPrefix: { text: marker, color: inkColors.muted },
          contPrefix: { text: ' '.repeat(textWidth(marker)) },
        });
        return;
      }
      case 'quote':
        pushStyledRows(parseInlineRuns(line.text), bodyWidth, lineKey, push, {
          firstPrefix: { text: '│ ', color: inkColors.muted },
          contPrefix: { text: '│ ', color: inkColors.muted },
          blockColor: inkColors.muted,
        });
        return;
      case 'table':
        pushTableLines(line.rows, line.alignments, bodyWidth, lineKey, push);
        return;
    }
  });
}

interface BlockStyle {
  bold?: boolean | undefined;
  blockColor?: string | undefined;
  firstPrefix?: StyledSegment | undefined;
  contPrefix?: StyledSegment | undefined;
}

function pushStyledRows(
  runs: StyledRun[],
  bodyWidth: number,
  keyBase: string,
  push: (key: string, node: React.ReactNode) => void,
  style: BlockStyle,
): void {
  const rows = wrapStyledRuns(runs, bodyWidth, style.firstPrefix, style.contPrefix);
  rows.forEach((segments, rowIndex) => {
    const key = `${keyBase}_${rowIndex}`;
    const styled =
      style.bold || style.blockColor !== undefined
        ? segments.map((segment) => applyBlockStyle(segment, style))
        : segments;
    push(key, <StyledLine key={key} segments={styled} />);
  });
}

function applyBlockStyle(segment: StyledSegment, style: BlockStyle): StyledSegment {
  // Preserve an explicit prefix color (list marker / quote bar) and never
  // recolor inline code, which keeps its own emphasis.
  if (segment.color !== undefined || segment.code) {
    return style.bold ? { ...segment, bold: true } : segment;
  }
  const next: StyledSegment = { ...segment };
  if (style.bold) {
    next.bold = true;
  }
  if (style.blockColor !== undefined) {
    next.color = style.blockColor;
  }
  return next;
}

function pushFenceLine(
  open: boolean,
  lang: string,
  bodyWidth: number,
  key: string,
  push: (key: string, node: React.ReactNode) => void,
): void {
  const label = open && lang ? `─── ${lang} ───` : '───';
  push(key, <StyledLine key={key} segments={[{ text: truncateToWidth(label, bodyWidth), dimColor: true }]} />);
}

function pushCodeLines(
  text: string,
  lang: string,
  bodyWidth: number,
  keyBase: string,
  push: (key: string, node: React.ReactNode) => void,
): void {
  const color = codeColor(text, lang);
  const chunks = wrapToWidth(text.length === 0 ? ' ' : text, bodyWidth);
  chunks.forEach((chunk, chunkIndex) => {
    const key = `${keyBase}_c${chunkIndex}`;
    push(key, <StyledLine key={key} segments={[{ text: chunk, color }]} />);
  });
}

function codeColor(text: string, lang: string): string {
  if (lang === 'diff') {
    if (text.startsWith('+')) {
      return inkColors.success;
    }
    if (text.startsWith('-')) {
      return inkColors.error;
    }
    if (text.startsWith('@@')) {
      return inkColors.accent;
    }
  }
  return inkColors.accent;
}

function pushTableLines(
  rows: string[][],
  alignments: TableAlignment[],
  bodyWidth: number,
  keyBase: string,
  push: (key: string, node: React.ReactNode) => void,
): void {
  if (rows.length === 0) {
    return;
  }
  const columnCount = rows.reduce((max, row) => Math.max(max, row.length), 0);
  if (columnCount === 0) {
    return;
  }

  const maxRenderableColumns = Math.max(1, Math.floor((bodyWidth - 1) / (MIN_TABLE_CELL_WIDTH + 3)));
  const visibleColumnCount = Math.min(columnCount, maxRenderableColumns);
  const hiddenColumnCount = columnCount - visibleColumnCount;
  const headers = normalizeTableCells(rows[0] ?? [], visibleColumnCount);
  const bodyRows = rows.slice(1);
  const hiddenColumnNote =
    hiddenColumnCount > 0
      ? `... ${hiddenColumnCount} more ${hiddenColumnCount === 1 ? 'column' : 'columns'} hidden ...`
      : undefined;
  const hiddenRowCount = Math.max(0, bodyRows.length - MAX_TABLE_ROWS);
  const hiddenRowNote =
    hiddenRowCount > 0
      ? `... ${hiddenRowCount} more ${hiddenRowCount === 1 ? 'row' : 'rows'}; open /outputs for full content ...`
      : undefined;
  const displayRows = bodyRows.slice(0, MAX_TABLE_ROWS).map((row) => normalizeTableCells(row, visibleColumnCount));
  const tableRowsForWidth = [headers, ...displayRows];
  const desiredColumnWidths = Array.from({ length: visibleColumnCount }, (_, column) =>
    tableRowsForWidth.reduce((max, row) => Math.max(max, cellVisibleWidth(row[column] ?? '')), MIN_TABLE_CELL_WIDTH),
  );
  const noteWidth = Math.max(
    0,
    ...[hiddenColumnNote, hiddenRowNote]
      .filter((note): note is string => note !== undefined)
      .map((note) => textWidth(` ${note} `) + 2),
  );
  const columnWidths = expandTableColumnWidths(
    fitTableColumnWidths(desiredColumnWidths, bodyWidth),
    bodyWidth,
    noteWidth,
  );
  const tableWidth = tableDisplayWidth(columnWidths);

  push(
    `${keyBase}:top`,
    <StyledLine key={`${keyBase}:top`} segments={tableBorderSegments(columnWidths, 'top')} />,
  );
  push(
    `${keyBase}:head`,
    <StyledLine key={`${keyBase}:head`} segments={tableRowSegments(headers, columnWidths, alignments, true)} />,
  );
  push(
    `${keyBase}:sep`,
    <StyledLine key={`${keyBase}:sep`} segments={tableBorderSegments(columnWidths, 'middle')} />,
  );

  displayRows.forEach((row, rowIndex) => {
    const key = `${keyBase}:r${rowIndex}`;
    const segments = tableRowSegments(row, columnWidths, alignments, false);
    push(key, <StyledLine key={key} segments={segments} />);
  });

  if (hiddenColumnNote) {
    const key = `${keyBase}:moreCols`;
    push(key, <StyledLine key={key} segments={tableNoteSegments(hiddenColumnNote, tableWidth)} />);
  }

  if (hiddenRowNote) {
    const key = `${keyBase}:more`;
    push(key, <StyledLine key={key} segments={tableNoteSegments(hiddenRowNote, tableWidth)} />);
  }

  push(
    `${keyBase}:bottom`,
    <StyledLine key={`${keyBase}:bottom`} segments={tableBorderSegments(columnWidths, 'bottom')} />,
  );
}

function tableRowSegments(
  cells: string[],
  columnWidths: number[],
  alignments: TableAlignment[],
  isHeader: boolean,
): StyledSegment[] {
  const segments: StyledSegment[] = [{ text: '│', dimColor: true }];
  columnWidths.forEach((width, column) => {
    const align: TableAlignment = isHeader ? 'left' : alignments[column] ?? 'left';
    segments.push({ text: ' ' });
    for (const segment of inlinePaddedCell(cells[column] ?? '', width, align, isHeader)) {
      segments.push(segment);
    }
    segments.push({ text: ' ' });
    segments.push({ text: '│', dimColor: true });
  });
  return segments;
}

function inlinePaddedCell(
  text: string,
  width: number,
  align: TableAlignment,
  isHeader: boolean,
): StyledSegment[] {
  const rawSegments = parseInlineRuns(text).map((run): StyledSegment => ({
    text: run.text,
    bold: isHeader || run.bold,
    code: run.code,
    color: isHeader && !run.code ? inkColors.accent : undefined,
  }));
  const content = truncateSegmentsWithEllipsis(rawSegments, width);
  const visible = segmentsWidth(content);
  const extra = Math.max(0, width - visible);
  const left = align === 'right' ? extra : align === 'center' ? Math.floor(extra / 2) : 0;
  const right = extra - left;
  const segments: StyledSegment[] = [];
  if (left > 0) {
    segments.push({ text: ' '.repeat(left) });
  }
  segments.push(...content);
  if (right > 0) {
    segments.push({ text: ' '.repeat(right) });
  }
  return segments;
}

function cellVisibleWidth(text: string): number {
  return parseInlineRuns(text).reduce((sum, run) => sum + textWidth(run.text), 0);
}

function normalizeTableCells(cells: string[], columnCount: number): string[] {
  return Array.from({ length: columnCount }, (_, column) => cells[column] ?? '');
}

function fitTableColumnWidths(desiredWidths: number[], bodyWidth: number): number[] {
  const columnCount = desiredWidths.length;
  const availableCellWidth = Math.max(columnCount, bodyWidth - (3 * columnCount + 1));
  const minWidth = availableCellWidth >= columnCount * MIN_TABLE_CELL_WIDTH ? MIN_TABLE_CELL_WIDTH : 1;
  const widths = Array.from({ length: columnCount }, () => minWidth);
  let remaining = availableCellWidth - columnCount * minWidth;
  const extras = desiredWidths.map((desired, index) => ({
    index,
    extra: Math.max(0, desired - minWidth),
  }));

  while (remaining > 0) {
    let progressed = false;
    for (const column of extras) {
      if (remaining <= 0) {
        break;
      }
      if (widths[column.index] - minWidth >= column.extra) {
        continue;
      }
      widths[column.index] += 1;
      remaining -= 1;
      progressed = true;
    }
    if (!progressed) {
      break;
    }
  }
  return widths;
}

function expandTableColumnWidths(widths: number[], bodyWidth: number, targetTableWidth: number): number[] {
  const expanded = [...widths];
  const safeTarget = Math.min(bodyWidth, Math.max(tableDisplayWidth(expanded), targetTableWidth));
  while (tableDisplayWidth(expanded) < safeTarget) {
    for (let index = 0; index < expanded.length && tableDisplayWidth(expanded) < safeTarget; index += 1) {
      expanded[index] += 1;
    }
  }
  return expanded;
}

function tableDisplayWidth(columnWidths: number[]): number {
  return columnWidths.reduce((sum, width) => sum + width, 0) + 3 * columnWidths.length + 1;
}

function tableBorderSegments(
  columnWidths: number[],
  position: 'top' | 'middle' | 'bottom',
): StyledSegment[] {
  const chars =
    position === 'top'
      ? { left: '┌', join: '┬', right: '┐' }
      : position === 'middle'
        ? { left: '├', join: '┼', right: '┤' }
        : { left: '└', join: '┴', right: '┘' };
  const segments: StyledSegment[] = [{ text: chars.left, dimColor: true }];
  columnWidths.forEach((width, index) => {
    segments.push({ text: '─'.repeat(width + 2), dimColor: true });
    segments.push({ text: index === columnWidths.length - 1 ? chars.right : chars.join, dimColor: true });
  });
  return segments;
}

function tableNoteSegments(text: string, tableWidth: number): StyledSegment[] {
  const innerWidth = Math.max(1, tableWidth - 2);
  const clipped = truncateToWidth(` ${text} `, innerWidth);
  const padding = Math.max(0, innerWidth - textWidth(clipped));
  return [
    { text: '│', dimColor: true },
    { text: clipped + ' '.repeat(padding), dimColor: true },
    { text: '│', dimColor: true },
  ];
}

function segmentsWidth(segments: StyledSegment[]): number {
  return segments.reduce((sum, segment) => sum + textWidth(segment.text), 0);
}

function truncateSegmentsWithEllipsis(segments: StyledSegment[], width: number): StyledSegment[] {
  const safeWidth = Math.max(0, Math.floor(width));
  if (segmentsWidth(segments) <= safeWidth) {
    return segments;
  }
  const ellipsis = '…';
  const ellipsisWidth = textWidth(ellipsis);
  if (safeWidth <= ellipsisWidth) {
    return [{ text: ellipsis, dimColor: true }];
  }
  const result = clipSegments(segments, safeWidth - ellipsisWidth);
  result.push({ text: ellipsis, dimColor: true });
  return result;
}

/** Clip a styled row to `width` display columns. */
function clipSegments(segments: StyledSegment[], width: number): StyledSegment[] {
  const safeWidth = Math.max(0, Math.floor(width));
  const result: StyledSegment[] = [];
  let used = 0;
  for (const segment of segments) {
    if (used >= safeWidth) {
      break;
    }
    let text = '';
    for (const grapheme of graphemes(segment.text)) {
      const advance = graphemeWidth(grapheme);
      if (used + advance > safeWidth) {
        used = safeWidth;
        break;
      }
      text += grapheme;
      used += advance;
    }
    if (text.length > 0) {
      result.push({ ...segment, text });
    }
  }
  return result;
}

const StyledLine: React.FC<{ segments: StyledSegment[] }> = ({ segments }) => (
  <Text>
    {INDENT}
    {segments.map((segment, index) => renderSegment(segment, index))}
  </Text>
);

const ToolBlockLine: React.FC<{ segments: StyledSegment[]; width: number }> = ({
  segments,
  width,
}) => {
  const fillWidth = Math.max(0, width - segmentsWidth(segments));
  const blockSegments: StyledSegment[] = [
    ...segments.map((segment) => ({
      ...segment,
      // 移除背景色，使用缩进表达层级
    })),
    ...(fillWidth > 0
      ? [{ text: ' '.repeat(fillWidth) }]
      : []),
  ];

  return (
    <Text>
      {TOOL_DETAIL_INDENT}
      {blockSegments.map((segment, index) => renderSegment(segment, index))}
    </Text>
  );
};

function renderSegment(segment: StyledSegment, key: number): React.ReactNode {
  const color = segment.color ?? (segment.code ? inkColors.accent : undefined);
  const props: {
    bold?: boolean;
    color?: string;
    backgroundColor?: string;
    dimColor?: boolean;
  } = {};
  if (segment.bold) {
    props.bold = true;
  }
  if (color !== undefined) {
    props.color = color;
  }
  if (segment.backgroundColor !== undefined) {
    props.backgroundColor = segment.backgroundColor;
  }
  if (segment.dimColor) {
    props.dimColor = true;
  }
  return (
    <Text key={key} {...props}>
      {segment.text}
    </Text>
  );
}

function pushStartupLines(
  startup: StartupInfo,
  contentWidth: number,
  push: (key: string, node: React.ReactNode) => void,
): void {
  const conn = connectionDisplay(startup.connectionStatus);
  const run = runDisplay(startup.runStatus);
  const bannerWidth = Math.max(20, Math.min(contentWidth, STARTUP_BANNER_MAX_WIDTH));
  const border = `+${'-'.repeat(Math.max(0, bannerWidth - 2))}+`;
  const session = startup.threadId ? startup.threadId.slice(0, 8) : 'new';
  const statusText = `${conn.icon} ${conn.text} | ${run.icon} ${run.text}`;
  const showArt = bannerWidth >= STARTUP_BANNER_ART_WIDTH + 4;

  push('startup:border:top', <Text key="startup:border:top" color={inkColors.accent}>{border}</Text>);
  push('startup:title', (
    <Text key="startup:title" bold color={inkColors.accent}>
      {bannerContent('DataFoundry', bannerWidth)}
    </Text>
  ));

  if (showArt) {
    STARTUP_BANNER_ART.forEach((line, index) => {
      const key = `startup:art:${index}`;
      push(
        key,
        <Text key={key} color={inkColors.accent}>
          {bannerContent(padToWidth(line.trimEnd(), STARTUP_BANNER_ART_WIDTH), bannerWidth)}
        </Text>,
      );
    });
  }

  push(
    'startup:session',
    <Text key="startup:session" dimColor>
      {bannerContent(`session ${session} | model ${startup.modelName}`, bannerWidth)}
    </Text>,
  );
  push(
    'startup:dir',
    <Text key="startup:dir" dimColor>
      {bannerContent(`cwd ${startup.directory}`, bannerWidth)}
    </Text>,
  );
  push(
    'startup:status',
    <Text key="startup:status">
      {bannerContent(statusText, bannerWidth)}
    </Text>,
  );
  push('startup:border:bottom', <Text key="startup:border:bottom" color={inkColors.accent}>{border}</Text>);
  push('startup:after', blankNode('startup:after'));
}

function bannerContent(text: string, width: number): string {
  const innerWidth = Math.max(0, width - 4);
  return `| ${centerToWidth(text, innerWidth)} |`;
}

function centerToWidth(text: string, width: number): string {
  const fitted = truncateToWidth(text, width);
  const remaining = Math.max(0, width - textWidth(fitted));
  const left = Math.floor(remaining / 2);
  const right = remaining - left;
  return `${' '.repeat(left)}${fitted}${' '.repeat(right)}`;
}

function padToWidth(text: string, width: number): string {
  const fitted = truncateToWidth(text, width);
  return `${fitted}${' '.repeat(Math.max(0, width - textWidth(fitted)))}`;
}

function blankNode(key: string): React.ReactNode {
  return <Text key={key}> </Text>;
}

interface MessageHeaderProps {
  message: DisplayMessage;
}

const MessageHeader: React.FC<MessageHeaderProps> = ({ message }) => {
  return (
    <Text>
      {INDENT}
      <Text bold color={roleColor(message.role)}>{roleLabel(message.role)}</Text>
      <Text dimColor>  {formatTimestamp(message.timestamp)}</Text>
      {message.isStreaming ? <Text dimColor> • working...</Text> : null}
    </Text>
  );
};

const ThinkingLine: React.FC = () => {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const timer = setInterval(() => setTick((value) => (value + 1) % 4), 360);
    return () => clearInterval(timer);
  }, []);
  const dotCount = tick % 4;
  const text = `thinking${'.'.repeat(dotCount)}${' '.repeat(3 - dotCount)}`;
  const bright = tick % 2 === 0;
  return (
    <Text color={bright ? inkColors.text : inkColors.muted} dimColor={!bright}>{INDENT + text}</Text>
  );
};

function roleColor(role: DisplayMessage['role']): string {
  switch (role) {
    case 'user':
      return inkColors.accent;
    case 'assistant':
      return inkColors.text;
    case 'system':
      return inkColors.muted;
    default:
      return inkColors.muted;
  }
}

function roleLabel(role: DisplayMessage['role']): string {
  switch (role) {
    case 'user':
      return 'YOU';
    case 'assistant':
      return 'AGENT';
    case 'system':
      return 'SYSTEM';
    default:
      return 'Unknown';
  }
}

function formatTimestamp(timestamp: number): string {
  const date = new Date(timestamp);
  const now = new Date();
  const isToday = date.toDateString() === now.toDateString();
  if (isToday) {
    return date.toLocaleTimeString('en-US', {
      hour: '2-digit',
      minute: '2-digit',
      hour12: false,
    });
  }
  return date.toLocaleString('en-US', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });
}

function connectionDisplay(status: ConnectionStatus): { color: string; icon: string; text: string } {
  switch (status) {
    case 'connected':
      return { color: inkColors.success, icon: '●', text: 'Connected' };
    case 'disconnected':
      return { color: inkColors.muted, icon: '○', text: 'Disconnected' };
    case 'error':
      return { color: inkColors.error, icon: '✖', text: 'Error' };
    default:
      return { color: inkColors.muted, icon: '○', text: 'Unknown' };
  }
}

function runDisplay(status: LiveRunStatus): { color: string; icon: string; text: string } {
  switch (status) {
    case 'idle':
      return { color: inkColors.muted, icon: '○', text: 'Idle' };
    case 'running':
      return { color: inkColors.warning, icon: '◐', text: 'Running' };
    case 'completed':
      return { color: inkColors.success, icon: '✓', text: 'Completed' };
    case 'failed':
      return { color: inkColors.error, icon: '✖', text: 'Failed' };
    default:
      return { color: inkColors.muted, icon: '○', text: 'Unknown' };
  }
}
