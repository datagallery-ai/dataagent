import React, { useEffect, useMemo, useState } from 'react';
import { Box, Text, useInput, useStdout } from 'ink';
import {
  graphemes,
  graphemeWidth,
  textWidth,
  truncateToWidth,
  type StyledSegment as TextStyledSegment,
} from '../text-width.js';
import { inkColors } from '../theme.js';

type Alignment = 'left' | 'right' | 'center';
type ColumnKind = 'text' | 'number' | 'currency' | 'percent' | 'boolean' | 'date' | 'badge';
type SortDirection = 'asc' | 'desc';

interface ColumnMeta {
  header: string;
  kind: ColumnKind;
  align: Alignment;
  currencySymbol?: string | undefined;
}

interface SortState {
  columnIndex: number;
  direction: SortDirection;
}

interface StyledSegment extends TextStyledSegment {
  inverse?: boolean | undefined;
}

export interface TableColumn {
  header: string;
  accessor: string;
  align?: Alignment;
  width?: number;
}

export interface TableViewProps {
  columns: string[];
  rows: string[][];
  pageSize?: number;
  title?: string;
  showPagination?: boolean;
  maxWidth?: number;
  enableKeyboardNav?: boolean;
  enableSorting?: boolean;
}

const SAMPLE_LIMIT = 40;
const MIN_CELL_WIDTH = 4;
const MAX_CELL_WIDTH = 28;
const MAX_TABLE_WIDTH = 120;
const EMPTY_CELL = '∅';

const BOX_CHARS = {
  top: { left: '┌', join: '┬', right: '┐' },
  middle: { left: '├', join: '┼', right: '┤' },
  bottom: { left: '└', join: '┴', right: '┘' },
};

function safeCell(row: string[] | undefined, columnIndex: number): string {
  const value = row?.[columnIndex];
  return value === undefined || value === null ? '' : String(value);
}

function nonEmptySample(rows: string[][], columnIndex: number): string[] {
  return rows
    .slice(0, SAMPLE_LIMIT)
    .map((row) => safeCell(row, columnIndex).trim())
    .filter((value) => value.length > 0);
}

function headerMatches(header: string, words: string[]): boolean {
  const normalized = header.toLowerCase().replace(/[^a-z0-9]+/gu, '_');
  return words.some((word) => {
    const escaped = word.replace(/[.*+?^${}()|[\]\\]/gu, '\\$&');
    return new RegExp(`(^|_)${escaped}($|_)`, 'u').test(normalized);
  });
}

function parseNumericValue(rawValue: string): number | null {
  let value = rawValue.trim();
  if (!value) return null;

  let negativeByParens = false;
  if (/^\(.+\)$/u.test(value)) {
    negativeByParens = true;
    value = value.slice(1, -1);
  }

  value = value
    .replace(/[%,$€£¥￥]/gu, '')
    .replace(/\s+/gu, '')
    .replace(/,/gu, '');

  if (!/^[+-]?(?:\d+|\d*\.\d+)$/u.test(value)) {
    return null;
  }

  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return null;
  return negativeByParens ? -Math.abs(parsed) : parsed;
}

function parseBooleanValue(rawValue: string): boolean | null {
  const value = rawValue.trim().toLowerCase();
  if (['true', 'yes', 'y', 'enabled', 'active'].includes(value)) return true;
  if (['false', 'no', 'n', 'disabled', 'inactive'].includes(value)) return false;
  return null;
}

function isDateLike(value: string): boolean {
  const trimmed = value.trim();
  return (
    /^\d{4}-\d{2}-\d{2}(?:[tT\s]\d{2}:\d{2}(?::\d{2})?)?/u.test(trimmed) ||
    /^\d{4}\/\d{2}\/\d{2}$/u.test(trimmed)
  );
}

function detectCurrencySymbol(values: string[], header: string): string | undefined {
  const fromValue = values.find((value) => /[$€£¥￥]/u.test(value));
  if (fromValue) {
    const match = fromValue.match(/[$€£¥￥]/u);
    if (match?.[0]) return match[0] === '￥' ? '¥' : match[0];
  }
  const lowerHeader = header.toLowerCase();
  if (lowerHeader.includes('usd') || lowerHeader.includes('$')) return '$';
  if (lowerHeader.includes('eur')) return '€';
  if (lowerHeader.includes('gbp')) return '£';
  if (lowerHeader.includes('cny') || lowerHeader.includes('rmb')) return '¥';
  return undefined;
}

function inferColumnMeta(header: string, rows: string[][], columnIndex: number): ColumnMeta {
  const values = nonEmptySample(rows, columnIndex);
  if (values.length === 0) {
    return { header, kind: 'text', align: 'left' };
  }

  const numericCount = values.filter((value) => parseNumericValue(value) !== null).length;
  const booleanCount = values.filter((value) => parseBooleanValue(value) !== null).length;
  const dateCount = values.filter(isDateLike).length;
  const percentByValue = values.some((value) => value.includes('%'));
  const percentByHeader = headerMatches(header, [
    'rate',
    'ratio',
    'percent',
    'percentage',
    'pct',
    'share',
    'margin',
    'ctr',
    'cvr',
  ]);
  const currencySymbol = detectCurrencySymbol(values, header);
  const currencyByHeader = headerMatches(header, [
    'usd',
    'eur',
    'gbp',
    'cny',
    'rmb',
    'jpy',
    'amount',
    'price',
    'cost',
    'spend',
    'budget',
    'arpu',
    'aov',
  ]);

  if (booleanCount / values.length >= 0.8) {
    return { header, kind: 'boolean', align: 'center' };
  }
  if (dateCount / values.length >= 0.8) {
    return { header, kind: 'date', align: 'left' };
  }
  if ((percentByValue || percentByHeader) && numericCount / values.length >= 0.7) {
    return { header, kind: 'percent', align: 'right' };
  }
  if ((currencySymbol || currencyByHeader) && numericCount / values.length >= 0.7) {
    return { header, kind: 'currency', align: 'right', currencySymbol };
  }
  if (numericCount / values.length >= 0.7) {
    return { header, kind: 'number', align: 'right' };
  }

  const distinctValues = new Set(values.map((value) => value.toLowerCase()));
  const averageWidth = values.reduce((sum, value) => sum + textWidth(value), 0) / values.length;
  const badgeByHeader = headerMatches(header, [
    'status',
    'state',
    'stage',
    'segment',
    'category',
    'channel',
    'region',
    'type',
    'priority',
  ]);
  if (badgeByHeader || (distinctValues.size <= Math.max(4, Math.ceil(values.length / 2)) && averageWidth <= 14)) {
    return { header, kind: 'badge', align: 'left' };
  }

  return { header, kind: 'text', align: 'left' };
}

function compactNumber(value: number): string {
  const absValue = Math.abs(value);
  const sign = value < 0 ? '-' : '';
  const units: Array<[number, string]> = [
    [1_000_000_000, 'B'],
    [1_000_000, 'M'],
    [1_000, 'K'],
  ];

  for (const [divisor, suffix] of units) {
    if (absValue >= divisor) {
      const scaled = absValue / divisor;
      const digits = scaled >= 100 ? 0 : scaled >= 10 ? 1 : 1;
      return `${sign}${trimTrailingZeros(scaled.toFixed(digits))}${suffix}`;
    }
  }

  if (Number.isInteger(value)) {
    return value.toLocaleString('en-US');
  }
  return trimTrailingZeros(value.toLocaleString('en-US', { maximumFractionDigits: 2 }));
}

function trimTrailingZeros(value: string): string {
  return value.replace(/\.0+$/u, '').replace(/(\.\d*?)0+$/u, '$1');
}

function formatPercent(value: number, rawValue: string): string {
  const normalized = rawValue.includes('%') || Math.abs(value) > 1 ? value : value * 100;
  const absValue = Math.abs(normalized);
  const digits = absValue >= 100 ? 0 : absValue >= 10 ? 1 : 1;
  return `${trimTrailingZeros(normalized.toFixed(digits))}%`;
}

function formatDateValue(value: string): string {
  const trimmed = value.trim();
  const match = trimmed.match(/^(\d{4}[-/]\d{2}[-/]\d{2})(?:[tT\s](\d{2}:\d{2}))?/u);
  if (!match) return trimmed;
  return match[2] ? `${match[1]} ${match[2]}` : match[1] ?? trimmed;
}

function formatCellValue(rawValue: string, meta: ColumnMeta): string {
  const trimmed = rawValue.trim();
  if (!trimmed) return EMPTY_CELL;

  if (meta.kind === 'boolean') {
    const parsed = parseBooleanValue(trimmed);
    if (parsed === true) return 'TRUE';
    if (parsed === false) return 'FALSE';
    return trimmed;
  }

  if (meta.kind === 'date') {
    return formatDateValue(trimmed);
  }

  if (meta.kind === 'badge') {
    return trimmed.length <= 16 ? trimmed.toUpperCase() : trimmed;
  }

  const numericValue = parseNumericValue(trimmed);
  if (numericValue === null) return trimmed;

  if (meta.kind === 'percent') {
    return formatPercent(numericValue, trimmed);
  }

  if (meta.kind === 'currency') {
    const symbol = meta.currencySymbol ?? '';
    const sign = numericValue < 0 ? '-' : '';
    return `${sign}${symbol}${compactNumber(Math.abs(numericValue))}`;
  }

  if (meta.kind === 'number') {
    return compactNumber(numericValue);
  }

  return trimmed;
}

function valueSegments(rawValue: string, meta: ColumnMeta, dimRow: boolean): StyledSegment[] {
  const displayValue = formatCellValue(rawValue, meta);
  const empty = displayValue === EMPTY_CELL;
  const segment: StyledSegment = { text: displayValue };

  if (empty) {
    segment.dimColor = true;
    return [segment];
  }

  if (meta.kind === 'number' || meta.kind === 'currency' || meta.kind === 'percent') {
    const numericValue = parseNumericValue(rawValue);
    if (numericValue === null || numericValue === 0) {
      segment.dimColor = true;
    } else {
      segment.color = numericValue < 0 ? inkColors.error : inkColors.success;
    }
    return [segment];
  }

  if (meta.kind === 'boolean') {
    const parsed = parseBooleanValue(rawValue);
    segment.bold = true;
    segment.color = parsed === false ? inkColors.error : parsed === true ? inkColors.success : inkColors.warning;
    return [segment];
  }

  if (meta.kind === 'date') {
    segment.color = inkColors.muted;
    return [segment];
  }

  if (meta.kind === 'badge') {
    segment.bold = true;
    segment.color = badgeColor(rawValue);
    return [segment];
  }

  if (dimRow) {
    segment.dimColor = true;
  }
  return [segment];
}

function badgeColor(value: string): string {
  const normalized = value.trim().toLowerCase();
  if (/(success|active|ok|passed|complete|completed|done|valid)/u.test(normalized)) return inkColors.success;
  if (/(fail|failed|error|invalid|disabled|cancel|blocked)/u.test(normalized)) return inkColors.error;
  if (/(warn|warning|pending|queued|running|processing|review)/u.test(normalized)) return inkColors.warning;
  if (/(info|note|draft|new)/u.test(normalized)) return inkColors.accent;

  const palette = [inkColors.accent, inkColors.muted, inkColors.success];
  let hash = 0;
  for (const char of normalized) {
    hash = (hash + char.charCodeAt(0)) % palette.length;
  }
  return palette[hash] ?? inkColors.muted;
}

function compareRows(a: string[], b: string[], sort: SortState, metas: ColumnMeta[]): number {
  const meta = metas[sort.columnIndex];
  const aValue = safeCell(a, sort.columnIndex);
  const bValue = safeCell(b, sort.columnIndex);
  let result = 0;

  if (meta && ['number', 'currency', 'percent'].includes(meta.kind)) {
    const aNumber = parseNumericValue(aValue);
    const bNumber = parseNumericValue(bValue);
    result = (aNumber ?? Number.NEGATIVE_INFINITY) - (bNumber ?? Number.NEGATIVE_INFINITY);
  } else if (meta?.kind === 'boolean') {
    const aBool = parseBooleanValue(aValue);
    const bBool = parseBooleanValue(bValue);
    result = Number(aBool === true) - Number(bBool === true);
  } else {
    result = formatCellValue(aValue, meta ?? { header: '', kind: 'text', align: 'left' }).localeCompare(
      formatCellValue(bValue, meta ?? { header: '', kind: 'text', align: 'left' }),
      undefined,
      { numeric: true, sensitivity: 'base' },
    );
  }

  return sort.direction === 'asc' ? result : -result;
}

function nextSortState(current: SortState | null, metas: ColumnMeta[]): SortState | null {
  if (metas.length === 0) return null;

  if (!current) {
    const numericIndex = metas.findIndex((meta) => ['number', 'currency', 'percent'].includes(meta.kind));
    return {
      columnIndex: numericIndex >= 0 ? numericIndex : 0,
      direction: numericIndex >= 0 ? 'desc' : 'asc',
    };
  }

  if (current.direction === 'asc') {
    return { ...current, direction: 'desc' };
  }

  const nextColumn = current.columnIndex + 1;
  if (nextColumn >= metas.length) return null;
  return { columnIndex: nextColumn, direction: 'asc' };
}

function sortLabel(sort: SortState | null, metas: ColumnMeta[]): string | null {
  if (!sort) return null;
  const meta = metas[sort.columnIndex];
  if (!meta) return null;
  return `${meta.header} ${sort.direction === 'asc' ? '升序' : '降序'}`;
}

function visibleColumnCount(columnCount: number, availableWidth: number): number {
  if (columnCount <= 0) return 0;
  const maxColumns = Math.max(1, Math.floor((availableWidth - 1) / (MIN_CELL_WIDTH + 3)));
  return Math.min(columnCount, maxColumns);
}

function desiredColumnWidths(
  metas: ColumnMeta[],
  rows: string[][],
  sort: SortState | null,
  count: number,
): number[] {
  const sampleRows = rows.slice(0, SAMPLE_LIMIT);
  return Array.from({ length: count }, (_, columnIndex) => {
    const meta = metas[columnIndex] ?? { header: '', kind: 'text' as const, align: 'left' as const };
    const sortIndicator =
      sort?.columnIndex === columnIndex ? (sort.direction === 'asc' ? ' ▲' : ' ▼') : '';
    let width = textWidth(`${meta.header}${sortIndicator}`);
    for (const row of sampleRows) {
      width = Math.max(width, textWidth(formatCellValue(safeCell(row, columnIndex), meta)));
    }
    return Math.min(MAX_CELL_WIDTH, Math.max(MIN_CELL_WIDTH, width));
  });
}

function fitColumnWidths(desiredWidths: number[], availableWidth: number): number[] {
  const columnCount = desiredWidths.length;
  if (columnCount === 0) return [];

  const availableCellWidth = Math.max(columnCount, availableWidth - (3 * columnCount + 1));
  const minWidth: number = availableCellWidth >= columnCount * MIN_CELL_WIDTH ? MIN_CELL_WIDTH : 1;
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
      const current = widths[column.index] ?? minWidth;
      if (current - minWidth >= column.extra) continue;
      widths[column.index] = current + 1;
      remaining -= 1;
      progressed = true;
    }
    if (!progressed) break;
  }

  return widths;
}

function tableWidth(columnWidths: number[]): number {
  return columnWidths.reduce((sum, width) => sum + width, 0) + 3 * columnWidths.length + 1;
}

function padText(text: string, width: number, align: Alignment): string {
  const clipped = truncateToWidth(text, width);
  const extra = Math.max(0, width - textWidth(clipped));
  if (align === 'right') return `${' '.repeat(extra)}${clipped}`;
  if (align === 'center') {
    const left = Math.floor(extra / 2);
    return `${' '.repeat(left)}${clipped}${' '.repeat(extra - left)}`;
  }
  return `${clipped}${' '.repeat(extra)}`;
}

function segmentWidth(segments: StyledSegment[]): number {
  return segments.reduce((sum, segment) => sum + textWidth(segment.text), 0);
}

function clipSegments(segments: StyledSegment[], width: number): StyledSegment[] {
  const result: StyledSegment[] = [];
  let used = 0;
  for (const segment of segments) {
    if (used >= width) break;
    let text = '';
    for (const piece of graphemes(segment.text)) {
      const pieceWidth = graphemeWidth(piece);
      if (used + pieceWidth > width) break;
      text += piece;
      used += pieceWidth;
    }
    if (text) {
      result.push({ ...segment, text });
    }
  }
  return result;
}

function truncateSegments(segments: StyledSegment[], width: number): StyledSegment[] {
  if (segmentWidth(segments) <= width) return segments;
  if (width <= 1) return [{ text: '…', dimColor: true }];
  return [...clipSegments(segments, width - 1), { text: '…', dimColor: true }];
}

function padSegments(segments: StyledSegment[], width: number, align: Alignment): StyledSegment[] {
  const clipped = truncateSegments(segments, width);
  const extra = Math.max(0, width - segmentWidth(clipped));
  const left = align === 'right' ? extra : align === 'center' ? Math.floor(extra / 2) : 0;
  const right = extra - left;
  const padded: StyledSegment[] = [];
  if (left > 0) padded.push({ text: ' '.repeat(left) });
  padded.push(...clipped);
  if (right > 0) padded.push({ text: ' '.repeat(right) });
  return padded;
}

function borderSegments(columnWidths: number[], position: keyof typeof BOX_CHARS): StyledSegment[] {
  const chars = BOX_CHARS[position];
  const segments: StyledSegment[] = [{ text: chars.left, dimColor: true }];
  columnWidths.forEach((width, index) => {
    segments.push({ text: '─'.repeat(width + 2), dimColor: true });
    segments.push({
      text: index === columnWidths.length - 1 ? chars.right : chars.join,
      dimColor: true,
    });
  });
  return segments;
}

function headerSegments(
  metas: ColumnMeta[],
  columnWidths: number[],
  sort: SortState | null,
): StyledSegment[] {
  const segments: StyledSegment[] = [{ text: '│', dimColor: true }];
  columnWidths.forEach((width, columnIndex) => {
    const meta = metas[columnIndex] ?? { header: '', kind: 'text' as const, align: 'left' as const };
    const indicator = sort?.columnIndex === columnIndex ? (sort.direction === 'asc' ? ' ▲' : ' ▼') : '';
    segments.push({ text: ' ' });
    segments.push({
      text: padText(`${meta.header}${indicator}`, width, meta.align),
      bold: true,
      color: inkColors.accent,
    });
    segments.push({ text: ' ' });
    segments.push({ text: '│', dimColor: true });
  });
  return segments;
}

function rowSegments(
  row: string[],
  metas: ColumnMeta[],
  columnWidths: number[],
  rowIndex: number,
): StyledSegment[] {
  const dimRow = rowIndex % 2 === 1;
  const segments: StyledSegment[] = [{ text: '│', dimColor: true }];
  columnWidths.forEach((width, columnIndex) => {
    const meta = metas[columnIndex] ?? { header: '', kind: 'text' as const, align: 'left' as const };
    segments.push({ text: ' ' });
    segments.push(...padSegments(valueSegments(safeCell(row, columnIndex), meta, dimRow), width, meta.align));
    segments.push({ text: ' ' });
    segments.push({ text: '│', dimColor: true });
  });
  return segments;
}

function noteSegments(text: string, width: number): StyledSegment[] {
  const innerWidth = Math.max(1, width - 2);
  const clipped = truncateToWidth(` ${text} `, innerWidth);
  const padded = `${clipped}${' '.repeat(Math.max(0, innerWidth - textWidth(clipped)))}`;
  return [
    { text: '│', dimColor: true },
    { text: padded, dimColor: true },
    { text: '│', dimColor: true },
  ];
}

const StyledLine: React.FC<{ segments: StyledSegment[] }> = ({ segments }) => (
  <Text>
    {segments.map((segment, index) => {
      const props: {
        bold?: boolean;
        color?: string;
        dimColor?: boolean;
        inverse?: boolean;
      } = {};
      if (segment.bold) props.bold = true;
      if (segment.color) props.color = segment.color;
      if (segment.dimColor) props.dimColor = true;
      if (segment.inverse) props.inverse = true;
      return (
        <Text key={index} {...props}>
          {segment.text}
        </Text>
      );
    })}
  </Text>
);

export const TableView: React.FC<TableViewProps> = ({
  columns,
  rows,
  pageSize = 12,
  title,
  showPagination = true,
  maxWidth,
  enableKeyboardNav = false,
  enableSorting = true,
}) => {
  const [currentPage, setCurrentPage] = useState(0);
  const [sort, setSort] = useState<SortState | null>(null);
  const { stdout } = useStdout();
  const terminalWidth = stdout.columns ?? process.stdout.columns ?? 100;
  const availableWidth = Math.max(
    24,
    Math.min(maxWidth ?? MAX_TABLE_WIDTH, Math.max(24, terminalWidth - 10)),
  );

  const metas = useMemo(
    () => columns.map((header, index) => inferColumnMeta(header, rows, index)),
    [columns, rows],
  );

  const sortedRows = useMemo(() => {
    if (!sort) return rows;
    return [...rows].sort((a, b) => compareRows(a, b, sort, metas));
  }, [metas, rows, sort]);

  const totalRows = sortedRows.length;
  const totalPages = Math.max(1, Math.ceil(totalRows / pageSize));
  const shouldPaginate = showPagination && totalRows > pageSize;

  useEffect(() => {
    setCurrentPage((page) => Math.min(page, totalPages - 1));
  }, [totalPages]);

  useInput(
    (input, key) => {
      if (!enableKeyboardNav) return;
      if (shouldPaginate && key.pageDown) {
        setCurrentPage((page) => Math.min(totalPages - 1, page + 1));
        return;
      }
      if (shouldPaginate && key.pageUp) {
        setCurrentPage((page) => Math.max(0, page - 1));
        return;
      }
      if (enableSorting && key.ctrl && input === 'r') {
        setSort((current) => nextSortState(current, metas));
        setCurrentPage(0);
      }
    },
    { isActive: enableKeyboardNav && (shouldPaginate || enableSorting) },
  );

  const pageRows = useMemo(() => {
    if (!shouldPaginate) return sortedRows;
    const start = currentPage * pageSize;
    return sortedRows.slice(start, start + pageSize);
  }, [currentPage, pageSize, shouldPaginate, sortedRows]);

  const shownStart = totalRows === 0 ? 0 : currentPage * pageSize + 1;
  const shownEnd = shouldPaginate ? Math.min((currentPage + 1) * pageSize, totalRows) : totalRows;
  const visibleColumns = visibleColumnCount(columns.length, availableWidth);
  const hiddenColumns = Math.max(0, columns.length - visibleColumns);
  const desiredWidths = desiredColumnWidths(metas, sortedRows, sort, visibleColumns);
  const columnWidths = fitColumnWidths(desiredWidths, availableWidth);
  const renderedTableWidth = tableWidth(columnWidths);
  const activeSortLabel = sortLabel(sort, metas);

  if (columns.length === 0 || rows.length === 0) {
    return (
      <Box flexDirection="column" paddingX={1}>
        {title && (
          <Box marginBottom={1}>
            <Text bold color={inkColors.accent}>{title}</Text>
          </Box>
        )}
        <Box borderStyle="round" borderColor={inkColors.border} paddingX={1}>
          <Text dimColor>无可预览的表格数据</Text>
        </Box>
      </Box>
    );
  }

  return (
    <Box flexDirection="column" paddingX={1}>
      {title && (
        <Box marginBottom={1}>
          <Text bold color={inkColors.accent}>{title}</Text>
        </Box>
      )}

      <Box marginBottom={1}>
        <Text color={inkColors.accent} bold>TABLE</Text>
        <Text dimColor>
          {' '}
          {columns.length} 列 × {totalRows.toLocaleString()} 行
          {shouldPaginate ? ` • 显示 ${shownStart}-${shownEnd}` : ' • 已显示全部'}
          {activeSortLabel ? ` • ${activeSortLabel}` : ''}
        </Text>
      </Box>

      <Box flexDirection="column">
        <StyledLine segments={borderSegments(columnWidths, 'top')} />
        <StyledLine segments={headerSegments(metas, columnWidths, sort)} />
        <StyledLine segments={borderSegments(columnWidths, 'middle')} />
        {pageRows.map((row, index) => (
          <StyledLine key={`${currentPage}:${index}`} segments={rowSegments(row, metas, columnWidths, index)} />
        ))}
        {hiddenColumns > 0 && (
          <StyledLine
            segments={noteSegments(
              `还有 ${hiddenColumns} 列因终端宽度隐藏；导出或放宽终端可查看完整列`,
              renderedTableWidth,
            )}
          />
        )}
        <StyledLine segments={borderSegments(columnWidths, 'bottom')} />
      </Box>

      <Box marginTop={1}>
        <Text dimColor>
          {shouldPaginate && enableKeyboardNav
            ? `第 ${currentPage + 1}/${totalPages} 页 • PageUp/PageDown 翻页`
            : shouldPaginate
              ? `前 ${shownEnd} 行预览，共 ${totalRows.toLocaleString()} 行`
              : `${totalRows.toLocaleString()} 行预览`}
          {enableKeyboardNav && enableSorting ? ' • Ctrl+R 切换排序列' : ''}
        </Text>
      </Box>
    </Box>
  );
};

export function datasetToTableProps(detail: {
  columns: string[];
  rows: string[][];
}): Pick<TableViewProps, 'columns' | 'rows'> {
  return {
    columns: detail.columns,
    rows: detail.rows,
  };
}
