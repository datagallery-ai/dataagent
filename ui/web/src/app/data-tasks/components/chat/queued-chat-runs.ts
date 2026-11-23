import type { Attachment } from "@copilotkit/react-core/v2";
import type { SessionActiveRunDto } from "../../../../lib/config-api/types";
import type { LiveRunStatus } from "../../live-run-state";
import type { RunForwardedProps } from "../../data-task-state";

export type QueuedChatPromptStatus = "queued" | "interrupting";

export type QueuedChatPrompt = {
  id: string;
  text: string;
  attachments: Attachment[];
  forwardedProps: RunForwardedProps;
  createdAt: number;
  status: QueuedChatPromptStatus;
};

export type QueuedChatPromptInput = {
  id: string;
  text: string;
  attachments: Attachment[];
  forwardedProps: RunForwardedProps;
  createdAt: number;
};

const QUEUED_PROMPTS_STORAGE_PREFIX = "data-tasks:queued-prompts:v1:";

function queuedPromptsStorageKey(threadId: string): string {
  return `${QUEUED_PROMPTS_STORAGE_PREFIX}${threadId}`;
}

function isPersistedQueuedPrompt(value: unknown): value is QueuedChatPrompt {
  if (!value || typeof value !== "object") return false;
  const record = value as Record<string, unknown>;
  return (
    typeof record.id === "string" &&
    typeof record.text === "string" &&
    typeof record.createdAt === "number" &&
    (record.status === "queued" || record.status === "interrupting") &&
    Array.isArray(record.attachments) &&
    record.forwardedProps != null &&
    typeof record.forwardedProps === "object"
  );
}

/** Persist text/forwardedProps queue so refresh does not drop pending prompts. */
export function loadQueuedChatPrompts(threadId: string | null | undefined): QueuedChatPrompt[] {
  if (!threadId || typeof window === "undefined") return [];
  try {
    const raw = window.sessionStorage.getItem(queuedPromptsStorageKey(threadId));
    if (!raw) return [];
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(isPersistedQueuedPrompt).map((prompt) => ({
      ...prompt,
      attachments: Array.isArray(prompt.attachments) ? prompt.attachments : [],
      status: prompt.status === "interrupting" ? "queued" : prompt.status,
    }));
  } catch {
    return [];
  }
}

export function persistQueuedChatPrompts(
  threadId: string | null | undefined,
  prompts: QueuedChatPrompt[],
): void {
  if (!threadId || typeof window === "undefined") return;
  try {
    const key = queuedPromptsStorageKey(threadId);
    if (prompts.length === 0) {
      window.sessionStorage.removeItem(key);
      return;
    }
    // Drop binary attachment payloads; text + run config is enough to re-send after refresh.
    const serializable = prompts.map((prompt) => ({
      ...prompt,
      attachments: prompt.attachments.map((attachment) => ({
        id: attachment.id,
        type: attachment.type,
        contentType: (attachment as { contentType?: string }).contentType,
        name: (attachment as { name?: string }).name,
        filename: (attachment as { filename?: string }).filename,
        status: attachment.status,
      })),
    }));
    window.sessionStorage.setItem(key, JSON.stringify(serializable));
  } catch {
    // Ignore quota / private-mode failures.
  }
}

export function createQueuedChatPrompt(
  input: QueuedChatPromptInput,
): QueuedChatPrompt {
  return {
    ...input,
    attachments: [...input.attachments],
    status: "queued",
  };
}

export function editQueuedChatPrompt(
  queue: QueuedChatPrompt[],
  id: string,
  text: string,
): QueuedChatPrompt[] {
  return queue.map((prompt) =>
    prompt.id === id ? { ...prompt, text } : prompt,
  );
}

export function deleteQueuedChatPrompt(
  queue: QueuedChatPrompt[],
  id: string,
): QueuedChatPrompt[] {
  return queue.filter((prompt) => prompt.id !== id);
}

export function markQueuedChatPromptInterrupting(
  queue: QueuedChatPrompt[],
  id: string,
): QueuedChatPrompt[] {
  const target = queue.find((prompt) => prompt.id === id);
  if (!target) return queue;
  const rest = queue.filter((prompt) => prompt.id !== id);
  return [{ ...target, status: "interrupting" }, ...rest];
}

export function takeNextQueuedChatPrompt(queue: QueuedChatPrompt[]): {
  prompt: QueuedChatPrompt | null;
  queue: QueuedChatPrompt[];
} {
  const [prompt, ...rest] = queue;
  return {
    prompt: prompt ?? null,
    queue: rest,
  };
}

export function resolveQueuedSubmitMode(input: {
  agentIsRunning: boolean;
  liveRunStatus: LiveRunStatus;
}): "queue" | "dispatch" {
  if (
    input.agentIsRunning ||
    input.liveRunStatus === "running" ||
    input.liveRunStatus === "suspended"
  ) {
    return "queue";
  }
  return "dispatch";
}

/** True when another client owns an active run on this session. */
export function isForeignSessionActiveRun(
  activeRun: SessionActiveRunDto | null | undefined,
  localRunId: string | null | undefined,
  localRunActive = false,
): activeRun is SessionActiveRunDto {
  if (!activeRun?.activeRunId) return false;
  if (localRunActive) return false;
  if (localRunId && activeRun.activeRunId === localRunId) return false;
  if (localRunId) return true;
  // No local run id yet: treat unknown active runs as foreign so multi-device
  // lock prompts still appear; callers should pass localRunActive when this tab owns the run.
  return true;
}

/** Extract the blocking run id from a RUN_ALREADY_ACTIVE backend error. */
export function parseAlreadyActiveRunId(message: string | null | undefined): string | null {
  if (!message) return null;
  const match = /RUN_ALREADY_ACTIVE[:\s]+([^\s:,]+)/u.exec(message);
  const runId = match?.[1]?.trim();
  return runId || null;
}

export function shouldShowRunningStopControl(input: {
  agentIsRunning: boolean;
  liveRunStatus: LiveRunStatus;
  draftText: string;
}): boolean {
  const activeRun =
    input.agentIsRunning ||
    input.liveRunStatus === "running" ||
    input.liveRunStatus === "suspended";
  return activeRun && input.draftText.trim().length === 0;
}

export function queuedPromptToRunInput(prompt: QueuedChatPrompt): {
  text: string;
  attachments: Attachment[];
  forwardedProps: RunForwardedProps;
} {
  return {
    text: prompt.text,
    attachments: prompt.attachments,
    forwardedProps: prompt.forwardedProps,
  };
}
