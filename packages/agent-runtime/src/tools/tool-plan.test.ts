import { describe, expect, it } from "vitest";

import { buildToolPlan } from "./tool-plan.js";

type FakeTool = { execute: () => Promise<unknown> };
const tool = (): FakeTool => ({ execute: async () => ({}) });

describe("buildToolPlan", () => {
  it("keeps everything exposed with reasons when no skill policy narrows the set", () => {
    const plan = buildToolPlan({
      groups: [
        { source: "data", tools: { inspect_schema: tool() } },
        { source: "workspace", tools: { write_file: tool() } }
      ]
    });

    expect(Object.keys(plan.exposedTools)).toEqual(["inspect_schema", "write_file"]);
    expect(plan.entries).toEqual([
      { name: "inspect_schema", source: "data", exposed: true, reasons: ["source:data", "skill-policy:open"] },
      { name: "write_file", source: "workspace", exposed: true, reasons: ["source:workspace", "skill-policy:open"] }
    ]);
  });

  it("applies deny before allow and records the losing reason", () => {
    const plan = buildToolPlan({
      groups: [{ source: "workspace", tools: { write_file: tool(), execute_command: tool() } }],
      skillPolicy: { allowedTools: ["write_file", "execute_command"], deniedTools: ["execute_command"] }
    });

    expect(Object.keys(plan.exposedTools)).toEqual(["write_file"]);
    expect(plan.entries.find((entry) => entry.name === "execute_command")).toEqual({
      name: "execute_command",
      source: "workspace",
      exposed: false,
      reasons: ["source:workspace", "skill-policy:denied"]
    });
  });

  it("exempts always-allow and skill-meta tools from a narrowing allow list", () => {
    const plan = buildToolPlan({
      groups: [{
        source: "data",
        tools: { inspect_schema: tool(), run_sql_readonly: tool(), skill_search: tool(), read_file: tool() }
      }],
      alwaysAllow: new Set(["inspect_schema", "run_sql_readonly"]),
      skillPolicy: { allowedTools: ["read_file"], deniedTools: [] }
    });

    expect(Object.keys(plan.exposedTools).sort()).toEqual(
      ["inspect_schema", "read_file", "run_sql_readonly", "skill_search"]
    );
    expect(plan.entries.find((entry) => entry.name === "inspect_schema")?.reasons)
      .toEqual(["source:data", "always-allow"]);
    expect(plan.entries.find((entry) => entry.name === "skill_search")?.reasons)
      .toEqual(["source:data", "skill-meta"]);
  });

  it("drops tools outside a narrowing allow list with an explicit reason", () => {
    const plan = buildToolPlan({
      groups: [{ source: "files", tools: { get_file: tool() } }],
      skillPolicy: { allowedTools: ["something_else"], deniedTools: [] }
    });

    expect(plan.exposedTools).toEqual({});
    expect(plan.entries[0]).toEqual({
      name: "get_file",
      source: "files",
      exposed: false,
      reasons: ["source:files", "skill-policy:not-allowed"]
    });
  });

  it("merges MCP tools after the skill policy and labels their own policy layer", () => {
    const plan = buildToolPlan({
      groups: [{ source: "data", tools: { inspect_schema: tool() } }],
      mcpTools: { datalink_explore: tool() },
      skillPolicy: { allowedTools: [], deniedTools: ["datalink_explore"] }
    });

    // The skill deny list does not govern MCP tools; their per-server allowlist does.
    expect(Object.keys(plan.exposedTools)).toContain("datalink_explore");
    expect(plan.entries.find((entry) => entry.name === "datalink_explore")?.reasons)
      .toEqual(["source:mcp", "mcp-policy:server-allowlist"]);
  });

  it("lets later groups override earlier names, matching spread-order semantics", () => {
    const first = tool();
    const second = tool();
    const plan = buildToolPlan({
      groups: [
        { source: "files", tools: { read_file: first } },
        { source: "workspace", tools: { read_file: second } }
      ]
    });

    expect(plan.exposedTools.read_file).toBe(second);
    expect(plan.entries).toHaveLength(1);
    expect(plan.entries[0]?.source).toBe("workspace");
  });
});
