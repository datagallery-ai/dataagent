import { describe, expect, it } from "vitest";

import { buildAgentInstructions } from "./index.js";
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
  it("declares data tools disabled when general-task governs a run that exposes them", () => {
    const instructions = buildAgentInstructions({ ...baseInput, protocolId: "general-task" });

    expect(instructions).toContain("DISABLED by the current protocol");
    expect(instructions).toContain("ACTION_NOT_ALLOWED_IN_PHASE");
    expect(instructions).toContain('protocol_handoff with targetProtocolId "data-analysis"');
    expect(instructions).not.toContain("Data tools: list_data_sources");
  });

  it("advertises data tools normally under the data-analysis protocol", () => {
    const instructions = buildAgentInstructions({ ...baseInput, protocolId: "data-analysis" });

    expect(instructions).toContain(
      "Data tools: list_data_sources, inspect_schema, preview_table, run_sql_readonly."
    );
    expect(instructions).not.toContain("DISABLED by the current protocol");
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
});
