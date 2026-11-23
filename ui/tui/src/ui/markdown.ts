/**
 * Lightweight Markdown parsing for the chat transcript.
 *
 * Ported/adapted from DataDock's renderer, but kept JSX-free: it only turns raw
 * text into a flat list of block descriptors plus inline "runs". The transcript
 * renderer (see transcript-lines.tsx) is responsible for laying each descriptor
 * out as exactly one pre-wrapped terminal row, which is why parsing and
 * rendering are split here.
 */
import type { StyledRun } from './text-width.js';

export type TableAlignment = 'left' | 'right' | 'center';

export type ParsedMarkdownLine =
  | { kind: 'blank' }
  | { kind: 'heading'; level: number; text: string }
  | { kind: 'paragraph'; text: string }
  | { kind: 'bullet'; text: string }
  | { kind: 'ordered'; index: string; text: string }
  | { kind: 'quote'; text: string }
  | { kind: 'code'; text: string }
  | { kind: 'codeFence'; open: boolean; lang: string }
  | { kind: 'table'; rows: string[][]; alignments: TableAlignment[] };

const PARSE_CACHE_LIMIT = 300;
const parseCache = new Map<string, ParsedMarkdownLine[]>();

/** Max code lines rendered per fenced block before the rest is summarized. */
export const MAX_RENDERED_CODE_LINES = 80;

const TABLE_SEPARATOR = /^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?$/;
const INLINE_TOKEN = /(`[^`]+`|\*\*[^*]+\*\*)/g;

/**
 * Parse text into block descriptors. Results are memoized by content hash so the
 * transcript (which re-derives every render) does not re-parse stable history.
 */
export function parseMarkdownLines(input: string): ParsedMarkdownLine[] {
  const normalized = input.replace(/\r\n/g, '\n');
  const cacheKey = `${normalized.length}:${hashString(normalized)}`;

  const cached = parseCache.get(cacheKey);
  if (cached) {
    // Refresh LRU recency.
    parseCache.delete(cacheKey);
    parseCache.set(cacheKey, cached);
    return cached;
  }

  const parsed = hasMarkdownSyntax(normalized)
    ? parseRichLines(normalized)
    : parsePlainLines(normalized);

  parseCache.set(cacheKey, parsed);
  if (parseCache.size > PARSE_CACHE_LIMIT) {
    const oldest = parseCache.keys().next().value;
    if (oldest !== undefined) {
      parseCache.delete(oldest);
    }
  }
  return parsed;
}

/**
 * Cap each fenced code block at {@link MAX_RENDERED_CODE_LINES}, replacing the
 * overflow with a single "N code lines hidden" notice. Tables are bounded by the
 * renderer (it knows the visible row budget), so they pass through untouched.
 */
export function boundCodeLines(lines: ParsedMarkdownLine[]): ParsedMarkdownLine[] {
  const bounded: ParsedMarkdownLine[] = [];
  let inCode = false;
  let codeLines = 0;
  let omitted = 0;

  for (const line of lines) {
    if (line.kind === 'codeFence') {
      if (inCode && omitted > 0) {
        bounded.push(codeOmittedLine(omitted));
      }
      bounded.push(line);
      inCode = line.open;
      codeLines = 0;
      omitted = 0;
      continue;
    }
    if (inCode && line.kind === 'code') {
      if (codeLines < MAX_RENDERED_CODE_LINES) {
        bounded.push(line);
      } else {
        omitted += 1;
      }
      codeLines += 1;
      continue;
    }
    bounded.push(line);
  }

  if (inCode && omitted > 0) {
    bounded.push(codeOmittedLine(omitted));
  }
  return bounded;
}

/**
 * Split a single line into styled inline runs, stripping the markup so the
 * renderer can measure/wrap on visible width. Recognizes `code` and **bold**.
 */
export function parseInlineRuns(text: string): StyledRun[] {
  const runs: StyledRun[] = [];
  let cursor = 0;

  for (const match of text.matchAll(INLINE_TOKEN)) {
    const index = match.index ?? 0;
    if (index > cursor) {
      runs.push({ text: text.slice(cursor, index) });
    }
    const token = match[0];
    if (token.startsWith('`')) {
      runs.push({ text: token.slice(1, -1), code: true });
    } else {
      runs.push({ text: token.slice(2, -2), bold: true });
    }
    cursor = index + token.length;
  }

  if (cursor < text.length) {
    runs.push({ text: text.slice(cursor) });
  }
  return runs.length > 0 ? runs : [{ text }];
}

function codeOmittedLine(count: number): ParsedMarkdownLine {
  return { kind: 'code', text: `... ${count} code lines hidden; open /outputs for full content ...` };
}

function parsePlainLines(input: string): ParsedMarkdownLine[] {
  return input.split('\n').map(
    (line): ParsedMarkdownLine => (line.trim() ? { kind: 'paragraph', text: line } : { kind: 'blank' }),
  );
}

function parseRichLines(input: string): ParsedMarkdownLine[] {
  const lines = input.split('\n');
  const parsed: ParsedMarkdownLine[] = [];
  let inCode = false;
  let codeLanguage = '';

  for (let index = 0; index < lines.length; index += 1) {
    const rawLine = lines[index] ?? '';
    const trimmed = rawLine.trim();

    if (trimmed.startsWith('```')) {
      if (!inCode) {
        inCode = true;
        codeLanguage = trimmed.slice(3).trim();
        parsed.push({ kind: 'codeFence', open: true, lang: codeLanguage });
      } else {
        inCode = false;
        parsed.push({ kind: 'codeFence', open: false, lang: codeLanguage });
        codeLanguage = '';
      }
      continue;
    }
    if (inCode) {
      parsed.push({ kind: 'code', text: rawLine });
      continue;
    }
    if (!trimmed) {
      parsed.push({ kind: 'blank' });
      continue;
    }

    const heading = /^(#{1,6})\s+(.+)$/.exec(trimmed);
    if (heading) {
      parsed.push({ kind: 'heading', level: heading[1].length, text: heading[2] });
      continue;
    }

    const bullet = /^\s*[-*+]\s+(.+)$/.exec(rawLine);
    if (bullet) {
      parsed.push({ kind: 'bullet', text: bullet[1] });
      continue;
    }

    const ordered = /^\s*(\d+\.)\s+(.+)$/.exec(rawLine);
    if (ordered) {
      parsed.push({ kind: 'ordered', index: ordered[1], text: ordered[2] });
      continue;
    }

    const quote = /^>\s?(.*)$/.exec(trimmed);
    if (quote) {
      parsed.push({ kind: 'quote', text: quote[1] });
      continue;
    }

    const table = parseTableAt(lines, index);
    if (table) {
      parsed.push({ kind: 'table', rows: table.rows, alignments: table.alignments });
      index = table.nextIndex - 1;
      continue;
    }

    parsed.push({ kind: 'paragraph', text: rawLine });
  }

  return parsed;
}

function hasMarkdownSyntax(input: string): boolean {
  return /(^|\n)(#{1,6}\s|[-*+]\s|\d+\.\s|>\s|```|\|)|`[^`]+`|\*\*[^*]+\*\*/.test(input);
}

interface TableParse {
  rows: string[][];
  alignments: TableAlignment[];
  nextIndex: number;
}

function parseTableAt(lines: string[], index: number): TableParse | undefined {
  const header = parseTableRow(lines[index] ?? '');
  const alignments = parseTableSeparator(lines[index + 1] ?? '');
  if (!header || !alignments || header.length < 2) {
    return undefined;
  }

  const rows = [header];
  let nextIndex = index + 2;
  while (nextIndex < lines.length) {
    const row = parseTableRow(lines[nextIndex] ?? '');
    if (!row) {
      break;
    }
    rows.push(row);
    nextIndex += 1;
  }

  return { rows, alignments, nextIndex };
}

function parseTableRow(line: string): string[] | undefined {
  const trimmed = line.trim();
  if (!trimmed.includes('|') || TABLE_SEPARATOR.test(trimmed)) {
    return undefined;
  }
  // Keep interior empty cells so columns stay aligned with the header.
  const cells = trimmed.replace(/^\|/, '').replace(/\|$/, '').split('|').map((cell) => cell.trim());
  return cells.length >= 2 ? cells : undefined;
}

function parseTableSeparator(line: string): TableAlignment[] | undefined {
  const trimmed = line.trim();
  if (!TABLE_SEPARATOR.test(trimmed)) {
    return undefined;
  }
  const cells = trimmed.replace(/^\|/, '').replace(/\|$/, '').split('|').map((cell) => cell.trim());
  return cells.map((cell) => {
    const left = cell.startsWith(':');
    const right = cell.endsWith(':');
    if (left && right) {
      return 'center';
    }
    if (right) {
      return 'right';
    }
    return 'left';
  });
}

function hashString(input: string): string {
  let hash = 2166136261;
  for (let index = 0; index < input.length; index += 1) {
    hash ^= input.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0).toString(36);
}
