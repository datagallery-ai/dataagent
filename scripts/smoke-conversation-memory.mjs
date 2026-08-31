import { EventType } from "@ag-ui/core";
import { rmSync } from "node:fs";

import {
  ConversationMemoryService,
  buildConversationMemoryMessages,
  buildConversationMemoryMessagesWithReport,
  persistCurrentUserMessage
} from "../apps/api/dist/conversation-memory.js";
import {
  CONVERSATION_WORKING_MEMORY_CONFIG,
  createAgentMemoryRuntime,
  createMastraConversationMemoryBridge
} from "../packages/agent-runtime/dist/index.js";
import { createMetadataStore } from "../packages/metadata/dist/index.js";
import { createVerifiedTestIdentity } from "./lib/metadata-test-identity.mjs";

const databasePath = `storage/metadata/conversation-memory-smoke-${Date.now()}.sqlite`;
const memoryDatabasePath = `storage/metadata/conversation-memory-shadow-${Date.now()}.sqlite`;
const consumingMemoryDatabasePath = `storage/metadata/conversation-memory-consuming-${Date.now()}.sqlite`;
const store = createMetadataStore({ database_path: databasePath });
const __testIdentity = createVerifiedTestIdentity(store);
const userId = __testIdentity.userId;
const workspaceId = __testIdentity.workspaceId;

let memoryRuntime;
let consumingMemoryRuntime;

try {
  memoryRuntime = await createAgentMemoryRuntime(memoryDatabasePath);
  consumingMemoryRuntime = await createAgentMemoryRuntime(consumingMemoryDatabasePath, {
    conversationMemoryMode: "working-memory-readonly"
  });
  const memoryBridge = createMastraConversationMemoryBridge({ memory: memoryRuntime.memory });
  const consumingMemoryBridge = createMastraConversationMemoryBridge({ memory: consumingMemoryRuntime.memory });
  
  const sessionId = "conversation-memory-session";
  const firstRunId = "conversation-memory-run-1";
  const secondRunId = "conversation-memory-run-2";
  const firstRunInput = {
    threadId: sessionId,
    runId: firstRunId,
    messages: [{ id: "user-message-1", role: "user", content: "分析 orders 表" }],
    tools: [],
    context: []
  };

  store.sessions.create({ user_id: userId, id: sessionId, title: "conversation memory smoke" });
  store.runs.create({
    user_id: userId,
    id: firstRunId,
    session_id: sessionId,
    request_fingerprint: "conversation-memory-first",
    user_input: "分析 orders 表",
    status: "running"
  });
  const firstService = new ConversationMemoryService({
    repository: store.conversationMessages,
    sessionId,
    userId
  });

  firstService.persistCurrentUserMessage({
    currentUserText: "分析 orders 表",
    runId: firstRunId,
    runInput: firstRunInput
  });

  const observer = firstService.createEventObserver({ runId: firstRunId });
  observer.observe({
    type: EventType.TEXT_MESSAGE_CHUNK,
    role: "assistant",
    messageId: "assistant-message-1",
    delta: "orders 表包含 3 行样例，"
  });
  observer.observe({
    type: EventType.TEXT_MESSAGE_CHUNK,
    role: "assistant",
    messageId: "assistant-message-1",
    delta: "建议先按 category 汇总。"
  });
  const assistantRecords = await observer.flushCompleted();
  if (assistantRecords.length !== 1) {
    throw new Error(`Expected one assistant message, got ${assistantRecords.length}`);
  }

  // Isolated session so reasoning fold checks do not pollute the main history assertions.
  const reasoningSessionId = "conversation-memory-reasoning-session";
  const reasoningRunId = "conversation-memory-run-reasoning";
  store.sessions.create({
    user_id: userId,
    id: reasoningSessionId,
    title: "conversation memory reasoning smoke"
  });
  store.runs.create({
    user_id: userId,
    id: reasoningRunId,
    session_id: reasoningSessionId,
    request_fingerprint: "conversation-memory-reasoning",
    user_input: "带 thinking 的分析",
    status: "running"
  });
  const reasoningService = new ConversationMemoryService({
    repository: store.conversationMessages,
    sessionId: reasoningSessionId,
    userId
  });
  const reasoningObserver = reasoningService.createEventObserver({ runId: reasoningRunId });
  reasoningObserver.observe({
    type: EventType.REASONING_MESSAGE_START,
    messageId: "reasoning-message-1",
    role: "reasoning"
  });
  reasoningObserver.observe({
    type: EventType.REASONING_MESSAGE_CONTENT,
    messageId: "reasoning-message-1",
    delta: "先检查 schema。"
  });
  reasoningObserver.observe({
    type: EventType.REASONING_MESSAGE_END,
    messageId: "reasoning-message-1"
  });
  reasoningObserver.observe({
    type: EventType.TEXT_MESSAGE_CHUNK,
    role: "assistant",
    messageId: "assistant-message-reasoning",
    delta: "查询完成。"
  });
  const reasoningRecords = await reasoningObserver.flushCompleted();
  if (reasoningRecords.length !== 2) {
    throw new Error(`Expected two folded assistant rows (reasoning + text), got ${reasoningRecords.length}`);
  }
  const reasoningOnly = reasoningRecords.find((record) => record.message_id === "reasoning-message-1");
  const textOnly = reasoningRecords.find((record) => record.message_id === "assistant-message-reasoning");
  if (!reasoningOnly || !textOnly) {
    throw new Error("Missing reasoning or text assistant rows after fold persist");
  }
  const reasoningJson = JSON.parse(reasoningOnly.content_json);
  if (!Array.isArray(reasoningJson.parts) || reasoningJson.parts[0]?.type !== "reasoning") {
    throw new Error(`Expected reasoning parts on reasoning row, got ${reasoningOnly.content_json}`);
  }
  if (reasoningOnly.content_text !== "先检查 schema。") {
    throw new Error(`Unexpected reasoning content_text: ${reasoningOnly.content_text}`);
  }
  const textJson = JSON.parse(textOnly.content_json);
  if (textOnly.content_text !== "查询完成。") {
    throw new Error(`Unexpected text content_text: ${textOnly.content_text}`);
  }
  if (textJson.parts) {
    throw new Error(`Text-only row should not include parts, got ${textOnly.content_json}`);
  }
  const reasoningHistory = buildConversationMemoryMessages({
    currentUserText: "继续",
    history: store.conversationMessages.listRecent({
      user_id: userId,
      session_id: reasoningSessionId,
      exclude_run_id: "conversation-memory-run-reasoning-next",
      limit: 24
    }),
    runId: "conversation-memory-run-reasoning-next",
    runInput: {
      threadId: reasoningSessionId,
      runId: "conversation-memory-run-reasoning-next",
      messages: [{ id: "user-next", role: "user", content: "继续" }],
      tools: [],
      context: []
    }
  });
  if (reasoningHistory.some((message) => String(message.content).includes("先检查 schema。"))) {
    throw new Error("Reasoning-only folded rows must not be re-fed into model history");
  }
  if (!reasoningHistory.some((message) => String(message.content).includes("查询完成。"))) {
    throw new Error("Text assistant rows from reasoning runs must remain in model history");
  }

  // Tool-only parents (write_file / edit_file style) must persist by message_id.
  const toolParentSessionId = "conversation-memory-tool-parent-session";
  const toolParentRunId = "conversation-memory-run-tool-parent";
  store.sessions.create({
    user_id: userId,
    id: toolParentSessionId,
    title: "conversation memory tool-parent smoke"
  });
  store.runs.create({
    user_id: userId,
    id: toolParentRunId,
    session_id: toolParentSessionId,
    request_fingerprint: "conversation-memory-tool-parent",
    user_input: "写报告并发布",
    status: "running"
  });
  const toolParentService = new ConversationMemoryService({
    repository: store.conversationMessages,
    sessionId: toolParentSessionId,
    userId
  });
  toolParentService.persistCurrentUserMessage({
    currentUserText: "写报告并发布",
    runId: toolParentRunId,
    runInput: {
      threadId: toolParentSessionId,
      runId: toolParentRunId,
      messages: [{ id: "user-tool-parent", role: "user", content: "写报告并发布" }],
      tools: [],
      context: []
    }
  });
  const toolParentObserver = toolParentService.createEventObserver({ runId: toolParentRunId });
  toolParentObserver.observe({
    type: EventType.TOOL_CALL_START,
    toolCallId: "write-file-call",
    toolCallName: "write_file",
    parentMessageId: "msg-write-parent"
  });
  toolParentObserver.observe({
    type: EventType.TOOL_CALL_START,
    toolCallId: "edit-file-call",
    toolCallName: "edit_file",
    parentMessageId: "msg-edit-parent"
  });
  toolParentObserver.observe({
    type: EventType.TEXT_MESSAGE_CHUNK,
    role: "assistant",
    messageId: "msg-final-answer",
    delta: "报告已发布。"
  });
  const toolParentRecords = await toolParentObserver.flushCompleted();
  const uniqueToolParentMessageIds = [...new Set(toolParentRecords.map((record) => record.message_id))];
  const writeParent = toolParentRecords.find((record) => record.message_id === "msg-write-parent");
  const editParent = toolParentRecords.find((record) => record.message_id === "msg-edit-parent");
  const finalAnswer = toolParentRecords.find((record) => record.message_id === "msg-final-answer");
  if (!writeParent || !editParent) {
    throw new Error(
      `Expected empty tool-parent rows for write/edit, got message_ids=${uniqueToolParentMessageIds.join(",")}`
    );
  }
  if (uniqueToolParentMessageIds.length !== toolParentRecords.length) {
    throw new Error(
      `Expected distinct persisted tool-parent rows, got duplicates: ${toolParentRecords
        .map((record) => record.message_id)
        .join(",")}`
    );
  }
  if (!finalAnswer) {
    throw new Error("Expected final text assistant row after tool-parent persist");
  }
  const writeParentJson = JSON.parse(writeParent.content_json);
  const editParentJson = JSON.parse(editParent.content_json);
  if (writeParentJson.kind !== "tool_parent" || editParentJson.kind !== "tool_parent") {
    throw new Error(
      `Expected kind=tool_parent on empty parents, got write=${writeParent.content_json} edit=${editParent.content_json}`
    );
  }
  if (writeParent.content_text !== "" || editParent.content_text !== "") {
    throw new Error("Empty tool-parent content_text must be empty string");
  }
  const toolParentHistory = buildConversationMemoryMessages({
    currentUserText: "继续",
    history: store.conversationMessages.listRecent({
      user_id: userId,
      session_id: toolParentSessionId,
      exclude_run_id: "conversation-memory-run-tool-parent-next",
      limit: 24
    }),
    runId: "conversation-memory-run-tool-parent-next",
    runInput: {
      threadId: toolParentSessionId,
      runId: "conversation-memory-run-tool-parent-next",
      messages: [{ id: "user-next-tp", role: "user", content: "继续" }],
      tools: [],
      context: []
    }
  });
  if (toolParentHistory.some((message) => message.id.includes("msg-write-parent") || message.id.includes("msg-edit-parent"))) {
    throw new Error("Empty tool-parent rows must not appear in model history by message id");
  }
  if (
    toolParentHistory.filter((message) => message.role === "assistant").some((message) => String(message.content).trim() === "")
  ) {
    throw new Error("Empty tool-parent rows must not be re-fed as empty assistant history");
  }
  if (!toolParentHistory.some((message) => String(message.content).includes("报告已发布。"))) {
    throw new Error("Text assistant rows after tool parents must remain in model history");
  }
  const persistedMessageIds = new Set(
    store.conversationMessages
      .listRecent({
        user_id: userId,
        session_id: toolParentSessionId,
        limit: 24
      })
      .map((record) => record.message_id)
      .filter(Boolean)
  );
  for (const parentId of ["msg-write-parent", "msg-edit-parent"]) {
    if (!persistedMessageIds.has(parentId)) {
      throw new Error(`Expected persisted message_id=${parentId} so restore has zero orphans`);
    }
  }

  store.runEvents.append({
    user_id: userId,
    session_id: sessionId,
    run_id: firstRunId,
    event: {
      type: EventType.TOOL_CALL_START,
      toolCallId: "inspect-schema-call",
      toolCallName: "Inspect data source schema",
      parentMessageId: "assistant-message-1",
      args: { datasourceId: "orders-demo" }
    }
  });
  store.runEvents.append({
    user_id: userId,
    session_id: sessionId,
    run_id: firstRunId,
    event: {
      type: EventType.TOOL_CALL_RESULT,
      toolCallId: "inspect-schema-call",
      toolCallName: "Inspect data source schema",
      messageId: "inspect-schema-result",
      content: {
        tables: [
          { name: "orders", columns: ["order_id", "gmv", "refund_rate", "gross_margin_rate"] }
        ]
      }
    }
  });
  store.runEvents.append({
    user_id: userId,
    session_id: sessionId,
    run_id: firstRunId,
    event: {
      type: EventType.TOOL_CALL_START,
      toolCallId: "missing-result-call",
      toolCallName: "Preview table",
      parentMessageId: "assistant-message-1"
    }
  });
  const checkpointService = new ConversationMemoryService({
    repository: store.conversationMessages,
    sessionId,
    userId
  });
  const resumeAfterCanceledRun = checkpointService.buildRunMessages({
    currentUserText: "继续，不要重复查看结构",
    runId: "conversation-memory-run-after-cancel",
    runInput: {
      threadId: sessionId,
      runId: "conversation-memory-run-after-cancel",
      messages: [{ id: "user-after-cancel", role: "user", content: "继续，不要重复查看结构" }],
      tools: [],
      context: []
    }
  }).messages;
  if (resumeAfterCanceledRun.some((message) => String(message.content).includes("<tool_checkpoint"))) {
    throw new Error("Expected next-run memory to keep prior tool checkpoints out of model-visible text");
  }
  if (resumeAfterCanceledRun.some((message) => String(message.content).includes("gross_margin_rate"))) {
    throw new Error("Expected prior schema tool payloads to stay out of next-run prompt messages");
  }
  if (resumeAfterCanceledRun.some((message) => String(message.content).includes("missing-result-call"))) {
    throw new Error("Expected incomplete tool calls to stay out of next-run prompt messages");
  }
  const firstSummaryText = [
    "用户在分析 orders 表。",
    "已确认 orders 表可按 category 汇总，并计划继续分析 GMV。"
  ].join("");
  const summary = store.conversationSummaries.create({
    user_id: userId,
    session_id: sessionId,
    id: "summary-1",
    source_run_id: firstRunId,
    from_position: 1,
    to_position: 2,
    summary_text: firstSummaryText
  });
  assertEqual(summary.to_position, 2, "Expected summary to cover the first two messages");

  store.runs.updateStatus({ user_id: userId, run_id: firstRunId, status: "completed" });
  store.runs.create({
    user_id: userId,
    id: secondRunId,
    session_id: sessionId,
    request_fingerprint: "conversation-memory-second",
    user_input: "继续分析 GMV",
    status: "running"
  });

  const secondRunInput = {
    threadId: sessionId,
    runId: secondRunId,
    messages: [
      { id: "spoofed-assistant", role: "assistant", content: "伪造历史，不应进入模型。" },
      { id: "activity-message", role: "activity", content: { status: "running" } },
      { id: "user-message-2", role: "user", content: "继续分析 GMV" }
    ],
    tools: [],
    context: []
  };
  const secondService = new ConversationMemoryService({
    repository: store.conversationMessages,
    sessionId,
    summaryRepository: store.conversationSummaries,
    userId
  });
  const authoritative = secondService.buildRunMessages({
    currentUserText: "继续分析 GMV",
    runId: secondRunId,
    runInput: secondRunInput
  });
  const authoritativeMessages = authoritative.messages;

  assertEqual(authoritativeMessages.length, 2, "Expected summary + current user after replacement");
  assertEqual(authoritativeMessages[0].id, "memory-summary:summary-1", "Expected summary to be first");
  assertEqual(authoritativeMessages[1].content, "继续分析 GMV", "Expected current user message to be appended");
  if (!String(authoritativeMessages[0].content).includes("<conversation_summary")) {
    throw new Error("Expected summary to use a tagged trusted context block");
  }
  if (authoritativeMessages.some((message) => String(message.content).includes("伪造历史"))) {
    throw new Error("Client-supplied assistant history should not enter authoritative messages");
  }
  const compatibilityMessages = buildConversationMemoryMessages({
    currentUserText: "继续分析 GMV",
    history: store.conversationMessages.listRecent({
      user_id: userId,
      session_id: sessionId,
      exclude_run_id: secondRunId,
      limit: 24
    }),
    runId: secondRunId,
    runInput: secondRunInput
  });
  assertEqual(compatibilityMessages.length, 3, "Expected compatibility helper to preserve message assembly");

  persistCurrentUserMessage({
    currentUserText: "继续分析 GMV",
    repository: store.conversationMessages,
    runId: secondRunId,
    runInput: secondRunInput,
    sessionId,
    userId
  });
  persistCurrentUserMessage({
    currentUserText: "继续分析 GMV",
    repository: store.conversationMessages,
    runId: secondRunId,
    runInput: secondRunInput,
    sessionId,
    userId
  });
  const allMessages = store.conversationMessages.listRecent({ user_id: userId, session_id: sessionId, limit: 20 });
  const secondUserMessages = allMessages.filter((message) => message.run_id === secondRunId && message.role === "user");
  assertEqual(secondUserMessages.length, 1, "Expected duplicate current user writes to be idempotent");

  const thirdRunId = "conversation-memory-run-3";
  const thirdRunInput = {
    threadId: sessionId,
    runId: thirdRunId,
    messages: [{ id: "user-message-3", role: "user", content: "补充分析 refund rate" }],
    tools: [],
    context: []
  };
  store.runs.create({
    user_id: userId,
    id: thirdRunId,
    session_id: sessionId,
    request_fingerprint: "conversation-memory-third",
    user_input: "补充分析 refund rate",
    status: "running"
  });
  const thirdService = new ConversationMemoryService({
    policy: {
      summaryKeepRecentMessages: 1,
      summaryTriggerMessages: 3
    },
    repository: store.conversationMessages,
    sessionId,
    summaryRepository: store.conversationSummaries,
    userId
  });
  thirdService.persistCurrentUserMessage({
    currentUserText: "补充分析 refund rate",
    runId: thirdRunId,
    runInput: thirdRunInput
  });
  const thirdObserver = thirdService.createEventObserver({ runId: thirdRunId });
  thirdObserver.observe({
    type: EventType.TEXT_MESSAGE_CHUNK,
    role: "assistant",
    messageId: "assistant-message-3",
    delta: "已将 refund rate 作为后续分析指标。"
  });
  await thirdObserver.flushCompleted();
  const automaticSummary = store.conversationSummaries.latest({ user_id: userId, session_id: sessionId });
  assertEqual(automaticSummary?.to_position, 4, "Expected automatic summary to advance summarized range");
  if (!automaticSummary?.summary_text.includes("Previous summary")) {
    throw new Error("Expected automatic summary to retain previous summary text");
  }
  const afterAutoSummary = thirdService.buildRunMessages({
    currentUserText: "下一步给出结论",
    runId: "conversation-memory-run-4",
    runInput: {
      threadId: sessionId,
      runId: "conversation-memory-run-4",
      messages: [{ id: "user-message-4", role: "user", content: "下一步给出结论" }],
      tools: [],
      context: []
    }
  }).messages;
  assertEqual(afterAutoSummary[0].id, `memory-summary:${automaticSummary?.id}`, "Expected latest summary first");
  const rawSummarizedIds = new Set([
    `memory:${secondRunId}:user`,
    `memory:${thirdRunId}:user`
  ]);
  if (afterAutoSummary.some((message) => rawSummarizedIds.has(message.id))) {
    throw new Error("Expected summarized positions to be replaced by latest summary");
  }

  const llmSummaryRunId = "conversation-memory-run-llm-summary";
  store.runs.create({
    user_id: userId,
    id: llmSummaryRunId,
    session_id: sessionId,
    request_fingerprint: "conversation-memory-llm-summary",
    user_input: "继续压缩记忆",
    status: "running"
  });
  const customSummaryService = new ConversationMemoryService({
    memoryBridge,
    policy: {
      summaryKeepRecentMessages: 1,
      summaryTriggerMessages: 2
    },
    repository: store.conversationMessages,
    sessionId,
    summarizer: {
      kind: "smoke-custom",
      summarize: () => "LLM summary: 保留用户正在分析 GMV 和 refund rate。"
    },
    summaryRepository: store.conversationSummaries,
    userId
  });
  customSummaryService.persistCurrentUserMessage({
    currentUserText: "继续压缩记忆",
    runId: llmSummaryRunId,
    runInput: {
      threadId: sessionId,
      runId: llmSummaryRunId,
      messages: [{ id: "user-message-llm-summary", role: "user", content: "继续压缩记忆" }],
      tools: [],
      context: []
    }
  });
  const customSummaryObserver = customSummaryService.createEventObserver({ runId: llmSummaryRunId });
  customSummaryObserver.observe({
    type: EventType.TEXT_MESSAGE_CHUNK,
    role: "assistant",
    messageId: "assistant-message-llm-summary",
    delta: "已更新记忆压缩结果。"
  });
  await customSummaryObserver.flushCompleted();
  const customSummary = store.conversationSummaries.latest({ user_id: userId, session_id: sessionId });
  if (!customSummary?.summary_text.startsWith("LLM summary:")) {
    throw new Error("Expected custom summarizer output to be persisted");
  }
  const mirroredWorkingMemory = await memoryRuntime.memory.getWorkingMemory({
    threadId: sessionId,
    resourceId: userId,
    memoryConfig: CONVERSATION_WORKING_MEMORY_CONFIG
  });
  if (!mirroredWorkingMemory?.includes(customSummary.summary_text)) {
    throw new Error("Expected latest summary to be mirrored into Mastra WorkingMemory");
  }
  if (!mirroredWorkingMemory.includes(`to_position: ${customSummary.to_position}`)) {
    throw new Error("Expected mirrored WorkingMemory to retain summary range metadata");
  }
  const shadowModeMessages = customSummaryService.buildRunMessages({
    currentUserText: "验证 shadow memory 不注入 prompt",
    runId: "conversation-memory-shadow-read",
    runInput: {
      threadId: sessionId,
      runId: "conversation-memory-shadow-read",
      messages: [{ id: "user-message-shadow-read", role: "user", content: "验证 shadow memory 不注入 prompt" }],
      tools: [],
      context: []
    }
  }).messages;
  assertEqual(
    shadowModeMessages[0].id,
    `memory-summary:${customSummary.id}`,
    "Expected shadow mode to keep metadata summary as the prompt-visible compact history"
  );
  const workingMemoryModeService = new ConversationMemoryService({
    compactMemorySource: "mastra-working-memory",
    memoryBridge: consumingMemoryBridge,
    repository: store.conversationMessages,
    sessionId,
    summaryRepository: store.conversationSummaries,
    userId
  });
  const synced = await workingMemoryModeService.syncLatestSummaryToMemory();
  assertEqual(synced, true, "Expected latest metadata summary to sync into Mastra WorkingMemory before prompt build");
  const workingMemoryModeMessages = workingMemoryModeService.buildRunMessages({
    currentUserText: "验证 working memory 只读消费",
    runId: "conversation-memory-working-read",
    runInput: {
      threadId: sessionId,
      runId: "conversation-memory-working-read",
      messages: [{ id: "user-message-working-read", role: "user", content: "验证 working memory 只读消费" }],
      tools: [],
      context: []
    }
  }).messages;
  if (workingMemoryModeMessages.some((message) => message.id.startsWith("memory-summary:"))) {
    throw new Error("Expected WorkingMemory mode to omit duplicate metadata summary messages");
  }
  assertEqual(
    workingMemoryModeMessages.at(-1)?.content,
    "验证 working memory 只读消费",
    "Expected WorkingMemory mode to keep the current user message"
  );
  const mastraMemoryContext = await consumingMemoryRuntime.memory.getContext({
    threadId: sessionId,
    resourceId: userId
  });
  if (!mastraMemoryContext.systemMessage.includes(customSummary.summary_text)) {
    throw new Error("Expected Mastra Memory context to contain the mirrored conversation summary");
  }
  if (mastraMemoryContext.systemMessage.includes("updateWorkingMemory")) {
    throw new Error("Expected read-only Mastra Memory context to omit memory write instructions");
  }

  const budgeted = buildConversationMemoryMessagesWithReport({
    currentUserText: "current user must remain",
    history: [
      createHistoryRecord("old-1", "user", "older ".repeat(200), 1),
      createHistoryRecord("old-2", "assistant", "middle ".repeat(200), 2),
      createHistoryRecord("old-3", "user", "recent ".repeat(10), 3)
    ],
    policy: {
      maxHistoryMessages: 3,
      maxHistoryTokens: 140,
      maxMessageChars: 2000,
      maxTotalChars: 2000
    },
    runId: "budget-run",
    runInput: {
      threadId: sessionId,
      runId: "budget-run",
      messages: [{ id: "budget-current-user", role: "user", content: "current user must remain" }],
      tools: [],
      context: []
    },
    summary: createSummaryRecord("budget-summary", 1, 2, "Earlier turns established the active analysis context.")
  });
  assertEqual(budgeted.messages.at(-1)?.content, "current user must remain", "Expected current user to remain");
  assertEqual(budgeted.report.includedSummary, true, "Expected budgeted window to include compact summary");
  assertEqual(budgeted.messages[0].id, "memory-summary:budget-summary", "Expected summary to lead the window");
  if (budgeted.report.droppedHistoryMessages < 1) {
    throw new Error("Expected budgeted window to drop at least one historical message");
  }

  console.log(
    `Conversation memory smoke OK: history=${authoritativeMessages.length}, persisted=${allMessages.length}, ` +
      `dropped=${budgeted.report.droppedHistoryMessages}`
  );
} finally {
  await memoryRuntime?.close();
  await consumingMemoryRuntime?.close();
  store.close();
  rmSync(memoryDatabasePath, { force: true });
  rmSync(consumingMemoryDatabasePath, { force: true });
}

function assertEqual(actual, expected, message) {
  if (actual !== expected) {
    throw new Error(`${message}: expected ${expected}, got ${actual}`);
  }
}

function createHistoryRecord(id, role, content, position) {
  return {
    id,
    user_id: userId,
    session_id: "conversation-memory-session",
    run_id: `history-${id}`,
    role,
    source: role === "user" ? "client" : "agent",
    message_id: `message-${id}`,
    content_json: JSON.stringify({ text: content }),
    content_text: content,
    content_hash: id,
    position,
    created_at: new Date().toISOString()
  };
}

function createSummaryRecord(id, fromPosition, toPosition, summaryText) {
  return {
    id,
    user_id: userId,
    session_id: "conversation-memory-session",
    from_position: fromPosition,
    to_position: toPosition,
    summary_text: summaryText,
    summary_hash: id,
    created_at: new Date().toISOString()
  };
}
