import { describe, expect, it } from "vitest";

import { InMemoryProtocolStateStore } from "./in-memory-protocol-state-store.js";
import { ProtocolHandoffCoordinator } from "./protocol-handoff-coordinator.js";
import { ProtocolRegistry } from "./protocol-registry.js";
import { ProtocolRuntime } from "./protocol-runtime.js";
import type { AgentProtocolDefinition, ProtocolEvent } from "./types.js";

describe("ProtocolHandoffCoordinator", () => {
  it("atomically ends the current segment and starts the target segment", () => {
    const store = new InMemoryProtocolStateStore();
    const registry = new ProtocolRegistry();
    registry.register(createDefinition("general-task"));
    registry.register(createDefinition("data-analysis"));
    const currentRuntime = new ProtocolRuntime(createDefinition("general-task"), store);
    const current = currentRuntime.start({
      runId: "run-1",
      segmentId: "run-1:segment:1",
      contextPackageRef: { packageId: "context-1", revision: 3 }
    });
    const events: ProtocolEvent[] = [];
    const coordinator = new ProtocolHandoffCoordinator(registry, store, {
      onEvent: (event) => events.push(event)
    });

    const result = coordinator.handoff({
      runId: "run-1",
      segmentId: current.segmentId,
      expectedRevision: current.revision,
      authorizedProtocolIds: ["general-task", "data-analysis"],
      target: { protocolId: "data-analysis", protocolVersion: "1" },
      reasonCodes: ["ANALYTIC_INTENT"]
    });

    expect(store.get("run-1", "run-1:segment:1")).toMatchObject({
      status: "handed_off",
      revision: 1
    });
    expect(result.next).toMatchObject({
      protocolId: "data-analysis",
      segmentId: "run-1:segment:2",
      status: "active",
      contextPackageRef: { packageId: "context-1", revision: 3 }
    });
    expect(store.get("run-1").segmentId).toBe("run-1:segment:2");
    expect(events.map((event) => event.type)).toEqual([
      "protocol.handoff.proposed",
      "protocol.segment.ended",
      "protocol.handoff.accepted",
      "protocol.segment.started"
    ]);
  });

  it("derives unresolved strict goals from protocol state instead of trusting the caller", () => {
    const store = new InMemoryProtocolStateStore();
    const registry = new ProtocolRegistry();
    registry.register(createDefinition("general-task"));
    registry.register(createDefinition("data-analysis"));
    const current = new ProtocolRuntime(createDefinition("data-analysis"), store).start({
      runId: "run-strict",
      segmentId: "run-strict:segment:1",
      contextPackageRef: { packageId: "context-strict", revision: 0 }
    });
    const coordinator = new ProtocolHandoffCoordinator(registry, store);

    expect(() => coordinator.handoff({
      runId: "run-strict",
      segmentId: current.segmentId,
      expectedRevision: current.revision,
      authorizedProtocolIds: ["general-task", "data-analysis"],
      target: { protocolId: "general-task", protocolVersion: "1" },
      reasonCodes: ["MODEL_REQUESTED"]
    })).toThrow("PROTOCOL_HANDOFF_UNRESOLVED_STRICT_GOALS");
    expect(store.get("run-strict", current.segmentId)).toMatchObject({ status: "active", revision: 1 });
    expect(store.pendingEvents("run-strict").map((event) => event.type)).toEqual([
      "protocol.run.started",
      "protocol.phase.entered",
      "protocol.handoff.proposed",
      "protocol.handoff.rejected"
    ]);
  });

  it("rejects a handoff to the active protocol without replacing its segment state", () => {
    const store = new InMemoryProtocolStateStore();
    const registry = new ProtocolRegistry();
    registry.register(createDefinition("data-analysis"));
    const current = new ProtocolRuntime(createDefinition("data-analysis"), store).start({
      runId: "run-same-protocol",
      segmentId: "run-same-protocol:segment:1",
      contextPackageRef: { packageId: "context-same-protocol", revision: 4 }
    });
    const coordinator = new ProtocolHandoffCoordinator(registry, store);

    expect(() => coordinator.handoff({
      runId: current.runId,
      segmentId: current.segmentId,
      expectedRevision: current.revision,
      authorizedProtocolIds: ["general-task", "data-analysis"],
      target: { protocolId: "data-analysis", protocolVersion: "1" },
      reasonCodes: ["MODEL_REQUESTED"]
    })).toThrow("PROTOCOL_HANDOFF_SAME_PROTOCOL");

    expect(store.get("run-same-protocol")).toMatchObject({
      protocolId: "data-analysis",
      protocolVersion: "1",
      segmentId: current.segmentId,
      revision: current.revision + 1,
      phase: current.phase,
      status: "active",
      contextPackageRef: current.contextPackageRef,
      actions: current.actions,
      completionRejections: current.completionRejections,
      domain: current.domain
    });
    expect(store.pendingEvents("run-same-protocol").map((event) => event.type)).toEqual([
      "protocol.run.started",
      "protocol.phase.entered",
      "protocol.handoff.proposed",
      "protocol.handoff.rejected"
    ]);
  });

  it("aborts an untouched misrouted strict segment during an authoritative route correction", () => {
    const store = new InMemoryProtocolStateStore();
    const registry = new ProtocolRegistry();
    registry.register(createDefinition("general-task"));
    registry.register(createDefinition("data-analysis"));
    const current = new ProtocolRuntime(createDefinition("data-analysis"), store).start({
      runId: "run-correction",
      segmentId: "run-correction:segment:1",
      contextPackageRef: { packageId: "context-correction", revision: 0 }
    });

    const result = new ProtocolHandoffCoordinator(registry, store).handoff({
      runId: current.runId,
      segmentId: current.segmentId,
      expectedRevision: current.revision,
      authorizedProtocolIds: ["general-task", "data-analysis"],
      target: { protocolId: "general-task", protocolVersion: "1" },
      reasonCodes: ["CLASSIFIER_ROUTE_CORRECTION"],
      transitionKind: "route-correction"
    });

    expect(result.current.status).toBe("aborted");
    expect(result.next).toMatchObject({ protocolId: "general-task", status: "active" });
    expect(store.pendingEvents("run-correction").map((event) => event.type)).toContain(
      "protocol.handoff.proposed"
    );
  });
});

const createDefinition = (id: string): AgentProtocolDefinition<Record<string, never>> => ({
  id,
  version: "1",
  initialPhase: "work",
  phases: { work: { allowedActions: [], transitions: [] } },
  createInitialState: () => ({}),
  completionPolicy: () => ({ status: "continue", reasons: ["WORK_REMAINS"], allowedActions: [] })
});
