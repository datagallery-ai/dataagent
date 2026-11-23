import { describe, expect, it } from "vitest";
import { resolveChatInputWidth } from "../chat-input-layout";
import {
  CHAT_INPUT_HORIZONTAL_PADDING,
  CHAT_INPUT_MIN_WIDTH,
  CHAT_INPUT_PREFERRED_WIDTH,
} from "../workspace-layout";

describe("resolveChatInputWidth", () => {
  it("returns the preferred width when the chat column is wide enough", () => {
    expect(resolveChatInputWidth(1200)).toBe(CHAT_INPUT_PREFERRED_WIDTH);
    expect(resolveChatInputWidth(
      CHAT_INPUT_PREFERRED_WIDTH + CHAT_INPUT_HORIZONTAL_PADDING,
    )).toBe(CHAT_INPUT_PREFERRED_WIDTH);
  });

  it("shrinks below the preferred width when the chat column is narrower", () => {
    expect(resolveChatInputWidth(600)).toBe(600 - CHAT_INPUT_HORIZONTAL_PADDING);
  });

  it("never drops below the minimum width", () => {
    expect(resolveChatInputWidth(300)).toBe(CHAT_INPUT_MIN_WIDTH);
  });
});
