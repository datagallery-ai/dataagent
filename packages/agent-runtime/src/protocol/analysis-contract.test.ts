import { describe, expect, it } from "vitest";

import {
  createAnalysisAssertions,
  createManualAnalysisAssertion,
  resolveRequirementAssertions,
  type AnalysisAssertionDraft
} from "./analysis-contract.js";

describe("analysis contract", () => {
  it("assigns stable server-owned assertion ids and normalizes optional fields", () => {
    const drafts: AnalysisAssertionDraft[] = [{
      kind: "metric",
      description: "计算验证期负利润率",
      sourceTables: ["orders"],
      dimensions: ["region"],
      sqlConstraints: [
        { kind: "aggregate", function: "count", alias: "order_count" },
        {
          kind: "time_range",
          column: "order_date",
          start: "2023-07-01",
          end: "2023-12-31",
          endInclusive: true
        }
      ],
      resultChecks: [{ kind: "non_empty", required: true }],
      claimValues: [{ name: "order_count", field: "order_count", required: true, unit: "orders" }]
    }];

    expect(createAnalysisAssertions("R3", drafts)).toEqual([{
      id: "R3.A1",
      requirementId: "R3",
      kind: "metric",
      description: "计算验证期负利润率",
      required: true,
      sourceTables: ["orders"],
      dimensions: ["region"],
      sqlConstraints: drafts[0]?.sqlConstraints,
      resultChecks: drafts[0]?.resultChecks,
      claimValues: drafts[0]?.claimValues
    }]);
  });

  it("creates an explicit manual assertion instead of pretending an unexpressed requirement is verified", () => {
    expect(createManualAnalysisAssertion("R2", "判断运营策略是否合理")).toMatchObject({
      id: "R2.A1",
      requirementId: "R2",
      kind: "manual",
      description: "判断运营策略是否合理",
      required: true,
      sqlConstraints: [],
      resultChecks: [],
      claimValues: []
    });
  });

  it("rejects query assertion references outside the selected requirements", () => {
    const requirements = [{
      id: "R1",
      assertions: createAnalysisAssertions("R1", [{ kind: "metric", description: "计算订单数" }])
    }];

    expect(() => resolveRequirementAssertions(requirements, ["R1"], ["R2.A1"]))
      .toThrow("ANALYSIS_ASSERTION_NOT_FOUND:R2.A1");
  });

  it("requires every selected requirement to have a selected assertion", () => {
    const requirements = [{
      id: "R1",
      assertions: createAnalysisAssertions("R1", [{ kind: "metric", description: "计算订单数" }])
    }];

    expect(() => resolveRequirementAssertions(requirements, ["R1"], []))
      .toThrow("ANALYSIS_ASSERTION_IDS_REQUIRED:R1");
  });
});
