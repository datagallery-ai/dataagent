import React from 'react';
import { Box, Text } from 'ink';
import type { LiveToolCallRecord, TimelineEvent } from '../state/index.js';
import { getStatusColor } from './theme.js';

interface ToolTraceListProps {
  toolCalls: LiveToolCallRecord[];
  events: TimelineEvent[];
}

export const ToolTraceList: React.FC<ToolTraceListProps> = ({
  toolCalls,
  events,
}) => {
  const visibleToolCalls = toolCalls.slice(-3);
  const hiddenToolCallCount = Math.max(0, toolCalls.length - visibleToolCalls.length);

  // Get status icon for tool call
  const getToolIcon = (status: LiveToolCallRecord['status']) => {
    switch (status) {
      case 'pending':
        return '○';
      case 'running':
        return '◐';
      case 'success':
        return '✓';
      case 'failed':
        return '✖';
      case 'cancelled':
        return '⊘';
    }
  };

  // Calculate duration for completed tool calls
  const getDuration = (toolCall: LiveToolCallRecord): string | null => {
    if (toolCall.startedAtMs && toolCall.finishedAtMs) {
      const durationMs = toolCall.finishedAtMs - toolCall.startedAtMs;
      if (durationMs < 1000) {
        return `${durationMs}ms`;
      }
      return `${(durationMs / 1000).toFixed(2)}s`;
    }
    return null;
  };

  return (
    <Box flexDirection="column">
      {toolCalls.length === 0 ? (
        <Text dimColor>No tool calls yet</Text>
      ) : (
        <>
        {hiddenToolCallCount > 0 && (
          <Text dimColor>... {hiddenToolCallCount} earlier</Text>
        )}
        {visibleToolCalls.map((toolCall) => {
          const duration = getDuration(toolCall);
          return (
            <Box key={toolCall.id} marginTop={0}>
              {/* Tool call header */}
              <Text color={getStatusColor(toolCall.status)}>
                {getToolIcon(toolCall.status)}
              </Text>
              <Text bold> {toolCall.name}</Text>
              {duration && (
                <Text dimColor> ({duration})</Text>
              )}
            </Box>
          );
        })}
        </>
      )}
    </Box>
  );
};
