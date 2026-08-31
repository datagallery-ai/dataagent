import type {
  CheckpointRecord,
  ConversationMessageRecord,
  ConversationSummaryRecord,
  MetadataStore,
  RunRecord,
  SessionBranchRecord,
  SessionRecord
} from "@datafoundry/metadata";
import { randomUUID } from "node:crypto";

const BRANCHABLE_RUN_STATUSES = new Set<RunRecord["status"]>(["completed", "failed", "canceled"]);
const BRANCHABLE_CHECKPOINT_STATUSES = new Set<CheckpointRecord["status"]>(["stable", "terminal"]);

export type VisibleConversationSegment = {
  sessionId: string;
  maxPosition?: number;
};

export type SessionLineage = {
  branch?: SessionBranchRecord;
  segments: VisibleConversationSegment[];
};

export type ConversationBranchOption = {
  createdAt: string;
  forkCheckpointId?: string;
  forkMessageEndPosition: number;
  forkRunId: string;
  isOriginal: boolean;
  parentSessionId: string;
  rootSessionId: string;
  sessionId: string;
  title?: string;
};

export type CreatedSessionBranch = {
  branch: SessionBranchRecord;
  session: SessionRecord;
};

export function resolveSessionLineage(input: {
  metadataStore: MetadataStore;
  sessionId: string;
  userId: string;
}): SessionLineage {
  return resolveSessionLineageInner(input, new Set<string>());
}

export function listVisibleConversationMessages(input: {
  excludeRunId?: string;
  limit: number;
  lineage: SessionLineage;
  metadataStore: MetadataStore;
  userId: string;
}): ConversationMessageRecord[] {
  const records = input.lineage.segments.flatMap((segment) =>
    input.metadataStore.conversationMessages.listBySessionRange({
      user_id: input.userId,
      session_id: segment.sessionId,
      ...(segment.maxPosition !== undefined ? { max_position: segment.maxPosition } : {}),
      ...(input.excludeRunId ? { exclude_run_id: input.excludeRunId } : {})
    })
  );
  return records.slice(Math.max(0, records.length - Math.max(0, Math.floor(input.limit))));
}

export function latestVisibleConversationSummary(input: {
  lineage: SessionLineage;
  metadataStore: MetadataStore;
  sessionId: string;
  userId: string;
}): ConversationSummaryRecord | undefined {
  if (input.lineage.branch) {
    return undefined;
  }
  return input.metadataStore.conversationSummaries.latest({
    user_id: input.userId,
    session_id: input.sessionId
  });
}

export function createSessionBranch(input: {
  activeSessionId: string;
  checkpointId?: string;
  metadataStore: MetadataStore;
  runId?: string;
  title?: string;
  userId: string;
}): CreatedSessionBranch {
  const activeLineage = resolveSessionLineage({
    metadataStore: input.metadataStore,
    sessionId: input.activeSessionId,
    userId: input.userId
  });
  if (input.runId && input.checkpointId) {
    throw new Error("BRANCH_TARGET_AMBIGUOUS");
  }
  if (!input.runId && !input.checkpointId) {
    throw new Error("BRANCH_TARGET_REQUIRED");
  }

  const target = resolveBranchTarget({
    activeLineage,
    checkpointId: input.checkpointId,
    metadataStore: input.metadataStore,
    runId: input.runId,
    userId: input.userId
  });
  const run = target.run;
  const visibleSessionIds = new Set(activeLineage.segments.map((segment) => segment.sessionId));
  if (!visibleSessionIds.has(run.session_id)) {
    throw new Error(`RUN_NOT_VISIBLE:${run.id}`);
  }
  if (!BRANCHABLE_RUN_STATUSES.has(run.status)) {
    throw new Error(`RUN_NOT_BRANCHABLE:${run.id}:${run.status}`);
  }

  const runMessages = input.metadataStore.conversationMessages.listBySessionRange({
    user_id: input.userId,
    session_id: run.session_id
  }).filter((message) => message.run_id === run.id);
  const forkMessageEndPosition = target.checkpoint
    ? checkpointForkMessageEndPosition(target.checkpoint, runMessages)
    : runRewriteForkMessageEndPosition(runMessages);
  const parentLineage = resolveSessionLineage({
    metadataStore: input.metadataStore,
    sessionId: run.session_id,
    userId: input.userId
  });
  const parentRootSessionId = parentLineage.branch?.root_session_id ?? run.session_id;
  const parentSession = input.metadataStore.sessions.get({
    user_id: input.userId,
    session_id: run.session_id
  });
  const runIntentBinding = input.metadataStore.sessionIntents.findRunBinding({
    user_id: input.userId,
    run_id: run.id
  });
  const forkIntentRevisionId = target.checkpoint
    ? target.checkpoint.intent_revision_id
    : runIntentBinding?.base_revision_id;
  const childSessionId = randomUUID();
  const session = input.metadataStore.sessions.create({
    user_id: input.userId,
    id: childSessionId,
    title: input.title?.trim().slice(0, 80) || parentSession.title || "Branched conversation",
    title_source: input.title?.trim() ? "user" : parentSession.title_source ?? "fallback",
    ...(parentSession.selected_datasource_id ? { selected_datasource_id: parentSession.selected_datasource_id } : {}),
    ...(parentSession.selected_collection_id ? { selected_collection_id: parentSession.selected_collection_id } : {})
  });
  const branch = input.metadataStore.sessionBranches.create({
    user_id: input.userId,
    id: `branch:${childSessionId}`,
    child_session_id: childSessionId,
    parent_session_id: run.session_id,
    root_session_id: parentRootSessionId,
    fork_run_id: run.id,
    ...(target.checkpoint ? { fork_checkpoint_id: target.checkpoint.id } : {}),
    ...(forkIntentRevisionId ? { fork_intent_revision_id: forkIntentRevisionId } : {}),
    fork_message_end_position: Math.max(0, forkMessageEndPosition)
  });
  return { branch, session };
}

function resolveBranchTarget(input: {
  activeLineage: SessionLineage;
  checkpointId?: string | undefined;
  metadataStore: MetadataStore;
  runId?: string | undefined;
  userId: string;
}): { checkpoint?: CheckpointRecord; run: RunRecord } {
  if (!input.checkpointId) {
    if (!input.runId) {
      throw new Error("BRANCH_TARGET_REQUIRED");
    }
    return {
      run: input.metadataStore.runs.get({ user_id: input.userId, run_id: input.runId })
    };
  }

  const checkpoint = input.metadataStore.checkpoints.get({
    user_id: input.userId,
    checkpoint_id: input.checkpointId
  });
  if (!BRANCHABLE_CHECKPOINT_STATUSES.has(checkpoint.status)) {
    throw new Error(`CHECKPOINT_NOT_BRANCHABLE:${checkpoint.id}:${checkpoint.status}`);
  }
  const run = input.metadataStore.runs.get({ user_id: input.userId, run_id: checkpoint.run_id });
  const visibleSessionIds = new Set(input.activeLineage.segments.map((segment) => segment.sessionId));
  if (!visibleSessionIds.has(checkpoint.session_id)) {
    throw new Error(`CHECKPOINT_NOT_VISIBLE:${checkpoint.id}`);
  }
  return { checkpoint, run };
}

function runRewriteForkMessageEndPosition(runMessages: ConversationMessageRecord[]): number {
  const positions = runMessages.map((message) => message.position);
  return positions.length > 0 ? Math.min(...positions) - 1 : 0;
}

function checkpointForkMessageEndPosition(
  checkpoint: CheckpointRecord,
  runMessages: ConversationMessageRecord[]
): number {
  if (checkpoint.message_position !== undefined) {
    return checkpoint.message_position;
  }
  const userPositions = runMessages
    .filter((message) => message.role === "user")
    .map((message) => message.position);
  if (userPositions.length > 0) {
    return Math.min(...userPositions);
  }
  return runRewriteForkMessageEndPosition(runMessages);
}

export function listConversationBranchOptions(input: {
  lineage: SessionLineage;
  metadataStore: MetadataStore;
  sessionId: string;
  userId: string;
}): ConversationBranchOption[] {
  const parentSessionIds = input.lineage.segments.map((segment) => segment.sessionId);
  const childBranches = input.metadataStore.sessionBranches.listChildrenForParents({
    user_id: input.userId,
    parent_session_ids: parentSessionIds
  });
  const activeBranch = input.lineage.branch;
  const branchByKey = new Map<string, ConversationBranchOption>();

  for (const branch of childBranches) {
    const child = safeGetSession(input.metadataStore, input.userId, branch.child_session_id);
    if (!child) {
      continue;
    }
    branchByKey.set(branchOptionKey(branch.child_session_id, branch.fork_run_id, branch.fork_checkpoint_id), {
      createdAt: branch.created_at,
      ...(branch.fork_checkpoint_id ? { forkCheckpointId: branch.fork_checkpoint_id } : {}),
      forkMessageEndPosition: branch.fork_message_end_position,
      forkRunId: branch.fork_run_id,
      isOriginal: false,
      parentSessionId: branch.parent_session_id,
      rootSessionId: branch.root_session_id,
      sessionId: branch.child_session_id,
      ...(child.title ? { title: child.title } : {})
    });
  }

  if (activeBranch) {
    const activeSession = safeGetSession(input.metadataStore, input.userId, activeBranch.child_session_id);
    if (activeSession) {
      branchByKey.set(
        branchOptionKey(activeBranch.child_session_id, activeBranch.fork_run_id, activeBranch.fork_checkpoint_id),
        {
          createdAt: activeBranch.created_at,
          ...(activeBranch.fork_checkpoint_id ? { forkCheckpointId: activeBranch.fork_checkpoint_id } : {}),
          forkMessageEndPosition: activeBranch.fork_message_end_position,
          forkRunId: activeBranch.fork_run_id,
          isOriginal: false,
          parentSessionId: activeBranch.parent_session_id,
          rootSessionId: activeBranch.root_session_id,
          sessionId: activeBranch.child_session_id,
          ...(activeSession.title ? { title: activeSession.title } : {})
        }
      );
    }
  }

  const originalForks = new Map<string, ConversationBranchOption>();
  for (const option of branchByKey.values()) {
    const parent = safeGetSession(input.metadataStore, input.userId, option.parentSessionId);
    if (!parent) {
      continue;
    }
    originalForks.set(branchOptionKey(option.parentSessionId, option.forkRunId, option.forkCheckpointId), {
      createdAt: parent.created_at,
      ...(option.forkCheckpointId ? { forkCheckpointId: option.forkCheckpointId } : {}),
      forkMessageEndPosition: option.forkMessageEndPosition,
      forkRunId: option.forkRunId,
      isOriginal: true,
      parentSessionId: option.parentSessionId,
      rootSessionId: option.rootSessionId,
      sessionId: option.parentSessionId,
      ...(parent.title ? { title: parent.title } : {})
    });
  }

  return [...originalForks.values(), ...branchByKey.values()]
    .sort((left, right) =>
      left.forkRunId.localeCompare(right.forkRunId)
      || Number(right.isOriginal) - Number(left.isOriginal)
      || left.createdAt.localeCompare(right.createdAt)
      || left.sessionId.localeCompare(right.sessionId)
    );
}

export function isRunVisibleInSessionLineage(input: {
  lineage: SessionLineage;
  run: RunRecord;
}): boolean {
  return input.lineage.segments.some((segment) => segment.sessionId === input.run.session_id);
}

function resolveSessionLineageInner(input: {
  metadataStore: MetadataStore;
  sessionId: string;
  userId: string;
}, seen: Set<string>): SessionLineage {
  if (seen.has(input.sessionId)) {
    throw new Error(`SESSION_BRANCH_CYCLE:${input.sessionId}`);
  }
  seen.add(input.sessionId);
  input.metadataStore.sessions.get({ user_id: input.userId, session_id: input.sessionId });
  const branch = input.metadataStore.sessionBranches.findByChild({
    user_id: input.userId,
    child_session_id: input.sessionId
  });
  if (!branch) {
    return { segments: [{ sessionId: input.sessionId }] };
  }
  const parentLineage = resolveSessionLineageInner({
    metadataStore: input.metadataStore,
    sessionId: branch.parent_session_id,
    userId: input.userId
  }, seen);
  const parentSegments = clampLineageAtSession(
    parentLineage.segments,
    branch.parent_session_id,
    branch.fork_message_end_position
  );
  return {
    branch,
    segments: [...parentSegments, { sessionId: input.sessionId }]
  };
}

function clampLineageAtSession(
  segments: VisibleConversationSegment[],
  sessionId: string,
  maxPosition: number
): VisibleConversationSegment[] {
  const index = segments.findIndex((segment) => segment.sessionId === sessionId);
  if (index < 0) {
    throw new Error(`SESSION_BRANCH_PARENT_NOT_VISIBLE:${sessionId}`);
  }
  return segments.slice(0, index + 1).map((segment, segmentIndex) => {
    if (segmentIndex !== index) {
      return segment;
    }
    return {
      sessionId: segment.sessionId,
      maxPosition: segment.maxPosition === undefined
        ? maxPosition
        : Math.min(segment.maxPosition, maxPosition)
    };
  });
}

function safeGetSession(
  metadataStore: MetadataStore,
  userId: string,
  sessionId: string
): SessionRecord | undefined {
  try {
    return metadataStore.sessions.get({ user_id: userId, session_id: sessionId });
  } catch {
    return undefined;
  }
}

function branchOptionKey(sessionId: string, forkRunId: string, forkCheckpointId?: string): string {
  return `${sessionId}:${forkRunId}:${forkCheckpointId ?? ""}`;
}
