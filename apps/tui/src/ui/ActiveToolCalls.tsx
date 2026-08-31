import React from 'react';
import { Box, Text } from 'ink';
import type { LiveToolCallRecord } from '../state/index.js';

interface ActiveToolCallsProps {
  toolCalls: LiveToolCallRecord[];
}

/**
 * Display active (running) tool calls in a compact, inline format
 * Similar to Claude Code's "● ToolName(args)" style
 */
export const ActiveToolCalls: React.FC<ActiveToolCallsProps> = ({ toolCalls }) => {
  if (toolCalls.length === 0) {
    return null;
  }

  const getToolDisplayName = (name: string): string => {
    const displayNames: Record<string, string> = {
      'run_sql_readonly': 'Execute SQL',
      'inspect_schema': 'Inspect Schema',
      'list_datasources': 'List Datasources',
      'get_table_schema': 'Get Table Schema',
      'query_data': 'Query Data',
    };
    return displayNames[name] || name;
  };

  return (
    <Box flexDirection="column" marginTop={0} marginBottom={1}>
      {toolCalls.map((toolCall) => (
        <Box key={toolCall.id} marginBottom={0}>
          <Text dimColor>● </Text>
          <Text dimColor>{getToolDisplayName(toolCall.name)}</Text>
          {toolCall.startedAtMs && (
            <Text dimColor> ({getElapsedTime(toolCall.startedAtMs)})</Text>
          )}
        </Box>
      ))}
    </Box>
  );
};

function getElapsedTime(startMs: number): string {
  const elapsedMs = Date.now() - startMs;

  if (elapsedMs < 1000) {
    return `${elapsedMs}ms`;
  } else if (elapsedMs < 60000) {
    return `${(elapsedMs / 1000).toFixed(1)}s`;
  } else {
    const minutes = Math.floor(elapsedMs / 60000);
    const seconds = Math.floor((elapsedMs % 60000) / 1000);
    return `${minutes}m ${seconds}s`;
  }
}
