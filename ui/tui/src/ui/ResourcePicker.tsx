import React, { useEffect, useMemo, useState } from 'react';
import { Box, Text, useInput } from 'ink';
import { inkColors, selectionColors } from './theme.js';

export interface ResourcePickerItem {
  id: string;
  name: string;
  description?: string | undefined;
  detail?: string | undefined;
  enabled?: boolean | undefined;
  active?: boolean | undefined;
}

interface ResourcePickerProps {
  title: string;
  items: ResourcePickerItem[];
  loading: boolean;
  error?: string | undefined;
  warning?: string | undefined;
  columns?: number | undefined;
  rows?: number | undefined;
  emptyMessage: string;
  onSelect: (item: ResourcePickerItem) => void;
  onCancel: () => void;
}

const WINDOW_SIZE = 10;
const FULLSCREEN_RESERVED_LINES = 7;
const FULLSCREEN_ITEM_HEIGHT = 2;

const truncate = (value: string, maxLength: number): string => {
  if (value.length <= maxLength) return value;
  return `${value.slice(0, Math.max(0, maxLength - 3))}...`;
};

export const ResourcePicker: React.FC<ResourcePickerProps> = ({
  title,
  items,
  loading,
  error,
  warning,
  columns,
  rows,
  emptyMessage,
  onSelect,
  onCancel,
}) => {
  const [query, setQuery] = useState('');
  const [selectedIndex, setSelectedIndex] = useState(0);
  const fullscreen = typeof columns === 'number' && typeof rows === 'number';
  const panelWidth = fullscreen ? Math.max(24, columns) : undefined;
  const panelHeight = fullscreen ? Math.max(8, rows - 1) : undefined;
  const visibleItemCount = fullscreen
    ? Math.max(
        1,
        Math.floor(((panelHeight ?? 8) - FULLSCREEN_RESERVED_LINES) / FULLSCREEN_ITEM_HEIGHT),
      )
    : WINDOW_SIZE;
  const titleMaxWidth = fullscreen ? Math.max(1, (panelWidth ?? 24) - 6) : 42;
  const idMaxWidth = fullscreen ? Math.min(32, Math.max(8, (panelWidth ?? 24) - 24)) : 24;
  const detailMaxWidth = fullscreen ? Math.max(12, (panelWidth ?? 24) - 10) : 56;
  const descriptionMaxWidth = fullscreen ? Math.max(12, (panelWidth ?? 24) - 8) : 70;

  const filteredItems = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase();
    if (!normalizedQuery) return items;

    return items.filter((item) => {
      const values = [
        item.id,
        item.name,
        item.description ?? '',
        item.detail ?? '',
      ];
      return values.some((value) => value.toLowerCase().includes(normalizedQuery));
    });
  }, [query, items]);

  useEffect(() => {
    const activeIndex = filteredItems.findIndex((item) => item.active);
    setSelectedIndex(activeIndex >= 0 ? activeIndex : 0);
  }, [filteredItems]);

  useInput((input, key) => {
    if (key.escape) {
      onCancel();
      return;
    }

    if (key.upArrow) {
      setSelectedIndex((index) => Math.max(0, index - 1));
      return;
    }

    if (key.downArrow) {
      if (filteredItems.length === 0) return;
      setSelectedIndex((index) => Math.min(filteredItems.length - 1, index + 1));
      return;
    }

    if (key.return) {
      const selectedItem = filteredItems[selectedIndex];
      if (selectedItem) {
        onSelect(selectedItem);
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
      selectedIndex - Math.floor(visibleItemCount / 2),
      Math.max(0, filteredItems.length - visibleItemCount),
    ),
  );
  const visibleItems = filteredItems.slice(windowStart, windowStart + visibleItemCount);

  return (
    <Box
      flexDirection="column"
      borderStyle="single"
      borderColor={selectionColors.border}
      backgroundColor={selectionColors.background}
      width={panelWidth}
      height={panelHeight}
      overflow={fullscreen ? 'hidden' : undefined}
      paddingX={1}
      paddingY={1}
      marginX={fullscreen ? 0 : 1}
    >
      <Text bold color={selectionColors.heading}>{title}</Text>
      <Box>
        <Text color={selectionColors.description}>Type to search: </Text>
        <Text color={selectionColors.title}>{query}</Text>
        <Text inverse> </Text>
      </Box>

      {warning ? (
        <Box marginTop={1}>
          <Text color={inkColors.warning}>{warning}</Text>
        </Box>
      ) : null}

      <Box
        flexDirection="column"
        marginTop={1}
        minHeight={fullscreen ? undefined : WINDOW_SIZE}
        flexGrow={fullscreen ? 1 : undefined}
        overflow={fullscreen ? 'hidden' : undefined}
      >
        {loading ? (
          <Text color={selectionColors.description}>Loading...</Text>
        ) : error ? (
          <Text color={inkColors.error}>{error}</Text>
        ) : items.length === 0 ? (
          <Text color={selectionColors.disabled}>{emptyMessage}</Text>
        ) : filteredItems.length === 0 ? (
          <Text color={selectionColors.disabled}>No items match "{query}".</Text>
        ) : (
          visibleItems.map((item, index) => {
            const absoluteIndex = windowStart + index;
            const selected = absoluteIndex === selectedIndex;
            const stateMarker = item.active ? '*' : item.enabled === false ? ' ' : '+';
            const titleText = truncate(item.name || item.id, titleMaxWidth);
            const idText = truncate(item.id, idMaxWidth);
            const detailText = item.detail ? truncate(item.detail, detailMaxWidth) : '';
            const itemColor = selected
              ? selectionColors.selectedTitle
              : item.enabled === false
                ? selectionColors.disabled
                : selectionColors.title;

            return (
              <Box
                key={item.id}
                flexDirection="column"
                backgroundColor={selected
                  ? selectionColors.selectedBackground
                  : selectionColors.background}
              >
                <Box>
                  <Text color={selected ? selectionColors.accent : selectionColors.disabled}>
                    {selected ? '› ' : '  '}
                  </Text>
                  <Text color={item.active ? selectionColors.accent : selectionColors.disabled}>
                    {stateMarker}{' '}
                  </Text>
                  <Text color={itemColor} bold={selected}>
                    {titleText}
                  </Text>
                  <Text
                    color={selected
                      ? selectionColors.selectedDescription
                      : selectionColors.description}
                  >
                    {' '}({idText})
                  </Text>
                </Box>
                {(item.description || detailText) ? (
                  <Box paddingLeft={4}>
                    <Text
                      color={selected
                        ? selectionColors.selectedDescription
                        : selectionColors.description}
                    >
                      {truncate(item.description ?? detailText, descriptionMaxWidth)}
                      {item.description && detailText ? ` - ${detailText}` : ''}
                    </Text>
                  </Box>
                ) : null}
              </Box>
            );
          })
        )}
      </Box>

      <Box marginTop={1}>
        <Text color={selectionColors.disabled}>
          Up/Down Navigate - Enter Select - Esc Cancel - * active, + enabled
        </Text>
      </Box>
    </Box>
  );
};
