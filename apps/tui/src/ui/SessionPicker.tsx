import React, { useEffect, useMemo, useState } from 'react';
import { Box, Text, useInput } from 'ink';
import type { SessionListItem } from '../config/index.js';
import { isMouseInput } from '../input/mouse-wheel.js';
import { textWidth, truncateToWidth } from './text-width.js';
import { inkColors, selectionColors } from './theme.js';

interface SessionPickerProps {
  sessions: SessionListItem[];
  loading: boolean;
  error?: string | undefined;
  columns?: number | undefined;
  rows?: number | undefined;
  onSelect: (sessionId: string) => void;
  onCancel: () => void;
}

const RESERVED_LINES = 7;
const ITEM_HEIGHT = 3;

const formatRelative = (timestamp: string | undefined): string => {
  if (!timestamp) return 'unknown';

  const time = new Date(timestamp).getTime();
  if (!Number.isFinite(time)) return 'unknown';

  const diffMs = Math.max(0, Date.now() - time);
  const minutes = Math.floor(diffMs / 60000);
  if (minutes < 1) return 'just now';
  if (minutes < 60) return `${minutes}m ago`;

  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;

  return `${Math.floor(hours / 24)}d ago`;
};

const truncate = (value: string, maxWidth: number): string => {
  const firstLine = value.split(/\r?\n/, 1)[0] ?? '';
  return truncateToWidth(firstLine, Math.max(1, maxWidth), '...');
};

const getSessionTimestamp = (session: SessionListItem): string | undefined => {
  return session.lastMessageAt ?? session.updatedAt ?? session.createdAt;
};

const getSessionTitle = (session: SessionListItem): string => {
  return session.title?.trim() || 'Untitled session';
};

export const SessionPicker: React.FC<SessionPickerProps> = ({
  sessions,
  loading,
  error,
  columns = 100,
  rows = 40,
  onSelect,
  onCancel,
}) => {
  const [query, setQuery] = useState('');
  const [selectedIndex, setSelectedIndex] = useState(0);
  const panelWidth = Math.max(24, columns);
  const panelHeight = Math.max(8, rows - 1);
  const contentWidth = Math.max(10, panelWidth - 4);
  const separatorWidth = Math.max(0, panelWidth - 2);
  const maxVisibleItems = Math.max(
    1,
    Math.floor((panelHeight - RESERVED_LINES) / ITEM_HEIGHT),
  );
  const searchPrefix = query ? 'Search: ' : 'Type to search';
  const queryWidth = Math.max(1, contentWidth - textWidth(searchPrefix) - 1);

  const filteredSessions = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    if (!normalizedQuery) return sessions;

    return sessions.filter((session) => {
      const title = getSessionTitle(session).toLowerCase();
      const threadId = session.threadId.toLowerCase();
      return title.includes(normalizedQuery) || threadId.includes(normalizedQuery);
    });
  }, [query, sessions]);

  useEffect(() => {
    setSelectedIndex(0);
  }, [filteredSessions]);

  useInput((input, key) => {
    if (isMouseInput(input)) {
      return;
    }

    if (key.escape) {
      onCancel();
      return;
    }

    if (key.upArrow || (key.ctrl && input === 'p') || input === 'k') {
      setSelectedIndex((index) => Math.max(0, index - 1));
      return;
    }

    if (key.downArrow || (key.ctrl && input === 'n') || input === 'j') {
      if (filteredSessions.length === 0) return;
      setSelectedIndex((index) => Math.min(filteredSessions.length - 1, index + 1));
      return;
    }

    if (key.return) {
      const selectedSession = filteredSessions[selectedIndex];
      if (selectedSession) {
        onSelect(selectedSession.threadId);
      }
      return;
    }

    if (key.backspace || key.delete) {
      setQuery((value) => value.slice(0, -1));
      return;
    }

    if (key.ctrl || key.meta || key.tab) {
      return;
    }

    if (input.length > 0) {
      setQuery((value) => value + input);
    }
  });

  const windowStart = Math.max(
    0,
    Math.min(
      selectedIndex - Math.floor(maxVisibleItems / 2),
      Math.max(0, filteredSessions.length - maxVisibleItems),
    ),
  );
  const visibleSessions = filteredSessions.slice(windowStart, windowStart + maxVisibleItems);
  const showScrollUp = windowStart > 0;
  const showScrollDown = windowStart + maxVisibleItems < filteredSessions.length;
  const matchText = query ? ` (${filteredSessions.length} matches)` : '';
  const titleText = truncate(
    'Resume Session',
    Math.max(1, contentWidth - textWidth(matchText)),
  );

  return (
    <Box
      flexDirection="column"
      width={panelWidth}
      height={panelHeight}
      overflow="hidden"
    >
      <Box
        flexDirection="column"
        borderStyle="single"
        borderColor={selectionColors.border}
        backgroundColor={selectionColors.background}
        width={panelWidth}
        height={panelHeight}
        overflow="hidden"
      >
        <Box paddingX={1}>
          <Text bold color={selectionColors.heading} wrap="truncate-end">
            {titleText}
          </Text>
          {matchText ? (
            <Text color={selectionColors.description} wrap="truncate-end">
              {truncate(matchText, Math.max(1, contentWidth - textWidth(titleText)))}
            </Text>
          ) : null}
        </Box>

        <Box paddingX={1}>
          {query ? (
            <>
              <Text color={selectionColors.description}>Search: </Text>
              <Text color={selectionColors.title} wrap="truncate-end">
                {truncate(query, queryWidth)}
              </Text>
            </>
          ) : (
            <Text color={selectionColors.disabled} wrap="truncate-end">
              {truncate('Type to search', contentWidth)}
            </Text>
          )}
        </Box>

        <Box>
          <Text color={selectionColors.border}>{'-'.repeat(separatorWidth)}</Text>
        </Box>

        <Box flexDirection="column" flexGrow={1} paddingX={1} overflow="hidden">
          {loading ? (
            <Box paddingY={1}>
              <Text color={selectionColors.description} wrap="truncate-end">
                {truncate('Loading recent sessions...', contentWidth)}
              </Text>
            </Box>
          ) : error ? (
            <Box paddingY={1}>
              <Text color={inkColors.error} wrap="truncate-end">
                {truncate(`Failed to load sessions: ${error}`, contentWidth)}
              </Text>
            </Box>
          ) : sessions.length === 0 ? (
            <Box paddingY={1}>
              <Text color={selectionColors.disabled} wrap="truncate-end">
                {truncate('No server sessions found.', contentWidth)}
              </Text>
            </Box>
          ) : filteredSessions.length === 0 ? (
            <Box paddingY={1}>
              <Text color={selectionColors.disabled} wrap="truncate-end">
                {truncate(`No sessions match "${query}".`, contentWidth)}
              </Text>
            </Box>
          ) : (
            visibleSessions.map((session, index) => {
              const absoluteIndex = windowStart + index;
              const selected = absoluteIndex === selectedIndex;
              const isFirst = index === 0;
              const isLast = index === visibleSessions.length - 1;
              const prefix = selected
                ? '› '
                : isFirst && showScrollUp
                  ? '^ '
                  : isLast && showScrollDown
                    ? 'v '
                    : '  ';
              const title = truncate(getSessionTitle(session), Math.max(1, contentWidth - 2));
              const threadId = truncate(session.threadId, Math.min(32, Math.max(8, contentWidth - 16)));
              const relativeTime = formatRelative(getSessionTimestamp(session));
              const metadata = truncate(
                `${relativeTime} - ${threadId}`,
                Math.max(1, contentWidth - 2),
              );

              return (
                <Box
                  key={session.threadId}
                  flexDirection="column"
                  marginBottom={isLast ? 0 : 1}
                  backgroundColor={selected
                    ? selectionColors.selectedBackground
                    : selectionColors.background}
                >
                  <Box>
                    <Text color={selected ? selectionColors.accent : selectionColors.disabled}>
                      {prefix}
                    </Text>
                    <Text
                      color={selected ? selectionColors.selectedTitle : selectionColors.title}
                      bold={selected}
                      wrap="truncate-end"
                    >
                      {title}
                    </Text>
                  </Box>
                  <Box paddingLeft={2}>
                    <Text
                      color={selected
                        ? selectionColors.selectedDescription
                        : selectionColors.description}
                      wrap="truncate-end"
                    >
                      {metadata}
                    </Text>
                  </Box>
                </Box>
              );
            })
          )}
        </Box>

        <Box>
          <Text color={selectionColors.border}>{'-'.repeat(separatorWidth)}</Text>
        </Box>

        <Box paddingX={1}>
          <Text color={selectionColors.disabled} wrap="truncate-end">
            {truncate('Up/Down/j/k navigate - Enter resume - Esc cancel', contentWidth)}
          </Text>
        </Box>
      </Box>
    </Box>
  );
};
