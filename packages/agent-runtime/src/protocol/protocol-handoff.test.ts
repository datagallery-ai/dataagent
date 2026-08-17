import { describe, expect, it } from "vitest";

import { evaluateProtocolHandoff } from "./protocol-handoff.js";

describe("evaluateProtocolHandoff", () => {
  it("rejects a handoff to the active protocol", () => {
    const decision = evaluateProtocolHandoff({
      authorizedProtocolIds: ["general-task", "data-analysis"],
      current: { protocolId: "data-analysis", protocolVersion: "1", segmentId: "segment-1" },
      target: { protocolId: "data-analysis", protocolVersion: "1" },
      reasonCodes: ["MODEL_REQUESTED"],
      unresolvedGoals: []
    });

    expect(decision).toEqual({
      status: "rejected",
      reasonCode: "PROTOCOL_HANDOFF_SAME_PROTOCOL"
    });
  });

  it("does not treat a version change as a protocol handoff", () => {
    const decision = evaluateProtocolHandoff({
      authorizedProtocolIds: ["data-analysis"],
      current: { protocolId: "data-analysis", protocolVersion: "1", segmentId: "segment-1" },
      target: { protocolId: "data-analysis", protocolVersion: "2" },
      reasonCodes: ["MODEL_REQUESTED"],
      unresolvedGoals: []
    });

    expect(decision).toEqual({
      status: "rejected",
      reasonCode: "PROTOCOL_HANDOFF_SAME_PROTOCOL"
    });
  });

  it("rejects a strict-to-general handoff while strict goals remain unresolved", () => {
    const decision = evaluateProtocolHandoff({
      authorizedProtocolIds: ["general-task", "data-analysis"],
      current: { protocolId: "data-analysis", protocolVersion: "1", segmentId: "segment-1" },
      target: { protocolId: "general-task", protocolVersion: "1" },
      reasonCodes: ["USER_CHANGED_TOPIC"],
      unresolvedGoals: ["validate generated SQL"]
    });

    expect(decision).toEqual({
      status: "rejected",
      reasonCode: "PROTOCOL_HANDOFF_UNRESOLVED_STRICT_GOALS"
    });
  });

  it("rejects a handoff to an unauthorized protocol", () => {
    const decision = evaluateProtocolHandoff({
      authorizedProtocolIds: ["general-task"],
      current: { protocolId: "general-task", protocolVersion: "1", segmentId: "segment-1" },
      target: { protocolId: "data-analysis", protocolVersion: "1" },
      reasonCodes: ["ANALYTIC_INTENT"],
      unresolvedGoals: []
    });

    expect(decision).toEqual({
      status: "rejected",
      reasonCode: "PROTOCOL_HANDOFF_NOT_AUTHORIZED"
    });
  });
});
