import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { ConfigClient } from "./config-client.js";

describe("ConfigClient auth transport", () => {
  it("routes REST GET/POST through the injected fetchImpl only", async () => {
    const calls: Array<{ method: string; url: string }> = [];
    const client = new ConfigClient({
      baseUrl: "http://127.0.0.1:8787",
      fetchImpl: async (input, init) => {
        calls.push({
          method: String(init?.method ?? "GET"),
          url: String(input),
        });
        return new Response(
          JSON.stringify({
            success: true,
            data: {
              activeDatasourceId: "ds-1",
              enabledDatasourceIds: ["ds-1"],
              enabledKnowledgeIds: [],
              enabledMcpServerIds: [],
            },
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      },
    });

    await client.getRunDefaults();
    assert.equal(calls.length, 1);
    assert.equal(calls[0]?.method, "GET");
    assert.match(calls[0]?.url ?? "", /\/api\/v1\/run-defaults$/);
    assert.equal(calls[0]?.url.includes("cookie"), false);
  });
});
