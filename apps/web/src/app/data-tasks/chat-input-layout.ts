import {
  CHAT_INPUT_HORIZONTAL_PADDING,
  CHAT_INPUT_MIN_WIDTH,
  CHAT_INPUT_PREFERRED_WIDTH,
} from "./workspace-layout";

export function resolveChatInputWidth(chatColumnWidth: number): number {
  const available = chatColumnWidth - CHAT_INPUT_HORIZONTAL_PADDING;
  return Math.max(
    CHAT_INPUT_MIN_WIDTH,
    Math.min(CHAT_INPUT_PREFERRED_WIDTH, available),
  );
}
