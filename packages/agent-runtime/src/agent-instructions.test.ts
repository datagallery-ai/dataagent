import { describe, expect, it } from "vitest";

import { buildAgentInstructions, protocolHandoffInputSchema } from "./index.js";
import type { AgentRunContext } from "./types.js";

const runContext: AgentRunContext = {
  user_id: "user-1",
  session_id: "session-1",
  run_id: "run-1",
  user_input: "再次尝试",
  chat_mode: "agent",
  selected_datasource_id: "orders-db",
  enabled_datasource_ids: ["orders-db"]
};

const baseInput = {
  runContext,
  commandExecutionEnabled: false,
  collaborationToolsEnabled: false,
  pythonRuntimeAvailable: false,
  selectedSkills: [],
  taskToolsEnabled: false,
  toolNames: ["list_data_sources", "inspect_schema", "preview_table", "run_sql_readonly", "protocol_handoff"],
  mcpToolNames: [],
  analysisRequirements: [],
  workspaceAttachments: []
};

describe("buildAgentInstructions", () => {
  it("keeps data-tool instructions valid after a protocol handoff", () => {
    const instructions = buildAgentInstructions({ ...baseInput, protocolId: "general-task" });

    expect(instructions).toContain("latest runtime protocol and phase");
    expect(instructions).toContain("latest runtime state and protocol_handoff observation are authoritative");
    expect(instructions).toContain('protocol_handoff with targetProtocolId "data-analysis"');
    expect(instructions).not.toContain("This run is governed by general-task@1");
  });

  it("does not pin data-analysis as authoritative after a reverse handoff", () => {
    const instructions = buildAgentInstructions({ ...baseInput, protocolId: "data-analysis" });

    expect(instructions).toContain(
      "Data tools (availability is governed by the latest runtime protocol and phase): "
      + "list_data_sources, inspect_schema, preview_table, run_sql_readonly."
    );
    expect(instructions).toContain("this startup label is not authoritative after a handoff");
    expect(instructions).not.toContain("This run is governed by data-analysis@1");
  });

  it("omits the data tool group entirely when no data tools are selected", () => {
    const instructions = buildAgentInstructions({
      ...baseInput,
      toolNames: ["retrieve_knowledge", "protocol_handoff"],
      protocolId: "general-task"
    });

    expect(instructions).not.toContain("Data tools");
    expect(instructions).not.toContain("DISABLED by the current protocol");
  });

  it("preserves the legacy protocol_handoff model input schema", () => {
    expect(protocolHandoffInputSchema.safeParse({
      targetProtocolId: "data-analysis",
      targetProtocolVersion: "1",
      reasonCodes: ["ANALYTIC_INTENT"],
      unresolvedGoals: []
    }).success).toBe(true);
    expect(protocolHandoffInputSchema.safeParse({
      targetProtocolId: "data-analysis",
      targetProtocolVersion: "1",
      reasonCodes: ["ANALYTIC_INTENT"]
    }).success).toBe(false);
  });
});
