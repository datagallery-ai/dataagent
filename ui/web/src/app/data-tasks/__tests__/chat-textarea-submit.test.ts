import { describe, expect, it, vi } from "vitest";
import { shouldSubmitChatTextareaOnEnter } from "../chat-textarea-submit";

function enterEvent(shiftKey = false, composing = false) {
  return {
    key: "Enter",
    shiftKey,
    keyCode: composing ? 229 : 13,
    nativeEvent: { isComposing: composing },
    preventDefault: vi.fn(),
  };
}

describe("shouldSubmitChatTextareaOnEnter", () => {
  it("accepts Enter without Shift", () => {
    expect(shouldSubmitChatTextareaOnEnter(enterEvent())).toBe(true);
  });

  it("rejects Shift+Enter", () => {
    expect(shouldSubmitChatTextareaOnEnter(enterEvent(true))).toBe(false);
  });

  it("rejects Enter during IME composition", () => {
    expect(shouldSubmitChatTextareaOnEnter(enterEvent(false, true))).toBe(false);
  });
});
