import { describe, expect, it } from "vitest";

import { enrichSqlDialectError, validateSqlDialect } from "./sql-dialect-validation.js";

describe("SQL dialect validation", () => {
  it("rejects percentile_cont within group for SQLite with a repair hint", () => {
    expect(validateSqlDialect(
      "SELECT PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY profit) FROM orders",
      "sqlite"
    )).toEqual([{
      code: "SQLITE_PERCENTILE_CONT_UNSUPPORTED",
      dialect: "sqlite",
      hint: "Use ROW_NUMBER and COUNT to select or interpolate ordered percentile rows."
    }]);
  });

  it("rejects ilike for SQLite", () => {
    expect(validateSqlDialect("SELECT * FROM orders WHERE name ILIKE '%a%'", "sqlite")).toEqual([
      expect.objectContaining({ code: "SQLITE_ILIKE_UNSUPPORTED", dialect: "sqlite" })
    ]);
  });

  it("allows supported SQLite read queries", () => {
    expect(validateSqlDialect("SELECT region, SUM(profit) FROM orders GROUP BY region", "sqlite")).toEqual([]);
  });

  it("enriches a known SQLite UNION LIMIT error", () => {
    const error = enrichSqlDialectError(
      new Error("LIMIT clause should come after UNION ALL not before"),
      "sqlite"
    );

    expect(error.message).toContain("SQLITE_UNION_LIMIT_POSITION");
    expect(error.message).toContain("Wrap the limited UNION branch in a subquery");
  });
});
