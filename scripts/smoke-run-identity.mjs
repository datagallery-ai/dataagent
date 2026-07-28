import { EventType } from "@ag-ui/core";

import {
  createRunRequestFingerprint,
  resolveExistingRun,
  validateParentRun
} from "../apps/api/dist/run-identity.js";
import { resolveRunIdentity } from "../apps/api/dist/run-identity-orchestrator.js";
import { RunEventWriter, createMetadataStore } from "../packages/metadata/dist/index.js";
import { createVerifiedTestIdentity } from "./lib/metadata-test-identity.mjs";

const databasePath = `storage/metadata/run-identity-smoke-${Date.now()}.sqlite`;
const store = createMetadataStore({ database_path: databasePath });
const __testIdentity = createVerifiedTestIdentity(store);
const userId = __testIdentity.userId;
const workspaceId = __testIdentity.workspaceId;


try {
  
  const sessionId = "identity-session";
  const runId = "identity-run";
  const requestInput = {
    threadId: sessionId,
    runId,
    messages: [{ id: "message-1", role: "user", content: "inspect orders" }],
    tools: [],
    context: []
  };
  const reorderedRequestInput = {
    context: [],
    tools: [],
    messages: [{ content: "inspect orders", role: "user", id: "message-1" }],
    runId,
    threadId: sessionId
  };
  const changedHistoryRequestInput = {
    threadId: sessionId,
    runId,
    messages: [
      { id: "older-user", role: "user", content: "old question" },
      { id: "changed-assistant", role: "assistant", content: "changed untrusted client history" },
      { id: "message-regenerated", role: "user", content: "inspect orders" }
    ],
    tools: [],
    context: []
  };
  const changedCurrentUserRequestInput = {
    threadId: sessionId,
    runId,
    messages: [{ id: "message-1", role: "user", content: "inspect customers" }],
    tools: [],
    context: []
  };
  const effectiveRunConfig = {
    activeDatasourceId: "api-duckdb-demo",
    enabledDatasourceIds: ["api-duckdb-demo"],
    enabledKnowledgeBaseIds: [],
    enabledMcpServerIds: [],
    enabledSkillIds: [],
    workspaceAttachments: []
  };
  const fingerprint = createRunRequestFingerprint(requestInput, effectiveRunConfig);
  const reorderedFingerprint = createRunRequestFingerprint(reorderedRequestInput, effectiveRunConfig);
  const changedHistoryFingerprint = createRunRequestFingerprint(changedHistoryRequestInput, effectiveRunConfig);
  const changedCurrentUserFingerprint = createRunRequestFingerprint(changedCurrentUserRequestInput, effectiveRunConfig);

  if (fingerprint !== reorderedFingerprint) {
    throw new Error("Expected semantically identical request objects to have the same fingerprint");
  }
  if (fingerprint !== changedHistoryFingerprint) {
    throw new Error("Expected untrusted client history changes to be ignored by run fingerprint");
  }
  if (fingerprint === changedCurrentUserFingerprint) {
    throw new Error("Expected current user changes to affect run fingerprint");
  }

  store.sessions.create({ user_id: userId, id: sessionId, title: "identity smoke" });
  store.dataSources.create({
    user_id: userId,
    id: "api-duckdb-demo",
    name: "Identity Smoke DuckDB",
    type: "duckdb",
    config: { database: ":memory:" },
    status: "ready"
  });
  store.runs.create({
    user_id: userId,
    id: runId,
    session_id: sessionId,
    request_fingerprint: fingerprint,
    user_input: "inspect orders",
    status: "running"
  });

  const writer = new RunEventWriter(store.runEvents);
  writer.write({
    user_id: userId,
    run_id: runId,
    session_id: sessionId,
    event: { type: EventType.RUN_STARTED, threadId: sessionId, runId }
  });

  assertThrows(
    () =>
      resolveExistingRun({
        existingRun: store.runs.get({ user_id: userId, run_id: runId }),
        requestFingerprint: fingerprint,
        runEventWriter: writer,
        sessionId
      }),
    "RUN_ALREADY_ACTIVE"
  );

  writer.write({
    user_id: userId,
    run_id: runId,
    session_id: sessionId,
    event: { type: EventType.RUN_FINISHED, threadId: sessionId, runId }
  });
  const completedRun = store.runs.updateStatus({ user_id: userId, run_id: runId, status: "completed" });
  const replayedEvents = resolveExistingRun({
    existingRun: completedRun,
    requestFingerprint: fingerprint,
    runEventWriter: writer,
    sessionId
  });

  if (replayedEvents.length !== 2 || replayedEvents[1].type !== EventType.RUN_FINISHED) {
    throw new Error("Expected a completed duplicate run to replay its persisted AG-UI events");
  }

  assertThrows(
    () =>
      resolveExistingRun({
        existingRun: completedRun,
        requestFingerprint: "different-request",
        runEventWriter: writer,
        sessionId
      }),
    "RUN_REQUEST_MISMATCH"
  );
  assertThrows(
    () =>
      resolveExistingRun({
        existingRun: completedRun,
        requestFingerprint: fingerprint,
        runEventWriter: writer,
        sessionId: "different-session"
      }),
    "RUN_SESSION_MISMATCH"
  );

  validateParentRun({ metadataStore: store, parentRunId: runId, sessionId, userId });
  assertThrows(
    () => validateParentRun({ metadataStore: store, parentRunId: runId, sessionId: "different-session", userId }),
    "PARENT_RUN_SESSION_MISMATCH"
  );

  store.runs.create({
    user_id: userId,
    id: "identity-background-active-run",
    session_id: sessionId,
    request_fingerprint: "background-active-fingerprint",
    user_input: "background active query",
    status: "running"
  });
  const replayWhileSessionActive = resolveRunIdentity({
    effectiveRunConfig,
    metadataStore: store,
    modelName: "identity-smoke-model",
    runEventWriter: writer,
    runInput: requestInput,
    userId,
    userInput: "inspect orders"
  });
  if (replayWhileSessionActive.kind !== "replay" || replayWhileSessionActive.events.length !== 2) {
    throw new Error("Expected completed duplicate run to replay even when another run is active in the session");
  }

  const activeSessionId = "identity-active-session";
  const activeRunId = "identity-active-run";
  store.sessions.create({ user_id: userId, id: activeSessionId, title: "active identity smoke" });
  store.runs.create({
    user_id: userId,
    id: activeRunId,
    session_id: activeSessionId,
    request_fingerprint: "active-fingerprint",
    user_input: "active query",
    status: "running"
  });

  assertThrows(
    () =>
      resolveRunIdentity({
        effectiveRunConfig,
        metadataStore: store,
        modelName: "identity-smoke-model",
        runEventWriter: writer,
        runInput: {
          threadId: activeSessionId,
          runId: "identity-active-run-2",
          messages: [{ id: "message-active-2", role: "user", content: "second active query" }],
          tools: [],
          context: []
        },
        userId,
        userInput: "second active query"
      }),
    "RUN_ALREADY_ACTIVE"
  );

  console.log(`Run identity smoke OK: fingerprint=${fingerprint.slice(0, 12)}, replayed=${replayedEvents.length}`);
} finally {
  store.close();
}

function assertThrows(callback, expectedMessage) {
  try {
    callback();
  } catch (error) {
    if (error instanceof Error && error.message.includes(expectedMessage)) {
      return;
    }
    throw error;
  }

  throw new Error(`Expected error containing: ${expectedMessage}`);
}
