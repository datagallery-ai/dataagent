import {
  RunEventWriter,
  createMetadataStore
} from "../packages/metadata/dist/index.js";
import { EventType } from "@ag-ui/core";
import { createVerifiedTestIdentity } from "./lib/metadata-test-identity.mjs";

const databasePath = `storage/metadata/metadata-smoke-${Date.now()}.sqlite`;
const store = createMetadataStore({ database_path: databasePath });
const __testIdentity = createVerifiedTestIdentity(store);
const userId = __testIdentity.userId;
const workspaceId = __testIdentity.workspaceId;


try {
  
  const sessionId = "session-smoke";
  const runId = "run-smoke";

  store.sessions.create({
    user_id: userId,
    id: sessionId,
    title: "metadata smoke"
  });
  store.runs.create({
    user_id: userId,
    id: runId,
    session_id: sessionId,
    request_fingerprint: "request-smoke",
    user_input: "metadata smoke",
    status: "running"
  });

  const duplicateClaim = store.runs.claim({
    user_id: userId,
    id: runId,
    session_id: sessionId,
    request_fingerprint: "request-smoke",
    user_input: "metadata smoke",
    status: "running"
  });

  if (duplicateClaim.created) {
    throw new Error("Expected duplicate run claim to return the existing run");
  }

  const childRun = store.runs.create({
    user_id: userId,
    id: "run-smoke-child",
    session_id: sessionId,
    parent_run_id: runId,
    request_fingerprint: "request-smoke-child",
    user_input: "metadata smoke child",
    status: "running"
  });

  if (childRun.parent_run_id !== runId) {
    throw new Error("Expected the same session to support a parent-linked second run");
  }

  const writer = new RunEventWriter(store.runEvents);
  writer.write({
    user_id: userId,
    run_id: runId,
    session_id: sessionId,
    event: {
      type: EventType.RUN_STARTED,
      threadId: sessionId,
      runId
    }
  });
  writer.write({
    user_id: userId,
    run_id: runId,
    session_id: sessionId,
    event: {
      type: EventType.RUN_FINISHED,
      threadId: sessionId,
      runId
    }
  });

  const replayed = writer.replay({ user_id: userId, run_id: runId });
  const otherUserReplay = writer.replay({ user_id: "other-user", run_id: runId });

  if (replayed.length !== 2) {
    throw new Error(`Expected 2 replayed events, got ${replayed.length}`);
  }

  if (otherUserReplay.length !== 0) {
    throw new Error("Expected user-scoped replay to hide another user's run events");
  }

  const otherIdentity = createVerifiedTestIdentity(store, {
    email: "metadata-smoke-other@example.test",
  });
  const otherUserId = otherIdentity.userId;
  store.sessions.create({
    user_id: otherUserId,
    id: sessionId,
    title: "same session id in another user scope"
  });
  store.runs.create({
    user_id: otherUserId,
    id: runId,
    session_id: sessionId,
    request_fingerprint: "other-user-request-smoke",
    user_input: "same run id in another user scope",
    status: "running"
  });
  writer.write({
    user_id: otherUserId,
    run_id: runId,
    session_id: sessionId,
    event: {
      type: EventType.RUN_STARTED,
      threadId: sessionId,
      runId
    }
  });

  const otherUserOwnReplay = writer.replay({ user_id: otherUserId, run_id: runId });

  if (otherUserOwnReplay.length !== 1) {
    throw new Error("Expected identical session/run IDs to remain isolated by user");
  }

  if (replayed[0].event.type !== EventType.RUN_STARTED || replayed[1].event.type !== EventType.RUN_FINISHED) {
    throw new Error("Expected replayed events to preserve AG-UI event payloads");
  }

  console.log(
    `Metadata smoke OK: session=${sessionId}, run=${runId}, replayed=${replayed.length}, isolatedUsers=2`
  );
} finally {
  store.close();
}
