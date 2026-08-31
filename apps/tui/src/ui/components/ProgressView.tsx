import React, { useEffect, useState } from 'react';
import { Box, Text } from 'ink';
import type { LivePlanTask, LiveTaskStatus } from '../../state/index.js';
import { getStatusColor, inkColors } from '../theme.js';

/**
 * Progress bar component for long-running operations
 */
interface ProgressBarProps {
  label: string;
  current: number;
  total: number;
  width?: number;
  showPercentage?: boolean;
  color?: string;
  status?: 'active' | 'success' | 'error' | 'warning';
}

export const ProgressBar: React.FC<ProgressBarProps> = ({
  label,
  current,
  total,
  width = 30,
  showPercentage = true,
  color,
  status = 'active',
}) => {
  const percentage = total > 0 ? Math.min(100, Math.max(0, (current / total) * 100)) : 0;
  const filledWidth = Math.round((percentage / 100) * width);
  const emptyWidth = width - filledWidth;

  // Auto-determine color based on status if not explicitly provided
  const barColor =
    color ||
    (status === 'success'
      ? inkColors.success
      : status === 'error'
        ? inkColors.error
        : status === 'warning'
          ? inkColors.warning
          : inkColors.accent);

  const filled = '█'.repeat(filledWidth);
  const empty = '░'.repeat(emptyWidth);

  return (
    <Box flexDirection="column">
      <Box>
        <Text>{label}</Text>
      </Box>
      <Box>
        <Text color={barColor}>
          {filled}
          {empty}
        </Text>
        {showPercentage && (
          <Text dimColor>
            {' '}
            {current}/{total} ({percentage.toFixed(0)}%)
          </Text>
        )}
      </Box>
    </Box>
  );
};

/**
 * Spinner component with customizable animation frames
 */
interface SpinnerProps {
  type?: 'dots' | 'line' | 'arc' | 'bounce' | 'pulse';
  color?: string;
}

export const Spinner: React.FC<SpinnerProps> = ({ type = 'dots', color = inkColors.accent }) => {
  const [frame, setFrame] = useState(0);

  const animations = {
    dots: ['⠋', '⠙', '⠹', '⠸', '⠼', '⠴', '⠦', '⠧', '⠇', '⠏'],
    line: ['|', '/', '-', '\\'],
    arc: ['◜', '◠', '◝', '◞', '◡', '◟'],
    bounce: ['⠁', '⠂', '⠄', '⠂'],
    pulse: ['●', '○', '◌', '○'],
  };

  const frames = animations[type];

  useEffect(() => {
    const timer = setInterval(() => {
      setFrame((prev) => (prev + 1) % frames.length);
    }, 80);

    return () => clearInterval(timer);
  }, [frames.length]);

  return <Text color={color}>{frames[frame]}</Text>;
};

/**
 * Task item component with status indicator
 */
interface TaskItemProps {
  task: LivePlanTask;
  showSpinner?: boolean;
  compact?: boolean;
}

export const TaskItem: React.FC<TaskItemProps> = ({
  task,
  showSpinner = true,
  compact = false,
}) => {
  const getTaskIcon = (status: LiveTaskStatus): string => {
    switch (status) {
      case 'pending':
        return '○';
      case 'running':
        return '◐';
      case 'completed':
        return '✓';
      case 'failed':
        return '✖';
    }
  };

  const getTaskColor = (status: LiveTaskStatus): string => {
    return getStatusColor(status);
  };

  const icon = getTaskIcon(task.status);
  const color = getTaskColor(task.status);
  const isRunning = task.status === 'running';

  return (
    <Box>
      <Box width={3}>
        {isRunning && showSpinner ? (
          <Spinner type="dots" color={color} />
        ) : (
          <Text color={color}>{icon}</Text>
        )}
      </Box>
      <Text color={color} bold={isRunning}>
        {task.title}
      </Text>
      {!compact && isRunning && (
        <Text dimColor> (in progress...)</Text>
      )}
    </Box>
  );
};

/**
 * Task list component with status summary
 */
interface TaskListProps {
  tasks: LivePlanTask[];
  title?: string;
  showProgress?: boolean;
  showSpinner?: boolean;
  compact?: boolean;
  maxHeight?: number;
}

export const TaskList: React.FC<TaskListProps> = ({
  tasks,
  title = 'Tasks',
  showProgress = true,
  showSpinner = true,
  compact = false,
  maxHeight,
}) => {
  const completed = tasks.filter((t) => t.status === 'completed').length;
  const failed = tasks.filter((t) => t.status === 'failed').length;
  const running = tasks.filter((t) => t.status === 'running').length;
  const pending = tasks.filter((t) => t.status === 'pending').length;
  const total = tasks.length;

  // Determine overall status
  const getOverallStatus = (): 'active' | 'success' | 'error' | 'warning' => {
    if (failed > 0) return 'error';
    if (completed === total && total > 0) return 'success';
    if (running > 0) return 'active';
    return 'warning';
  };

  return (
    <Box flexDirection="column">
      {/* Header with title and summary */}
      <Box marginBottom={showProgress ? 0 : 1}>
        <Text bold underline>
          {title}
        </Text>
        {!compact && total > 0 && (
          <Text dimColor>
            {' '}
            ({completed + failed}/{total} done
            {running > 0 && `, ${running} running`}
            {pending > 0 && `, ${pending} pending`})
          </Text>
        )}
      </Box>

      {/* Progress bar */}
      {showProgress && total > 0 && (
        <Box marginBottom={1}>
          <ProgressBar
            label=""
            current={completed}
            total={total}
            status={getOverallStatus()}
            width={40}
          />
        </Box>
      )}

      {/* Task list */}
      <Box flexDirection="column" height={maxHeight}>
        {tasks.length === 0 ? (
          <Text dimColor>No tasks yet</Text>
        ) : (
          tasks.map((task) => (
            <TaskItem
              key={task.id}
              task={task}
              showSpinner={showSpinner}
              compact={compact}
            />
          ))
        )}
      </Box>

      {/* Status summary footer */}
      {!compact && total > 0 && (failed > 0 || (completed === total && total > 0)) && (
        <Box marginTop={1} borderStyle="round" paddingX={1}>
          {completed === total && failed === 0 ? (
            <Text color={inkColors.success}>✓ All tasks completed successfully</Text>
          ) : failed > 0 ? (
            <Text color={inkColors.error}>
              ✖ {failed} task{failed > 1 ? 's' : ''} failed
            </Text>
          ) : null}
        </Box>
      )}
    </Box>
  );
};

/**
 * Compact progress indicator for inline use
 */
interface CompactProgressProps {
  current: number;
  total: number;
  status?: 'idle' | 'running' | 'completed' | 'failed';
  showSpinner?: boolean;
}

export const CompactProgress: React.FC<CompactProgressProps> = ({
  current,
  total,
  status = 'running',
  showSpinner = true,
}) => {
  const percentage = total > 0 ? Math.round((current / total) * 100) : 0;

  const getStatusIcon = (): React.ReactNode => {
    switch (status) {
      case 'idle':
        return <Text color={inkColors.muted}>○</Text>;
      case 'running':
        return showSpinner ? <Spinner type="dots" color={inkColors.warning} /> : <Text color={inkColors.warning}>◐</Text>;
      case 'completed':
        return <Text color={inkColors.success}>✓</Text>;
      case 'failed':
        return <Text color={inkColors.error}>✖</Text>;
    }
  };

  const getStatusColor = (): string => {
    switch (status) {
      case 'idle':
        return inkColors.muted;
      case 'running':
        return inkColors.warning;
      case 'completed':
        return inkColors.success;
      case 'failed':
        return inkColors.error;
    }
  };

  return (
    <Box>
      {getStatusIcon()}
      <Text color={getStatusColor()}>
        {' '}
        {current}/{total} ({percentage}%)
      </Text>
    </Box>
  );
};

/**
 * Full-featured progress view combining all components
 */
interface ProgressViewProps {
  tasks: LivePlanTask[];
  title?: string;
  showOverallProgress?: boolean;
  showTaskProgress?: boolean;
  showSpinner?: boolean;
  compact?: boolean;
}

export const ProgressView: React.FC<ProgressViewProps> = ({
  tasks,
  title = 'Progress',
  showOverallProgress = true,
  showTaskProgress = true,
  showSpinner = true,
  compact = false,
}) => {
  const completed = tasks.filter((t) => t.status === 'completed').length;
  const failed = tasks.filter((t) => t.status === 'failed').length;
  const running = tasks.filter((t) => t.status === 'running').length;
  const total = tasks.length;

  // Calculate elapsed time for running tasks (simulated for demo)
  const [elapsedSeconds, setElapsedSeconds] = useState(0);

  useEffect(() => {
    if (running > 0) {
      const timer = setInterval(() => {
        setElapsedSeconds((prev) => prev + 1);
      }, 1000);

      return () => clearInterval(timer);
    } else {
      setElapsedSeconds(0);
    }
  }, [running]);

  const formatElapsed = (seconds: number): string => {
    if (seconds < 60) return `${seconds}s`;
    const minutes = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return `${minutes}m ${secs}s`;
  };

  return (
    <Box flexDirection="column" paddingX={1}>
      {/* Overall progress header */}
      {showOverallProgress && (
        <Box flexDirection="column" marginBottom={1}>
          <Box>
            <Text bold underline color={inkColors.accent}>
              {title}
            </Text>
            {running > 0 && !compact && (
              <Text dimColor> - Running for {formatElapsed(elapsedSeconds)}</Text>
            )}
          </Box>

          {total > 0 && (
            <Box marginTop={0}>
              <ProgressBar
                label="Overall"
                current={completed}
                total={total}
                status={
                  failed > 0
                    ? 'error'
                    : completed === total
                      ? 'success'
                      : running > 0
                        ? 'active'
                        : 'warning'
                }
                width={50}
              />
            </Box>
          )}
        </Box>
      )}

      {/* Task list */}
      {showTaskProgress && (
        <TaskList
          tasks={tasks}
          title="Tasks"
          showProgress={false}
          showSpinner={showSpinner}
          compact={compact}
        />
      )}

      {/* Statistics */}
      {!compact && total > 0 && (
        <Box flexDirection="column" marginTop={1} borderStyle="single" paddingX={1}>
          <Text dimColor>Statistics:</Text>
          <Box>
            <Box width={15}>
              <Text>Completed:</Text>
            </Box>
            <Text color={inkColors.success}>{completed}</Text>
          </Box>
          <Box>
            <Box width={15}>
              <Text>Running:</Text>
            </Box>
            <Text color={inkColors.warning}>{running}</Text>
          </Box>
          <Box>
            <Box width={15}>
              <Text>Pending:</Text>
            </Box>
            <Text color={inkColors.muted}>{total - completed - running - failed}</Text>
          </Box>
          {failed > 0 && (
            <Box>
              <Box width={15}>
                <Text>Failed:</Text>
              </Box>
              <Text color={inkColors.error}>{failed}</Text>
            </Box>
          )}
        </Box>
      )}
    </Box>
  );
};
