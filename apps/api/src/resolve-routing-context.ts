import type { ProtocolRunState, RoutingContext } from "@datafoundry/agent-runtime";
import type { MetadataStore } from "@datafoundry/metadata";

import { MetadataProtocolStateStore } from "./protocol-state-store.js";

type ResolveRoutingContextInput = {
  metadataStore: MetadataStore;
  /** Current run id (excluded when looking up the previous run). */
  runId: string;
  selectedDatasourceId?: string;
  /** Skill IDs active this run, if any. */
  selectedSkillIds?: string[];
  sessionId: string;
  userId: string;
};

/**
 * Resolve the compact routing context for the protocol classifier from the
 * previous run in the same session. Lets short follow-ups such as "再次尝试"
 * ("try again") inherit the prior data-analysis intent instead of being routed
 * to general-task on the ambiguous text alone.
 *
 * Returns undefined when there is no prior run in the session (first turn),
 * so callers without history are unaffected.
 */
export const resolveRoutingContext = (input: ResolveRoutingContextInput): RoutingContext | undefined => {
  const previousRun = input.metadataStore.runs.findPreviousRunBySession({
    user_id: input.userId,
    session_id: input.sessionId,
    exclude_run_id: input.runId
  });
  if (!previousRun) {
    return undefined;
  }
  const protocolStateStore = new MetadataProtocolStateStore(input.metadataStore, input.userId);
  const previousProtocolState = protocolStateStore.find(previousRun.id);
  const previousQuery = previousRun.user_input.trim() ? previousRun.user_input : null;
  const previousProtocol = previousProtocolState
    ? buildPreviousProtocol(previousProtocolState)
    : null;
  const selectedSkillIds = input.selectedSkillIds?.length ? [...input.selectedSkillIds] : null;
  const selectedDatasourceId = input.selectedDatasourceId ?? null;
  if (!previousQuery && !previousProtocol && !selectedSkillIds && !selectedDatasourceId) {
    return undefined;
  }
  const routingContext: RoutingContext = {
    ...(previousQuery ? { previousQuery } : {}),
    ...(previousProtocol ? { previousProtocol } : {}),
    ...(selectedSkillIds ? { selectedSkillIds } : {}),
    ...(selectedDatasourceId ? { selectedDatasourceId } : {})
  };
  return routingContext;
};

const buildPreviousProtocol = (state: ProtocolRunState): {
  protocolId: string;
  protocolVersion: string;
  terminalStatus?: string;
} => {
  const terminalStatus = state.status === "terminal"
    ? state.terminalDecision?.status
    : state.status;
  return {
    protocolId: state.protocolId,
    protocolVersion: state.protocolVersion,
    ...(terminalStatus ? { terminalStatus } : {})
  };
};
