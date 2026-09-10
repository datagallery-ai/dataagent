import React from 'react';
import { Box, Text } from 'ink';
import type { StartupInfo } from './transcript-lines.js';
import { truncateToWidth } from './text-width.js';
import { inkColors } from './theme.js';

interface StatusBarProps {
  columns: number;
  startup: StartupInfo;
}

function statusDisplay(startup: StartupInfo): {
  label: string;
  color: typeof inkColors[keyof typeof inkColors];
} | null {
  if (startup.connectionStatus === 'error') {
    return { label: 'Error', color: inkColors.error };
  }
  if (startup.connectionStatus === 'disconnected') {
    return { label: 'Disconnected', color: inkColors.error };
  }
  return null;
}

export function StatusBar({ columns, startup }: StatusBarProps) {
  const safeColumns = Math.max(1, Math.floor(columns));
  const status = statusDisplay(startup);
  const hasDatasource = Boolean(startup.datasourceId && startup.datasourceId !== 'undefined');
  const showSource = hasDatasource && safeColumns >= 44;

  return (
    <Box
      width="100%"
      height={1}
      flexDirection="row"
      justifyContent="space-between"
      paddingX={1}
      flexShrink={0}
      overflowX="hidden"
    >
      <Box flexDirection="row" flexGrow={1}>
        <Text color={inkColors.muted}>
          {truncateToWidth(`model: ${startup.modelName || 'auto'}`, Math.max(1,
            safeColumns - 2 - (status ? status.label.length + 4 : showSource ? 30 : 0)))}
        </Text>
      </Box>

      {status ? <Text color={status.color}>● {status.label}</Text> : showSource && (
        <Box flexDirection="row" flexShrink={0}>
          {showSource && (
            <>
              <Text color={inkColors.muted}>source: </Text>
              <Text color={inkColors.text}>
                {truncateToWidth(startup.datasourceId ?? '', 20)}
              </Text>
            </>
          )}
        </Box>
      )}
    </Box>
  );
}
