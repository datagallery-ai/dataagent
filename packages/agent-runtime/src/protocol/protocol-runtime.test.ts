import { describe, expect, it } from "vitest";

import { InMemoryProtocolStateStore } from "./in-memory-protocol-state-store.js";
import { ProtocolRuntime } from "./protocol-runtime.js";
import type { AgentProtocolDefinition, ContextPackageRef } from "./types.js";

const contextPackageRef: ContextPackageRef = { packageId: "context-1", revision: 3 };

describe("ProtocolRuntime", () => {
  it("starts a run in the protocol initial phase with its ContextPackage reference", () => {
    const runtime = new ProtocolRuntime(createDefinition(), new InMemoryProtocolStateStore());

    const state = runtime.start({ runId: "run-1", segmentId: "segment-1", contextPackageRef });

    expect(state).toMatchObject({
      protocolId: "test/protocol",
      protocolVersion: "1",
      runId: "run-1",
      segmentId: "segment-1",
      revision: 0,
      phase: "inspect",
      status: "active",
      contextPackageRef,
      actions: [],
      domain: { inspected: false }
    });
  });

  it("rejects an action that is not allowed in the current phase", () => {
    const runtime = new ProtocolRuntime(createDefinition(), new InMemoryProtocolStateStore());
    runtime.start({ runId: "run-1", segmentId: "segment-1", contextPackageRef });

    expect(() => runtime.assertActionAllowed({
      runId: "run-1",
      actionName: "data.query",
      actionInput: {}
    })).toThrow("ACTION_NOT_ALLOWED_IN_PHASE:inspect:data.query");
  });

  it("carries the allowed actions of the current phase in the rejection message", () => {
    const runtime = new ProtocolRuntime(createDefinition(), new InMemoryProtocolStateStore());
    runtime.start({ runId: "run-1", segmentId: "segment-1", contextPackageRef });

    expect(() => runtime.assertActionAllowed({
      runId: "run-1",
      actionName: "data.query",
      actionInput: {}
    })).toThrow("ACTION_NOT_ALLOWED_IN_PHASE:inspect:data.query:data.inspect");
  });

  it("rejects an action when a phase guard denies it", () => {
    const definition = createDefinition();
    const inspectPhase = definition.phases.inspect;
    if (!inspectPhase) {
      throw new Error("TEST_PHASE_REQUIRED");
    }
    inspectPhase.actionGuards = {
      "data.inspect": [() => ({ allowed: false, reasonCode: "DATASOURCE_REQUIRED" })]
    };
    const runtime = new ProtocolRuntime(definition, new InMemoryProtocolStateStore());
    runtime.start({ runId: "run-1", segmentId: "segment-1", contextPackageRef });

    expect(() => runtime.assertActionAllowed({
      runId: "run-1",
      actionName: "data.inspect",
      actionInput: {}
    })).toThrow("PROTOCOL_GUARD_REJECTED:DATASOURCE_REQUIRED:data.inspect");
  });

  it("records a successful action and transitions using the reduced domain state", () => {
    const runtime = new ProtocolRuntime(createDefinition(), new InMemoryProtocolStateStore());
    runtime.start({ runId: "run-1", segmentId: "segment-1", contextPackageRef });
    const outputContextPackageRef = { packageId: "context-1", revision: 4 };
    beginInspect(runtime, "action-1");

    const state = runtime.recordActionSuccess({
      runId: "run-1",
      segmentId: "segment-1",
      actionId: "action-1",
      actionName: "data.inspect",
      reduceDomain: () => ({ inspected: true }),
      outputContextPackageRef
    });

    expect(state).toMatchObject({
      revision: 2,
      phase: "query",
      domain: { inspected: true },
      contextPackageRef: outputContextPackageRef,
      actions: [{
        actionId: "action-1",
        actionName: "data.inspect",
        status: "succeeded",
        inputContextPackageRef: contextPackageRef,
        outputContextPackageRef
      }]
    });
  });

  it("commits a late result admitted in an earlier phase without regressing the current phase", () => {
    const runtime = new ProtocolRuntime(createDefinition(), new InMemoryProtocolStateStore());
    runtime.start({ runId: "run-1", segmentId: "segment-1", contextPackageRef });
    beginInspect(runtime, "action-1");
    beginInspect(runtime, "action-2");
    runtime.recordActionSuccess({
      runId: "run-1",
      segmentId: "segment-1",
      actionId: "action-1",
      actionName: "data.inspect",
      reduceDomain: () => ({ inspected: true }),
      outputContextPackageRef: { packageId: "context-1", revision: 4 }
    });

    const state = runtime.recordActionSuccess({
      runId: "run-1",
      segmentId: "segment-1",
      actionId: "action-2",
      actionName: "data.inspect",
      reduceDomain: (domain) => domain,
      outputContextPackageRef: { packageId: "context-1", revision: 5 }
    });

    expect(state).toMatchObject({
      revision: 4,
      phase: "query",
      domain: { inspected: true },
      actions: [
        { actionId: "action-1", status: "succeeded" },
        { actionId: "action-2", status: "succeeded" }
      ]
    });
  });

  it("keeps the current ContextPackage reference when a result has the same revision", () => {
    const runtime = new ProtocolRuntime(createDefinition(), new InMemoryProtocolStateStore());
    const current = { ...contextPackageRef, eventId: "current-event" };
    runtime.start({ runId: "run-1", segmentId: "segment-1", contextPackageRef: current });
    beginInspect(runtime, "action-1");

    const state = runtime.recordActionSuccess({
      runId: "run-1",
      segmentId: "segment-1",
      actionId: "action-1",
      actionName: "data.inspect",
      outputContextPackageRef: { ...contextPackageRef, eventId: "candidate-event" }
    });

    expect(state.contextPackageRef).toEqual(current);
  });

  it("rejects a result from a different ContextPackage lineage", () => {
    const runtime = new ProtocolRuntime(createDefinition(), new InMemoryProtocolStateStore());
    runtime.start({ runId: "run-1", segmentId: "segment-1", contextPackageRef });
    beginInspect(runtime, "action-1");

    expect(() => runtime.recordActionSuccess({
      runId: "run-1",
      segmentId: "segment-1",
      actionId: "action-1",
      actionName: "data.inspect",
      outputContextPackageRef: { packageId: "context-stale", revision: contextPackageRef.revision }
    })).toThrow("PROTOCOL_CONTEXT_PACKAGE_MISMATCH:context-1:context-stale");
  });

  it("rejects a run whose ContextPackage reference is not resolvable", () => {
    const runtime = new ProtocolRuntime(createDefinition(), new InMemoryProtocolStateStore());

    expect(() => runtime.start({
      runId: "run-1",
      segmentId: "segment-1",
      contextPackageRef: { packageId: "", revision: -1 }
    })).toThrow("PROTOCOL_CONTEXT_REF_INVALID");
  });

  it("rejects a well-formed ContextPackage reference missing from durable storage", () => {
    const runtime = new ProtocolRuntime(createDefinition(), new InMemoryProtocolStateStore(), {
      contextPackageExists: () => false
    });

    expect(() => runtime.start({
      runId: "run-1",
      segmentId: "segment-1",
      contextPackageRef
    })).toThrow("PROTOCOL_CONTEXT_REF_NOT_FOUND:context-1@3");
  });

  it("records a terminal completion decision against the evaluated ContextPackage", () => {
    const runtime = new ProtocolRuntime(createDefinition(), new InMemoryProtocolStateStore());
    runtime.start({ runId: "run-1", segmentId: "segment-1", contextPackageRef });
    const outputContextPackageRef = { packageId: "context-1", revision: 4 };
    beginInspect(runtime, "action-1");
    runtime.recordActionSuccess({
      runId: "run-1",
      segmentId: "segment-1",
      actionId: "action-1",
      actionName: "data.inspect",
      reduceDomain: () => ({ inspected: true }),
      outputContextPackageRef
    });

    const state = runtime.proposeCompletion({
      runId: "run-1",
      segmentId: "segment-1",
      expectedRevision: 2
    });

    expect(state.status).toBe("terminal");
    expect(state.terminalDecision).toEqual({
      status: "completed",
      evaluatedContextPackageRef: outputContextPackageRef,
      evidenceRefs: []
    });
  });

  it("returns partial instead of completed when the completion rejection budget is exhausted", () => {
    const runtime = new ProtocolRuntime(createDefinition(), new InMemoryProtocolStateStore(), {
      maxCompletionRejections: 1
    });
    runtime.start({ runId: "run-1", segmentId: "segment-1", contextPackageRef });

    const state = runtime.proposeCompletion({
      runId: "run-1",
      segmentId: "segment-1",
      expectedRevision: 0
    });

    expect(state.status).toBe("terminal");
    expect(state.terminalDecision).toEqual({
      status: "partial",
      evaluatedContextPackageRef: contextPackageRef,
      missing: ["not done"],
      evidenceRefs: []
    });
  });

  it("emits ordered events for run start and initial phase entry", () => {
    const events: Array<{ type: string; revision: number }> = [];
    const runtime = new ProtocolRuntime(createDefinition(), new InMemoryProtocolStateStore(), {
      onEvent: (event) => events.push({ type: event.type, revision: event.revision })
    });

    runtime.start({ runId: "run-1", segmentId: "segment-1", contextPackageRef });

    expect(events).toEqual([
      { type: "protocol.run.started", revision: 0 },
      { type: "protocol.phase.entered", revision: 0 }
    ]);
  });

  it("emits action, transition, and completion events in causal order", () => {
    const events: string[] = [];
    const runtime = new ProtocolRuntime(createDefinition(), new InMemoryProtocolStateStore(), {
      onEvent: (event) => events.push(event.type)
    });
    runtime.start({ runId: "run-1", segmentId: "segment-1", contextPackageRef });
    events.length = 0;
    beginInspect(runtime, "action-1");
    runtime.recordActionSuccess({
      runId: "run-1",
      segmentId: "segment-1",
      actionId: "action-1",
      actionName: "data.inspect",
      reduceDomain: () => ({ inspected: true }),
      outputContextPackageRef: { packageId: "context-1", revision: 4 }
    });
    runtime.proposeCompletion({ runId: "run-1", segmentId: "segment-1", expectedRevision: 2 });

    expect(events).toEqual([
      "protocol.action.requested",
      "protocol.action.started",
      "protocol.action.succeeded",
      "protocol.state.updated",
      "protocol.phase.entered",
      "protocol.completion.proposed",
      "protocol.run.completed"
    ]);
  });

  it("rejects actions after the action budget is exhausted", () => {
    const runtime = new ProtocolRuntime(createDefinition(), new InMemoryProtocolStateStore(), { maxActions: 1 });
    runtime.start({ runId: "run-1", segmentId: "segment-1", contextPackageRef });
    beginInspect(runtime, "action-1");
    runtime.recordActionSuccess({
      runId: "run-1",
      segmentId: "segment-1",
      actionId: "action-1",
      actionName: "data.inspect",
      reduceDomain: () => ({ inspected: true }),
      outputContextPackageRef: { packageId: "context-1", revision: 4 }
    });

    expect(() => runtime.assertActionAllowed({
      runId: "run-1",
      actionName: "data.query",
      actionInput: {}
    })).toThrow("PROTOCOL_ACTION_BUDGET_EXHAUSTED:1");
  });

  it("returns partial when the time budget is exhausted", () => {
    const runtime = new ProtocolRuntime(createDefinition(), new InMemoryProtocolStateStore(), {
      deadlineMs: 1000,
      now: () => 1000
    });
    runtime.start({ runId: "run-1", segmentId: "segment-1", contextPackageRef });

    const state = runtime.proposeCompletion({
      runId: "run-1",
      segmentId: "segment-1",
      expectedRevision: 0
    });

    expect(state.terminalDecision).toMatchObject({
      status: "partial",
      missing: ["TIME_BUDGET_EXHAUSTED"]
    });
  });

  it("returns partial when the model ends with unmet completion requirements", () => {
    const runtime = new ProtocolRuntime(createDefinition(), new InMemoryProtocolStateStore());
    runtime.start({ runId: "run-1", segmentId: "segment-1", contextPackageRef });

    const state = runtime.proposeCompletion({
      runId: "run-1",
      segmentId: "segment-1",
      expectedRevision: 0,
      forceTerminal: true
    });

    expect(state.terminalDecision).toMatchObject({
      status: "partial",
      missing: ["not done"]
    });
  });
});

const createDefinition = (): AgentProtocolDefinition<{ inspected: boolean }> => ({
  id: "test/protocol",
  version: "1",
  initialPhase: "inspect",
  phases: {
    inspect: {
      allowedActions: ["data.inspect"],
      transitions: [{ targetPhase: "query", when: ({ state }) => state.inspected }]
    },
    query: { allowedActions: ["data.query"], transitions: [] }
  },
  createInitialState: () => ({ inspected: false }),
  completionPolicy: ({ contextPackageRef, state }) => state.inspected
    ? { status: "completed", evaluatedContextPackageRef: contextPackageRef, evidenceRefs: [] }
    : { status: "continue", reasons: ["not done"], allowedActions: ["data.inspect"] }
});

const beginInspect = (runtime: ProtocolRuntime<{ inspected: boolean }>, actionId: string): void => {
  runtime.beginAction({
    runId: "run-1",
    segmentId: "segment-1",
    actionId,
    actionName: "data.inspect",
    actionInput: {}
  });
};
