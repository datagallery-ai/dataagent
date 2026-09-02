import type { RunAgentInput } from "@ag-ui/client";
import {
  createDataFoundryRunContext,
  type AgentRunContext
} from "@datafoundry/agent-runtime";

import type { InteractionResume } from "./interaction-runtime-adapter.js";
import { createRuntimeTransport } from "./runtime/factory.js";
import {
  RUNTIME_PROVIDER,
  V1_SYSTEM_PROMPT,
  type RuntimeRunRequest,
  type RuntimeTransport
} from "./runtime/types.js";
import type { ResolvedRunConfig } from "./run-config-resolver.js";
import type { EffectiveRunConfig } from "./run-input.js";

export type RunAgentAssembly = {
  destroyWorkspace(): Promise<void>;
  governedMessages: RunAgentInput["messages"];
  runtime: RuntimeTransport;
  buildRunRequest(input: {
    checkpointRef?: string;
    interactionResume?: InteractionResume;
    messages: RunAgentInput["messages"];
    runId: string;
    sessionId: string;
    userId: string;
    workspaceId: string;
  }): RuntimeRunRequest;
  sessionDir: string;
  workspaceDir: string;
};

type CreateRunAgentContextInput = {
  effectiveRunConfig: EffectiveRunConfig;
  modelProvider: ResolvedRunConfig["modelProvider"];
  runId: string;
  selectedDatasourceId?: string;
  sessionId: string;
  userId: string;
  userInput: string;
  workspaceId: string;
};

type CreateRunAgentAssemblyInput = {
  messages: RunAgentInput["messages"];
  modelProvider: ResolvedRunConfig["modelProvider"];
  runtime?: RuntimeTransport;
};

/** Create the canonical agent run context used by projections and metadata. */
export const createRunAgentContext = (input: CreateRunAgentContextInput): AgentRunContext =>
  createDataFoundryRunContext({
    user_id: input.userId,
    workspace_id: input.workspaceId,
    session_id: input.sessionId,
    run_id: input.runId,
    user_input: input.userInput,
    chat_mode: "copilotkit",
    ...(input.effectiveRunConfig.enabledDatasourceIds.length > 0
      ? {
          enabled_datasource_ids: input.effectiveRunConfig.enabledDatasourceIds,
          ...(input.selectedDatasourceId ? { selected_datasource_id: input.selectedDatasourceId } : {})
        }
      : {}),
    ...(input.effectiveRunConfig.activeLlmProfileId
      ? { requested_llm_profile_id: input.effectiveRunConfig.activeLlmProfileId }
      : {}),
    model_name: input.modelProvider.model_name
  });

/** Assemble a Deep Agents runtime client for one AG-UI run. */
export const createRunAgentAssembly = (
  input: CreateRunAgentAssemblyInput
): RunAgentAssembly => {
  const runtime = input.runtime ?? createRuntimeTransport({
    ...(process.env.RUNTIME_SERVICE_TOKEN ? { token: process.env.RUNTIME_SERVICE_TOKEN } : {})
  });
  return {
    destroyWorkspace: async () => undefined,
    governedMessages: input.messages,
    runtime,
    sessionDir: "",
    workspaceDir: "",
    buildRunRequest: ({ checkpointRef, interactionResume, messages, runId, sessionId, userId, workspaceId }) => ({
      threadId: sessionId,
      runId,
      messages,
      systemPrompt: V1_SYSTEM_PROMPT,
      model: {
        provider: RUNTIME_PROVIDER,
        name: input.modelProvider.model_name,
        ...(input.modelProvider.kind === "openai-compatible"
          ? { profileId: "openai-compatible" }
          : {})
      },
      limits: { maxSteps: 80 },
      ...(checkpointRef ? { checkpointRef } : {}),
      ...(interactionResume
        ? {
            resume: {
              interrupt: {
                type: interactionResume.interrupt.type === "mastra_suspend"
                  ? "mastra_suspend"
                  : "agent_interrupt",
                toolCallId: interactionResume.interrupt.toolCallId,
                toolName: interactionResume.interrupt.toolName,
                runId: interactionResume.interrupt.runId,
                args: interactionResume.interrupt.args,
                suspendPayload: interactionResume.interrupt.suspendPayload,
                resumeSchema: interactionResume.interrupt.resumeSchema
              },
              response: interactionResume.response
            }
          }
        : {}),
      trace: { userId, workspaceId }
    })
  };
};
