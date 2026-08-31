import type {
  ProtocolEvent,
  ProtocolRunState,
  SessionIntent,
  TaskRelation
} from "@datafoundry/agent-runtime";
import type { MetadataStore } from "@datafoundry/metadata";

/** Route facts needed to decide whether this run redefines the session's task. */
export type SessionIntentRouteResult = {
  definition: { id: string; version: string };
  reasonCodes: string[];
  source: string;
  taskRelation: TaskRelation;
};

/** Resolve the session's governing intent (following branch lineage) for routing. */
export const resolveSessionIntentForRun = (input: {
  metadataStore: MetadataStore;
  userId: string;
  sessionId: string;
  runId?: string;
}): SessionIntent | undefined => {
  const repository = input.metadataStore.sessionIntents;
  const binding = input.runId
    ? repository.findRunBinding({ user_id: input.userId, run_id: input.runId })
    : undefined;
  const record = binding
    ? binding.active_revision_id
      ? repository.findRevision({ user_id: input.userId, revision_id: binding.active_revision_id })
      : undefined
    : repository.resolveForSession({
        user_id: input.userId,
        session_id: input.sessionId
      });
  return record
    ? {
        intentId: record.intent_id,
        revisionId: record.id,
        protocolId: record.protocol_id,
        protocolVersion: record.protocol_version,
        intentText: record.intent_text
      }
    : undefined;
};

const INTENT_TEXT_MAX_CHARS = 2000;

/**
 * Atomically bind this run to the resolved base intent and apply the route's explicit
 * task relationship. Continue/side-chat preserve the task; refine/replace create an
 * immutable revision before model execution begins.
 */
export const commitSessionIntentFromRoute = (input: {
  metadataStore: MetadataStore;
  userId: string;
  sessionId: string;
  runId: string;
  userInput: string;
  expectedBaseRevisionId?: string;
  route: SessionIntentRouteResult;
}): SessionIntent | undefined => {
  input.metadataStore.db.exec("BEGIN IMMEDIATE");
  try {
    const active = commitSessionIntentFromRouteWithinTransaction(input);
    input.metadataStore.db.exec("COMMIT");
    return active;
  } catch (error) {
    input.metadataStore.db.exec("ROLLBACK");
    throw error;
  }
};

/** Same operation as commitSessionIntentFromRoute, for callers that already own
 * the metadata transaction (notably initial protocol-state creation). */
export const commitSessionIntentFromRouteWithinTransaction = (input: {
  metadataStore: MetadataStore;
  userId: string;
  sessionId: string;
  runId: string;
  userInput: string;
  expectedBaseRevisionId?: string;
  route: SessionIntentRouteResult;
}): SessionIntent | undefined => {
  const repository = input.metadataStore.sessionIntents;
  const existingBinding = repository.findRunBinding({ user_id: input.userId, run_id: input.runId });
  const base = existingBinding
    ? existingBinding.active_revision_id
      ? repository.findRevision({ user_id: input.userId, revision_id: existingBinding.active_revision_id })
      : undefined
    : repository.resolveForSession({ user_id: input.userId, session_id: input.sessionId });
  if (base?.id !== input.expectedBaseRevisionId) {
    throw new Error(`SESSION_INTENT_REVISION_CONFLICT:${input.sessionId}`);
  }
  if (existingBinding && input.route.reasonCodes.includes("PROTOCOL_SEGMENT_RESTORED")) {
    return toSessionIntent(base);
  }
  const ownHeadRevisionId = repository.findHeadRevisionId({ user_id: input.userId, session_id: input.sessionId });
  const normalizedRelation = input.route.taskRelation === "refine" && !base
    ? "replace"
    : input.route.taskRelation;
  const protocolChanged = Boolean(base)
    && (base?.protocol_id !== input.route.definition.id
      || base.protocol_version !== input.route.definition.version);
  let active = base;
  if (normalizedRelation === "replace" || normalizedRelation === "refine"
    || (normalizedRelation === "continue" && protocolChanged)) {
    const currentText = input.userInput.trim();
    if (currentText) {
      const intentText = normalizedRelation === "refine" && base
        ? `${base.intent_text}\n(补充要求: ${currentText})`.slice(0, INTENT_TEXT_MAX_CHARS)
        : normalizedRelation === "continue" && base
          ? base.intent_text
          : currentText.slice(0, INTENT_TEXT_MAX_CHARS);
      active = repository.commitWithinTransaction({
        user_id: input.userId,
        session_id: input.sessionId,
        ...(ownHeadRevisionId ? { expected_head_revision_id: ownHeadRevisionId } : {}),
        ...(normalizedRelation !== "replace" && base ? { intent_id: base.intent_id } : {}),
        ...(base ? { previous_revision_id: base.id } : {}),
        protocol_id: input.route.definition.id,
        protocol_version: input.route.definition.version,
        intent_text: intentText,
        change_kind: normalizedRelation === "replace"
          ? (base ? "replace" : "initial")
          : normalizedRelation === "refine" ? "refine" : "route-switch",
        source_run_id: input.runId
      });
    }
  }
  repository.bindRun({
    user_id: input.userId,
    run_id: input.runId,
    session_id: input.sessionId,
    ...(base ? { base_revision_id: base.id } : {}),
    ...(active ? { active_revision_id: active.id } : {}),
    task_relation: normalizedRelation
  });
  return toSessionIntent(active);
};

/** Parse the authoritative resolved-route event and commit its intent while the
 * caller's initial protocol-state transaction is still open. */
export const commitSessionIntentFromProtocolStartWithinTransaction = (input: {
  metadataStore: MetadataStore;
  userId: string;
  sessionId: string;
  runId: string;
  userInput: string;
  expectedBaseRevisionId?: string;
  state: ProtocolRunState;
  events: ProtocolEvent[];
}): SessionIntent | undefined => {
  const routeEvent = input.events.find((event) => event.type === "protocol.route.resolved");
  const payload = routeEvent?.payload;
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("PROTOCOL_ROUTE_EVENT_REQUIRED");
  }
  const routePayload = payload as Record<string, unknown>;
  const taskRelation = routePayload.taskRelation;
  if (taskRelation !== "continue" && taskRelation !== "refine"
    && taskRelation !== "replace" && taskRelation !== "side-chat") {
    throw new Error("PROTOCOL_ROUTE_TASK_RELATION_INVALID");
  }
  return commitSessionIntentFromRouteWithinTransaction({
    metadataStore: input.metadataStore,
    userId: input.userId,
    sessionId: input.sessionId,
    runId: input.runId,
    userInput: input.userInput,
    ...(input.expectedBaseRevisionId !== undefined
      ? { expectedBaseRevisionId: input.expectedBaseRevisionId }
      : {}),
    route: {
      definition: { id: input.state.protocolId, version: input.state.protocolVersion },
      reasonCodes: Array.isArray(routePayload.reasonCodes)
        ? routePayload.reasonCodes.filter((value): value is string => typeof value === "string")
        : [],
      source: typeof routePayload.source === "string" ? routePayload.source : "unknown",
      taskRelation
    }
  });
};

const toSessionIntent = (
  record: ReturnType<MetadataStore["sessionIntents"]["findRevision"]>
): SessionIntent | undefined => record
  ? {
      intentId: record.intent_id,
      revisionId: record.id,
      protocolId: record.protocol_id,
      protocolVersion: record.protocol_version,
      intentText: record.intent_text
    }
  : undefined;
