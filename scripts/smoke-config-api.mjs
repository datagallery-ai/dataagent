import assert from "node:assert/strict";
import { existsSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { createServer as createHttpServer } from "node:http";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StreamableHTTPServerTransport } from "@modelcontextprotocol/sdk/server/streamableHttp.js";
import { z } from "zod";

import { createServer as createApiServer } from "../apps/api/dist/server.js";
import { resolveRunConfig } from "../apps/api/dist/run-config-resolver.js";
import { resolveEffectiveRunConfig } from "../apps/api/dist/run-input.js";
import { createTaskStateRuntime } from "../packages/agent-runtime/dist/index.js";
import { LocalDataGateway } from "../packages/data-gateway/dist/index.js";
import { RunEventWriter, createMetadataStore } from "../packages/metadata/dist/index.js";
import { createAuthenticatedTestClient } from "./lib/authenticated-test-client.mjs";

const root = mkdtempSync(join(tmpdir(), "open-data-foundry-config-smoke-"));
const datasourcePath = join(root, "source.sqlite");
const source = new DatabaseSync(datasourcePath);
source.exec(`
  CREATE TABLE metrics (name TEXT, value INTEGER, secret TEXT);
  INSERT INTO metrics VALUES ('revenue', 42, 'top-secret'), ('cost', 12, 'hidden-cost');
  CREATE TABLE private_metrics (name TEXT, value INTEGER);
  INSERT INTO private_metrics VALUES ('internal', 999);
`);
source.close();

process.env.DATAFOUNDRY_AUTH_MODE = "password";
process.env.AUTH_SESSION_SECRET = "config-smoke-session-secret-32bytes!!";
process.env.AUTH_PUBLIC_BASE_URL = "http://127.0.0.1:3000";
process.env.AUTH_EMAIL_DELIVERY = "test";
process.env.AUTH_REGISTRATION_MODE = "open";
process.env.EMBEDDING_API_KEY = "";
process.env.MASTRA_STORAGE_PATH = join(root, "mastra.sqlite");
process.env.STORAGE_ROOT_DIR = root;

const metadataStore = createMetadataStore({
  database_path: join(root, "metadata.sqlite"),
  secret_master_key: "config-smoke-master-key"
});
const taskStateRuntime = await createTaskStateRuntime(join(root, "task-state.sqlite"));
let modelProbeRequest;
const modelProviderServer = createHttpServer(async (request, response) => {
  const chunks = [];
  for await (const chunk of request) {
    chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
  }
  const body = chunks.length > 0 ? JSON.parse(Buffer.concat(chunks).toString("utf8")) : {};
  modelProbeRequest = {
    authorization: request.headers.authorization,
    body,
    method: request.method,
    path: request.url
  };
  response.writeHead(200, { "Connection": "close", "Content-Type": "application/json" });
  response.end(JSON.stringify({
    id: "chatcmpl-config-smoke",
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
process.env.LLM_PROVIDER = "openai-compatible";
process.env.LLM_MODEL = "smoke-model";
process.env.LLM_BASE_URL = `http://127.0.0.1:${modelProviderAddress.port}`;
process.env.LLM_API_KEY = "smoke-model-key";

const mcpServer = createHttpServer(async (request, response) => {
  const server = new McpServer({ name: "config-smoke-mcp", version: "1.0.0" });
  server.registerTool("echo", {
    description: "Echo one value.",
    inputSchema: { value: z.string() }
  }, async ({ value }) => ({ content: [{ type: "text", text: value }] }));
  server.registerTool("hidden", {
    description: "Hidden tool.",
    inputSchema: { value: z.string() }
  }, async ({ value }) => ({ content: [{ type: "text", text: value }] }));
  const transport = new StreamableHTTPServerTransport({ sessionIdGenerator: undefined });
  await server.connect(transport);
  await transport.handleRequest(request, response);
  response.on("close", () => {
    void transport.close();
    void server.close();
  });
});
await new Promise((resolve) => mcpServer.listen(0, "127.0.0.1", resolve));
const mcpAddress = mcpServer.address();
assert(mcpAddress && typeof mcpAddress === "object");

const datalinkApiRequests = [];
const datalinkApiServer = createHttpServer(async (request, response) => {
  const chunks = [];
  for await (const chunk of request) {
    chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
  }
  const text = Buffer.concat(chunks).toString("utf8");
  const body = text ? JSON.parse(text) : undefined;
  datalinkApiRequests.push({ method: request.method, path: request.url, body });

  const responseBody = request.method === "GET" && request.url === "/show"
    ? {
        nodes: [{ id: "table:metrics", name: "metrics", properties: {}, type: "table" }],
        edges: [],
      }
    : request.method === "POST" && request.url === "/explore"
      ? "metrics table"
      : request.method === "POST" && request.url === "/add-table"
        ? { added_tables: ["metrics"] }
        : request.method === "POST" && request.url === "/remove-table"
          ? { removed_table: "metrics" }
          : request.method === "POST" && request.url === "/rebuild"
            ? { rebuilt: true }
            : undefined;
  const isTextResponse = typeof responseBody === "string";
  response.writeHead(responseBody ? 200 : 404, {
    "Content-Type": isTextResponse ? "text/plain; charset=utf-8" : "application/json",
  });
  response.end(isTextResponse ? responseBody : JSON.stringify(responseBody ?? { detail: "not found" }));
});
await new Promise((resolve) => datalinkApiServer.listen(0, "127.0.0.1", resolve));
const datalinkApiAddress = datalinkApiServer.address();
assert(datalinkApiAddress && typeof datalinkApiAddress === "object");

const server = await createApiServer({ metadataStore, taskStateRuntime });
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const address = server.address();
assert(address && typeof address === "object");
const baseUrl = `http://127.0.0.1:${address.port}`;

const client = createAuthenticatedTestClient({ baseUrl });
const identity = await client.registerAndLogin({ displayName: "Config Smoke User" });
const { userId, workspaceId } = identity;

const tenantClient = createAuthenticatedTestClient({ baseUrl });
const tenantIdentity = await tenantClient.registerAndLogin({ displayName: "Tenant User" });

const requestJson = async (path, init = {}, authClient = client) => {
  const response = await authClient.fetch(path, init);
  const body = await response.json();
  return { body, response };
};

const requestRaw = async (path, init = {}, authClient = client) => authClient.fetch(path, init);

const closeHttpServer = async (httpServer) => {
  await new Promise((resolve, reject) => {
    httpServer.close((error) => error ? reject(error) : resolve());
    setImmediate(() => httpServer.closeAllConnections?.());
  });
  httpServer.unref?.();
};

const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

try {
  const capabilities = await requestJson("/api/v1/capabilities");
  assert.equal(capabilities.body.data["chat.fileUpload"], true);
  assert.equal(capabilities.body.data["chat.imageInput"], true);
  assert.equal(capabilities.body.data["datasource.fieldMasking"], true);
  assert.equal(capabilities.body.data["datasource.extendedTypes"], true);
  assert.equal(capabilities.body.data["datasource.introspectionPolicy"], true);
  assert.equal(capabilities.body.data["datasource.samplePolicy"], true);
  assert.equal(capabilities.body.data["kb.chunking"], true);
  assert.equal(capabilities.body.data["kb.citationPolicy"], true);
  assert.equal(capabilities.body.data["kb.scope"], true);
  assert.equal(capabilities.body.data["llm.advancedSampling"], true);
  assert.equal(capabilities.body.data["mcp.stdio"], true);
  assert.equal(capabilities.body.data["mcp.toolPolicy"], true);
  assert.equal(capabilities.body.data["skill.resourceBinding"], true);

  const datasourceTypes = await requestJson("/api/v1/datasource-types");
  assert.equal(datasourceTypes.response.status, 200);
  for (const typeName of [
    "sqlite",
    "postgresql",
    "clickhouse",
    "duckdb",
    "snowflake",
    "bigquery",
    "sqlserver",
    "oracle",
    "mongodb",
    "gaussdb",
    "access",
    "redis",
    "starrocks",
    "trino",
    "presto",
    "spark",
    "databricks",
    "redshift",
    "elasticsearch",
    "opensearch",
    "doris",
    "mariadb",
    "tidb",
    "oceanbase",
    "greenplum"
  ]) {
    assert(
      datasourceTypes.body.data.some((type) => type.name === typeName && type.enabled === true),
      `datasource-types should expose enabled ${typeName}`
    );
  }

  const anonymousClient = createAuthenticatedTestClient({ baseUrl });
  const unauthenticated = await requestJson("/api/v1/workspace-config", {}, anonymousClient);
  assert.equal(unauthenticated.response.status, 401);
  assert.equal(unauthenticated.body.error.code, "UNAUTHORIZED");

  const defaultMe = await requestJson("/api/v1/me");
  assert.equal(defaultMe.response.status, 200);
  assert.equal(defaultMe.body.data.user.id, userId);
  assert.equal(defaultMe.body.data.workspace.id, workspaceId);

  const analystClient = createAuthenticatedTestClient({ baseUrl });
  const analystIdentity = await analystClient.registerAndLogin({
    email: `analyst-${Date.now()}@example.test`,
    displayName: "Analyst User"
  });
  const analystMe = await requestJson("/api/v1/me", {}, analystClient);
  assert.equal(analystMe.response.status, 200);
  assert.equal(analystMe.body.data.user.id, analystIdentity.userId);
  assert.equal(analystMe.body.data.workspace.id, analystIdentity.workspaceId);
  assert.notEqual(analystIdentity.userId, userId);

  const tenantDatasource = await requestJson("/api/v1/datasources", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      id: "local-sqlite",
      name: "Tenant SQLite",
      type: "sqlite",
      settings: { filePath: datasourcePath }
    })
  }, tenantClient);
  assert.equal(tenantDatasource.response.status, 201, JSON.stringify(tenantDatasource.body));
  const devDatasourceListBeforeTenant = await requestJson("/api/v1/datasources");
  assert.equal(devDatasourceListBeforeTenant.body.data.some((item) => item.id === "local-sqlite"), false);
  const tenantKnowledge = await requestJson("/api/v1/knowledge-bases", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      id: "tenant-docs",
      name: "Tenant Docs",
      defaultEnabled: true,
      retrievalTopK: 2
    })
  }, tenantClient);
  assert.equal(tenantKnowledge.response.status, 201);
  const devTenantKnowledge = await requestJson("/api/v1/knowledge-bases/tenant-docs");
  assert.equal(devTenantKnowledge.response.status, 404);
  const tenantVisibleKnowledge = await requestJson("/api/v1/knowledge-bases/tenant-docs", {}, tenantClient);
  assert.equal(tenantVisibleKnowledge.response.status, 200);

  const uploadForm = new FormData();
  uploadForm.append("sessionId", "session-smoke-upload");
  uploadForm.append("file", new Blob(["name,value\nrevenue,42\n"], { type: "text/csv" }), "metrics.csv");
  const chatUpload = await requestJson("/api/v1/chat/uploads", {
    method: "POST",
    body: uploadForm
  });
  assert.equal(chatUpload.response.status, 200);
  assert.equal(chatUpload.body.path, "uploads/metrics.csv");
  assert.equal(chatUpload.body.mimeType, "text/csv");
  assert.equal(chatUpload.body.size > 0, true);

  const datasourceUploadForm = new FormData();
  datasourceUploadForm.append(
    "file",
    new Blob(["name,value\nrevenue,42\n"], { type: "text/csv" }),
    "sales-metrics.csv"
  );
  const datasourceUpload = await requestJson("/api/v1/datasources/uploads", {
    method: "POST",
    body: datasourceUploadForm
  });
  assert.equal(datasourceUpload.response.status, 200);
  assert.equal(typeof datasourceUpload.body.path, "string");
  assert.match(datasourceUpload.body.path, /datasources[/\\]sales-metrics\.csv$/);
  assert.equal(datasourceUpload.body.originalName, "sales-metrics.csv");
  assert.equal(datasourceUpload.body.mimeType, "text/csv");
  assert.equal(datasourceUpload.body.size > 0, true);
  assert.equal(existsSync(datasourceUpload.body.path), true);

  const rejectedDatasourceUploadForm = new FormData();
  rejectedDatasourceUploadForm.append(
    "file",
    new Blob(["not-a-datasource"], { type: "application/pdf" }),
    "notes.pdf"
  );
  const rejectedDatasourceUpload = await requestJson("/api/v1/datasources/uploads", {
    method: "POST",
    body: rejectedDatasourceUploadForm
  });
  assert.equal(rejectedDatasourceUpload.response.status, 415);

  const conversationSessionId = "conversation-api-session";
  const conversationRunId = "conversation-api-run";
  metadataStore.sessions.create({ user_id: userId, id: conversationSessionId, title: "conversation API" });
  metadataStore.runs.create({
    user_id: userId,
    id: conversationRunId,
    session_id: conversationSessionId,
    request_fingerprint: "conversation-api-fingerprint",
    user_input: "inspect orders",
    status: "completed"
  });
  metadataStore.conversationMessages.append({
    user_id: userId,
    session_id: conversationSessionId,
    run_id: conversationRunId,
    id: `${conversationRunId}:user`,
    role: "user",
    source: "client",
    message_id: "frontend-user-message",
    content_text: "inspect orders",
    content: { text: "inspect orders" }
  });
  metadataStore.conversationMessages.append({
    user_id: userId,
    session_id: conversationSessionId,
    run_id: conversationRunId,
    id: `${conversationRunId}:assistant`,
    role: "assistant",
    source: "agent",
    message_id: "assistant-message",
    content_text: "orders has 2 columns",
    content: { text: "orders has 2 columns" }
  });
  metadataStore.conversationSummaries.create({
    user_id: userId,
    session_id: conversationSessionId,
    id: `summary:${conversationSessionId}:1-1`,
    source_run_id: conversationRunId,
    from_position: 1,
    to_position: 1,
    summary_text: "User asked to inspect orders."
  });
  const conversationWriter = new RunEventWriter(metadataStore.runEvents);
  conversationWriter.write({
    user_id: userId,
    run_id: conversationRunId,
    session_id: conversationSessionId,
    event: { type: "TOOL_CALL_START", toolCallId: "call_schema", toolCallName: "inspect_schema" }
  });
  conversationWriter.write({
    user_id: userId,
    run_id: conversationRunId,
    session_id: conversationSessionId,
    event: { type: "TOOL_CALL_END", toolCallId: "call_schema", toolCallName: "inspect_schema" }
  });
  conversationWriter.write({
    user_id: userId,
    run_id: conversationRunId,
    session_id: conversationSessionId,
    event: {
      type: "TOOL_CALL_RESULT",
      toolCallId: "call_schema",
      toolCallName: "inspect_schema",
      messageId: "tool-result-message",
      content: JSON.stringify({ columns: 2 })
    }
  });
  const conversation = await requestJson(`/api/v1/sessions/${conversationSessionId}/conversation?limit=10`);
  assert.equal(conversation.response.status, 200);
  assert.equal(conversation.body.data.sessionId, conversationSessionId);
  assert.equal(conversation.body.data.messages.length, 2);
  assert.equal(conversation.body.data.messages[0].messageId, "frontend-user-message");
  assert.equal(conversation.body.data.summary.summaryText, "User asked to inspect orders.");
  assert.equal(conversation.body.data.runEventRefs[0].eventCount, 3);
  assert.deepEqual(
    pick(conversation.body.data.checkpoints[0], [
      "runId",
      "status",
      "messageStartPosition",
      "messageEndPosition",
      "firstEventSeq",
      "lastEventSeq"
    ]),
    {
      runId: conversationRunId,
      status: "completed",
      messageStartPosition: 1,
      messageEndPosition: 2,
      firstEventSeq: 1,
      lastEventSeq: 3
    }
  );
  assert.deepEqual(
    pick(conversation.body.data.toolCalls[0], [
      "runId",
      "toolCallId",
      "status",
      "toolName",
      "callEventSeq",
      "endEventSeq",
      "resultEventSeq",
      "resultMessageId",
      "resultPreview"
    ]),
    {
      runId: conversationRunId,
      toolCallId: "call_schema",
      status: "completed",
      toolName: "inspect_schema",
      callEventSeq: 1,
      endEventSeq: 2,
      resultEventSeq: 3,
      resultMessageId: "tool-result-message",
      resultPreview: JSON.stringify({ columns: 2 })
    }
  );

  // R-018: pending HITL without TOOL_CALL_START still synthesizes toolCalls + checkpoint.
  const hitlOnlyRunId = "conversation-hitl-only-run";
  metadataStore.runs.create({
    user_id: userId,
    id: hitlOnlyRunId,
    session_id: conversationSessionId,
    user_input: "ask the user",
    status: "suspended"
  });
  metadataStore.interactions.request({
    id: "interaction-hitl-only",
    user_id: userId,
    session_id: conversationSessionId,
    run_id: hitlOnlyRunId,
    tool_call_id: "call_ask_missing_start",
    tool_name: "ask_user",
    payload: { question: "Continue?" },
    interrupt_event: {
      type: "mastra_suspend",
      toolCallId: "call_ask_missing_start",
      toolName: "ask_user",
      runId: hitlOnlyRunId,
      args: { question: "Continue?" },
      resumeSchema: {},
      suspendPayload: { question: "Continue?" }
    }
  });
  const hitlConversation = await requestJson(
    `/api/v1/sessions/${conversationSessionId}/conversation?limit=20`
  );
  assert.equal(hitlConversation.response.status, 200);
  const synthesizedHitl = hitlConversation.body.data.toolCalls.find(
    (call) => call.toolCallId === "call_ask_missing_start"
  );
  assert.ok(synthesizedHitl, "Expected synthesized toolCall for pending interaction");
  assert.equal(synthesizedHitl.toolName, "ask_user");
  assert.equal(synthesizedHitl.status, "pending");
  assert.equal(synthesizedHitl.awaitingInteraction, true);
  const hitlCheckpoint = hitlConversation.body.data.checkpoints.find(
    (checkpoint) => checkpoint.runId === hitlOnlyRunId
  );
  assert.ok(hitlCheckpoint, "Expected checkpoint for HITL-only run");
  assert.equal(hitlCheckpoint.status, "suspended");

  const branchBaseRunId = "conversation-branch-base-run";
  const branchOriginalRunId = "conversation-branch-original-run";
  const branchPendingRunId = "conversation-branch-pending-run";
  metadataStore.runs.create({
    user_id: userId,
    id: branchBaseRunId,
    session_id: conversationSessionId,
    user_input: "summarize orders",
    status: "completed"
  });
  metadataStore.conversationMessages.append({
    user_id: userId,
    session_id: conversationSessionId,
    run_id: branchBaseRunId,
    id: `${branchBaseRunId}:user`,
    role: "user",
    source: "client",
    message_id: "branch-base-user-message",
    content_text: "summarize orders",
    content: { text: "summarize orders" }
  });
  metadataStore.conversationMessages.append({
    user_id: userId,
    session_id: conversationSessionId,
    run_id: branchBaseRunId,
    id: `${branchBaseRunId}:assistant`,
    role: "assistant",
    source: "agent",
    message_id: "branch-base-assistant-message",
    content_text: "orders summary",
    content: { text: "orders summary" }
  });
  metadataStore.runs.create({
    user_id: userId,
    id: branchOriginalRunId,
    session_id: conversationSessionId,
    user_input: "make a chart",
    status: "completed"
  });
  metadataStore.conversationMessages.append({
    user_id: userId,
    session_id: conversationSessionId,
    run_id: branchOriginalRunId,
    id: `${branchOriginalRunId}:user`,
    role: "user",
    source: "client",
    message_id: "branch-original-user-message",
    content_text: "make a chart",
    content: { text: "make a chart" }
  });
  metadataStore.conversationMessages.append({
    user_id: userId,
    session_id: conversationSessionId,
    run_id: branchOriginalRunId,
    id: `${branchOriginalRunId}:assistant`,
    role: "assistant",
    source: "agent",
    message_id: "branch-original-assistant-message",
    content_text: "original chart answer",
    content: { text: "original chart answer" }
  });
  metadataStore.runs.create({
    user_id: userId,
    id: branchPendingRunId,
    session_id: conversationSessionId,
    user_input: "still running",
    status: "running"
  });

  const rejectedRunningBranch = await requestJson(`/api/v1/sessions/${conversationSessionId}/branches`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ runId: branchPendingRunId })
  });
  assert.equal(rejectedRunningBranch.response.status, 409);
  assert.equal(rejectedRunningBranch.body.error.code, "RUN_NOT_BRANCHABLE");

  const createdBranch = await requestJson(`/api/v1/sessions/${conversationSessionId}/branches`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ runId: branchOriginalRunId })
  });
  assert.equal(createdBranch.response.status, 201, JSON.stringify(createdBranch.body));
  assert.equal(createdBranch.body.data.parentSessionId, conversationSessionId);
  assert.equal(createdBranch.body.data.forkRunId, branchOriginalRunId);
  assert.equal(createdBranch.body.data.forkMessageEndPosition, 4);
  const branchSessionId = createdBranch.body.data.session.id;

  const branchRunId = "conversation-branch-child-run";
  metadataStore.runs.create({
    user_id: userId,
    id: branchRunId,
    session_id: branchSessionId,
    parent_run_id: branchOriginalRunId,
    user_input: "make a table instead",
    status: "completed"
  });
  metadataStore.conversationMessages.append({
    user_id: userId,
    session_id: branchSessionId,
    run_id: branchRunId,
    id: `${branchRunId}:user`,
    role: "user",
    source: "client",
    message_id: "branch-child-user-message",
    content_text: "make a table instead",
    content: { text: "make a table instead" }
  });
  metadataStore.conversationMessages.append({
    user_id: userId,
    session_id: branchSessionId,
    run_id: branchRunId,
    id: `${branchRunId}:assistant`,
    role: "assistant",
    source: "agent",
    message_id: "branch-child-assistant-message",
    content_text: "branch table answer",
    content: { text: "branch table answer" }
  });

  const branchConversation = await requestJson(`/api/v1/sessions/${branchSessionId}/conversation?limit=20`);
  assert.equal(branchConversation.response.status, 200, JSON.stringify(branchConversation.body));
  assert.deepEqual(
    branchConversation.body.data.messages.map((message) => message.contentText),
    [
      "inspect orders",
      "orders has 2 columns",
      "summarize orders",
      "orders summary",
      "make a table instead",
      "branch table answer"
    ]
  );
  assert.equal(
    branchConversation.body.data.messages.some((message) => message.contentText === "original chart answer"),
    false
  );
  assert.equal(branchConversation.body.data.branch.parentSessionId, conversationSessionId);
  assert.equal(
    branchConversation.body.data.branches.some((branch) =>
      branch.forkRunId === branchOriginalRunId
      && branch.sessionId === branchSessionId
      && branch.parentSessionId === conversationSessionId
    ),
    true
  );

  const createdDatasource = await requestJson("/api/v1/datasources", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      id: "local-sqlite",
      name: "Local SQLite",
      type: "sqlite",
      settings: { filePath: datasourcePath, password: "must-not-leak" },
      queryPolicy: { maxRows: 25, timeoutMs: 5000 }
    })
  });
  assert.equal(createdDatasource.response.status, 201);
  assert.equal(createdDatasource.body.data.hasSecret, true);
  assert.equal(JSON.stringify(createdDatasource.body).includes("must-not-leak"), false);
  assert.equal(createdDatasource.body.data.config.path, datasourcePath);

  const datasource = await requestJson("/api/v1/datasources/local-sqlite");
  assert.equal(JSON.stringify(datasource.body).includes("password"), false);
  const datasourceRevision = datasource.body.data.revision;
  const connection = await requestJson("/api/v1/datasources/local-sqlite/test", { method: "POST" });
  assert.equal(connection.body.success, true);

  const introspection = await requestJson("/api/v1/datasources/local-sqlite/introspect", {
    method: "POST",
    headers: { "Idempotency-Key": "schema-once" }
  });
  const repeatedIntrospection = await requestJson("/api/v1/datasources/local-sqlite/introspect", {
    method: "POST",
    headers: { "Idempotency-Key": "schema-once" }
  });
  assert.equal(introspection.body.data.id, repeatedIntrospection.body.data.id);
  const schema = await requestJson("/api/v1/datasources/local-sqlite/schema");
  assert(datasourceSchemaTables(schema.body.data).some((table) => table.name === "metrics"));
  const refreshPatch = await requestJson("/api/v1/datasources/local-sqlite", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      revision: datasourceRevision,
      introspection: { refreshIntervalSec: 1 }
    })
  });
  assert.equal(refreshPatch.response.status, 200);
  assert.equal(refreshPatch.body.data.config.introspection.refreshIntervalSec, 1);
  const refreshSource = new DatabaseSync(datasourcePath);
  refreshSource.exec("CREATE TABLE refreshed_metrics (name TEXT, value INTEGER);");
  refreshSource.close();
  await delay(1100);
  const refreshedSchema = await requestJson("/api/v1/datasources/local-sqlite/schema");
  const refreshedTableNames = datasourceSchemaTables(refreshedSchema.body.data).map((table) => table.name);
  assert(
    refreshedTableNames.includes("refreshed_metrics"),
    `refreshIntervalSec should refresh expired datasource schema snapshots, got ${refreshedTableNames.join(",")}`
  );

  const conflict = await requestJson("/api/v1/datasources/local-sqlite", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: "Conflict", revision: datasourceRevision + 100 })
  });
  assert.equal(conflict.response.status, 409);
  assert.equal(conflict.body.error.code, "REVISION_CONFLICT");

  const policyPatch = await requestJson("/api/v1/datasources/local-sqlite", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      revision: refreshPatch.body.data.revision,
      introspection: { tableAllowlist: ["metrics"] },
      maskFields: ["secret"],
      queryPolicy: { maxRows: 25, timeoutMs: 5000 },
      samplePolicy: { allowSample: true, maxSampleRows: 1 }
    })
  });
  assert.equal(policyPatch.response.status, 200);
  assert.deepEqual(policyPatch.body.data.config.introspection.tableAllowlist, ["metrics"]);
  assert.equal(policyPatch.body.data.config.introspection.refreshIntervalSec, 1);
  assert.deepEqual(policyPatch.body.data.config.maskFields, ["secret"]);
  assert.equal(policyPatch.body.data.config.samplePolicy.maxSampleRows, 1);
  const policyGateway = new LocalDataGateway(metadataStore, {
    defaultLimit: 100,
    maxLimit: 1000,
    timeoutMs: 10000,
    workspaceId
  });
  const policySchema = await policyGateway.inspectSchema({
    user_id: userId,
    workspace_id: workspaceId,
    datasource_id: "local-sqlite"
  });
  assert.deepEqual(policySchema.tables.map((table) => table.name), ["metrics"]);
  const preview = await policyGateway.previewTable({
    user_id: userId,
    workspace_id: workspaceId,
    datasource_id: "local-sqlite",
    table: "metrics",
    limit: 10
  });
  assert.equal(preview.row_count, 1, "samplePolicy.maxSampleRows should cap preview rows");
  assert.equal(preview.rows[0]?.[preview.columns.indexOf("secret")], "[MASKED]");
  const maskedSql = await policyGateway.runSqlReadonly({
    user_id: userId,
    workspace_id: workspaceId,
    datasource_id: "local-sqlite",
    sql: "SELECT name, secret FROM metrics",
    limit: 10
  });
  assert.equal(maskedSql.rows[0]?.[maskedSql.columns.indexOf("secret")], "[MASKED]");
  await assert.rejects(
    () => policyGateway.previewTable({
      user_id: userId,
      workspace_id: workspaceId,
      datasource_id: "local-sqlite",
      table: "private_metrics",
      limit: 1
    }),
    /TABLE_NOT_ALLOWED:private_metrics/u
  );
  await assert.rejects(
    () => policyGateway.runSqlReadonly({
      user_id: userId,
      workspace_id: workspaceId,
      datasource_id: "local-sqlite",
      sql: "SELECT * FROM private_metrics",
      limit: 1
    }),
    /TABLE_NOT_ALLOWED:private_metrics/u
  );
  const sampleDisabledPatch = await requestJson("/api/v1/datasources/local-sqlite", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      revision: policyPatch.body.data.revision,
      samplePolicy: { allowSample: false, maxSampleRows: 1 }
    })
  });
  assert.equal(sampleDisabledPatch.response.status, 200);
  await assert.rejects(
    () => policyGateway.previewTable({
      user_id: userId,
      workspace_id: workspaceId,
      datasource_id: "local-sqlite",
      table: "metrics",
      limit: 1
    }),
    /SAMPLE_BLOCKED:local-sqlite:metrics/u
  );

  const knowledgeBase = await requestJson("/api/v1/knowledge-bases", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      id: "metrics-docs",
      name: "Metrics Docs",
      chunkOverlap: 25,
      chunkSize: 1200,
      citationRequired: true,
      retrievalTopK: 3,
      scope: "workspace"
    })
  });
  assert.equal(knowledgeBase.response.status, 201);
  assert.equal(knowledgeBase.body.data.chunkSize, 1200);
  assert.equal(knowledgeBase.body.data.chunkOverlap, 25);
  assert.equal(knowledgeBase.body.data.citationRequired, true);
  assert.equal(knowledgeBase.body.data.scope, "workspace");
  const fileForm = new FormData();
  fileForm.append("files", new Blob(["Revenue metric can be imported from file assets."], {
    type: "text/markdown"
  }), "file-metrics.md");
  const fileUploadResponse = await requestRaw("/api/v1/files", {
    method: "POST",
    headers: { "X-Session-Id": "config-smoke-session" },
    body: fileForm
  });
  assert.equal(fileUploadResponse.status, 201);
  const fileUpload = await fileUploadResponse.json();
  assert.equal(fileUpload.body?.success ?? fileUpload.success, true);
  const uploadedFile = fileUpload.body?.data?.files?.[0] ?? fileUpload.data.files[0];
  assert.equal(uploadedFile.filename, "file-metrics.md");
  const fileDownload = await requestRaw(`/api/v1/files/${uploadedFile.id}/download`);
  assert.equal(fileDownload.status, 200);
  assert.equal(await fileDownload.text(), "Revenue metric can be imported from file assets.");
  const fileImport = await requestJson("/api/v1/knowledge-bases/metrics-docs/files/import", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ fileIds: [uploadedFile.id] })
  });
  assert.equal(fileImport.response.status, 207);
  assert.equal(fileImport.body.data.results[0].status, "ready");
  const upload = await requestJson("/api/v1/knowledge-bases/metrics-docs/files", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ filename: "metrics.md", content: "Revenue metric is gross sales before refunds." })
  });
  assert.equal(upload.response.status, 201);
  assert.equal(upload.body.data.status, "ready");

  // KB type gate: Office stays 415; invalid PDF payloads are 422 (PDF is now allowed).
  const invalidPdfForm = new FormData();
  invalidPdfForm.append(
    "file",
    new Blob(["%PDF-1.4"], { type: "application/pdf" }),
    "doc.pdf"
  );
  const invalidPdf = await requestJson("/api/v1/knowledge-bases/metrics-docs/files", {
    method: "POST",
    body: invalidPdfForm
  });
  assert.equal(invalidPdf.response.status, 422);
  assert.match(
    String(invalidPdf.body?.error?.message ?? ""),
    /KNOWLEDGE_PDF_(EMPTY|PARSE_FAILED)/
  );

  const rejectedDocxForm = new FormData();
  rejectedDocxForm.append(
    "file",
    new Blob(["PK"], {
      type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    }),
    "report.docx"
  );
  const rejectedDocx = await requestJson("/api/v1/knowledge-bases/metrics-docs/files", {
    method: "POST",
    body: rejectedDocxForm
  });
  assert.equal(rejectedDocx.response.status, 415);
  assert.match(String(rejectedDocx.body?.error?.message ?? ""), /KNOWLEDGE_FILE_TYPE_UNSUPPORTED/);

  // MIME spoofing must not bypass extension handling: .pdf still goes through PDF parse.
  const forgedMimeForm = new FormData();
  forgedMimeForm.append(
    "file",
    new Blob(["spoofed"], { type: "text/plain" }),
    "doc.pdf"
  );
  const forgedMime = await requestJson("/api/v1/knowledge-bases/metrics-docs/files", {
    method: "POST",
    body: forgedMimeForm
  });
  assert.equal(forgedMime.response.status, 422);
  assert.match(
    String(forgedMime.body?.error?.message ?? ""),
    /KNOWLEDGE_PDF_(EMPTY|PARSE_FAILED)/
  );

  const rejectedJsonPdf = await requestJson("/api/v1/knowledge-bases/metrics-docs/files", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ filename: "doc.pdf", content: "not a pdf but must still be rejected" })
  });
  assert.equal(rejectedJsonPdf.response.status, 422);
  assert.match(
    String(rejectedJsonPdf.body?.error?.message ?? ""),
    /KNOWLEDGE_PDF_(EMPTY|PARSE_FAILED)/
  );

  // Light upload → promote → list(scope=workspace) path for workspace assets.
  const workspaceSessionId = "config-smoke-workspace-promote";
  metadataStore.sessions.create({
    user_id: userId,
    id: workspaceSessionId,
    title: "config smoke workspace promote"
  });
  const workspaceUploadForm = new FormData();
  workspaceUploadForm.append(
    "files",
    new Blob(["workspace-asset-body"], { type: "text/plain" }),
    "workspace-asset.txt"
  );
  const workspaceUploadResponse = await requestRaw("/api/v1/files", {
    method: "POST",
    headers: { "X-Session-Id": workspaceSessionId },
    body: workspaceUploadForm
  });
  assert.equal(workspaceUploadResponse.status, 201);
  const workspaceUploadBody = await workspaceUploadResponse.json();
  const workspaceUploaded =
    workspaceUploadBody.body?.data?.files?.[0] ?? workspaceUploadBody.data.files[0];
  assert.equal(workspaceUploaded.filename, "workspace-asset.txt");
  const promotedWorkspace = await requestJson(`/api/v1/files/${workspaceUploaded.id}/promote`, {
    method: "POST"
  });
  assert.equal(promotedWorkspace.response.status, 200);
  assert.equal(promotedWorkspace.body.data.scope, "workspace");
  assert.equal(promotedWorkspace.body.data.sessionId ?? null, null);
  const workspaceList = await requestJson("/api/v1/files?scope=workspace&origin=uploaded,saved");
  assert.equal(workspaceList.response.status, 200);
  assert.equal(
    workspaceList.body.data.files.some((file) => file.id === promotedWorkspace.body.data.id),
    true
  );

  const search = await requestJson("/api/v1/knowledge-bases/metrics-docs/search", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query: "revenue metric" })
  });
  assert.equal(search.body.data[0].filename, "metrics.md");

  const mcpConfig = await requestJson("/api/v1/mcp-servers", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      id: "smoke-mcp",
      name: "Smoke MCP",
      transport: "streamable-http",
      serverUrl: `http://127.0.0.1:${mcpAddress.port}`,
      toolAllowlist: ["echo"],
      timeoutMs: 5000
    })
  });
  assert.equal(mcpConfig.response.status, 201);
  assert.deepEqual(mcpConfig.body.data.toolAllowlist, ["echo"]);
  assert.equal(mcpConfig.body.data.timeoutMs, 5000);
  const mcpTest = await requestJson("/api/v1/mcp-servers/smoke-mcp/test", { method: "POST" });
  assert.equal(mcpTest.body.data.toolCount, 1);
  const mcpTools = await requestJson("/api/v1/mcp-servers/smoke-mcp/tools");
  assert.equal(mcpTools.body.data.length, 1);
  assert.equal(mcpTools.body.data[0].name, "echo");

  const datalinkConfig = await requestJson("/api/v1/mcp-servers", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      id: "datalink",
      name: "DataLink",
      transport: "streamable-http",
      serverUrl: `http://127.0.0.1:${mcpAddress.port}/mcp`,
      apiUrl: `http://127.0.0.1:${datalinkApiAddress.port}`,
    })
  });
  assert.equal(datalinkConfig.response.status, 201);
  assert.equal(datalinkConfig.body.data.apiUrl, `http://127.0.0.1:${datalinkApiAddress.port}`);

  const datalinkServers = await requestJson("/api/v1/datalink/servers");
  assert.equal(datalinkServers.body.data.servers[0].apiUrl, `http://127.0.0.1:${datalinkApiAddress.port}`);
  const datalinkGraph = await requestJson("/api/v1/datalink/datalink/graph");
  assert.equal(datalinkGraph.body.data.graph.nodes[0].id, "table:metrics");
  const datalinkExplore = await requestJson("/api/v1/datalink/datalink/explore", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query: "metrics", focus: "schema" })
  });
  assert.equal(datalinkExplore.body.data.result, "metrics table");
  await requestJson("/api/v1/datalink/datalink/tables", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source: "/data/metrics.csv", sourceType: "csv" })
  });
  await requestJson("/api/v1/datalink/datalink/tables", {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tableId: "table:metrics" })
  });
  await requestJson("/api/v1/datalink/datalink/rebuild", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({})
  });
  assert.deepEqual(
    datalinkApiRequests.map((entry) => [entry.method, entry.path]),
    [
      ["GET", "/show"],
      ["POST", "/explore"],
      ["POST", "/add-table"],
      ["POST", "/remove-table"],
      ["POST", "/rebuild"],
    ],
  );

  const skillForm = new FormData();
  skillForm.set("file", new Blob([
    "---\n",
    "name: Smoke Skill\n",
    "description: Smoke skill package\n",
    "version: 1.0.0\n",
    "allowed-tools: [inspect_schema, echo]\n",
    "---\n",
    "Inspect schema first.\n"
  ], { type: "text/markdown" }), "SKILL.md");
  skillForm.set("defaultDbIds", "local-sqlite");
  skillForm.set("defaultKbIds", "metrics-docs");
  skillForm.set("defaultMcpIds", "smoke-mcp");
  skillForm.set("modelProfileId", "smoke-openai-compatible");
  const skill = await requestJson("/api/v1/skills", { method: "POST", body: skillForm });
  assert.equal(skill.response.status, 201);
  assert.equal(skill.body.data.validationStatus, "valid");
  assert.equal("packageContent" in skill.body.data, false);
  assert.equal(typeof skill.body.data.packageFileRefId, "string");
  assert.deepEqual(skill.body.data.allowedTools, ["inspect_schema", "echo"]);
  assert.deepEqual(skill.body.data.defaultDbIds, ["local-sqlite"]);
  assert.deepEqual(skill.body.data.defaultKbIds, ["metrics-docs"]);
  assert.deepEqual(skill.body.data.defaultMcpIds, ["smoke-mcp"]);
  assert.equal(skill.body.data.modelProfileId, "smoke-openai-compatible");
  const workspaceFileAssets = await requestJson("/api/v1/files?scope=workspace&origin=uploaded,saved");
  assert.equal(workspaceFileAssets.response.status, 200);
  assert.equal(
    workspaceFileAssets.body.data.files.some((file) => file.id === skill.body.data.packageFileRefId),
    false
  );
  const skillDownload = await requestRaw(`/api/v1/skills/${skill.body.data.id}/download`);
  assert.equal(skillDownload.status, 200);
  assert.equal((await skillDownload.text()).includes("Inspect schema first."), true);
  const skillSelection = await requestJson("/api/v1/skills/select", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      user_input: "use smoke skill to inspect schema",
      run_config: {
        activeDatasourceId: "local-sqlite",
        enabledDatasourceIds: ["local-sqlite"],
        skill_mode: "auto"
      }
    })
  });
  assert.equal(skillSelection.response.status, 200);
  assert.equal(skillSelection.body.data.skills.some((item) => item.id === skill.body.data.id), true);
  assert.equal(skillSelection.body.data.effectivePolicy.allowedTools.includes("inspect_schema"), true);

  const modelProfile = await requestJson("/api/v1/model-profiles", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      id: "smoke-openai-compatible",
      name: "Smoke OpenAI Compatible",
      provider: "openai-compatible",
      modelName: "smoke-model",
      baseUrl: `http://127.0.0.1:${modelProviderAddress.port}`,
      credentials: { apiKey: "smoke-model-key" },
      topP: 0.75,
      frequencyPenalty: 0.25,
      presencePenalty: -0.25,
      contextLength: 64000,
      reasoningModel: true,
      timeoutMs: 5000
    })
  });
  assert.equal(modelProfile.response.status, 201);
  assert.equal(modelProfile.body.data.hasSecret, true);
  assert.equal(modelProfile.body.data.contextLength, 64000);
  assert.equal(modelProfile.body.data.reasoningModel, true);
  assert.equal(JSON.stringify(modelProfile.body).includes("smoke-model-key"), false);
  assert.equal(modelProfile.body.data.connectionStatus, "untested");

  const serverDefaultProfile = await requestJson("/api/v1/model-profiles/server-default");
  assert.equal(serverDefaultProfile.body.data.connectionStatus, "untested");

  const modelProfileTest = await requestJson("/api/v1/model-profiles/smoke-openai-compatible/test", {
    method: "POST"
  });
  assert.equal(modelProfileTest.body.success, true);
  assert.equal(modelProfileTest.body.data.status, "connected");
  assert.equal(modelProfileTest.body.data.model, "smoke-model");
  assert.equal(modelProfileTest.body.data.response, "OK");
  assert.match(
    modelProfileTest.body.data.reason,
    /Model "smoke-model" responded successfully \(OK\)\./
  );
  assert.equal(modelProbeRequest.method, "POST");
  assert.equal(modelProbeRequest.path, "/chat/completions");
  assert.equal(modelProbeRequest.authorization, "Bearer smoke-model-key");
  assert.equal(modelProbeRequest.body.model, "smoke-model");
  assert.equal(modelProbeRequest.body.messages.some((message) => message.role === "system"), true);
  const resolvedModelRun = resolveRunConfig({
    defaultDatasourceId: "dtc-growth-demo",
    metadataStore,
    runInput: {
      threadId: "model-profile-session",
      runId: "model-profile-run",
      parentRunId: undefined,
      messages: [{ id: "user-message-model-profile", role: "user", content: "inspect metrics" }],
      tools: [],
      context: [],
      state: {},
      forwardedProps: {
        run_config: {
          activeDatasourceId: "local-sqlite",
          activeLlmProfileId: "smoke-openai-compatible",
          enabledDatasourceIds: ["local-sqlite"],
          enabledKnowledgeIds: [],
          enabledMcpServerIds: [],
          enabledSkillIds: []
        }
      }
    },
    userId: userId,
    userInput: "inspect metrics",
    workspaceId
  });
  assert.equal(resolvedModelRun.modelSettings?.topP, 0.75);
  assert.equal(resolvedModelRun.modelSettings?.frequencyPenalty, 0.25);
  assert.equal(resolvedModelRun.modelSettings?.presencePenalty, -0.25);
  assert.equal(resolvedModelRun.modelContextProfile?.contextWindow, 64000);
  assert.equal(resolvedModelRun.modelContextProfile?.outputReserve, 4096);
  assert.equal(resolvedModelRun.reasoningModel, true);
  assert.equal(resolvedModelRun.runTimeoutMs, 5000);
  const skillBoundRun = resolveRunConfig({
    defaultDatasourceId: "dtc-growth-demo",
    metadataStore,
    runInput: {
      threadId: "skill-bound-session",
      runId: "skill-bound-run",
      parentRunId: undefined,
      messages: [{ id: "user-message-skill-bound", role: "user", content: "use smoke skill to inspect schema" }],
      tools: [],
      context: [],
      state: {},
      forwardedProps: {
        run_config: {
          enabledDatasourceIds: ["dtc-growth-demo"],
          enabledKnowledgeIds: [],
          enabledMcpServerIds: [],
          enabledSkillIds: [skill.body.data.id],
          skill_mode: "auto"
        }
      }
    },
    userId: userId,
    userInput: "use smoke skill to inspect schema",
    workspaceId
  });
  assert.equal(skillBoundRun.effectiveRunConfig.activeDatasourceId, "local-sqlite");
  assert.equal(skillBoundRun.effectiveRunConfig.activeLlmProfileId, "smoke-openai-compatible");
  assert(skillBoundRun.effectiveRunConfig.enabledDatasourceIds.includes("local-sqlite"));
  assert(skillBoundRun.effectiveRunConfig.enabledKnowledgeIds.includes("metrics-docs"));
  assert(skillBoundRun.effectiveRunConfig.enabledMcpServerIds.includes("smoke-mcp"));
  assert.deepEqual(skillBoundRun.mcpRuntime.toolNames, ["echo"]);
  assert.equal(skillBoundRun.mcpRuntime.servers[0]?.timeoutMs, 5000);
  assert.deepEqual(skillBoundRun.mcpRuntime.servers[0]?.toolAllowlist, ["echo"]);
  const currentMetricsDocsResource = metadataStore.configResources.get({
    id: "metrics-docs",
    workspace_id: workspaceId,
    user_id: userId,
    kind: "knowledge-base"
  });
  const currentMcpResource = metadataStore.configResources.get({
    id: "smoke-mcp",
    workspace_id: workspaceId,
    user_id: userId,
    kind: "mcp-server"
  });
  const currentModelProfileResource = metadataStore.configResources.get({
    id: "smoke-openai-compatible",
    workspace_id: workspaceId,
    user_id: userId,
    kind: "model-profile"
  });
  assert.equal(
    skillBoundRun.effectiveRunConfig.resourceRevisions["datasource:local-sqlite"],
    sampleDisabledPatch.body.data.revision
  );
  assert.equal(
    skillBoundRun.effectiveRunConfig.resourceRevisions["knowledge-base:metrics-docs"],
    currentMetricsDocsResource.revision
  );
  assert.equal(skillBoundRun.effectiveRunConfig.resourceRevisions["mcp-server:smoke-mcp"], currentMcpResource.revision);
  assert.equal(
    skillBoundRun.effectiveRunConfig.resourceRevisions["model-profile:smoke-openai-compatible"],
    currentModelProfileResource.revision
  );
  const explicitResourceRun = resolveRunConfig({
    defaultDatasourceId: "dtc-growth-demo",
    metadataStore,
    runInput: {
      threadId: "skill-explicit-session",
      runId: "skill-explicit-run",
      parentRunId: undefined,
      messages: [{ id: "user-message-skill-explicit", role: "user", content: "use smoke skill to inspect schema" }],
      tools: [],
      context: [],
      state: {},
      forwardedProps: {
        run_config: {
          activeDatasourceId: "dtc-growth-demo",
          enabledDatasourceIds: ["dtc-growth-demo"],
          enabledKnowledgeIds: [],
          enabledMcpServerIds: [],
          enabledSkillIds: [skill.body.data.id],
          skill_mode: "auto"
        }
      }
    },
    userId: userId,
    userInput: "use smoke skill to inspect schema",
    workspaceId
  });
  assert.equal(explicitResourceRun.effectiveRunConfig.activeDatasourceId, "dtc-growth-demo");
  assert(explicitResourceRun.effectiveRunConfig.enabledDatasourceIds.includes("local-sqlite"));
  const testedModelProfile = await requestJson("/api/v1/model-profiles/smoke-openai-compatible");
  assert.equal(testedModelProfile.body.data.connectionStatus, "connected");
  assert.equal(testedModelProfile.body.data.revision, modelProfileTest.body.data.revision);

  const patchedModelProfile = await requestJson("/api/v1/model-profiles/smoke-openai-compatible", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      modelName: "smoke-model-v2",
      revision: testedModelProfile.body.data.revision
    })
  });
  assert.equal(patchedModelProfile.response.status, 200);
  assert.equal(patchedModelProfile.body.data.connectionStatus, "untested");

  const badModelProfile = await requestJson("/api/v1/model-profiles", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      id: "smoke-bad-model",
      name: "Smoke Bad Model",
      provider: "openai-compatible",
      modelName: "smoke-model",
      baseUrl: "http://127.0.0.1:1",
      credentials: { apiKey: "bad-key" }
    })
  });
  assert.equal(badModelProfile.response.status, 201);
  const badModelProfileTest = await requestJson("/api/v1/model-profiles/smoke-bad-model/test", {
    method: "POST"
  });
  assert.equal(badModelProfileTest.body.success, false);
  assert.equal(badModelProfileTest.body.error.code, "PROVIDER_TEST_FAILED");
  assert.match(badModelProfileTest.body.error.message, /^PROVIDER_TEST_FAILED:/);
  assert.equal(
    badModelProfileTest.body.error.message.includes("REVISION_CONFLICT"),
    false
  );
  const failedModelProfile = await requestJson("/api/v1/model-profiles/smoke-bad-model");
  assert.equal(failedModelProfile.body.data.connectionStatus, "failed");

  const effective = resolveEffectiveRunConfig({
    threadId: "effective-session",
    runId: "effective-run",
    parentRunId: undefined,
    messages: [{ id: "user-message", role: "user", content: "inspect metrics" }],
    tools: [],
    context: [{
      description: "run_config",
      value: {
        activeDatasourceId: "local-sqlite",
        activeLlmProfileId: "server-default",
        enabledDatasourceIds: ["local-sqlite"],
        enabledKnowledgeIds: [],
        enabledMcpServerIds: [],
        enabledSkillIds: []
      }
    }],
    state: {},
    forwardedProps: {}
  }, metadataStore, userId, "dtc-growth-demo", workspaceId);
  assert.equal(effective.activeSkillId, undefined);
  assert.equal(effective.resourceRevisions["datasource:local-sqlite"], sampleDisabledPatch.body.data.revision);
  assert.equal(effective.resourceRevisions["model-profile:server-default"] > 0, true);

  const currentKnowledgeBase = await requestJson("/api/v1/knowledge-bases/metrics-docs");
  const workspacePatch = await requestJson("/api/v1/workspace-config", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      knowledgeBases: [{
        id: "metrics-docs",
        defaultEnabled: false,
        revision: currentKnowledgeBase.body.data.revision
      }]
    })
  });
  assert.equal(workspacePatch.body.data.knowledgeBases[0].defaultEnabled, false);
  const defaults = await requestJson("/api/v1/run-defaults");
  assert.equal(defaults.body.data.enabledKnowledgeIds.includes("metrics-docs"), false);

  metadataStore.sessions.create({ user_id: userId, id: "session-smoke", title: "config smoke" });
  metadataStore.runs.create({
    user_id: userId,
    id: "run-smoke",
    session_id: "session-smoke",
    request_fingerprint: "config-smoke",
    user_input: "config smoke",
    status: "completed"
  });
  metadataStore.contextPackageSnapshots.create({
    user_id: userId,
    session_id: "session-smoke",
    run_id: "run-smoke",
    package_id: "context-smoke",
    revision: 0,
    payload: {}
  });
  metadataStore.protocolStates.compareAndSetWithEvents({
    user_id: userId,
    run_id: "run-smoke",
    segment_id: "segment-smoke",
    expected_revision: -1,
    state: {
      protocolId: "general-task",
      protocolVersion: "1",
      runId: "run-smoke",
      segmentId: "segment-smoke",
      revision: 0,
      phase: "work",
      status: "completed",
      contextPackageRef: { packageId: "context-smoke", revision: 0 },
      actions: [],
      completionRejections: 0,
      domain: {}
    }
  }, [{
    eventId: "protocol-event-smoke",
    type: "protocol.run.completed",
    runId: "run-smoke",
    segmentId: "segment-smoke",
    revision: 0
  }]);
  assert.equal(metadataStore.protocolStates.pendingEvents({
    user_id: userId,
    run_id: "run-smoke"
  }).length, 1);
  const artifact = metadataStore.artifacts.create({
    id: "artifact-smoke",
    user_id: userId,
    session_id: "session-smoke",
    run_id: "run-smoke",
    type: "table",
    name: "metrics",
    preview_json: { columns: ["name", "value"], rows: [{ name: "revenue", value: 42 }] }
  });
  assert.equal(artifact.id, "artifact-smoke");
  const download = await client.fetch("/api/v1/artifacts/artifact-smoke/download");
  assert.equal(download.headers.get("content-type"), "text/csv; charset=utf-8");
  assert.equal((await download.text()).includes("revenue,42"), true);

  const deletedSession = await requestJson("/api/v1/sessions/session-smoke", { method: "DELETE" });
  assert.equal(deletedSession.response.status, 200, JSON.stringify(deletedSession.body));
  assert.deepEqual(deletedSession.body.data, {
    sessionId: "session-smoke",
    deleted: true,
    deletedSessionIds: ["session-smoke"]
  });
  assert.equal(metadataStore.protocolStates.find({
    user_id: userId,
    run_id: "run-smoke",
    segment_id: "segment-smoke"
  }), undefined);
  assert.deepEqual(metadataStore.protocolStates.pendingEvents({
    user_id: userId,
    run_id: "run-smoke"
  }), []);

  const builtinDelete = await requestJson("/api/v1/datasources/dtc-growth-demo", { method: "DELETE" });
  assert.equal(builtinDelete.response.status, 200);
  assert.equal(builtinDelete.body.data.deleted, true);

  metadataStore.sqlAuditLogs.create({
    user_id: userId,
    id: "audit-before-delete",
    datasource_id: "local-sqlite",
    sql_text: "SELECT 1",
    status: "succeeded"
  });
  const deletedDatasource = await requestJson("/api/v1/datasources/local-sqlite", { method: "DELETE" });
  assert.equal(deletedDatasource.body.data.deleted, true);
  const datasourceList = await requestJson("/api/v1/datasources");
  assert.equal(datasourceList.body.data.some((item) => item.id === "local-sqlite"), false);
  assert.throws(() => metadataStore.secrets.get({
    ref: createdDatasource.body.data.secretRef,
    workspace_id: workspaceId,
    user_id: userId
  }), /SECRET_NOT_FOUND/u);

  console.log(
    "Config API smoke OK: secrets, datasource/types/policies, chat upload, datasource upload, conversation, revision, "
      + "KB, MCP, model profile, skill binding, defaults, artifact, session delete, tombstone"
  );
} finally {
  await closeHttpServer(server);
  await closeHttpServer(datalinkApiServer);
  await closeHttpServer(mcpServer);
  await closeHttpServer(modelProviderServer);
  await taskStateRuntime.close();
}

function pick(value, keys) {
  return Object.fromEntries(keys.map((key) => [key, value[key]]));
}

function datasourceSchemaTables(value) {
  return value?.schema?.tables ?? value?.tables ?? [];
}

process.exit(0);
