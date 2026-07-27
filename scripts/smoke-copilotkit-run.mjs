import assert from "node:assert/strict";
import { createServer as createHttpServer } from "node:http";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { DatabaseSync } from "node:sqlite";

import { EventType } from "@ag-ui/core";
import { createAuthenticatedTestClient } from "./lib/authenticated-test-client.mjs";

const root = mkdtempSync(join(tmpdir(), "open-data-foundry-copilotkit-run-"));
const metadataPath = join(root, "metadata.sqlite");
const mastraStoragePath = join(root, "mastra.sqlite");
const workspaceRoot = join(root, "workspaces");

process.env.DATAFOUNDRY_AUTH_MODE = "password";
process.env.AUTH_SESSION_SECRET = "copilotkit-run-session-secret-32b!!!";
process.env.AUTH_PUBLIC_BASE_URL = "http://127.0.0.1:3000";
process.env.AUTH_EMAIL_DELIVERY = "test";
process.env.AUTH_REGISTRATION_MODE = "open";
process.env.EMBEDDING_API_KEY = "";
process.env.LLM_PROVIDER = "openai-compatible";
process.env.LLM_MODEL = "copilotkit-smoke-model";
process.env.LLM_API_KEY = "copilotkit-smoke-key";
process.env.MASTRA_STORAGE_PATH = mastraStoragePath;
process.env.MEMORY_EXTRACTION_TIMEOUT_MS = "25";
process.env.METADATA_DB_PATH = metadataPath;
process.env.SECRET_MASTER_KEY = "copilotkit-smoke-secret-master-key";
process.env.STORAGE_ROOT_DIR = root;
process.env.WORKSPACE_ROOT = workspaceRoot;

let llmRequestCount = 0;
const llmRequests = [];
const modelProviderServer = createHttpServer(async (request, response) => {
  const chunks = [];
  for await (const chunk of request) {
    chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
  }
  const body = chunks.length > 0 ? JSON.parse(Buffer.concat(chunks).toString("utf8")) : {};
  llmRequestCount += 1;
  llmRequests.push({
    authorization: request.headers.authorization,
    body,
    method: request.method,
    path: request.url
  });

  if (body.stream) {
    response.writeHead(200, {
      "Connection": "close",
      "Content-Type": "text/event-stream; charset=utf-8"
    });
    const latestUserText = [...(body.messages ?? [])].reverse().find((message) => message.role === "user")?.content;
    const hasToolResult = (body.messages ?? []).some((message) => message.role === "tool");
    if (String(latestUserText).includes("需要询问用户") && !hasToolResult) {
      writeStreamChunk(response, body.model, { role: "assistant" });
      writeStreamChunk(response, body.model, {
        tool_calls: [{
          index: 0,
          id: "call_ask_user",
          type: "function",
          function: {
            name: "ask_user",
            arguments: JSON.stringify({
              question: "请选择要分析的数据源",
              options: [{ label: "api-duckdb-demo" }]
            })
          }
        }]
      });
      writeStreamDone(response, body.model, "tool_calls");
      return;
    }
    if (String(latestUserText).includes("使用 copilotkit smoke skill") && !hasToolResult) {
      writeStreamChunk(response, body.model, { role: "assistant" });
      writeStreamChunk(response, body.model, {
        tool_calls: [{
          index: 0,
          id: "call_skill",
          type: "function",
          function: {
            name: "skill",
            arguments: JSON.stringify({ name: "copilotkit-smoke-skill" })
          }
        }]
      });
      writeStreamDone(response, body.model, "tool_calls");
      return;
    }
    if (String(latestUserText).includes("触发运行超时")) {
      await sleep(2000);
      writeStreamChunk(response, body.model, { role: "assistant" });
      writeStreamChunk(response, body.model, { content: "too late" });
      writeStreamDone(response, body.model, "stop");
      return;
    }
    if (!hasToolResult) {
      writeStreamChunk(response, body.model, { role: "assistant" });
      writeStreamChunk(response, body.model, {
        tool_calls: [{
          index: 0,
          id: "call_inspect_schema",
          type: "function",
          function: {
            name: "inspect_schema",
            arguments: JSON.stringify({ datasource_id: "api-duckdb-demo" })
          }
        }]
      });
      writeStreamDone(response, body.model, "tool_calls");
      return;
    }
    writeStreamChunk(response, body.model, { role: "assistant" });
    writeStreamChunk(response, body.model, {
      content: "已检查 api-duckdb-demo 的 schema，可以继续进行只读分析。"
    });
    writeStreamDone(response, body.model, "stop");
    return;
  }

  await sleep(250);
  response.writeHead(200, { "Connection": "close", "Content-Type": "application/json" });
  response.end(JSON.stringify({
    id: `chatcmpl-copilotkit-smoke-${llmRequestCount}`,
    object: "chat.completion",
    created: Math.floor(Date.now() / 1000),
    model: body.model,
    choices: [{
      index: 0,
      message: { role: "assistant", content: "OK" },
      finish_reason: "stop"
    }],
    usage: { prompt_tokens: 8, completion_tokens: 1, total_tokens: 9 }
  }));
});

await new Promise((resolve) => modelProviderServer.listen(0, "127.0.0.1", resolve));
const modelProviderAddress = modelProviderServer.address();
assert(modelProviderAddress && typeof modelProviderAddress === "object");
process.env.LLM_BASE_URL = `http://127.0.0.1:${modelProviderAddress.port}`;

const { createServer } = await import("../apps/api/dist/server.js");
const { createMetadataStore } = await import("../packages/metadata/dist/index.js");

const server = await createServer();
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const address = server.address();
assert(address && typeof address === "object");
const baseUrl = `http://127.0.0.1:${address.port}`;

const client = createAuthenticatedTestClient({ baseUrl });
const identity = await client.registerAndLogin({ displayName: "CopilotKit Run Smoke" });
const { userId } = identity;

try {
  const demoDbPath = join(root, "api-duckdb-demo.sqlite");
  const demoDb = new DatabaseSync(demoDbPath);
  demoDb.exec(`
    CREATE TABLE orders (id INTEGER, amount REAL);
    INSERT INTO orders VALUES (1, 10.5), (2, 20);
  `);
  demoDb.close();
  const demoDatasource = await client.fetch("/api/v1/datasources", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      id: "api-duckdb-demo",
      name: "CopilotKit Smoke Demo",
      type: "sqlite",
      settings: { filePath: demoDbPath }
    })
  });
  assert.equal(demoDatasource.status, 201, await demoDatasource.text());

  const skillForm = new FormData();
  skillForm.set("file", new Blob([
    "---\n",
    "name: copilotkit-smoke-skill\n",
    "description: Use for CopilotKit smoke skill runs.\n",
    "version: 1.0.0\n",
    "tags: [copilotkit, smoke]\n",
    "allowed-tools: [inspect_schema]\n",
    "---\n",
    "Use this skill to prove Mastra skill tool loading works in the AG-UI runtime.\n"
  ], { type: "text/markdown" }), "SKILL.md");
  const skillUploadResponse = await client.fetch("/api/v1/skills", { method: "POST", body: skillForm });
  assert.equal(skillUploadResponse.status, 201);
  const skillUpload = await skillUploadResponse.json();
  assert.equal(skillUpload.data.validationStatus, "valid");

  const timeoutProfileResponse = await client.fetch("/api/v1/model-profiles", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      id: "copilotkit-timeout-profile",
      name: "CopilotKit Timeout Profile",
      provider: "openai-compatible",
      modelName: "copilotkit-smoke-model",
      baseUrl: `http://127.0.0.1:${modelProviderAddress.port}`,
      credentials: { apiKey: "copilotkit-smoke-key" },
      timeoutMs: 1000
    })
  });
  assert.equal(timeoutProfileResponse.status, 201);

  const badModelRunId = `copilotkit-bad-model-run-${Date.now()}`;
  const badModelThreadId = `copilotkit-bad-model-thread-${Date.now()}`;
  const badModelEvents = await runCopilotKitAgent(baseUrl, {
    threadId: badModelThreadId,
    runId: badModelRunId,
    parentRunId: undefined,
    state: {},
    messages: [{
      id: "user-message-bad-model",
      role: "user",
      content: "这条错误模型请求应该保留并显示失败。"
    }],
    tools: [],
    context: [],
    forwardedProps: {
      run_config: {
        activeDatasourceId: "api-duckdb-demo",
        activeLlmProfileId: "copilotkit-missing-profile",
        enabledDatasourceIds: ["api-duckdb-demo"],
        enabledKnowledgeIds: [],
        enabledMcpServerIds: [],
        enabledSkillIds: []
      }
    }
  });
  assert(
    badModelEvents.some((event) => event.type === EventType.RUN_ERROR),
    "Missing model profile should emit RUN_ERROR"
  );
  assertRunStatusDelta(badModelEvents, "failed");
  const badModelConversationResponse = await client.fetch(
    `/api/v1/sessions/${badModelThreadId}/conversation?limit=10`
  );
  assert.equal(badModelConversationResponse.status, 200);
  const badModelConversation = await badModelConversationResponse.json();
  assert(
    badModelConversation.data.messages.some((message) =>
      message.runId === badModelRunId
      && message.role === "user"
      && message.contentText === "这条错误模型请求应该保留并显示失败。"
    ),
    `Early model config failure should persist the failed user turn: ${JSON.stringify(badModelConversation.data.messages)}`
  );

  const runId = `copilotkit-run-smoke-${Date.now()}`;
  const threadId = `copilotkit-thread-smoke-${Date.now()}`;
  const events = await runCopilotKitAgent(baseUrl, {
    threadId,
    runId,
    parentRunId: undefined,
    state: {},
    messages: [{
      id: "user-message-1",
      role: "user",
      content: "请先检查 api-duckdb-demo 的 schema，然后简要说明结果。"
    }],
    tools: [],
    context: [],
    forwardedProps: {
      run_config: {
        activeDatasourceId: "api-duckdb-demo",
        activeLlmProfileId: "server-default",
        enabledDatasourceIds: ["api-duckdb-demo"],
        enabledKnowledgeIds: [],
        enabledMcpServerIds: [],
        enabledSkillIds: []
      }
    }
  });

  assert(events.length > 0, "CopilotKit run should emit events");
  if (events[events.length - 1]?.type !== EventType.RUN_FINISHED) {
    console.error("Unexpected normal run events:", JSON.stringify(events, null, 2));
  }
  assert.equal(events[events.length - 1]?.type, EventType.RUN_FINISHED, "RUN_FINISHED should be the last event");
  assertEventOrder(events, EventType.RUN_STARTED, EventType.RUN_FINISHED);
  assertRunStatusCompletedBeforeFinish(events);
  assertNoDuplicatePlanSnapshots(events);
  assertToolResultsBeforeTerminal(events);
  assertMemoryExtractionTimeoutBeforeTerminal(events);
  assert(events.some((event) => event.type === EventType.TOOL_CALL_END), "Expected at least one tool call");
  assert(events.some((event) => event.type === EventType.TOOL_CALL_RESULT), "Expected at least one tool result");
  assert.equal(
    events.some((event) => event.type === EventType.CUSTOM && event.name === "memory.long-term.extracted"),
    false,
    "Timed-out completed-run memory extraction should not emit late extracted event"
  );
  assert.equal(llmRequests[0]?.path, "/chat/completions");
  assert.equal(llmRequests[0]?.authorization, "Bearer copilotkit-smoke-key");
  assert(llmRequests.length >= 2, "The fake model should be called once for tool call and once for final answer");

  const branchParentSessionId = `copilotkit-branch-parent-${Date.now()}`;
  const branchParentRunId = `copilotkit-branch-parent-run-${Date.now()}`;
  const branchSeedStore = createMetadataStore({ database_path: metadataPath });
  try {
    branchSeedStore.sessions.create({
      user_id: userId,
      id: branchParentSessionId,
      title: "CopilotKit branch smoke"
    });
    branchSeedStore.runs.create({
      user_id: userId,
      id: branchParentRunId,
      session_id: branchParentSessionId,
      user_input: "请分析订单。",
      status: "completed"
    });
    branchSeedStore.conversationMessages.append({
      user_id: userId,
      session_id: branchParentSessionId,
      run_id: branchParentRunId,
      id: `${branchParentRunId}:user`,
      role: "user",
      source: "client",
      message_id: `${branchParentRunId}:frontend-user`,
      content_text: "请分析订单。",
      content: { text: "请分析订单。" }
    });
    branchSeedStore.conversationMessages.append({
      user_id: userId,
      session_id: branchParentSessionId,
      run_id: branchParentRunId,
      id: `${branchParentRunId}:assistant`,
      role: "assistant",
      source: "agent",
      message_id: `${branchParentRunId}:assistant`,
      content_text: "订单分析已完成。",
      content: { text: "订单分析已完成。" }
    });
    branchSeedStore.sessions.touchLastMessage({
      user_id: userId,
      session_id: branchParentSessionId
    });
  } finally {
    branchSeedStore.close();
  }
  const branchResponse = await client.fetch(`/api/v1/sessions/${branchParentSessionId}/branches`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ runId: branchParentRunId })
  });
  const branchPayload = await branchResponse.json();
  assert.equal(branchResponse.status, 201, JSON.stringify(branchPayload));
  const branchThreadId = branchPayload.data.session.id;
  const branchRunId = `copilotkit-branch-run-${Date.now()}`;
  const branchEvents = await runCopilotKitAgent(baseUrl, {
    threadId: branchThreadId,
    runId: branchRunId,
    parentRunId: branchParentRunId,
    state: {},
    messages: [{
      id: "user-message-branch",
      role: "user",
      content: "请把订单分析改成表格。"
    }],
    tools: [],
    context: [],
    forwardedProps: {
      run_config: {
        activeDatasourceId: "api-duckdb-demo",
        activeLlmProfileId: "server-default",
        enabledDatasourceIds: ["api-duckdb-demo"],
        enabledKnowledgeIds: [],
        enabledMcpServerIds: [],
        enabledSkillIds: []
      }
    }
  });
  assert.equal(
    branchEvents.some((event) => event.type === EventType.RUN_ERROR && String(event.message).includes("Thread")),
    false,
    `Branch session run should create its working-memory thread before Mastra run: ${JSON.stringify(branchEvents)}`
  );
  assert.equal(branchEvents[branchEvents.length - 1]?.type, EventType.RUN_FINISHED);

  const skillRunId = `copilotkit-skill-run-smoke-${Date.now()}`;
  const skillThreadId = `copilotkit-skill-thread-smoke-${Date.now()}`;
  const skillEvents = await runCopilotKitAgent(baseUrl, {
    threadId: skillThreadId,
    runId: skillRunId,
    parentRunId: undefined,
    state: {},
    messages: [{
      id: "user-message-skill",
      role: "user",
      content: "请使用 copilotkit smoke skill，然后说明它已经加载。"
    }],
    tools: [],
    context: [],
    forwardedProps: {
      run_config: {
        activeDatasourceId: "api-duckdb-demo",
        activeLlmProfileId: "server-default",
        enabledDatasourceIds: ["api-duckdb-demo"],
        enabledKnowledgeIds: [],
        enabledMcpServerIds: [],
        skill_mode: "auto",
        skill_tags: ["copilotkit"]
      }
    }
  });
  assert.equal(skillEvents[skillEvents.length - 1]?.type, EventType.RUN_FINISHED);
  assert(
    skillEvents.some((event) => event.type === EventType.CUSTOM && event.name === "skill.selection"),
    "Skill run should emit skill.selection"
  );
  assert(
    skillEvents.some((event) => event.type === EventType.TOOL_CALL_END && event.toolCallId === "call_skill"),
    "Skill run should call the Mastra skill tool"
  );
  assert(
    skillEvents.some((event) => event.type === EventType.TOOL_CALL_RESULT && event.toolCallId === "call_skill"),
    "Skill run should emit a skill tool result"
  );

  const timeoutRunId = `copilotkit-timeout-run-${Date.now()}`;
  const timeoutThreadId = `copilotkit-timeout-thread-${Date.now()}`;
  const timeoutEvents = await runCopilotKitAgent(baseUrl, {
    threadId: timeoutThreadId,
    runId: timeoutRunId,
    parentRunId: undefined,
    state: {},
    messages: [{
      id: "user-message-timeout",
      role: "user",
      content: "触发运行超时。"
    }],
    tools: [],
    context: [],
    forwardedProps: {
      run_config: {
        activeDatasourceId: "api-duckdb-demo",
        activeLlmProfileId: "copilotkit-timeout-profile",
        enabledDatasourceIds: ["api-duckdb-demo"],
        enabledKnowledgeIds: [],
        enabledMcpServerIds: [],
        enabledSkillIds: []
      }
    }
  });
  assert.equal(timeoutEvents.some((event) => event.type === EventType.RUN_FINISHED), false);
  assert.equal(timeoutEvents[timeoutEvents.length - 1]?.type, EventType.RUN_ERROR);
  assert.equal(timeoutEvents[timeoutEvents.length - 1]?.message, "RUN_TIMEOUT:1000");
  assertRunStatusDelta(timeoutEvents, "failed");

  const store = createMetadataStore({ database_path: metadataPath });
  try {
    const persisted = store.runEvents.listByRun({ user_id: userId, run_id: runId });
    assert(persisted.length > 0, "DataFoundryAgUiAgent.run should persist AG-UI events");
    const persistedEvents = persisted.map((item) => JSON.parse(item.payload_json));
    assert.equal(persistedEvents[persistedEvents.length - 1]?.type, EventType.RUN_FINISHED);
    assertRunStatusCompletedBeforeFinish(persistedEvents);
  } finally {
    store.close();
  }

  const replayEvents = await connectCopilotKitAgent(baseUrl, threadId);
  assert(replayEvents.length > 0, "CopilotKit connect should replay persisted runtime events");
  assert.equal(
    replayEvents.filter((event) => event.type === EventType.RUN_FINISHED).length,
    1,
    "Replay should include one terminal event"
  );
  assert.equal(replayEvents[replayEvents.length - 1]?.type, EventType.RUN_FINISHED);

  const suspendedRunId = `copilotkit-suspended-smoke-${Date.now()}`;
  const suspendedThreadId = `copilotkit-suspended-thread-${Date.now()}`;
  const suspendedEvents = await runCopilotKitAgent(baseUrl, {
    threadId: suspendedThreadId,
    runId: suspendedRunId,
    parentRunId: undefined,
    state: {},
    messages: [{
      id: "user-message-suspended",
      role: "user",
      content: "需要询问用户之后再继续。"
    }],
    tools: [],
    context: [],
    forwardedProps: {
      run_config: {
        activeDatasourceId: "api-duckdb-demo",
        activeLlmProfileId: "server-default",
        enabledDatasourceIds: ["api-duckdb-demo"],
        enabledKnowledgeIds: [],
        enabledMcpServerIds: [],
        enabledSkillIds: []
      }
    }
  });
  assert(
    suspendedEvents.some((event) => event.type === EventType.CUSTOM && event.name === "interaction.requested"),
    "Suspended run should emit interaction.requested"
  );
  assertRunStatusDelta(suspendedEvents, "suspended");
  assert.equal(
    suspendedEvents.some((event) => event.type === EventType.CUSTOM && event.name === "memory.long-term.extracted"),
    false,
    "Suspended run should not emit completed-run memory extraction"
  );

  const suspendedStore = createMetadataStore({ database_path: metadataPath });
  try {
    const suspendedRun = suspendedStore.runs.get({ user_id: userId, run_id: suspendedRunId });
    assert.equal(suspendedRun.status, "suspended");
    const suspendedPersistedEvents = suspendedStore.runEvents
      .listByRun({ user_id: userId, run_id: suspendedRunId })
      .map((item) => JSON.parse(item.payload_json));
    assert.equal(
      suspendedPersistedEvents.some((event) => event.type === EventType.RUN_FINISHED),
      false,
      `Suspended run should not persist RUN_FINISHED; persisted=${suspendedPersistedEvents
        .map((event) => event.type)
        .join(",")}`
    );
    assertRunStatusDelta(suspendedPersistedEvents, "suspended");
    const suspendedMessages = suspendedStore.conversationMessages.listRecent({
      user_id: userId,
      session_id: suspendedThreadId,
      limit: 20
    });
    assert.equal(
      suspendedMessages.some((message) => message.run_id === suspendedRunId && message.role === "assistant"),
      false,
      "Suspended run should not flush partial assistant memory"
    );
  } finally {
    suspendedStore.close();
  }

  console.log(
    "CopilotKit run smoke OK: endpoint run, branch thread creation, bad model failure persistence, terminal order, tool results, persistence, replay, suspended state, run timeout"
  );
} finally {
  await closeHttpServer(server);
  await closeHttpServer(modelProviderServer);
}

process.exit(0);

function writeStreamChunk(response, model, delta) {
  response.write(`data: ${JSON.stringify({
    id: `chatcmpl-copilotkit-smoke-${llmRequestCount}`,
    object: "chat.completion.chunk",
    created: Math.floor(Date.now() / 1000),
    model,
    choices: [{ index: 0, delta, finish_reason: null }]
  })}\n\n`);
}

function writeStreamDone(response, model, finishReason) {
  response.write(`data: ${JSON.stringify({
    id: `chatcmpl-copilotkit-smoke-${llmRequestCount}`,
    object: "chat.completion.chunk",
    created: Math.floor(Date.now() / 1000),
    model,
    choices: [{ index: 0, delta: {}, finish_reason: finishReason }]
  })}\n\n`);
  response.end("data: [DONE]\n\n");
}

async function runCopilotKitAgent(_baseUrl, input) {
  return readAgUiEventStream(await client.fetch("/api/copilotkit", {
    method: "POST",
    headers: {
      "Accept": "text/event-stream",
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      method: "agent/run",
      params: { agentId: "dataFoundry" },
      body: input
    })
  }));
}

async function connectCopilotKitAgent(_baseUrl, threadId) {
  return readAgUiEventStream(await client.fetch("/api/copilotkit", {
    method: "POST",
    headers: {
      "Accept": "text/event-stream",
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      method: "agent/connect",
      params: { agentId: "dataFoundry" },
      body: {
        threadId,
        runId: `connect-${threadId}`,
        state: {},
        messages: [],
        tools: [],
        context: [],
        forwardedProps: {}
      }
    })
  }));
}

async function readAgUiEventStream(response) {
  if (response.status !== 200) {
    throw new Error(`Unexpected CopilotKit status: ${response.status} ${await response.text()}`);
  }
  assert(response.headers.get("content-type")?.includes("text/event-stream"), "Expected AG-UI event stream");
  const text = await response.text();
  return text
    .split("\n\n")
    .map((chunk) => chunk.trim())
    .filter((chunk) => chunk.startsWith("data: "))
    .map((chunk) => JSON.parse(chunk.slice("data: ".length)));
}

function assertEventOrder(events, beforeType, afterType) {
  const beforeIndex = events.findIndex((event) => event.type === beforeType);
  const afterIndex = events.findIndex((event) => event.type === afterType);
  assert(beforeIndex >= 0, `${beforeType} should be present`);
  assert(afterIndex >= 0, `${afterType} should be present`);
  assert(beforeIndex < afterIndex, `${beforeType} should be before ${afterType}`);
}

function assertRunStatusCompletedBeforeFinish(events) {
  const finishIndex = events.findIndex((event) => event.type === EventType.RUN_FINISHED);
  const completedIndex = events.findIndex((event) =>
    event.type === EventType.STATE_DELTA
    && Array.isArray(event.delta)
    && event.delta.some((op) => op.path === "/runStatus" && op.value === "completed")
  );
  assert(completedIndex >= 0, "runStatus=completed delta should be emitted");
  assert(finishIndex >= 0, "RUN_FINISHED should be emitted");
  assert(completedIndex < finishIndex, "runStatus=completed delta should be before RUN_FINISHED");
}

function assertRunStatusDelta(events, status) {
  assert(
    events.some((event) =>
      event.type === EventType.STATE_DELTA
      && Array.isArray(event.delta)
      && event.delta.some((op) => op.path === "/runStatus" && op.value === status)
    ),
    `runStatus=${status} delta should be emitted`
  );
}

function assertNoDuplicatePlanSnapshots(events) {
  const planSnapshots = events.filter((event) => event.type === EventType.ACTIVITY_SNAPSHOT
    && event.activityType === "PLAN");
  const keys = planSnapshots.map((event) => JSON.stringify(event.content));
  assert.equal(new Set(keys).size, keys.length, "PLAN snapshots should not be duplicated");
}

function assertToolResultsBeforeTerminal(events) {
  const terminalIndex = events.findIndex((event) =>
    event.type === EventType.RUN_FINISHED || event.type === EventType.RUN_ERROR
  );
  assert(terminalIndex >= 0, "Terminal event should be emitted");
  const resultByToolCallId = new Set(
    events.slice(0, terminalIndex)
      .filter((event) => event.type === EventType.TOOL_CALL_RESULT && event.toolCallId)
      .map((event) => event.toolCallId)
  );
  const missing = events.slice(0, terminalIndex)
    .filter((event) => event.type === EventType.TOOL_CALL_END && event.toolCallId)
    .map((event) => event.toolCallId)
    .filter((toolCallId) => !resultByToolCallId.has(toolCallId));
  assert.deepEqual(missing, [], "Every TOOL_CALL_END should have TOOL_CALL_RESULT before terminal event");
}

function assertMemoryExtractionTimeoutBeforeTerminal(events) {
  const timeoutIndex = events.findIndex((event) =>
    event.type === EventType.CUSTOM && event.name === "memory.completed-flush.timeout"
  );
  const finishIndex = events.findIndex((event) => event.type === EventType.RUN_FINISHED);
  assert(timeoutIndex >= 0, "Slow completed-run memory extraction should emit timeout event");
  assert(finishIndex >= 0, "RUN_FINISHED should be emitted");
  assert(timeoutIndex < finishIndex, "Memory extraction timeout event should be before RUN_FINISHED");
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function closeHttpServer(httpServer) {
  await new Promise((resolve, reject) => {
    httpServer.close((error) => error ? reject(error) : resolve());
    setImmediate(() => httpServer.closeAllConnections?.());
  });
  httpServer.unref?.();
}
