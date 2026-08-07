import { MastraAgent } from "@ag-ui/mastra";
import type { RunAgentInput } from "@ag-ui/client";
import type { ArtifactService, SessionOutputService } from "@datafoundry/artifacts";
import {
  createDataFoundry,
  createDataFoundryRunContext,
  type AgentRunContext,
  type AgentContextItem,
  type AgUiEventEmitter,
  type ContextPackage,
  type ContextPackageRecorder,
  type GoalRuntimeAdapter,
  type RunProtocolBoundary,
  type ContextPackageRef,
  type ProtocolStateStore,
  type SessionIntent,
  type TaskStateRuntime,
  type WorkspaceAttachment
} from "@datafoundry/agent-runtime";
import type { DataGateway } from "@datafoundry/data-gateway";
import type { FileAssetService } from "@datafoundry/files";
import type { KnowledgeService } from "@datafoundry/knowledge";
import type { LongTermMemoryRecord } from "@datafoundry/metadata";
import type { SkillRecord, SkillSelectionResult } from "@datafoundry/skills";

import type { InteractionResume } from "./interaction-runtime-adapter.js";
import { createPolicyMcpTools } from "./policy-mcp-tools.js";
import type { McpRuntime, ResolvedRunConfig } from "./run-config-resolver.js";
import type { EffectiveRunConfig } from "./run-input.js";

export type RunAgentAssembly = {
  destroyWorkspace(): Promise<void>;
  goalRuntime?: GoalRuntimeAdapter | undefined;
  governedMessages: RunAgentInput["messages"];
  mastraAgent: MastraAgent;
  protocol: RunProtocolBoundary;
  flushProtocolEvents(): void;
  workspace: {
    command_execution_enabled: boolean;
    isolation: "bwrap" | "none" | "seatbelt";
  };
  /** Persistent cross-session workspace root (read-only asset area). */
  workspaceDir: string;
  /** Per-session directory (agent filesystem basePath; new files default here). */
  sessionDir: string;
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
  abortSignal?: AbortSignal | undefined;
  artifactService: ArtifactService;
  dataGateway: DataGateway;
  effectiveRunConfig: EffectiveRunConfig;
  emitter: AgUiEventEmitter;
  contextPackageRecorder?: ContextPackageRecorder;
  contextPackageExists(reference: ContextPackageRef): boolean;
  evidenceContextItems?: AgentContextItem[] | undefined;
  fileAssetService: FileAssetService;
  goal?: EffectiveRunConfig["goal"] | undefined;
  initialContextPackage?: ContextPackage | undefined;
  interactionResume?: InteractionResume | undefined;
  knowledgeService: KnowledgeService;
  longTermMemories: LongTermMemoryRecord[];
  mcpRuntime: McpRuntime;
  messages: RunAgentInput["messages"];
  modelContextProfile?: ResolvedRunConfig["modelContextProfile"] | undefined;
  modelProvider: ResolvedRunConfig["modelProvider"];
  protocolStateStore: ProtocolStateStore;
  modelSettings?: ResolvedRunConfig["modelSettings"] | undefined;
  runContext: AgentRunContext;
  sessionOutputService: SessionOutputService;
  selectedSkills: SkillRecord[];
  /** Session intent resolved by the caller; enables deterministic protocol
   * inheritance for weak follow-ups. */
  sessionIntent?: SessionIntent | undefined;
  /** Budgeted background block for the protocol classifier. */
  classifierContext?: string | undefined;
  skillSelection: SkillSelectionResult;
  taskStateRuntime: TaskStateRuntime;
  userId: string;
  workspaceId: string;
  workspaceRoot: string;
};

/** Create the canonical agent run context used by Mastra tools, projections, and metadata. */
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
    ...(input.effectiveRunConfig.activeSkillId ? { active_skill_id: input.effectiveRunConfig.activeSkillId } : {}),
    ...(input.effectiveRunConfig.enabledKnowledgeIds.length > 0
      ? { enabled_knowledge_ids: input.effectiveRunConfig.enabledKnowledgeIds }
      : {}),
    ...(input.effectiveRunConfig.enabledMcpServerIds.length > 0
      ? { enabled_mcp_server_ids: input.effectiveRunConfig.enabledMcpServerIds }
      : {}),
    ...(input.effectiveRunConfig.mentioned
      ? {
          mentioned: {
            db: input.effectiveRunConfig.mentioned.db,
            kb: input.effectiveRunConfig.mentioned.kb,
            mcp: input.effectiveRunConfig.mentioned.mcp,
            skill: input.effectiveRunConfig.mentioned.skill
          }
        }
      : {}),
    ...((input.effectiveRunConfig.pinnedPaths?.length ?? 0) > 0
      ? { pinned_paths: input.effectiveRunConfig.pinnedPaths }
      : {}),
    ...(input.effectiveRunConfig.evidenceRefs.length > 0
      ? { evidence_refs: input.effectiveRunConfig.evidenceRefs }
      : {}),
    model_name: input.modelProvider.model_name
  });

/** Assemble the Mastra-backed AG-UI agent and its run-scoped execution metadata. */
export const createRunAgentAssembly = async (
  input: CreateRunAgentAssemblyInput
): Promise<RunAgentAssembly> => {
  const mcpTools = createPolicyMcpTools(input.mcpRuntime.servers);
  const {
    agent,
    commandExecutionEnabled,
    destroyWorkspace,
    goalRuntime,
    governedMessages,
    flushProtocolEvents,
    isolation,
    protocol,
    workspaceDir,
    sessionDir
  } = await createDataFoundry({
    ...(input.abortSignal ? { abortSignal: input.abortSignal } : {}),
    artifactService: input.artifactService,
    ...(input.contextPackageRecorder ? { contextPackageRecorder: input.contextPackageRecorder } : {}),
    contextPackageExists: input.contextPackageExists,
    dataGateway: input.dataGateway,
    fileAssetService: input.fileAssetService,
    ...(input.initialContextPackage ? { initialContextPackage: input.initialContextPackage } : {}),
    knowledgeService: input.knowledgeService,
    ...(input.mcpRuntime.toolNames.length > 0 ? { mcpToolNames: input.mcpRuntime.toolNames } : {}),
    ...(Object.keys(mcpTools).length > 0 ? { mcpTools } : {}),
    emitter: input.emitter,
    ...(input.effectiveRunConfig.protocol ? { explicitProtocol: input.effectiveRunConfig.protocol } : {}),
    messages: input.messages,
    ...(input.modelContextProfile ? { modelContextProfile: input.modelContextProfile } : {}),
    modelProvider: input.modelProvider,
    protocolStateStore: input.protocolStateStore,
    ...(input.effectiveRunConfig.resourceRevisions
      ? { resourceRevisions: input.effectiveRunConfig.resourceRevisions }
      : {}),
    ...(input.modelSettings ? { modelSettings: input.modelSettings } : {}),
    ...(input.evidenceContextItems?.length ? { evidenceContextItems: input.evidenceContextItems } : {}),
    ...(input.longTermMemories.length > 0 ? { longTermMemory: { records: input.longTermMemories } } : {}),
    runContext: input.runContext,
    sessionOutputService: input.sessionOutputService,
    selectedSkills: input.selectedSkills,
    ...(input.sessionIntent ? { sessionIntent: input.sessionIntent } : {}),
    ...(input.classifierContext ? { classifierContext: input.classifierContext } : {}),
    skillSelection: input.skillSelection,
    taskStateRuntime: input.taskStateRuntime,
    ...(!input.interactionResume && input.goal ? { goal: input.goal } : {}),
    ...(input.effectiveRunConfig.fileIds.length > 0
      ? { workspaceAttachments: resolveWorkspaceAttachments(input) }
      : {}),
    workspaceRoot: input.workspaceRoot
  });
  const mastraAgent = new MastraAgent({
    agent,
    resourceId: input.userId
  });

  return {
    destroyWorkspace,
    flushProtocolEvents,
    ...(goalRuntime ? { goalRuntime } : {}),
    governedMessages,
    mastraAgent,
    protocol,
    workspace: {
      command_execution_enabled: commandExecutionEnabled,
      isolation
    },
    workspaceDir,
    sessionDir
  };
};

const resolveWorkspaceAttachments = (input: CreateRunAgentAssemblyInput): WorkspaceAttachment[] =>
  input.effectiveRunConfig.fileIds.map((fileId) => {
    const resolved = input.fileAssetService.getRef({
      user_id: input.userId,
      workspace_id: input.workspaceId,
      id: fileId
    });
    return {
      file_id: resolved.ref.id,
      filename: resolved.ref.filename,
      ...(resolved.ref.declared_mime_type ?? resolved.asset.detected_mime_type
        ? { mime_type: resolved.ref.declared_mime_type ?? resolved.asset.detected_mime_type }
        : {}),
      size_bytes: resolved.asset.size_bytes,
      source_path: resolved.asset.storage_path
    };
  });
