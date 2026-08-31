import React from 'react';
import { Box, Text } from 'ink';
import type { ConnectionStatus, LiveRunStatus } from '../state/index.js';
import { inkColors } from './theme.js';

interface HeaderProps {
  threadId: string | undefined;
  connectionStatus: ConnectionStatus;
  runStatus: LiveRunStatus;
  lastError: string | undefined;
  modelName: string;
  directory: string;
}

// Helper functions for status display (shared by all components)
const getConnectionDisplay = (connectionStatus: ConnectionStatus): { color: typeof inkColors[keyof typeof inkColors]; icon: string; text: string } => {
  switch (connectionStatus) {
    case 'connected':
      return { color: inkColors.success, icon: '●', text: 'Connected' };
    case 'disconnected':
      return { color: inkColors.muted, icon: '○', text: 'Disconnected' };
    case 'error':
      return { color: inkColors.error, icon: '✖', text: 'Error' };
    default:
      return { color: inkColors.muted, icon: '○', text: 'Unknown' };
  }
};

const getRunDisplay = (runStatus: LiveRunStatus): { color: typeof inkColors[keyof typeof inkColors]; icon: string; text: string } => {
  switch (runStatus) {
    case 'idle':
      return { color: inkColors.muted, icon: '○', text: 'Idle' };
    case 'running':
      return { color: inkColors.warning, icon: '◐', text: 'Running' };
    case 'completed':
      return { color: inkColors.success, icon: '✓', text: 'Completed' };
    case 'failed':
      return { color: inkColors.error, icon: '✖', text: 'Failed' };
    default:
      return { color: inkColors.muted, icon: '○', text: 'Unknown' };
  }
};

// Bordered banner printed once at the top (snapshot of initial state)
interface SessionBannerProps {
  threadId: string | undefined;
  connectionStatus: ConnectionStatus;
  runStatus: LiveRunStatus;
  modelName: string;
  directory: string;
}

export const SessionBanner: React.FC<SessionBannerProps> = ({
  threadId,
  connectionStatus,
  runStatus,
  modelName,
  directory,
}) => {
  const connDisplay = getConnectionDisplay(connectionStatus);
  const runDisplay = getRunDisplay(runStatus);

  return (
    <Box flexDirection="column" borderStyle="round" borderColor={inkColors.accent} paddingX={1} paddingY={0}>
      <Box>
        <Text bold color={inkColors.accent}>
          DataFoundry TUI
        </Text>
        {threadId && (
          <Text dimColor>
            {'  '}session: {threadId.slice(0, 8)}
          </Text>
        )}
      </Box>

      <Box>
        <Text dimColor>model:     </Text>
        <Text>{modelName}</Text>
      </Box>

      <Box>
        <Text dimColor>directory: </Text>
        <Text>{directory}</Text>
      </Box>

      <Box>
        <Text color={connDisplay.color}>
          {connDisplay.icon} {connDisplay.text}
        </Text>
        <Text dimColor> | </Text>
        <Text color={runDisplay.color}>
          {runDisplay.icon} {runDisplay.text}
        </Text>
      </Box>
    </Box>
  );
};

// Compact status line pinned below the input box (live status)
interface StatusFooterProps {
  connectionStatus: ConnectionStatus;
  runStatus: LiveRunStatus;
  modelName: string;
  directory: string;
  compactMode?: boolean | undefined;
  thoughtExpanded?: boolean | undefined;
}

export const StatusFooter: React.FC<StatusFooterProps> = ({
  connectionStatus,
  runStatus,
  modelName,
  directory,
  compactMode,
  thoughtExpanded,
}) => {
  const connDisplay = getConnectionDisplay(connectionStatus);
  const runDisplay = getRunDisplay(runStatus);

  return (
    <Box paddingX={1} flexShrink={0} width="100%" height={1} overflow="hidden">
      <Text wrap="truncate-end">
        <Text dimColor>{modelName}</Text>
        <Text dimColor> · </Text>
        <Text dimColor>{directory}</Text>
        <Text dimColor> · </Text>
        <Text color={connDisplay.color}>
          {connDisplay.icon} {connDisplay.text}
        </Text>
        <Text dimColor> | </Text>
        <Text color={runDisplay.color}>
          {runDisplay.icon} {runDisplay.text}
        </Text>
        {compactMode !== undefined && (
          <>
            <Text dimColor> | </Text>
            <Text dimColor>{compactMode ? 'Compact' : 'Full'}</Text>
          </>
        )}
        {thoughtExpanded !== undefined && (
          <>
            <Text dimColor> · </Text>
            <Text dimColor>{thoughtExpanded ? 'Thinking open' : 'Thinking closed'}</Text>
          </>
        )}
      </Text>
    </Box>
  );
};

// Legacy Header component (keeping for backwards compatibility if needed)
export const Header: React.FC<HeaderProps> = ({
  threadId,
  connectionStatus,
  runStatus,
  lastError,
  modelName,
  directory,
}) => {
  const connDisplay = getConnectionDisplay(connectionStatus);
  const runDisplay = getRunDisplay(runStatus);

  return (
    <Box flexDirection="column" flexShrink={0} paddingX={1} marginBottom={1}>
      <Box>
        <Text bold color={inkColors.accent}>
          DataFoundry TUI
        </Text>
        {threadId && (
          <Text dimColor>
            {'  '}session: {threadId.slice(0, 8)}
          </Text>
        )}
      </Box>

      <Box>
        <Text dimColor>model:     </Text>
        <Text>{modelName}</Text>
      </Box>

      <Box>
        <Text dimColor>directory: </Text>
        <Text>{directory}</Text>
      </Box>

      <Box>
        <Text color={connDisplay.color}>
          {connDisplay.icon} {connDisplay.text}
        </Text>
        <Text dimColor> | </Text>
        <Text color={runDisplay.color}>
          {runDisplay.icon} {runDisplay.text}
        </Text>
      </Box>

      {lastError && (
        <Box>
          <Text color={inkColors.error}>
            Error: {lastError}
          </Text>
        </Box>
      )}
    </Box>
  );
};
