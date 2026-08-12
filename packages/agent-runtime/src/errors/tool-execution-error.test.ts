import { describe, expect, it } from "vitest";

import { toolErrorObservation } from "./tool-execution-error.js";

describe("toolErrorObservation", () => {
  it("surfaces the allowed actions carried by a phase rejection", () => {
    const observation = toolErrorObservation(
      new Error("ACTION_NOT_ALLOWED_IN_PHASE:understand:inspect_schema:retrieve_knowledge,read_file,protocol.handoff.propose"),
      { toolName: "inspect_schema" }
    );

    expect(observation.error).toMatchObject({
      code: "ACTION_NOT_ALLOWED_IN_PHASE",
      executionStatus: "not_started",
      retryable: false,
      details: { allowedActions: ["retrieve_knowledge", "read_file", "protocol.handoff.propose"] }
    });
    expect(observation.recovery.instruction).toBe(
      "Continue with one of the actions allowed in phase understand: "
      + "retrieve_knowledge, read_file, protocol.handoff.propose."
    );
    expect(observation.recovery.avoid).toEqual([
      "Do not repeat inspect_schema while the protocol remains in phase understand."
    ]);
  });

  it("keeps the generic guidance when the rejection carries no allowed actions", () => {
    const observation = toolErrorObservation(
      new Error("ACTION_NOT_ALLOWED_IN_PHASE:answer:inspect_schema:"),
      { toolName: "inspect_schema" }
    );

    expect(observation.error.details).toBeUndefined();
    expect(observation.recovery.instruction).toBe(
      "Choose an action allowed in the current phase before calling this tool again."
    );
  });

  it("keeps parsing legacy three-segment rejection messages", () => {
    const observation = toolErrorObservation(
      new Error("ACTION_NOT_ALLOWED_IN_PHASE:answer:inspect_schema"),
      { toolName: "inspect_schema" }
    );

    expect(observation.error.message).toBe("Tool inspect_schema is not allowed in protocol phase answer.");
    expect(observation.error.details).toBeUndefined();
  });

  it("truncates very long allowed-action lists in the instruction but keeps them complete in details", () => {
    const actions = Array.from({ length: 15 }, (_, index) => `tool_${index + 1}`);
    const observation = toolErrorObservation(
      new Error(`ACTION_NOT_ALLOWED_IN_PHASE:scope:run_sql_readonly:${actions.join(",")}`),
      { toolName: "run_sql_readonly" }
    );

    expect(observation.error.details).toEqual({ allowedActions: actions });
    expect(observation.recovery.instruction).toContain("(+3 more)");
  });
});
