import type { ProtocolIdentity } from "./protocol-router.js";

export type ProtocolSegmentIdentity = ProtocolIdentity & {
  segmentId: string;
};

export type ProtocolHandoffKind = "continue" | "route-correction";

export type ProtocolHandoffInput = {
  authorizedProtocolIds: string[];
  current: ProtocolSegmentIdentity;
  target: ProtocolIdentity;
  reasonCodes: string[];
  strictProtocolIds?: string[];
  unresolvedGoals: string[];
  transitionKind?: ProtocolHandoffKind;
};

export type ProtocolHandoffDecision =
  | { status: "accepted"; reasonCodes: string[]; target: ProtocolIdentity }
  | { status: "rejected"; reasonCode: string };

/** Evaluate whether a proposed protocol handoff may create a new segment. */
export const evaluateProtocolHandoff = (input: ProtocolHandoffInput): ProtocolHandoffDecision => {
  if (input.current.protocolId === input.target.protocolId) {
    return { status: "rejected", reasonCode: "PROTOCOL_HANDOFF_SAME_PROTOCOL" };
  }
  if (!input.authorizedProtocolIds.includes(input.target.protocolId)) {
    return { status: "rejected", reasonCode: "PROTOCOL_HANDOFF_NOT_AUTHORIZED" };
  }
  const strictProtocolIds = new Set(input.strictProtocolIds ?? ["data-analysis"]);
  if (
    strictProtocolIds.has(input.current.protocolId)
    && !strictProtocolIds.has(input.target.protocolId)
    && input.unresolvedGoals.length > 0
    && input.transitionKind !== "route-correction"
  ) {
    return { status: "rejected", reasonCode: "PROTOCOL_HANDOFF_UNRESOLVED_STRICT_GOALS" };
  }
  return { status: "accepted", reasonCodes: input.reasonCodes, target: input.target };
};
