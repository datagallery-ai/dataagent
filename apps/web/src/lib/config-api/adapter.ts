import type {
  DatasourceDto,
  KnowledgeBaseDto,
  McpServerDto,
  ModelProfileDto,
  SkillDto,
  WorkspaceConfigDto,
} from "./types";
import type {
  ConfigItemStatus,
  WorkspaceConfigItem,
  WorkspaceConfigKind,
  WorkspaceConfigStore,
} from "../../app/data-tasks/data-task-state";

function asString(value: unknown): string {
  if (value === undefined || value === null) return "";
  return String(value);
}

function pickString(record: Record<string, unknown>, ...keys: string[]): string {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "string" && value.trim()) return value.trim();
    if (typeof value === "number" && Number.isFinite(value)) return String(value);
  }
  return "";
}

function normalizeLlmProvider(provider?: string): string {
  const normalized = provider?.trim().toLowerCase().replaceAll("_", "-") ?? "";
  if (
    normalized === "bailian"
    || normalized === "deepseek"
    || normalized === "openai"
    || normalized === "openai-compatible"
  ) {
    return "openai-compatible";
  }
  return normalized || "openai-compatible";
}

function pickBooleanString(record: Record<string, unknown>, key: string): string {
  const value = record[key];
  if (typeof value === "boolean") return value ? "true" : "false";
  if (value === "true" || value === "false") return value;
  return "";
}

function mapConnectionStatus(status?: string): ConfigItemStatus {
  if (status === "connected" || status === "ready") return "connected";
  if (status === "failed") return "failed";
  return "untested";
}

function mapResourceStatus(status?: string): ConfigItemStatus {
  if (status === "connected" || status === "valid" || status === "ready") {
    return "connected";
  }
  if (status === "failed" || status === "invalid") return "failed";
  return "untested";
}

export function datasourceDtoToItem(dto: DatasourceDto): WorkspaceConfigItem {
  const config = dto.config ?? {};
  const queryPolicy =
    typeof config.queryPolicy === "object" && config.queryPolicy !== null
      ? (config.queryPolicy as Record<string, unknown>)
      : {};
  const introspection =
    typeof config.introspection === "object" && config.introspection !== null
      ? (config.introspection as Record<string, unknown>)
      : {};
  const samplePolicy =
    typeof config.samplePolicy === "object" && config.samplePolicy !== null
      ? (config.samplePolicy as Record<string, unknown>)
      : {};
  const filePath = pickString(
    config,
    "filePath",
    "file_path",
    "path",
  );
  const tableAllowlist = Array.isArray(introspection.tableAllowlist)
    ? introspection.tableAllowlist.filter((value): value is string => typeof value === "string").join(", ")
    : pickString(introspection, "tableAllowlist");
  const maskFields = Array.isArray(config.maskFields)
    ? config.maskFields.filter((value): value is string => typeof value === "string").join(", ")
    : pickString(config, "maskFields");

  return {
    id: dto.id,
    name: dto.name,
    description: dto.description ?? "",
    enabled: dto.defaultEnabled ?? true,
    builtin: dto.builtin ?? false,
    secretRef: dto.secretRef ?? undefined,
    hasSecret: dto.hasSecret ?? false,
    revision: dto.revision,
    status: mapConnectionStatus(dto.connectionStatus),
    settings: {
      datasourceId: dto.id,
      type: dto.type,
      mode: dto.mode ?? "readonly",
      filePath,
      connectionString: pickString(config, "connectionString", "connection_string"),
      host: pickString(config, "host"),
      port: pickString(config, "port"),
      database: pickString(config, "database"),
      schema: pickString(config, "schema"),
      username: pickString(config, "username"),
      secure: pickBooleanString(config, "secure"),
      projectId: pickString(config, "projectId"),
      dataset: pickString(config, "dataset"),
      account: pickString(config, "account"),
      warehouse: pickString(config, "warehouse"),
      maxRows: pickString(queryPolicy, "maxRows"),
      timeoutMs: pickString(queryPolicy, "timeoutMs"),
      tableAllowlist,
      refreshIntervalSec: pickString(introspection, "refreshIntervalSec"),
      denyWrite: pickBooleanString(queryPolicy, "denyWrite") || "true",
      maskFields,
      allowSample: pickBooleanString(samplePolicy, "allowSample"),
      maxSampleRows: pickString(samplePolicy, "maxSampleRows"),
      connectionStatus: dto.connectionStatus ?? "untested",
    },
  };
}

export function knowledgeBaseDtoToItem(dto: KnowledgeBaseDto): WorkspaceConfigItem {
  const payloadEmbeddingProvider = dto.embeddingProvider;
  const payloadEmbeddingModel = dto.embeddingModel;
  const payloadEmbeddingBaseUrl = dto.embeddingBaseUrl;

  return {
    id: dto.id,
    name: dto.name,
    description: dto.description ?? "",
    enabled: dto.defaultEnabled ?? true,
    builtin: dto.builtin ?? false,
    secretRef: dto.secretRef ?? undefined,
    hasSecret: dto.hasSecret ?? false,
    revision: dto.revision,
    status: mapResourceStatus(dto.indexStatus),
    settings: {
      indexName: dto.id,
      retrievalTopK: asString(dto.retrievalTopK ?? 5),
      scoreThreshold: asString(dto.scoreThreshold ?? 0.2),
      embeddingProvider: payloadEmbeddingProvider ?? "bailian",
      embeddingModel: payloadEmbeddingModel ?? "text-embedding-v4",
      embeddingBaseUrl:
        payloadEmbeddingBaseUrl ??
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
      embeddingApiKey: "",
      vectorStore: dto.vectorStore ?? "local-sqlite",
      rerankEnabled: asString(dto.rerankEnabled ?? false),
      rerankModel: dto.rerankModel ?? "",
      citationRequired: asString(dto.citationRequired ?? true),
      chunkSize: asString(dto.chunkSize ?? 1600),
      chunkOverlap: asString(dto.chunkOverlap ?? 200),
      scope: dto.scope ?? "workspace",
      graphRagEnabled: asString(dto.graphRagEnabled ?? false),
      indexStatus: dto.indexStatus ?? "empty",
    },
  };
}

export function mcpServerDtoToItem(dto: McpServerDto): WorkspaceConfigItem {
  const toolNames = Array.isArray(dto.toolManifest)
    ? dto.toolManifest.flatMap((tool) => {
        const record = tool && typeof tool === "object" ? tool as Record<string, unknown> : {};
        return typeof record.name === "string" ? [record.name] : [];
      })
    : [];
  return {
    id: dto.id,
    name: dto.name,
    description: dto.description ?? "",
    enabled: dto.defaultEnabled ?? true,
    builtin: dto.builtin ?? false,
    secretRef: dto.secretRef ?? undefined,
    hasSecret: dto.hasSecret ?? false,
    revision: dto.revision,
    status: mapResourceStatus(dto.healthStatus),
    settings: {
      transport: dto.transport ?? "streamable-http",
      serverUrl: dto.serverUrl ?? "",
      apiUrl: dto.apiUrl ?? "",
      authType: dto.authType ?? "none",
      apiKey: "",
      toolAllowlist: Array.isArray(dto.toolAllowlist) ? dto.toolAllowlist.join(", ") : dto.toolAllowlist ?? "",
      timeoutMs: asString(dto.timeoutMs ?? ""),
      command: dto.command ?? "",
      args: Array.isArray(dto.args) ? dto.args.join(" ") : "",
      cwd: dto.cwd ?? "",
      env:
        dto.env && typeof dto.env === "object"
          ? JSON.stringify(dto.env, null, 2)
          : "",
      toolCount: asString(Array.isArray(dto.toolManifest) ? dto.toolManifest.length : 0),
      toolNames: toolNames.join(", "),
      healthStatus: dto.healthStatus ?? "untested",
    },
  };
}

export function modelProfileDtoToItem(dto: ModelProfileDto): WorkspaceConfigItem {
  return {
    id: dto.id,
    name: dto.name,
    description: dto.description ?? "",
    enabled: dto.defaultEnabled ?? true,
    builtin: dto.builtin ?? false,
    secretRef: dto.secretRef ?? undefined,
    hasSecret: dto.hasSecret ?? false,
    revision: dto.revision,
    status: mapConnectionStatus(dto.connectionStatus),
    settings: {
      provider: normalizeLlmProvider(dto.provider),
      baseUrl: dto.baseUrl ?? "",
      modelName: dto.modelName ?? "",
      apiKey: "",
      fallbackProfileId: dto.fallbackProfileId ?? "",
      temperature: asString(dto.temperature ?? ""),
      topP: asString(dto.topP ?? ""),
      frequencyPenalty: asString(dto.frequencyPenalty ?? ""),
      presencePenalty: asString(dto.presencePenalty ?? ""),
      maxTokens: asString(dto.maxTokens ?? ""),
      timeoutMs: asString(dto.timeoutMs ?? ""),
      contextLength: asString(dto.contextLength ?? ""),
      reasoningModel: dto.reasoningModel === true ? "true" : dto.reasoningModel === false ? "false" : "",
      connectionStatus: dto.connectionStatus ?? "untested",
    },
  };
}

export function skillDtoToItem(dto: SkillDto): WorkspaceConfigItem {
  const manifest = dto.manifest ?? {};
  const manifestFileName = Array.isArray(manifest.files) && manifest.files[0] ? manifest.files[0] : undefined;
  const packageFileName = (dto.packageFileName ?? asString(manifestFileName)) || "SKILL.md";
  const packageSource = dto.packageSource ?? (dto.builtin ? `builtin://${dto.id}` : "server");
  const hasPackageContent = Boolean(dto.packageFileRefId) || packageSource.startsWith("builtin://");
  return {
    id: dto.id,
    name: dto.name,
    description: dto.description ?? "",
    enabled: dto.defaultEnabled ?? true,
    builtin: dto.builtin ?? false,
    secretRef: dto.secretRef ?? undefined,
    hasSecret: dto.hasSecret ?? false,
    revision: dto.revision,
    status: mapResourceStatus(dto.validationStatus),
    settings: {
      packageFormat: dto.packageFormat ?? "skill-md",
      packageFileName,
      packageSource,
      packageContent: "",
      allowedTools: (dto.allowedTools ?? []).join(", "),
      packageVersion: dto.version ?? "",
      validationStatus: dto.validationStatus ?? "untested",
      hasPackageContent: hasPackageContent ? "true" : "false",
      defaultDbIds: (dto.defaultDbIds ?? []).join(", "),
      defaultKbIds: (dto.defaultKbIds ?? []).join(", "),
      defaultMcpIds: (dto.defaultMcpIds ?? []).join(", "),
      modelProfileId: dto.modelProfileId ?? "",
    },
  };
}

export function workspaceConfigDtoToStore(dto: WorkspaceConfigDto): WorkspaceConfigStore {
  return {
    db: dto.datasources.map(datasourceDtoToItem),
    kb: dto.knowledgeBases.map(knowledgeBaseDtoToItem),
    mcp: dto.mcpServers.map(mcpServerDtoToItem),
    llm: dto.modelProfiles.map(modelProfileDtoToItem),
    skill: dto.skills.map(skillDtoToItem),
  };
}

function parseNumber(value: string | undefined): number | undefined {
  if (!value?.trim()) return undefined;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : undefined;
}

function parseBoolean(value: string | undefined): boolean | undefined {
  if (value === "true") return true;
  if (value === "false") return false;
  return undefined;
}

function splitCsv(value: string | undefined): string[] | undefined {
  if (!value?.trim()) return undefined;
  const items = value
    .split(",")
    .map((entry) => entry.trim())
    .filter(Boolean);
  return items.length > 0 ? items : undefined;
}

function splitArgs(value: string | undefined): string[] | undefined {
  if (!value?.trim()) return undefined;
  const items = value.trim().split(/\s+/).filter(Boolean);
  return items.length > 0 ? items : undefined;
}

function parseEnvObject(value: string | undefined): Record<string, string> | undefined {
  if (!value?.trim()) return undefined;
  try {
    const parsed = JSON.parse(value) as unknown;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      return undefined;
    }
    const record: Record<string, string> = {};
    for (const [key, entry] of Object.entries(parsed as Record<string, unknown>)) {
      if (entry === null || entry === undefined) continue;
      record[key] = typeof entry === "string" ? entry : String(entry);
    }
    return Object.keys(record).length > 0 ? record : undefined;
  } catch {
    return undefined;
  }
}

function appendMcpStdioFields(
  body: Record<string, unknown>,
  settings: Record<string, string>,
): void {
  if (settings.command?.trim()) {
    body.command = settings.command.trim();
  }
  const args = splitArgs(settings.args);
  if (args) {
    body.args = args;
  }
  if (settings.cwd?.trim()) {
    body.cwd = settings.cwd.trim();
  }
  const env = parseEnvObject(settings.env);
  if (env) {
    body.env = env;
  }
}

function buildCredentials(
  settings: Record<string, string>,
  keys: Array<{ settingKey: string; credentialKey: string }>,
): Record<string, string> | undefined {
  const entries = keys.flatMap(({ settingKey, credentialKey }) => {
    const value = settings[settingKey]?.trim();
    return value ? [[credentialKey, value] as const] : [];
  });
  return entries.length > 0 ? Object.fromEntries(entries) : undefined;
}

export function itemToCreateBody(
  kind: WorkspaceConfigKind,
  item: WorkspaceConfigItem,
): Record<string, unknown> {
  const settings = item.settings ?? {};
  const base = {
    id: settings.datasourceId?.trim() || item.id,
    name: item.name.trim(),
    description: item.description.trim(),
    defaultEnabled: item.enabled,
  };

  switch (kind) {
    case "db": {
      const type = settings.type ?? "duckdb";
      const config: Record<string, unknown> = {
        type,
        mode: settings.mode ?? "readonly",
      };
      if (settings.filePath?.trim()) {
        config.filePath = settings.filePath.trim();
      }
      if (settings.connectionString?.trim()) {
        config.connectionString = settings.connectionString.trim();
      }
      for (const key of [
        "host",
        "port",
        "database",
        "schema",
        "username",
        "projectId",
        "dataset",
        "account",
        "warehouse",
      ] as const) {
        if (settings[key]?.trim()) config[key] = settings[key].trim();
      }
      const maxRows = parseNumber(settings.maxRows);
      const secure = parseBoolean(settings.secure);
      if (secure !== undefined) {
        config.secure = secure;
      }
      const timeoutMs = parseNumber(settings.timeoutMs);
      const denyWrite = parseBoolean(settings.denyWrite);
      if (maxRows !== undefined || timeoutMs !== undefined || denyWrite !== undefined) {
        config.queryPolicy = {
          ...(maxRows !== undefined ? { maxRows } : {}),
          ...(timeoutMs !== undefined ? { timeoutMs } : {}),
          ...(denyWrite !== undefined ? { denyWrite } : {}),
        };
      }
      const tableAllowlist = splitCsv(settings.tableAllowlist);
      const refreshIntervalSec = parseNumber(settings.refreshIntervalSec);
      if (tableAllowlist || refreshIntervalSec !== undefined) {
        config.introspection = {
          ...(tableAllowlist ? { tableAllowlist } : {}),
          ...(refreshIntervalSec !== undefined ? { refreshIntervalSec } : {}),
        };
      }
      const maskFields = splitCsv(settings.maskFields);
      if (maskFields) {
        config.maskFields = maskFields;
      }
      const allowSample = parseBoolean(settings.allowSample);
      const maxSampleRows = parseNumber(settings.maxSampleRows);
      if (allowSample !== undefined || maxSampleRows !== undefined) {
        config.samplePolicy = {
          ...(allowSample !== undefined ? { allowSample } : {}),
          ...(maxSampleRows !== undefined ? { maxSampleRows } : {}),
        };
      }
      const credentials = buildCredentials(settings, [
        { settingKey: "password", credentialKey: "password" },
        { settingKey: "credentialsJson", credentialKey: "credentialsJson" },
      ]);
      return {
        ...base,
        type,
        config,
        ...(credentials ? { credentials } : {}),
      };
    }
    case "kb": {
      const credentials = buildCredentials(settings, [
        { settingKey: "embeddingApiKey", credentialKey: "apiKey" },
      ]);
      return {
        ...base,
        id: settings.indexName?.trim() || item.id,
        retrievalTopK: parseNumber(settings.retrievalTopK) ?? 5,
        scoreThreshold: parseNumber(settings.scoreThreshold) ?? 0.2,
        ...(settings.embeddingProvider?.trim()
          ? { embeddingProvider: settings.embeddingProvider.trim() }
          : {}),
        ...(settings.embeddingModel?.trim()
          ? { embeddingModel: settings.embeddingModel.trim() }
          : {}),
        ...(settings.embeddingBaseUrl?.trim()
          ? { embeddingBaseUrl: settings.embeddingBaseUrl.trim() }
          : {}),
        ...(settings.vectorStore?.trim() ? { vectorStore: settings.vectorStore.trim() } : {}),
        ...(parseBoolean(settings.rerankEnabled) !== undefined
          ? { rerankEnabled: parseBoolean(settings.rerankEnabled) }
          : {}),
        ...(settings.rerankModel?.trim() ? { rerankModel: settings.rerankModel.trim() } : {}),
        ...(parseBoolean(settings.citationRequired) !== undefined
          ? { citationRequired: parseBoolean(settings.citationRequired) }
          : {}),
        ...(parseNumber(settings.chunkSize) !== undefined ? { chunkSize: parseNumber(settings.chunkSize) } : {}),
        ...(parseNumber(settings.chunkOverlap) !== undefined ? { chunkOverlap: parseNumber(settings.chunkOverlap) } : {}),
        ...(settings.scope?.trim() ? { scope: settings.scope.trim() } : {}),
        ...(parseBoolean(settings.graphRagEnabled) !== undefined
          ? { graphRagEnabled: parseBoolean(settings.graphRagEnabled) }
          : {}),
        ...(credentials ? { credentials } : {}),
      };
    }
    case "mcp": {
      const credentials = buildCredentials(settings, [
        { settingKey: "apiKey", credentialKey: "token" },
      ]);
      const body: Record<string, unknown> = {
        ...base,
        transport: settings.transport ?? "streamable-http",
        serverUrl: settings.serverUrl?.trim() ?? "",
        ...(settings.apiUrl?.trim() ? { apiUrl: settings.apiUrl.trim() } : {}),
        ...(settings.authType?.trim() ? { authType: settings.authType.trim() } : {}),
        ...(splitCsv(settings.toolAllowlist) ? { toolAllowlist: splitCsv(settings.toolAllowlist) } : {}),
        ...(parseNumber(settings.timeoutMs) !== undefined ? { timeoutMs: parseNumber(settings.timeoutMs) } : {}),
        ...(credentials ? { credentials } : {}),
      };
      appendMcpStdioFields(body, settings);
      return body;
    }
    case "llm": {
      const credentials = buildCredentials(settings, [
        { settingKey: "apiKey", credentialKey: "apiKey" },
      ]);
      return {
        ...base,
        provider: normalizeLlmProvider(settings.provider),
        modelName: settings.modelName?.trim() ?? "",
        baseUrl: settings.baseUrl?.trim() ?? "",
        ...(parseNumber(settings.timeoutMs) !== undefined
          ? { timeoutMs: parseNumber(settings.timeoutMs) }
          : {}),
        ...(parseNumber(settings.temperature) !== undefined
          ? { temperature: parseNumber(settings.temperature) }
          : {}),
        ...(parseNumber(settings.topP) !== undefined
          ? { topP: parseNumber(settings.topP) }
          : {}),
        ...(parseNumber(settings.frequencyPenalty) !== undefined
          ? { frequencyPenalty: parseNumber(settings.frequencyPenalty) }
          : {}),
        ...(parseNumber(settings.presencePenalty) !== undefined
          ? { presencePenalty: parseNumber(settings.presencePenalty) }
          : {}),
        ...(parseNumber(settings.maxTokens) !== undefined
          ? { maxTokens: parseNumber(settings.maxTokens) }
          : {}),
        ...(parseNumber(settings.contextLength) !== undefined
          ? { contextLength: parseNumber(settings.contextLength) }
          : {}),
        ...(parseBoolean(settings.reasoningModel) !== undefined
          ? { reasoningModel: parseBoolean(settings.reasoningModel) }
          : {}),
        ...(settings.fallbackProfileId?.trim()
          ? { fallbackProfileId: settings.fallbackProfileId.trim() }
          : {}),
        ...(credentials ? { credentials } : {}),
      };
    }
    case "skill":
      return {
        ...base,
        ...(splitCsv(settings.defaultDbIds) ? { defaultDbIds: splitCsv(settings.defaultDbIds) } : {}),
        ...(splitCsv(settings.defaultKbIds) ? { defaultKbIds: splitCsv(settings.defaultKbIds) } : {}),
        ...(splitCsv(settings.defaultMcpIds) ? { defaultMcpIds: splitCsv(settings.defaultMcpIds) } : {}),
        ...(settings.modelProfileId?.trim() ? { modelProfileId: settings.modelProfileId.trim() } : {}),
      };
    default:
      return base;
  }
}

export function itemToPatchBody(
  kind: WorkspaceConfigKind,
  item: WorkspaceConfigItem,
): Record<string, unknown> {
  const settings = item.settings ?? {};
  const body: Record<string, unknown> = {
    name: item.name.trim(),
    description: item.description.trim(),
    defaultEnabled: item.enabled,
    ...(item.revision !== undefined ? { revision: item.revision } : {}),
  };

  switch (kind) {
    case "db": {
      const config: Record<string, unknown> = {};
      if (settings.filePath?.trim()) config.filePath = settings.filePath.trim();
      if (settings.connectionString?.trim()) {
        config.connectionString = settings.connectionString.trim();
      }
      for (const key of [
        "host",
        "port",
        "database",
        "schema",
        "username",
        "projectId",
        "dataset",
        "account",
        "warehouse",
      ] as const) {
        if (settings[key]?.trim()) config[key] = settings[key].trim();
      }
      const maxRows = parseNumber(settings.maxRows);
      const secure = parseBoolean(settings.secure);
      if (secure !== undefined) {
        config.secure = secure;
      }
      const timeoutMs = parseNumber(settings.timeoutMs);
      const denyWrite = parseBoolean(settings.denyWrite);
      if (maxRows !== undefined || timeoutMs !== undefined || denyWrite !== undefined) {
        body.queryPolicy = {
          ...(maxRows !== undefined ? { maxRows } : {}),
          ...(timeoutMs !== undefined ? { timeoutMs } : {}),
          ...(denyWrite !== undefined ? { denyWrite } : {}),
        };
      }
      const tableAllowlist = splitCsv(settings.tableAllowlist);
      const refreshIntervalSec = parseNumber(settings.refreshIntervalSec);
      if (tableAllowlist || refreshIntervalSec !== undefined) {
        body.introspection = {
          ...(tableAllowlist ? { tableAllowlist } : {}),
          ...(refreshIntervalSec !== undefined ? { refreshIntervalSec } : {}),
        };
      }
      const maskFields = splitCsv(settings.maskFields);
      if (maskFields) {
        body.maskFields = maskFields;
      }
      const allowSample = parseBoolean(settings.allowSample);
      const maxSampleRows = parseNumber(settings.maxSampleRows);
      if (allowSample !== undefined || maxSampleRows !== undefined) {
        body.samplePolicy = {
          ...(allowSample !== undefined ? { allowSample } : {}),
          ...(maxSampleRows !== undefined ? { maxSampleRows } : {}),
        };
      }
      if (Object.keys(config).length > 0) body.config = config;
      const password = settings.password?.trim();
      const credentialsJson = settings.credentialsJson?.trim();
      if (password || credentialsJson) {
        body.credentials = {
          ...(password ? { password } : {}),
          ...(credentialsJson ? { credentialsJson } : {}),
        };
      }
      break;
    }
    case "kb":
      if (settings.retrievalTopK?.trim()) {
        body.retrievalTopK = parseNumber(settings.retrievalTopK);
      }
      if (settings.scoreThreshold?.trim()) {
        body.scoreThreshold = parseNumber(settings.scoreThreshold);
      }
      if (settings.embeddingProvider?.trim()) {
        body.embeddingProvider = settings.embeddingProvider.trim();
      }
      if (settings.embeddingModel?.trim()) {
        body.embeddingModel = settings.embeddingModel.trim();
      }
      if (settings.embeddingBaseUrl?.trim()) {
        body.embeddingBaseUrl = settings.embeddingBaseUrl.trim();
      }
      if (settings.vectorStore?.trim()) {
        body.vectorStore = settings.vectorStore.trim();
      }
      if (parseBoolean(settings.rerankEnabled) !== undefined) {
        body.rerankEnabled = parseBoolean(settings.rerankEnabled);
      }
      if (settings.rerankModel?.trim()) {
        body.rerankModel = settings.rerankModel.trim();
      }
      if (parseBoolean(settings.citationRequired) !== undefined) {
        body.citationRequired = parseBoolean(settings.citationRequired);
      }
      if (parseNumber(settings.chunkSize) !== undefined) {
        body.chunkSize = parseNumber(settings.chunkSize);
      }
      if (parseNumber(settings.chunkOverlap) !== undefined) {
        body.chunkOverlap = parseNumber(settings.chunkOverlap);
      }
      if (settings.scope?.trim()) {
        body.scope = settings.scope.trim();
      }
      if (parseBoolean(settings.graphRagEnabled) !== undefined) {
        body.graphRagEnabled = parseBoolean(settings.graphRagEnabled);
      }
      if (settings.embeddingApiKey?.trim()) {
        body.credentials = { apiKey: settings.embeddingApiKey.trim() };
      }
      break;
    case "mcp":
      if (settings.transport?.trim()) body.transport = settings.transport.trim();
      if (settings.serverUrl?.trim()) body.serverUrl = settings.serverUrl.trim();
      if (settings.apiUrl?.trim()) body.apiUrl = settings.apiUrl.trim();
      if (settings.authType?.trim()) body.authType = settings.authType.trim();
      if (splitCsv(settings.toolAllowlist)) {
        body.toolAllowlist = splitCsv(settings.toolAllowlist);
      }
      if (parseNumber(settings.timeoutMs) !== undefined) {
        body.timeoutMs = parseNumber(settings.timeoutMs);
      }
      if (settings.apiKey?.trim()) body.credentials = { token: settings.apiKey.trim() };
      appendMcpStdioFields(body, settings);
      break;
    case "llm":
      body.provider = normalizeLlmProvider(settings.provider);
      if (settings.modelName?.trim()) body.modelName = settings.modelName.trim();
      if (settings.baseUrl?.trim()) body.baseUrl = settings.baseUrl.trim();
      if (settings.fallbackProfileId?.trim()) {
        body.fallbackProfileId = settings.fallbackProfileId.trim();
      }
      if (parseNumber(settings.timeoutMs) !== undefined) {
        body.timeoutMs = parseNumber(settings.timeoutMs);
      }
      if (parseNumber(settings.temperature) !== undefined) {
        body.temperature = parseNumber(settings.temperature);
      }
      if (parseNumber(settings.topP) !== undefined) {
        body.topP = parseNumber(settings.topP);
      }
      if (parseNumber(settings.frequencyPenalty) !== undefined) {
        body.frequencyPenalty = parseNumber(settings.frequencyPenalty);
      }
      if (parseNumber(settings.presencePenalty) !== undefined) {
        body.presencePenalty = parseNumber(settings.presencePenalty);
      }
      if (parseNumber(settings.maxTokens) !== undefined) {
        body.maxTokens = parseNumber(settings.maxTokens);
      }
      if (parseNumber(settings.contextLength) !== undefined) {
        body.contextLength = parseNumber(settings.contextLength);
      }
      if (parseBoolean(settings.reasoningModel) !== undefined) {
        body.reasoningModel = parseBoolean(settings.reasoningModel);
      }
      if (settings.apiKey?.trim()) body.credentials = { apiKey: settings.apiKey.trim() };
      break;
    case "skill":
      if (splitCsv(settings.defaultDbIds)) {
        body.defaultDbIds = splitCsv(settings.defaultDbIds);
      }
      if (splitCsv(settings.defaultKbIds)) {
        body.defaultKbIds = splitCsv(settings.defaultKbIds);
      }
      if (splitCsv(settings.defaultMcpIds)) {
        body.defaultMcpIds = splitCsv(settings.defaultMcpIds);
      }
      if (settings.modelProfileId?.trim()) {
        body.modelProfileId = settings.modelProfileId.trim();
      }
      break;
  }

  return body;
}

export function mergeItemFromDto(
  kind: WorkspaceConfigKind,
  current: WorkspaceConfigItem,
  dto: DatasourceDto | KnowledgeBaseDto | McpServerDto | ModelProfileDto | SkillDto,
): WorkspaceConfigItem {
  const mapped =
    kind === "db"
      ? datasourceDtoToItem(dto as DatasourceDto)
      : kind === "kb"
        ? knowledgeBaseDtoToItem(dto as KnowledgeBaseDto)
        : kind === "mcp"
          ? mcpServerDtoToItem(dto as McpServerDto)
          : kind === "llm"
            ? modelProfileDtoToItem(dto as ModelProfileDto)
            : skillDtoToItem(dto as SkillDto);

  return {
    ...mapped,
    settings: {
      ...mapped.settings,
      ...(current.settings?.password?.trim() ? { password: current.settings.password } : {}),
      ...(current.settings?.apiKey?.trim() ? { apiKey: current.settings.apiKey } : {}),
      ...(current.settings?.embeddingApiKey?.trim()
        ? { embeddingApiKey: current.settings.embeddingApiKey }
        : {}),
      ...(current.settings?.credentialsJson?.trim()
        ? { credentialsJson: current.settings.credentialsJson }
        : {}),
    },
  };
}
