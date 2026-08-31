import { describe, expect, it } from "vitest";

import {
  ensureNonEmptyMessageContentPrompt,
  shouldApplyNonEmptyMessageContentCompat,
} from "../../../../../../packages/agent-runtime/src/provider-compat/non-empty-message-content-compat";

describe("non-empty message content compat", () => {
  it("enables compat only for providers that require non-empty message content", () => {
    expect(shouldApplyNonEmptyMessageContentCompat({
      prompt_compat: { requires_non_empty_message_content: true },
    })).toBe(true);
    expect(shouldApplyNonEmptyMessageContentCompat({})).toBe(false);
  });

  it("adds placeholder text to assistant messages that only contain tool calls", () => {
    const prompt = ensureNonEmptyMessageContentPrompt([
      {
        role: "assistant",
        content: [
          {
            type: "tool-call",
            toolCallId: "call-1",
            toolName: "read_file",
            input: { path: "test.txt" },
          },
        ],
      },
    ]);

    expect(prompt[0]?.content[0]).toEqual({ type: "text", text: "." });
  });

  it("adds placeholder text to assistant messages that only contain reasoning", () => {
    const prompt = ensureNonEmptyMessageContentPrompt([
      {
        role: "assistant",
        content: [{ type: "reasoning", text: "thinking..." }],
      },
    ]);

    expect(prompt[0]?.content[0]).toEqual({ type: "text", text: "." });
  });

  it("fills empty tool result outputs", () => {
    const prompt = ensureNonEmptyMessageContentPrompt([
      {
        role: "tool",
        content: [
          {
            type: "tool-result",
            toolCallId: "call-1",
            toolName: "list_files",
            output: { type: "text", value: "" },
          },
        ],
      },
    ]);

    expect(prompt[0]?.content[0]).toMatchObject({
      output: { type: "text", value: "(empty tool result)" },
    });
  });
});
