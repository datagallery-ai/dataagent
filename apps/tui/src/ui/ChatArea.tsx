import React, {
  forwardRef,
  memo,
  useCallback,
  useEffect,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
} from 'react';
import { Box, Text } from 'ink';
import type { DataArtifact, DisplayMessage, LiveToolCallRecord } from '../state/index.js';
import {
  buildChatLines,
  chatContentWidth,
  countChatLines,
  type StartupInfo,
} from './transcript-lines.js';
import { ScrollAnchor } from './timeline/scroll-anchor.js';

export { chatContentWidth, countChatLines };
export type { StartupInfo };

interface ChatAreaProps {
  messages: DisplayMessage[];
  artifacts: DataArtifact[];
  toolCalls?: LiveToolCallRecord[];
  totalMessageCount?: number;
  maxMessageContentLength?: number | undefined;
  viewportRows?: number;
  scrollbackRows?: number;
  columns?: number;
  startup?: StartupInfo | undefined;
  compactMode?: boolean | undefined;
  thoughtExpanded?: boolean | undefined;
}

export type ChatAreaRef = {
  scrollBy(delta: number): void;
  scrollToTop(): void;
  scrollToBottom(): void;
  reset(): void;
  getScrollbackRows(): number;
};

/**
 * Chat transcript viewport.
 *
 * The transcript is rendered into a flat list of single-row lines (see
 * {@link buildChatLines}), then the visible window is an exact slice of that
 * list. Because each line is pre-wrapped to the content width, Ink renders one
 * terminal row per line and the row count is deterministic - so scrolling is a
 * pure integer slice with no height estimation and no negative-margin cropping.
 */
const ChatAreaComponent = forwardRef<ChatAreaRef, ChatAreaProps>(({
  messages,
  artifacts,
  toolCalls = [],
  totalMessageCount,
  maxMessageContentLength,
  viewportRows,
  scrollbackRows,
  columns = 100,
  startup,
  compactMode = false,
  thoughtExpanded = false,
}, ref) => {
  const scrollAnchor = useRef(new ScrollAnchor());
  const [internalScrollbackRows, setInternalScrollbackRows] = useState(
    scrollbackRows ?? 0,
  );

  const lines = useMemo(() => buildChatLines({
    messages,
    artifacts,
    toolCalls,
    totalMessageCount,
    maxMessageContentLength,
    columns,
    startup,
    compactMode,
    thoughtExpanded,
  }), [
    messages,
    artifacts,
    toolCalls,
    totalMessageCount,
    maxMessageContentLength,
    columns,
    startup,
    compactMode,
    thoughtExpanded,
  ]);

  const viewport = viewportRows === undefined ? undefined : Math.max(0, viewportRows);
  const total = lines.length;
  const maxScroll = viewport === undefined ? 0 : Math.max(0, total - viewport);
  const controlled = scrollbackRows !== undefined;
  const effectiveScrollbackRows = Math.max(
    0,
    Math.min(controlled ? scrollbackRows : internalScrollbackRows, maxScroll),
  );

  const updateScrollbackRows = useCallback((nextValue: number | ((current: number) => number)) => {
    if (controlled || viewport === undefined) return;

    setInternalScrollbackRows((current) => {
      const raw = typeof nextValue === 'function' ? nextValue(current) : nextValue;
      const next = Math.max(0, Math.min(maxScroll, raw));
      scrollAnchor.current.handleUserScroll(next);
      return next;
    });
  }, [controlled, maxScroll, viewport]);

  useImperativeHandle(ref, () => ({
    scrollBy(delta: number): void {
      updateScrollbackRows((current) => current + delta);
    },
    scrollToTop(): void {
      updateScrollbackRows(maxScroll);
    },
    scrollToBottom(): void {
      scrollAnchor.current.jumpToLatest();
      updateScrollbackRows(0);
    },
    reset(): void {
      scrollAnchor.current.reset();
      updateScrollbackRows(0);
    },
    getScrollbackRows(): number {
      return effectiveScrollbackRows;
    },
  }), [effectiveScrollbackRows, maxScroll, updateScrollbackRows]);

  useEffect(() => {
    if (controlled || viewport === undefined) return;

    setInternalScrollbackRows((current) => {
      const adjustedScrollback = scrollAnchor.current.handleContentGrowth(total, current);
      return Math.max(0, Math.min(maxScroll, adjustedScrollback));
    });
  }, [controlled, maxScroll, total, viewport]);

  if (viewportRows === undefined) {
    return (
      <Box flexDirection="column">
        {lines.map((line) => line.node)}
      </Box>
    );
  }

  const resolvedViewport = viewport ?? 1;
  const safeScroll = effectiveScrollbackRows;

  // Match qwen-code's bottom-anchored transcript behavior: once content exceeds
  // the viewport, keep the newest rows visible unless the user has scrolled back.
  const messageCount = totalMessageCount ?? messages.length;
  const hasContent = messageCount > 0;

  let visible: typeof lines;

  if (!hasContent) {
    visible = lines.slice(0, resolvedViewport);
  } else if (total <= resolvedViewport) {
    visible = lines;
  } else {
    const rawTop = Math.max(0, total - resolvedViewport - safeScroll);
    visible = lines.slice(rawTop, rawTop + resolvedViewport);
  }

  const bottomPadding = Math.max(0, resolvedViewport - visible.length);

  return (
    <Box
      flexDirection="column"
      width="100%"
      height={resolvedViewport}
      flexShrink={0}
      overflowY="hidden"
    >
      <Box flexDirection="column" overflowY="hidden">
        {visible.map((line) => line.node)}
        {Array.from({ length: bottomPadding }, (_, index) => (
          <Text key={`pad-bottom:${index}`}> </Text>
        ))}
      </Box>
    </Box>
  );
});

ChatAreaComponent.displayName = 'ChatArea';

export const ChatArea = memo(ChatAreaComponent);
