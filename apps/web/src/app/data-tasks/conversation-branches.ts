import type {
  ConversationBranchDto,
  ConversationCheckpointDto,
  ConversationMessageDto,
  SessionConversationDto,
} from "../../lib/config-api/types";

const ENDED_RUN_STATUSES = new Set<ConversationCheckpointDto["status"]>([
  "completed",
  "failed",
  "canceled",
]);

const ACTIVE_RUN_STATUSES = new Set<ConversationCheckpointDto["status"]>([
  "queued",
  "running",
  "suspended",
]);

type BranchForkMeta = Pick<
  ConversationBranchDto,
  "forkCheckpointId" | "forkMessageEndPosition" | "forkRunId" | "sessionId"
> & {
  isOriginal?: boolean;
};

export type UserMessageBranchState = {
  branchable: boolean;
  currentIndex: number;
  nextSessionId?: string;
  options: ConversationBranchDto[];
  previousSessionId?: string;
  refreshOnly: boolean;
  runId: string;
  status: ConversationCheckpointDto["status"];
  total: number;
};

export function resolveUserMessageBranchState(input: {
  activeSessionId: string | null | undefined;
  conversation: SessionConversationDto | null | undefined;
  messageId: string | undefined;
}): UserMessageBranchState | null {
  if (!input.conversation || !input.messageId) {
    return null;
  }
  const userMessage = findConversationUserMessage(input.conversation, input.messageId);
  if (!userMessage) {
    return null;
  }
  const checkpoint = findCheckpointForMessage(input.conversation, userMessage);
  if (!checkpoint) {
    return null;
  }
  const options = branchOptionsForRun(
    input.conversation,
    checkpoint.runId,
    userMessage,
    input.activeSessionId,
  );
  const matchedIndex = options.findIndex((option) => option.sessionId === input.activeSessionId);
  const currentIndex = matchedIndex >= 0 ? matchedIndex : 0;
  const total = Math.max(1, options.length);
  const previous = total > 1 ? options[(currentIndex - 1 + total) % total] : undefined;
  const next = total > 1 ? options[(currentIndex + 1) % total] : undefined;
  const status = checkpoint.status;

  return {
    branchable: ENDED_RUN_STATUSES.has(status),
    currentIndex,
    options,
    ...(previous ? { previousSessionId: previous.sessionId } : {}),
    ...(next ? { nextSessionId: next.sessionId } : {}),
    refreshOnly: ACTIVE_RUN_STATUSES.has(status),
    runId: checkpoint.runId,
    status,
    total,
  };
}

function findConversationUserMessage(
  conversation: SessionConversationDto,
  messageId: string,
): ConversationMessageDto | undefined {
  return conversation.messages.find((message) =>
    message.role === "user" &&
    (message.messageId === messageId || message.id === messageId)
  );
}

function findCheckpointForMessage(
  conversation: SessionConversationDto,
  message: ConversationMessageDto,
): ConversationCheckpointDto | undefined {
  return (conversation.checkpoints ?? []).find((checkpoint) => {
    if (checkpoint.runId === message.runId) {
      return true;
    }
    const start = checkpoint.messageStartPosition;
    const end = checkpoint.messageEndPosition;
    return (
      start !== undefined &&
      end !== undefined &&
      message.position >= start &&
      message.position <= end
    );
  });
}

function branchesForForkRun(
  conversation: SessionConversationDto,
  forkRunId: string,
): ConversationBranchDto[] {
  const branches = (conversation.branches ?? []).filter((branch) =>
    branch.forkRunId === forkRunId && !branch.forkCheckpointId
  );
  if (branches.length === 0) {
    return [];
  }
  return [...branches].sort((left, right) =>
    Number(right.isOriginal ?? false) - Number(left.isOriginal ?? false) ||
    left.createdAt.localeCompare(right.createdAt) ||
    left.sessionId.localeCompare(right.sessionId)
  );
}

function isSameUserMessage(
  left: ConversationMessageDto,
  right: ConversationMessageDto,
): boolean {
  return left.id === right.id ||
    (!!left.messageId && left.messageId === right.messageId);
}

function resolveActiveChildBranch(
  conversation: SessionConversationDto,
  activeSessionId: string,
): BranchForkMeta | undefined {
  const lineageBranch = conversation.branch;
  if (
    lineageBranch &&
    lineageBranch.sessionId === activeSessionId &&
    !lineageBranch.forkCheckpointId
  ) {
    return lineageBranch;
  }
  // Prefer the real child option; never the synthetic isOriginal parent entry.
  return (conversation.branches ?? []).find((branch) =>
    branch.sessionId === activeSessionId &&
    !branch.isOriginal &&
    !branch.forkCheckpointId
  );
}

/**
 * On a child branch session the rewritten user turn gets a new run id, so it no
 * longer matches `forkRunId`. Only the first user message after the fork boundary
 * should inherit that fork's sibling switcher — later turns must not.
 *
 * Conversation message `position` values are the API's visible 1..n order for the
 * active lineage; `forkMessageEndPosition` is the last parent-prefix position in
 * that same visible sequence when the parent prefix is present.
 */
function isActiveBranchForkPointMessage(
  conversation: SessionConversationDto,
  message: ConversationMessageDto,
  activeBranch: BranchForkMeta,
): boolean {
  const forkPoint = conversation.messages
    .filter((entry) =>
      entry.role === "user" && entry.position > activeBranch.forkMessageEndPosition
    )
    .sort((left, right) => left.position - right.position)[0];
  return !!forkPoint && isSameUserMessage(forkPoint, message);
}

function branchOptionsForRun(
  conversation: SessionConversationDto,
  runId: string,
  message: ConversationMessageDto,
  activeSessionId?: string | null,
): ConversationBranchDto[] {
  const direct = branchesForForkRun(conversation, runId);
  if (direct.length > 0) {
    return direct;
  }
  if (!activeSessionId) {
    return [];
  }
  const activeBranch = resolveActiveChildBranch(conversation, activeSessionId);
  if (!activeBranch || !isActiveBranchForkPointMessage(conversation, message, activeBranch)) {
    return [];
  }
  return branchesForForkRun(conversation, activeBranch.forkRunId);
}
