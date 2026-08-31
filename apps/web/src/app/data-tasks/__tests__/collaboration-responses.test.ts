import { describe, expect, it, vi } from "vitest";
import { createElement } from "react";

vi.mock("@copilotkit/react-core/v2", () => ({
  CopilotChatAssistantMessage: {
    MarkdownRenderer: ({ content }: { content: string }) =>
      createElement("span", null, content),
  },
  useAgent: () => ({ agent: { messages: [], threadId: "t" } }),
  useCopilotChatConfiguration: () => ({ threadId: "t", agentId: "dataFoundry" }),
  useCopilotKit: () => ({
    copilotkit: {
      renderCustomMessages: [],
      setRenderCustomMessages: vi.fn(),
    },
  }),
}));
import { renderToStaticMarkup } from "react-dom/server";
import { CollaborationChoiceBubble } from "../components/chat/collaboration-responses";
import {
  collaborationResponseLayout,
  formatCollaborationResponseDisplay,
} from "../components/chat/collaboration-response-display";

describe("formatCollaborationResponseDisplay", () => {
  it("maps ask_user option values to labels", () => {
    expect(
      formatCollaborationResponseDisplay("ask_user", "list_data_sources", [
        { label: "list_data_sources", value: "list_data_sources", description: "x" },
      ]),
    ).toBe("list_data_sources");
  });

  it("formats submit_plan approval", () => {
    expect(
      formatCollaborationResponseDisplay("submit_plan", { action: "approved" }),
    ).toBe("Plan approved");
  });

  it("formats submit_plan rejection with feedback", () => {
    expect(
      formatCollaborationResponseDisplay("submit_plan", {
        action: "rejected",
        feedback: "需要调整计划",
      }),
    ).toBe("Plan rejected: 需要调整计划");
  });

  it("preserves free-text ask_user answers", () => {
    expect(formatCollaborationResponseDisplay("ask_user", "  继续分析  ")).toBe("继续分析");
  });

  it("maps ask_user multi-select values to option labels", () => {
    expect(
      formatCollaborationResponseDisplay("ask_user", ["schema", "sql"], [
        { label: "Inspect schema", value: "schema" },
        { label: "执行 SQL", value: "sql" },
      ]),
    ).toBe("Inspect schema, 执行 SQL");
  });

  it("formats submit_plan rejection without feedback", () => {
    expect(
      formatCollaborationResponseDisplay("submit_plan", { action: "rejected" }),
    ).toBe("Plan rejected");
  });

  it("renders collaboration prompts as inline assistant-side records", () => {
    expect(collaborationResponseLayout("ask_user")).toEqual({
      recapSide: "assistant",
      choiceSide: "inline",
      planRenderer: undefined,
    });
  });

  it("renders submitted plans as assistant-side markdown recap", () => {
    expect(collaborationResponseLayout("submit_plan")).toEqual({
      recapSide: "assistant",
      choiceSide: "inline",
      planRenderer: "markdown",
    });
  });

  it("shows the question before the accepted answer in the recap bubble", () => {
    const html = renderToStaticMarkup(
      createElement(CollaborationChoiceBubble, {
        response: {
          id: "t:tc-ask",
          threadId: "t",
          toolCallId: "tc-ask",
          toolName: "ask_user",
          question: "请问您对当前测试流程是否满意？",
          displayText: "已确认满意",
          createdAt: 1,
        },
      }),
    );

    expect(html.indexOf("answered question")).toBeGreaterThan(-1);
    expect(html.indexOf("已确认满意")).toBeGreaterThan(-1);
    expect(html.indexOf("answered question")).toBeLessThan(html.indexOf("已确认满意"));
  });
});
