#!/usr/bin/env node
/**
 * Quick test for EnhancedInputBox
 *
 * This script creates a minimal Ink app to test the new input component.
 * Run: node dist/test-enhanced-input.js
 */

import { render, Box, Text } from 'ink';
import React, { useState } from 'react';
import { EnhancedInputBox } from './ui/components/EnhancedInputBox.js';

const TestApp = () => {
  const [submitted, setSubmitted] = useState<string[]>([]);
  const [current, setCurrent] = useState('');

  return (
    <Box flexDirection="column" padding={1}>
      <Box marginBottom={1}>
        <Text bold color="green">Enhanced Input Box Test</Text>
      </Box>

      <Box marginBottom={1}>
        <Text dimColor>
          Try: Type and press Enter, Shift+Enter for newline, Paste large text, Ctrl+U to clear
        </Text>
      </Box>

      <EnhancedInputBox
        onChange={setCurrent}
        onSubmit={(value) => {
          setSubmitted(prev => [...prev, value]);
          console.log('\n=== SUBMITTED ===');
          console.log(value);
          console.log('=================\n');
        }}
        commands={['/test', '/clear', '/quit']}
        placeholder="Type something or paste large content..."
        modelName="test-model"
        datasourceId="test-db"
      />

      {submitted.length > 0 && (
        <Box flexDirection="column" marginTop={1}>
          <Text bold>Last 3 submissions:</Text>
          {submitted.slice(-3).map((item, index) => (
            <Box key={index} marginLeft={2}>
              <Text dimColor>
                {index + 1}. {item.length > 60 ? item.slice(0, 60) + '...' : item}
                {' '}
                <Text color="cyan">({item.length} chars)</Text>
              </Text>
            </Box>
          ))}
        </Box>
      )}

      <Box marginTop={1}>
        <Text dimColor>Current length: {current.length} chars</Text>
      </Box>
    </Box>
  );
};

// Render the app
const instance = render(<TestApp />);

// Handle exit
process.on('SIGINT', () => {
  instance.unmount();
  process.exit(0);
});
