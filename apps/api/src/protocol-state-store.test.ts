import { describe, expect, it } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { createMetadataStore, createVerifiedTestIdentity } from "@datafoundry/metadata";
import { MetadataProtocolStateStore } from "./protocol-state-store.js";

describe("MetadataProtocolStateStore", () => {
  it("restores a persisted protocol segment", () => {
    const root = mkdtempSync(join(tmpdir(), "protocol-state-store-"));
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
      metadata.contextPackageSnapshots.create({
        user_id: userId,
        session_id: "session-1",
        run_id: "run-1",
        package_id: "context-1",
        revision: 0,
        payload: {}
      });
      const store = new MetadataProtocolStateStore(metadata, userId);
      const startedEvent = {
        eventId: "run-1:segment:1:0:protocol.run.started",
        type: "protocol.run.started",
        runId: "run-1",
        segmentId: "run-1:segment:1",
        protocolId: "general-task",
        protocolVersion: "1",
        revision: 0
      };
      store.create({
        protocolId: "general-task",
        protocolVersion: "1",
        runId: "run-1",
        segmentId: "run-1:segment:1",
        revision: 0,
        phase: "work",
        status: "active",
        contextPackageRef: { packageId: "context-1", revision: 0 },
        actions: [],
        completionRejections: 0,
        domain: {}
      }, [startedEvent]);

      const restored = new MetadataProtocolStateStore(metadata, userId)
        .get("run-1", "run-1:segment:1");
      expect(restored).toMatchObject({ protocolId: "general-task", revision: 0, phase: "work" });
      expect(store.pendingEvents("run-1")).toEqual([startedEvent]);
      store.acknowledgeEvent(startedEvent);
      expect(store.pendingEvents("run-1")).toEqual([]);
      metadata.close();
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  it("persists a handoff as one segment transition", () => {
    const root = mkdtempSync(join(tmpdir(), "protocol-state-handoff-"));
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
      metadata.contextPackageSnapshots.create({
        user_id: userId,
        session_id: "session-1",
        run_id: "run-1",
        package_id: "context-1",
        revision: 0,
        payload: {}
      });
      const store = new MetadataProtocolStateStore(metadata, userId);
      const current = store.create(createState("general-task", "run-1:segment:1", 0, "active"));

      store.transitionSegment({
        current: { ...current, revision: 1, status: "handed_off" },
        expectedRevision: 0,
        next: createState("data-analysis", "run-1:segment:2", 0, "active")
      });

      expect(store.get("run-1", "run-1:segment:1").status).toBe("handed_off");
      expect(store.get("run-1")).toMatchObject({
        protocolId: "data-analysis",
        segmentId: "run-1:segment:2"
      });
      metadata.close();
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });
});

const createState = (
  protocolId: string,
  segmentId: string,
  revision: number,
  status: "active" | "handed_off"
) => ({
  protocolId,
  protocolVersion: "1",
  runId: "run-1",
  segmentId,
  revision,
  phase: "work",
  status,
  contextPackageRef: { packageId: "context-1", revision: 0 },
  actions: [],
  completionRejections: 0,
  domain: {}
});
