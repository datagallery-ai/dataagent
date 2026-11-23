import React from 'react';
import { Box, Text } from 'ink';
import { inkColors } from '../theme.js';

const MAX_DISPLAYED_QUEUED_PROMPTS = 3;

export function queuedPromptDisplayRows(queueLength: number): number {
  if (queueLength <= 0) return 0;
  return 2
    + Math.min(queueLength, MAX_DISPLAYED_QUEUED_PROMPTS)
    + (queueLength > MAX_DISPLAYED_QUEUED_PROMPTS ? 1 : 0);
}

export function QueuedPromptDisplay({
  prompts,
}: {
  prompts: string[];
}) {
  if (prompts.length === 0) {
    return null;
  }

  return (
    <Box flexDirection="column" paddingX={1} flexShrink={0}>
      <Text color={inkColors.accent}>Queued follow-ups</Text>
      {prompts.slice(0, MAX_DISPLAYED_QUEUED_PROMPTS).map((prompt, index) => (
        <Box key={`${index}:${prompt}`} paddingLeft={2} width="100%">
          <Text dimColor wrap="truncate-end">
            {prompt.replace(/\s+/g, ' ')}
          </Text>
        </Box>
      ))}
      {prompts.length > MAX_DISPLAYED_QUEUED_PROMPTS && (
        <Box paddingLeft={2}>
          <Text dimColor>
            ... (+{prompts.length - MAX_DISPLAYED_QUEUED_PROMPTS} more)
          </Text>
        </Box>
      )}
      <Box paddingLeft={2}>
        <Text dimColor italic>Press Up or Esc to edit queued messages</Text>
      </Box>
    </Box>
  );
}
