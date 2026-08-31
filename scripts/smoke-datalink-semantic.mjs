import assert from "node:assert/strict";

import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

const url = process.env.DATALINK_MCP_URL ?? "http://1.95.190.111:8080/mcp";
const client = new Client({ name: "datafoundry-semantic-contract-smoke", version: "0.1.0" });

try {
  await client.connect(new StreamableHTTPClientTransport(new URL(url)));
  const listed = await client.listTools();
  const explore = listed.tools.find((tool) => tool.name === "datalink_explore");
  assert.ok(explore, "datalink_explore must be exposed");
  assert.deepEqual(explore.inputSchema.required, ["query"]);
  assert.equal(Object.hasOwn(explore.inputSchema.properties ?? {}, "dataset"), false);
  assert.equal(Object.hasOwn(explore.inputSchema.properties ?? {}, "mask_credential"), true);

  const result = await client.callTool({
    name: "datalink_explore",
    arguments: {
      query: "schema relationships",
      max_nodes: 2,
      focus: "schema",
      mask_credential: true
    }
  });
  assert.equal(result.isError, false);
  assert.ok(Array.isArray(result.content) && result.content.length > 0);
  console.log(`DataLink semantic contract smoke OK: ${listed.tools.length} tools at ${url}.`);
} finally {
  await client.close().catch(() => undefined);
}
