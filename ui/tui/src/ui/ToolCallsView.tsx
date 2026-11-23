import React from 'react';
import { Box, Text } from 'ink';
import type { LiveToolCallRecord } from '../state/index.js';
import { getStatusColor, inkColors } from './theme.js';

interface ToolCallsViewProps {
  toolCalls: LiveToolCallRecord[];
  compact?: boolean;
}

export const ToolCallsView: React.FC<ToolCallsViewProps> = ({
  toolCalls,
  compact = false
}) => {
  if (toolCalls.length === 0) {
    return null;
  }

  // Get status icon and color
  const getStatusDisplay = (status: LiveToolCallRecord['status']) => {
    switch (status) {
      case 'pending':
        return { icon: '○', color: getStatusColor(status), label: 'Pending' };
      case 'running':
        return { icon: '⟳', color: getStatusColor(status), label: 'Running' };
      case 'success':
        return { icon: '✓', color: getStatusColor(status), label: 'Success' };
      case 'failed':
        return { icon: '✗', color: getStatusColor(status), label: 'Failed' };
      case 'cancelled':
        return { icon: '⊘', color: getStatusColor(status), label: 'Cancelled' };
      default:
        return { icon: '?', color: inkColors.muted, label: 'Unknown' };
    }
  };

  // Format duration
  const formatDuration = (toolCall: LiveToolCallRecord): string => {
    if (!toolCall.startedAtMs) return '';

    const endTime = toolCall.finishedAtMs ??
      (toolCall.status === 'running' || toolCall.status === 'pending'
        ? Date.now()
        : toolCall.startedAtMs);
    const durationMs = endTime - toolCall.startedAtMs;

    if (durationMs < 1000) {
      return `${durationMs}ms`;
    } else if (durationMs < 60000) {
      return `${(durationMs / 1000).toFixed(1)}s`;
    } else {
      const minutes = Math.floor(durationMs / 60000);
      const seconds = Math.floor((durationMs % 60000) / 1000);
      return `${minutes}m ${seconds}s`;
    }
  };

  // Compact view for completed tool calls
  if (compact) {
    return (
      <Box flexDirection="column">
        <Text dimColor>Tool calls:</Text>
        {toolCalls.map((toolCall) => {
          const { icon, color } = getStatusDisplay(toolCall.status);
          return (
            <Box key={toolCall.id} marginLeft={1}>
              <Text color={color}>{icon}</Text>
              <Text dimColor> {toolCall.name}</Text>
              {toolCall.finishedAtMs && toolCall.startedAtMs && (
                <Text dimColor> ({formatDuration(toolCall)})</Text>
              )}
            </Box>
          );
        })}
      </Box>
    );
  }

  // Full view for active tool calls
  return (
    <Box flexDirection="column">
      {toolCalls.map((toolCall, index) => {
        const { icon, color, label } = getStatusDisplay(toolCall.status);
        const duration = formatDuration(toolCall);

        return (
          <Box key={toolCall.id} flexDirection="column" marginBottom={index < toolCalls.length - 1 ? 1 : 0}>
            {/* Tool call header */}
            <Box>
              <Text color={color}>{icon}</Text>
              <Text bold color={color}> {toolCall.name}</Text>
              <Text dimColor> • {label}</Text>
              {duration && <Text dimColor> • {duration}</Text>}
            </Box>

            {/* Additional details */}
            {toolCall.stepId && (
              <Box marginLeft={2}>
                <Text dimColor>Step ID: {toolCall.stepId}</Text>
              </Box>
            )}

            {/* Result preview for completed calls */}
            {toolCall.result && toolCall.status !== 'running' && toolCall.status !== 'pending' && (
              <Box marginLeft={2} flexDirection="column">
                <Text dimColor>Result:</Text>
                <Box marginLeft={1}>
                  <Text dimColor>
                    {toolCall.result.length > 100
                      ? toolCall.result.substring(0, 100) + '...'
                      : toolCall.result}
                  </Text>
                </Box>
              </Box>
            )}
          </Box>
        );
      })}
    </Box>
  );
};
