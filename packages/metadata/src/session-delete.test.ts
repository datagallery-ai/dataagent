import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { createMetadataStore } from "./index.js";

describe("SessionRepository.delete", () => {
  it("deletes persisted protocol state and journal events with the session", () => {
    const root = mkdtempSync(join(tmpdir(), "session-delete-protocol-"));
    const metadata = createMetadataStore({ database_path: join(root, "metadata.sqlite") });
    try {
      metadata.sessions.create({ user_id: "dev-user", id: "session-1", title: "Protocol" });
      metadata.runs.create({
        user_id: "dev-user",
        id: "run-1",
        session_id: "session-1",
        user_input: "test",
        status: "completed"
      });
      metadata.contextPackageSnapshots.create({
        user_id: "dev-user",
        session_id: "session-1",
        run_id: "run-1",
        package_id: "context-1",
        revision: 0,
        payload: {}
      });
      metadata.protocolStates.compareAndSetWithEvents({
        user_id: "dev-user",
        run_id: "run-1",
        segment_id: "segment-1",
        expected_revision: -1,
        state: {
          protocolId: "general-task",
          protocolVersion: "1",
          runId: "run-1",
          segmentId: "segment-1",
          revision: 0,
          phase: "work",
          status: "completed",
          contextPackageRef: { packageId: "context-1", revision: 0 },
          actions: [],
          completionRejections: 0,
          domain: {}
        }
      }, [{
        eventId: "event-1",
        type: "protocol.run.completed",
        runId: "run-1",
        segmentId: "segment-1",
        revision: 0
      }]);

      expect(metadata.sessions.delete({ user_id: "dev-user", session_id: "session-1" })).toEqual({
        deleted: true,
        deletedSessionIds: ["session-1"]
      });
      expect(metadata.sessions.list({ user_id: "dev-user" })).toHaveLength(0);
      expect(metadata.protocolStates.find({
        user_id: "dev-user",
        run_id: "run-1",
        segment_id: "segment-1"
      })).toBeUndefined();
      expect(metadata.protocolStates.pendingEvents({ user_id: "dev-user", run_id: "run-1" })).toEqual([]);
    } finally {
      metadata.close();
      rmSync(root, { recursive: true, force: true });
    }
  });
});
