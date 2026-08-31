import { describe, expect, it, vi } from "vitest";
import { buildChatAddActions } from "../components/chat/chat-add-actions";

describe("chat add actions", () => {
  it("puts file upload behind the unified plus menu", () => {
    const openFilePicker = vi.fn();
    const actions = buildChatAddActions({ openFilePicker });

    expect(actions).toHaveLength(1);
    expect(actions[0]).toMatchObject({
      id: "upload-file",
      label: "Upload file",
      description: "Add an image, table, or document to this chat",
    });

    actions[0].run();
    expect(openFilePicker).toHaveBeenCalledOnce();
  });
});
