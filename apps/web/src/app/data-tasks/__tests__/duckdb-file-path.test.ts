import { describe, expect, it } from "vitest";
import {
  isWorkspaceConfigItemValid,
  visibleConfigFields,
  type WorkspaceConfigItem,
} from "../data-task-state";

describe("duckdb datasource form", () => {
  it("shows a required file path for DuckDB (real file, not demo)", () => {
    const fields = visibleConfigFields("db", { type: "duckdb", mode: "readonly" });
    const filePath = fields.find((field) => field.key === "filePath");

    expect(filePath).toBeDefined();
    expect(filePath?.required).toBe(true);
    expect(filePath?.helpText?.toLowerCase()).toContain("duckdb");
    expect(filePath?.helpText?.toLowerCase()).toContain("upload");
    expect(filePath?.helpText?.toLowerCase()).not.toContain("demo");
  });

  it("rejects DuckDB configs without a file path", () => {
    const item: WorkspaceConfigItem = {
      id: "sales-duckdb",
      name: "Sales DuckDB",
      description: "",
      enabled: true,
      settings: {
        datasourceId: "sales-duckdb",
        type: "duckdb",
        mode: "readonly",
        filePath: "",
      },
    };

    expect(isWorkspaceConfigItemValid("db", item, item.settings ?? {})).toBe(false);
    expect(
      isWorkspaceConfigItemValid("db", item, {
        ...item.settings,
        filePath: "/data/sales.duckdb",
      }),
    ).toBe(true);
  });

  it("shows Access file path (not host/port) and allows ODBC connection string", () => {
    const fields = visibleConfigFields("db", { type: "access", mode: "readonly" });
    expect(fields.some((field) => field.key === "filePath")).toBe(true);
    expect(fields.some((field) => field.key === "host")).toBe(false);
    expect(fields.some((field) => field.key === "connectionString")).toBe(true);
  });
});
