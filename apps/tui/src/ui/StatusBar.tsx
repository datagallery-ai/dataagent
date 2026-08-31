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
} {
  if (startup.connectionStatus === 'error') {
    return { label: 'Error', color: inkColors.error };
  }
  if (startup.connectionStatus === 'disconnected') {
    return { label: 'Disconnected', color: inkColors.error };
  }
  if (startup.runStatus === 'running') {
    return { label: 'Running', color: inkColors.warning };
  }
  if (startup.runStatus === 'failed') {
    return { label: 'Failed', color: inkColors.error };
  }
  return { label: 'Ready', color: inkColors.success };
}

export function StatusBar({ columns, startup }: StatusBarProps) {
  const safeColumns = Math.max(1, Math.floor(columns));
  const status = statusDisplay(startup);
  const hasDatasource = Boolean(startup.datasourceId && startup.datasourceId !== 'undefined');
  const showSource = hasDatasource && safeColumns >= 44;
  const showModel = safeColumns >= (showSource ? 72 : 40);

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
      <Box flexDirection="row" flexShrink={0}>
        <Text color={status.color}>●</Text>
        <Text color={inkColors.text}> {status.label}</Text>
      </Box>

      {(showSource || showModel) && (
        <Box flexDirection="row" flexShrink={0}>
          {showSource && (
            <>
              <Text color={inkColors.muted}>source: </Text>
              <Text color={inkColors.text}>
                {truncateToWidth(startup.datasourceId ?? '', 20)}
              </Text>
              {showModel && <Text color={inkColors.muted}>  </Text>}
            </>
          )}
          {showModel && (
            <>
              <Text color={inkColors.muted}>model: </Text>
              <Text color={inkColors.text}>
                {truncateToWidth(startup.modelName || 'auto', 16)}
              </Text>
            </>
          )}
        </Box>
      )}
    </Box>
  );
}
