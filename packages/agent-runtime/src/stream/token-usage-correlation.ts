import { createCustomEvent } from "../events.js";
import type { AgUiEventEmitter } from "../types.js";

export type TokenUsageCorrelationPayload = {
  stepId: string;
  toolCallId: string;
  toolName: string;
};

/** Links ACTIVITY STEP step_id to AG-UI/Mastra tool_call_id for per-step token display. */
export const createTokenUsageCorrelationStore = () => ({
  emitCorrelation(emitter: AgUiEventEmitter, payload: TokenUsageCorrelationPayload): void {
    emitter.emit(
      createCustomEvent("token_usage.correlation", {
        step_id: payload.stepId,
        tool_call_id: payload.toolCallId,
        tool_name: payload.toolName,
      }),
    );
  },
});
