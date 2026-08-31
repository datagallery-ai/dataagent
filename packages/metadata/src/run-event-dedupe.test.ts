import { EventType } from "@ag-ui/core";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import { createMetadataStore, createVerifiedTestIdentity, RunEventWriter } from "./index.js";

describe("RunEventWriter protocol event deduplication", () => {
  it("does not append a replayed protocol journal event twice", () => {
    const root = mkdtempSync(join(tmpdir(), "protocol-event-dedupe-"));
    try {
      const metadata = createMetadataStore({ database_path: join(root, "metadata.sqlite") });
      const { userId } = createVerifiedTestIdentity(metadata);
      metadata.sessions.create({ user_id: userId, id: "session-1", title: "Protocol" });
      metadata.runs.create({
        user_id: userId,
        id: "run-1",
        session_id: "session-1",
        user_input: "test",
        status: "running"
      });
      const writer = new RunEventWriter(metadata.runEvents);
      const event = {
        type: EventType.CUSTOM,
        name: "protocol.state.updated",
        value: { eventId: "protocol-event-1" }
      };

      const first = writer.write({ user_id: userId, run_id: "run-1", session_id: "session-1", event });
      const replay = writer.write({ user_id: userId, run_id: "run-1", session_id: "session-1", event });

      expect(replay.seq).toBe(first.seq);
      expect(writer.replay({ user_id: userId, run_id: "run-1" })).toHaveLength(1);
      metadata.close();
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
});
