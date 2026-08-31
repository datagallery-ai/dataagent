import { describe, expect, it } from "vitest";

import { createAnalysisAssertions } from "./analysis-contract.js";
import { verifyAnalysisResult } from "./result-verifier.js";

describe("analysis result verifier", () => {
  it("recomputes a ratio and extracts verified claim values", () => {
    const assertions = createAnalysisAssertions("R1", [{
      kind: "metric",
      description: "计算负利润率",
      resultChecks: [{
        kind: "ratio",
        required: true,
        value: { field: "negative_rate" },
        numerator: { field: "negative_orders" },
        denominator: { field: "orders" },
        tolerance: 0.000001
      }],
      claimValues: [{
        name: "negative_rate",
        field: "negative_rate",
        required: true,
        unit: "ratio",
        tolerance: 0.000001
      }]
    }]);

    const verification = verifyAnalysisResult({
      columns: ["orders", "negative_orders", "negative_rate"],
      rows: [[200, 10, 0.05]],
      rowCount: 1
    }, assertions);

    expect(verification.valid).toBe(true);
    expect(verification.findings).toEqual([]);
    expect(verification.verifiedValues).toEqual([{
      name: "negative_rate",
      value: 0.05,
      unit: "ratio",
      tolerance: 0.000001,
      assertionId: "R1.A1"
    }]);
  });

  it("blocks verified values when a required non-empty check fails", () => {
    const assertions = createAnalysisAssertions("R1", [{
      kind: "metric",
      description: "必须返回订单",
      resultChecks: [{ kind: "non_empty", required: true }],
      claimValues: [{ name: "orders", field: "orders", required: true }]
    }]);

    const verification = verifyAnalysisResult({ columns: ["orders"], rows: [], rowCount: 0 }, assertions);

    expect(verification).toMatchObject({
      valid: false,
      verifiedValues: [],
      findings: [{ code: "RESULT_CHECK_NON_EMPTY_FAILED", severity: "error", assertionId: "R1.A1" }]
    });
  });

  it("rejects a row count outside the declared range", () => {
    const assertions = createAnalysisAssertions("R1", [{
      kind: "grain",
      description: "只返回三个冻结组合",
      resultChecks: [{ kind: "row_count", required: true, min: 3, max: 3 }]
    }]);

    const verification = verifyAnalysisResult({ columns: ["group"], rows: [["A"], ["B"]], rowCount: 2 }, assertions);

    expect(verification.valid).toBe(false);
    expect(verification.findings[0]?.code).toBe("RESULT_CHECK_ROW_COUNT_FAILED");
  });

  it("rejects null values in required result fields", () => {
    const assertions = createAnalysisAssertions("R1", [{
      kind: "metric",
      description: "利润不得为空",
      resultChecks: [{ kind: "not_null", required: true, fields: ["profit"] }]
    }]);

    const verification = verifyAnalysisResult({ columns: ["profit"], rows: [[null]], rowCount: 1 }, assertions);

    expect(verification.valid).toBe(false);
    expect(verification.findings[0]?.code).toBe("RESULT_CHECK_NOT_NULL_FAILED:profit");
  });

  it("rejects duplicate rows at the declared grain", () => {
    const assertions = createAnalysisAssertions("R1", [{
      kind: "grain",
      description: "每个大区一行",
      resultChecks: [{ kind: "unique", required: true, fields: ["region"] }]
    }]);

    const verification = verifyAnalysisResult({
      columns: ["region", "orders"],
      rows: [["华东", 10], ["华东", 20]],
      rowCount: 2
    }, assertions);

    expect(verification.valid).toBe(false);
    expect(verification.findings[0]?.code).toBe("RESULT_CHECK_UNIQUE_FAILED:region");
  });

  it("resolves selectors when comparing values across result rows", () => {
    const assertions = createAnalysisAssertions("R1", [{
      kind: "comparison",
      description: "两个方案预算相同",
      resultChecks: [{
        kind: "equals",
        required: true,
        left: { field: "budget", selector: { plan: "A" } },
        right: { field: "budget", selector: { plan: "B" } },
        tolerance: 0.01
      }]
    }]);

    const verification = verifyAnalysisResult({
      columns: ["plan", "budget"],
      rows: [["A", 100], ["B", 100.02]],
      rowCount: 2
    }, assertions);

    expect(verification.valid).toBe(false);
    expect(verification.findings[0]?.code).toBe("RESULT_CHECK_EQUALS_FAILED");
  });

  it("reconciles a total against selected part rows", () => {
    const assertions = createAnalysisAssertions("R1", [{
      kind: "reconciliation",
      description: "分区订单数合计等于全体",
      resultChecks: [{
        kind: "sum",
        required: true,
        total: { field: "orders", selector: { scope: "all" } },
        parts: [
          { field: "orders", selector: { scope: "east" } },
          { field: "orders", selector: { scope: "west" } }
        ]
      }]
    }]);

    const verification = verifyAnalysisResult({
      columns: ["scope", "orders"],
      rows: [["east", 40], ["west", 50], ["all", 100]],
      rowCount: 3
    }, assertions);

    expect(verification.valid).toBe(false);
    expect(verification.findings[0]?.code).toBe("RESULT_CHECK_SUM_FAILED");
  });

  it("evaluates a declared comparison operator", () => {
    const assertions = createAnalysisAssertions("R1", [{
      kind: "comparison",
      description: "方案A必须严格优于方案B",
      resultChecks: [{
        kind: "comparison",
        required: true,
        left: { field: "per_10k", selector: { plan: "A" } },
        right: { field: "per_10k", selector: { plan: "B" } },
        operator: "gt"
      }]
    }]);

    const verification = verifyAnalysisResult({
      columns: ["plan", "per_10k"],
      rows: [["A", 4.225], ["B", 4.225]],
      rowCount: 2
    }, assertions);

    expect(verification.valid).toBe(false);
    expect(verification.findings[0]?.code).toBe("RESULT_CHECK_COMPARISON_FAILED:gt");
  });

  it("enforces counterfactual budget conservation", () => {
    const assertions = createAnalysisAssertions("R1", [{
      kind: "counterfactual",
      description: "新增利润必须等于预算",
      resultChecks: [{
        kind: "budget_conservation",
        required: true,
        left: { field: "budget" },
        right: { field: "incremental_profit" },
        tolerance: 0.01
      }]
    }]);

    const verification = verifyAnalysisResult({
      columns: ["budget", "incremental_profit"],
      rows: [[7100.6, 7099.5]],
      rowCount: 1
    }, assertions);

    expect(verification.valid).toBe(false);
    expect(verification.findings[0]?.code).toBe("RESULT_CHECK_BUDGET_CONSERVATION_FAILED");
  });

  it("rejects a missing required claim value field", () => {
    const assertions = createAnalysisAssertions("R1", [{
      kind: "metric",
      description: "必须暴露订单数",
      claimValues: [{ name: "orders", field: "order_count", required: true }]
    }]);

    const verification = verifyAnalysisResult({ columns: ["total"], rows: [[10]], rowCount: 1 }, assertions);

    expect(verification.valid).toBe(false);
    expect(verification.verifiedValues).toEqual([]);
    expect(verification.findings[0]?.code).toBe("RESULT_CLAIM_VALUE_MISSING:orders");
  });
});
