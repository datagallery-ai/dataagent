#!/usr/bin/env node
/**
 * Test script for SlashCommandPopover
 * Run with: tsx test-slash-popover.tsx
 */
import React from 'react';
import { render, Box, Text } from 'ink';
import { InputBox } from './src/ui/InputBox.js';

const TestApp: React.FC = () => {
  const [value, setValue] = React.useState('');
  const [submitted, setSubmitted] = React.useState<string[]>([]);

  const handleSubmit = (val: string) => {
    setSubmitted([...submitted, val]);
    setValue('');
  };

  return (
    <Box flexDirection="column" padding={1}>
      <Box marginBottom={1}>
        <Text bold color="cyan">
          🧪 Slash Command Popover Test
        </Text>
      </Box>

      <Box marginBottom={1} flexDirection="column">
        <Text>
          Type <Text color="yellow">/</Text> to open the slash command popover
        </Text>
        <Text dimColor>
          • Use ↑↓ to navigate commands
        </Text>
        <Text dimColor>
          • Press Enter to select a command
        </Text>
        <Text dimColor>
          • Press Esc to close the popover
        </Text>
        <Text dimColor>
          • Type to filter commands (e.g., /he for help)
        </Text>
      </Box>

      <Box marginBottom={2} borderStyle="single" borderColor="gray" padding={1}>
        <InputBox
          value={value}
          onChange={setValue}
          onSubmit={handleSubmit}
          placeholder="Type / to see slash commands..."
          datasourceId="test-datasource"
          skillId="test-skill"
        />
      </Box>

      {submitted.length > 0 && (
        <Box flexDirection="column" borderStyle="single" borderColor="green" padding={1}>
          <Text bold color="green">Submitted Commands:</Text>
          {submitted.map((cmd, i) => (
            <Text key={i}>  {i + 1}. {cmd}</Text>
          ))}
        </Box>
      )}

      <Box marginTop={1}>
        <Text dimColor>Press Ctrl+C to exit</Text>
      </Box>
    </Box>
  );
};

render(<TestApp />);
