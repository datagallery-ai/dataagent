/**
 * Example usage of EnhancedInputBox
 *
 * This file demonstrates how to use the new EnhancedInputBox component
 * with multi-line support and large paste handling.
 */

import React, { useState } from 'react';
import { Box, Text } from 'ink';
import { EnhancedInputBox } from './EnhancedInputBox.js';

/**
 * Basic example - minimal setup
 */
export const BasicExample = () => {
  const [input, setInput] = useState('');

  return (
    <Box flexDirection="column">
      <Text>Basic Input Example</Text>
      <EnhancedInputBox
        onChange={setInput}
        onSubmit={(value) => {
          console.log('Submitted:', value);
        }}
        placeholder="Type something and press Enter..."
      />
    </Box>
  );
};

/**
 * Full example - with all props
 */
export const FullExample = () => {
  const [input, setInput] = useState('');
  const [disabled, setDisabled] = useState(false);
  const [history, setHistory] = useState<string[]>([]);

  const handleSubmit = (value: string) => {
    console.log('Submitted:', value);
    setHistory((prev) => [...prev, value]);
  };

  return (
    <Box flexDirection="column">
      <Text bold>Enhanced Input Box Demo</Text>
      <Text dimColor>Try pasting large content (over 1000 chars or over 10 lines)</Text>

      <EnhancedInputBox
        onChange={setInput}
        onSubmit={handleSubmit}
        disabled={disabled}
        commands={['/help', '/clear', '/quit', '/paste']}
        placeholder="Type a command or paste large content..."
        modelName="gpt-4"
        datasourceId="my-database"
        skillId="data-analysis"
      />

      {history.length > 0 && (
        <Box flexDirection="column" marginTop={1}>
          <Text bold>History:</Text>
          {history.slice(-5).map((item, index) => (
            <Text key={index} dimColor>
              {index + 1}. {item.length > 50 ? item.slice(0, 50) + '...' : item}
            </Text>
          ))}
        </Box>
      )}
    </Box>
  );
};

/**
 * Test scenarios for the enhanced input box
 */
export const testScenarios = {
  /**
   * Test 1: Small paste (should insert directly)
   */
  smallPaste: `SELECT * FROM users WHERE active = true`,

  /**
   * Test 2: Large paste (should be collapsed to placeholder)
   */
  largePaste: `
    SELECT
      u.id,
      u.name,
      u.email,
      u.created_at,
      COUNT(o.id) as order_count,
      SUM(o.total) as total_spent
    FROM users u
    LEFT JOIN orders o ON u.id = o.user_id
    WHERE u.active = true
      AND u.created_at > '2023-01-01'
      AND u.email LIKE '%@example.com'
    GROUP BY u.id, u.name, u.email, u.created_at
    HAVING COUNT(o.id) > 5
    ORDER BY total_spent DESC
    LIMIT 100;

    -- This is a comment explaining the query
    -- The query finds top customers by spend
    -- Only includes active users from 2023
    -- Minimum 5 orders required
  `.repeat(10), // Repeat to make it > 1000 chars

  /**
   * Test 3: Multi-line input using Shift+Enter
   */
  multiLineDemo: [
    'SELECT *',
    'FROM users',
    'WHERE active = true;',
  ].join('\n'),

  /**
   * Test 4: Unicode and emoji
   */
  unicodeTest: `Hello 👋 World 🌍! 你好 مرحبا`,
};

/**
 * Instructions for testing
 */
export const testInstructions = `
Enhanced Input Box Testing Guide
=================================

1. Basic Input
   - Type some text
   - Press Enter to submit
   - Press Shift+Enter to add a new line

2. Large Paste Test
   - Copy the largePaste content (see testScenarios)
   - Paste it into the input box
   - You should see: [Pasted Content XXXX chars]
   - Press Enter - it will expand and submit

3. Multi-Paste Test
   - Paste large content multiple times
   - You should see: [Pasted Content XXXX chars] #1, #2, etc.
   - Press Backspace at the end of a placeholder - entire placeholder deleted
   - Paste again - ID #1 is reused

4. Navigation
   - Use arrow keys to navigate in multi-line input
   - Ctrl+A: Move to start of line
   - Ctrl+E: Move to end of line
   - Ctrl+K: Delete from cursor to end of line
   - Ctrl+U: Clear entire input
   - Ctrl+W: Delete previous word

5. Command Completion
   - Type "/" and press Tab
   - Available commands appear as hints
   - Press Tab again to cycle through completions

6. History
   - Press Up arrow: Previous command
   - Press Down arrow: Next command
   - History persists across submits

7. Unicode Test
   - Type or paste: Hello 👋 World 🌍!
   - Cursor should move correctly through emojis
   - Backspace should delete entire emoji

Expected Behaviors
==================

✓ Pasting < 1000 chars: Insert directly
✓ Pasting > 1000 chars OR > 10 lines: Show placeholder
✓ Multiple pastes: Assign unique IDs
✓ Backspace on placeholder: Delete entire placeholder
✓ Submit with placeholder: Auto-expand to full content
✓ Multi-line: Shift+Enter inserts newline
✓ Navigation: Arrow keys work in multi-line
✓ Emoji: Counted as single character
✓ Visual feedback: "✓ Pasted (collapsed)" shown briefly
`;
