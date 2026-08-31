import type { AgentProtocolDefinition } from "./types.js";

/** Validate the structural invariants required to register a protocol definition. */
export const validateProtocolDefinition = <TState>(definition: AgentProtocolDefinition<TState>): void => {
  if (!Object.hasOwn(definition.phases, definition.initialPhase)) {
    throw new Error(
      `PROTOCOL_INITIAL_PHASE_NOT_FOUND:${definition.id}@${definition.version}:${definition.initialPhase}`
    );
  }
  for (const [phaseName, phase] of Object.entries(definition.phases)) {
    const allowedActions = new Set<string>();
    for (const actionName of phase.allowedActions) {
      if (allowedActions.has(actionName)) {
        throw new Error(
          `PROTOCOL_DUPLICATE_PHASE_ACTION:${definition.id}@${definition.version}:${phaseName}:${actionName}`
        );
      }
      allowedActions.add(actionName);
    }
    for (const transition of phase.transitions) {
      if (!Object.hasOwn(definition.phases, transition.targetPhase)) {
        throw new Error(
          `PROTOCOL_TRANSITION_TARGET_NOT_FOUND:${definition.id}@${definition.version}:${phaseName}:`
          + transition.targetPhase
        );
      }
    }
  }
};
