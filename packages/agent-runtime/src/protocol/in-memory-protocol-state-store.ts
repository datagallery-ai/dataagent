import type { ProtocolEvent, ProtocolRunState, ProtocolStateStore } from "./types.js";

export class InMemoryProtocolStateStore implements ProtocolStateStore {
  private readonly states = new Map<string, ProtocolRunState>();
  private readonly currentSegments = new Map<string, string>();
  private readonly events = new Map<string, ProtocolEvent>();

  create<TDomainState>(
    state: ProtocolRunState<TDomainState>,
    events: ProtocolEvent[] = []
  ): ProtocolRunState<TDomainState> {
    const key = protocolStateKey(state.runId, state.segmentId);
    if (this.states.has(key)) {
      throw new Error(`PROTOCOL_SEGMENT_ALREADY_STARTED:${state.runId}:${state.segmentId}`);
    }
    this.states.set(key, state as ProtocolRunState);
    this.currentSegments.set(state.runId, state.segmentId);
    this.recordEvents(events);
    return state;
  }

  get<TDomainState>(runId: string, segmentId?: string): ProtocolRunState<TDomainState> {
    const state = this.find<TDomainState>(runId, segmentId);
    if (!state) {
      throw new Error(`PROTOCOL_RUN_NOT_FOUND:${runId}`);
    }
    return state;
  }

  find<TDomainState>(runId: string, segmentId?: string): ProtocolRunState<TDomainState> | undefined {
    const resolvedSegmentId = segmentId ?? this.currentSegments.get(runId);
    const state = resolvedSegmentId ? this.states.get(protocolStateKey(runId, resolvedSegmentId)) : undefined;
    return state as ProtocolRunState<TDomainState> | undefined;
  }

  compareAndSet<TDomainState>(
    state: ProtocolRunState<TDomainState>,
    expectedRevision: number,
    events: ProtocolEvent[] = []
  ): ProtocolRunState<TDomainState> {
    const current = this.get<TDomainState>(state.runId, state.segmentId);
    if (current.revision !== expectedRevision) {
      throw new Error(
        `PROTOCOL_REVISION_CONFLICT:${state.runId}:${state.segmentId}:${expectedRevision}:${current.revision}`
      );
    }
    this.states.set(protocolStateKey(state.runId, state.segmentId), state as ProtocolRunState);
    this.recordEvents(events);
    return state;
  }

  transitionSegment<TCurrentDomainState, TNextDomainState>(input: {
    current: ProtocolRunState<TCurrentDomainState>;
    expectedRevision: number;
    next: ProtocolRunState<TNextDomainState>;
    events?: ProtocolEvent[];
  }): {
    current: ProtocolRunState<TCurrentDomainState>;
    next: ProtocolRunState<TNextDomainState>;
  } {
    const current = this.get<TCurrentDomainState>(input.current.runId, input.current.segmentId);
    if (current.revision !== input.expectedRevision) {
      throw new Error(
        `PROTOCOL_REVISION_CONFLICT:${current.runId}:${current.segmentId}:`
        + `${input.expectedRevision}:${current.revision}`
      );
    }
    const nextKey = protocolStateKey(input.next.runId, input.next.segmentId);
    if (this.states.has(nextKey)) {
      throw new Error(`PROTOCOL_SEGMENT_ALREADY_STARTED:${input.next.runId}:${input.next.segmentId}`);
    }
    this.states.set(protocolStateKey(input.current.runId, input.current.segmentId), input.current as ProtocolRunState);
    this.states.set(nextKey, input.next as ProtocolRunState);
    this.currentSegments.set(input.next.runId, input.next.segmentId);
    this.recordEvents(input.events ?? []);
    return input;
  }

  acknowledgeEvent(event: ProtocolEvent): void {
    this.events.delete(event.eventId);
  }

  pendingEvents(runId: string): ProtocolEvent[] {
    return [...this.events.values()].filter((event) => event.runId === runId);
  }

  private recordEvents(events: ProtocolEvent[]): void {
    for (const event of events) {
      this.events.set(event.eventId, event);
    }
  }
}

const protocolStateKey = (runId: string, segmentId: string): string => `${runId}:${segmentId}`;
