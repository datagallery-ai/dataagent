import { describe, expect, it, vi } from "vitest";
import { z } from "zod";

import { InMemoryProtocolStateStore } from "../protocol/in-memory-protocol-state-store.js";
import { ProtocolRuntime } from "../protocol/protocol-runtime.js";
import type {
  AgentProtocolDefinition,
  ContextPackageRef,
  ProtocolEvent,
  ProtocolRunState
} from "../protocol/types.js";
import { ActionRouter } from "./action-router.js";
import { CapabilityRegistry } from "./capability-registry.js";

const contextPackageRef: ContextPackageRef = { packageId: "context-1", revision: 0 };

describe("ActionRouter", () => {
  it("denies execution when server policy rejects the action", async () => {
    let executed = false;
    const registry = createRegistry(async () => {
      executed = true;
      return { value: 1 };
    });
    const runtime = createRuntime();
    const router = new ActionRouter(registry, runtime, {
      serverPolicy: () => ({ allowed: false, reasonCode: "SERVER_POLICY_DENIED" }),
      projectContext: () => ({ packageId: "context-1", revision: 1 })
    });

    await expect(router.execute({
      runId: "run-1",
      segmentId: "segment-1",
      actionId: "action-1",
      actionName: "test.calculate",
      input: { value: 1 }
    })).rejects.toThrow("ACTION_REJECTED:SERVER_POLICY_DENIED:test.calculate");
    expect(executed).toBe(false);
    expect(runtime.getState("run-1", "segment-1").actions).toMatchObject([{
      actionName: "test.calculate",
      status: "rejected",
      reasonCode: "SERVER_POLICY_DENIED"
    }]);
  });

  it("validates, executes, reduces state, and records the output context", async () => {
    const registry = createRegistry(async () => ({ value: 2 }));
    const runtime = createRuntime();
    const router = new ActionRouter(registry, runtime, {
      serverPolicy: () => ({ allowed: true }),
      projectContext: () => ({ packageId: "context-1", revision: 1 })
    });

    const result = await router.execute({
      runId: "run-1",
      segmentId: "segment-1",
      actionId: "action-1",
      actionName: "test.calculate",
      input: { value: 2 }
    });

    expect(result).toMatchObject({
      rawResult: { value: 2 },
      observation: { value: 2 },
      contextPackageRef: { packageId: "context-1", revision: 1 }
    });
    expect(runtime.getState("run-1", "segment-1")).toMatchObject({
      revision: 2,
      domain: { total: 2 }
    });
  });

  it("returns the recorded result for a repeated idempotency key", async () => {
    let executions = 0;
    const registry = createRegistry(async () => {
      executions += 1;
      return { value: 2 };
    });
    const runtime = createRuntime();
    const router = new ActionRouter(registry, runtime, {
      serverPolicy: () => ({ allowed: true }),
      projectContext: () => ({ packageId: "context-1", revision: 1 })
    });
    const input = {
      runId: "run-1",
      segmentId: "segment-1",
      actionId: "action-1",
      actionName: "test.calculate",
      input: { value: 2 },
      idempotencyKey: "same-calculation"
    };

    const first = await router.execute(input);
    const second = await router.execute({ ...input, actionId: "action-2" });

    expect(second).toEqual(first);
    expect(executions).toBe(1);
    expect(runtime.getState("run-1", "segment-1").actions).toHaveLength(1);
  });

  it("returns the governed context package and observation projection", async () => {
    const router = new ActionRouter(createRegistry(async () => ({ value: 2 })), createRuntime(), {
      serverPolicy: () => ({ allowed: true }),
      projectContext: () => ({
        contextPackageRef: { packageId: "context-1", revision: 1 },
        contextPackage: { packageId: "context-1", revision: 1, items: [] },
        observation: { summary: "calculated" }
      })
    });

    const result = await router.execute({
      runId: "run-1",
      segmentId: "segment-1",
      actionId: "action-1",
      actionName: "test.calculate",
      input: { value: 2 }
    });

    expect(result.observation).toEqual({ summary: "calculated" });
    expect(result.contextPackage).toMatchObject({ packageId: "context-1", revision: 1 });
  });

  it("executes runtime automatic actions after a successful primary action", async () => {
    let revision = 0;
    const runtime = createRuntime();
    const router = new ActionRouter(createRegistry(async () => ({ value: 2 })), runtime, {
      serverPolicy: () => ({ allowed: true }),
      projectContext: () => ({ packageId: "context-1", revision: ++revision }),
      automaticActions: ({ actionName }) => actionName === "test.calculate"
        ? [{ actionName: "test.audit", input: { valid: true } }]
        : []
    });

    await router.execute({
      runId: "run-1",
      segmentId: "segment-1",
      actionId: "action-1",
      actionName: "test.calculate",
      input: { value: 2 }
    });

    expect(runtime.getState("run-1", "segment-1").actions.map((action) => action.actionName))
      .toEqual(["test.calculate", "test.audit"]);
  });

  it("projects the model-visible observation after automatic actions update protocol state", async () => {
    let revision = 0;
    const runtime = createRuntime();
    const router = new ActionRouter(createRegistry(async () => ({ value: 2 })), runtime, {
      serverPolicy: () => ({ allowed: true }),
      projectContext: () => ({ packageId: "context-1", revision: ++revision }),
      automaticActions: ({ actionName }) => actionName === "test.calculate"
        ? [{ actionName: "test.audit", input: { valid: true } }]
        : [],
      projectFinalObservation: ({ actionName, domain, observation }) => ({
        ...(observation as Record<string, unknown>),
        actionName,
        protocolTotal: (domain as { total: number }).total
      })
    });

    const result = await router.execute({
      runId: "run-1",
      segmentId: "segment-1",
      actionId: "action-1",
      actionName: "test.calculate",
      input: { value: 2 }
    });

    expect(result.observation).toEqual({ value: 2, actionName: "test.calculate", protocolTotal: 2 });
    expect(runtime.getState("run-1", "segment-1").actions.map((action) => action.actionName))
      .toEqual(["test.calculate", "test.audit"]);
  });

  it("executes runtime preparatory actions before checking the primary action phase", async () => {
    const registry = createRegistry(async () => ({ value: 2 }));
    const definition: AgentProtocolDefinition<{ total: number }> = {
      ...createProtocol(),
      initialPhase: "prepare",
      phases: {
        prepare: {
          allowedActions: ["test.audit"],
          transitions: [{ targetPhase: "active", when: ({ actionName }) => actionName === "test.audit" }]
        },
        active: { allowedActions: ["test.calculate"], transitions: [] }
      }
    };
    const runtime = new ProtocolRuntime(definition, new InMemoryProtocolStateStore());
    runtime.start({ runId: "run-1", segmentId: "segment-1", contextPackageRef });
    let revision = 0;
    const router = new ActionRouter(registry, runtime, {
      serverPolicy: () => ({ allowed: true }),
      projectContext: () => ({ packageId: "context-1", revision: ++revision }),
      preparatoryActions: ({ actionName }) => actionName === "test.calculate"
        ? [{ actionName: "test.audit", input: { valid: true } }]
        : []
    });

    await router.execute({
      runId: "run-1",
      segmentId: "segment-1",
      actionId: "action-1",
      actionName: "test.calculate",
      input: { value: 2 }
    });

    expect(runtime.getState("run-1", "segment-1").actions.map((action) => action.actionName))
      .toEqual(["test.audit", "test.calculate"]);
  });

  it("records a failed action when its executor throws", async () => {
    const runtime = createRuntime();
    const router = new ActionRouter(createRegistry(async () => {
      throw new Error("EXECUTOR_FAILED");
    }), runtime, {
      serverPolicy: () => ({ allowed: true }),
      projectContext: () => ({ packageId: "context-1", revision: 1 })
    });

    await expect(router.execute({
      runId: "run-1",
      segmentId: "segment-1",
      actionId: "action-failed",
      actionName: "test.calculate",
      input: { value: 2 }
    })).rejects.toThrow("EXECUTOR_FAILED");

    expect(runtime.getState("run-1", "segment-1").actions).toMatchObject([{
      actionId: "action-failed",
      actionName: "test.calculate",
      status: "failed",
      reasonCode: "EXECUTOR_FAILED"
    }]);
  });

  it("reports succeeded_uncommitted when external execution returns an invalid result", async () => {
    const runtime = createRuntime();
    const router = new ActionRouter(createRegistry(async () => ({ value: "invalid" })), runtime, {
      serverPolicy: () => ({ allowed: true }),
      projectContext: () => ({ packageId: "context-1", revision: 1 })
    });

    await expect(router.execute({
      runId: "run-1",
      segmentId: "segment-1",
      actionId: "action-invalid-result",
      actionName: "test.calculate",
      input: { value: 2 }
    })).rejects.toMatchObject({
      observation: {
        error: { executionStatus: "succeeded_uncommitted" },
        recovery: { strategy: "refresh_and_replan" }
      }
    });
    expect(runtime.getState("run-1", "segment-1").actions).toMatchObject([{
      actionId: "action-invalid-result",
      status: "failed"
    }]);
  });

  it("commits parallel action results without re-executing tools or losing domain updates", async () => {
    const releases = new Map<number, () => void>();
    const executions: number[] = [];
    const registry = createRegistry(async (input) => {
      const value = (input as { value: number }).value;
      executions.push(value);
      await new Promise<void>((resolve) => releases.set(value, resolve));
      return { value };
    });
    const runtime = createRuntime();
    let contextRevision = 0;
    const router = new ActionRouter(registry, runtime, {
      serverPolicy: () => ({ allowed: true }),
      projectContext: () => ({ packageId: "context-1", revision: ++contextRevision })
    });

    const first = router.execute({
      runId: "run-1",
      segmentId: "segment-1",
      actionId: "action-1",
      actionName: "test.calculate",
      input: { value: 1 }
    });
    const second = router.execute({
      runId: "run-1",
      segmentId: "segment-1",
      actionId: "action-2",
      actionName: "test.calculate",
      input: { value: 2 }
    });
    await vi.waitFor(() => expect(releases.size).toBe(2));

    releases.get(2)?.();
    await second;
    releases.get(1)?.();
    await first;

    expect(executions.sort()).toEqual([1, 2]);
    expect(runtime.getState("run-1", "segment-1")).toMatchObject({
      revision: 4,
      domain: { total: 3 },
      actions: [
        { actionId: "action-1", status: "succeeded" },
        { actionId: "action-2", status: "succeeded" }
      ]
    });
  });

  it("replays the reducer on the latest domain after a compare-and-set conflict", async () => {
    let executions = 0;
    const store = new ConflictOnceProtocolStateStore();
    const runtime = new ProtocolRuntime(createProtocol(), store);
    runtime.start({ runId: "run-1", segmentId: "segment-1", contextPackageRef });
    const router = new ActionRouter(createRegistry(async () => {
      executions += 1;
      return { value: 2 };
    }), runtime, {
      serverPolicy: () => ({ allowed: true }),
      projectContext: () => ({ packageId: "context-1", revision: 1 })
    });
    store.injectConflictOnNextCommit();

    await router.execute({
      runId: "run-1",
      segmentId: "segment-1",
      actionId: "action-1",
      actionName: "test.calculate",
      input: { value: 2 }
    });

    expect(executions).toBe(1);
    expect(runtime.getState("run-1", "segment-1")).toMatchObject({
      revision: 3,
      domain: { total: 12 },
      actions: [{ actionId: "action-1", status: "succeeded" }]
    });
  });
});

class ConflictOnceProtocolStateStore extends InMemoryProtocolStateStore {
  private shouldInjectConflict = false;

  injectConflictOnNextCommit(): void {
    this.shouldInjectConflict = true;
  }

  override compareAndSet<TDomainState>(
    state: ProtocolRunState<TDomainState>,
    expectedRevision: number,
    events: ProtocolEvent[] = []
  ): ProtocolRunState<TDomainState> {
    if (this.shouldInjectConflict && state.actions.some((action) => action.status === "succeeded")) {
      this.shouldInjectConflict = false;
      const current = this.get<{ total: number }>(state.runId, state.segmentId);
      super.compareAndSet({
        ...current,
        revision: current.revision + 1,
        domain: { total: current.domain.total + 10 }
      }, expectedRevision);
    }
    return super.compareAndSet(state, expectedRevision, events);
  }
}

const createRuntime = (): ProtocolRuntime<{ total: number }> => {
  const runtime = new ProtocolRuntime(createProtocol(), new InMemoryProtocolStateStore());
  runtime.start({ runId: "run-1", segmentId: "segment-1", contextPackageRef });
  return runtime;
};

const createProtocol = (): AgentProtocolDefinition<{ total: number }> => ({
  id: "test/protocol",
  version: "1",
  initialPhase: "active",
  phases: { active: { allowedActions: ["test.calculate", "test.audit"], transitions: [] } },
  createInitialState: () => ({ total: 0 }),
  completionPolicy: () => ({ status: "continue", reasons: ["not done"], allowedActions: [] })
});

const createRegistry = (execute: (input: unknown) => Promise<unknown>): CapabilityRegistry => {
  const registry = new CapabilityRegistry();
  registry.register({
    manifest: { id: "test-plugin", version: "1", provides: ["test.calculate", "test.audit"] },
    actions: [{
      name: "test.calculate",
      exposure: "agent",
      inputSchema: z.object({ value: z.number() }),
      outputSchema: z.object({ value: z.number() }),
      idempotency: "supported",
      execute: async (_context, input) => execute(input),
      reduce: (domain, result) => ({
        total: (domain as { total: number }).total + (result as { value: number }).value
      })
    }, {
      name: "test.audit",
      exposure: "runtime",
      inputSchema: z.object({ valid: z.boolean() }),
      outputSchema: z.object({ valid: z.boolean() }),
      idempotency: "supported",
      execute: async (_context, input) => input
    }]
  });
  return registry;
};
