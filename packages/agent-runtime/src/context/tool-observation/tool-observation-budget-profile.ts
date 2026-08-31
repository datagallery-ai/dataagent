import { AGENT_RUNTIME_LIMITS } from "../../config/agent-runtime-limits.js";
import {
  SCHEMA_MAX_COLUMNS_PER_TABLE,
  SCHEMA_MAX_TABLES,
  SQL_MAX_ACTIVITY_ROWS,
  SQL_MAX_CELL_CHARS,
  SQL_MAX_MODEL_ROWS,
  SQL_MAX_SQL_CHARS
} from "../inventory/context-limits.js";

export const DEFAULT_TOOL_OBSERVATION_SOURCE_LIMIT_PROFILES: Record<string, Record<string, number>> = {
  inspect_schema: {
    maxColumnsPerTable: SCHEMA_MAX_COLUMNS_PER_TABLE,
    maxTables: SCHEMA_MAX_TABLES
  },
  "mcp__*": {
    maxChars: AGENT_RUNTIME_LIMITS.toolObservationMaxChars
  },
  run_sql_readonly: {
    maxActivityRows: SQL_MAX_ACTIVITY_ROWS,
    maxCellChars: SQL_MAX_CELL_CHARS,
    maxModelRows: SQL_MAX_MODEL_ROWS,
    maxSqlChars: SQL_MAX_SQL_CHARS
  }
};
