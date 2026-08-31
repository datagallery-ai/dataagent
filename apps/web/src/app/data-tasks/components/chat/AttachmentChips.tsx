"use client";

import {
  formatFileSize,
  isAttachmentUnsupported,
  type ChatAttachment,
} from "./chat-attachments";

export function AttachmentChips({
  attachments,
  onRemove,
}: {
  attachments: ChatAttachment[];
  onRemove: (id: string) => void;
}) {
  if (attachments.length === 0) return null;
  return (
    <div className="flex flex-wrap gap-1.5 pb-1">
      {attachments.map((att) => {
        const unsupported = isAttachmentUnsupported(att);
        return (
          <span
            key={att.id}
            className="inline-flex max-w-[220px] items-center gap-1.5 rounded-lg border border-border bg-surface-subtle px-2 py-1 text-xs text-foreground"
            title={unsupported ? "Will be sent with the message after backend support lands" : att.filename}
          >
            <AttachmentIcon modality={att.type} />
            <span className="truncate">{att.filename ?? "Attachment"}</span>
            {typeof att.size === "number" && (
              <span className="shrink-0 text-muted-light">
                {formatFileSize(att.size)}
              </span>
            )}
            {att.status === "uploading" && (
              <span className="h-3 w-3 shrink-0 animate-spin rounded-full border border-border border-t-primary" />
            )}
            {unsupported && (
              <span className="shrink-0 rounded bg-amber-100 px-1 text-[10px] font-medium text-amber-700">
                Backend unsupported
              </span>
            )}
            <button
              type="button"
              aria-label="Remove attachment"
              onClick={() => onRemove(att.id)}
              className="shrink-0 cursor-pointer text-muted-light hover:text-foreground"
            >
              <CloseIcon />
            </button>
          </span>
        );
      })}
    </div>
  );
}

function AttachmentIcon({ modality }: { modality: ChatAttachment["type"] }) {
  return (
    <svg
      viewBox="0 0 20 20"
      className="h-3.5 w-3.5 shrink-0 text-muted"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      aria-hidden
    >
      {modality === "image" ? (
        <>
          <rect x="3" y="3" width="14" height="14" rx="2" />
          <circle cx="7.5" cy="7.5" r="1.5" />
          <path d="m4 15 4-4 3 3 2-2 3 3" />
        </>
      ) : (
        <>
          <path d="M6 2.5h6l4 4V17a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V3.5a1 1 0 0 1 1-1Z" />
          <path d="M12 2.5v4h4" />
        </>
      )}
    </svg>
  );
}

function CloseIcon() {
  return (
    <svg
      viewBox="0 0 20 20"
      className="h-3.5 w-3.5"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      aria-hidden
    >
      <path d="m6 6 8 8M14 6l-8 8" />
    </svg>
  );
}
