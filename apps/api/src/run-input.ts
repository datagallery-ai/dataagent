import type { RunAgentInput } from "@ag-ui/client";
import type { EvidenceKind, EvidenceRef, EvidenceSelection } from "@datafoundry/contracts";
import type { ConfigResourceKind, MetadataStore } from "@datafoundry/metadata";
import type { SkillMode, SkillPolicyConfig } from "@datafoundry/skills";

import { preferConnectedResourceId } from "./model-profile-connection-status.js";

export type RunConfigDefaults = {
  activeDatasourceId?: string;
  activeLlmProfileId?: string;
  activeSkillId?: string;
  enabledDatasourceIds: string[];
  enabledKnowledgeIds: string[];
  enabledMcpServerIds: string[];
  enabledSkillIds: string[];
};

export type EffectiveRunConfig = {
  activeDatasourceId?: string;
  activeLlmProfileId?: string;
  activeSkillId?: string;
  enabledDatasourceIds: string[];
  fileIds: string[];
  enabledKnowledgeIds: string[];
  enabledMcpServerIds: string[];
  enabledSkillIds: string[];
  skillIds: string[];
  skillMode: SkillMode;
  skillPolicy: SkillPolicyConfig;
  skillTags: string[];
  resourceRevisions?: Record<string, number>;
  goal?: {
    maxRuns?: number;
    objective: string;
  };
  /**
   * Per-run @ mentions (R-019). Each kind lists the IDs the user explicitly focused on
   * this run — a *focus* signal, not a narrowing of `enabled*Ids`. IDs that fall outside
   * the corresponding enabled set are dropped (and surfaced as `mentioned_excluded` for
   * diagnostics) rather than failing the run.
   */
  mentioned?: {
    db: string[];
    kb: string[];
    mcp: string[];
    skill: string[];
    excluded?: { kind: PerRunMentionKind; id: string }[];
  };
  /** Per-run pinned session-relative paths (R-024). Sanitized, escape-checked. */
  pinnedPaths?: string[];
  /** User-selected evidence references for this run. Resolved server-side before prompt assembly. */
  evidenceRefs: EvidenceRef[];
  protocol?: {
    protocolId: string;
    protocolVersion: string;
  };
  /**
   * Resources silently dropped from `enabled*Ids` because `default_enabled=false` (R-020).
   * The run continues; this list is surfaced in `run.config.resolved` for diagnostics.
   */
  disabledByPolicy?: { kind: "knowledge-base" | "mcp-server" | "model-profile"; id: string }[];
  /**
   * Resources silently dropped because runtime configuration is invalid (missing manifest,
   * URL, etc.). The run continues without those MCP tools.
   */
  unavailableResources?: { kind: "mcp-server"; id: string; reason: string }[];
};

export type PerRunMentionKind = "db" | "kb" | "mcp" | "skill";

/** Parse and validate the frontend run_config into the backend's effective run policy. */
export const extractEffectiveRunConfig = (
  input: RunAgentInput,
  defaultDatasourceId?: string,
  defaults?: RunConfigDefaults
): EffectiveRunConfig => {
  const runConfig = extractRunConfigRecord(input);
  const legacyDatasourceId = extractDatasourceId(input);
  const configuredDatasourceId = stringFromAliases(runConfig, ["activeDatasourceId", "active_datasource_id"]);
  const datasourceOverride = stringArrayOptionFromAliases(
    runConfig,
    ["enabledDatasourceIds", "enabled_datasource_ids"]
  );
  const requestedActiveDatasourceId = configuredDatasourceId ?? legacyDatasourceId ?? datasourceOverride?.[0]
    ?? defaults?.activeDatasourceId
    ?? (datasourceOverride === undefined ? defaultDatasourceId : undefined);
  const effectiveDatasourceIds = unique(
    datasourceOverride ?? defaults?.enabledDatasourceIds
      ?? (requestedActiveDatasourceId ? [requestedActiveDatasourceId] : [])
  );
  const activeDatasourceId = effectiveDatasourceIds.length === 0
    ? undefined
    : (requestedActiveDatasourceId && effectiveDatasourceIds.includes(requestedActiveDatasourceId)
      ? requestedActiveDatasourceId
      : effectiveDatasourceIds[0]);
  const activeLlmProfileId = stringFromAliases(runConfig, ["activeLlmProfileId", "active_llm_profile_id"])
    ?? defaults?.activeLlmProfileId;
  const skillOverride = stringArrayOptionFromAliases(runConfig, ["enabledSkillIds", "enabled_skill_ids"]);
  const skillIdsOverride = stringArrayOptionFromAliases(runConfig, ["skillIds", "skill_ids"]);
  const configuredSkillId = stringFromAliases(runConfig, ["activeSkillId", "active_skill_id"]);
  const activeSkillId = configuredSkillId
    ?? (skillIdsOverride ? skillIdsOverride[0] : skillOverride ? skillOverride[0] : defaults?.activeSkillId);
  const enabledSkillIds = unique(
    skillOverride ?? defaults?.enabledSkillIds ?? (configuredSkillId ? [configuredSkillId] : [])
  );
  const skillMode = skillModeFromValue(runConfig.skillMode ?? runConfig.skill_mode, skillIdsOverride);
  const skillPolicy = extractSkillPolicy(runConfig);
  const skillTags = unique(stringArrayOptionFromAliases(runConfig, ["skillTags", "skill_tags"]) ?? []);
  const goal = extractGoal(runConfig);
  // R-019: parse per-run @ mentions. Focus signal (not narrowing). IDs outside the
  // matching enabled*Ids set are dropped and collected into `excluded[]` for diagnostics.
  const mentionedRaw = perRunSelectionFromAliases(runConfig, ["mentioned"]);
  const enabledKnowledgeIds = unique(stringArrayOptionFromAliases(
    runConfig,
    ["enabledKnowledgeIds", "enabled_knowledge_ids"]
  ) ?? defaults?.enabledKnowledgeIds ?? []);
  const enabledMcpServerIds = unique(stringArrayOptionFromAliases(
    runConfig,
    ["enabledMcpServerIds", "enabled_mcp_server_ids"]
  ) ?? defaults?.enabledMcpServerIds ?? []);
  const mentioned = clampMentioned(mentionedRaw, {
    db: effectiveDatasourceIds,
    kb: enabledKnowledgeIds,
    mcp: enabledMcpServerIds,
    skill: enabledSkillIds
  });
  // R-024: parse pinned session-relative paths. Drop anything that escapes or is unsafe.
  const pinnedPaths = pinnedPathsFromAliases(runConfig, ["pinnedPaths", "pinned_paths"]);
  const evidenceRefs = evidenceRefsFromAliases(runConfig, ["evidenceRefs", "evidence_refs"]);
  const protocol = protocolSelectionFromRunConfig(runConfig);

  if (
    activeDatasourceId
    && effectiveDatasourceIds.length > 0
    && !effectiveDatasourceIds.includes(activeDatasourceId)
  ) {
    throw new Error("ACTIVE_DATASOURCE_NOT_ENABLED");
  }
  if (activeSkillId && !enabledSkillIds.includes(activeSkillId)) {
    throw new Error("ACTIVE_SKILL_NOT_ENABLED");
  }

  return {
    ...(activeDatasourceId ? { activeDatasourceId } : {}),
    ...(activeLlmProfileId ? { activeLlmProfileId } : {}),
    ...(activeSkillId ? { activeSkillId } : {}),
    enabledDatasourceIds: effectiveDatasourceIds,
    fileIds: unique(stringArrayOptionFromAliases(runConfig, ["fileIds", "file_ids"]) ?? []),
    enabledKnowledgeIds,
    enabledMcpServerIds,
    enabledSkillIds,
    skillIds: unique(skillIdsOverride ?? (configuredSkillId ? [configuredSkillId] : [])),
    skillMode,
    skillPolicy,
    skillTags,
    ...(goal ? { goal } : {}),
    ...(mentioned ? { mentioned } : {}),
    ...(pinnedPaths.length > 0 ? { pinnedPaths } : {}),
    ...(protocol ? { protocol } : {}),
    evidenceRefs
  };
};

/** Resolve workspace defaults, per-run overrides, and immutable resource revisions for one run. */
export const resolveEffectiveRunConfig = (
  input: RunAgentInput,
  metadataStore: MetadataStore,
  userId: string,
  defaultDatasourceId?: string,
  workspaceId = "default"
): EffectiveRunConfig => {
  const defaults = loadWorkspaceRunDefaults(metadataStore, userId, workspaceId);
  const config = extractEffectiveRunConfig(input, defaultDatasourceId, defaults);
  return {
    ...config,
    resourceRevisions: resolveResourceRevisions(config, metadataStore, userId, workspaceId)
  };
};

const loadWorkspaceRunDefaults = (
  metadataStore: MetadataStore,
  userId: string,
  workspaceId: string
): RunConfigDefaults => {
  const datasourceIds = metadataStore.dataSources.list({ user_id: userId })
    .filter((item) => {
      const config = recordFromUnknown(item.config_json) ?? {};
      return item.status === "ready" && config.defaultEnabled !== false;
    })
    .map((item) => item.id);
  const enabled = (kind: ConfigResourceKind) => metadataStore.configResources.list({
    workspace_id: workspaceId,
    user_id: userId,
    kind
  }).filter((item) => item.default_enabled && item.status !== "disabled");
  const modelProfiles = enabled("model-profile");
  const skillIds = enabled("skill").map((item) => item.id);
  const activeLlmProfileId = preferConnectedResourceId(modelProfiles);
  return {
    ...(datasourceIds[0] ? { activeDatasourceId: datasourceIds[0] } : {}),
    ...(activeLlmProfileId ? { activeLlmProfileId } : {}),
    ...(skillIds[0] ? { activeSkillId: skillIds[0] } : {}),
    enabledDatasourceIds: datasourceIds,
    enabledKnowledgeIds: enabled("knowledge-base").map((item) => item.id),
    enabledMcpServerIds: enabled("mcp-server").map((item) => item.id),
    enabledSkillIds: skillIds
  };
};

const resolveResourceRevisions = (
  config: EffectiveRunConfig,
  metadataStore: MetadataStore,
  userId: string,
  workspaceId: string
): Record<string, number> => {
  const revisions: Record<string, number> = {};
  config.enabledDatasourceIds.forEach((id) => {
    revisions[`datasource:${id}`] = metadataStore.dataSources.get({ user_id: userId, datasource_id: id }).revision;
  });
  const addResources = (kind: ConfigResourceKind, ids: string[]): void => {
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
    const visited = new Set<string>();
    let profileId: string | undefined = config.activeLlmProfileId;
    while (profileId && !visited.has(profileId)) {
      visited.add(profileId);
      const profile = metadataStore.configResources.get({
        id: profileId,
        workspace_id: workspaceId,
        user_id: userId,
        kind: "model-profile"
      });
      revisions[`model-profile:${profileId}`] = profile.revision;
      profileId = typeof profile.payload.fallbackProfileId === "string"
        ? profile.payload.fallbackProfileId
        : undefined;
    }
  }
  return revisions;
};

const extractGoal = (runConfig: Record<string, unknown>): EffectiveRunConfig["goal"] => {
  const goal = recordFromUnknown(runConfig.goal);
  if (!goal) {
    return undefined;
  }
  const objective = stringFromAliases(goal, ["objective"]);
  if (!objective) {
    throw new Error("GOAL_OBJECTIVE_REQUIRED");
  }
  const rawMaxRuns = goal.maxRuns ?? goal.max_runs;
  const maxRunsInvalid = rawMaxRuns !== undefined
    && (!Number.isInteger(rawMaxRuns) || Number(rawMaxRuns) < 1 || Number(rawMaxRuns) > 20);
  if (maxRunsInvalid) {
    throw new Error("GOAL_MAX_RUNS_INVALID");
  }
  return {
    objective,
    ...(rawMaxRuns !== undefined ? { maxRuns: Number(rawMaxRuns) } : {})
  };
};

const extractSkillPolicy = (runConfig: Record<string, unknown>): SkillPolicyConfig => {
  const policy = recordFromUnknown(runConfig.skillPolicy ?? runConfig.skill_policy) ?? {};
  const maxSkills = integerInRange(policy.maxSkills ?? policy.max_skills, 1, 20) ?? 20;
  const allowedToolNames = unique(
    stringArrayOptionFromAliases(policy, ["allowedToolNames", "allowed_tool_names"]) ?? []
  );
  return {
    ...(allowedToolNames.length > 0 ? { allowedToolNames } : {}),
    deniedToolNames: unique(stringArrayOptionFromAliases(
      policy,
      ["deniedToolNames", "denyToolNames", "denied_tool_names", "deny_tool_names"]
    ) ?? []),
    maxSkills,
    requireUserInvocable: booleanFromAliases(policy, ["requireUserInvocable", "require_user_invocable"], true),
    strictSkillTools: booleanFromAliases(policy, ["strictSkillTools", "strict_skill_tools"], false)
  };
};

const skillModeFromValue = (value: unknown, explicitSkillIds: string[] | undefined): SkillMode => {
  if (value === "none" || value === "selected" || value === "auto" || value === "all") {
    return value;
  }
  return explicitSkillIds && explicitSkillIds.length > 0 ? "selected" : "auto";
};

const booleanFromAliases = (record: Record<string, unknown>, keys: string[], fallback: boolean): boolean => {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "boolean") {
      return value;
    }
  }
  return fallback;
};

const integerInRange = (value: unknown, min: number, max: number): number | undefined =>
  Number.isInteger(value) && Number(value) >= min && Number(value) <= max ? Number(value) : undefined;

export const extractDatasourceId = (input: RunAgentInput): string | undefined => {
  const forwardedProps = isRecord(input.forwardedProps) ? input.forwardedProps : {};
  const state = isRecord(input.state) ? input.state : {};
  const contextDatasourceId = input.context.find((item) => item.description === "datasource_id")?.value;
  const forwardedDatasourceId =
    stringFromRecord(forwardedProps, "datasourceId") ?? stringFromRecord(forwardedProps, "datasource_id");
  const stateDatasourceId = stringFromRecord(state, "datasourceId") ?? stringFromRecord(state, "datasource_id");

  return forwardedDatasourceId ?? stateDatasourceId ?? contextDatasourceId;
};

export const extractLastUserText = (input: RunAgentInput): string | undefined => {
  const userMessage = [...input.messages].reverse().find((message) => message.role === "user");
  const content = userMessage?.content;

  if (typeof content === "string") {
    return content;
  }

  if (!Array.isArray(content)) {
    return undefined;
  }

  return content
    .filter(
      (part): part is { type: "text"; text: string } =>
        isRecord(part) && part.type === "text" && typeof part.text === "string"
    )
    .map((part) => part.text)
    .join("\n")
    .trim();
};

const stringFromRecord = (record: Record<string, unknown>, key: string): string | undefined => {
  const value = record[key];
  return typeof value === "string" && value.trim() ? value : undefined;
};

const extractRunConfigRecord = (input: RunAgentInput): Record<string, unknown> => {
  const forwardedProps = isRecord(input.forwardedProps) ? input.forwardedProps : {};
  const state = isRecord(input.state) ? input.state : {};
  const contextValue = input.context.find((item) => item.description === "run_config")?.value;
  return recordFromUnknown(forwardedProps.run_config ?? forwardedProps.runConfig) ??
    recordFromUnknown(state.run_config ?? state.runConfig) ?? recordFromUnknown(contextValue) ?? {};
};

const recordFromUnknown = (value: unknown): Record<string, unknown> | undefined => {
  if (isRecord(value)) {
    return value;
  }
  if (typeof value !== "string" || !value.trim()) {
    return undefined;
  }
  try {
    const parsed: unknown = JSON.parse(value);
    return isRecord(parsed) ? parsed : undefined;
  } catch {
    throw new Error("INVALID_RUN_CONFIG_JSON");
  }
};

const stringFromAliases = (record: Record<string, unknown>, aliases: string[]): string | undefined => {
  for (const alias of aliases) {
    const value = stringFromRecord(record, alias);
    if (value) {
      return value;
    }
  }
  return undefined;
};

const protocolSelectionFromRunConfig = (
  runConfig: Record<string, unknown>
): EffectiveRunConfig["protocol"] => {
  const value = runConfig.protocol;
  if (value === undefined) {
    return undefined;
  }
  if (!isRecord(value)) {
    throw new Error("INVALID_PROTOCOL_SELECTION");
  }
  const protocolId = stringFromAliases(value, ["id", "protocolId", "protocol_id"]);
  const protocolVersion = stringFromAliases(value, ["version", "protocolVersion", "protocol_version"]);
  if (!protocolId || !protocolVersion) {
    throw new Error("INVALID_PROTOCOL_SELECTION");
  }
  return { protocolId, protocolVersion };
};

const stringArrayFromAliases = (record: Record<string, unknown>, aliases: string[]): string[] => {
  for (const alias of aliases) {
    const value = record[alias];
    if (Array.isArray(value)) {
      return value.filter((item): item is string => typeof item === "string" && item.trim().length > 0);
    }
  }
  return [];
};

const stringArrayOptionFromAliases = (record: Record<string, unknown>, aliases: string[]): string[] | undefined => {
  for (const alias of aliases) {
    if (alias in record) {
      return stringArrayFromAliases(record, [alias]);
    }
  }
  return undefined;
};

const unique = (values: string[]): string[] => [...new Set(values)];

const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null;

/**
 * Parse the per-run `mentioned` selection (R-019). Accepts snake_case or camelCase keys
 * for each kind. Returns undefined when no `mentioned` field is present (backward compat).
 */
const perRunSelectionFromAliases = (
  record: Record<string, unknown>,
  aliases: string[]
): Record<PerRunMentionKind, string[]> | undefined => {
  let raw: unknown;
  for (const alias of aliases) {
    if (alias in record) {
      raw = record[alias];
      break;
    }
  }
  if (raw === undefined) {
    return undefined;
  }
  if (!isRecord(raw)) {
    return { db: [], kb: [], mcp: [], skill: [] };
  }
  const kinds: PerRunMentionKind[] = ["db", "kb", "mcp", "skill"];
  const result = {} as Record<PerRunMentionKind, string[]>;
  for (const kind of kinds) {
    const value = raw[kind] ?? raw[`${kind}_ids`];
    result[kind] = Array.isArray(value)
      ? value.filter((item): item is string => typeof item === "string" && item.trim().length > 0)
      : [];
  }
  return result;
};

/**
 * Clamp mentioned IDs to their enabled*Ids subsets (R-019 validation). Out-of-scope IDs
 * are dropped (not thrown) and collected into `excluded[]` so the run continues and the
 * frontend can surface a diagnostic. Returns undefined only when mentioned is undefined.
 */
const clampMentioned = (
  mentioned: Record<PerRunMentionKind, string[]> | undefined,
  enabled: Record<PerRunMentionKind, string[]>
): EffectiveRunConfig["mentioned"] | undefined => {
  if (!mentioned) {
    return undefined;
  }
  const kinds: PerRunMentionKind[] = ["db", "kb", "mcp", "skill"];
  const excluded: { kind: PerRunMentionKind; id: string }[] = [];
  const clamped = {} as Record<PerRunMentionKind, string[]>;
  for (const kind of kinds) {
    const allowed = new Set(enabled[kind]);
    const kept: string[] = [];
    for (const id of mentioned[kind]) {
      if (allowed.has(id)) {
        kept.push(id);
      } else {
        excluded.push({ kind, id });
      }
    }
    clamped[kind] = unique(kept);
  }
  return {
    db: clamped.db,
    kb: clamped.kb,
    mcp: clamped.mcp,
    skill: clamped.skill,
    ...(excluded.length > 0 ? { excluded } : {})
  };
};

/**
 * Parse `pinnedPaths` (R-024). Each entry must be a session-relative path: non-empty,
 * not absolute, no NUL bytes, no `..` traversal. Invalid entries are silently dropped.
 */
const pinnedPathsFromAliases = (record: Record<string, unknown>, aliases: string[]): string[] => {
  const raw = stringArrayOptionFromAliases(record, aliases);
  if (!raw || raw.length === 0) {
    return [];
  }
  const safe: string[] = [];
  for (const candidate of raw) {
    const trimmed = candidate.trim();
    if (trimmed.length === 0 || trimmed.startsWith("/") || trimmed.includes("\0")) {
      continue;
    }
    // Reject any segment that is exactly ".." (path traversal into parent).
    const segments = trimmed.split(/[/\\]+/);
    if (segments.some((segment) => segment === "..")) {
      continue;
    }
    safe.push(trimmed);
  }
  return unique(safe);
};

const EVIDENCE_REF_LIMIT = 20;

const evidenceRefsFromAliases = (record: Record<string, unknown>, aliases: string[]): EvidenceRef[] => {
  let raw: unknown;
  for (const alias of aliases) {
    if (alias in record) {
      raw = record[alias];
      break;
    }
  }
  if (!Array.isArray(raw)) {
    return [];
  }
  const refs: EvidenceRef[] = [];
  const seen = new Set<string>();
  for (const entry of raw) {
    const ref = evidenceRefFromUnknown(entry);
    if (!ref || seen.has(ref.id)) continue;
    seen.add(ref.id);
    refs.push(ref);
    if (refs.length >= EVIDENCE_REF_LIMIT) break;
  }
  return refs;
};

const evidenceRefFromUnknown = (value: unknown): EvidenceRef | undefined => {
  if (!isRecord(value)) {
    return undefined;
  }
  const id = stringFromRecord(value, "id");
  const kindValue = stringFromRecord(value, "kind");
  const label = stringFromRecord(value, "label");
  const sessionId = stringFromAliases(value, ["sessionId", "session_id"]);
  if (!id || !isEvidenceKind(kindValue) || !label || !sessionId) {
    return undefined;
  }
  const summary = stringFromRecord(value, "summary");
  const runId = stringFromAliases(value, ["runId", "run_id"]);
  const source = evidenceRefSourceFromUnknown(value.source);
  return {
    id,
    kind: kindValue,
    label,
    ...(summary ? { summary } : {}),
    sessionId,
    ...(runId ? { runId } : {}),
    source
  };
};

const evidenceRefSourceFromUnknown = (value: unknown): EvidenceRef["source"] => {
  const source = isRecord(value) ? value : {};
  const selection = evidenceSelectionFromUnknown(source.selection);
  return {
    ...optionalStringField(source, "artifactId", "artifact_id"),
    ...optionalStringField(source, "toolCallId", "tool_call_id"),
    ...optionalStringField(source, "eventId", "event_id"),
    ...optionalStringField(source, "auditLogId", "audit_log_id"),
    ...optionalStringField(source, "fileId", "file_id"),
    ...optionalStringField(source, "datasourceId", "datasource_id"),
    ...optionalStringField(source, "tableName", "table_name"),
    ...optionalStringField(source, "documentId", "document_id"),
    ...optionalStringField(source, "chunkId", "chunk_id"),
    ...(selection ? { selection } : {})
  };
};

/** Parses fine-grained table/text selections so partial cites survive run_config intake. */
const evidenceSelectionFromUnknown = (value: unknown): EvidenceSelection | undefined => {
  if (!isRecord(value) || typeof value.mode !== "string") {
    return undefined;
  }
  if (value.mode === "text") {
    const quote = typeof value.quote === "string" ? value.quote.trim() : "";
    if (!quote) return undefined;
    const offset = typeof value.offset === "number" && Number.isFinite(value.offset)
      ? Math.trunc(value.offset)
      : undefined;
    return offset === undefined ? { mode: "text", quote } : { mode: "text", quote, offset };
  }
  if (value.mode !== "cells" && value.mode !== "rows" && value.mode !== "cols") {
    return undefined;
  }
  const range = evidenceCellRangeFromUnknown(value.range);
  if (!range) return undefined;
  const columns = Array.isArray(value.columns)
    ? value.columns.filter((column): column is string => typeof column === "string" && column.length > 0)
    : undefined;
  return columns && columns.length > 0
    ? { mode: value.mode, range, columns }
    : { mode: value.mode, range };
};

const evidenceCellRangeFromUnknown = (
  value: unknown
): { r0: number; c0: number; r1: number; c1: number } | undefined => {
  if (!isRecord(value)) return undefined;
  const r0 = finiteIndex(value.r0);
  const c0 = finiteIndex(value.c0);
  const r1 = finiteIndex(value.r1);
  const c1 = finiteIndex(value.c1);
  if (r0 === undefined || c0 === undefined || r1 === undefined || c1 === undefined) {
    return undefined;
  }
  return { r0, c0, r1, c1 };
};

const finiteIndex = (value: unknown): number | undefined =>
  typeof value === "number" && Number.isFinite(value) ? Math.trunc(value) : undefined;

const optionalStringField = (
  record: Record<string, unknown>,
  camelKey: keyof EvidenceRef["source"],
  snakeKey: string
): Partial<EvidenceRef["source"]> => {
  const value = stringFromAliases(record, [camelKey, snakeKey]);
  return value ? { [camelKey]: value } : {};
};

const isEvidenceKind = (value: unknown): value is EvidenceKind =>
  value === "table" ||
  value === "chart" ||
  value === "report" ||
  value === "file" ||
  value === "sql" ||
  value === "schema" ||
  value === "preview" ||
  value === "knowledge" ||
  value === "step";
