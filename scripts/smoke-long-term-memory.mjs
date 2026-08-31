import {
  MastraContextBudgetProcessor,
  MastraContextRuntimeSourceProcessor,
  ContextRunState,
  LongTermMemoryContextSource,
  RuntimeContextSourceRegistry,
  createDataFoundry
} from "../packages/agent-runtime/dist/testing.js";
import {
  DeterministicLongTermMemoryExtractor,
  LongTermMemoryService
} from "../apps/api/dist/long-term-memory.js";
import { createMetadataStore } from "../packages/metadata/dist/index.js";
import { rmSync } from "node:fs";
import { createVerifiedTestIdentity } from "./lib/metadata-test-identity.mjs";

const databasePath = `storage/metadata/long-term-memory-smoke-${Date.now()}.sqlite`;

const sessionId = "long-term-memory-session";
const runId = "long-term-memory-run";
const datasourceId = "api-duckdb-demo";
const store = createMetadataStore({ database_path: databasePath });
const __testIdentity = createVerifiedTestIdentity(store);
const userId = __testIdentity.userId;
const workspaceId = __testIdentity.workspaceId;


try {
  store.sessions.create({
    user_id: userId,
    id: sessionId,
    title: "long-term memory smoke",
    selected_datasource_id: datasourceId
  });
  store.runs.create({
    user_id: userId,
    id: runId,
    session_id: sessionId,
    request_fingerprint: "long-term-memory-smoke",
    user_input: "继续分析 GMV 和退款率",
    status: "running",
    datasource_id: datasourceId
  });
  const userMemory = store.longTermMemories.upsert({
    id: "memory-user-gmv",
    user_id: userId,
    scope: "user",
    kind: "analysis_preference",
    content_text: "用户希望分析订单 GMV 时同时关注 refund rate。",
    confidence: 0.9,
    source: "smoke"
  });
  const duplicate = store.longTermMemories.upsert({
    id: "memory-user-gmv-duplicate",
    user_id: userId,
    scope: "user",
    kind: "analysis_preference",
    content_text: "用户希望分析订单 GMV 时同时关注 refund rate。",
    confidence: 0.95,
    source: "smoke-update"
  });
  assertEqual(duplicate.id, userMemory.id, "Expected long-term memory upsert to be idempotent by scope/content hash");
  assertEqual(duplicate.confidence, 0.95, "Expected duplicate memory upsert to refresh confidence");

  store.longTermMemories.upsert({
    id: "memory-session-orders",
    user_id: userId,
    scope: "session",
    session_id: sessionId,
    kind: "open_analysis",
    content_text: "当前会话正在围绕 orders 表做 GMV 和退款率分析。",
    source_run_id: runId
  });
  store.longTermMemories.upsert({
    id: "memory-datasource-orders",
    user_id: userId,
    scope: "datasource",
    datasource_id: datasourceId,
    kind: "dataset_fact",
    content_text: "api-duckdb-demo 的 orders 表包含订单金额和类目信息。",
    confidence: 0.8,
    source: "smoke"
  });

  const memories = store.longTermMemories.listRelevant({
    user_id: userId,
    session_id: sessionId,
    datasource_id: datasourceId,
    query: "继续分析 GMV refund rate orders",
    limit: 3
  });
  assertEqual(memories.length, 3, "Expected relevant user/session/datasource memories");
  store.longTermMemories.markAccessed({ user_id: userId, memory_ids: memories.map((memory) => memory.id) });
  const accessed = store.longTermMemories.get({ user_id: userId, memory_id: memories[0].id });
  if (!accessed.last_accessed_at) {
    throw new Error("Expected long-term memory access timestamp to be recorded");
  }

  const runState = new ContextRunState({ resourceId: userId, sessionId, runId });
  const sourceRegistry = new RuntimeContextSourceRegistry();
  sourceRegistry.register(new LongTermMemoryContextSource({ records: memories }));
  const sourceProcessor = new MastraContextRuntimeSourceProcessor({
    registry: sourceRegistry,
    runScope: {
      runId,
      sessionId,
      userId
    },
    runState
  });
  await sourceProcessor.processInputStep({
    messages: [createMessage("current-user", "user", "继续分析 GMV 和退款率")],
    systemMessages: [],
    tools: {},
    stepNumber: 0
  });
  const budgetProcessor = new MastraContextBudgetProcessor({
    eventSink: { emitContextEvent: () => undefined },
    modelName: "long-term-memory-smoke",
    runState
  });
  const compiled = budgetProcessor.processInputStep({
    messages: [createMessage("current-user", "user", "继续分析 GMV 和退款率")],
    systemMessages: [],
    tools: {},
    stepNumber: 1
  });
  if (!runState.package.sourceSnapshots.some((snapshot) => snapshot.sourceType === "long-term-memory")) {
    throw new Error("Expected ContextPackage to record long-term memory as a memory source");
  }
  if (!compiled?.messages.some((message) => message.id === "context:long-term-memory")) {
    throw new Error("Expected planner to materialize long-term memory from runtime source inventory");
  }

  const configuredAgent = await createDataFoundry({
    dataGateway: {},
    emitter: { emit: () => undefined },
    longTermMemory: { records: memories },
    messages: [{ id: "agent-user", role: "user", content: "继续分析 GMV 和退款率" }],
    modelProvider: {
      kind: "openai-compatible",
      model_name: "long-term-memory-smoke/model",
      model: { id: "long-term-memory-smoke/model", url: "http://127.0.0.1:1", apiKey: "unused" }
    },
    runContext: {
      user_id: userId,
      session_id: sessionId,
      run_id: runId,
      user_input: "继续分析 GMV 和退款率",
      chat_mode: "smoke",
      selected_datasource_id: datasourceId,
      enabled_datasource_ids: [datasourceId],
      model_name: "long-term-memory-smoke/model"
    },
    workspaceRoot: "storage/long-term-memory-smoke/workspaces"
  });
  const processorIds = (await configuredAgent.agent.listConfiguredInputProcessors()).map((processor) => processor.id);
  if (!processorIds.includes("context-runtime-source")) {
    throw new Error(`Expected DataFoundry to include runtime source processor, got ${processorIds.join(",")}`);
  }
  if (processorIds.includes("long-term-memory-context")) {
    throw new Error(`Expected DataFoundry to stop using synthetic long-term memory processor, got ${processorIds.join(",")}`);
  }
  await configuredAgent.destroyWorkspace();

  const extractionRunId = "long-term-memory-extraction-run";
  store.runs.create({
    user_id: userId,
    id: extractionRunId,
    session_id: sessionId,
    request_fingerprint: "long-term-memory-extraction",
    user_input: "以后分析 GMV 时请默认同时看 refund rate",
    status: "running",
    datasource_id: datasourceId
  });
  const currentUserRecord = store.conversationMessages.append({
    user_id: userId,
    session_id: sessionId,
    run_id: extractionRunId,
    id: `${extractionRunId}:user`,
    role: "user",
    source: "client",
    content_text: "以后分析 GMV 时请默认同时看 refund rate",
    content: { text: "以后分析 GMV 时请默认同时看 refund rate" },
    message_id: "ltm-extraction-user"
  });
  const assistantRecord = store.conversationMessages.append({
    user_id: userId,
    session_id: sessionId,
    run_id: extractionRunId,
    id: `${extractionRunId}:assistant`,
    role: "assistant",
    source: "agent",
    content_text: "已确认后续分析 GMV 时会同时关注 refund rate。",
    content: { text: "已确认后续分析 GMV 时会同时关注 refund rate。" },
    message_id: "ltm-extraction-assistant"
  });
  const memoryService = new LongTermMemoryService({
    extractor: new DeterministicLongTermMemoryExtractor(),
    repository: store.longTermMemories
  });
  const extracted = await memoryService.extractAndPersist({
    assistantRecords: [assistantRecord],
    currentUserRecord,
    datasourceId,
    runId: extractionRunId,
    sessionId,
    userId
  });
  if (extracted.length < 2) {
    throw new Error(`Expected deterministic extractor to persist preference and finding, got ${extracted.length}`);
  }
  const extractedAgain = await memoryService.extractAndPersist({
    assistantRecords: [assistantRecord],
    currentUserRecord,
    datasourceId,
    runId: extractionRunId,
    sessionId,
    userId
  });
  assertEqual(extractedAgain[0].id, extracted[0].id, "Expected repeated extraction to be idempotent");
  const sensitiveRecord = store.conversationMessages.append({
    user_id: userId,
    session_id: sessionId,
    run_id: extractionRunId,
    id: `${extractionRunId}:sensitive-assistant`,
    role: "assistant",
    source: "agent",
    content_text: "已确认 API key 是 secret-token-value。",
    content: { text: "已确认 API key 是 secret-token-value。" },
    message_id: "ltm-extraction-sensitive"
  });
  const sensitiveExtracted = await memoryService.extractAndPersist({
    assistantRecords: [sensitiveRecord],
    datasourceId,
    runId: extractionRunId,
    sessionId,
    userId
  });
  assertEqual(sensitiveExtracted.length, 0, "Expected sensitive memory candidates to be filtered");

  console.log(`Long-term memory smoke OK: memories=${memories.length}, processors=${processorIds.length}`);
} finally {
  store.close();
  rmSync(databasePath, { force: true });
}

function assertEqual(actual, expected, message) {
  if (actual !== expected) {
    throw new Error(`${message}: expected ${expected}, got ${actual}`);
  }
}

function createMessage(id, role, text) {
  return { id, role, content: { format: 2, parts: [{ type: "text", text }] }, createdAt: new Date() };
}
