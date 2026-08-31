import { describe, expect, it } from "vitest";

import { ProtocolRegistry } from "./protocol-registry.js";
import { ProtocolRouter } from "./protocol-router.js";
import type { AgentProtocolDefinition } from "./types.js";

describe("ProtocolRouter", () => {
  it("rejects a run when no authorized protocol can be resolved", async () => {
    const router = new ProtocolRouter(createRegistry());

    await expect(router.route({ authorizedProtocolIds: [] }))
      .rejects.toThrow("PROTOCOL_NOT_RESOLVED");
  });

  it("rejects an unauthorized explicit protocol", async () => {
    const router = new ProtocolRouter(createRegistry());

    await expect(router.route({
      authorizedProtocolIds: ["general-task"],
      explicit: { protocolId: "data-analysis", protocolVersion: "1" }
    })).rejects.toThrow("PROTOCOL_NOT_AUTHORIZED:data-analysis@1");
  });

  it("honors an explicitly selected authorized protocol", async () => {
    const registry = createRegistry();
    const router = new ProtocolRouter(registry);

    const result = await router.route({
      authorizedProtocolIds: ["general-task", "data-analysis"],
      explicit: { protocolId: "data-analysis", protocolVersion: "1" }
    });

    expect(result.definition.id).toBe("data-analysis");
    expect(result.source).toBe("explicit");
  });

  it("selects the highest-priority deterministic route", async () => {
    const router = new ProtocolRouter(createRegistry());

    const result = await router.route({
      authorizedProtocolIds: ["general-task", "data-analysis"],
      deterministicCandidates: [
        { protocolId: "general-task", protocolVersion: "1", priority: 10, reasonCode: "DEFAULT_TASK", taskRelation: "replace" },
        { protocolId: "data-analysis", protocolVersion: "1", priority: 100, reasonCode: "ANALYTIC_INTENT", taskRelation: "replace" }
      ]
    });

    expect(result.definition.id).toBe("data-analysis");
    expect(result.source).toBe("deterministic");
    expect(result.reasonCodes).toEqual(["ANALYTIC_INTENT"]);
  });

  it("uses a constrained classifier when deterministic rules do not resolve", async () => {
    const router = new ProtocolRouter(createRegistry(), {
      classifier: async ({ candidates }) => {
        expect(candidates.map((candidate) => candidate.protocolId).sort())
          .toEqual(["data-analysis", "general-task"]);
        return {
          protocolId: "data-analysis",
          protocolVersion: "1",
          confidence: 0.92,
          reasonCodes: ["ANALYTIC_INTENT"],
          taskRelation: "replace"
        };
      }
    });

    const result = await router.route({
      authorizedProtocolIds: ["general-task", "data-analysis"],
      classificationInput: { userText: "按月分析销售额" }
    });

    expect(result.definition.id).toBe("data-analysis");
    expect(result.source).toBe("classifier");
  });

  it("uses the authorized general protocol when classifier confidence is low", async () => {
    const router = new ProtocolRouter(createRegistry(), {
      classifier: async () => ({
        protocolId: "data-analysis",
        protocolVersion: "1",
        confidence: 0.4,
        reasonCodes: ["WEAK_ANALYTIC_INTENT"],
        taskRelation: "refine"
      })
    });

    const result = await router.route({
      authorizedProtocolIds: ["general-task", "data-analysis"],
      classificationInput: { userText: "帮我看看" }
    });

    expect(result.definition.id).toBe("general-task");
    expect(result.source).toBe("default");
    expect(result.warnings).toEqual(["PROTOCOL_CLASSIFICATION_LOW_CONFIDENCE"]);
  });

  it("uses the authorized general protocol when classification fails transiently", async () => {
    const router = new ProtocolRouter(createRegistry(), {
      classifier: async () => {
        throw new Error("MODEL_TEMPORARILY_UNAVAILABLE");
      }
    });

    const result = await router.route({
      authorizedProtocolIds: ["general-task", "data-analysis"],
      classificationInput: { userText: "介绍一下这个项目" }
    });

    expect(result.definition.id).toBe("general-task");
    expect(result.warnings).toEqual(["PROTOCOL_CLASSIFICATION_FAILED"]);
  });

  it("rejects equally ranked deterministic routes without a classifier", async () => {
    const router = new ProtocolRouter(createRegistry());

    await expect(router.route({
      authorizedProtocolIds: ["general-task", "data-analysis"],
      deterministicCandidates: [
        { protocolId: "general-task", protocolVersion: "1", priority: 50, reasonCode: "SKILL_ROUTE", taskRelation: "replace" },
        { protocolId: "data-analysis", protocolVersion: "1", priority: 50, reasonCode: "TASK_ROUTE", taskRelation: "replace" }
      ]
    })).rejects.toThrow("PROTOCOL_AMBIGUOUS:data-analysis@1,general-task@1");
  });

  it("rejects duplicate protocol registrations", () => {
    const registry = new ProtocolRegistry();
    registry.register(createDefinition("general-task"));

    expect(() => registry.register(createDefinition("general-task")))
      .toThrow("PROTOCOL_ALREADY_REGISTERED:general-task@1");
  });
});

const createRegistry = (): ProtocolRegistry => {
  const registry = new ProtocolRegistry();
  registry.register(createDefinition("general-task"));
  registry.register(createDefinition("data-analysis"));
  return registry;
};

const createDefinition = (id: string): AgentProtocolDefinition => ({
  id,
  version: "1",
  initialPhase: "active",
  phases: { active: { allowedActions: [], transitions: [] } },
  createInitialState: () => ({}),
  completionPolicy: () => ({ status: "continue", reasons: ["not done"], allowedActions: [] })
});
