import { describe, expect, it } from "vitest";

import {
  parseSchemaToolResult,
  parseSqlToolResult,
  sqlFromToolPayload,
  unwrapToolResultPayload,
} from "../tool-result-normalize";

describe("tool-result-normalize adversarial cases", () => {
  it("does not treat arbitrary JSON objects with observation-looking keys as envelopes", () => {
    const payload = { observation: "plain text result", extra: true };
    expect(unwrapToolResultPayload(payload)).toBe("plain text result");
  });

  it("handles double-encoded JSON strings", () => {
    const inner = JSON.stringify({ columns: ["a"], rows: [[1]], row_count: 1 });
    expect(parseSqlToolResult(JSON.stringify({ observation: inner }))).toMatchObject({
      columns: ["a"],
      row_count: 1,
    });
  });

  it("returns null for malformed schema payloads", () => {
    expect(parseSchemaToolResult(JSON.stringify({ datasource_id: "x" }))).toBeNull();
    expect(parseSchemaToolResult("not-json")).toBeNull();
  });

  it("filters malformed schema tables and columns", () => {
    expect(parseSchemaToolResult(JSON.stringify({
      datasource_id: "x",
      tables: [
        { name: "orders", columns: [{ name: "id", type: "INTEGER" }, { type: "TEXT" }] },
        { columns: [{ name: "missing_table_name" }] },
        null,
      ],
    }))).toEqual({
      datasource_id: "x",
      tables: [{ name: "orders", columns: [{ name: "id", type: "INTEGER" }] }],
    });
  });

  it("returns null for SQL payloads missing rows", () => {
    expect(parseSqlToolResult(JSON.stringify({ columns: ["a"] }))).toBeNull();
  });

  it("prefers nested SQL result over wrapper metadata", () => {
    expect(parseSqlToolResult(JSON.stringify({
      sql: "SELECT 1",
      result: { columns: ["n"], rows: [[1]], row_count: 1, audit_log_id: "audit-2" },
      audit_log_id: "ignored",
    }))).toMatchObject({
      columns: ["n"],
      audit_log_id: "audit-2",
    });
  });

  it("reads SQL from nested tool result payloads", () => {
    expect(
      sqlFromToolPayload(undefined, JSON.stringify({
        sql: "SELECT COUNT(*) FROM orders",
        result: { columns: ["n"], rows: [[1]], row_count: 1 },
      })),
    ).toBe("SELECT COUNT(*) FROM orders");
  });
});
