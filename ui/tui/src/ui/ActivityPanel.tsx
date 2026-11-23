import React from 'react';
import { Box, Text } from 'ink';
import type { LivePlanTask, LiveToolCallRecord, TimelineEvent } from '../state/index.js';
import { ToolTraceList } from './ToolTraceList.js';
import { getStatusColor } from './theme.js';

interface ActivityPanelProps {
  plan: LivePlanTask[];
  toolCalls: LiveToolCallRecord[];
  events: TimelineEvent[];
}

export const ActivityPanel: React.FC<ActivityPanelProps> = ({
  plan,
  toolCalls,
  events,
}) => {
  // Get status icon for plan task
  const getTaskIcon = (status: LivePlanTask['status']) => {
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

  return (
    <Box flexDirection="column">
      {/* Plan section */}
      <Box flexDirection="column" marginBottom={1}>
        <Text bold underline>
          Plan
        </Text>
        {plan.length === 0 ? (
          <Text dimColor>No plan yet</Text>
        ) : (
          plan.map((task) => (
            <Box key={task.id} marginTop={0}>
              <Text color={getStatusColor(task.status)}>
                {getTaskIcon(task.status)}
              </Text>
              <Text> {task.title}</Text>
            </Box>
          ))
        )}
      </Box>

      {/* Tool calls section */}
      <Box flexDirection="column" flexGrow={1}>
        <Text bold underline>
          Tool Calls
        </Text>
        <ToolTraceList toolCalls={toolCalls} events={events} />
      </Box>
    </Box>
  );
};
