#!/usr/bin/env node
/**
 * Compatibility test for the current element-based ReAct message model.
 *
 * The old `segments/content` fields were replaced by ordered `elements`.
 * Keep this filename so existing local verification commands still exercise
 * the same text -> tool -> text ordering contract.
 */

import { createInitialTuiState } from './src/state/tui-state.js';
import {
  addAssistantMessage,
  appendToLastAssistantMessage,
  insertToolCallIntoLastMessage,
  getMessageTextContent,
} from './src/state/message-history.js';

console.log('Testing element-based ReAct ordering\n');

let failures = 0;
function check(condition: boolean, message: string): void {
  if (condition) {
    console.log(`✓ ${message}`);
  } else {
    failures += 1;
    console.error(`✗ ${message}`);
  }
}

let state = createInitialTuiState();
state = addAssistantMessage(state, '', true);
state = appendToLastAssistantMessage(state, 'Let me check the datasources...');
state = insertToolCallIntoLastMessage(state, 'tool-list-ds-123');
state = appendToLastAssistantMessage(state, '\n\nNow inspecting the schema...');
state = insertToolCallIntoLastMessage(state, 'tool-inspect-456');
state = appendToLastAssistantMessage(state, '\n\nI found 5 tables.');

const message = state.messages[state.messages.length - 1];
const elementTypes = message?.elements.map((element) => element.type) ?? [];
const expected = ['text', 'tool_call', 'text', 'tool_call', 'text'];

check(JSON.stringify(elementTypes) === JSON.stringify(expected), 'elements preserve text/tool/text ordering');
check(
  getMessageTextContent(message) ===
    'Let me check the datasources...\n\nNow inspecting the schema...\n\nI found 5 tables.',
  'text extraction joins text elements and skips tool markers',
);
check(
  message?.elements.filter((element) => element.type === 'tool_call').length === 2,
  'tool calls remain separate message elements',
);

if (failures > 0) {
  console.error(`\n${failures} check(s) failed.`);
  process.exit(1);
}

console.log('\nAll checks passed.');
