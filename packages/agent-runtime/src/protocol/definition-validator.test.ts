import { describe, expect, it } from "vitest";

import { validateProtocolDefinition } from "./definition-validator.js";

describe("validateProtocolDefinition", () => {
  it("rejects a missing initial phase", () => {
    expect(() => validateProtocolDefinition({
      id: "test/protocol",
      version: "1",
      initialPhase: "missing",
      phases: {
        active: {
          allowedActions: [],
          transitions: []
        }
      },
      createInitialState: () => ({}),
      completionPolicy: () => ({ status: "continue", reasons: ["not done"], allowedActions: [] })
    })).toThrow("PROTOCOL_INITIAL_PHASE_NOT_FOUND:test/protocol@1:missing");
  });

  it("rejects a transition to an unknown phase", () => {
    expect(() => validateProtocolDefinition({
      id: "test/protocol",
      version: "1",
      initialPhase: "active",
      phases: {
        active: {
          allowedActions: ["test.read"],
          transitions: [{ targetPhase: "missing", when: () => true }]
        }
      },
      createInitialState: () => ({}),
      completionPolicy: () => ({ status: "continue", reasons: ["not done"], allowedActions: [] })
    })).toThrow("PROTOCOL_TRANSITION_TARGET_NOT_FOUND:test/protocol@1:active:missing");
  });

  it("rejects a duplicate action within one phase", () => {
    expect(() => validateProtocolDefinition({
      id: "test/protocol",
      version: "1",
      initialPhase: "active",
      phases: {
        active: {
          allowedActions: ["test.read", "test.read"],
          transitions: []
        }
      },
      createInitialState: () => ({}),
      completionPolicy: () => ({ status: "continue", reasons: ["not done"], allowedActions: [] })
    })).toThrow("PROTOCOL_DUPLICATE_PHASE_ACTION:test/protocol@1:active:test.read");
  });

  it("accepts an explicitly cyclic protocol", () => {
    expect(() => validateProtocolDefinition({
      id: "test/cyclic",
      version: "1",
      initialPhase: "inspect",
      phases: {
        inspect: {
          allowedActions: ["data.inspect"],
          transitions: [{ targetPhase: "query", when: () => true }]
        },
        query: {
          allowedActions: ["data.query"],
          transitions: [{ targetPhase: "inspect", when: () => true }]
        }
      },
      createInitialState: () => ({}),
      completionPolicy: () => ({ status: "continue", reasons: ["not done"], allowedActions: [] })
    })).not.toThrow();
  });
});
