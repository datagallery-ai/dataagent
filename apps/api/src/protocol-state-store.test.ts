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
      const intent = metadata.sessionIntents.upsert({
        user_id: userId,
        session_id: "session-1",
        protocol_id: "general-task",
        protocol_version: "1",
        intent_text: "分析订单",
        source_run_id: "run-1"
      });
      metadata.sessionIntents.bindRun({
        user_id: userId,
        run_id: "run-1",
        session_id: "session-1",
        active_revision_id: intent.id,
        task_relation: "replace"
      });

      store.transitionSegment({
        current: { ...current, revision: 1, status: "handed_off" },
        expectedRevision: 0,
        next: createState("data-analysis", "run-1:segment:2", 0, "active"),
        intentTransition: {
          sessionId: "session-1",
          sourceRunId: "run-1",
          userInput: "分析订单",
          taskRelation: "replace",
          targetProtocolId: "data-analysis",
          targetProtocolVersion: "1"
        }
      });

      expect(store.get("run-1", "run-1:segment:1").status).toBe("handed_off");
      expect(store.get("run-1")).toMatchObject({
        protocolId: "data-analysis",
        segmentId: "run-1:segment:2"
      });
      expect(metadata.sessionIntents.find({ user_id: userId, session_id: "session-1" })).toMatchObject({
        protocol_id: "data-analysis",
        intent_id: intent.intent_id,
        previous_revision_id: intent.id,
        change_kind: "handoff"
      });

      metadata.runs.create({
        user_id: userId,
        id: "run-2",
        session_id: "session-1",
        user_input: "new concurrent task",
        status: "running"
      });
      const concurrentHead = metadata.sessionIntents.upsert({
        user_id: userId,
        session_id: "session-1",
        protocol_id: "general-task",
        protocol_version: "1",
        intent_text: "new concurrent task",
        source_run_id: "run-2"
      });
      const activeSegment = store.get("run-1", "run-1:segment:2");
      expect(() => store.transitionSegment({
        current: { ...activeSegment, revision: 1, status: "handed_off" },
        expectedRevision: 0,
        next: createState("general-task", "run-1:segment:3", 0, "active"),
        intentTransition: {
          sessionId: "session-1",
          sourceRunId: "run-1",
          userInput: "分析订单",
          taskRelation: "replace",
          targetProtocolId: "general-task",
          targetProtocolVersion: "1"
        }
      })).toThrow("SESSION_INTENT_REVISION_CONFLICT:session-1");
      expect(store.get("run-1", "run-1:segment:2")).toMatchObject({ revision: 0, status: "active" });
      expect(store.find("run-1", "run-1:segment:3")).toBeUndefined();
      expect(metadata.sessionIntents.find({ user_id: userId, session_id: "session-1" })?.id)
        .toBe(concurrentHead.id);
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
