import { describe, expect, it, vi } from "vitest";
import type { Attachment } from "@copilotkit/shared";
import {
  buildMessageContent,
  createChatOnUpload,
  isAttachmentUnsupported,
  isImageMime,
  UNSUPPORTED_METADATA_KEY,
} from "../components/chat/chat-attachments";

function imageFile(): File {
  return new File([new Uint8Array([1, 2, 3])], "a.png", { type: "image/png" });
}
function csvFile(): File {
  return new File(["a,b\n1,2"], "data.csv", { type: "text/csv" });
}
function att(over: Partial<Attachment>): Attachment {
  return {
    id: "1",
    type: "image",
    source: { type: "data", value: "x", mimeType: "image/png" } as Attachment["source"],
    filename: "a.png",
    status: "ready",
    ...over,
  };
}

describe("chat-attachments", () => {
  it("classifies image mime", () => {
    expect(isImageMime("image/png")).toBe(true);
    expect(isImageMime("text/csv")).toBe(false);
  });

  it("image upload inlines base64; marks unsupported when imageInput off", async () => {
    const onUpload = createChatOnUpload({
      capabilities: () => ({ imageInput: false, fileUpload: false }),
      readBase64: vi.fn().mockResolvedValue("BASE64"),
      uploadDataFile: vi.fn(),
    });
    const result = await onUpload(imageFile());
    expect(result).toMatchObject({ type: "data", value: "BASE64" });
    expect(result.metadata?.[UNSUPPORTED_METADATA_KEY]).toBe(true);
  });

  it("data file without capability is placeholder, no upload, no base64", async () => {
    const readBase64 = vi.fn();
    const uploadDataFile = vi.fn();
    const onUpload = createChatOnUpload({
      capabilities: () => ({ imageInput: true, fileUpload: false }),
      readBase64,
      uploadDataFile,
    });
    const result = await onUpload(csvFile());
    expect(result).toMatchObject({ type: "url", value: "" });
    expect(result.metadata?.[UNSUPPORTED_METADATA_KEY]).toBe(true);
    expect(readBase64).not.toHaveBeenCalled();
    expect(uploadDataFile).not.toHaveBeenCalled();
  });

  it("data file with capability uploads to backend path", async () => {
    const onUpload = createChatOnUpload({
      capabilities: () => ({ imageInput: true, fileUpload: true }),
      readBase64: vi.fn(),
      uploadDataFile: vi.fn().mockResolvedValue({
        path: "uploads/data.csv",
        mimeType: "text/csv",
        size: 9,
      }),
    });
    const result = await onUpload(csvFile());
    expect(result).toMatchObject({ type: "url", value: "uploads/data.csv" });
    expect(result.metadata?.[UNSUPPORTED_METADATA_KEY]).toBeUndefined();
  });

  it("buildMessageContent drops unsupported attachments but keeps text", () => {
    const content = buildMessageContent("hi", [
      att({ id: "ok" }),
      att({ id: "bad", metadata: { [UNSUPPORTED_METADATA_KEY]: true } }),
    ]);
    expect(content[0]).toEqual({ type: "text", text: "hi" });
    expect(content).toHaveLength(2);
  });

  it("isAttachmentUnsupported reads metadata flag", () => {
    expect(
      isAttachmentUnsupported(att({ metadata: { [UNSUPPORTED_METADATA_KEY]: true } })),
    ).toBe(true);
    expect(isAttachmentUnsupported(att({}))).toBe(false);
  });
});
