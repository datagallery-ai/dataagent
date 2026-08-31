import { describe, expect, it } from "vitest";
import {
  parseToolResultError,
  resolveToolDisplayStatus,
  toolDisplayStatusLabel,
  toolResultLooksLikeError,
} from "../tool-call-display";

describe("resolveToolDisplayStatus", () => {
  it("prefers complete when tool message exists", () => {
    expect(
      resolveToolDisplayStatus({
        copilotStatus: "complete",
        backendPhase: "running",
        hasResult: true,
      }),
    ).toBe("complete");
  });

  it("maps backend running to executing while copilot is still inProgress", () => {
    expect(
      resolveToolDisplayStatus({
        copilotStatus: "inProgress",
        backendPhase: "running",
        hasResult: false,
      }),
    ).toBe("executing");
  });

  it("keeps pending before backend starts", () => {
    expect(
      resolveToolDisplayStatus({
        copilotStatus: "inProgress",
        hasResult: false,
      }),
    ).toBe("pending");
  });

  it("maps copilot complete without observation to failed", () => {
    expect(
      resolveToolDisplayStatus({
        copilotStatus: "complete",
        hasResult: false,
      }),
    ).toBe("failed");
  });

  it("maps error payload to failed even when backend marked success", () => {
    expect(
      resolveToolDisplayStatus({
        copilotStatus: "complete",
        backendPhase: "success",
        hasResult: true,
        resultIsError: true,
      }),
    ).toBe("failed");
  });

  it("surfaces backend failure", () => {
    expect(
      resolveToolDisplayStatus({
        copilotStatus: "inProgress",
        backendPhase: "failed",
        hasResult: false,
      }),
    ).toBe("failed");
  });
});

describe("parseToolResultError", () => {
  it("detects CopilotKit protocol error payloads", () => {
    const failure = parseToolResultError(
      JSON.stringify({
        status: "error",
        reason: "missing_terminal_event",
        message:
          "Cannot send event type 'TOOL_CALL_RESULT': The run has already finished with 'RUN_FINISHED'.",
      }),
    );
    expect(failure?.kind).toBe("protocol");
    expect(failure?.title).toBe("Result sync failed");
    expect(toolResultLooksLikeError(JSON.stringify({ status: "error" }))).toBe(true);
  });
});

describe("toolDisplayStatusLabel", () => {
  it("uses unified Chinese labels", () => {
    expect(toolDisplayStatusLabel("pending")).toBe("Pending");
    expect(toolDisplayStatusLabel("executing")).toBe("Running");
    expect(toolDisplayStatusLabel("complete")).toBe("Completed");
    expect(toolDisplayStatusLabel("failed")).toBe("Failed");
  });
});
