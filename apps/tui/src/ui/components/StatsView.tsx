import React from 'react';
import { Box, Text } from 'ink';
import type { RunUsageSnapshot } from '../../state/index.js';
import { getStatusColor, inkColors } from '../theme.js';

interface StatsViewProps {
  stats: RunUsageSnapshot;
}

/**
 * Format number with thousand separators
 */
function formatNumber(num: number): string {
  return num.toLocaleString('en-US');
}

/**
 * Format milliseconds to human-readable duration
 */
function formatDuration(ms: number): string {
  if (ms < 1000) {
    return `${ms}ms`;
  }
  if (ms < 60000) {
    return `${(ms / 1000).toFixed(2)}s`;
  }
  const minutes = Math.floor(ms / 60000);
  const seconds = Math.floor((ms % 60000) / 1000);
  return `${minutes}m ${seconds}s`;
}

/**
 * Simple progress bar component
 */
interface ProgressBarProps {
  label: string;
  value: number;
  max: number;
  width?: number;
  color?: string;
}

const ProgressBar: React.FC<ProgressBarProps> = ({
  label,
  value,
  max,
  width = 20,
  color = inkColors.success,
}) => {
  const percentage = max > 0 ? Math.min(100, (value / max) * 100) : 0;
  const filledWidth = Math.round((percentage / 100) * width);
  const emptyWidth = width - filledWidth;
  const bar = '█'.repeat(filledWidth) + '░'.repeat(emptyWidth);

  return (
    <Box>
      <Box width={25}>
        <Text>{label}:</Text>
      </Box>
      <Box width={width + 2}>
        <Text color={color}>{bar}</Text>
      </Box>
      <Text dimColor>
        {' '}
        {value}/{max} ({percentage.toFixed(0)}%)
      </Text>
    </Box>
  );
};

/**
 * Stats row component
 */
interface StatsRowProps {
  label: string;
  value: string | number;
  color?: string | undefined;
  dimValue?: boolean | undefined;
}

const StatsRow: React.FC<StatsRowProps> = ({ label, value, color, dimValue }) => {
  return (
    <Box>
      <Box width={25}>
        <Text>{label}:</Text>
      </Box>
      {color !== undefined ? (
        dimValue !== undefined ? (
          <Text color={color} dimColor={dimValue}>
            {typeof value === 'number' ? formatNumber(value) : value}
          </Text>
        ) : (
          <Text color={color}>
            {typeof value === 'number' ? formatNumber(value) : value}
          </Text>
        )
      ) : (
        dimValue !== undefined ? (
          <Text dimColor={dimValue}>
            {typeof value === 'number' ? formatNumber(value) : value}
          </Text>
        ) : (
          <Text>
            {typeof value === 'number' ? formatNumber(value) : value}
          </Text>
        )
      )}
    </Box>
  );
};

/**
 * Section header component
 */
const SectionHeader: React.FC<{ title: string }> = ({ title }) => {
  return (
    <Box marginTop={1} marginBottom={0}>
      <Text bold underline color={inkColors.accent}>
        {title}
      </Text>
    </Box>
  );
};

/**
 * StatsView component - displays comprehensive run statistics
 */
export const StatsView: React.FC<StatsViewProps> = ({ stats }) => {
  // Run status display
  const getRunStatusDisplay = (): { color: string; icon: string; text: string } => {
    switch (stats.runStatus) {
      case 'idle':
        return { color: getStatusColor(stats.runStatus), icon: '○', text: 'Idle' };
      case 'running':
        return { color: getStatusColor(stats.runStatus), icon: '◐', text: 'Running' };
      case 'completed':
        return { color: getStatusColor(stats.runStatus), icon: '✓', text: 'Completed' };
      case 'failed':
        return { color: getStatusColor(stats.runStatus), icon: '✖', text: 'Failed' };
      default:
        return { color: inkColors.muted, icon: '○', text: 'Unknown' };
    }
  };

  const runDisplay = getRunStatusDisplay();

  // Calculate averages for SQL
  const avgSqlDuration =
    stats.sql.total > 0 ? stats.sql.elapsedMs / stats.sql.total : 0;
  const avgRowsPerQuery =
    stats.sql.success > 0 ? stats.sql.rowsScanned / stats.sql.success : 0;

  // Tool call success rate
  const toolSuccessRate =
    stats.toolCalls.total > 0
      ? (stats.toolCalls.success / stats.toolCalls.total) * 100
      : 0;

  // SQL success rate
  const sqlSuccessRate =
    stats.sql.total > 0 ? (stats.sql.success / stats.sql.total) * 100 : 0;

  // Total tokens
  const totalTokens = stats.tokens.inputTokens + stats.tokens.outputTokens;

  return (
    <Box flexDirection="column" paddingX={1}>
      {/* Run Status Section */}
      <SectionHeader title="Run Status" />
      <Box marginTop={0}>
        <Text color={runDisplay.color}>
          {runDisplay.icon} {runDisplay.text}
        </Text>
        {stats.durationMs !== undefined && (
          <Text dimColor> - {formatDuration(stats.durationMs)}</Text>
        )}
      </Box>
      {stats.errorMessage && (
        <Box marginTop={0}>
          <Text color={inkColors.error}>Error: {stats.errorMessage}</Text>
        </Box>
      )}

      {/* Tool Calls Section */}
      <SectionHeader title="Tool Calls" />
      <StatsRow label="Total Calls" value={stats.toolCalls.total} />
      <StatsRow
        label="Success"
        value={stats.toolCalls.success}
        color={inkColors.success}
      />
      <StatsRow label="Failed" value={stats.toolCalls.failed} color={inkColors.error} />
      {stats.toolCalls.total > 0 && (
        <ProgressBar
          label="Success Rate"
          value={stats.toolCalls.success}
          max={stats.toolCalls.total}
          color={toolSuccessRate >= 80 ? inkColors.success : toolSuccessRate >= 50 ? inkColors.warning : inkColors.error}
        />
      )}

      {/* Tool breakdown by type */}
      {Object.keys(stats.toolCalls.byTool).length > 0 && (
        <>
          <Box marginTop={1}>
            <Text dimColor underline>
              By Tool:
            </Text>
          </Box>
          {Object.entries(stats.toolCalls.byTool)
            .sort(([, a], [, b]) => b.calls - a.calls)
            .map(([toolName, toolStats]) => (
              <Box key={toolName} marginLeft={2}>
                <Box width={23}>
                  <Text dimColor>• {toolName}:</Text>
                </Box>
                <Text>
                  {toolStats.calls} calls
                  <Text color={inkColors.success}> ({toolStats.success} ✓</Text>
                  {toolStats.failed > 0 && (
                    <Text color={inkColors.error}> {toolStats.failed} ✖</Text>
                  )}
                  <Text>)</Text>
                </Text>
              </Box>
            ))}
        </>
      )}

      {/* SQL Query Section */}
      <SectionHeader title="SQL Queries" />
      <StatsRow label="Total Queries" value={stats.sql.total} />
      <StatsRow label="Success" value={stats.sql.success} color={inkColors.success} />
      <StatsRow label="Failed" value={stats.sql.failed} color={inkColors.error} />
      <StatsRow label="Rows Scanned" value={stats.sql.rowsScanned} />
      <StatsRow
        label="Total Duration"
        value={formatDuration(stats.sql.elapsedMs)}
      />
      {stats.sql.total > 0 && (
        <>
          <StatsRow
            label="Avg Duration"
            value={formatDuration(avgSqlDuration)}
            dimValue
          />
          <StatsRow
            label="Avg Rows/Query"
            value={formatNumber(Math.round(avgRowsPerQuery))}
            dimValue
          />
          <ProgressBar
            label="Success Rate"
            value={stats.sql.success}
            max={stats.sql.total}
            color={sqlSuccessRate >= 80 ? inkColors.success : sqlSuccessRate >= 50 ? inkColors.warning : inkColors.error}
          />
        </>
      )}

      {/* Token Usage Section */}
      {stats.tokenUsageReported && (
        <>
          <SectionHeader title="Token Usage" />
          <StatsRow label="Input Tokens" value={stats.tokens.inputTokens} />
          <StatsRow
            label="Output Tokens"
            value={stats.tokens.outputTokens}
          />
          <StatsRow
            label="Total Tokens"
            value={totalTokens}
            color={inkColors.accent}
          />
          {totalTokens > 0 && (
            <ProgressBar
              label="Input/Output Ratio"
              value={stats.tokens.inputTokens}
              max={totalTokens}
              width={20}
              color={inkColors.accent}
            />
          )}
        </>
      )}

      {/* Artifacts Section */}
      <SectionHeader title="Artifacts" />
      <StatsRow label="Total Artifacts" value={stats.artifactCount} />
      {stats.artifactCount > 0 && (
        <Box marginTop={0}>
          <Text dimColor>
            {stats.artifactCount} artifact{stats.artifactCount > 1 ? 's' : ''}{' '}
            generated
          </Text>
        </Box>
      )}

      {/* Summary Section */}
      {stats.runStatus === 'completed' && (
        <Box
          marginTop={1}
          borderStyle="round"
          borderColor={inkColors.success}
          paddingX={1}
        >
          <Text color={inkColors.success}>
            ✓ Run completed in {formatDuration(stats.durationMs ?? 0)} with{' '}
            {stats.toolCalls.total} tool calls and {stats.sql.total} SQL{' '}
            {stats.sql.total === 1 ? 'query' : 'queries'}
          </Text>
        </Box>
      )}

      {stats.runStatus === 'failed' && (
        <Box marginTop={1} borderStyle="round" borderColor={inkColors.error} paddingX={1}>
          <Text color={inkColors.error}>
            ✖ Run failed after {formatDuration(stats.durationMs ?? 0)}
          </Text>
        </Box>
      )}
    </Box>
  );
};
