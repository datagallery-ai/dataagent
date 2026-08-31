import type { InputContent } from "@ag-ui/core";
import { getBackendCapabilities } from "../../../../lib/config-api/capabilities";
export type ChatAttachmentUploadResult = {
  type: "data" | "url";
  value: string;
  mimeType?: string;
  metadata?: Record<string, unknown>;
};


export const CHAT_ATTACHMENT_MAX_BYTES = 20 * 1024 * 1024;

export const CHAT_ATTACHMENT_ACCEPT = [
  "image/png",
  "image/jpeg",
  "image/webp",
  "image/gif",
  ".csv",
  ".tsv",
  ".xlsx",
  ".json",
  ".parquet",
  ".txt",
  ".pdf",
  "text/csv",
  "text/tab-separated-values",
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  "application/json",
  "text/plain",
  "application/pdf",
].join(",");

export type ChatAttachmentCapabilities = {
  imageInput: boolean;
  fileUpload: boolean;
};

export type ChatAttachment = {
  id: string;
  type: string;
  source?: unknown;
  filename?: string;
  size?: number;
  status?: string;
  metadata?: Record<string, unknown>;
};

export const UNSUPPORTED_METADATA_KEY = "__chatUnsupported";

export function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function readFileAsBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",")[1] ?? "");
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

export function isImageMime(mimeType: string): boolean {
  return mimeType.startsWith("image/");
}

export type UploadDataFile = (
  file: File,
) => Promise<{ path: string; mimeType: string; size: number }>;

export function createChatOnUpload(deps: {
  capabilities: () => ChatAttachmentCapabilities;
  readBase64: (file: File) => Promise<string>;
  uploadDataFile: UploadDataFile;
}) {
  return async (file: File): Promise<ChatAttachmentUploadResult> => {
    const caps = deps.capabilities();
    if (isImageMime(file.type)) {
      const value = await deps.readBase64(file);
      return {
        type: "data",
        value,
        mimeType: file.type,
        metadata: caps.imageInput ? {} : { [UNSUPPORTED_METADATA_KEY]: true },
      };
    }
    if (!caps.fileUpload) {
      return {
        type: "url",
        value: "",
        mimeType: file.type,
        metadata: { [UNSUPPORTED_METADATA_KEY]: true },
      };
    }
    const uploaded = await deps.uploadDataFile(file);
    return {
      type: "url",
      value: uploaded.path,
      mimeType: uploaded.mimeType,
      metadata: {},
    };
  };
}

export function isAttachmentUnsupported(att: ChatAttachment): boolean {
  if (att.metadata?.[UNSUPPORTED_METADATA_KEY] !== true) {
    return false;
  }
  const caps = getBackendCapabilities();
  const mimeType = att.type ?? "";
  if (isImageMime(mimeType)) {
    return !caps["chat.imageInput"];
  }
  return !caps["chat.fileUpload"];
}

export function attachmentToInputContent(att: ChatAttachment): InputContent {
  const metadata = {
    ...(att.filename ? { filename: att.filename } : {}),
    ...att.metadata,
  };
  return { type: att.type, source: att.source, metadata } as InputContent;
}

export function buildMessageContent(
  text: string,
  attachments: ChatAttachment[],
): InputContent[] {
  const sendable = attachments.filter((att) => !isAttachmentUnsupported(att));
  return [
    { type: "text", text } as InputContent,
    ...sendable.map(attachmentToInputContent),
  ];
}
