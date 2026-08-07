import type { SessionIntent } from "@datafoundry/agent-runtime";
import type { MetadataStore } from "@datafoundry/metadata";

/** Route facts needed to decide whether this run redefines the session's task. */
export type SessionIntentRouteResult = {
  definition: { id: string; version: string };
  reasonCodes: string[];
  source: string;
};

/** Resolve the session's governing intent (following branch lineage) for routing. */
export const resolveSessionIntentForRun = (input: {
  metadataStore: MetadataStore;
  userId: string;
  sessionId: string;
}): SessionIntent | undefined => {
  const record = input.metadataStore.sessionIntents.resolveForSession({
    user_id: input.userId,
    session_id: input.sessionId
  });
  return record
    ? {
        protocolId: record.protocol_id,
        protocolVersion: record.protocol_version,
        intentText: record.intent_text
      }
    : undefined;
};

const INTENT_PRESERVING_REASONS = new Set(["SESSION_INTENT_INHERITED", "PROTOCOL_SEGMENT_RESTORED"]);
const INTENT_TEXT_MAX_CHARS = 2000;

/**
 * Persist the session intent when the route was resolved by a strong signal: an
 * explicit protocol selection, the keyword accelerator, or a confident classifier.
 * Inherited weak follow-ups, restored segments, and default-route fallbacks never
 * overwrite the recorded task — "再次尝试" must not become the session's intent.
 */
export const persistSessionIntentFromRoute = (input: {
  metadataStore: MetadataStore;
  userId: string;
  sessionId: string;
  runId: string;
  userInput: string;
  route: SessionIntentRouteResult;
}): boolean => {
  const strongSignal = input.route.source === "explicit"
    || input.route.source === "classifier"
    || (input.route.source === "deterministic"
      && !input.route.reasonCodes.some((code) => INTENT_PRESERVING_REASONS.has(code)));
  const intentText = input.userInput.trim();
  if (!strongSignal || !intentText) {
    return false;
  }
  input.metadataStore.sessionIntents.upsert({
    user_id: input.userId,
    session_id: input.sessionId,
    protocol_id: input.route.definition.id,
    protocol_version: input.route.definition.version,
    intent_text: intentText.slice(0, INTENT_TEXT_MAX_CHARS),
    source_run_id: input.runId
  });
  return true;
};
