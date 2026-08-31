import { describe, expect, it } from "vitest";

import { DataLinkSemanticProvider } from "./datalink-semantic-provider.js";
import { SemanticProviderError } from "./types.js";

const request = {
  userId: "user-1",
  workspaceId: "workspace-1",
  datasourceId: "orders-db",
  datasourceRevision: "schema-v1",
  query: "monthly revenue"
};

describe("DataLinkSemanticProvider", () => {
  it("uses DataLink explore and preserves inference metadata", async () => {
    const calls: unknown[] = [];
    const provider = new DataLinkSemanticProvider({
      callTool: async (name, args) => {
        calls.push({ name, args });
        return {
          snapshot_id: "graph-7",
          nodes: [{ id: "concept:revenue", source: "llm_inference", confidence: 0.72 }]
        };
      }
    });

    const result = await provider.resolve(request);

    expect(calls).toEqual([{
      name: "datalink_explore",
      args: { query: "monthly revenue", mask_credential: true }
    }]);
    expect(result).toMatchObject({
      snapshotId: "graph-7",
      trust: "inferred",
      capabilities: ["graph-explore"]
    });
  });

  it("marks authorization errors as non-fallback", async () => {
    const provider = new DataLinkSemanticProvider({
      callTool: async () => {
        throw new Error("HTTP 403 forbidden");
      }
    });

    await expect(provider.resolve(request)).rejects.toMatchObject({
      code: "DATALINK_NOT_AUTHORIZED",
      fallbackAllowed: false
    } satisfies Partial<SemanticProviderError>);
  });

  it("treats policy MCP error text as provider failure", async () => {
    const provider = new DataLinkSemanticProvider({
      callTool: async () => "Error executing tool datalink_explore: MCP_TIMEOUT"
    });

    await expect(provider.resolve(request)).rejects.toMatchObject({
      code: "DATALINK_UNAVAILABLE",
      fallbackAllowed: true
    } satisfies Partial<SemanticProviderError>);
  });

  it.each([
    "",
    "   ",
    "No results found for \"orders\". Try different keywords.",
    { nodes: [] },
    { content: "No results found for orders" }
  ])("treats empty semantic output as a fallback-allowed failure", async (value) => {
    const provider = new DataLinkSemanticProvider({ callTool: async () => value });

    await expect(provider.resolve(request)).rejects.toMatchObject({
      code: "DATALINK_EMPTY_RESULT",
      fallbackAllowed: true
    } satisfies Partial<SemanticProviderError>);
  });
});
