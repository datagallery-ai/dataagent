import React from 'react';
import { Box, Text } from 'ink';
import type { StartupInfo } from './transcript-lines.js';
import { textWidth } from './text-width.js';
import { inkColors } from './theme.js';

interface HomeSplashProps {
  rows: number;
  columns: number;
  startup: StartupInfo;
  input: React.ReactNode | ((width: number) => React.ReactNode);
  canResume?: boolean | undefined;
  canSelectDatasource?: boolean | undefined;
}

const WORDMARK = [
  {
    left: '█▀▄ ▄▀█ ▀█▀ ▄▀█',
    right: '█▀▀ █▀█ █ █ █▄ █ █▀▄ █▀█ █▄█',
  },
  {
    left: '█▄▀ █▀█  █  █▀█',
    right: '█▀  █▄█ █▄█ █ ▀█ █▄▀ █▀▄  █ ',
  },
];

const WORDMARK_WIDTH = Math.max(
  ...WORDMARK.map((line) => textWidth(`${line.left}   ${line.right}`)),
);

export function HomeSplash({ rows, columns, startup, input, canResume = false, canSelectDatasource = true }: HomeSplashProps) {
  const availableWidth = Math.max(24, columns - 4);
  const containerWidth = Math.min(76, availableWidth);
  const showLogo = availableWidth >= WORDMARK_WIDTH && rows >= 20;

  // 根据数据源状态决定显示什么提示
  const hasDataSource = startup.datasourceId && startup.datasourceId !== 'undefined';

  return (
    <Box width="100%" height={rows} flexDirection="column" overflowY="hidden">
      <Box flexGrow={1} minHeight={0} />
      <Box width="100%" flexDirection="column" alignItems="center" flexShrink={0}>
        {showLogo ? (
          <Box flexDirection="column" width={WORDMARK_WIDTH}>
            {WORDMARK.map((line, index) => (
              <Text key={`home-logo-${index}`}>
                <Text color={inkColors.muted}>{line.left}</Text>
                <Text color={inkColors.muted}>   </Text>
                <Text color={inkColors.text} bold>{line.right}</Text>
              </Text>
            ))}
          </Box>
        ) : (
          <Box flexDirection="row">
            <Text color={inkColors.muted}>data</Text>
            <Text color={inkColors.text} bold>foundry</Text>
          </Box>
        )}

        {showLogo && (
          <>
            <Box height={1} />
            <Box width={containerWidth} flexDirection="row" justifyContent="center">
              <Text color={inkColors.muted}>From question to query to evidence.</Text>
            </Box>
          </>
        )}

        <Box height={showLogo ? 2 : 1} />
        <Box width={containerWidth} flexDirection="column">
          {hasDataSource ? (
            // 有数据源：显示建议的业务问题
            <Box flexDirection="row" justifyContent="center">
              <Text color={inkColors.muted}>
                Try: <Text color={inkColors.text}>Why did revenue decline last month?</Text>
              </Text>
            </Box>
          ) : canSelectDatasource ? (
            // 无数据源：提示选择数据源
            <Box flexDirection="column" alignItems="center">
              <Text color={inkColors.muted}>No datasource selected</Text>
              <Box height={1} />
              <Box flexDirection="row" gap={2}>
                <Text color={inkColors.accent}>[/datasource]</Text>
                <Text color={inkColors.muted}>Choose a datasource to get started</Text>
              </Box>
            </Box>
          ) : (
            <Box flexDirection="column" alignItems="center">
              <Text color={inkColors.muted}>Ask a question to get started.</Text>
              <Text color={inkColors.muted}>Using your backend model and enabled tools. Type /help for commands.</Text>
            </Box>
          )}
        </Box>

        {hasDataSource && (
          <>
            <Box height={1} />
            <Box width={containerWidth} flexDirection="row" justifyContent="center" gap={2}>
              <Text color={inkColors.muted}>
                <Text color={inkColors.accent}>[1]</Text> Explore schema
              </Text>
              {canResume && (
                <Text color={inkColors.muted}>
                  <Text color={inkColors.accent}>[2]</Text> Resume latest
                </Text>
              )}
              <Text color={inkColors.muted}>
                <Text color={inkColors.accent}>[/]</Text> Commands
              </Text>
            </Box>
          </>
        )}
        <Box height={1} />
        <Box width="100%" flexDirection="column" alignSelf="flex-start">
          {typeof input === 'function' ? input(columns) : input}
        </Box>
      </Box>
    </Box>
  );
}
