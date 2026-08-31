import { describe, expect, it } from "vitest";

import { createDefaultSemanticProvider } from "./default-semantic-provider.js";

const request = {
  userId: "user-1",
  workspaceId: "workspace-1",
  datasourceId: "orders-db",
  datasourceRevision: "3",
  query: "revenue"
};

describe("createDefaultSemanticProvider", () => {
  it("uses the selected DataLink MCP capability by default", async () => {
    const calls: unknown[] = [];
    const provider = createDefaultSemanticProvider({
      tools: {
        datalink_explore: {
          execute: async (input: unknown) => {
            calls.push(input);
            return { snapshot_id: "snapshot-1", nodes: [{ id: "metric:revenue", source: "verified" }] };
          }
        }
      }
    });

    await expect(provider.resolve(request)).resolves.toMatchObject({
      provider: "datalink",
      mode: "live",
      snapshotId: "snapshot-1"
    });
    expect(calls).toEqual([{ query: "revenue", mask_credential: true }]);
  });
});
