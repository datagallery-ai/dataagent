import { validateProtocolDefinition } from "./definition-validator.js";
import type {
  AgentProtocolDefinition,
  ContextPackageRef,
  ProtocolEvent,
  ProtocolRunState,
  ProtocolStateStore
} from "./types.js";
import { AGENT_RUNTIME_LIMITS } from "../config/agent-runtime-limits.js";

export type ProtocolRuntimeOptions = {
  contextPackageExists?(reference: ContextPackageRef): boolean;
  deadlineMs?: number;
  maxActions?: number;
  maxCommitRetries?: number;
  maxCompletionRejections?: number;
  now?(): number;
  onEvent?(event: ProtocolEvent): unknown;
  startEvents?: Array<{ type: string; payload?: unknown }>;
};

export class ProtocolRuntime<TDomainState> {
  constructor(
    private readonly definition: AgentProtocolDefinition<TDomainState>,
    private readonly stateStore: ProtocolStateStore,
    private readonly options: ProtocolRuntimeOptions = {}
  ) {
    validateProtocolDefinition(definition);
  }

  start(input: {
    runId: string;
    segmentId: string;
    contextPackageRef: ContextPackageRef;
  }): ProtocolRunState<TDomainState> {
    this.assertContextPackageRef(input.contextPackageRef);
    const initialState: ProtocolRunState<TDomainState> = {
      protocolId: this.definition.id,
      protocolVersion: this.definition.version,
      runId: input.runId,
      segmentId: input.segmentId,
      revision: 0,
      phase: this.definition.initialPhase,
      status: "active",
      contextPackageRef: input.contextPackageRef,
      actions: [],
      completionRejections: 0,
      domain: this.definition.createInitialState(input)
    };
    const events = [
      ...(this.options.startEvents ?? []).map((event) =>
        this.createEvent(event.type, initialState, event.payload)),
      this.createEvent("protocol.run.started", initialState),
      this.createEvent("protocol.phase.entered", initialState, { phase: initialState.phase })
    ];
    const state = this.stateStore.create(initialState, events);
    events.forEach((event) => this.publish(event));
    return state;
  }

  assertActionAllowed(input: {
    runId: string;
    actionName: string;
    actionInput: unknown;
  }): ProtocolRunState<TDomainState> {
    const state = this.stateStore.get<TDomainState>(input.runId);
    this.assertActionAllowedInState(state, input.actionName, input.actionInput);
    return state;
  }

  private assertActionAllowedInState(
    state: ProtocolRunState<TDomainState>,
    actionName: string,
    actionInput: unknown
  ): void {
    const maxActions = this.options.maxActions ?? AGENT_RUNTIME_LIMITS.protocolDefaultMaxActions;
    if (state.actions.length >= maxActions) {
      throw new Error(`PROTOCOL_ACTION_BUDGET_EXHAUSTED:${maxActions}`);
    }
    const phase = this.definition.phases[state.phase];
    if (!phase?.allowedActions.includes(actionName)) {
      // Fourth segment lists the currently allowed actions so the rejection can steer
      // the model to a viable next step instead of leaving it to guess.
      throw new Error(
        `ACTION_NOT_ALLOWED_IN_PHASE:${state.phase}:${actionName}:${phase?.allowedActions.join(",") ?? ""}`
      );
    }
    for (const guard of phase.actionGuards?.[actionName] ?? []) {
      const result = guard({
        actionInput,
        actionName,
        state: state.domain
      });
      if (!result.allowed) {
        throw new Error(`PROTOCOL_GUARD_REJECTED:${result.reasonCode}:${actionName}`);
      }
    }
  }

  getState(runId: string, segmentId?: string): ProtocolRunState<TDomainState> {
    return this.stateStore.get<TDomainState>(runId, segmentId);
  }

  allowedActions(runId: string, segmentId?: string): string[] {
    const state = this.getState(runId, segmentId);
    return [...(this.definition.phases[state.phase]?.allowedActions ?? [])];
  }

  beginAction(input: {
    runId: string;
    segmentId: string;
    actionId: string;
    actionName: string;
    actionInput: unknown;
  }): ProtocolRunState<TDomainState> {
    return this.commitWithRetry("begin_action", input.runId, input.segmentId, (current) => {
      this.assertActionAllowedInState(current, input.actionName, input.actionInput);
      const next: ProtocolRunState<TDomainState> = {
        ...current,
        revision: current.revision + 1,
        actions: [...current.actions, {
          actionId: input.actionId,
          actionName: input.actionName,
          status: "requested",
          inputContextPackageRef: current.contextPackageRef
        }]
      };
      return {
        next,
        events: [
          this.createEvent("protocol.action.requested", next, {
            actionId: input.actionId,
            actionName: input.actionName
          }),
          this.createEvent("protocol.action.started", next, {
            actionId: input.actionId,
            actionName: input.actionName
          })
        ]
      };
    });
  }

  recordActionRejection(input: {
    runId: string;
    segmentId: string;
    actionId: string;
    actionName: string;
    reasonCode: string;
  }): ProtocolRunState<TDomainState> {
    return this.commitWithRetry("reject_action", input.runId, input.segmentId, (current) => {
      const next: ProtocolRunState<TDomainState> = {
        ...current,
        revision: current.revision + 1,
        actions: [...current.actions, {
          actionId: input.actionId,
          actionName: input.actionName,
          status: "rejected",
          inputContextPackageRef: current.contextPackageRef,
          reasonCode: input.reasonCode
        }]
      };
      return {
        next,
        events: [
          this.createEvent("protocol.action.requested", next, {
            actionId: input.actionId,
            actionName: input.actionName
          }),
          this.createEvent("protocol.action.rejected", next, {
            actionId: input.actionId,
            actionName: input.actionName,
            reasonCode: input.reasonCode
          })
        ]
      };
    });
  }

  restore(runId: string, segmentId: string): ProtocolRunState<TDomainState> {
    const state = this.stateStore.get<TDomainState>(runId, segmentId);
    if (state.protocolId !== this.definition.id || state.protocolVersion !== this.definition.version) {
      throw new Error(
        `PROTOCOL_RESTORE_DEFINITION_MISMATCH:${state.protocolId}@${state.protocolVersion}:`
        + `${this.definition.id}@${this.definition.version}`
      );
    }
    this.assertContextPackageRef(state.contextPackageRef);
    return state;
  }

  recordActionSuccess(input: {
    runId: string;
    segmentId: string;
    actionId: string;
    actionName: string;
    reduceDomain?(domain: TDomainState): TDomainState;
    eventResult?: unknown;
    outputContextPackageRef: ContextPackageRef;
  }): ProtocolRunState<TDomainState> {
    this.assertContextPackageRef(input.outputContextPackageRef);
    return this.commitWithRetry("complete_action", input.runId, input.segmentId, (current) => {
      const action = requireRequestedAction(current, input.actionId, input.actionName);
      const domain = input.reduceDomain?.(current.domain) ?? current.domain;
      const phase = this.definition.phases[current.phase];
      const transitions = phase?.allowedActions.includes(input.actionName)
        ? phase.transitions.filter((transition) => transition.when({ actionName: input.actionName, state: domain }))
        : [];
      if (transitions.length > 1) {
        throw new Error(`PROTOCOL_TRANSITION_AMBIGUOUS:${current.phase}:${input.actionName}`);
      }
      const next: ProtocolRunState<TDomainState> = {
        ...current,
        revision: current.revision + 1,
        phase: transitions[0]?.targetPhase ?? current.phase,
        contextPackageRef: latestContextPackageRef(current.contextPackageRef, input.outputContextPackageRef),
        actions: updateActionRecord(current.actions, input.actionId, {
          actionId: input.actionId,
          actionName: input.actionName,
          status: "succeeded",
          inputContextPackageRef: action.inputContextPackageRef,
          outputContextPackageRef: input.outputContextPackageRef
        }),
        domain
      };
      const events = [this.createEvent("protocol.action.succeeded", next, {
        actionId: input.actionId,
        actionName: input.actionName,
        ...(input.eventResult === undefined ? {} : { result: input.eventResult })
      }), this.createEvent("protocol.state.updated", next)];
      if (next.phase !== current.phase) {
        events.push(this.createEvent("protocol.phase.entered", next, { phase: next.phase }));
      }
      return { next, events };
    });
  }

  recordActionFailure(input: {
    runId: string;
    segmentId: string;
    actionId: string;
    actionName: string;
    reasonCode: string;
  }): ProtocolRunState<TDomainState> {
    return this.commitWithRetry("fail_action", input.runId, input.segmentId, (current) => {
      const action = requireRequestedAction(current, input.actionId, input.actionName);
      const next: ProtocolRunState<TDomainState> = {
        ...current,
        revision: current.revision + 1,
        actions: updateActionRecord(current.actions, input.actionId, {
          actionId: input.actionId,
          actionName: input.actionName,
          status: "failed",
          inputContextPackageRef: action.inputContextPackageRef,
          reasonCode: input.reasonCode
        })
      };
      return {
        next,
        events: [
          this.createEvent("protocol.action.failed", next, {
            actionId: input.actionId,
            actionName: input.actionName,
            reasonCode: input.reasonCode
          }),
          this.createEvent("protocol.state.updated", next)
        ]
      };
    });
  }

  proposeCompletion(input: {
    runId: string;
    segmentId: string;
    expectedRevision: number;
    forceTerminal?: boolean;
  }): ProtocolRunState<TDomainState> {
    const current = this.stateStore.get<TDomainState>(input.runId, input.segmentId);
    const decision = this.definition.completionPolicy({
      contextPackageRef: current.contextPackageRef,
      state: current.domain
    });
    const rejectionCount = current.completionRejections + (decision.status === "continue" ? 1 : 0);
    const budgetExhausted = decision.status === "continue"
      && rejectionCount >= (this.options.maxCompletionRejections
        ?? AGENT_RUNTIME_LIMITS.protocolMaxCompletionRejections);
    const timeBudgetExhausted = this.options.deadlineMs !== undefined
      && (this.options.now?.() ?? Date.now()) >= this.options.deadlineMs;
    const terminalDecision = timeBudgetExhausted
      ? {
          status: "partial" as const,
          evaluatedContextPackageRef: current.contextPackageRef,
          missing: ["TIME_BUDGET_EXHAUSTED"],
          evidenceRefs: []
        }
      : input.forceTerminal && decision.status === "continue"
      ? {
          status: "partial" as const,
          evaluatedContextPackageRef: current.contextPackageRef,
          missing: decision.reasons,
          evidenceRefs: []
        }
      : budgetExhausted
      ? {
          status: "partial" as const,
          evaluatedContextPackageRef: current.contextPackageRef,
          missing: decision.reasons,
          evidenceRefs: []
        }
      : decision;
    const next: ProtocolRunState<TDomainState> = terminalDecision.status === "continue"
      ? { ...current, revision: current.revision + 1, completionRejections: rejectionCount }
      : {
          ...current,
          revision: current.revision + 1,
          status: "terminal",
          completionRejections: rejectionCount,
          terminalDecision
        };
    const events = [this.createEvent("protocol.completion.proposed", next, { decision: terminalDecision })];
    if (terminalDecision.status === "continue") {
      events.push(this.createEvent("protocol.completion.rejected", next, { decision: terminalDecision }));
    } else {
      events.push(this.createEvent(`protocol.run.${terminalDecision.status}`, next, { decision: terminalDecision }));
    }
    const persisted = this.stateStore.compareAndSet(next, input.expectedRevision, events);
    events.forEach((event) => this.publish(event));
    return persisted;
  }

  /** Persist an explicit failed terminal decision when an external governance
   * invariant proves the run cannot honestly complete. Unlike forceTerminal,
   * this never rewrites an unmet completion decision into `partial`. */
  terminateFailure(input: {
    runId: string;
    segmentId: string;
    expectedRevision: number;
    reasons: string[];
  }): ProtocolRunState<TDomainState> {
    const current = this.stateStore.get<TDomainState>(input.runId, input.segmentId);
    if (current.status !== "active" && current.status !== "waiting") {
      throw new Error(`PROTOCOL_RUN_NOT_ACTIVE:${current.status}`);
    }
    const reasons = [...new Set(input.reasons.map((reason) => reason.trim()).filter(Boolean))];
    if (reasons.length === 0) {
      throw new Error("PROTOCOL_FAILURE_REASON_REQUIRED");
    }
    const terminalDecision = { status: "failed" as const, reasons };
    const next: ProtocolRunState<TDomainState> = {
      ...current,
      revision: current.revision + 1,
      status: "terminal",
      terminalDecision
    };
    const events = [
      this.createEvent("protocol.completion.proposed", next, { decision: terminalDecision }),
      this.createEvent("protocol.run.failed", next, { decision: terminalDecision })
    ];
    const persisted = this.stateStore.compareAndSet(next, input.expectedRevision, events);
    events.forEach((event) => this.publish(event));
    return persisted;
  }

  private commitWithRetry(
    operation: string,
    runId: string,
    segmentId: string,
    build: (current: ProtocolRunState<TDomainState>) => {
      next: ProtocolRunState<TDomainState>;
      events: ProtocolEvent[];
    }
  ): ProtocolRunState<TDomainState> {
    const maxAttempts = Math.max(1, (this.options.maxCommitRetries
      ?? AGENT_RUNTIME_LIMITS.protocolMaxCommitRetries) + 1);
    let lastConflict: unknown;
    for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
      const current = this.stateStore.get<TDomainState>(runId, segmentId);
      const { next, events } = build(current);
      try {
        const persisted = this.stateStore.compareAndSet(next, current.revision, events);
        events.forEach((event) => this.publish(event));
        return persisted;
      } catch (error) {
        if (!isProtocolRevisionConflict(error)) {
          throw error;
        }
        lastConflict = error;
      }
    }
    const detail = lastConflict instanceof Error ? lastConflict.message : "PROTOCOL_REVISION_CONFLICT";
    throw new Error(`PROTOCOL_COMMIT_CONTENTION:${operation}:${runId}:${segmentId}:${maxAttempts}:${detail}`);
  }

  private createEvent(
    type: string,
    state: ProtocolRunState<TDomainState>,
    payload?: unknown
  ): ProtocolEvent {
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
    const delivered = this.options.onEvent(event);
    if (delivered !== false) {
      this.stateStore.acknowledgeEvent(event);
    }
  }

  private assertContextPackageRef(reference: ContextPackageRef): void {
    if (!reference.packageId.trim() || !Number.isInteger(reference.revision) || reference.revision < 0) {
      throw new Error("PROTOCOL_CONTEXT_REF_INVALID");
    }
    if (this.options.contextPackageExists && !this.options.contextPackageExists(reference)) {
      throw new Error(`PROTOCOL_CONTEXT_REF_NOT_FOUND:${reference.packageId}@${reference.revision}`);
    }
  }
}

const updateActionRecord = (
  actions: ProtocolRunState["actions"],
  actionId: string,
  replacement: ProtocolRunState["actions"][number]
): ProtocolRunState["actions"] => {
  const index = actions.findIndex((action) => action.actionId === actionId);
  if (index < 0) {
    return [...actions, replacement];
  }
  return actions.map((action, actionIndex) => actionIndex === index ? replacement : action);
};

const requireRequestedAction = <TDomainState>(
  state: ProtocolRunState<TDomainState>,
  actionId: string,
  actionName: string
): ProtocolRunState["actions"][number] => {
  const action = state.actions.find((candidate) => candidate.actionId === actionId);
  if (!action) {
    throw new Error(`PROTOCOL_ACTION_NOT_STARTED:${actionId}:${actionName}`);
  }
  if (action.actionName !== actionName) {
    throw new Error(`PROTOCOL_ACTION_NAME_MISMATCH:${actionId}:${action.actionName}:${actionName}`);
  }
  if (action.status !== "requested") {
    throw new Error(`PROTOCOL_ACTION_ALREADY_SETTLED:${actionId}:${action.status}`);
  }
  return action;
};

const latestContextPackageRef = (
  current: ContextPackageRef,
  candidate: ContextPackageRef
): ContextPackageRef => {
  if (current.packageId !== candidate.packageId) {
    throw new Error(`PROTOCOL_CONTEXT_PACKAGE_MISMATCH:${current.packageId}:${candidate.packageId}`);
  }
  return current.revision >= candidate.revision ? current : candidate;
};

const isProtocolRevisionConflict = (error: unknown): boolean =>
  error instanceof Error && error.message.startsWith("PROTOCOL_REVISION_CONFLICT:");
