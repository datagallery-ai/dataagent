"use client";

import {
  CopilotChatInput,
  type CopilotChatInputProps,
  type UseAttachmentsReturn,
} from "@copilotkit/react-core/v2";
import type { EvidenceRef } from "../../../../lib/evidence-ref";
import { useCallback, useEffect, useRef, useState } from "react";
import type { CSSProperties, ReactNode } from "react";
import type {
  FileMentionResource,
  MentionResource,
  PerRunMentionKind,
  PerRunFileSelection,
  PerRunSelection,
  SessionStartedHints,
  WorkspaceConfigItem,
} from "../../data-task-state";
import { MentionChips, useMentionAutocomplete } from "./chat-mentions";
import { useT } from "../../../../i18n/locale-context";
import { AttachmentChips } from "./AttachmentChips";
import { CHAT_ATTACHMENT_ACCEPT } from "./chat-attachments";
import { useChatTextareaAutoresize, scheduleChatTextareaResize } from "./use-chat-textarea-autoresize";
import { resolveChatInputWidth } from "../../chat-input-layout";
import { evidenceChipLabel, evidenceChipTooltip } from "../../evidence";
import { useDataTaskChatInputBindings } from "./DataTaskChatInputBindingsContext";
import type { ChatSession, WorkspaceConfigStore } from "../../data-task-state";
import type { QueuedChatPrompt } from "./queued-chat-runs";
import { createChatTextareaKeyDownCaptureHandler } from "../../chat-textarea-submit";

type DataTaskChatInputProps = CopilotChatInputProps & {
  llmOptions: WorkspaceConfigItem[];
  activeLlmId: string | null;
  onActiveLlmChange: (llmId: string) => void;
  onOpenLlmConfig?: () => void;
  mentionResources: MentionResource[];
  perRunSelection: PerRunSelection;
  onTogglePerRunMention: (kind: PerRunMentionKind, id: string) => void;
  onRemovePerRunMention: (kind: PerRunMentionKind, id: string) => void;
  onClearPerRunMentions: () => void;
  fileMentionResources: FileMentionResource[];
  perRunFiles: PerRunFileSelection;
  onTogglePerRunFileMention: (resource: FileMentionResource) => void;
  onRemovePerRunFileMention: (resource: FileMentionResource) => void;
  onClearPerRunFileMentions: () => void;
  selectedEvidenceRefs: EvidenceRef[];
  onRemoveEvidenceRef: (id: string) => void;
  onClearEvidenceRefs: () => void;
  workspaceConfig: WorkspaceConfigStore;
  activeSession: ChatSession | null;
  sessionStartedHints?: SessionStartedHints;
  onToggleSessionResource: (kind: PerRunMentionKind, id: string) => void;
  attachmentsApi: UseAttachmentsReturn;
  queuedPrompts?: QueuedChatPrompt[];
  onEditQueuedPrompt?: (id: string, text: string) => void;
  onDeleteQueuedPrompt?: (id: string) => void;
  onSendQueuedPromptNow?: (id: string) => void;
  /** Current run or client-side send/upload failure shown above the composer. */
  submitError?: string | null;
  /** Session lock / remote-busy prompt rendered above the composer. */
  sessionLockSlot?: ReactNode;
};

export function DataTaskChatInput({
  llmOptions,
  activeLlmId,
  onActiveLlmChange,
  onOpenLlmConfig,
  mentionResources,
  perRunSelection,
  onTogglePerRunMention,
  onRemovePerRunMention,
  onClearPerRunMentions,
  fileMentionResources,
  perRunFiles,
  onTogglePerRunFileMention,
  onRemovePerRunFileMention,
  onClearPerRunFileMentions,
  selectedEvidenceRefs,
  onRemoveEvidenceRef,
  onClearEvidenceRefs,
  workspaceConfig,
  activeSession,
  sessionStartedHints,
  onToggleSessionResource,
  attachmentsApi,
  queuedPrompts = [],
  onEditQueuedPrompt,
  onDeleteQueuedPrompt,
  onSendQueuedPromptNow,
  submitError = null,
  sessionLockSlot = null,
  textArea,
  onChange,
  ...props
}: DataTaskChatInputProps) {
  const t = useT();
  const textAreaSlot =
    textArea && typeof textArea === "object" && !Array.isArray(textArea)
      ? {
          ...textArea,
          placeholder:
            (textArea as { placeholder?: string }).placeholder ??
            t("chatInput.placeholder"),
          style: {
            overflow: "hidden",
            resize: "none" as const,
            ...(textArea as { style?: CSSProperties }).style,
          },
        }
      : {
          placeholder: t("chatInput.placeholder"),
          style: {
            overflow: "hidden",
            resize: "none" as const,
          },
        };

  return (
    <CopilotChatInput
      {...props}
      onChange={(value) => {
        onChange?.(value);
        requestAnimationFrame(scheduleChatTextareaResize);
      }}
      textArea={textAreaSlot}
    >
      {(slots) => (
        <DataTaskChatInputLayout
          {...slots}
          activeLlmId={activeLlmId}
          llmOptions={llmOptions}
          onActiveLlmChange={onActiveLlmChange}
          onOpenLlmConfig={onOpenLlmConfig}
          mentionResources={mentionResources}
          perRunSelection={perRunSelection}
          onTogglePerRunMention={onTogglePerRunMention}
          onRemovePerRunMention={onRemovePerRunMention}
          onClearPerRunMentions={onClearPerRunMentions}
          fileMentionResources={fileMentionResources}
          perRunFiles={perRunFiles}
          onTogglePerRunFileMention={onTogglePerRunFileMention}
          onRemovePerRunFileMention={onRemovePerRunFileMention}
          onClearPerRunFileMentions={onClearPerRunFileMentions}
          selectedEvidenceRefs={selectedEvidenceRefs}
          onRemoveEvidenceRef={onRemoveEvidenceRef}
          onClearEvidenceRefs={onClearEvidenceRefs}
          workspaceConfig={workspaceConfig}
          activeSession={activeSession}
          sessionStartedHints={sessionStartedHints}
          onToggleSessionResource={onToggleSessionResource}
          attachmentsApi={attachmentsApi}
          queuedPrompts={queuedPrompts}
          onEditQueuedPrompt={onEditQueuedPrompt}
          onDeleteQueuedPrompt={onDeleteQueuedPrompt}
          onSendQueuedPromptNow={onSendQueuedPromptNow}
          submitError={submitError}
          sessionLockSlot={sessionLockSlot}
        />
      )}
    </CopilotChatInput>
  );
}

function DataTaskChatInputLayout({
  textArea,
  sendButton,
  startTranscribeButton,
  cancelTranscribeButton,
  finishTranscribeButton,
  audioRecorder,
  disclaimer,
  mode = "input",
  showDisclaimer,
  containerRef,
  positioning = "static",
  keyboardHeight = 0,
  bottomAnchored = false,
  mentionResources,
  perRunSelection,
  onTogglePerRunMention,
  onRemovePerRunMention,
  onClearPerRunMentions,
  fileMentionResources,
  perRunFiles,
  onTogglePerRunFileMention,
  onRemovePerRunFileMention,
  onClearPerRunFileMentions,
  selectedEvidenceRefs,
  onRemoveEvidenceRef,
  onClearEvidenceRefs,
  attachmentsApi,
  queuedPrompts,
  onEditQueuedPrompt,
  onDeleteQueuedPrompt,
  onSendQueuedPromptNow,
  submitError = null,
  sessionLockSlot = null,
}: {
  textArea: ReactNode;
  sendButton: ReactNode;
  addMenuButton: ReactNode;
  startTranscribeButton?: ReactNode;
  cancelTranscribeButton?: ReactNode;
  finishTranscribeButton?: ReactNode;
  audioRecorder?: ReactNode;
  disclaimer: ReactNode;
  mode?: "input" | "transcribe" | "processing";
  showDisclaimer?: boolean;
  containerRef?: React.Ref<HTMLDivElement>;
  positioning?: "static" | "absolute";
  keyboardHeight?: number;
  bottomAnchored?: boolean;
  llmOptions: WorkspaceConfigItem[];
  activeLlmId: string | null;
  onActiveLlmChange: (llmId: string) => void;
  onOpenLlmConfig?: () => void;
  mentionResources: MentionResource[];
  perRunSelection: PerRunSelection;
  onTogglePerRunMention: (kind: PerRunMentionKind, id: string) => void;
  onRemovePerRunMention: (kind: PerRunMentionKind, id: string) => void;
  onClearPerRunMentions: () => void;
  fileMentionResources: FileMentionResource[];
  perRunFiles: PerRunFileSelection;
  onTogglePerRunFileMention: (resource: FileMentionResource) => void;
  onRemovePerRunFileMention: (resource: FileMentionResource) => void;
  onClearPerRunFileMentions: () => void;
  selectedEvidenceRefs: EvidenceRef[];
  onRemoveEvidenceRef: (id: string) => void;
  onClearEvidenceRefs: () => void;
  workspaceConfig: WorkspaceConfigStore;
  activeSession: ChatSession | null;
  sessionStartedHints?: SessionStartedHints;
  onToggleSessionResource: (kind: PerRunMentionKind, id: string) => void;
  attachmentsApi: UseAttachmentsReturn;
  queuedPrompts: QueuedChatPrompt[];
  onEditQueuedPrompt?: (id: string, text: string) => void;
  onDeleteQueuedPrompt?: (id: string) => void;
  onSendQueuedPromptNow?: (id: string) => void;
  submitError?: string | null;
  sessionLockSlot?: ReactNode;
}) {
  const t = useT();
  const { chatColumnWidth, draftPromptRequest, onDraftPromptConsumed } =
    useDataTaskChatInputBindings();
  const chatInputWidth = resolveChatInputWidth(chatColumnWidth);
  const mention = useMentionAutocomplete({
    resources: mentionResources,
    fileResources: fileMentionResources,
    selection: perRunSelection,
    fileSelection: perRunFiles,
    onToggle: onTogglePerRunMention,
    onToggleFile: onTogglePerRunFileMention,
    refreshToken: mode,
  });
  const autoresizeRef = useChatTextareaAutoresize(mode);
  const columnRef = useCallback(
    (node: HTMLDivElement | null) => {
      mention.columnRef(node);
      autoresizeRef(node);
      attachmentsApi.containerRef.current = node;
    },
    [mention.columnRef, autoresizeRef, attachmentsApi.containerRef],
  );

  useEffect(() => {
    if (!draftPromptRequest || mode !== "input") return;

    const container = attachmentsApi.containerRef.current;
    const textarea =
      container?.querySelector<HTMLTextAreaElement>(
        "[data-testid=copilot-chat-textarea]",
      ) ?? container?.querySelector<HTMLTextAreaElement>("textarea");
    if (!textarea) return;

    const valueSetter = Object.getOwnPropertyDescriptor(
      window.HTMLTextAreaElement.prototype,
      "value",
    )?.set;
    if (valueSetter) {
      valueSetter.call(textarea, draftPromptRequest.text);
    } else {
      textarea.value = draftPromptRequest.text;
    }
    textarea.dispatchEvent(new Event("input", { bubbles: true }));
    textarea.focus({ preventScroll: true });
    textarea.setSelectionRange(
      draftPromptRequest.text.length,
      draftPromptRequest.text.length,
    );
    requestAnimationFrame(scheduleChatTextareaResize);
    onDraftPromptConsumed(draftPromptRequest.id);
  }, [
    attachmentsApi.containerRef,
    draftPromptRequest,
    mode,
    onDraftPromptConsumed,
  ]);

  const focusTextArea = (textarea: HTMLTextAreaElement) => {
    textarea.focus({ preventScroll: true });
    const end = textarea.value.length;
    textarea.setSelectionRange(end, end);
  };

  const handleTextAreaKeyDownCapture = createChatTextareaKeyDownCaptureHandler(
    () => {
      const root = attachmentsApi.containerRef.current?.closest(
        '[data-testid="copilot-chat-input"]',
      );
      const sendButton = root?.querySelector<HTMLButtonElement>(
        '[data-testid="copilot-send-button"]',
      );
      sendButton?.click();
    },
  );

  const handleTextAreaMouseDown = (
    event: React.MouseEvent<HTMLDivElement>,
  ) => {
    const target = event.target as HTMLElement;
    if (target.tagName === "TEXTAREA") return;

    const textarea = event.currentTarget.querySelector("textarea");
    if (!textarea || mode !== "input") return;

    event.preventDefault();
    focusTextArea(textarea);
  };

  return (
    <div
      data-copilotkit
      ref={containerRef}
      className={[
        "pointer-events-none relative z-20",
        positioning === "absolute"
          ? "absolute bottom-0 left-0 right-0 bg-gradient-to-t from-surface from-60% to-transparent pt-8"
          : "",
      ].join(" ")}
      style={{
        transform:
          keyboardHeight > 0 ? `translateY(-${keyboardHeight}px)` : undefined,
        transition: "transform 0.2s ease-out",
        paddingBottom:
          "calc(0.75rem + var(--copilotkit-license-banner-offset, 0px))",
      }}
    >
      <div
        className="mx-auto w-full"
        style={{ width: chatInputWidth, maxWidth: "100%" }}
      >
        <QueuedPromptStrip
          prompts={queuedPrompts}
          onEdit={onEditQueuedPrompt}
          onDelete={onDeleteQueuedPrompt}
          onSendNow={onSendQueuedPromptNow}
        />
        {sessionLockSlot}
        {submitError ? (
          <div
            role="alert"
            data-testid="chat-submit-error"
            className="pointer-events-auto mb-2 rounded-lg bg-danger/8 px-3 py-2 text-xs leading-5 text-danger"
          >
            {submitError}
          </div>
        ) : null}
        <div
          data-guide-id="chat-input"
          data-testid="copilot-chat-input"
          onDragOver={attachmentsApi.handleDragOver}
          onDragLeave={attachmentsApi.handleDragLeave}
          onDrop={attachmentsApi.handleDrop}
          className="pointer-events-auto relative flex w-full flex-col overflow-visible rounded-[14px] bg-surface shadow-[var(--shadow-float)] transition-shadow duration-150 focus-within:shadow-[0_6px_24px_rgb(0_0_0/0.07),0_0_0_1px_var(--border-strong),0_0_0_4px_var(--focus-ring)]"
        >
          {attachmentsApi.dragOver && (
            <div className="pointer-events-none absolute inset-0 z-30 grid place-items-center rounded-[14px] border border-dashed border-border-strong bg-surface-subtle/95 text-sm font-medium text-foreground">
              {t("chatInput.dropFiles")}
            </div>
          )}
          <div className="w-full px-3.5 pt-2">
            <div
              ref={columnRef}
              className="relative flex w-full min-w-0 flex-col gap-1"
            >
              {mode === "input" ? mention.menu : null}
              {mode === "transcribe" ? (
                audioRecorder
              ) : mode === "processing" ? (
                <div className="flex w-full items-center justify-center px-5 py-3">
                  <span className="h-[26px] w-[26px] animate-spin rounded-full border-2 border-border border-t-primary" />
                </div>
              ) : (
                <>
                  <AttachmentChips
                    attachments={attachmentsApi.attachments}
                    onRemove={attachmentsApi.removeAttachment}
                  />
                  <MentionChips
                    resources={mentionResources}
                    fileResources={fileMentionResources}
                    selection={perRunSelection}
                    fileSelection={perRunFiles}
                    onRemove={onRemovePerRunMention}
                    onRemoveFile={onRemovePerRunFileMention}
                    onClear={onClearPerRunMentions}
                    onClearFiles={onClearPerRunFileMentions}
                  />
                  <EvidenceChips
                    refs={selectedEvidenceRefs}
                    onRemove={onRemoveEvidenceRef}
                    onClear={onClearEvidenceRefs}
                  />
                  <div
                    onKeyDownCapture={handleTextAreaKeyDownCapture}
                    className={[
                      "w-full cursor-text",
                      "[&_[data-testid=copilot-chat-textarea]]:block",
                      "[&_[data-testid=copilot-chat-textarea]]:w-full",
                      "[&_[data-testid=copilot-chat-textarea]]:resize-none",
                      "[&_[data-testid=copilot-chat-textarea]]:bg-transparent",
                      "[&_[data-testid=copilot-chat-textarea]]:!px-0.5",
                      "[&_[data-testid=copilot-chat-textarea]]:!py-1.5",
                      "[&_[data-testid=copilot-chat-textarea]]:text-[14.5px]",
                      "[&_[data-testid=copilot-chat-textarea]]:leading-6",
                      "[&_[data-testid=copilot-chat-textarea]]:placeholder:text-muted-light",
                      "[&_[data-testid=copilot-chat-textarea]]:outline-none",
                      "[&_[data-testid=copilot-chat-textarea]]:overflow-y-hidden",
                      "[&_[data-testid=copilot-chat-textarea]:not(:focus)]:caret-transparent",
                    ].join(" ")}
                    onMouseDown={handleTextAreaMouseDown}
                  >
                    {textArea}
                  </div>
                </>
              )}
            </div>
            {mode === "transcribe" && (
              <div className="mt-2 flex items-center justify-end gap-1">
                {cancelTranscribeButton}
                {finishTranscribeButton}
              </div>
            )}
          </div>
          {mode !== "transcribe" && (
            <div className="flex items-center justify-end gap-2 px-2 pb-2">
              <input
                ref={attachmentsApi.fileInputRef}
                type="file"
                multiple
                accept={CHAT_ATTACHMENT_ACCEPT}
                className="hidden"
                onChange={attachmentsApi.handleFileUpload}
              />
              <div className="shrink-0">{sendButton}</div>
            </div>
          )}
        </div>
      </div>
      {showDisclaimer ? disclaimer : null}
    </div>
  );
}

function QueuedPromptStrip({
  prompts,
  onEdit,
  onDelete,
  onSendNow,
}: {
  prompts: QueuedChatPrompt[];
  onEdit?: (id: string, text: string) => void;
  onDelete?: (id: string) => void;
  onSendNow?: (id: string) => void;
}) {
  const t = useT();
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");

  if (prompts.length === 0) {
    return null;
  }

  return (
    <div className="pointer-events-auto mb-2 grid gap-1 px-0.5">
      {prompts.map((prompt, index) => {
        const isEditing = editingId === prompt.id;
        const isInterrupting = prompt.status === "interrupting";
        return (
          <div
            key={prompt.id}
            className={[
              "flex min-w-0 items-center gap-2 rounded-lg px-2 py-1.5 text-xs shadow-[var(--shadow-card)]",
              isInterrupting
                ? "bg-surface-hover text-foreground"
                : "bg-surface text-muted",
            ].join(" ")}
          >
            <span className="grid h-5 min-w-5 place-items-center rounded-md bg-surface-subtle text-[11px] font-semibold text-muted-light tabular">
              {index + 1}
            </span>
            {isEditing ? (
              <input
                value={draft}
                autoFocus
                onChange={(event) => setDraft(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter") {
                    const next = draft.trim();
                    if (next) {
                      onEdit?.(prompt.id, next);
                      setEditingId(null);
                    }
                  } else if (event.key === "Escape") {
                    setEditingId(null);
                  }
                }}
                className="min-w-0 flex-1 rounded-md border border-border bg-surface px-2 py-1 text-xs text-foreground outline-none focus:border-primary"
              />
            ) : (
              <span className="min-w-0 flex-1 truncate text-foreground">
                {prompt.text}
              </span>
            )}
            {prompt.attachments.length > 0 ? (
              <span className="shrink-0 rounded bg-surface px-1.5 py-0.5 text-[11px] text-muted-light">
                +{prompt.attachments.length}
              </span>
            ) : null}
            {isEditing ? (
              <>
                <button
                  type="button"
                  title={t("chatInput.saveQueued")}
                  aria-label={t("chatInput.saveQueued")}
                  onClick={() => {
                    const next = draft.trim();
                    if (!next) return;
                    onEdit?.(prompt.id, next);
                    setEditingId(null);
                  }}
                  className="grid h-6 w-6 shrink-0 cursor-pointer place-items-center rounded-md text-muted transition-colors hover:bg-surface hover:text-primary"
                >
                  <CheckIcon />
                </button>
                <button
                  type="button"
                  title={t("chatInput.cancelQueued")}
                  aria-label={t("chatInput.cancelQueued")}
                  onClick={() => setEditingId(null)}
                  className="grid h-6 w-6 shrink-0 cursor-pointer place-items-center rounded-md text-muted transition-colors hover:bg-surface hover:text-foreground"
                >
                  <XIcon />
                </button>
              </>
            ) : (
              <>
                <button
                  type="button"
                  title={t("chatInput.editQueued")}
                  aria-label={t("chatInput.editQueued")}
                  onClick={() => {
                    setEditingId(prompt.id);
                    setDraft(prompt.text);
                  }}
                  className="grid h-6 w-6 shrink-0 cursor-pointer place-items-center rounded-md text-muted transition-colors hover:bg-surface hover:text-foreground"
                >
                  <PencilIcon />
                </button>
                <button
                  type="button"
                  title={t("chatInput.sendNow")}
                  aria-label={t("chatInput.sendQueuedNow")}
                  disabled={isInterrupting}
                  onClick={() => onSendNow?.(prompt.id)}
                  className="grid h-6 w-6 shrink-0 cursor-pointer place-items-center rounded-md text-muted transition-colors hover:bg-surface hover:text-primary disabled:cursor-default disabled:opacity-50"
                >
                  <BoltIcon />
                </button>
                <button
                  type="button"
                  title={t("chatInput.deleteQueued")}
                  aria-label={t("chatInput.deleteQueued")}
                  onClick={() => onDelete?.(prompt.id)}
                  className="grid h-6 w-6 shrink-0 cursor-pointer place-items-center rounded-md text-muted transition-colors hover:bg-surface hover:text-rose-600"
                >
                  <TrashIcon />
                </button>
              </>
            )}
          </div>
        );
      })}
    </div>
  );
}

function EvidenceChips({
  refs,
  onRemove,
  onClear,
}: {
  refs: EvidenceRef[];
  onRemove: (id: string) => void;
  onClear: () => void;
}) {
  if (refs.length === 0) {
    return null;
  }
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-1.5 py-1">
      {refs.map((ref) => (
        <span
          key={ref.id}
          title={evidenceChipTooltip(ref)}
          className="inline-flex h-[26px] max-w-[220px] items-center gap-1.5 rounded-[7px] bg-surface-hover pl-2 pr-1 text-xs text-foreground"
        >
          <span className="min-w-0 truncate">{evidenceChipLabel(ref)}</span>
          <button
            type="button"
            aria-label={`Remove ${ref.label}`}
            title="Remove evidence"
            onClick={() => onRemove(ref.id)}
            className="grid h-[18px] w-[18px] shrink-0 cursor-pointer place-items-center rounded-[5px] text-muted-light hover:bg-border hover:text-foreground"
          >
            <XIcon />
          </button>
        </span>
      ))}
      {refs.length > 1 ? (
        <button
          type="button"
          onClick={onClear}
          className="cursor-pointer rounded-md px-1.5 py-1 text-[11px] font-medium text-muted hover:bg-surface-subtle"
        >
          Clear evidence
        </button>
      ) : null}
    </div>
  );
}

function CheckIcon() {
  return (
    <svg
      viewBox="0 0 20 20"
      className="h-4 w-4"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="m5 10 3 3 7-7" />
    </svg>
  );
}

function XIcon() {
  return (
    <svg
      viewBox="0 0 20 20"
      className="h-4 w-4"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      strokeLinecap="round"
      aria-hidden
    >
      <path d="M5.5 5.5 14.5 14.5M14.5 5.5 5.5 14.5" />
    </svg>
  );
}

function PencilIcon() {
  return (
    <svg
      viewBox="0 0 20 20"
      className="h-4 w-4"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.7}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="M12.8 4.7 15.3 7.2M5 15l2.6-.5 7.2-7.2a1.7 1.7 0 0 0-2.4-2.4L5.2 12.1 5 15Z" />
    </svg>
  );
}

function BoltIcon() {
  return (
    <svg
      viewBox="0 0 20 20"
      className="h-4 w-4"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.7}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="M11 2.8 5.8 10.6h3.8L9 17.2l5.2-7.8h-3.8L11 2.8Z" />
    </svg>
  );
}

function TrashIcon() {
  return (
    <svg
      viewBox="0 0 20 20"
      className="h-4 w-4"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.7}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden
    >
      <path d="M4.5 6h11M8 6V4.5h4V6M6 6l.6 9.5h6.8L14 6" />
    </svg>
  );
}

function PlusIcon() {
  return (
    <svg
      viewBox="0 0 20 20"
      className="h-4 w-4"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      aria-hidden
    >
      <path d="M10 4.5v11M4.5 10h11" />
    </svg>
  );
}

