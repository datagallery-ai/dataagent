import type { RunAgentInput } from "@ag-ui/client";
import {
  createModelProviderFromEnv,
  createModelProviderFromProfile,
  STATIC_AGENT_TOOL_NAMES,
  type AgentModelContextProfile
} from "@datafoundry/agent-runtime";
import type { MetadataStore } from "@datafoundry/metadata";
import {
  selectSkillsForRun,
  type SkillRecord,
  type SkillSelectionResult
} from "@datafoundry/skills";

import type { PolicyMcpClientConfig, PolicyMcpToolConfig } from "./policy-mcp-middleware.js";

import { resolveEffectiveRunConfig, type EffectiveRunConfig } from "./run-input.js";

export type ResolvedRunConfig = {
  effectiveRunConfig: EffectiveRunConfig;
  mcpRuntime: McpRuntime;
  modelProvider: Exclude<ReturnType<typeof createModelProviderFromEnv>, { kind: "mock" }>;
  modelSettings?: {
    frequencyPenalty?: number;
    maxOutputTokens?: number;
    presencePenalty?: number;
    temperature?: number;
    topP?: number;
  };
  modelContextProfile?: AgentModelContextProfile;
  reasoningModel?: boolean;
  runTimeoutMs?: number;
  skillSelection: SkillSelectionResult;
  selectedSkills: SkillRecord[];
};

export type McpRuntime = {
  servers: PolicyMcpClientConfig[];
  toolNames: string[];
  toolNamesByServerId: Record<string, string[]>;
  skipped?: { id: string; reason: string }[];
};

type ResolveRunConfigInput = {
  defaultDatasourceId?: string;
  metadataStore: MetadataStore;
  runInput: RunAgentInput;
  userId: string;
  userInput: string;
  workspaceId: string;
};

/** Resolve one run's config, model, skill policy, MCP runtime, and enabled resources. */
export const resolveRunConfig = (input: ResolveRunConfigInput): ResolvedRunConfig => {
  const effectiveRunConfig = resolveEffectiveRunConfig(
    input.runInput,
    input.metadataStore,
    input.userId,
    input.defaultDatasourceId,
    input.workspaceId
  );
  const skillSelection = selectSkillsForRun({
    metadataStore: input.metadataStore,
    runConfig: effectiveRunConfig,
    userId: input.userId,
    userInput: input.userInput,
    workspaceId: input.workspaceId
  });
  const skillDefaultMcpServerIds = unique(skillSelection.selectedSkills.flatMap((skill) => skill.defaultMcpIds));
  applySkillResourceBindings({
    config: effectiveRunConfig,
    explicitRunConfig: explicitRunConfigKeys(input.runInput),
    selectedSkills: skillSelection.selectedSkills
  });
  validateEffectiveResources(effectiveRunConfig, input.metadataStore, input.userId, input.workspaceId);
  effectiveRunConfig.resourceRevisions = {
    ...resolveEffectiveResourceRevisions(effectiveRunConfig, input.metadataStore, input.userId, input.workspaceId),
    ...Object.fromEntries(skillSelection.selectedSkills.map((skill) => [`skill:${skill.id}`, skill.revision]))
  };
  const modelProvider = resolveRunModelProvider(
    effectiveRunConfig.activeLlmProfileId,
    input.metadataStore,
    input.userId,
    input.workspaceId
  );
  const modelSettings = resolveModelSettings(
    effectiveRunConfig.activeLlmProfileId,
    input.metadataStore,
    input.userId,
    input.workspaceId
  );
  const modelContextProfile = resolveModelContextProfile(
    effectiveRunConfig.activeLlmProfileId,
    modelProvider.model_name,
    input.metadataStore,
    input.userId,
    input.workspaceId
  );
  const reasoningModel = resolveReasoningModel(
    effectiveRunConfig.activeLlmProfileId,
    input.metadataStore,
    input.userId,
    input.workspaceId
  );
  const runTimeoutMs = resolveRunTimeoutMs(
    effectiveRunConfig.activeLlmProfileId,
    input.metadataStore,
    input.userId,
    input.workspaceId
  );
  const mcpRuntime = resolveMcpRuntime(
    effectiveRunConfig.enabledMcpServerIds,
    input.metadataStore,
    input.userId,
    input.workspaceId
  );
  if (mcpRuntime.skipped && mcpRuntime.skipped.length > 0) {
    const skippedIds = new Set(mcpRuntime.skipped.map((entry) => entry.id));
    effectiveRunConfig.enabledMcpServerIds = effectiveRunConfig.enabledMcpServerIds.filter(
      (id) => !skippedIds.has(id)
    );
    effectiveRunConfig.unavailableResources = [
      ...(effectiveRunConfig.unavailableResources ?? []),
      ...mcpRuntime.skipped.map((entry) => ({
        kind: "mcp-server" as const,
        id: entry.id,
        reason: entry.reason
      }))
    ];
  }
  enforceSkillMcpPolicy(
    skillSelection.effectiveToolPolicy.allowedTools,
    skillDefaultMcpServerIds.flatMap((id) => mcpRuntime.toolNamesByServerId[id] ?? [])
  );

  return {
    effectiveRunConfig,
    mcpRuntime,
    modelProvider,
    ...(modelContextProfile ? { modelContextProfile } : {}),
    ...(modelSettings ? { modelSettings } : {}),
    ...(reasoningModel !== undefined ? { reasoningModel } : {}),
    ...(runTimeoutMs !== undefined ? { runTimeoutMs } : {}),
    selectedSkills: skillSelection.selectedSkills,
    skillSelection
  };
};

type ExplicitRunConfigKeys = {
  activeDatasource: boolean;
  activeLlmProfile: boolean;
};

const applySkillResourceBindings = (input: {
  config: EffectiveRunConfig;
  explicitRunConfig: ExplicitRunConfigKeys;
  selectedSkills: SkillRecord[];
}): void => {
  const defaultDbIds = unique(input.selectedSkills.flatMap((skill) => skill.defaultDbIds));
  const defaultKbIds = unique(input.selectedSkills.flatMap((skill) => skill.defaultKbIds));
  const defaultMcpIds = unique(input.selectedSkills.flatMap((skill) => skill.defaultMcpIds));
  const modelProfileId = input.selectedSkills.find((skill) => skill.modelProfileId)?.modelProfileId;

  if (defaultDbIds.length > 0) {
    input.config.enabledDatasourceIds = unique([...input.config.enabledDatasourceIds, ...defaultDbIds]);
    if (!input.explicitRunConfig.activeDatasource) {
      input.config.activeDatasourceId = defaultDbIds[0] as string;
    }
  }
  if (defaultKbIds.length > 0) {
    input.config.enabledKnowledgeIds = unique([...input.config.enabledKnowledgeIds, ...defaultKbIds]);
  }
  if (defaultMcpIds.length > 0) {
    input.config.enabledMcpServerIds = unique([...input.config.enabledMcpServerIds, ...defaultMcpIds]);
  }
  if (modelProfileId && !input.explicitRunConfig.activeLlmProfile) {
    input.config.activeLlmProfileId = modelProfileId;
  }
};

const explicitRunConfigKeys = (runInput: RunAgentInput): ExplicitRunConfigKeys => {
  const forwardedProps = isRecord(runInput.forwardedProps) ? runInput.forwardedProps : {};
  const runConfig = isRecord(forwardedProps.run_config) ? forwardedProps.run_config : {};
  const contextRunConfig = runInput.context.find((item) => item.description === "run_config")?.value;
  const contextConfig = isRecord(contextRunConfig) ? contextRunConfig : {};
  return {
    activeDatasource: hasAnyKey(runConfig, ["activeDatasourceId", "active_datasource_id"])
      || hasAnyKey(contextConfig, ["activeDatasourceId", "active_datasource_id"])
      || hasAnyKey(forwardedProps, ["datasourceId", "datasource_id"]),
    activeLlmProfile: hasAnyKey(runConfig, ["activeLlmProfileId", "active_llm_profile_id"])
      || hasAnyKey(contextConfig, ["activeLlmProfileId", "active_llm_profile_id"])
  };
};

const resolveEffectiveResourceRevisions = (
  config: EffectiveRunConfig,
  metadataStore: MetadataStore,
  userId: string,
  workspaceId: string
): Record<string, number> => {
  const revisions: Record<string, number> = {};
  unique(config.enabledDatasourceIds).forEach((id) => {
    revisions[`datasource:${id}`] = metadataStore.dataSources.get({ user_id: userId, datasource_id: id }).revision;
  });
  const addResources = (kind: "knowledge-base" | "mcp-server" | "skill", ids: string[]): void => {
    unique(ids).forEach((id) => {
      revisions[`${kind}:${id}`] = metadataStore.configResources.get({
        id,
        workspace_id: workspaceId,
        user_id: userId,
        kind
      }).revision;
    });
  };
  addResources("knowledge-base", config.enabledKnowledgeIds);
  addResources("mcp-server", config.enabledMcpServerIds);
  addResources("skill", [
    ...config.enabledSkillIds,
    ...config.skillIds,
    ...(config.activeSkillId ? [config.activeSkillId] : [])
  ]);
  if (config.activeLlmProfileId) {
    let profileId: string | undefined = config.activeLlmProfileId;
    const visited = new Set<string>();
    while (profileId && !visited.has(profileId)) {
      visited.add(profileId);
      const profile = metadataStore.configResources.get({
        id: profileId,
        workspace_id: workspaceId,
        user_id: userId,
        kind: "model-profile"
      });
      revisions[`model-profile:${profileId}`] = profile.revision;
      profileId = stringRecordValue(profile.payload, "fallbackProfileId");
    }
  }
  return revisions;
};

const resolveRunModelProvider = (
  profileId: string | undefined,
  metadataStore: MetadataStore,
  userId: string,
  workspaceId: string
): Exclude<ReturnType<typeof createModelProviderFromEnv>, { kind: "mock" }> => {
  if (!profileId || profileId === "server-default") {
    const provider = createModelProviderFromEnv(process.env);
    if (provider.kind === "mock") {
      throw new Error("PROVIDER_CONFIG_MISSING:LLM_API_KEY is required for server-default.");
    }
    return provider;
  }
  const providers: Array<Exclude<ReturnType<typeof createModelProviderFromEnv>, { kind: "mock" }>> = [];
  const profileIds: string[] = [];
  const visited = new Set<string>();
  let currentId: string | undefined = profileId;
  while (currentId) {
    if (visited.has(currentId)) {
      throw new Error(`MODEL_FALLBACK_CYCLE:${currentId}`);
    }
    visited.add(currentId);
    const profile = metadataStore.configResources.get({
      id: currentId,
      workspace_id: workspaceId,
      user_id: userId,
      kind: "model-profile"
    });
    const credentials = profile.secret_ref
      ? metadataStore.secrets.get({ ref: profile.secret_ref, workspace_id: workspaceId, user_id: userId })
      : {};
    const apiKey = stringRecordValue(credentials, "apiKey") ?? stringRecordValue(credentials, "api_key");
    const provider = createModelProviderFromProfile({
      provider: stringRecordValue(profile.payload, "provider") ?? "openai-compatible",
      model: stringRecordValue(profile.payload, "modelName") ?? stringRecordValue(profile.payload, "model") ?? "",
      base_url: stringRecordValue(profile.payload, "baseUrl") ?? stringRecordValue(profile.payload, "base_url") ?? "",
      ...(apiKey ? { api_key: apiKey } : {})
    });
    if (provider.kind === "mock") {
      throw new Error(`PROVIDER_CONFIG_MISSING:${currentId}`);
    }
    providers.push(provider);
    profileIds.push(currentId);
    currentId = stringRecordValue(profile.payload, "fallbackProfileId");
  }
  const primary = providers[0];
  if (!primary) {
    throw new Error(`PROVIDER_CONFIG_MISSING:${profileId}`);
  }
  if (providers.length === 1) {
    return primary;
  }
  return {
    kind: primary.kind,
    model_name: profileIds.join(" -> "),
    model: providers.map((provider, index) => ({
      id: profileIds[index] as string,
      model: provider.model,
      maxRetries: 1,
      enabled: true
    }))
  };
};

const validateEffectiveResources = (
  config: EffectiveRunConfig,
  metadataStore: MetadataStore,
  userId: string,
  workspaceId: string
): void => {
  config.enabledDatasourceIds.forEach((id) => {
    const datasource = metadataStore.dataSources.get({ user_id: userId, datasource_id: id });
    if (datasource.status !== "ready") {
      throw new Error(`DATASOURCE_NOT_ENABLED:${id}`);
    }
  });
  // R-020: non-skill resources with default_enabled=false are silently dropped (not thrown).
  // Each dropped ID is removed from its enabled set and recorded for run.config.resolved.
  const disabledByPolicy: { kind: "knowledge-base" | "mcp-server" | "model-profile"; id: string }[] = [];
  const droppedKb = validateConfigIds(
    metadataStore,
    userId,
    workspaceId,
    "knowledge-base",
    config.enabledKnowledgeIds
  ).dropped;
  const droppedMcp = validateConfigIds(
    metadataStore,
    userId,
    workspaceId,
    "mcp-server",
    config.enabledMcpServerIds
  ).dropped;
  // Skills keep fail-closed semantics (status disabled/archived throws inside validateConfigIds).
  validateConfigIds(metadataStore, userId, workspaceId, "skill", [...config.enabledSkillIds, ...config.skillIds]);
  if (config.activeSkillId) {
    validateConfigIds(metadataStore, userId, workspaceId, "skill", [config.activeSkillId]);
  }
  if (config.activeLlmProfileId && config.activeLlmProfileId !== "server-default") {
    // An explicitly requested LLM profile that is default_enabled=false is still honored
    // (the user asked for it); we only drop from the *enabled* passive sets above.
    validateConfigIds(metadataStore, userId, workspaceId, "model-profile", [config.activeLlmProfileId]);
  }
  if (droppedKb.length > 0) {
    config.enabledKnowledgeIds = config.enabledKnowledgeIds.filter((id) => !droppedKb.includes(id));
    droppedKb.forEach((id) => disabledByPolicy.push({ kind: "knowledge-base", id }));
  }
  if (droppedMcp.length > 0) {
    config.enabledMcpServerIds = config.enabledMcpServerIds.filter((id) => !droppedMcp.includes(id));
    droppedMcp.forEach((id) => disabledByPolicy.push({ kind: "mcp-server", id }));
  }
  if (disabledByPolicy.length > 0) {
    config.disabledByPolicy = disabledByPolicy;
  }
};

const resolveModelSettings = (
  profileId: string | undefined,
  metadataStore: MetadataStore,
  userId: string,
  workspaceId: string
): ResolvedRunConfig["modelSettings"] | undefined => {
  if (!profileId || profileId === "server-default") {
    return undefined;
  }
  const profile = metadataStore.configResources.get({
    id: profileId,
    workspace_id: workspaceId,
    user_id: userId,
    kind: "model-profile"
  });
  const temperature = numericRecordValue(profile.payload, "temperature");
  const topP = numericRecordValue(profile.payload, "topP") ?? numericRecordValue(profile.payload, "top_p");
  const frequencyPenalty =
    numericRecordValue(profile.payload, "frequencyPenalty") ?? numericRecordValue(profile.payload, "frequency_penalty");
  const presencePenalty =
    numericRecordValue(profile.payload, "presencePenalty") ?? numericRecordValue(profile.payload, "presence_penalty");
  const maxOutputTokens = numericRecordValue(profile.payload, "maxTokens")
    ?? numericRecordValue(profile.payload, "maxOutputTokens");
  return {
    ...(temperature !== undefined ? { temperature: Math.max(0, Math.min(2, temperature)) } : {}),
    ...(topP !== undefined ? { topP: Math.max(0, Math.min(1, topP)) } : {}),
    ...(frequencyPenalty !== undefined
      ? { frequencyPenalty: Math.max(-2, Math.min(2, frequencyPenalty)) }
      : {}),
    ...(presencePenalty !== undefined ? { presencePenalty: Math.max(-2, Math.min(2, presencePenalty)) } : {}),
    ...(maxOutputTokens !== undefined
      ? { maxOutputTokens: Math.max(1, Math.min(100000, Math.floor(maxOutputTokens))) }
      : {})
  };
};

const resolveModelContextProfile = (
  profileId: string | undefined,
  modelName: string,
  metadataStore: MetadataStore,
  userId: string,
  workspaceId: string
): AgentModelContextProfile | undefined => {
  if (!profileId || profileId === "server-default") {
    return undefined;
  }
  const profile = metadataStore.configResources.get({
    id: profileId,
    workspace_id: workspaceId,
    user_id: userId,
    kind: "model-profile"
  });
  const contextLength = numericRecordValue(profile.payload, "contextLength")
    ?? numericRecordValue(profile.payload, "context_length");
  if (contextLength === undefined) {
    return undefined;
  }
  const contextWindow = Math.max(8192, Math.min(2_000_000, Math.floor(contextLength)));
  const maxOutputTokens = numericRecordValue(profile.payload, "maxTokens")
    ?? numericRecordValue(profile.payload, "maxOutputTokens");
  const outputReserve = Math.max(
    256,
    Math.min(Math.floor(contextWindow * 0.4), Math.floor(maxOutputTokens ?? 4096))
  );
  const safetyMargin = Math.max(512, Math.min(4096, Math.floor(contextWindow * 0.05)));
  return {
    id: `profile:${profileId}`,
    modelPattern: modelName || "*",
    contextWindow,
    outputReserve,
    safetyMargin,
    messageOverhead: 4,
    toolSchemaOverhead: 32
  };
};

const resolveReasoningModel = (
  profileId: string | undefined,
  metadataStore: MetadataStore,
  userId: string,
  workspaceId: string
): boolean | undefined => {
  if (!profileId || profileId === "server-default") {
    return undefined;
  }
  const profile = metadataStore.configResources.get({
    id: profileId,
    workspace_id: workspaceId,
    user_id: userId,
    kind: "model-profile"
  });
  return booleanRecordValue(profile.payload, "reasoningModel")
    ?? booleanRecordValue(profile.payload, "reasoning_model");
};

const resolveRunTimeoutMs = (
  profileId: string | undefined,
  metadataStore: MetadataStore,
  userId: string,
  workspaceId: string
): number | undefined => {
  if (!profileId || profileId === "server-default") {
    return undefined;
  }
  const profile = metadataStore.configResources.get({
    id: profileId,
    workspace_id: workspaceId,
    user_id: userId,
    kind: "model-profile"
  });
  const timeoutMs = numericRecordValue(profile.payload, "timeoutMs")
    ?? numericRecordValue(profile.payload, "timeout_ms");
  return timeoutMs !== undefined ? Math.max(1000, Math.min(10 * 60 * 1000, Math.floor(timeoutMs))) : undefined;
};

const resolveMcpRuntime = (
  serverIds: string[],
  metadataStore: MetadataStore,
  userId: string,
  workspaceId: string
): McpRuntime => {
  const usedNames = new Set<string>(STATIC_AGENT_TOOL_NAMES);
  const toolNames: string[] = [];
  const toolNamesByServerId: Record<string, string[]> = {};
  const servers: PolicyMcpClientConfig[] = [];
  const skipped: { id: string; reason: string }[] = [];
  for (const id of serverIds) {
    try {
      const startIndex = toolNames.length;
      servers.push(buildMcpServerConfig({
        id,
        metadataStore,
        usedNames,
        toolNames,
        userId,
        workspaceId
      }));
      toolNamesByServerId[id] = toolNames.slice(startIndex);
    } catch (error) {
      skipped.push({
        id,
        reason: error instanceof Error ? error.message : String(error)
      });
    }
  }
  return {
    servers,
    toolNames,
    toolNamesByServerId,
    ...(skipped.length > 0 ? { skipped } : {})
  };
};

const buildMcpServerConfig = (input: {
  id: string;
  metadataStore: MetadataStore;
  usedNames: Set<string>;
  toolNames: string[];
  userId: string;
  workspaceId: string;
}): PolicyMcpClientConfig => {
  const { id, metadataStore, usedNames, toolNames, userId, workspaceId } = input;
  const resource = metadataStore.configResources.get({
    id,
    workspace_id: workspaceId,
    user_id: userId,
    kind: "mcp-server"
  });
  const transport = stringRecordValue(resource.payload, "transport") ?? "streamable-http";
  if (transport !== "streamable-http" && transport !== "sse" && transport !== "stdio") {
    throw new Error(`MCP_TRANSPORT_UNSUPPORTED:${id}:${transport}`);
  }
  const urlOrCommand = stringRecordValue(resource.payload, "serverUrl")
    ?? stringRecordValue(resource.payload, "url")
    ?? (transport === "stdio" ? stringRecordValue(resource.payload, "command") : undefined);
  if (!urlOrCommand) {
    throw new Error(transport === "stdio" ? `MCP_STDIO_COMMAND_REQUIRED:${id}` : `MCP_SERVER_URL_REQUIRED:${id}`);
  }
  const manifest = resource.payload.toolManifest;
  if (!Array.isArray(manifest)) {
    throw new Error(`MCP_TOOL_MANIFEST_REQUIRED:${id}`);
  }
  const toolAllowlist = stringArrayRecordValue(resource.payload, "toolAllowlist")
    ?? csvRecordValue(resource.payload, "toolAllowlist");
  const tools: PolicyMcpToolConfig[] = [];
  manifest.forEach((tool) => {
    if (!isRecord(tool) || typeof tool.name !== "string") {
      throw new Error(`MCP_TOOL_MANIFEST_INVALID:${id}`);
    }
    if (!matchesMcpToolAllowlist(id, tool.name, toolAllowlist)) {
      return;
    }
    if (!isValidAgentToolName(tool.name)) {
      throw new Error(`MCP_TOOL_NAME_INVALID:${id}:${tool.name}`);
    }
    if (usedNames.has(tool.name)) {
      throw new Error(`MCP_TOOL_NAME_CONFLICT:${id}:${tool.name}`);
    }
    usedNames.add(tool.name);
    toolNames.push(tool.name);
    tools.push({
      name: tool.name,
      ...(typeof tool.description === "string" ? { description: tool.description } : {}),
      ...(tool.inputSchema !== undefined ? { inputSchema: tool.inputSchema } : {})
    });
  });
  const secret = resource.secret_ref
    ? metadataStore.secrets.get({ ref: resource.secret_ref, workspace_id: workspaceId, user_id: userId })
    : {};
  const headers = resolveMcpHeaders(resource.payload, secret);
  const timeoutMs = numericRecordValue(resource.payload, "timeoutMs")
    ?? numericRecordValue(resource.payload, "timeout_ms");
  const common = {
    serverId: id,
    tools,
    ...(toolAllowlist && toolAllowlist.length > 0 ? { toolAllowlist } : {}),
    ...(timeoutMs !== undefined ? { timeoutMs: Math.max(1000, Math.min(10 * 60 * 1000, Math.floor(timeoutMs))) } : {})
  };
  if (transport === "stdio") {
    const stdio = resolveStdioCommand(resource.payload, urlOrCommand);
    const cwd = stringRecordValue(resource.payload, "cwd");
    const env = recordStringMapValue(resource.payload.env);
    return {
      ...common,
      type: "stdio",
      command: stdio.command,
      ...(stdio.args.length > 0 ? { args: stdio.args } : {}),
      ...(cwd ? { cwd } : {}),
      ...(env ? { env } : {})
    };
  }
  return {
    ...common,
    type: transport === "streamable-http" ? "http" : "sse",
    url: urlOrCommand,
    ...(Object.keys(headers).length > 0 ? { headers } : {})
  };
};

const resolveMcpHeaders = (
  payload: Record<string, unknown>,
  secret: Record<string, unknown>
): Record<string, string> => {
  const configured = isRecord(secret.headers)
    ? Object.fromEntries(Object.entries(secret.headers).filter((entry): entry is [string, string] =>
        typeof entry[1] === "string"))
    : {};
  const token = stringRecordValue(secret, "token") ?? stringRecordValue(secret, "apiKey");
  const authType = stringRecordValue(payload, "authType") ?? "none";
  if (authType === "bearer" && token) {
    return { ...configured, Authorization: `Bearer ${token}` };
  }
  return configured;
};

const enforceSkillMcpPolicy = (
  allowedTools: string[] | undefined,
  mcpToolNames: string[]
): void => {
  if (!allowedTools || mcpToolNames.length === 0) {
    return;
  }
  const disallowed = mcpToolNames.filter((name) => !allowedTools.includes(name));
  if (disallowed.length > 0) {
    throw new Error(`SKILL_MCP_TOOL_POLICY_UNSUPPORTED:${disallowed.join(",")}`);
  }
};

const sanitizeMcpName = (value: string): string => value.replace(/[^a-zA-Z0-9_-]/gu, "_");

const isValidAgentToolName = (value: string): boolean =>
  value.length > 0 && value.length <= 64 && /^[a-zA-Z0-9_-]+$/u.test(value);

const matchesMcpToolAllowlist = (
  serverId: string,
  toolName: string,
  toolAllowlist: string[] | undefined
): boolean => {
  if (!toolAllowlist || toolAllowlist.length === 0) {
    return true;
  }
  return mcpToolAllowlistCandidates(toolName).some((candidate) => {
    const baseName = `mcp__${sanitizeMcpName(serverId)}__${sanitizeMcpName(candidate)}`;
    return toolAllowlist.includes(candidate) || toolAllowlist.includes(baseName);
  });
};

const mcpToolAllowlistCandidates = (toolName: string): string[] => {
  if (toolName === "datalink_show" || toolName === "datagraph_show") {
    return ["datalink_show", "datagraph_show"];
  }
  if (toolName === "datalink_explore" || toolName === "datagraph_explore") {
    return ["datalink_explore", "datagraph_explore"];
  }
  return [toolName];
};

const resolveStdioCommand = (
  payload: Record<string, unknown>,
  fallbackCommand: string
): { args: string[]; command: string } => {
  const command = stringRecordValue(payload, "command");
  const args = stringArrayRecordValue(payload, "args");
  if (command) {
    return { command, args: args ?? [] };
  }
  const parts = splitCommandLine(fallbackCommand);
  const head = parts[0];
  if (!head) {
    throw new Error("MCP_STDIO_COMMAND_REQUIRED");
  }
  return { command: head, args: parts.slice(1) };
};

const hasAnyKey = (record: Record<string, unknown>, keys: string[]): boolean =>
  keys.some((key) => record[key] !== undefined);

const unique = <T>(values: T[]): T[] => [...new Set(values)];

/**
 * Validate config resource IDs. For skills, a `disabled`/`archived` status still throws
 * (fail-closed). For non-skill resources, `default_enabled=false` no longer throws —
 * instead the ID is returned in `dropped` so the caller can silently remove it from the
 * enabled set and report it as `disabled_by_policy` (R-020). Existence of the resource
 * record is still required (a missing record throws `CONFIG_RESOURCE_NOT_FOUND`).
 */
const validateConfigIds = (
  metadataStore: MetadataStore,
  userId: string,
  workspaceId: string,
  kind: "knowledge-base" | "mcp-server" | "model-profile" | "skill",
  ids: string[]
): { dropped: string[] } => {
  const dropped: string[] = [];
  ids.forEach((id) => {
    const resource = metadataStore.configResources.get({
      id,
      workspace_id: workspaceId,
      user_id: userId,
      kind
    });
    if (kind === "skill" && (resource.status === "disabled" || resource.status === "archived")) {
      throw new Error(`CONFIG_RESOURCE_NOT_ENABLED:${kind}:${id}`);
    }
    if (kind !== "skill" && !resource.default_enabled) {
      // R-020: degrade instead of failing the whole run.
      dropped.push(id);
    }
  });
  return { dropped };
};

const stringRecordValue = (record: Record<string, unknown>, key: string): string | undefined => {
  const value = record[key];
  return typeof value === "string" && value.trim() ? value : undefined;
};

const csvRecordValue = (record: Record<string, unknown>, key: string): string[] | undefined => {
  const value = stringRecordValue(record, key);
  const items = value?.split(",").map((item) => item.trim()).filter(Boolean);
  return items && items.length > 0 ? items : undefined;
};

const stringArrayRecordValue = (record: Record<string, unknown>, key: string): string[] | undefined => {
  const value = record[key];
  if (!Array.isArray(value)) {
    return undefined;
  }
  const items = value.filter((item): item is string => typeof item === "string" && item.trim().length > 0);
  return items.length > 0 ? items : undefined;
};

const recordStringMapValue = (value: unknown): Record<string, string> | undefined => {
  if (!isRecord(value)) {
    return undefined;
  }
  const entries = Object.entries(value).filter((entry): entry is [string, string] => typeof entry[1] === "string");
  return entries.length > 0 ? Object.fromEntries(entries) : undefined;
};

const numericRecordValue = (record: Record<string, unknown>, key: string): number | undefined => {
  const value = record[key];
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
};

const booleanRecordValue = (record: Record<string, unknown>, key: string): boolean | undefined => {
  const value = record[key];
  return typeof value === "boolean" ? value : undefined;
};

const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null;

const splitCommandLine = (value: string): string[] => {
  const parts: string[] = [];
  let current = "";
  let quote: "\"" | "'" | undefined;
  for (let index = 0; index < value.length; index += 1) {
    const char = value[index];
    if ((char === "\"" || char === "'") && !quote) {
      quote = char;
      continue;
    }
    if (char === quote) {
      quote = undefined;
      continue;
    }
    if (/\s/u.test(char ?? "") && !quote) {
      if (current) {
        parts.push(current);
        current = "";
      }
      continue;
    }
    current += char;
  }
  if (current) {
    parts.push(current);
  }
  return parts;
};
