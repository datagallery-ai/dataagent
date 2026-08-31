import { describe, expect, it } from "vitest";

import { LocalSemanticProvider } from "./local-semantic-provider.js";
import { SemanticProviderError } from "./types.js";

const request = {
  userId: "user-1",
  workspaceId: "workspace-1",
  datasourceId: "orders-db",
  datasourceRevision: "schema-v1",
  query: "monthly revenue"
};

describe("LocalSemanticProvider", () => {
  it("returns only deterministic physical schema context", async () => {
    const provider = new LocalSemanticProvider({
      inspectSchema: async () => ({
        tables: [{ name: "orders", columns: [{ name: "amount", type: "decimal" }] }],
        metrics: [{ name: "revenue", expression: "sum(amount)" }],
        inferred_joins: [{ left: "orders.customer_id", right: "customers.id" }]
      })
    });

    await expect(provider.resolve(request)).resolves.toEqual({
      value: {
        tables: [{ name: "orders", columns: [{ name: "amount", type: "decimal" }] }]
      },
      capabilities: ["physical-schema"],
      trust: "verified",
      warnings: ["LOCAL_SEMANTIC_LIMITED_TO_PHYSICAL_SCHEMA"]
    });
  });

  it("reports unavailable schema as a fallback-safe error", async () => {
    const provider = new LocalSemanticProvider({ inspectSchema: async () => undefined });

    await expect(provider.resolve(request)).rejects.toMatchObject({
      code: "LOCAL_SEMANTIC_UNAVAILABLE",
      fallbackAllowed: true
    } satisfies Partial<SemanticProviderError>);
  });

  it("uses schema already inspected by the governed data action", async () => {
    const provider = new LocalSemanticProvider({
      inspectSchema: async () => {
        throw new Error("SHOULD_NOT_REINSPECT");
      }
    });

    await expect(provider.resolve({
      ...request,
      physicalSchema: { tables: [{ name: "orders", columns: [] }] }
    })).resolves.toMatchObject({
      value: { tables: [{ name: "orders", columns: [] }] },
      capabilities: ["physical-schema"]
    });
  });
});
