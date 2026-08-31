import type { BaseEvent, RunAgentInput } from "@ag-ui/client";
import { type MetadataStore, type RunEventWriter } from "@datafoundry/metadata";

import type { InteractionResume } from "./interaction-runtime-adapter.js";
import {
  createRunRequestFingerprint,
  resolveExistingRun,
  validateParentRun
} from "./run-identity.js";
import type { EffectiveRunConfig } from "./run-input.js";
import type { RunCancelRegistry } from "./run-cancel-registry.js";
import { resolveLiveSessionActiveRun } from "./stale-active-runs.js";

export type RunIdentityResolution =
  | {
      kind: "replay";
      events: BaseEvent[];
    }
  | {
      kind: "active";
      isResume: boolean;
      selectedDatasourceId?: string;
    };

type ResolveRunIdentityInput = {
  effectiveRunConfig: EffectiveRunConfig;
  interactionResume?: InteractionResume | undefined;
  metadataStore: MetadataStore;
  modelName: string;
  runCancelRegistry: RunCancelRegistry;
  runEventWriter: RunEventWriter;
  runInput: RunAgentInput;
  userId: string;
  userInput: string;
};

/** Resolve run/session identity, replay idempotent runs, and claim or resume active work. */
export const resolveRunIdentity = (input: ResolveRunIdentityInput): RunIdentityResolution => {
  const sessionId = input.runInput.threadId;
  const runId = input.runInput.runId;
  const resume = input.interactionResume;
  const requestFingerprint = createRunRequestFingerprint(input.runInput, input.effectiveRunConfig);
  const existingRun = input.metadataStore.runs.find({
    user_id: input.userId,
    run_id: runId
  });
  const selectedDatasourceId = resume && existingRun?.datasource_id
    ? existingRun.datasource_id
    : input.effectiveRunConfig.activeDatasourceId;
  const isResume = resume !== undefined && existingRun?.status === "suspended";

  if (existingRun && !isResume) {
    if (resume) {
      const interaction = input.metadataStore.interactions.getByToolCall({
        user_id: input.userId,
        run_id: runId,
        tool_call_id: resume.interrupt.toolCallId
      });
      if (interaction.status !== "resolved" || interaction.resume_fingerprint !== resume.fingerprint) {
        throw new Error(`INTERACTION_NOT_RESUMABLE:${resume.interrupt.toolCallId}`);
      }
    }
    return {
      kind: "replay",
      events: resolveExistingRun({
        existingRun,
        requestFingerprint: resume ? existingRun.request_fingerprint ?? "" : requestFingerprint,
        runEventWriter: input.runEventWriter,
        sessionId
      })
    };
  }

  const activeSessionRun = resolveLiveSessionActiveRun({
    metadataStore: input.metadataStore,
    runCancelRegistry: input.runCancelRegistry,
    userId: input.userId,
    sessionId,
    excludeRunId: runId
  });

  if (activeSessionRun) {
    throw new Error(`RUN_ALREADY_ACTIVE:${activeSessionRun.id}`);
  }

  if (isResume) {
    if (!resume) {
      throw new Error(`INTERACTION_RESUME_REQUIRED:${runId}`);
    }
    if (existingRun?.session_id !== sessionId) {
      throw new Error(`RUN_SESSION_MISMATCH:${runId}`);
    }
    const interaction = input.metadataStore.interactions.getByToolCall({
      user_id: input.userId,
      run_id: runId,
      tool_call_id: resume.interrupt.toolCallId
    });
    if (
      interaction.session_id !== sessionId
      || interaction.tool_name !== resume.interrupt.toolName
      || interaction.status !== "pending"
    ) {
      throw new Error(`INTERACTION_IDENTITY_MISMATCH:${resume.interrupt.toolCallId}`);
    }
    input.metadataStore.runs.updateStatus({
      user_id: input.userId,
      run_id: runId,
      status: "running"
    });
  } else {
    validateParentRun({
      metadataStore: input.metadataStore,
      parentRunId: input.runInput.parentRunId,
      sessionId,
      userId: input.userId
    });
    if (selectedDatasourceId) {
      input.metadataStore.dataSources.get({
        user_id: input.userId,
        datasource_id: selectedDatasourceId
      });
    }
    input.metadataStore.sessions.create({
      user_id: input.userId,
      id: sessionId,
      ...(selectedDatasourceId ? { selected_datasource_id: selectedDatasourceId } : {})
    });
    const claim = input.metadataStore.runs.claim({
      user_id: input.userId,
      id: runId,
      session_id: sessionId,
      ...(input.runInput.parentRunId ? { parent_run_id: input.runInput.parentRunId } : {}),
      request_fingerprint: requestFingerprint,
      user_input: input.userInput,
      status: "running",
      model_name: input.modelName,
      ...(selectedDatasourceId ? { datasource_id: selectedDatasourceId } : {})
    });
    if (!claim.created) {
      throw new Error(`RUN_CLAIM_CONFLICT:${runId}`);
    }
  }

  return {
    kind: "active",
    isResume,
    ...(selectedDatasourceId ? { selectedDatasourceId } : {})
  };
};
