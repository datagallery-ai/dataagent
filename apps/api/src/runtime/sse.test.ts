import { EventType } from "@ag-ui/client";
import { describe, expect, it } from "vitest";

import { consumeSseBuffer, encodeAgUiSseEvent } from "./sse.js";

describe("AG-UI SSE frames", () => {
  it("round-trips one event across split chunks", () => {
    const event = { type: EventType.RUN_STARTED, runId: "run-1", timestamp: 1 };
    const encoded = encodeAgUiSseEvent(event);
    const first = consumeSseBuffer(encoded.slice(0, 12));
    expect(first.events).toEqual([]);
    const second = consumeSseBuffer(first.remainder + encoded.slice(12));
    expect(second.events).toEqual([event]);
  });
});
