import type { RunAgentInput } from "@ag-ui/client";
import type { ContextPackage } from "@datafoundry/agent-runtime";
import type { CheckpointRecord, MetadataStore } from "@datafoundry/metadata";

import { resolveSessionLineage } from "./session-branching.js";

export type CheckpointResumeSeed = {
  checkpoint: CheckpointRecord;
  contextPackage: ContextPackage;
};

export function resolveCheckpointResumeSeed(input: {
  metadataStore: MetadataStore;
  runInput: RunAgentInput;
  sessionId: string;
  userId: string;
}): CheckpointResumeSeed | undefined {
  const checkpointId =
    checkpointIdFromRunInput(input.runInput) ??
    checkpointIdFromEmptyBranchSession(input.metadataStore, input.userId, input.sessionId);
  if (!checkpointId) {
    return undefined;
  }

  const checkpoint = input.metadataStore.checkpoints.get({
    user_id: input.userId,
    checkpoint_id: checkpointId
  });
  assertCheckpointVisible({
    checkpoint,
    metadataStore: input.metadataStore,
    sessionId: input.sessionId,
    userId: input.userId
  });
  const snapshot = input.metadataStore.contextPackageSnapshots.get({
    user_id: input.userId,
    id: checkpoint.context_package_id
  });
  return {
    checkpoint,
    contextPackage: parseContextPackage(snapshot.payload_json, snapshot.id)
  };
}

function checkpointIdFromRunInput(runInput: RunAgentInput): string | undefined {
  const forwardedProps = recordValue(runInput.forwardedProps);
  const state = recordValue(runInput.state);
  return stringValue(forwardedProps?.checkpointId) ??
    stringValue(forwardedProps?.checkpoint_id) ??
    stringValue(state?.checkpointId) ??
    stringValue(state?.checkpoint_id);
}

function checkpointIdFromEmptyBranchSession(
  metadataStore: MetadataStore,
  userId: string,
  sessionId: string
): string | undefined {
  const branch = metadataStore.sessionBranches.findByChild({
    user_id: userId,
    child_session_id: sessionId
  });
  if (!branch?.fork_checkpoint_id) {
    return undefined;
  }
  const childMessages = metadataStore.conversationMessages.listBySessionRange({
    user_id: userId,
    session_id: sessionId
  });
  return childMessages.length === 0 ? branch.fork_checkpoint_id : undefined;
}

function assertCheckpointVisible(input: {
  checkpoint: CheckpointRecord;
  metadataStore: MetadataStore;
  sessionId: string;
  userId: string;
}): void {
  const lineage = resolveSessionLineage({
    metadataStore: input.metadataStore,
    sessionId: input.sessionId,
    userId: input.userId
  });
  const visibleSessionIds = new Set(lineage.segments.map((segment) => segment.sessionId));
  if (!visibleSessionIds.has(input.checkpoint.session_id)) {
    throw new Error(`CHECKPOINT_NOT_VISIBLE:${input.checkpoint.id}`);
  }
}

function parseContextPackage(payloadJson: string, snapshotId: string): ContextPackage {
  const parsed = parseJson(payloadJson);
  if (!isContextPackage(parsed)) {
    throw new Error(`INVALID_CONTEXT_PACKAGE_SNAPSHOT:${snapshotId}`);
  }
  return parsed;
}

function isContextPackage(value: unknown): value is ContextPackage {
  const record = recordValue(value);
  return record?.version === 2 &&
    typeof record.packageId === "string" &&
    typeof record.revision === "number" &&
    Array.isArray(record.items) &&
    Array.isArray(record.groups);
}

function parseJson(value: string): unknown {
  try {
    return JSON.parse(value) as unknown;
  } catch {
    return undefined;
  }
}

function recordValue(value: unknown): Record<string, unknown> | undefined {
  return typeof value === "object" && value !== null ? value as Record<string, unknown> : undefined;
}

function stringValue(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value : undefined;
}
