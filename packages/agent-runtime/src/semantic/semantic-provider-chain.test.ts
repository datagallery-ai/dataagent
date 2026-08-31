import { describe, expect, it } from "vitest";

import { SemanticProviderChain } from "./semantic-provider-chain.js";
import { SemanticProviderError, type SemanticProvider } from "./types.js";

const request = {
  userId: "user-1",
  workspaceId: "workspace-1",
  datasourceId: "datasource-1",
  datasourceRevision: "schema-v1",
  query: "monthly revenue"
};

describe("SemanticProviderChain", () => {
  it("uses live DataLink as the default provider", async () => {
    const chain = new SemanticProviderChain({
      live: provider("datalink", { nodes: ["revenue"] }),
      local: provider("local", { tables: [] })
    });

    const result = await chain.resolve(request);

    expect(result).toMatchObject({ provider: "datalink", mode: "live", value: { nodes: ["revenue"] } });
  });

  it("uses a fresh DataLink snapshot after a transient live failure", async () => {
    const chain = new SemanticProviderChain({
      live: failingProvider("DATALINK_TIMEOUT", true),
      local: provider("local", { tables: [] }),
      now: () => 1000
    });
    chain.cacheSnapshot(request, {
      value: { nodes: ["cached-revenue"] },
      snapshotId: "snapshot-1",
      expiresAt: 2000
    });

    const result = await chain.resolve(request);

    expect(result).toMatchObject({
      provider: "datalink",
      mode: "cached",
      snapshotId: "snapshot-1",
      value: { nodes: ["cached-revenue"] }
    });
  });

  it("uses the deterministic local provider when no fresh snapshot exists", async () => {
    const chain = new SemanticProviderChain({
      live: failingProvider("DATALINK_UNAVAILABLE", true),
      local: provider("local", { tables: ["orders"] }),
      now: () => 3000
    });
    chain.cacheSnapshot(request, { value: {}, snapshotId: "expired", expiresAt: 2000 });

    const result = await chain.resolve(request);

    expect(result).toMatchObject({ provider: "local", mode: "fallback", value: { tables: ["orders"] } });
    expect(result.fallbackReason).toBe("DATALINK_UNAVAILABLE");
  });

  it("preserves an empty-result fallback reason", async () => {
    const chain = new SemanticProviderChain({
      live: failingProvider("DATALINK_EMPTY_RESULT", true),
      local: provider("local", { tables: ["orders"] })
    });

    const result = await chain.resolve(request);

    expect(result).toMatchObject({
      provider: "local",
      mode: "fallback",
      fallbackReason: "DATALINK_EMPTY_RESULT"
    });
  });

  it("does not hide DataLink authorization failures with a fallback", async () => {
    const chain = new SemanticProviderChain({
      live: failingProvider("DATALINK_NOT_AUTHORIZED", false),
      local: provider("local", { tables: ["orders"] })
    });

    await expect(chain.resolve(request)).rejects.toThrow("DATALINK_NOT_AUTHORIZED");
  });
});

const provider = (id: "datalink" | "local", value: unknown): SemanticProvider => ({
  id,
  resolve: async () => ({
    value,
    capabilities: id === "datalink" ? ["graph-explore"] : ["physical-schema"],
    trust: id === "datalink" ? "inferred" : "verified",
    warnings: []
  })
});

const failingProvider = (code: string, fallbackAllowed: boolean): SemanticProvider => ({
  id: "datalink",
  resolve: async () => {
    throw new SemanticProviderError(code, fallbackAllowed);
  }
});
