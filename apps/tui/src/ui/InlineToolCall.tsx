import React from 'react';
import { Text } from 'ink';
import type { LiveToolCallRecord } from '../state/index.js';
import { basename, formatBytes, getStatusColor, inkColors } from './theme.js';
import { textWidth, truncateToWidth } from './text-width.js';

const RUNNING_TOOL_FRAME_MS = 250;
const RUNNING_TOOL_FRAMES = ['◐', '◓', '◑', '◒'] as const;

interface InlineToolCallProps {
  toolCall: LiveToolCallRecord;
  showName?: boolean;
  maxWidth?: number | undefined;
}

function formatElapsedDuration(elapsedMs: number, status: LiveToolCallRecord['status']): string {
  const safeElapsedMs = Math.max(0, elapsedMs);

  if (status === 'running') {
    const totalSeconds = Math.floor(safeElapsedMs / 1000);
    if (totalSeconds < 60) {
      return `${totalSeconds}s`;
    }

    const minutes = Math.floor(totalSeconds / 60);
    const seconds = totalSeconds % 60;
    return `${minutes}m ${seconds}s`;
  }

  if (safeElapsedMs < 1000) {
    return `${Math.round(safeElapsedMs)}ms`;
  }

  if (safeElapsedMs < 60000) {
    return `${(safeElapsedMs / 1000).toFixed(1)}s`;
  }

  const minutes = Math.floor(safeElapsedMs / 60000);
  const seconds = Math.floor((safeElapsedMs % 60000) / 1000);
  return `${minutes}m ${seconds}s`;
}

/**
 * Display a single tool call inline within message content
 * Shows status icon, name, and duration in a compact format
 */
export const InlineToolCall: React.FC<InlineToolCallProps> = ({
  toolCall,
  showName = true,
  maxWidth,
}) => {
  const [nowMs, setNowMs] = React.useState(() => Date.now());
  const [frameIndex, setFrameIndex] = React.useState(0);

  React.useEffect(() => {
    if (
      (toolCall.status !== 'running' && toolCall.status !== 'pending') ||
      !toolCall.startedAtMs
    ) {
      setFrameIndex(0);
      return;
    }

    setNowMs(Date.now());
    setFrameIndex((index) => (index + 1) % RUNNING_TOOL_FRAMES.length);
    const interval = setInterval(() => {
      setNowMs(Date.now());
      setFrameIndex((index) => (index + 1) % RUNNING_TOOL_FRAMES.length);
    }, RUNNING_TOOL_FRAME_MS);

    return () => clearInterval(interval);
  }, [toolCall.status, toolCall.startedAtMs]);

  const getStatusIcon = (status: LiveToolCallRecord['status']): string => {
    switch (status) {
      case 'running':
        return RUNNING_TOOL_FRAMES[frameIndex];
      case 'pending':
        return '○';
      case 'success':
        return '•';
      case 'failed':
        return '✗';
      case 'cancelled':
        return '⊘';
      default:
        return '?';
    }
  };

  const getDuration = (): string => {
    if (!toolCall.startedAtMs) return '';

    const isActive = toolCall.status === 'running' || toolCall.status === 'pending';
    const endTime = toolCall.finishedAtMs ?? (isActive ? nowMs : toolCall.startedAtMs);
    const elapsedMs = endTime - toolCall.startedAtMs;

    return formatElapsedDuration(elapsedMs, toolCall.status);
  };

  const icon = getStatusIcon(toolCall.status);
  const color = getStatusColor(toolCall.status);
  const duration = getDuration();
  const summary = toolSummary(toolCall);
  const status = toolCall.status === 'failed' ? 'Failed · '
    : toolCall.status === 'cancelled' ? 'Cancelled · '
      : toolCall.status === 'pending' ? 'Queued · ' : '';
  const width = Math.max(1, (maxWidth ?? 1000) - textWidth(icon) - 1);
  const suffix = duration ? ` · ${duration}` : '';
  const title = showName ? truncateToWidth(status + summary.title, Math.max(1, width - textWidth(suffix))) : '';
  const detailBudget = Math.max(0, width - textWidth(title) - textWidth(suffix) - 1);
  const detail = showName && summary.detail && detailBudget > 0
    ? truncateToWidth(summary.detail, detailBudget) : '';

  return (
    <Text wrap="truncate-end">
      <Text color={color}>{icon}</Text>{' '}
      <Text color={inkColors.text} bold>{title}</Text>
      {detail && <Text color={inkColors.accent}> {detail}</Text>}
      <Text color={inkColors.muted}>{showName ? suffix : duration}</Text>
    </Text>
  );
};

function toolSummary(toolCall: LiveToolCallRecord): { title: string; detail: string } {
  const file = fileToolSummary(toolCall);
  const args = parsePayloadRecord(toolCall.args);
  const target = file
    ? [file.path, file.size].filter(Boolean).join(' · ')
    : stringField(args, 'file_path') ?? stringField(args, 'path')
      ?? stringField(args, 'command') ?? stringField(args, 'pattern')
      ?? stringField(args, 'subagent_type') ?? stringField(args, 'sql')
      ?? stringField(args, 'query') ?? '';
  return { title: toolDisplayName(toolCall.name), detail: target.replace(/\s+/gu, ' ').trim() };
}

function toolDisplayName(name: string): string {
  const displayNames: Record<string, string> = {
    read_file: 'Read',
    ls: 'List',
    glob: 'Find',
    grep: 'Search',
    task: 'Delegate',
    execute: 'Run',
    write_file: 'Write file',
    edit_file: 'Edit file',
    write_todos: 'Update plan',
    run_sql_readonly: 'Run SQL',
    inspect_schema: 'Inspect schema',
    list_data_sources: 'List sources',
    get_table_schema: 'Read schema',
    query_data: 'Query data',
    publish_artifact: 'Publish artifact',
    promote_workspace_file: 'Create file',
  };
  return displayNames[name] || name;
}

function fileToolSummary(toolCall: LiveToolCallRecord): {
  path?: string | undefined;
  size?: string | undefined;
} | undefined {
  if (!['write_file', 'edit_file', 'publish_artifact', 'promote_workspace_file'].includes(toolCall.name)) {
    return undefined;
  }

  const args = parsePayloadRecord(toolCall.args);
  const result = parsePayloadRecord(toolCall.result ?? toolCall.resultPreview);
  const observation = stringField(result, 'observation') ?? stringPayload(toolCall.result ?? toolCall.resultPreview);
  const wrote = observation ? /^Wrote (\d+) bytes to (.+)$/u.exec(firstLine(observation)) : null;
  const replaced = observation ? /^Replaced \d+ occurrence(?:s)? in (.+)$/u.exec(firstLine(observation)) : null;
  const rawPath =
    wrote?.[2] ??
    replaced?.[1]?.replace(/\s+\(lines [^)]+\)$/u, '') ??
    stringField(args, 'path') ??
    stringField(args, 'file_path') ??
    stringField(args, 'filename') ??
    stringField(result, 'path') ??
    stringField(result, 'name');
  const bytes =
    numberField(result, 'bytes') ??
    numberField(result, 'size') ??
    (wrote?.[1] ? Number(wrote[1]) : undefined);

  return {
    ...(rawPath ? { path: basename(rawPath.trim()) } : {}),
    ...(Number.isFinite(bytes) ? { size: formatBytes(bytes as number) } : {}),
  };
}

function parsePayloadRecord(value: unknown): Record<string, unknown> | undefined {
  if (isRecord(value)) return value;
  if (typeof value !== 'string') return undefined;
  try {
    const parsed = JSON.parse(value) as unknown;
    return isRecord(parsed) ? parsed : undefined;
  } catch {
    return undefined;
  }
}

function stringPayload(value: unknown): string | undefined {
  return typeof value === 'string' ? value.trim() : undefined;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function stringField(record: Record<string, unknown> | undefined, field: string): string | undefined {
  const value = record?.[field];
  return typeof value === 'string' && value.trim().length > 0 ? value : undefined;
}

function numberField(record: Record<string, unknown> | undefined, field: string): number | undefined {
  const value = record?.[field];
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined;
}

function firstLine(value: string): string {
  return value.split('\n')[0]?.trim() ?? value.trim();
}
