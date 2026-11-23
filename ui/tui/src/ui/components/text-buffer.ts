/**
 * TextBuffer - terminal-oriented multi-line input state.
 *
 * This mirrors the important parts of qwen-code's input architecture: logical
 * text is kept separate from terminal visual rows, cursor movement works across
 * soft-wrapped rows, and all mutations go through grapheme-aware helpers.
 */

import { useRef } from 'react';
import { graphemeWidth, graphemes } from '../text-width.js';

export function toCodePoints(str: string): string[] {
  return graphemes(str);
}

export function cpLen(str: string): number {
  return toCodePoints(str).length;
}

export function cpSlice(str: string, start: number, end?: number): string {
  return toCodePoints(str).slice(start, end).join('');
}

type Direction =
  | 'left'
  | 'right'
  | 'up'
  | 'down'
  | 'home'
  | 'end'
  | 'wordLeft'
  | 'wordRight';

interface VisualSegment {
  visualRow: number;
  startCol: number;
  endCol: number;
}

interface VisualLayout {
  visualLines: string[];
  logicalToVisual: VisualSegment[][];
  visualToLogical: Array<{ row: number; startCol: number; endCol: number }>;
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max);
}

function sanitizeInput(input: string): string {
  return toCodePoints(input.replace(/\r\n/g, '\n').replace(/\r/g, '\n'))
    .filter((char) => {
      const code = char.codePointAt(0);
      if (code === undefined) return false;
      if (char === '\n' || char === '\t') return true;
      if (code >= 0x00 && code <= 0x1f) return false;
      if (code === 0x7f) return false;
      if (code >= 0x80 && code <= 0x9f) return false;
      return true;
    })
    .join('');
}

function isWordChar(char: string | undefined): boolean {
  return char !== undefined && /[\w\p{L}\p{N}]/u.test(char);
}

function isWhitespace(char: string | undefined): boolean {
  return char !== undefined && /\s/u.test(char);
}

function calculateVisualLayout(lines: string[], width: number): VisualLayout {
  const safeWidth = Math.max(1, Math.floor(width));
  const visualLines: string[] = [];
  const logicalToVisual: VisualSegment[][] = [];
  const visualToLogical: Array<{ row: number; startCol: number; endCol: number }> = [];

  lines.forEach((line, row) => {
    const chars = toCodePoints(line);
    logicalToVisual[row] = [];

    if (chars.length === 0) {
      const visualRow = visualLines.length;
      logicalToVisual[row]!.push({ visualRow, startCol: 0, endCol: 0 });
      visualToLogical.push({ row, startCol: 0, endCol: 0 });
      visualLines.push('');
      return;
    }

    let startCol = 0;
    while (startCol < chars.length) {
      let endCol = startCol;
      let widthSoFar = 0;

      while (endCol < chars.length) {
        const charWidth = Math.max(0, graphemeWidth(chars[endCol]!));
        if (widthSoFar > 0 && widthSoFar + charWidth > safeWidth) {
          break;
        }
        widthSoFar += charWidth;
        endCol++;
        if (widthSoFar >= safeWidth) {
          break;
        }
      }

      if (endCol === startCol) {
        endCol++;
      }

      const visualRow = visualLines.length;
      const chunk = chars.slice(startCol, endCol).join('');
      logicalToVisual[row]!.push({ visualRow, startCol, endCol });
      visualToLogical.push({ row, startCol, endCol });
      visualLines.push(chunk);
      startCol = endCol;
    }
  });

  if (visualLines.length === 0) {
    visualLines.push('');
    logicalToVisual[0] = [{ visualRow: 0, startCol: 0, endCol: 0 }];
    visualToLogical.push({ row: 0, startCol: 0, endCol: 0 });
  }

  return { visualLines, logicalToVisual, visualToLogical };
}

function offsetToCursor(lines: string[], offset: number): [number, number] {
  let remaining = Math.max(0, offset);

  for (let row = 0; row < lines.length; row++) {
    const lineLength = cpLen(lines[row] ?? '');
    if (remaining <= lineLength) {
      return [row, remaining];
    }
    remaining -= lineLength;
    if (row < lines.length - 1) {
      if (remaining === 0) {
        return [row + 1, 0];
      }
      remaining -= 1;
    }
  }

  const lastRow = Math.max(0, lines.length - 1);
  return [lastRow, cpLen(lines[lastRow] ?? '')];
}

export class TextBuffer {
  private logicalLines: string[] = [''];
  private cursorRow = 0;
  private cursorCol = 0;
  private viewportHeight: number;
  private viewportWidth: number;
  private visualScrollRow = 0;
  private preferredVisualCol: number | null = null;
  private layout: VisualLayout;

  constructor(initialText = '', viewportHeight = 5, viewportWidth = 72) {
    this.viewportHeight = Math.max(1, viewportHeight);
    this.viewportWidth = Math.max(1, viewportWidth);
    this.logicalLines = this.normalizeLines(initialText);
    const lastRow = this.logicalLines.length - 1;
    this.cursorRow = lastRow;
    this.cursorCol = cpLen(this.logicalLines[lastRow] ?? '');
    this.layout = calculateVisualLayout(this.logicalLines, this.viewportWidth);
    this.ensureCursorVisible();
  }

  get text(): string {
    return this.logicalLines.join('\n');
  }

  get lines(): string[] {
    return [...this.logicalLines];
  }

  get cursor(): [number, number] {
    return [this.cursorRow, this.cursorCol];
  }

  get cursorOffset(): number {
    let offset = 0;
    for (let row = 0; row < this.cursorRow; row++) {
      offset += cpLen(this.logicalLines[row] ?? '') + 1;
    }
    return offset + this.cursorCol;
  }

  get visualCursor(): [number, number] {
    return this.logicalToVisual(this.cursorRow, this.cursorCol);
  }

  get allVisualLines(): string[] {
    return this.layout.visualLines;
  }

  get viewportVisualLines(): string[] {
    const end = Math.min(
      this.visualScrollRow + this.viewportHeight,
      this.layout.visualLines.length,
    );
    return this.layout.visualLines.slice(this.visualScrollRow, end);
  }

  get viewportLines(): string[] {
    return this.viewportVisualLines;
  }

  get scrollOffset(): number {
    return this.visualScrollRow;
  }

  get visualScrollOffset(): number {
    return this.visualScrollRow;
  }

  setViewport(width: number, height = this.viewportHeight): void {
    const nextWidth = Math.max(1, Math.floor(width));
    const nextHeight = Math.max(1, Math.floor(height));
    if (nextWidth === this.viewportWidth && nextHeight === this.viewportHeight) {
      return;
    }
    this.viewportWidth = nextWidth;
    this.viewportHeight = nextHeight;
    this.recalculateLayout();
  }

  setViewportHeight(height: number): void {
    this.setViewport(this.viewportWidth, height);
  }

  setText(text: string, options: { cursor?: 'start' | 'end' } = {}): void {
    this.logicalLines = this.normalizeLines(text);
    if (options.cursor === 'start') {
      this.cursorRow = 0;
      this.cursorCol = 0;
    } else {
      this.cursorRow = this.logicalLines.length - 1;
      this.cursorCol = cpLen(this.logicalLines[this.cursorRow] ?? '');
    }
    this.preferredVisualCol = null;
    this.recalculateLayout();
  }

  insert(text: string): void {
    const normalized = sanitizeInput(text);
    if (normalized.length === 0) {
      return;
    }
    this.replaceRange(this.cursorRow, this.cursorCol, this.cursorRow, this.cursorCol, normalized);
  }

  newline(): void {
    this.insert('\n');
  }

  backspace(): void {
    if (this.cursorRow === 0 && this.cursorCol === 0) {
      return;
    }

    if (this.cursorCol > 0) {
      this.replaceRange(
        this.cursorRow,
        this.cursorCol - 1,
        this.cursorRow,
        this.cursorCol,
        '',
      );
      return;
    }

    const previousRow = this.cursorRow - 1;
    const previousCol = cpLen(this.logicalLines[previousRow] ?? '');
    this.replaceRange(previousRow, previousCol, this.cursorRow, 0, '');
  }

  delete(): void {
    const lineLength = cpLen(this.logicalLines[this.cursorRow] ?? '');
    if (this.cursorCol < lineLength) {
      this.replaceRange(
        this.cursorRow,
        this.cursorCol,
        this.cursorRow,
        this.cursorCol + 1,
        '',
      );
      return;
    }

    if (this.cursorRow < this.logicalLines.length - 1) {
      this.replaceRange(this.cursorRow, this.cursorCol, this.cursorRow + 1, 0, '');
    }
  }

  move(direction: Direction): void {
    switch (direction) {
      case 'left':
        this.moveLeft();
        break;
      case 'right':
        this.moveRight();
        break;
      case 'up':
        this.moveVisualVertical(-1);
        break;
      case 'down':
        this.moveVisualVertical(1);
        break;
      case 'home':
        this.moveVisualHome();
        break;
      case 'end':
        this.moveVisualEnd();
        break;
      case 'wordLeft':
        this.moveWordLeft();
        break;
      case 'wordRight':
        this.moveWordRight();
        break;
    }
    this.ensureCursorVisible();
  }

  moveToOffset(offset: number): void {
    const [row, col] = offsetToCursor(this.logicalLines, offset);
    this.cursorRow = row;
    this.cursorCol = col;
    this.preferredVisualCol = null;
    this.ensureCursorVisible();
  }

  killLineRight(): void {
    const lineLength = cpLen(this.logicalLines[this.cursorRow] ?? '');
    if (this.cursorCol < lineLength) {
      this.replaceRange(this.cursorRow, this.cursorCol, this.cursorRow, lineLength, '');
    }
  }

  killLineLeft(): void {
    if (this.cursorCol > 0) {
      this.replaceRange(this.cursorRow, 0, this.cursorRow, this.cursorCol, '');
    }
  }

  deleteWordLeft(): void {
    if (this.cursorRow === 0 && this.cursorCol === 0) {
      return;
    }

    if (this.cursorCol === 0) {
      this.backspace();
      return;
    }

    const line = this.logicalLines[this.cursorRow] ?? '';
    const chars = toCodePoints(line);
    let start = this.cursorCol;

    while (start > 0 && isWhitespace(chars[start - 1])) {
      start--;
    }

    if (start > 0 && isWordChar(chars[start - 1])) {
      while (start > 0 && isWordChar(chars[start - 1])) {
        start--;
      }
    } else {
      while (
        start > 0 &&
        !isWhitespace(chars[start - 1]) &&
        !isWordChar(chars[start - 1])
      ) {
        start--;
      }
    }

    this.replaceRange(this.cursorRow, start, this.cursorRow, this.cursorCol, '');
  }

  replaceRangeByOffset(startOffset: number, endOffset: number, replacement: string): void {
    const [startRow, startCol] = offsetToCursor(this.logicalLines, startOffset);
    const [endRow, endCol] = offsetToCursor(this.logicalLines, endOffset);
    this.replaceRange(startRow, startCol, endRow, endCol, replacement);
  }

  handleInput(input: string): void {
    this.insert(input);
  }

  private normalizeLines(text: string): string[] {
    const lines = sanitizeInput(text).split('\n');
    return lines.length > 0 ? lines : [''];
  }

  private recalculateLayout(): void {
    this.clampCursor();
    this.layout = calculateVisualLayout(this.logicalLines, this.viewportWidth);
    this.ensureCursorVisible();
  }

  private replaceRange(
    startRow: number,
    startCol: number,
    endRow: number,
    endCol: number,
    replacement: string,
  ): void {
    const safeStartRow = clamp(startRow, 0, this.logicalLines.length - 1);
    const safeEndRow = clamp(endRow, 0, this.logicalLines.length - 1);
    const startLine = this.logicalLines[safeStartRow] ?? '';
    const endLine = this.logicalLines[safeEndRow] ?? '';
    const safeStartCol = clamp(startCol, 0, cpLen(startLine));
    const safeEndCol = clamp(endCol, 0, cpLen(endLine));
    const normalized = sanitizeInput(replacement);
    const replacementLines = normalized.split('\n');
    const prefix = cpSlice(startLine, 0, safeStartCol);
    const suffix = cpSlice(endLine, safeEndCol);

    if (replacementLines.length === 1) {
      this.logicalLines.splice(
        safeStartRow,
        safeEndRow - safeStartRow + 1,
        prefix + replacementLines[0] + suffix,
      );
      this.cursorRow = safeStartRow;
      this.cursorCol = cpLen(prefix) + cpLen(replacementLines[0] ?? '');
    } else {
      const first = prefix + (replacementLines[0] ?? '');
      const last = (replacementLines[replacementLines.length - 1] ?? '') + suffix;
      const middle = replacementLines.slice(1, -1);
      this.logicalLines.splice(
        safeStartRow,
        safeEndRow - safeStartRow + 1,
        first,
        ...middle,
        last,
      );
      this.cursorRow = safeStartRow + replacementLines.length - 1;
      this.cursorCol = cpLen(replacementLines[replacementLines.length - 1] ?? '');
    }

    if (this.logicalLines.length === 0) {
      this.logicalLines = [''];
    }
    this.preferredVisualCol = null;
    this.recalculateLayout();
  }

  private clampCursor(): void {
    this.cursorRow = clamp(this.cursorRow, 0, this.logicalLines.length - 1);
    this.cursorCol = clamp(
      this.cursorCol,
      0,
      cpLen(this.logicalLines[this.cursorRow] ?? ''),
    );
  }

  private logicalToVisual(row: number, col: number): [number, number] {
    const segments = this.layout.logicalToVisual[row] ?? this.layout.logicalToVisual[0] ?? [];
    if (segments.length === 0) {
      return [0, 0];
    }

    for (let index = 0; index < segments.length; index++) {
      const segment = segments[index]!;
      const isLast = index === segments.length - 1;
      if (col >= segment.startCol && (col < segment.endCol || isLast)) {
        return [
          segment.visualRow,
          clamp(col - segment.startCol, 0, segment.endCol - segment.startCol),
        ];
      }
    }

    const last = segments[segments.length - 1]!;
    return [last.visualRow, Math.max(0, last.endCol - last.startCol)];
  }

  private visualToLogical(row: number, col: number): [number, number] {
    const mapping = this.layout.visualToLogical[
      clamp(row, 0, this.layout.visualToLogical.length - 1)
    ];
    if (!mapping) {
      return [0, 0];
    }
    return [
      mapping.row,
      clamp(mapping.startCol + col, mapping.startCol, mapping.endCol),
    ];
  }

  private moveLeft(): void {
    this.preferredVisualCol = null;
    if (this.cursorCol > 0) {
      this.cursorCol--;
      return;
    }
    if (this.cursorRow > 0) {
      this.cursorRow--;
      this.cursorCol = cpLen(this.logicalLines[this.cursorRow] ?? '');
    }
  }

  private moveRight(): void {
    this.preferredVisualCol = null;
    const lineLength = cpLen(this.logicalLines[this.cursorRow] ?? '');
    if (this.cursorCol < lineLength) {
      this.cursorCol++;
      return;
    }
    if (this.cursorRow < this.logicalLines.length - 1) {
      this.cursorRow++;
      this.cursorCol = 0;
    }
  }

  private moveVisualVertical(delta: -1 | 1): void {
    const [visualRow, visualCol] = this.visualCursor;
    const targetRow = visualRow + delta;
    if (targetRow < 0 || targetRow >= this.layout.visualLines.length) {
      return;
    }
    const preferredCol = this.preferredVisualCol ?? visualCol;
    const targetLineLength = cpLen(this.layout.visualLines[targetRow] ?? '');
    const [row, col] = this.visualToLogical(targetRow, clamp(preferredCol, 0, targetLineLength));
    this.cursorRow = row;
    this.cursorCol = col;
    this.preferredVisualCol = preferredCol;
  }

  private moveVisualHome(): void {
    const [visualRow] = this.visualCursor;
    const [row, col] = this.visualToLogical(visualRow, 0);
    this.cursorRow = row;
    this.cursorCol = col;
    this.preferredVisualCol = null;
  }

  private moveVisualEnd(): void {
    const [visualRow] = this.visualCursor;
    const lineLength = cpLen(this.layout.visualLines[visualRow] ?? '');
    const [row, col] = this.visualToLogical(visualRow, lineLength);
    this.cursorRow = row;
    this.cursorCol = col;
    this.preferredVisualCol = null;
  }

  private moveWordLeft(): void {
    if (this.cursorRow === 0 && this.cursorCol === 0) {
      return;
    }
    if (this.cursorCol === 0) {
      this.cursorRow--;
      this.cursorCol = cpLen(this.logicalLines[this.cursorRow] ?? '');
      this.preferredVisualCol = null;
      return;
    }

    const chars = toCodePoints(this.logicalLines[this.cursorRow] ?? '');
    let col = this.cursorCol;
    while (col > 0 && isWhitespace(chars[col - 1])) col--;
    while (col > 0 && isWordChar(chars[col - 1])) col--;
    if (col === this.cursorCol) {
      while (
        col > 0 &&
        !isWhitespace(chars[col - 1]) &&
        !isWordChar(chars[col - 1])
      ) {
        col--;
      }
    }
    this.cursorCol = col;
    this.preferredVisualCol = null;
  }

  private moveWordRight(): void {
    const chars = toCodePoints(this.logicalLines[this.cursorRow] ?? '');
    if (this.cursorCol >= chars.length) {
      if (this.cursorRow < this.logicalLines.length - 1) {
        this.cursorRow++;
        this.cursorCol = 0;
      }
      this.preferredVisualCol = null;
      return;
    }

    let col = this.cursorCol;
    if (isWordChar(chars[col])) {
      while (col < chars.length && isWordChar(chars[col])) col++;
    } else {
      while (
        col < chars.length &&
        !isWhitespace(chars[col]) &&
        !isWordChar(chars[col])
      ) {
        col++;
      }
    }
    while (col < chars.length && isWhitespace(chars[col])) col++;
    this.cursorCol = col;
    this.preferredVisualCol = null;
  }

  private ensureCursorVisible(): void {
    const [visualRow] = this.visualCursor;
    if (visualRow < this.visualScrollRow) {
      this.visualScrollRow = visualRow;
    } else if (visualRow >= this.visualScrollRow + this.viewportHeight) {
      this.visualScrollRow = visualRow - this.viewportHeight + 1;
    }

    const maxScroll = Math.max(0, this.layout.visualLines.length - this.viewportHeight);
    this.visualScrollRow = clamp(this.visualScrollRow, 0, maxScroll);
  }
}

export function useTextBuffer(initialText = '', viewportHeight = 5, viewportWidth = 72): TextBuffer {
  const bufferRef = useRef<TextBuffer | null>(null);

  if (!bufferRef.current) {
    bufferRef.current = new TextBuffer(initialText, viewportHeight, viewportWidth);
  }

  return bufferRef.current;
}
