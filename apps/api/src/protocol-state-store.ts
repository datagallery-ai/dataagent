import type {
  ProtocolEvent,
  ProtocolIntentTransition,
  ProtocolRunState,
  ProtocolStateStore
} from "@datafoundry/agent-runtime";
import type { MetadataStore, ProtocolStateSnapshotRecord } from "@datafoundry/metadata";

export type MetadataProtocolStateStoreOptions = {
  onCreateWithinTransaction?(input: {
    state: ProtocolRunState;
    events: ProtocolEvent[];
  }): void;
};

/** Persist Protocol Runtime snapshots through the user-scoped metadata repository. */
export class MetadataProtocolStateStore implements ProtocolStateStore {
  constructor(
    private readonly metadataStore: MetadataStore,
    private readonly userId: string,
    private readonly options: MetadataProtocolStateStoreOptions = {}
  ) {}

  create<TDomainState>(
    state: ProtocolRunState<TDomainState>,
    events: ProtocolEvent[] = []
  ): ProtocolRunState<TDomainState> {
    if (this.options.onCreateWithinTransaction) {
      this.metadataStore.db.exec("BEGIN IMMEDIATE");
      try {
        const record = this.metadataStore.protocolStates.compareAndSetWithEventsWithinTransaction({
          user_id: this.userId,
          run_id: state.runId,
          segment_id: state.segmentId,
          expected_revision: -1,
          state
        }, events);
        this.options.onCreateWithinTransaction({ state, events });
        this.metadataStore.db.exec("COMMIT");
        return parseProtocolState<TDomainState>(record);
      } catch (error) {
        this.metadataStore.db.exec("ROLLBACK");
        throw error;
      }
    }
    return this.persist(state, -1, events);
  }

  find<TDomainState>(runId: string, segmentId?: string): ProtocolRunState<TDomainState> | undefined {
    const record = segmentId
      ? this.metadataStore.protocolStates.find({
          user_id: this.userId,
          run_id: runId,
          segment_id: segmentId
        })
      : this.metadataStore.protocolStates.latestByRun({ user_id: this.userId, run_id: runId });
    return record ? parseProtocolState<TDomainState>(record) : undefined;
  }

  get<TDomainState>(runId: string, segmentId?: string): ProtocolRunState<TDomainState> {
    const state = this.find<TDomainState>(runId, segmentId);
    if (!state) {
      throw new Error(`PROTOCOL_RUN_NOT_FOUND:${runId}${segmentId ? `:${segmentId}` : ""}`);
    }
    return state;
  }

  compareAndSet<TDomainState>(
    state: ProtocolRunState<TDomainState>,
    expectedRevision: number,
    events: ProtocolEvent[] = []
  ): ProtocolRunState<TDomainState> {
    return this.persist(state, expectedRevision, events);
  }

  transitionSegment<TCurrentDomainState, TNextDomainState>(input: {
    current: ProtocolRunState<TCurrentDomainState>;
    expectedRevision: number;
    next: ProtocolRunState<TNextDomainState>;
    events?: ProtocolEvent[];
    intentTransition?: ProtocolIntentTransition;
  }): {
    current: ProtocolRunState<TCurrentDomainState>;
    next: ProtocolRunState<TNextDomainState>;
  } {
    const records = this.metadataStore.protocolStates.transitionSegment({
      user_id: this.userId,
      current: {
        run_id: input.current.runId,
        segment_id: input.current.segmentId,
        expected_revision: input.expectedRevision,
        state: input.current
      },
      next: {
        run_id: input.next.runId,
        segment_id: input.next.segmentId,
        expected_revision: -1,
        state: input.next
      },
      events: input.events ?? [],
      ...(input.intentTransition
        ? {
            intent_transition: {
              session_id: input.intentTransition.sessionId,
              source_run_id: input.intentTransition.sourceRunId,
              user_input: input.intentTransition.userInput,
              task_relation: input.intentTransition.taskRelation,
              target_protocol_id: input.intentTransition.targetProtocolId,
              target_protocol_version: input.intentTransition.targetProtocolVersion
            }
          }
        : {})
    });
    return {
      current: parseProtocolState<TCurrentDomainState>(records.current),
      next: parseProtocolState<TNextDomainState>(records.next)
    };
  }

  acknowledgeEvent(event: ProtocolEvent): void {
    this.metadataStore.protocolStates.acknowledgeEvent({
      user_id: this.userId,
      event_id: event.eventId
    });
  }

  pendingEvents(runId: string): ProtocolEvent[] {
    return this.metadataStore.protocolStates.pendingEvents({
      user_id: this.userId,
      run_id: runId
    }) as ProtocolEvent[];
  }

  private persist<TDomainState>(
    state: ProtocolRunState<TDomainState>,
    expectedRevision: number,
    events: ProtocolEvent[]
  ): ProtocolRunState<TDomainState> {
    const record = this.metadataStore.protocolStates.compareAndSetWithEvents({
      user_id: this.userId,
      run_id: state.runId,
      segment_id: state.segmentId,
      expected_revision: expectedRevision,
      state
    }, events);
    return parseProtocolState<TDomainState>(record);
  }
}

const parseProtocolState = <TDomainState>(
  record: ProtocolStateSnapshotRecord
): ProtocolRunState<TDomainState> => JSON.parse(record.state_json) as ProtocolRunState<TDomainState>;
