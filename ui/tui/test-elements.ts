#!/usr/bin/env node
/**
 * Test script for ReAct streaming with elements architecture
 *
 * This script validates the new element-based message structure:
 * 1. Text deltas append to text elements
 * 2. Tool calls insert as separate elements
 * 3. Elements maintain insertion order
 */

import { createInitialTuiState } from './src/state/tui-state.js';
import {
  addAssistantMessage,
  appendToLastAssistantMessage,
  insertToolCallIntoLastMessage,
  updateLastAssistantMessage,
  getMessageTextContent,
} from './src/state/message-history.js';

console.log('🧪 Testing ReAct Streaming with Elements Architecture\n');

// Test 1: Create initial assistant message
console.log('Test 1: Create initial assistant message');
let state = createInitialTuiState();
state = addAssistantMessage(state, '', true);
console.log('✓ Initial message created');
console.log(`  Messages: ${state.messages.length}`);
console.log(`  Elements: ${state.messages[0]?.elements?.length || 0}\n`);

// Test 2: Append text delta
console.log('Test 2: Append text delta');
state = appendToLastAssistantMessage(state, 'Let me check the datasources...');
const msg1 = state.messages[state.messages.length - 1];
console.log('✓ Text appended');
console.log(`  Elements: ${msg1?.elements?.length}`);
console.log(`  Text content: "${getMessageTextContent(msg1)}"`);
console.log(`  Last element type: ${msg1?.elements?.[msg1.elements.length - 1]?.type}\n`);

// Test 3: Insert tool call
console.log('Test 3: Insert tool call');
state = insertToolCallIntoLastMessage(state, 'tool-list-ds-123');
const msg2 = state.messages[state.messages.length - 1];
console.log('✓ Tool call inserted');
console.log(`  Elements: ${msg2?.elements?.length}`);
console.log(`  Element types: ${msg2?.elements?.map(e => e.type).join(', ')}`);
if (msg2?.elements?.[1]?.type === 'tool_call') {
  console.log(`  Tool call ID: ${msg2.elements[1].toolCallId}\n`);
}

// Test 4: Append more text after tool call
console.log('Test 4: Append more text after tool call');
state = appendToLastAssistantMessage(state, '\n\nNow inspecting the schema...');
const msg3 = state.messages[state.messages.length - 1];
console.log('✓ Text appended after tool call');
console.log(`  Elements: ${msg3?.elements?.length}`);
console.log(`  Element types: ${msg3?.elements?.map(e => e.type).join(', ')}`);
console.log(`  Text content: "${getMessageTextContent(msg3)}"\n`);

// Test 5: Insert another tool call
console.log('Test 5: Insert another tool call');
state = insertToolCallIntoLastMessage(state, 'tool-inspect-456');
const msg4 = state.messages[state.messages.length - 1];
console.log('✓ Second tool call inserted');
console.log(`  Elements: ${msg4?.elements?.length}`);
console.log(`  Element types: ${msg4?.elements?.map(e => e.type).join(', ')}\n`);

// Test 6: Append final text
console.log('Test 6: Append final text');
state = appendToLastAssistantMessage(state, '\n\nI found 5 tables.');
const msg5 = state.messages[state.messages.length - 1];
console.log('✓ Final text appended');
console.log(`  Elements: ${msg5?.elements?.length}`);
console.log(`  Element types: ${msg5?.elements?.map(e => e.type).join(', ')}`);
console.log(`  Is streaming: ${msg5?.isStreaming}`);
console.log(`  Full text: "${getMessageTextContent(msg5)}"\n`);

// Final validation
console.log('📊 Final State Validation:');
const finalMsg = msg5;
if (!finalMsg?.elements) {
  console.error('❌ FAIL: No elements found');
  process.exit(1);
}

const expectedPattern = ['text', 'tool_call', 'text', 'tool_call', 'text'];
let allValid = true;

if (finalMsg.elements.length !== expectedPattern.length) {
  console.error(`❌ FAIL: Expected ${expectedPattern.length} elements, got ${finalMsg.elements.length}`);
  allValid = false;
}

console.log('\nElement breakdown:');
for (let i = 0; i < Math.min(finalMsg.elements.length, expectedPattern.length); i++) {
  const expected = expectedPattern[i];
  const actual = finalMsg.elements[i];

  if (actual.type !== expected) {
    console.error(`❌ FAIL: Element ${i} type mismatch: expected ${expected}, got ${actual.type}`);
    allValid = false;
  } else {
    if (actual.type === 'text') {
      const preview = actual.content.replace(/\n/g, '\\n').substring(0, 40);
      console.log(`✓ Element ${i}: text "${preview}..."`);
    } else {
      console.log(`✓ Element ${i}: tool_call (${actual.toolCallId})`);
    }
  }
}

// Test getMessageTextContent
console.log('\n📝 Testing text extraction:');
const extractedText = getMessageTextContent(finalMsg);
const expectedText = 'Let me check the datasources...\n\nNow inspecting the schema...\n\nI found 5 tables.';
if (extractedText === expectedText) {
  console.log('✓ Text extraction works correctly');
  console.log(`  Extracted: "${extractedText}"`);
} else {
  console.error('❌ FAIL: Text extraction mismatch');
  console.error(`  Expected: "${expectedText}"`);
  console.error(`  Got: "${extractedText}"`);
  allValid = false;
}

console.log();
if (allValid) {
  console.log('✅ All tests passed! Elements-based ReAct streaming works correctly.');
  process.exit(0);
} else {
  console.log('❌ Some tests failed. Please review the implementation.');
  process.exit(1);
}
