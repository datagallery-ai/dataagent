import React from 'react';
import { Box, Text } from 'ink';
import type { StartupInfo } from './transcript-lines.js';
import { truncateToWidth } from './text-width.js';
import { inkColors } from './theme.js';

interface HomeSplashProps {
  rows: number;
  columns: number;
  startup: StartupInfo;
  input: React.ReactNode | ((width: number) => React.ReactNode);
  canResume?: boolean | undefined;
  localAgent?: boolean | undefined;
  mounts?: string | undefined;
}

export function HomeSplash({ rows, columns, startup, input, canResume = false, localAgent = false, mounts }: HomeSplashProps) {
  const width = Math.max(1, columns - 4);
  const cardWidth = Math.min(70, width);
  const valueWidth = Math.max(1, cardWidth - 15);
  const hasDataSource = startup.datasourceId && startup.datasourceId !== 'undefined';
  const title = localAgent ? 'DataAgent' : 'DataFoundry';

  return (
    <Box width="100%" height={rows} flexDirection="column" overflowY="hidden">
      <Box marginX={2} marginTop={1} flexDirection="column" flexShrink={1} overflowY="hidden">
        {rows >= 24 ? (
          <Box width={cardWidth} borderStyle="round" borderColor={inkColors.border} paddingX={1} flexDirection="column" flexShrink={0}>
            <Text><Text color={inkColors.muted}>›_ </Text><Text color={inkColors.text} bold>{title}</Text></Text>
            <Text> </Text>
            <Text><Text color={inkColors.muted}>model:     </Text><Text color={inkColors.text}>{truncateToWidth(startup.modelName, valueWidth)}</Text></Text>
            <Text><Text color={inkColors.muted}>directory: </Text><Text color={inkColors.text}>{truncateToWidth(startup.directory, valueWidth)}</Text></Text>
          </Box>
        ) : <Text color={inkColors.text} bold>›_ {title}</Text>}
        <Box marginTop={1} paddingX={1} flexDirection="column" flexShrink={0}>
          <Text color={inkColors.muted} wrap="truncate-end">
            <Text color={inkColors.text}>Try: </Text>
            {localAgent ? 'Summarize the numbers 1, 2, 3.' : hasDataSource ? 'Why did revenue decline last month?' : '/datasource to choose a datasource.'}
          </Text>
          {localAgent && <Text color={inkColors.muted} wrap="truncate-end">{mounts ? 'Inputs are read-only. ' : ''}Write files in session outputs.</Text>}
          {mounts && <Text color={inkColors.muted} wrap="truncate-end">{mounts}</Text>}
          <Box marginTop={1}>
            <Text color={inkColors.muted} wrap="truncate-end">
              {(hasDataSource || localAgent) && <><Text color={inkColors.accent}>[1]</Text> {localAgent ? 'Try statistics' : 'Explore schema'}   </>}
              {canResume && <><Text color={inkColors.accent}>[2]</Text> Resume latest   </>}
              <Text color={inkColors.accent}>[/]</Text> Commands
            </Text>
          </Box>
        </Box>
      </Box>
      <Box flexGrow={1} minHeight={0} />
      <Box width="100%" flexDirection="column" flexShrink={0}>
        {typeof input === 'function' ? input(columns) : input}
      </Box>
    </Box>
  );
}
