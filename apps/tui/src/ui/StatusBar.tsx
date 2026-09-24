import React from 'react';
import { Box, Text } from 'ink';
import type { StartupInfo } from './transcript-lines.js';
import { textWidth, truncateToWidth } from './text-width.js';
import { inkColors } from './theme.js';

interface StatusBarProps {
  columns: number;
  startup: StartupInfo;
}

/** One quiet context line, aligned with the composer's text rather than its prompt. */
export function StatusBar({ columns, startup }: StatusBarProps) {
  const width = Math.max(1, Math.floor(columns));
  const inset = Math.min(3, width - 1);
  const contentWidth = width - inset;
  const error = startup.connectionStatus === 'error' ? 'Error'
    : startup.connectionStatus === 'disconnected' ? 'Disconnected' : '';
  const errorWidth = error ? Math.min(contentWidth, textWidth(error)) : 0;
  const budget = Math.max(0, contentWidth - (error ? errorWidth + 2 : 0));
  const model = truncateToWidth(startup.modelName || 'auto', budget);
  let remaining = Math.max(0, budget - textWidth(model));
  const directory = startup.directory && remaining >= 8
    ? truncateToWidth(startup.directory, remaining - 3) : '';
  if (directory) remaining -= textWidth(directory) + 3;
  const source = startup.datasourceId && startup.datasourceId !== 'undefined'
    ? `source: ${startup.datasourceId}` : '';
  const showSource = Boolean(source) && textWidth(source) + 3 <= remaining;
  if (showSource) remaining -= textWidth(source) + 3;
  const hint = 'Ctrl+O details · / commands';
  const showHint = !error && remaining >= textWidth(hint) + 3;

  return (
    <Box width={width} height={1} paddingLeft={inset} flexShrink={0} overflowX="hidden" justifyContent="space-between">
      <Text wrap="truncate-end">
        <Text color={inkColors.emphasis}>{model}</Text>
        {directory && <Text color={inkColors.muted}> · <Text color={inkColors.success}>{directory}</Text></Text>}
        {showSource && <Text color={inkColors.muted}> · {source}</Text>}
        {showHint && <Text color={inkColors.muted}> · {hint}</Text>}
      </Text>
      {error && <Text color={inkColors.error} wrap="truncate-end">{truncateToWidth(error, errorWidth)}</Text>}
    </Box>
  );
}
