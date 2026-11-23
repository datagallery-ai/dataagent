import { describe, expect, it } from "vitest";

import {
  filterTableRows,
  normalizeSqlTable,
  resolveColumnValue,
  sortTableRows,
  tableToCsv,
} from "../table-rows";

describe("table-rows", () => {
  it("reads object rows by column alias", () => {
    const rows = [{ total_orders: 42 }];
    const { columns, rows: normalized } = normalizeSqlTable(
      ["COUNT(*) as total_orders"],
      rows,
    );

    expect(columns).toEqual(["COUNT(*) as total_orders"]);
    expect(normalized).toEqual([[42]]);
  });

  it("falls back to object keys when declared columns do not match", () => {
    const rows = [
      { order_id: "o_001", gmv: 1280 },
      { order_id: "o_002", gmv: 640 },
    ];
    const { columns, rows: normalized } = normalizeSqlTable(
      ["COUNT(*) as total_orders"],
      rows,
    );

    expect(columns).toEqual(["order_id", "gmv"]);
    expect(normalized).toEqual([
      ["o_001", 1280],
      ["o_002", 640],
    ]);
  });

  it("resolves alias keys directly", () => {
    expect(resolveColumnValue({ total_orders: 7 }, "COUNT(*) as total_orders")).toBe(7);
  });

  it("filters rows by any visible cell", () => {
    expect(
      filterTableRows(
        [
          ["search", 42],
          ["direct", 18],
        ],
        "sea",
      ),
    ).toEqual([["search", 42]]);
  });

  it("sorts rows by numeric or textual column values", () => {
    expect(
      sortTableRows(
        [
          ["search", 42],
          ["direct", 18],
        ],
        { columnIndex: 1, direction: "asc" },
      ),
    ).toEqual([
      ["direct", 18],
      ["search", 42],
    ]);
    expect(
      sortTableRows(
        [
          ["b", 1],
          ["a", 2],
        ],
        { columnIndex: 0, direction: "asc" },
      ),
    ).toEqual([
      ["a", 2],
      ["b", 1],
    ]);
  });

  it("exports escaped csv content", () => {
    expect(tableToCsv(["name", "note"], [["Alice", "a,b"], ["Bob", "line\nbreak"]])).toBe(
      'name,note\nAlice,"a,b"\nBob,"line\nbreak"',
    );
  });
});
