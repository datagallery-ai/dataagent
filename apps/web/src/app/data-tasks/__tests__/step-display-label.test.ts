import { describe, expect, it } from "vitest";
import {
  resolveCollaborationCompletedStepLabel,
  resolveStepBadgePresentation,
  resolveCollaborationStepLabel,
  resolveStepSummaryText,
  resolveToolStepActionLabel,
} from "../step-display-label";

describe("resolveToolStepActionLabel", () => {
  it("uses specific action labels for common data tools", () => {
    expect(resolveToolStepActionLabel(["list_data_sources"])).toBe("List data sources");
    expect(resolveToolStepActionLabel(["inspect_schema"])).toBe("Inspect schema");
    expect(resolveToolStepActionLabel(["preview_table"])).toBe("Preview data");
    expect(resolveToolStepActionLabel(["run_sql_readonly"])).toBe("Run query");
  });

  it("uses specific action labels for file and knowledge tools", () => {
    expect(resolveToolStepActionLabel(["retrieve_knowledge"])).toBe("Retrieve knowledge");
    expect(resolveToolStepActionLabel(["edit_file"])).toBe("Edit file");
    expect(resolveToolStepActionLabel(["read_file"])).toBe("Read file");
  });

  it("summarizes mixed or unknown tool sets without templated wording", () => {
    expect(resolveToolStepActionLabel(["read_file", "edit_file"])).toBe("Handle files");
    expect(resolveToolStepActionLabel(["list_files", "grep"])).toBe("Operate workspace");
    expect(resolveToolStepActionLabel(["unknown_tool", "another_tool"])).toBe("Call 2 tools");
  });

  it("falls back to readable labels for unmapped and namespaced tools", () => {
    expect(resolveToolStepActionLabel(["custom_analytics_tool"])).toBe("Custom Analytics Tool");
    expect(resolveToolStepActionLabel(["mcp__demo__inspect_schema"])).toBe("Inspect schema");
    expect(resolveToolStepActionLabel(["List data sources"])).toBe("List data sources");
  });

  it("labels repeated parallel calls to the same tool as one concurrent batch", () => {
    expect(
      resolveToolStepActionLabel([
        "list_data_sources",
        "list_data_sources",
        "list_data_sources",
      ]),
    ).toBe("Run 3 tools in parallel");
  });
});

describe("resolveStepSummaryText", () => {
  it("prefers Chinese tool labels over English body text when tools ran", () => {
    expect(
      resolveStepSummaryText({
        content: "I'll inspect the schema for you.",
        hasToolCalls: true,
        displayToolNames: "Inspect data source schema",
        toolActionLabel: "Inspect schema",
        isThought: false,
      }),
    ).toBe("I'll inspect the schema for you. · Call Inspect data source schema");
  });

  it("shows thought preview before tool invocation when both are available", () => {
    expect(
      resolveStepSummaryText({
        content: "我先查看一下可用的数据源。",
        hasToolCalls: true,
        displayToolNames: "List data sources",
        toolActionLabel: "List data sources",
        isThought: false,
      }),
    ).toBe("我先查看一下可用的数据源。 · Call List data sources");
  });

  it("shows only tool invocation when there is no thought text", () => {
    expect(
      resolveStepSummaryText({
        content: "",
        hasToolCalls: true,
        displayToolNames: "List data sources",
        toolActionLabel: "List data sources",
        isThought: false,
      }),
    ).toBe("Call List data sources");
  });

  it("shows thought preview text for English-only interim messages", () => {
    expect(
      resolveStepSummaryText({
        content: "Let me think about this query.",
        hasToolCalls: false,
        displayToolNames: "",
        toolActionLabel: "思考",
        isThought: true,
      }),
    ).toBe("Let me think about this query.");
  });

  it("shows Chinese thought preview when available", () => {
    expect(
      resolveStepSummaryText({
        content: "我需要分析不同品类的销售额数据。",
        hasToolCalls: false,
        displayToolNames: "",
        toolActionLabel: "思考",
        isThought: true,
      }),
    ).toBe("我需要分析不同品类的销售额数据。");
  });
});

describe("resolveCollaborationStepLabel", () => {
  it("labels completed ask_user steps by interaction type, not accepted result", () => {
    expect(resolveCollaborationStepLabel(["ask_user"], false)).toBe("User collaboration");
  });

  it("labels submit_plan steps distinctly from ask_user", () => {
    expect(resolveCollaborationStepLabel(["submit_plan"], false)).toBe("Plan approval");
  });

  it("uses the linked collaboration tool name when the chat message has no raw toolCalls", () => {
    expect(resolveCollaborationStepLabel([], false, "submit_plan")).toBe("Plan approval");
    expect(resolveCollaborationStepLabel([], false, "ask_user")).toBe("User collaboration");
  });
});

describe("resolveStepBadgePresentation", () => {
  it("keeps collaboration tools in the numbered tool sequence", () => {
    expect(
      resolveStepBadgePresentation({
        stepNumber: 2,
        isCollaboration: true,
        isWaitingForUser: false,
        isActive: false,
        isFinalAnswer: false,
        isStreamingAnswer: false,
        isThought: false,
      }),
    ).toEqual({ kind: "number", value: 2 });
  });
});

describe("resolveCollaborationCompletedStepLabel", () => {
  it("labels answered ask_user steps as Confirmation complete", () => {
    expect(resolveCollaborationCompletedStepLabel(["ask_user"])).toBe("Confirmation complete");
  });

  it("labels answered submit_plan steps as Plan approved", () => {
    expect(resolveCollaborationCompletedStepLabel(["submit_plan"])).toBe("Plan approved");
  });
});
