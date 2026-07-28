import { EventType } from "@ag-ui/client";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { ConversationMemoryService } from "../apps/api/dist/conversation-memory.js";
import { RunFinalizer } from "../apps/api/dist/run-finalizer.js";
import { createMetadataStore } from "../packages/metadata/dist/index.js";
import { createVerifiedTestIdentity } from "./lib/metadata-test-identity.mjs";

const root = mkdtempSync(join(tmpdir(), "datafoundry-run-finalizer-"));


try {
  const metadataStore = createMetadataStore({ database_path: join(root, "metadata.sqlite") });
const __testIdentity = createVerifiedTestIdentity(metadataStore);
const userId = __testIdentity.userId;
const workspaceId = __testIdentity.workspaceId;


  await runCanceledDraftScenario(metadataStore);
  await runCanceledEmptyDraftScenario(metadataStore);
  await runCompletionRequiresDecisionScenario(metadataStore);
  await runCompletedNoDuplicateScenario(metadataStore);

  console.log("Run finalizer smoke OK: canceled runs persist assistant drafts without completion memory.");
} finally {
  rmSync(root, { recursive: true, force: true });
}

async function runCanceledDraftScenario(metadataStore) {
  const sessionId = "cancel-draft-session";
  const runId = "cancel-draft-run";
  const observer = createObservedRun(metadataStore, sessionId, runId, "Inspect orders");
  observer.observe({
    type: EventType.TEXT_MESSAGE_CHUNK,
    role: "assistant",
    messageId: "assistant-cancel-draft",
    delta: "I inspected the first table before cancellation.",
  });

  const emitted = [];
  const finalizer = createFinalizer(metadataStore, sessionId, runId, observer, emitted);
  await finalizer.cancelRun({
    reason: "user-requested",
    terminalEvent: { type: EventType.RUN_FINISHED, status: "cancelled", timestamp: Date.now() },
  });

  const messages = metadataStore.conversationMessages.listRecent({
    user_id: userId,
    session_id: sessionId,
    limit: 10,
  });
  assert.equal(metadataStore.runs.get({ user_id: userId, run_id: runId }).status, "canceled");
  assert.equal(messages.filter((message) => message.role === "assistant").length, 1);
  assert.match(messages.find((message) => message.role === "assistant")?.content_text ?? "", /first table/);
  assert.equal(emitted.at(-1)?.type, EventType.RUN_FINISHED);
}

async function runCanceledEmptyDraftScenario(metadataStore) {
  const sessionId = "cancel-empty-session";
  const runId = "cancel-empty-run";
  const observer = createObservedRun(metadataStore, sessionId, runId, "Stop immediately");
  const finalizer = createFinalizer(metadataStore, sessionId, runId, observer, []);

  await finalizer.cancelRun({
    reason: "user-requested",
    terminalEvent: { type: EventType.RUN_FINISHED, status: "cancelled", timestamp: Date.now() },
  });

  const messages = metadataStore.conversationMessages.listRecent({
    user_id: userId,
    session_id: sessionId,
    limit: 10,
  });
  assert.equal(messages.filter((message) => message.role === "assistant").length, 0);
}

async function runCompletedNoDuplicateScenario(metadataStore) {
  const sessionId = "complete-session";
  const runId = "complete-run";
  const observer = createObservedRun(metadataStore, sessionId, runId, "Complete normally");
  observer.observe({
    type: EventType.TEXT_MESSAGE_CHUNK,
    role: "assistant",
    messageId: "assistant-complete",
    delta: "Completed final answer.",
  });

  const finalizer = createFinalizer(metadataStore, sessionId, runId, observer, []);
  await finalizer.complete({
    terminalDecision: {
      status: "completed",
      evaluatedContextPackageRef: { packageId: "context-complete", revision: 1 },
      evidenceRefs: []
    },
    terminalEvent: { type: EventType.RUN_FINISHED, timestamp: Date.now() },
  });

  const messages = metadataStore.conversationMessages.listRecent({
    user_id: userId,
    session_id: sessionId,
    limit: 10,
  });
  assert.equal(messages.filter((message) => message.role === "assistant").length, 1);
  assert.equal(metadataStore.runs.get({ user_id: userId, run_id: runId }).status, "completed");
}

async function runCompletionRequiresDecisionScenario(metadataStore) {
  const sessionId = "complete-requires-decision-session";
  const runId = "complete-requires-decision-run";
  const observer = createObservedRun(metadataStore, sessionId, runId, "Complete only when governed");
  const finalizer = createFinalizer(metadataStore, sessionId, runId, observer, []);

  await assert.rejects(
    finalizer.complete({ terminalEvent: { type: EventType.RUN_FINISHED, timestamp: Date.now() } }),
    /PROTOCOL_TERMINAL_DECISION_REQUIRED/
  );
  assert.equal(metadataStore.runs.get({ user_id: userId, run_id: runId }).status, "running");
}

function createObservedRun(metadataStore, sessionId, runId, userInput) {
  metadataStore.sessions.create({ user_id: userId, id: sessionId, title: sessionId });
  metadataStore.runs.create({
    user_id: userId,
    id: runId,
    session_id: sessionId,
    request_fingerprint: `${runId}-fingerprint`,
    user_input: userInput,
    status: "running",
  });

  const service = new ConversationMemoryService({
    repository: metadataStore.conversationMessages,
    sessionId,
    userId,
  });
  service.persistCurrentUserMessage({
    currentUserText: userInput,
    runId,
    runInput: {
      threadId: sessionId,
      runId,
      messages: [{ id: `${runId}:frontend-user`, role: "user", content: userInput }],
      tools: [],
      context: [],
    },
  });
  return service.createEventObserver({ runId });
}

function createFinalizer(metadataStore, sessionId, runId, observer, emitted) {
  return new RunFinalizer({
    destroyWorkspace: async () => undefined,
    emit: (event) => emitted.push(event),
    fileAssetService: {
      gcOrphanAssets: () => undefined,
      syncWorkspaceFile: () => undefined,
    },
    flushCompletedMemory: async ({ signal }) => {
      await observer.flushCompleted({ signal });
    },
    flushDraftsMemory: () => observer.flushDrafts(),
    memoryExtractionTimeoutMs: 50,
    metadataStore,
    runId,
    sessionDir: join(root, "sessions", sessionId),
    sessionId,
    userId,
    workspaceId,
  });
}
