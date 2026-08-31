import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { CopilotKitClient } from "./copilotkit-client.js";

describe("CopilotKitClient auth transport", () => {
  it("routes AG-UI POST through the injected fetchImpl only", async () => {
    const calls: string[] = [];
    const client = new CopilotKitClient({
      runtimeUrl: "http://127.0.0.1:8787/api/copilotkit",
      agent: "dataFoundry",
      fetchImpl: async (input, init) => {
        calls.push(`${String(init?.method ?? "GET")} ${String(input)}`);
        return new Response("event: RUN_FINISHED\ndata: {}\n\n", {
          status: 200,
          headers: { "content-type": "text/event-stream" },
        });
      },
    });

    const response = await (client as unknown as {
      postRunAgent: (input: unknown) => Promise<Response>;
    }).postRunAgent({
      threadId: "t1",
      runId: "r1",
      messages: [],
      tools: [],
      context: [],
      state: {},
      forwardedProps: {},
    });

    assert.equal(response.status, 200);
    assert.deepEqual(calls, ["POST http://127.0.0.1:8787/api/copilotkit"]);
  });
});
