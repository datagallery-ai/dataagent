import type { BaseEvent } from "@ag-ui/core";

import { createCustomEvent } from "../../../events.js";
import type { ContextProtocolEventSink } from "../context-protocol-event-sink.js";

export type AgUiContextEventEmitter = {
  emit(event: BaseEvent): void;
};

export const createAgUiContextEventSink = (emitter: AgUiContextEventEmitter): ContextProtocolEventSink => ({
  emitContextEvent: (name, value) => {
    emitter.emit(createCustomEvent(name, value));
  }
});
