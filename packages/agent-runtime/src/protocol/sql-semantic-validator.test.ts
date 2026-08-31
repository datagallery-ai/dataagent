import { describe, expect, it } from "vitest";

import { createAnalysisAssertions } from "./analysis-contract.js";
import { validateSqlSemantics } from "./sql-semantic-validator.js";

describe("SQL semantic validator", () => {
  it("accepts SQL that satisfies source, aggregate, grain and inclusive end-date constraints", () => {
    const assertions = createAnalysisAssertions("R1", [{
      kind: "metric",
      description: "按大区计算完整验证期订单数",
      sourceTables: ["orders"],
      sqlConstraints: [
        { kind: "aggregate", function: "count", alias: "order_count" },
        { kind: "group_by", columns: ["region"] },
        {
          kind: "time_range",
          column: "order_date",
          start: "2023-07-01",
          end: "2023-12-31",
          endInclusive: true
        }
      ]
    }]);
    const sql = `
      SELECT region, COUNT(*) AS order_count
      FROM orders
      WHERE order_date >= '2023-07-01' AND order_date < '2024-01-01'
      GROUP BY region
    `;

    expect(validateSqlSemantics(sql, "sqlite", assertions)).toEqual([]);
  });

  it("accepts COUNT star when the grounded contract requires the star operand", () => {
    const assertions = createAnalysisAssertions("R1", [{
      kind: "metric",
      description: "计算订单总数",
      sourceTables: ["orders"],
      sqlConstraints: [{ kind: "aggregate", function: "COUNT", column: "*", alias: "order_count" }]
    }]);

    const findings = validateSqlSemantics(
      "SELECT COUNT(*) AS order_count FROM orders",
      "sqlite",
      assertions
    );

    expect(findings).toEqual([]);
  });

  it("rejects a query against the wrong source table", () => {
    const assertions = createAnalysisAssertions("R1", [{
      kind: "metric",
      description: "统计订单",
      sourceTables: ["orders"]
    }]);

    const findings = validateSqlSemantics("SELECT COUNT(*) FROM customers", "sqlite", assertions);

    expect(findings[0]?.code).toBe("SQL_SEMANTIC_SOURCE_MISSING:orders");
  });

  it("rejects an aggregate with the wrong function or alias", () => {
    const assertions = createAnalysisAssertions("R1", [{
      kind: "metric",
      description: "统计订单数",
      sqlConstraints: [{ kind: "aggregate", function: "count", alias: "order_count" }]
    }]);

    const findings = validateSqlSemantics("SELECT SUM(amount) AS order_count FROM orders", "sqlite", assertions);

    expect(findings[0]?.code).toBe("SQL_SEMANTIC_AGGREGATE_MISSING:count");
    expect(findings[0]?.message).toContain("Expected COUNT AS order_count");
    expect(findings[0]?.message).toContain("observed SUM(amount) AS order_count");
  });

  it("reports the exact expected and observed aliases for aggregate mismatches", () => {
    const assertions = createAnalysisAssertions("R1", [{
      kind: "metric",
      description: "统计订单数",
      sqlConstraints: [{ kind: "aggregate", function: "COUNT", column: "*", alias: "total_orders" }]
    }]);

    const findings = validateSqlSemantics(
      "SELECT COUNT(*) AS order_count FROM orders",
      "sqlite",
      assertions
    );

    expect(findings[0]?.message).toContain("Expected COUNT(*) AS total_orders");
    expect(findings[0]?.message).toContain("observed COUNT(*) AS order_count");
  });

  it("rejects a missing group-by grain", () => {
    const assertions = createAnalysisAssertions("R1", [{
      kind: "grain",
      description: "按大区统计",
      sqlConstraints: [{ kind: "group_by", columns: ["region"] }]
    }]);

    const findings = validateSqlSemantics("SELECT COUNT(*) FROM orders", "sqlite", assertions);

    expect(findings[0]?.code).toBe("SQL_SEMANTIC_GROUP_BY_MISSING:region");
  });

  it("rejects an inclusive end date that omits the final day", () => {
    const assertions = createAnalysisAssertions("R1", [{
      kind: "filter",
      description: "包含完整验证期",
      sqlConstraints: [{
        kind: "time_range",
        column: "order_date",
        start: "2023-07-01",
        end: "2023-12-31",
        endInclusive: true
      }]
    }]);
    const sql = "SELECT COUNT(*) FROM orders WHERE order_date >= '2023-07-01' AND order_date < '2023-12-31'";

    const findings = validateSqlSemantics(sql, "sqlite", assertions);

    expect(findings.map((finding) => finding.code)).toContain("SQL_SEMANTIC_TIME_END_MISSING:order_date");
  });

  it("returns a stable finding for unparsable SQL", () => {
    const findings = validateSqlSemantics("SELECT FROM", "sqlite", []);

    expect(findings[0]?.code).toBe("SQL_SEMANTIC_PARSE_FAILED");
  });
});
