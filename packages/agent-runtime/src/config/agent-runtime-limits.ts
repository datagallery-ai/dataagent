export type AgentRuntimeLimitDefinition = {
  defaultValue: number;
  min: number;
  max: number;
  env: string;
  description: string;
};

/**
 * Registry for every configurable numeric limit owned by agent-runtime.
 *
 * Defaults, bounds, environment names, and operational intent live together so callers never introduce
 * unexplained numeric thresholds in workflow code.
 */
export const AGENT_RUNTIME_LIMIT_DEFINITIONS = {
  agentMaxSteps: {
    defaultValue: 80,
    min: 10,
    max: 500,
    env: "DATAFOUNDRY_AGENT_MAX_STEPS",
    description: "Maximum model reasoning and tool-use steps available to one Agent run."
  },
  sqlMaxExecutionCount: {
    defaultValue: 60,
    min: 1,
    max: 500,
    env: "DATAFOUNDRY_SQL_MAX_EXECUTION_COUNT",
    description: "Maximum uncached read-only SQL executions allowed during one Agent run."
  },
  dataAnalysisMaxProtocolActions: {
    defaultValue: 500,
    min: 50,
    max: 5000,
    env: "DATAFOUNDRY_DATA_ANALYSIS_MAX_PROTOCOL_ACTIONS",
    description: "Maximum governed protocol actions retained for one data-analysis segment."
  },
  generalTaskMaxProtocolActions: {
    defaultValue: 100,
    min: 10,
    max: 1000,
    env: "DATAFOUNDRY_GENERAL_TASK_MAX_PROTOCOL_ACTIONS",
    description: "Maximum governed protocol actions retained for one general-task segment."
  },
  protocolDefaultMaxActions: {
    defaultValue: 100,
    min: 10,
    max: 5000,
    env: "DATAFOUNDRY_PROTOCOL_DEFAULT_MAX_ACTIONS",
    description: "Fallback action budget used when a ProtocolRuntime caller provides no explicit budget."
  },
  protocolMaxCompletionRejections: {
    defaultValue: 3,
    min: 1,
    max: 100,
    env: "DATAFOUNDRY_PROTOCOL_MAX_COMPLETION_REJECTIONS",
    description: "Maximum rejected completion proposals before a run is finalized as partial."
  },
  protocolMaxCommitRetries: {
    defaultValue: 8,
    min: 0,
    max: 100,
    env: "DATAFOUNDRY_PROTOCOL_MAX_COMMIT_RETRIES",
    description: "Maximum optimistic protocol state commit retries after concurrent revision changes."
  },
  protocolAutomaticActionMaxDepth: {
    defaultValue: 10,
    min: 1,
    max: 100,
    env: "DATAFOUNDRY_PROTOCOL_AUTOMATIC_ACTION_MAX_DEPTH",
    description: "Maximum nesting depth for preparatory and automatic protocol actions."
  },
  schemaMaxTables: {
    defaultValue: 20,
    min: 1,
    max: 500,
    env: "DATAFOUNDRY_SCHEMA_MAX_TABLES",
    description: "Maximum inspected tables materialized into model-visible schema context."
  },
  schemaMaxColumnsPerTable: {
    defaultValue: 50,
    min: 1,
    max: 1000,
    env: "DATAFOUNDRY_SCHEMA_MAX_COLUMNS_PER_TABLE",
    description: "Maximum columns per inspected table materialized into model-visible schema context."
  },
  sqlMaxModelRows: {
    defaultValue: 20,
    min: 1,
    max: 1000,
    env: "DATAFOUNDRY_SQL_MAX_MODEL_ROWS",
    description: "Maximum SQL result rows included in the compact observation shown to the model."
  },
  sqlMaxActivityRows: {
    defaultValue: 20,
    min: 1,
    max: 1000,
    env: "DATAFOUNDRY_SQL_MAX_ACTIVITY_ROWS",
    description: "Maximum SQL result rows included in activity and trace event previews."
  },
  sqlMaxCellChars: {
    defaultValue: 500,
    min: 32,
    max: 100000,
    env: "DATAFOUNDRY_SQL_MAX_CELL_CHARS",
    description: "Maximum characters retained from one SQL result cell in model-visible context."
  },
  sqlMaxSqlChars: {
    defaultValue: 4000,
    min: 256,
    max: 100000,
    env: "DATAFOUNDRY_SQL_MAX_SQL_CHARS",
    description: "Maximum SQL text characters retained in activity, trace, and context previews."
  },
  contextMaxTokens: {
    defaultValue: 32000,
    min: 1000,
    max: 1000000,
    env: "DATAFOUNDRY_CONTEXT_MAX_TOKENS",
    description: "Maximum estimated tokens admitted into one assembled Agent context package."
  },
  contextMaxChars: {
    defaultValue: 32000,
    min: 1000,
    max: 4000000,
    env: "DATAFOUNDRY_CONTEXT_MAX_CHARS",
    description: "Maximum source characters admitted before token-aware Agent context shaping."
  },
  toolErrorMaxMessageChars: {
    defaultValue: 500,
    min: 100,
    max: 10000,
    env: "DATAFOUNDRY_TOOL_ERROR_MAX_MESSAGE_CHARS",
    description: "Maximum sanitized tool-error message characters returned to the model."
  },
  knowledgeMaxTopK: {
    defaultValue: 20,
    min: 1,
    max: 100,
    env: "DATAFOUNDRY_KNOWLEDGE_MAX_TOP_K",
    description: "Maximum knowledge retrieval result count accepted from one Agent tool call."
  },
  requirementCommitMaxClaims: {
    defaultValue: 16,
    min: 1,
    max: 100,
    env: "DATAFOUNDRY_REQUIREMENT_COMMIT_MAX_CLAIMS",
    description: "Maximum requirement claims accepted in one analysis commit tool call."
  },
  requirementCommitMaxOutputFields: {
    defaultValue: 32,
    min: 1,
    max: 500,
    env: "DATAFOUNDRY_REQUIREMENT_COMMIT_MAX_OUTPUT_FIELDS",
    description: "Maximum named result fields attached to one committed analysis claim."
  },
  modelHelperMaxSteps: {
    defaultValue: 1,
    min: 1,
    max: 10,
    env: "DATAFOUNDRY_MODEL_HELPER_MAX_STEPS",
    description: "Maximum model steps used by deterministic classifier, extraction, and grounding helpers."
  },
  protocolClassifierMaxOutputTokens: {
    defaultValue: 512,
    min: 64,
    max: 4096,
    env: "DATAFOUNDRY_PROTOCOL_CLASSIFIER_MAX_OUTPUT_TOKENS",
    description: "Maximum output tokens for protocol classification."
  },
  requirementExtractorMaxOutputTokens: {
    defaultValue: 4096,
    min: 256,
    max: 32768,
    env: "DATAFOUNDRY_REQUIREMENT_EXTRACTOR_MAX_OUTPUT_TOKENS",
    description: "Maximum output tokens for logical analysis requirement extraction."
  },
  contractGrounderMaxOutputTokens: {
    defaultValue: 8192,
    min: 512,
    max: 65536,
    env: "DATAFOUNDRY_CONTRACT_GROUNDER_MAX_OUTPUT_TOKENS",
    description: "Maximum output tokens for schema-grounded analysis contract generation."
  },
  contractGrounderMaxAttempts: {
    defaultValue: 2,
    min: 1,
    max: 5,
    env: "DATAFOUNDRY_CONTRACT_GROUNDER_MAX_ATTEMPTS",
    description: "Maximum model attempts for producing one schema-valid grounded analysis contract."
  },
  helperContextMaxChars: {
    defaultValue: 2000,
    min: 200,
    max: 20000,
    env: "DATAFOUNDRY_HELPER_CONTEXT_MAX_CHARS",
    description: "Maximum characters of budgeted background context shared with helper models."
  },
  toolObservationMaxNames: {
    defaultValue: 5,
    min: 1,
    max: 100,
    env: "DATAFOUNDRY_TOOL_OBSERVATION_MAX_NAMES",
    description: "Maximum table or column names retained in a compact tool observation summary."
  },
  toolObservationMaxNameChars: {
    defaultValue: 120,
    min: 16,
    max: 2000,
    env: "DATAFOUNDRY_TOOL_OBSERVATION_MAX_NAME_CHARS",
    description: "Maximum characters retained for one name in a compact tool observation summary."
  },
  toolObservationMaxChars: {
    defaultValue: 12000,
    min: 1000,
    max: 100000,
    env: "DATAFOUNDRY_TOOL_OBSERVATION_MAX_CHARS",
    description: "Maximum characters retained for a default compact tool observation."
  }
} as const satisfies Record<string, AgentRuntimeLimitDefinition>;

type ResolvedAgentRuntimeLimits = {
  [Key in keyof typeof AGENT_RUNTIME_LIMIT_DEFINITIONS]: number;
};

/** Resolve all Agent runtime limits once from documented defaults and optional environment overrides. */
export const AGENT_RUNTIME_LIMITS: Readonly<ResolvedAgentRuntimeLimits> = Object.freeze(
  Object.fromEntries(Object.entries(AGENT_RUNTIME_LIMIT_DEFINITIONS).map(([key, definition]) => [
    key,
    readBoundedInteger(process.env[definition.env], definition)
  ])) as ResolvedAgentRuntimeLimits
);

function readBoundedInteger(rawValue: string | undefined, definition: AgentRuntimeLimitDefinition): number {
  if (rawValue === undefined || rawValue.trim() === "") {
    return definition.defaultValue;
  }
  const parsed = Number.parseInt(rawValue, 10);
  return Number.isSafeInteger(parsed) && parsed >= definition.min && parsed <= definition.max
    ? parsed
    : definition.defaultValue;
}
