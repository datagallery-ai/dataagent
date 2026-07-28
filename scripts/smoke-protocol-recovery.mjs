import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { createMetadataStore } from "../packages/metadata/dist/index.js";
import { createVerifiedTestIdentity } from "./lib/metadata-test-identity.mjs";

const root = mkdtempSync(join(tmpdir(), "datafoundry-protocol-recovery-"));
const databasePath = join(root, "metadata.sqlite");

const sessionId = "protocol-session";
const runId = "protocol-run";

try {
  let store = createMetadataStore({ database_path: databasePath });
  store.sessions.create({ user_id: userId, id: sessionId, title: "Protocol recovery" });
  store.runs.create({
    user_id: userId,
    id: runId,
    session_id: sessionId,
    user_input: "Analyze orders",
    status: "running"
  });
  store.contextPackageSnapshots.create({
    user_id: userId,
    session_id: sessionId,
    run_id: runId,
    package_id: "context-1",
    revision: 0,
    payload: { packageId: "context-1", revision: 0 }
  });
  const initialState = createState(0, "semantic_grounding");
  store.protocolStates.compareAndSet({
    user_id: userId,
    run_id: runId,
    segment_id: "segment-1",
    expected_revision: -1,
    state: initialState
  });

  assert.throws(() => store.protocolStates.compareAndSet({
    user_id: userId,
    run_id: runId,
    segment_id: "segment-1",
    expected_revision: -1,
    state: initialState
  }), /PROTOCOL_REVISION_CONFLICT:protocol-run:segment-1:-1:0/);

  const stateUpdatedEvent = {
    eventId: "segment-1:1:protocol.state.updated",
    type: "protocol.state.updated",
    runId,
    segmentId: "segment-1",
    protocolId: "data-analysis",
    protocolVersion: "1",
    revision: 1
  };
  store.protocolStates.compareAndSetWithEvents({
    user_id: userId,
    run_id: runId,
    segment_id: "segment-1",
    expected_revision: 0,
    state: createState(1, "execution")
  }, [stateUpdatedEvent]);
  store.close();

  store = createMetadataStore({ database_path: databasePath });
  const recovered = store.protocolStates.latestByRun({ user_id: userId, run_id: runId });
  assert.equal(recovered?.revision, 1);
  assert.equal(JSON.parse(recovered?.state_json ?? "{}").phase, "execution");
  assert.deepEqual(store.protocolStates.pendingEvents({ user_id: userId, run_id: runId }), [stateUpdatedEvent]);
  store.protocolStates.acknowledgeEvent({ user_id: userId, event_id: stateUpdatedEvent.eventId });
  assert.deepEqual(store.protocolStates.pendingEvents({ user_id: userId, run_id: runId }), []);
  assert.throws(() => store.protocolStates.compareAndSet({
    user_id: userId,
    run_id: runId,
    segment_id: "segment-missing-context",
    expected_revision: -1,
    state: {
      ...createState(0, "semantic_grounding"),
      segmentId: "segment-missing-context",
      contextPackageRef: { packageId: "missing-context", revision: 0 }
    }
  }), /PROTOCOL_CONTEXT_REF_NOT_FOUND:missing-context@0/);
  store.close();

  console.log("Protocol recovery smoke OK: CAS, event journal, restart recovery, and ContextPackage refs verified.");
} finally {
  rmSync(root, { recursive: true, force: true });
}

function createState(revision, phase) {
  return {
    protocolId: "data-analysis",
    protocolVersion: "1",
    runId,
    segmentId: "segment-1",
    revision,
    phase,
    status: "active",
    contextPackageRef: { packageId: "context-1", revision: 0 },
    actions: [],
    completionRejections: 0,
    domain: {}
  };
}
