import type { BaseEvent } from "@ag-ui/client";
import { createCustomEvent, type ProtocolEvent } from "@datafoundry/agent-runtime";

type ProtocolEventJournal = {
  acknowledgeEvent(event: ProtocolEvent): void;
  pendingEvents(runId: string): ProtocolEvent[];
};

/** Deliver durable protocol journal entries that were not published before a previous process stopped. */
export const replayPendingProtocolEvents = (input: {
  runId: string;
  stateStore: ProtocolEventJournal;
  emit(event: BaseEvent): void;
}): void => {
  for (const event of input.stateStore.pendingEvents(input.runId)) {
    input.emit(createCustomEvent(event.type, event));
    input.stateStore.acknowledgeEvent(event);
  }
};
