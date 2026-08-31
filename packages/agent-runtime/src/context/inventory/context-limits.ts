import { AGENT_RUNTIME_LIMITS } from "../../config/agent-runtime-limits.js";

// Backward-compatible named exports. Definitions and environment overrides live in the central registry.
export const SCHEMA_MAX_TABLES = AGENT_RUNTIME_LIMITS.schemaMaxTables;
export const SCHEMA_MAX_COLUMNS_PER_TABLE = AGENT_RUNTIME_LIMITS.schemaMaxColumnsPerTable;

export const SQL_MAX_MODEL_ROWS = AGENT_RUNTIME_LIMITS.sqlMaxModelRows;
export const SQL_MAX_ACTIVITY_ROWS = AGENT_RUNTIME_LIMITS.sqlMaxActivityRows;
export const SQL_MAX_CELL_CHARS = AGENT_RUNTIME_LIMITS.sqlMaxCellChars;
export const SQL_MAX_SQL_CHARS = AGENT_RUNTIME_LIMITS.sqlMaxSqlChars;

export const CONTEXT_MAX_TOKENS = AGENT_RUNTIME_LIMITS.contextMaxTokens;
export const CONTEXT_MAX_CHARS = AGENT_RUNTIME_LIMITS.contextMaxChars;
