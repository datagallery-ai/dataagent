import { describe, expect, it } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { createMetadataStore, createVerifiedTestIdentity } from "@datafoundry/metadata";
import {
  ProtocolHandoffCoordinator,
  ProtocolRegistry,
  ProtocolRuntime,
  type AgentProtocolDefinition,
  type ProtocolEvent
} from "@datafoundry/agent-runtime";
import { MetadataProtocolStateStore } from "./protocol-state-store.js";
import { commitSessionIntentFromProtocolStartWithinTransaction } from "./session-intent.js";

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

  it("atomically creates initial protocol state, route journal, and intent binding", () => {
    const root = mkdtempSync(join(tmpdir(), "protocol-state-intent-start-"));
    try {
      const metadata = createMetadataStore({ database_path: join(root, "metadata.sqlite") });
      const { userId } = createVerifiedTestIdentity(metadata);
      metadata.sessions.create({ user_id: userId, id: "session-1", title: "Protocol" });
      metadata.runs.create({
        user_id: userId,
        id: "run-1",
        session_id: "session-1",
        user_input: "分析订单",
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
      const store = new MetadataProtocolStateStore(metadata, userId, {
        onCreateWithinTransaction: ({ state, events }) => {
          commitSessionIntentFromProtocolStartWithinTransaction({
            metadataStore: metadata,
            userId,
            sessionId: "session-1",
            runId: "run-1",
            userInput: "分析订单",
            state,
            events
          });
        }
      });
      const routeEvent = createRouteResolvedEvent("run-1", "data-analysis", "replace");

      store.create(createState("data-analysis", "run-1:segment:1", 0, "active"), [routeEvent]);

      expect(store.get("run-1")).toMatchObject({ protocolId: "data-analysis", status: "active" });
      expect(store.pendingEvents("run-1").map((event) => event.type)).toEqual(["protocol.route.resolved"]);
      expect(metadata.sessionIntents.findRunBinding({ user_id: userId, run_id: "run-1" }))
        .toMatchObject({ task_relation: "replace", active_revision_id: expect.any(String) });
      expect(metadata.sessionIntents.find({ user_id: userId, session_id: "session-1" }))
        .toMatchObject({ protocol_id: "data-analysis", intent_text: "分析订单" });
      metadata.close();
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  it("rolls back initial protocol state and journal when the real intent commit loses CAS", () => {
    const root = mkdtempSync(join(tmpdir(), "protocol-state-intent-cas-"));
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
      metadata.runs.create({
        user_id: userId,
        id: "run-concurrent",
        session_id: "session-1",
        user_input: "写 README",
        status: "running"
      });
      const concurrentHead = metadata.sessionIntents.upsert({
        user_id: userId,
        session_id: "session-1",
        protocol_id: "general-task",
        protocol_version: "1",
        intent_text: "写 README",
        source_run_id: "run-concurrent"
      });
      const store = new MetadataProtocolStateStore(metadata, userId, {
        onCreateWithinTransaction: ({ state, events }) => {
          commitSessionIntentFromProtocolStartWithinTransaction({
            metadataStore: metadata,
            userId,
            sessionId: "session-1",
            runId: "run-1",
            userInput: "分析订单",
            state,
            events
          });
        }
      });
      const event = createRouteResolvedEvent("run-1", "data-analysis", "replace");

      expect(() => store.create(
        createState("data-analysis", "run-1:segment:1", 0, "active"),
        [event]
      )).toThrow("SESSION_INTENT_REVISION_CONFLICT:session-1");
      expect(store.find("run-1")).toBeUndefined();
      expect(store.pendingEvents("run-1")).toEqual([]);
      expect(metadata.sessionIntents.findRunBinding({ user_id: userId, run_id: "run-1" })).toBeUndefined();
      expect(metadata.sessionIntents.find({ user_id: userId, session_id: "session-1" })?.id)
        .toBe(concurrentHead.id);
      metadata.close();
    } finally {
      rmSync(root, { recursive: true, force: true });
    }
  });

  it("durably journals accepted and rejected handoff proposals", () => {
    const root = mkdtempSync(join(tmpdir(), "protocol-state-rejected-handoff-"));
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
      const registry = new ProtocolRegistry();
      registry.register(createProtocolDefinition("general-task"));
      registry.register(createProtocolDefinition("data-analysis"));
      const current = new ProtocolRuntime(createProtocolDefinition("data-analysis"), store).start({
        runId: "run-1",
        segmentId: "run-1:segment:1",
        contextPackageRef: { packageId: "context-1", revision: 0 }
      });

      expect(() => new ProtocolHandoffCoordinator(registry, store).handoff({
        runId: current.runId,
        segmentId: current.segmentId,
        expectedRevision: current.revision,
        authorizedProtocolIds: ["general-task", "data-analysis"],
        target: { protocolId: "general-task", protocolVersion: "1" },
        reasonCodes: ["MODEL_REQUESTED"]
      })).toThrow("PROTOCOL_HANDOFF_UNRESOLVED_STRICT_GOALS");

      const restored = new MetadataProtocolStateStore(metadata, userId);
      expect(restored.get("run-1")).toMatchObject({ status: "active", revision: 1 });
      expect(restored.pendingEvents("run-1").map((event) => event.type)).toContain(
        "protocol.handoff.proposed"
      );
      expect(restored.pendingEvents("run-1").map((event) => event.type)).toContain(
        "protocol.handoff.rejected"
      );

      metadata.runs.create({
        user_id: userId,
        id: "run-2",
        session_id: "session-1",
        user_input: "test accepted",
        status: "running"
      });
      metadata.contextPackageSnapshots.create({
        user_id: userId,
        session_id: "session-1",
        run_id: "run-2",
        package_id: "context-2",
        revision: 0,
        payload: {}
      });
      const acceptedCurrent = new ProtocolRuntime(createProtocolDefinition("general-task"), store).start({
        runId: "run-2",
        segmentId: "run-2:segment:1",
        contextPackageRef: { packageId: "context-2", revision: 0 }
      });
      new ProtocolHandoffCoordinator(registry, store).handoff({
        runId: acceptedCurrent.runId,
        segmentId: acceptedCurrent.segmentId,
        expectedRevision: acceptedCurrent.revision,
        authorizedProtocolIds: ["general-task", "data-analysis"],
        target: { protocolId: "data-analysis", protocolVersion: "1" },
        reasonCodes: ["ANALYTIC_INTENT"]
      });
      expect(restored.pendingEvents("run-2").map((event) => event.type)).toEqual([
        "protocol.run.started",
        "protocol.phase.entered",
        "protocol.handoff.proposed",
        "protocol.segment.ended",
        "protocol.handoff.accepted",
        "protocol.segment.started"
      ]);
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

const createProtocolDefinition = (
  id: string
): AgentProtocolDefinition<Record<string, never>> => ({
  id,
  version: "1",
  initialPhase: "work",
  phases: { work: { allowedActions: [], transitions: [] } },
  createInitialState: () => ({}),
  completionPolicy: () => ({ status: "continue", reasons: ["WORK_REMAINS"], allowedActions: [] })
});

const createRouteResolvedEvent = (
  runId: string,
  protocolId: string,
  taskRelation: "continue" | "refine" | "replace" | "side-chat"
): ProtocolEvent => ({
  eventId: `${runId}:segment:1:0:protocol.route.resolved`,
  type: "protocol.route.resolved",
  runId,
  segmentId: `${runId}:segment:1`,
  protocolId,
  protocolVersion: "1",
  revision: 0,
  payload: {
    protocolId,
    protocolVersion: "1",
    reasonCodes: ["TEST_ROUTE"],
    source: "classifier",
    taskRelation,
    warnings: []
  }
});
