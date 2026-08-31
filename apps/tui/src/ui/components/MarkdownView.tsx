import React, { useEffect, useMemo, useState } from 'react';
import { Box, Text, useInput } from 'ink';
import { isMouseInput } from '../../input/mouse-wheel.js';
import {
  boundCodeLines,
  parseInlineRuns,
  parseMarkdownLines,
  type TableAlignment,
} from '../markdown.js';
import {
  textWidth,
  truncateToWidth,
  wrapStyledRuns,
  wrapToWidth,
  type StyledRun,
  type StyledSegment,
} from '../text-width.js';
import { inkColors } from '../theme.js';

interface MarkdownViewProps {
  content: string;
  width: number;
  maxRows?: number | undefined;
  keyboardActive?: boolean | undefined;
}

interface MarkdownRow {
  key: string;
  segments: StyledSegment[];
}

interface BlockStyle {
  bold?: boolean | undefined;
  blockColor?: string | undefined;
  firstPrefix?: StyledSegment | undefined;
  contPrefix?: StyledSegment | undefined;
}

const MIN_TABLE_CELL_WIDTH = 4;
const MAX_TABLE_ROWS = 200;

export const MarkdownView: React.FC<MarkdownViewProps> = ({
  content,
  width,
  maxRows,
  keyboardActive = false,
}) => {
  const bodyWidth = Math.max(8, Math.floor(width));
  const viewportRows = maxRows === undefined ? undefined : Math.max(1, Math.floor(maxRows));
  const rows = useMemo(
    () => markdownRows(content, bodyWidth),
    [bodyWidth, content],
  );
  const [scrollOffset, setScrollOffset] = useState(0);
  const maxOffset = viewportRows === undefined
    ? 0
    : Math.max(0, rows.length - viewportRows);
  const scrollable = viewportRows !== undefined && rows.length > viewportRows;

  useEffect(() => {
    setScrollOffset((current) => Math.min(current, maxOffset));
  }, [maxOffset]);

  useInput(
    (input, key) => {
      if (!scrollable || isMouseInput(input)) return;
      if (key.pageDown) {
        setScrollOffset((current) => Math.min(maxOffset, current + Math.max(1, (viewportRows ?? 1) - 1)));
        return;
      }
      if (key.pageUp) {
        setScrollOffset((current) => Math.max(0, current - Math.max(1, (viewportRows ?? 1) - 1)));
        return;
      }
      if (key.downArrow || input === 'j') {
        setScrollOffset((current) => Math.min(maxOffset, current + 1));
        return;
      }
      if (key.upArrow || input === 'k') {
        setScrollOffset((current) => Math.max(0, current - 1));
      }
    },
    { isActive: keyboardActive && scrollable },
  );

  const visibleRows = viewportRows === undefined
    ? rows
    : rows.slice(scrollOffset, scrollOffset + viewportRows);
  const shownEnd = Math.min(rows.length, scrollOffset + visibleRows.length);

  return (
    <Box flexDirection="column" overflow="hidden">
      {visibleRows.map((row) => (
        <StyledLine key={row.key} segments={row.segments} />
      ))}
      {scrollable && (
        <Text dimColor>
          {`Rows ${scrollOffset + 1}-${shownEnd}/${rows.length} - Up/Down scroll, PageUp/PageDown page`}
        </Text>
      )}
    </Box>
  );
};

function markdownRows(content: string, width: number): MarkdownRow[] {
  const rows: MarkdownRow[] = [];
  const parsed = boundCodeLines(parseMarkdownLines(content));
  let codeLang = '';

  const push = (key: string, segments: StyledSegment[]) => {
    rows.push({ key, segments });
  };

  parsed.forEach((line, lineIndex) => {
    const lineKey = `md:${lineIndex}`;
    switch (line.kind) {
      case 'blank':
        push(lineKey, [{ text: ' ' }]);
        return;
      case 'codeFence':
        codeLang = line.open ? line.lang : '';
        push(lineKey, fenceSegments(line.open, line.lang, width));
        return;
      case 'code':
        pushCodeRows(line.text, codeLang, width, lineKey, push);
        return;
      case 'heading':
        pushStyledRows(parseInlineRuns(line.text), width, lineKey, push, {
          bold: true,
          blockColor: inkColors.accent,
        });
        return;
      case 'paragraph':
        pushStyledRows(parseInlineRuns(line.text), width, lineKey, push, {});
        return;
      case 'bullet':
        pushStyledRows(parseInlineRuns(line.text), width, lineKey, push, {
          firstPrefix: { text: '- ', color: inkColors.muted },
          contPrefix: { text: '  ' },
        });
        return;
      case 'ordered': {
        const marker = `${line.index} `;
        pushStyledRows(parseInlineRuns(line.text), width, lineKey, push, {
          firstPrefix: { text: marker, color: inkColors.muted },
          contPrefix: { text: ' '.repeat(textWidth(marker)) },
        });
        return;
      }
      case 'quote':
        pushStyledRows(parseInlineRuns(line.text), width, lineKey, push, {
          firstPrefix: { text: '| ', color: inkColors.muted },
          contPrefix: { text: '| ', color: inkColors.muted },
          blockColor: inkColors.muted,
        });
        return;
      case 'table':
        pushTableRows(line.rows, line.alignments, width, lineKey, push);
        return;
    }
  });

  return rows.length > 0 ? rows : [{ key: 'empty', segments: [{ text: ' ' }] }];
}

function pushStyledRows(
  runs: StyledRun[],
  width: number,
  keyBase: string,
  push: (key: string, segments: StyledSegment[]) => void,
  style: BlockStyle,
): void {
  const rows = wrapStyledRuns(runs, width, style.firstPrefix, style.contPrefix);
  rows.forEach((segments, rowIndex) => {
    const styled =
      style.bold || style.blockColor !== undefined
        ? segments.map((segment) => applyBlockStyle(segment, style))
        : segments;
    push(`${keyBase}:${rowIndex}`, styled);
  });
}

function applyBlockStyle(segment: StyledSegment, style: BlockStyle): StyledSegment {
  if (segment.color !== undefined || segment.code) {
    return style.bold ? { ...segment, bold: true } : segment;
  }
  return {
    ...segment,
    ...(style.bold ? { bold: true } : {}),
    ...(style.blockColor !== undefined ? { color: style.blockColor } : {}),
  };
}

function fenceSegments(open: boolean, lang: string, width: number): StyledSegment[] {
  const label = open && lang ? `--- ${lang} ---` : '---';
  return [{ text: truncateToWidth(label, width), dimColor: true }];
}

function pushCodeRows(
  text: string,
  lang: string,
  width: number,
  keyBase: string,
  push: (key: string, segments: StyledSegment[]) => void,
): void {
  const color = codeColor(text, lang);
  const chunks = wrapToWidth(text.length === 0 ? ' ' : text, width);
  chunks.forEach((chunk, chunkIndex) => {
    push(`${keyBase}:c${chunkIndex}`, [{ text: chunk, color }]);
  });
}

function codeColor(text: string, lang: string): string {
  if (lang === 'diff') {
    if (text.startsWith('+')) return inkColors.success;
    if (text.startsWith('-')) return inkColors.error;
    if (text.startsWith('@@')) return inkColors.accent;
  }
  return inkColors.accent;
}

function pushTableRows(
  rows: string[][],
  alignments: TableAlignment[],
  width: number,
  keyBase: string,
  push: (key: string, segments: StyledSegment[]) => void,
): void {
  if (rows.length === 0) return;
  const columnCount = rows.reduce((max, row) => Math.max(max, row.length), 0);
  if (columnCount === 0) return;

  const maxRenderableColumns = Math.max(1, Math.floor((width - 1) / (MIN_TABLE_CELL_WIDTH + 3)));
  const visibleColumnCount = Math.min(columnCount, maxRenderableColumns);
  const hiddenColumnCount = columnCount - visibleColumnCount;
  const headers = normalizeTableCells(rows[0] ?? [], visibleColumnCount);
  const bodyRows = rows.slice(1);
  const hiddenRowCount = Math.max(0, bodyRows.length - MAX_TABLE_ROWS);
  const displayRows = bodyRows
    .slice(0, MAX_TABLE_ROWS)
    .map((row) => normalizeTableCells(row, visibleColumnCount));
  const tableRowsForWidth = [headers, ...displayRows];
  const desiredColumnWidths = Array.from({ length: visibleColumnCount }, (_, column) =>
    tableRowsForWidth.reduce((max, row) => Math.max(max, cellVisibleWidth(row[column] ?? '')), MIN_TABLE_CELL_WIDTH),
  );
  const notes = [
    hiddenColumnCount > 0
      ? `${hiddenColumnCount} more ${hiddenColumnCount === 1 ? 'column' : 'columns'} hidden`
      : undefined,
    hiddenRowCount > 0
      ? `${hiddenRowCount} more ${hiddenRowCount === 1 ? 'row' : 'rows'} hidden`
      : undefined,
  ].filter((note): note is string => note !== undefined);
  const noteWidth = Math.max(
    0,
    ...notes.map((note) => textWidth(` ${note} `) + 2),
  );
  const columnWidths = expandTableColumnWidths(
    fitTableColumnWidths(desiredColumnWidths, width),
    width,
    noteWidth,
  );
  const tableWidth = tableDisplayWidth(columnWidths);

  push(`${keyBase}:top`, tableBorderSegments(columnWidths, 'top'));
  push(`${keyBase}:head`, tableRowSegments(headers, columnWidths, alignments, true));
  push(`${keyBase}:sep`, tableBorderSegments(columnWidths, 'middle'));
  displayRows.forEach((row, rowIndex) => {
    push(`${keyBase}:r${rowIndex}`, tableRowSegments(row, columnWidths, alignments, false));
  });
  notes.forEach((note, index) => {
    push(`${keyBase}:note${index}`, tableNoteSegments(note, tableWidth));
  });
  push(`${keyBase}:bottom`, tableBorderSegments(columnWidths, 'bottom'));
}

function tableRowSegments(
  cells: string[],
  columnWidths: number[],
  alignments: TableAlignment[],
  isHeader: boolean,
): StyledSegment[] {
  const segments: StyledSegment[] = [{ text: '|', dimColor: true }];
  columnWidths.forEach((width, column) => {
    const align: TableAlignment = isHeader ? 'left' : alignments[column] ?? 'left';
    segments.push({ text: ' ' });
    segments.push(...inlinePaddedCell(cells[column] ?? '', width, align, isHeader));
    segments.push({ text: ' ' });
    segments.push({ text: '|', dimColor: true });
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
  return [
    ...(left > 0 ? [{ text: ' '.repeat(left) }] : []),
    ...content,
    ...(right > 0 ? [{ text: ' '.repeat(right) }] : []),
  ];
}

function cellVisibleWidth(text: string): number {
  return parseInlineRuns(text).reduce((sum, run) => sum + textWidth(run.text), 0);
}

function normalizeTableCells(cells: string[], columnCount: number): string[] {
  return Array.from({ length: columnCount }, (_, column) => cells[column] ?? '');
}

function fitTableColumnWidths(desiredWidths: number[], width: number): number[] {
  const columnCount = desiredWidths.length;
  const availableCellWidth = Math.max(columnCount, width - (3 * columnCount + 1));
  const minWidth: number = availableCellWidth >= columnCount * MIN_TABLE_CELL_WIDTH ? MIN_TABLE_CELL_WIDTH : 1;
  const widths: number[] = Array.from({ length: columnCount }, () => minWidth);
  let remaining = availableCellWidth - columnCount * minWidth;
  const extras = desiredWidths.map((desired, index) => ({
    index,
    extra: Math.max(0, desired - minWidth),
  }));

  while (remaining > 0) {
    let progressed = false;
    for (const column of extras) {
      if (remaining <= 0) break;
      if ((widths[column.index] ?? minWidth) - minWidth >= column.extra) continue;
      widths[column.index] = (widths[column.index] ?? minWidth) + 1;
      remaining -= 1;
      progressed = true;
    }
    if (!progressed) break;
  }
  return widths;
}

function expandTableColumnWidths(widths: number[], width: number, targetTableWidth: number): number[] {
  const expanded = [...widths];
  const safeTarget = Math.min(width, Math.max(tableDisplayWidth(expanded), targetTableWidth));
  while (tableDisplayWidth(expanded) < safeTarget) {
    for (let index = 0; index < expanded.length && tableDisplayWidth(expanded) < safeTarget; index += 1) {
      expanded[index] = (expanded[index] ?? MIN_TABLE_CELL_WIDTH) + 1;
    }
  }
  return expanded;
}

function tableDisplayWidth(columnWidths: number[]): number {
  return columnWidths.reduce((sum, columnWidth) => sum + columnWidth, 0) + 3 * columnWidths.length + 1;
}

function tableBorderSegments(
  columnWidths: number[],
  _position: 'top' | 'middle' | 'bottom',
): StyledSegment[] {
  const segments: StyledSegment[] = [{ text: '+', dimColor: true }];
  columnWidths.forEach((columnWidth) => {
    segments.push({ text: '-'.repeat(columnWidth + 2), dimColor: true });
    segments.push({ text: '+', dimColor: true });
  });
  return segments;
}

function tableNoteSegments(text: string, tableWidth: number): StyledSegment[] {
  const innerWidth = Math.max(1, tableWidth - 2);
  const clipped = truncateToWidth(` ${text} `, innerWidth);
  const padding = Math.max(0, innerWidth - textWidth(clipped));
  return [
    { text: '|', dimColor: true },
    { text: clipped + ' '.repeat(padding), dimColor: true },
    { text: '|', dimColor: true },
  ];
}

function segmentsWidth(segments: StyledSegment[]): number {
  return segments.reduce((sum, segment) => sum + textWidth(segment.text), 0);
}

function truncateSegmentsWithEllipsis(segments: StyledSegment[], width: number): StyledSegment[] {
  const safeWidth = Math.max(0, Math.floor(width));
  if (segmentsWidth(segments) <= safeWidth) return segments;
  const ellipsis = '...';
  const ellipsisWidth = textWidth(ellipsis);
  if (safeWidth <= ellipsisWidth) return [{ text: ellipsis, dimColor: true }];

  const result: StyledSegment[] = [];
  let used = 0;
  for (const segment of segments) {
    if (used >= safeWidth - ellipsisWidth) break;
    const remaining = safeWidth - ellipsisWidth - used;
    const clipped = truncateToWidth(segment.text, remaining, '');
    if (!clipped) break;
    result.push({ ...segment, text: clipped });
    used += textWidth(clipped);
    if (clipped.length < segment.text.length) break;
  }
  result.push({ text: ellipsis, dimColor: true });
  return result;
}

const StyledLine: React.FC<{ segments: StyledSegment[] }> = ({ segments }) => (
  <Text>
    {segments.map((segment, index) => renderSegment(segment, index))}
  </Text>
);

function renderSegment(segment: StyledSegment, key: number): React.ReactNode {
  const color = segment.color ?? (segment.code ? inkColors.accent : undefined);
  const props: { bold?: boolean; color?: string; dimColor?: boolean } = {};
  if (segment.bold) props.bold = true;
  if (color !== undefined) props.color = color;
  if (segment.dimColor) props.dimColor = true;
  return (
    <Text key={key} {...props}>
      {segment.text}
    </Text>
  );
}
