import { evaluateProtocolHandoff } from "./protocol-handoff.js";
import type { ProtocolRegistry } from "./protocol-registry.js";
import type {
  ProtocolEvent,
  ProtocolRunState,
  ProtocolStateStore
} from "./types.js";
import type { ProtocolIdentity } from "./protocol-router.js";

export type ProtocolHandoffCoordinatorOptions = {
  onEvent?(event: ProtocolEvent): void;
};

export type CoordinateProtocolHandoffInput = {
  runId: string;
  segmentId: string;
  expectedRevision: number;
  authorizedProtocolIds: string[];
  target: ProtocolIdentity;
  reasonCodes: string[];
  unresolvedGoals: string[];
  intentTransition?: import("./types.js").ProtocolIntentTransition;
};

/** Validate a handoff and atomically replace the active protocol segment. */
export class ProtocolHandoffCoordinator {
  constructor(
    private readonly registry: ProtocolRegistry,
    private readonly store: ProtocolStateStore,
    private readonly options: ProtocolHandoffCoordinatorOptions = {}
  ) {}

  handoff(input: CoordinateProtocolHandoffInput): {
    current: ProtocolRunState;
    next: ProtocolRunState;
  } {
    const current = this.store.get(input.runId, input.segmentId);
    this.publish(this.createEvent("protocol.handoff.proposed", current, {
      target: input.target,
      reasonCodes: input.reasonCodes,
      unresolvedGoals: input.unresolvedGoals
    }));
    const targetDefinition = this.registry.find(input.target.protocolId, input.target.protocolVersion);
    if (!targetDefinition) {
      this.publish(this.createEvent(
        "protocol.handoff.rejected",
        current,
        { reasonCode: "PROTOCOL_HANDOFF_TARGET_UNAVAILABLE" }
      ));
      throw new Error("PROTOCOL_HANDOFF_TARGET_UNAVAILABLE");
    }
    const decision = evaluateProtocolHandoff({
      authorizedProtocolIds: input.authorizedProtocolIds,
      current: {
        protocolId: current.protocolId,
        protocolVersion: current.protocolVersion,
        segmentId: current.segmentId
      },
      target: input.target,
      reasonCodes: input.reasonCodes,
      unresolvedGoals: input.unresolvedGoals
    });
    if (decision.status === "rejected") {
      this.publish(this.createEvent("protocol.handoff.rejected", current, { reasonCode: decision.reasonCode }));
      throw new Error(decision.reasonCode);
    }
    if (current.status !== "active" && current.status !== "waiting") {
      throw new Error(`PROTOCOL_HANDOFF_SOURCE_NOT_ACTIVE:${current.status}`);
    }
    const ended: ProtocolRunState = {
      ...current,
      revision: current.revision + 1,
      status: "handed_off"
    };
    const next: ProtocolRunState = {
      protocolId: targetDefinition.id,
      protocolVersion: targetDefinition.version,
      runId: current.runId,
      segmentId: nextSegmentId(current.runId, current.segmentId),
      revision: 0,
      phase: targetDefinition.initialPhase,
      status: "active",
      contextPackageRef: current.contextPackageRef,
      actions: [],
      completionRejections: 0,
      domain: targetDefinition.createInitialState({
        contextPackageRef: current.contextPackageRef,
        runId: current.runId
      })
    };
    const events = [
      this.createEvent("protocol.segment.ended", ended, { status: "handed_off" }),
      this.createEvent("protocol.handoff.accepted", next, {
        previousSegmentId: current.segmentId,
        reasonCodes: decision.reasonCodes
      }),
      this.createEvent("protocol.segment.started", next, { phase: next.phase })
    ];
    const persisted = this.store.transitionSegment({
      current: ended,
      expectedRevision: input.expectedRevision,
      next,
      events,
      ...(input.intentTransition ? { intentTransition: input.intentTransition } : {})
    });
    events.forEach((event) => this.publish(event));
    return persisted;
  }

  private createEvent(type: string, state: ProtocolRunState, payload?: unknown): ProtocolEvent {
    return {
      eventId: `${state.segmentId}:${state.revision}:${type}`,
      type,
      runId: state.runId,
      segmentId: state.segmentId,
      protocolId: state.protocolId,
      protocolVersion: state.protocolVersion,
      revision: state.revision,
      ...(payload === undefined ? {} : { payload })
    };
  }

  private publish(event: ProtocolEvent): void {
    if (!this.options.onEvent) {
      return;
    }
    this.options.onEvent(event);
    this.store.acknowledgeEvent(event);
  }
}

const nextSegmentId = (runId: string, currentSegmentId: string): string => {
  const match = currentSegmentId.match(/:segment:(\d+)$/u);
  const nextIndex = match ? Number(match[1]) + 1 : 2;
  return `${runId}:segment:${nextIndex}`;
};
