import { describe, expect, it } from "vitest";

import { replayPendingProtocolEvents } from "./protocol-event-recovery.js";

describe("replayPendingProtocolEvents", () => {
  it("acknowledges a journal event only after delivery succeeds", () => {
    const event = {
      eventId: "event-1",
      type: "protocol.state.updated",
      runId: "run-1",
      segmentId: "run-1:segment:1",
      protocolId: "general-task",
      protocolVersion: "1",
      revision: 2
    };
    const delivered: unknown[] = [];
    const acknowledged: unknown[] = [];

    replayPendingProtocolEvents({
      runId: "run-1",
      stateStore: {
        pendingEvents: () => [event],
        acknowledgeEvent: (pending) => acknowledged.push(pending)
      },
      emit: (pending) => delivered.push(pending)
    });

    expect(delivered).toMatchObject([{
      type: "CUSTOM",
      name: "protocol.state.updated",
      value: event
    }]);
    expect(acknowledged).toEqual([event]);
  });
});
