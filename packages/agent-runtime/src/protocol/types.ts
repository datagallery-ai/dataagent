export type ContextPackageRef = {
  packageId: string;
  revision: number;
  eventId?: string;
};

export type ProtocolCompletionDecision =
  | { status: "completed"; evaluatedContextPackageRef: ContextPackageRef; evidenceRefs: string[] }
  | {
      status: "degraded";
      evaluatedContextPackageRef: ContextPackageRef;
      reasons: string[];
      evidenceRefs: string[];
    }
  | {
      status: "partial";
      evaluatedContextPackageRef: ContextPackageRef;
      missing: string[];
      evidenceRefs: string[];
    }
  | { status: "continue"; reasons: string[]; allowedActions: string[] }
  | { status: "failed"; reasons: string[] };

export type ProtocolGuardResult =
  | { allowed: true }
  | { allowed: false; reasonCode: string; message?: string };

export type ProtocolGuard<TState> = (input: {
  actionInput: unknown;
  actionName: string;
  state: TState;
}) => ProtocolGuardResult;

export type ProtocolTransition<TState> = {
  targetPhase: string;
  when(input: { actionName: string; state: TState }): boolean;
};

export type ProtocolPhaseDefinition<TState> = {
  allowedActions: string[];
  actionGuards?: Record<string, ProtocolGuard<TState>[]>;
  transitions: ProtocolTransition<TState>[];
};

export type AgentProtocolDefinition<TState = unknown> = {
  id: string;
  version: string;
  initialPhase: string;
  phases: Record<string, ProtocolPhaseDefinition<TState>>;
  createInitialState(input: { contextPackageRef: ContextPackageRef; runId: string }): TState;
  completionPolicy(input: {
    contextPackageRef: ContextPackageRef;
    state: TState;
  }): ProtocolCompletionDecision;
};

export type ProtocolActionRecord = {
  actionId: string;
  actionName: string;
  status: "requested" | "rejected" | "succeeded" | "failed";
  inputContextPackageRef: ContextPackageRef;
  outputContextPackageRef?: ContextPackageRef;
  reasonCode?: string;
};

export type ProtocolRunState<TDomainState = unknown> = {
  protocolId: string;
  protocolVersion: string;
  runId: string;
  segmentId: string;
  revision: number;
  phase: string;
  status: "active" | "waiting" | "terminal" | "handed_off";
  contextPackageRef: ContextPackageRef;
  actions: ProtocolActionRecord[];
  completionRejections: number;
  domain: TDomainState;
  terminalDecision?: ProtocolCompletionDecision;
};

export type ProtocolEvent = {
  eventId: string;
  type: string;
  runId: string;
  segmentId: string;
  protocolId: string;
  protocolVersion: string;
  revision: number;
  payload?: unknown;
};

export type ProtocolIntentTransition = {
  sessionId: string;
  sourceRunId: string;
  userInput: string;
  taskRelation: "continue" | "refine" | "replace" | "side-chat";
  targetProtocolId: string;
  targetProtocolVersion: string;
};

export interface ProtocolStateStore {
  create<TDomainState>(
    state: ProtocolRunState<TDomainState>,
    events?: ProtocolEvent[]
  ): ProtocolRunState<TDomainState>;
  find<TDomainState>(runId: string, segmentId?: string): ProtocolRunState<TDomainState> | undefined;
  get<TDomainState>(runId: string, segmentId?: string): ProtocolRunState<TDomainState>;
  compareAndSet<TDomainState>(
    state: ProtocolRunState<TDomainState>,
    expectedRevision: number,
    events?: ProtocolEvent[]
  ): ProtocolRunState<TDomainState>;
  transitionSegment<TCurrentDomainState, TNextDomainState>(input: {
    current: ProtocolRunState<TCurrentDomainState>;
    expectedRevision: number;
    next: ProtocolRunState<TNextDomainState>;
    events?: ProtocolEvent[];
    intentTransition?: ProtocolIntentTransition;
  }): {
    current: ProtocolRunState<TCurrentDomainState>;
    next: ProtocolRunState<TNextDomainState>;
  };
  acknowledgeEvent(event: ProtocolEvent): void;
  pendingEvents(runId: string): ProtocolEvent[];
}
