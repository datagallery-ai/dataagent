import React, { useEffect, useState } from 'react';
import { Text } from 'ink';
import type { SubagentProgress as Progress, SubagentStep } from '../state/subagent-progress.js';
import { inkColors } from './theme.js';
import { textWidth, truncateToWidth } from './text-width.js';

const STEP_LABELS: Record<string, string> = {
  model: 'Model response', tools: 'Execute tools',
};

function stepLabel(name: string): string {
  return STEP_LABELS[name] ?? name.replace(/[\x00-\x1f\x7f]/gu, '').replace(/_/gu, ' ');
}

export function stepHistoryLabel(step: SubagentStep, steps: SubagentStep[]): string {
  const attempt = steps.slice(0, steps.indexOf(step) + 1)
    .filter((item) => item.name === step.name && item.subagentRunId === step.subagentRunId).length;
  const suffix = attempt > 1 ? ` (attempt ${attempt})` : '';
  const duration = step.durationMs === undefined ? '' : ` · ${(step.durationMs / 1000).toFixed(1)}s`;
  const icon = step.status === 'completed' ? '✓' : step.status === 'failed' ? '✗' : step.status === 'suspended' ? '–' : '·';
  return `${icon} ${stepLabel(step.name)}${suffix}${duration}`;
}

/** One stable row. Waiting time is not evidence of a failure or of server liveness. */
export function SubagentProgressLine({ progress, width }: { progress: Progress; width: number }) {
  const [now, setNow] = useState(() => performance.now());
  useEffect(() => {
    setNow(performance.now());
    const timer = setInterval(() => setNow(performance.now()), 1000);
    return () => clearInterval(timer);
  }, [progress.updatedAtMs]);
  const step = [...progress.steps].reverse().find((item) => item.status === 'running');
  const idleSeconds = Math.max(0, Math.floor((now - progress.updatedAtMs) / 1000));
  const elapsed = Math.max(0, Math.floor((now - (step?.startedAtMs ?? progress.updatedAtMs)) / 1000));
  const label = step ? stepHistoryLabel(step, progress.steps).replace(/^· /u, '')
    : progress.status === 'failed' ? 'Subagent error'
      : progress.status === 'completed' ? 'Returning result'
        : progress.status === 'suspended' ? 'Subagent suspended' : 'Starting subagent';
  const waiting = idleSeconds >= 30 ? ' · waiting for update' : '';
  const timer = `${elapsed}s`.padStart(6);
  const budget = Math.max(1, width - 4 - textWidth(timer) - 3);
  const body = truncateToWidth(label + waiting, budget);
  return <Text color={progress.status === 'failed' ? inkColors.error : idleSeconds >= 30 ? inkColors.warning : inkColors.muted}>
    {'  └ ' + body + ' '.repeat(Math.max(0, budget - textWidth(body))) + ' · ' + timer}
  </Text>;
}
