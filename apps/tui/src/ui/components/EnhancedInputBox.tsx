import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { Box, Text, useInput, usePaste, useStdin, useStdout, type Key } from 'ink';
import { isMouseInput } from '../../input/mouse-wheel.js';
import { CommandCompletion, CommandHistory, DEFAULT_COMMANDS } from '../keybindings.js';
import { inkColors } from '../theme.js';
import { TextBuffer, cpLen, cpSlice, toCodePoints } from './text-buffer.js';
import {
  SlashCommandPopover,
  filterSlashCommands,
} from '../SlashCommandPopover.js';
import { commandProcessor } from '../../commands/CommandProcessor.js';

interface EnhancedInputBoxProps {
  value?: string;
  onChange: (value: string) => void;
  onSubmit: (value: string) => void;
  onFocusChange?: (focused: boolean) => void;
  onClearScreen?: () => void;
  onNewSession?: () => void;
  onExitRequest?: (clearInputDraft: () => boolean) => void;
  onRestoreQueuedMessages?: () => string | null;
  ctrlCExitPending?: boolean | undefined;
  onLayoutChange?: (rows: number) => void;
  disabled?: boolean;
  commands?: string[];
  placeholder?: string | undefined;
  modelName?: string | undefined;
  datasourceId?: string | undefined;
  skillId?: string | undefined;
  inputWidth?: number | undefined;
  outputCount?: number | undefined;
  history?: CommandHistory | undefined;
  onShortcut?: ((input: string) => boolean) | undefined;
}

const INPUT_VIEWPORT_HEIGHT = 3;
const LARGE_PASTE_CHAR_THRESHOLD = 1000;
const LARGE_PASTE_LINE_THRESHOLD = 10;

function inputBoxRowsFor(renderedInputRows: number): number {
  // Full border + vertical input padding + divider + metadata row.
  return Math.max(1, renderedInputRows) + 6;
}

export const ENHANCED_INPUT_RESERVED_ROWS = inputBoxRowsFor(INPUT_VIEWPORT_HEIGHT);

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function isModifiedReturn(key: Key): boolean {
  return key.return && (key.shift || key.ctrl || key.meta || key.super);
}

function isPlainReturn(key: Key): boolean {
  return key.return && !key.shift && !key.ctrl && !key.meta && !key.super;
}

function isNavigationKey(key: Key): boolean {
  return (
    key.upArrow ||
    key.downArrow ||
    key.leftArrow ||
    key.rightArrow ||
    key.pageUp ||
    key.pageDown ||
    key.home ||
    key.end
  );
}

function rawBackspaceCount(input: string): number {
  if (!input) return 0;
  let count = 0;
  for (const char of input) {
    if (char !== '\x7f' && char !== '\b') {
      return 0;
    }
    count++;
  }
  return count;
}

function isLikelyPaste(input: string, key: Key): boolean {
  if (
    !input
    || rawBackspaceCount(input) > 0
    || key.return
    || key.backspace
    || key.delete
    || key.escape
    || isNavigationKey(key)
  ) {
    return false;
  }
  return input.includes('\n') || cpLen(input) > LARGE_PASTE_CHAR_THRESHOLD;
}

function isPrintableInput(input: string, key: Key): boolean {
  if (!input || key.ctrl || key.meta || key.super || key.escape || isNavigationKey(key)) {
    return false;
  }
  if (key.return || key.backspace || key.delete || key.tab) {
    return false;
  }
  if (rawBackspaceCount(input) > 0) {
    return false;
  }
  for (const char of toCodePoints(input)) {
    const code = char.codePointAt(0);
    if (code === undefined) return false;
    if (code <= 0x1f || code === 0x7f || (code >= 0x80 && code <= 0x9f)) {
      return false;
    }
  }
  return true;
}

export interface InputLineFragment {
  text: string;
  isPastePlaceholder: boolean;
  hasCursor: boolean;
}

export function inputLineFragments(
  lineText: string,
  cursorPos: number | null,
): InputLineFragment[] {
  const chars = toCodePoints(lineText);
  const placeholderMask = Array.from({ length: chars.length }, () => false);

  for (const match of lineText.matchAll(/\[Pasted Content \d+ chars\](?: #\d+)?/g)) {
    const start = cpLen(lineText.slice(0, match.index));
    const end = start + cpLen(match[0]);
    placeholderMask.fill(true, start, end);
  }

  const fragments: InputLineFragment[] = [];
  chars.forEach((char, index) => {
    const isPastePlaceholder = placeholderMask[index] ?? false;
    const hasCursor = cursorPos === index;
    const previous = fragments.at(-1);
    if (
      previous
      && previous.isPastePlaceholder === isPastePlaceholder
      && previous.hasCursor === hasCursor
    ) {
      previous.text += char;
      return;
    }
    fragments.push({ text: char, isPastePlaceholder, hasCursor });
  });
  return fragments;
}

export const EnhancedInputBox: React.FC<EnhancedInputBoxProps> = ({
  value,
  onChange,
  onSubmit,
  onFocusChange,
  onClearScreen,
  onNewSession,
  onExitRequest,
  onRestoreQueuedMessages,
  ctrlCExitPending = false,
  onLayoutChange,
  disabled = false,
  commands = DEFAULT_COMMANDS,
  placeholder = 'Ask about your data... "Show tables"',
  modelName,
  datasourceId,
  skillId,
  inputWidth,
  outputCount = 0,
  history,
  onShortcut,
}) => {
  const [, forceRender] = useState(0);
  const [completionHint, setCompletionHint] = useState('');
  const [showSlashPopover, setShowSlashPopover] = useState(false);
  const [slashPopoverActiveIndex, setSlashPopoverActiveIndex] = useState(0);
  const pendingPastesRef = useRef<Map<string, string>>(new Map());
  const activePlaceholderIds = useRef<Map<number, Set<number>>>(new Map());
  const localHistoryRef = useRef(new CommandHistory());
  const commandHistory = history ?? localHistoryRef.current;
  const completionRef = useRef(new CommandCompletion(commands));
  const { isRawModeSupported } = useStdin();
  const { stdout } = useStdout();
  const fallbackWidth = stdout.columns ?? process.stdout.columns ?? 80;
  const composerWidth = Math.max(12, Math.floor(inputWidth ?? fallbackWidth));
  const visualWidth = Math.max(12, composerWidth - 6);
  const bufferRef = useRef<TextBuffer>(new TextBuffer('', INPUT_VIEWPORT_HEIGHT, visualWidth));
  const buffer = bufferRef.current;
  const inputIsActive = isRawModeSupported;

  const availableCommands = React.useMemo(() => commandProcessor.getCommands(), []);

  const accent = disabled ? inkColors.muted : inkColors.focus;
  const borderColor = disabled ? inkColors.muted : (inputIsActive ? inkColors.focus : inkColors.border);
  const metaParts = [
    datasourceId || 'no datasource',
    skillId,
  ].filter((part): part is string => Boolean(part));

  const redraw = useCallback(() => {
    forceRender((version) => version + 1);
  }, []);

  const lastReportedLayoutRows = useRef<number | null>(null);
  const reportLayoutRows = useCallback((rows: number) => {
    if (lastReportedLayoutRows.current === rows) return;
    lastReportedLayoutRows.current = rows;
    onLayoutChange?.(rows);
  }, [onLayoutChange]);

  const setPendingPaste = useCallback((placeholderText: string, pasted: string) => {
    pendingPastesRef.current.set(placeholderText, pasted);
  }, []);

  const clearPendingPastes = useCallback(() => {
    pendingPastesRef.current.clear();
    activePlaceholderIds.current.clear();
  }, []);

  const resetCompletion = useCallback(() => {
    completionRef.current.reset();
    setCompletionHint('');
  }, []);

  const updateCompletionHint = useCallback((text: string) => {
    const completions = completionRef.current.getCompletions(text);
    if (completions.length > 0) {
      setCompletionHint(
        `Tab: ${completions.slice(0, 3).join(', ')}${completions.length > 3 ? '...' : ''}`,
      );
    } else {
      setCompletionHint('');
    }
  }, []);

  const getSlashQuery = useCallback((): string | undefined => {
    return buffer.text.match(/^\/([^\s]*)$/u)?.[1];
  }, [buffer]);

  const getFilteredCommands = useCallback(() => (
    filterSlashCommands(availableCommands, getSlashQuery() ?? '')
  ), [availableCommands, getSlashQuery]);

  const currentLayoutRows = useCallback(() => ENHANCED_INPUT_RESERVED_ROWS, []);

  const syncChange = useCallback(() => {
    onChange(buffer.text);
    reportLayoutRows(currentLayoutRows());
    redraw();
  }, [buffer, currentLayoutRows, onChange, redraw, reportLayoutRows]);

  const selectSlashCommand = useCallback((index: number) => {
    const filtered = getFilteredCommands();
    const cmd = filtered[index];
    if (cmd) {
      buffer.setText(`/${cmd.name} `);
      buffer.move('end');
      setShowSlashPopover(false);
      setSlashPopoverActiveIndex(0);
      resetCompletion();
      syncChange();
    }
  }, [buffer, getFilteredCommands, resetCompletion, syncChange]);

  useEffect(() => {
    if (getSlashQuery() !== undefined && !disabled) {
      setShowSlashPopover(true);
      setSlashPopoverActiveIndex(0);
    } else {
      setShowSlashPopover(false);
    }
  }, [buffer.text, disabled, getSlashQuery]);

  const parsePlaceholder = useCallback(
    (placeholderText: string): { charCount: number; id: number } | null => {
      const match = placeholderText.match(/^\[Pasted Content (\d+) chars\](?: #(\d+))?$/);
      if (!match) return null;
      const charCount = Number.parseInt(match[1]!, 10);
      const id = match[2] ? Number.parseInt(match[2], 10) : 1;
      return { charCount, id };
    },
    [],
  );

  const freePlaceholderId = useCallback((charCount: number, id: number) => {
    const activeIds = activePlaceholderIds.current.get(charCount);
    if (!activeIds) return;
    activeIds.delete(id);
    if (activeIds.size === 0) {
      activePlaceholderIds.current.delete(charCount);
    }
  }, []);

  const nextLargePastePlaceholder = useCallback((charCount: number): string => {
    const activeIds = activePlaceholderIds.current.get(charCount) ?? new Set<number>();
    let id = 1;
    while (activeIds.has(id)) {
      id++;
    }
    activeIds.add(id);
    activePlaceholderIds.current.set(charCount, activeIds);

    const base = `[Pasted Content ${charCount} chars]`;
    return id === 1 ? base : `${base} #${id}`;
  }, []);

  const expandPendingPastes = useCallback((text: string): string => {
    if (pendingPastesRef.current.size === 0) {
      return text;
    }

    const placeholders = Array.from(pendingPastesRef.current.keys()).sort(
      (a, b) => b.length - a.length,
    );
    const regex = new RegExp(placeholders.map(escapeRegExp).join('|'), 'g');
    return text.replace(
      regex,
      (matchedPlaceholder) => pendingPastesRef.current.get(matchedPlaceholder) ?? matchedPlaceholder,
    );
  }, []);

  const submitText = useCallback((rawText: string) => {
    const trimmed = rawText.trim();
    if (!trimmed) {
      return;
    }
    if (disabled && !trimmed.startsWith('/')) {
      return;
    }

    const finalValue = expandPendingPastes(rawText);
    commandHistory.add(finalValue);
    commandHistory.reset();
    completionRef.current.reset();
    clearPendingPastes();
    setCompletionHint('');
    buffer.setText('');
    onChange('');
    reportLayoutRows(currentLayoutRows());
    redraw();
    onSubmit(finalValue);
  }, [
    buffer,
    clearPendingPastes,
    commandHistory,
    currentLayoutRows,
    disabled,
    expandPendingPastes,
    onChange,
    onSubmit,
    redraw,
    reportLayoutRows,
  ]);

  const submitBuffer = useCallback(() => {
    submitText(buffer.text);
  }, [buffer, submitText]);

  const submitSlashCommand = useCallback((index: number) => {
    const command = getFilteredCommands()[index];
    if (!command) return;
    setShowSlashPopover(false);
    setSlashPopoverActiveIndex(0);
    submitText(`/${command.name}`);
  }, [getFilteredCommands, submitText]);

  const clearBuffer = useCallback(() => {
    buffer.setText('');
    clearPendingPastes();
    resetCompletion();
    onChange('');
    reportLayoutRows(currentLayoutRows());
    redraw();
  }, [
    buffer,
    clearPendingPastes,
    currentLayoutRows,
    onChange,
    redraw,
    reportLayoutRows,
    resetCompletion,
  ]);

  const handlePaste = useCallback(
    (input: string) => {
      const pasted = input.replace(/\r\n/g, '\n').replace(/\r/g, '\n');
      const charCount = cpLen(pasted);
      const lineCount = pasted.split('\n').length;

      if (charCount > LARGE_PASTE_CHAR_THRESHOLD || lineCount > LARGE_PASTE_LINE_THRESHOLD) {
        const placeholderText = nextLargePastePlaceholder(charCount);
        setPendingPaste(placeholderText, pasted);
        buffer.insert(placeholderText);
      } else {
        buffer.insert(pasted);
      }

      syncChange();
      updateCompletionHint(buffer.text);
      completionRef.current.reset();
    },
    [buffer, nextLargePastePlaceholder, setPendingPaste, syncChange, updateCompletionHint],
  );

  // Bracketed paste keeps pasted newlines and control-like characters out of
  // the normal key handler. Without it, terminal paste shortcuts can be
  // parsed as individual key presses (and appear to do nothing).
  usePaste(handlePaste, { isActive: inputIsActive });

  const deletePlaceholderBeforeCursor = useCallback((): boolean => {
    if (pendingPastesRef.current.size === 0) {
      return false;
    }

    const cursorOffset = buffer.cursorOffset;
    const textBeforeCursor = cpSlice(buffer.text, 0, cursorOffset);

    for (const placeholderText of pendingPastesRef.current.keys()) {
      if (!textBeforeCursor.endsWith(placeholderText)) {
        continue;
      }
      const startOffset = cursorOffset - cpLen(placeholderText);
      buffer.replaceRangeByOffset(startOffset, cursorOffset, '');
      const parsed = parsePlaceholder(placeholderText);
      if (parsed) {
        freePlaceholderId(parsed.charCount, parsed.id);
      }
      pendingPastesRef.current.delete(placeholderText);
      syncChange();
      resetCompletion();
      return true;
    }

    return false;
  }, [buffer, freePlaceholderId, parsePlaceholder, resetCompletion, syncChange]);

  const backspaceOnce = useCallback(() => {
    if (deletePlaceholderBeforeCursor()) {
      return;
    }
    buffer.backspace();
  }, [buffer, deletePlaceholderBeforeCursor]);

  const restoreHistory = useCallback(
    (text: string | null, cursor: 'start' | 'end') => {
      if (text === null) {
        return;
      }
      buffer.setText(text, { cursor });
      clearPendingPastes();
      resetCompletion();
      onChange(buffer.text);
      reportLayoutRows(currentLayoutRows());
      redraw();
    },
    [
      buffer,
      clearPendingPastes,
      currentLayoutRows,
      onChange,
      redraw,
      reportLayoutRows,
      resetCompletion,
    ],
  );

  const restoreQueuedMessages = useCallback((): boolean => {
    const queuedText = onRestoreQueuedMessages?.();
    if (!queuedText) {
      return false;
    }

    const currentText = buffer.text;
    const currentCursorOffset = buffer.cursorOffset;
    if (currentText.length > 0) {
      buffer.setText(`${queuedText}\n${currentText}`);
      buffer.moveToOffset(cpLen(queuedText) + 1 + currentCursorOffset);
    } else {
      buffer.setText(queuedText);
    }

    clearPendingPastes();
    resetCompletion();
    onChange(buffer.text);
    reportLayoutRows(currentLayoutRows());
    redraw();
    return true;
  }, [
    buffer,
    clearPendingPastes,
    currentLayoutRows,
    onChange,
    onRestoreQueuedMessages,
    redraw,
    reportLayoutRows,
    resetCompletion,
  ]);

  useEffect(() => {
    completionRef.current.setCommands(commands);
  }, [commands]);

  useEffect(() => {
    buffer.setViewport(visualWidth, INPUT_VIEWPORT_HEIGHT);
    redraw();
  }, [buffer, redraw, visualWidth]);

  useEffect(() => {
    if (value !== undefined && value !== buffer.text) {
      buffer.setText(value);
      redraw();
    }
  }, [buffer, redraw, value]);

  useEffect(() => {
    onFocusChange?.(inputIsActive);
    return () => onFocusChange?.(false);
  }, [inputIsActive, onFocusChange]);

  useInput(
    (input, key) => {
      if (isMouseInput(input)) {
        return;
      }

      const backspaceCount = rawBackspaceCount(input);
      if (backspaceCount > 0) {
        for (let index = 0; index < backspaceCount; index++) {
          backspaceOnce();
        }
        syncChange();
        resetCompletion();
        return;
      }

      if (isLikelyPaste(input, key)) {
        handlePaste(input);
        return;
      }

      if (isModifiedReturn(key) || (key.ctrl && input === 'j')) {
        buffer.newline();
        syncChange();
        resetCompletion();
        return;
      }

      if (isPlainReturn(key)) {
        if (showSlashPopover) {
          const filtered = getFilteredCommands();
          if (filtered.length > 0) {
            submitSlashCommand(slashPopoverActiveIndex);
            return;
          }
          setShowSlashPopover(false);
        }
        submitBuffer();
        return;
      }

      if (key.ctrl && input === 'c') {
        if (onExitRequest) {
          onExitRequest(() => {
            if (buffer.text.length === 0) {
              return false;
            }
            clearBuffer();
            return true;
          });
        } else if (buffer.text.length > 0) {
          clearBuffer();
        }
        return;
      }

      if (key.ctrl && input === 'l') {
        onClearScreen?.();
        return;
      }

      if (key.ctrl && input === 'n') {
        onNewSession?.();
        return;
      }

      if (key.escape) {
        if (showSlashPopover) {
          setShowSlashPopover(false);
          setSlashPopoverActiveIndex(0);
          return;
        }
        if (completionHint) {
          resetCompletion();
        } else if (restoreQueuedMessages()) {
          return;
        } else if (buffer.text.length > 0) {
          clearBuffer();
        }
        return;
      }

      if (key.ctrl && input === 'u') {
        buffer.killLineLeft();
        syncChange();
        resetCompletion();
        return;
      }

      if (key.ctrl && input === 'k') {
        buffer.killLineRight();
        syncChange();
        resetCompletion();
        return;
      }

      if (key.ctrl && input === 'w') {
        buffer.deleteWordLeft();
        syncChange();
        resetCompletion();
        return;
      }

      if ((key.ctrl || key.meta) && key.leftArrow) {
        buffer.move('wordLeft');
        redraw();
        return;
      }

      if ((key.ctrl || key.meta) && key.rightArrow) {
        buffer.move('wordRight');
        redraw();
        return;
      }

      if (key.ctrl && input === 'a') {
        buffer.move('home');
        redraw();
        return;
      }

      if (key.ctrl && input === 'e') {
        buffer.move('end');
        redraw();
        return;
      }

      if (key.home) {
        buffer.move('home');
        redraw();
        return;
      }

      if (key.end) {
        buffer.move('end');
        redraw();
        return;
      }

      // Ink reports the DEL byte (\x7f), which most terminals send for the
      // Backspace key, as `key.delete`. Treat both as backward deletion.
      if (key.backspace || key.delete) {
        backspaceOnce();
        syncChange();
        resetCompletion();
        return;
      }

      if (key.leftArrow) {
        buffer.move('left');
        redraw();
        return;
      }

      if (key.rightArrow) {
        buffer.move('right');
        redraw();
        return;
      }

      if (key.upArrow || (key.ctrl && input === 'p')) {
        if (showSlashPopover) {
          const filtered = getFilteredCommands();
          if (filtered.length > 0) {
            setSlashPopoverActiveIndex((previous) => Math.max(0, previous - 1));
          }
          return;
        }

        const [visualRow, visualCol] = buffer.visualCursor;
        if (visualRow > 0) {
          buffer.move('up');
          redraw();
          return;
        }
        if (visualCol > 0) {
          buffer.move('home');
          redraw();
          return;
        }
        if (restoreQueuedMessages()) {
          return;
        }
        restoreHistory(commandHistory.previous(buffer.text), 'start');
        return;
      }

      if (key.downArrow) {
        if (showSlashPopover) {
          const filtered = getFilteredCommands();
          if (filtered.length > 0) {
            setSlashPopoverActiveIndex((previous) => (
              Math.min(filtered.length - 1, previous + 1)
            ));
          }
          return;
        }

        const [visualRow, visualCol] = buffer.visualCursor;
        const lastVisualRow = buffer.allVisualLines.length - 1;
        const lastVisualLineLength = cpLen(buffer.allVisualLines[lastVisualRow] ?? '');
        if (visualRow < lastVisualRow) {
          buffer.move('down');
          redraw();
          return;
        }
        if (visualCol < lastVisualLineLength) {
          buffer.move('end');
          redraw();
          return;
        }
        restoreHistory(commandHistory.next(), 'end');
        return;
      }

      if (key.tab) {
        if (showSlashPopover) {
          const filtered = getFilteredCommands();
          if (filtered.length > 0) {
            selectSlashCommand(slashPopoverActiveIndex);
          } else {
            setShowSlashPopover(false);
          }
          return;
        }

        const completion = completionRef.current.complete(buffer.text);
        if (completion !== null) {
          buffer.setText(completion);
          onChange(buffer.text);
          reportLayoutRows(currentLayoutRows());
          redraw();
          setCompletionHint('');
        } else {
          const completions = completionRef.current.getCompletions(buffer.text);
          setCompletionHint(completions.length > 0 ? completions.join(', ') : '');
        }
        return;
      }

      if (!isPrintableInput(input, key)) {
        return;
      }

      if (buffer.text.length === 0 && onShortcut?.(input)) {
        resetCompletion();
        return;
      }

      buffer.insert(input);
      syncChange();
      updateCompletionHint(buffer.text);
      completionRef.current.reset();
    },
    { isActive: inputIsActive },
  );

  const lines = buffer.viewportVisualLines;
  const [cursorVisualRow, cursorVisualCol] = buffer.visualCursor;
  const relativeCursorRow = cursorVisualRow - buffer.visualScrollOffset;
  const placeholderFirst = cpSlice(placeholder, 0, 1);
  const placeholderRest = cpSlice(placeholder, 1);
  const layoutRows = currentLayoutRows();
  const showNewlineHint = composerWidth >= 96;
  const showSendHint = composerWidth >= 40;
  const showOutputCount = outputCount > 0 && composerWidth >= 64;
  const layoutSignature = [
    layoutRows,
    visualWidth,
    metaParts.join('\u0000'),
  ].join(':');
  const isBufferEmpty = buffer.text.length === 0;

  useLayoutEffect(() => {
    reportLayoutRows(layoutRows);
  }, [layoutRows, layoutSignature, reportLayoutRows]);

  /**
   * Unified rendering function for all input lines.
   * This approach eliminates conditional rendering branches that can cause
   * incomplete terminal redraws, leading to visual artifacts (ghosting).
   */
  const renderInputLine = (index: number) => {
    // Determine line content based on buffer state
    let lineText: string;
    let shouldShowCursor = false;

    if (isBufferEmpty) {
      // Empty buffer: show placeholder on first line, padding on others
      lineText = index === 0 ? placeholder : '';
    } else {
      // Buffer has content: show actual lines
      lineText = lines[index] || '';
      shouldShowCursor = index === relativeCursorRow && !disabled;
    }

    // Render placeholder line (first line when empty)
    if (isBufferEmpty && index === 0) {
      return (
        <Box key={`input-line-${index}`} minHeight={1}>
          <Text color={inkColors.muted} wrap="truncate-end">
            {!disabled ? <Text inverse>{placeholderFirst || ' '}</Text> : placeholderFirst}
            {placeholderRest}
          </Text>
        </Box>
      );
    }

    // Render empty line (padding lines or empty content lines)
    if (!lineText || lineText === '') {
      return (
        <Box key={`input-line-${index}`} minHeight={1}>
          <Text wrap="truncate-end"> </Text>
        </Box>
      );
    }

    const codePoints = toCodePoints(lineText);
    const cursorPos = shouldShowCursor
      ? Math.min(cursorVisualCol, codePoints.length)
      : null;
    const fragments = inputLineFragments(lineText, cursorPos);
    const textColor = disabled ? inkColors.muted : inkColors.text;

    return (
      <Box key={`input-line-${index}`} minHeight={1}>
        <Text color={textColor} wrap="truncate-end">
          {fragments.map((fragment, fragmentIndex) => (
            <Text
              key={`${index}:${fragmentIndex}`}
              color={fragment.isPastePlaceholder ? inkColors.accent : textColor}
              bold={fragment.isPastePlaceholder}
              inverse={fragment.hasCursor}
            >
              {fragment.text}
            </Text>
          ))}
          {cursorPos === codePoints.length && <Text inverse> </Text>}
        </Text>
      </Box>
    );
  };
  return (
    <Box
      flexDirection="column"
      flexShrink={0}
      height={ENHANCED_INPUT_RESERVED_ROWS}
      width="100%"
      position="relative"
    >
      {showSlashPopover && !disabled && (
        <Box
          position="absolute"
          left={0}
          right={0}
          bottom={ENHANCED_INPUT_RESERVED_ROWS}
        >
          <SlashCommandPopover
            commands={getFilteredCommands()}
            activeIndex={slashPopoverActiveIndex}
          />
        </Box>
      )}

      <Box
        flexDirection="column"
        width="100%"
        borderStyle="single"
        borderColor={borderColor}
      >
        {/* 主输入区域 */}
        <Box
          flexDirection="row"
          width="100%"
          paddingLeft={1}
          paddingRight={2}
          paddingY={1}
        >
          <Text color={accent} bold>›</Text>
          <Box
            flexDirection="column"
            flexGrow={1}
            paddingLeft={1}
          >
            <Box
              flexDirection="column"
              height={INPUT_VIEWPORT_HEIGHT}
              overflowY="hidden"
              flexShrink={0}
            >
              {Array.from({ length: INPUT_VIEWPORT_HEIGHT }).map((_, index) => renderInputLine(index))}
            </Box>
          </Box>
        </Box>

        {/* 分隔线 */}
        <Box
          width="100%"
          borderStyle="single"
          borderTop={true}
          borderBottom={false}
          borderLeft={false}
          borderRight={false}
          borderColor={inkColors.border}
        />

        {/* 底部元数据栏 */}
        <Box
          flexDirection="row"
          width="100%"
          paddingX={1}
          paddingY={0}
          justifyContent="space-between"
        >
          {ctrlCExitPending ? (
            <Text color={inkColors.warning} wrap="truncate-end">
              Press Ctrl+C again to exit.
            </Text>
          ) : (
            <>
              <Box flexDirection="row" flexGrow={1} flexShrink={1} minWidth={0} marginRight={1}>
                <Text wrap="truncate-end">
                  {!disabled && completionHint && !showSlashPopover ? (
                    <Text color={inkColors.muted}>{completionHint}</Text>
                  ) : (
                    <>
                      <Text color={inkColors.muted}>ANALYZE / </Text>
                      <Text color={disabled ? inkColors.muted : inkColors.text}>
                        {datasourceId || 'no datasource'}
                      </Text>
                      {skillId && (
                        <>
                          <Text color={inkColors.muted}> / </Text>
                          <Text color={disabled ? inkColors.muted : inkColors.text}>{skillId}</Text>
                        </>
                      )}
                    </>
                  )}
                </Text>
              </Box>
              {showSendHint && (
                <Box flexDirection="row" flexShrink={0}>
                  {showOutputCount && (
                    <>
                      <Text color={inkColors.accent}>Outputs {outputCount}</Text>
                      <Text color={inkColors.muted}>  </Text>
                    </>
                  )}
                  {showNewlineHint && (
                    <>
                      <Text color={inkColors.muted}>[Shift+Enter]</Text>
                      <Text color={inkColors.muted}> new line  </Text>
                    </>
                  )}
                  <Text color={inkColors.muted}>[Enter]</Text>
                  <Text color={inkColors.text}> send</Text>
                </Box>
              )}
            </>
          )}
        </Box>
      </Box>
    </Box>
  );
};
