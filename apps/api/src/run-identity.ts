import type { BaseEvent, RunAgentInput } from "@ag-ui/client";
import { type MetadataStore, type RunEventWriter, type RunRecord } from "@datafoundry/metadata";
import { createHash } from "node:crypto";
import {
  isRunVisibleInSessionLineage,
  resolveSessionLineage
} from "./session-branching.js";

export type ResolveExistingRunInput = {
  existingRun: RunRecord;
  requestFingerprint: string;
  runEventWriter: RunEventWriter;
  sessionId: string;
};

export const resolveExistingRun = ({
  existingRun,
  requestFingerprint,
  runEventWriter,
  sessionId
}: ResolveExistingRunInput): BaseEvent[] => {
  if (existingRun.session_id !== sessionId) {
    throw new Error(`RUN_SESSION_MISMATCH: ${existingRun.id}`);
  }
  if (!existingRun.request_fingerprint) {
    throw new Error(`RUN_REQUEST_FINGERPRINT_UNAVAILABLE: ${existingRun.id}`);
  }
  if (existingRun.request_fingerprint !== requestFingerprint) {
    throw new Error(`RUN_REQUEST_MISMATCH: ${existingRun.id}`);
  }
  if (existingRun.status === "queued" || existingRun.status === "running") {
    throw new Error(`RUN_ALREADY_ACTIVE: ${existingRun.id}`);
  }

  const replayedEvents = runEventWriter.replay({
    user_id: existingRun.user_id,
    run_id: existingRun.id
  });

  if (replayedEvents.length === 0) {
    throw new Error(`RUN_REPLAY_UNAVAILABLE: ${existingRun.id}`);
  }

  return replayedEvents.map((envelope) => envelope.event);
};

export type ValidateParentRunInput = {
  metadataStore: MetadataStore;
  parentRunId: string | undefined;
  sessionId: string;
  userId: string;
};

export const validateParentRun = ({
  metadataStore,
  parentRunId,
  sessionId,
  userId
}: ValidateParentRunInput): void => {
  if (!parentRunId) {
    return;
  }

  const parentRun = metadataStore.runs.find({ user_id: userId, run_id: parentRunId });

  if (!parentRun) {
    throw new Error(`PARENT_RUN_NOT_FOUND: ${parentRunId}`);
  }
  if (parentRun.session_id === sessionId) {
    return;
  }

  try {
    const lineage = resolveSessionLineage({ metadataStore, sessionId, userId });
    if (!isRunVisibleInSessionLineage({ lineage, run: parentRun })) {
      throw new Error(`PARENT_RUN_SESSION_MISMATCH: ${parentRunId}`);
    }
  } catch (error) {
    if (error instanceof Error && error.message.includes("PARENT_RUN_SESSION_MISMATCH")) {
      throw error;
    }
    throw new Error(`PARENT_RUN_SESSION_MISMATCH: ${parentRunId}`);
  }
};

export const createRunRequestFingerprint = (runInput: RunAgentInput, effectiveRunConfig: unknown): string => {
  const canonicalRequest = canonicalizeJson({
    effectiveRunConfig,
    runInput: {
      context: runInput.context,
      forwardedProps: runInput.forwardedProps,
      messages: latestUserFingerprintMessages(runInput),
      parentRunId: runInput.parentRunId,
      runId: runInput.runId,
      state: runInput.state,
      threadId: runInput.threadId,
      tools: runInput.tools
    }
  });
  return createHash("sha256").update(JSON.stringify(canonicalRequest)).digest("hex");
};

const latestUserFingerprintMessages = (runInput: RunAgentInput): Array<{ content: unknown; role: "user" }> => {
  const latestUser = [...runInput.messages].reverse().find((message) => message.role === "user");
  return latestUser ? [{ role: "user", content: latestUser.content }] : [];
};

const canonicalizeJson = (value: unknown): unknown => {
  if (Array.isArray(value)) {
    return value.map((item) => canonicalizeJson(item));
  }
  if (!isRecord(value)) {
    return value;
  }

  return Object.fromEntries(
    Object.entries(value)
      .filter(([, item]) => item !== undefined)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => [key, canonicalizeJson(item)])
  );
};

const isRecord = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === "object";
