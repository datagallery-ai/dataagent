import type { SchemaSummary, SqlExecutionResult } from "@datafoundry/data-gateway";

import { AGENT_RUNTIME_LIMITS } from "../../config/agent-runtime-limits.js";
import type { ArtifactRef, AuditRef, ContextTruncation } from "../inventory/context-package.js";
import {
  CONTEXT_MAX_CHARS,
  SCHEMA_MAX_COLUMNS_PER_TABLE,
  SCHEMA_MAX_TABLES,
  SQL_MAX_ACTIVITY_ROWS,
  SQL_MAX_CELL_CHARS,
  SQL_MAX_MODEL_ROWS,
  SQL_MAX_SQL_CHARS
} from "../inventory/context-limits.js";
import { truncateString } from "../inventory/context-text.js";
import type { ToolObservationProjection } from "./tool-observation-projection-items.js";

export type ToolObservationProjectionPolicy = {
  schema: {
    max_chars: number;
    max_columns_per_table: number;
    max_tables: number;
  };
  sql: {
    max_activity_chars: number;
    max_activity_rows: number;
    max_cell_chars: number;
    max_model_rows: number;
    max_model_chars: number;
    max_sql_chars: number;
  };
};

export const DEFAULT_TOOL_OBSERVATION_PROJECTION_POLICY: ToolObservationProjectionPolicy = {
  schema: {
    max_chars: CONTEXT_MAX_CHARS,
    max_columns_per_table: SCHEMA_MAX_COLUMNS_PER_TABLE,
    max_tables: SCHEMA_MAX_TABLES
  },
  sql: {
    max_activity_chars: CONTEXT_MAX_CHARS,
    max_activity_rows: SQL_MAX_ACTIVITY_ROWS,
    max_cell_chars: SQL_MAX_CELL_CHARS,
    max_model_rows: SQL_MAX_MODEL_ROWS,
    max_model_chars: CONTEXT_MAX_CHARS,
    max_sql_chars: SQL_MAX_SQL_CHARS
  }
};

export const projectSchemaToolObservation = (
  schema: SchemaSummary,
  policy: ToolObservationProjectionPolicy = DEFAULT_TOOL_OBSERVATION_PROJECTION_POLICY
): ToolObservationProjection => {
  const limitedTables = schema.tables.slice(0, policy.schema.max_tables).map((table) => ({
    ...table,
    columns: table.columns.slice(0, policy.schema.max_columns_per_table)
  }));
  const tables = fitSchemaToBudget(schema.datasource_id, limitedTables, policy.schema.max_chars);
  const omittedTables = Math.max(schema.tables.length - tables.length, 0);
  const omittedTableNames = schema.tables
    .slice(tables.length)
    .map((t) => t.name);
  const omittedColumns = tables.reduce((total, table, index) => {
    const originalColumnCount = schema.tables[index]?.columns.length ?? table.columns.length;
    return total + Math.max(originalColumnCount - table.columns.length, 0);
  }, 0);

  const truncationReason = omittedTables > 0
    ? describeOmittedTables(omittedTables, omittedTableNames)
    : omittedColumns > 0
      ? `Omitted ${omittedColumns} column(s) across tables`
      : "";

  const truncation: ContextTruncation[] =
    omittedTables > 0 || omittedColumns > 0
      ? [{
          sourceId: "schema",
          truncated: true,
          reason: truncationReason,
          originalSize: schema.tables.length,
          returnedSize: tables.length
        }]
      : [];

  const modelVisible: SchemaSummary = {
    datasource_id: schema.datasource_id,
    tables,
    ...(truncation.length > 0 ? { context: { truncation: truncation[0] } } : {})
  };

  return {
    model: modelVisible,
    activity: modelVisible,
    artifactRefs: [],
    auditRefs: [],
    truncation
  };
};

export const projectSqlToolObservation = (
  result: SqlExecutionResult,
  sql: string,
  policy: ToolObservationProjectionPolicy = DEFAULT_TOOL_OBSERVATION_PROJECTION_POLICY
): ToolObservationProjection => {
  const modelRows = fitRowsToBudget(
    truncateRows(result.rows, policy.sql.max_model_rows, policy.sql.max_cell_chars),
    policy.sql.max_model_chars,
    (rows) => ({ columns: result.columns, rows, row_count: result.row_count })
  );
  const modelTruncation = createSqlTruncation(result, modelRows, "sql-model");
  const sqlPreview = truncateString(sql, policy.sql.max_sql_chars);

  const activityRows = fitRowsToBudget(
    truncateRows(result.rows, policy.sql.max_activity_rows, policy.sql.max_cell_chars),
    policy.sql.max_activity_chars,
    (rows) => ({ columns: result.columns, rows, row_count: result.row_count, sql: sqlPreview?.value })
  );
  const activityTruncation = createSqlTruncation(result, activityRows, "sql-activity");

  const activityContent: Record<string, unknown> = {
    columns: result.columns,
    rows: activityRows.rows,
    row_count: result.row_count,
    audit_log_id: result.audit_log_id
  };

  if (result.artifact_id) {
    activityContent.artifact_id = result.artifact_id;
  }

  if (sqlPreview) {
    activityContent.sql = sqlPreview.value;
  }

  if (activityTruncation.truncated || sqlPreview?.truncated) {
    activityContent.context = {
      truncation: activityTruncation,
      sql_truncated: Boolean(sqlPreview?.truncated)
    };
  }

  const artifactRefs: ArtifactRef[] = result.artifact_id
    ? [{ artifact_id: result.artifact_id, source: "sql" }]
    : [];

  const auditRefs: AuditRef[] = result.audit_log_id
    ? [{ audit_log_id: result.audit_log_id, source: "sql" }]
    : [];

  const truncation: ContextTruncation[] = [];
  if (modelTruncation.truncated) {
    truncation.push(modelTruncation);
  }
  if (activityTruncation.truncated && activityTruncation !== modelTruncation) {
    truncation.push(activityTruncation);
  }
  if (sqlPreview?.truncated) {
    truncation.push({
      sourceId: "sql-text",
      truncated: true,
      reason: `SQL text exceeded ${policy.sql.max_sql_chars} characters`,
      originalSize: sql.length,
      returnedSize: sqlPreview.value.length
    });
  }

  const modelVisible: Record<string, unknown> = {
    columns: result.columns,
    rows: modelRows.rows,
    row_count: result.row_count,
    audit_log_id: result.audit_log_id,
    elapsed_ms: result.elapsed_ms,
    ...(result.artifact_id ? { artifact_id: result.artifact_id } : {})
  };

  if (modelTruncation.truncated) {
    modelVisible.context = { truncation: modelTruncation };
  }

  return {
    model: modelVisible,
    activity: activityContent,
    artifactRefs,
    auditRefs,
    truncation
  };
};

const createSqlTruncation = (
  result: SqlExecutionResult,
  truncatedRows: TruncatedRows,
  sourceId: string
): ContextTruncation => {
  const originalRowCount = result.rows.length;
  const omittedRows = Math.max(originalRowCount - truncatedRows.rows.length, 0);
  const reasons = [
    ...(omittedRows > 0 ? [`Omitted ${omittedRows} row(s)`] : []),
    ...(sum(truncatedRows.truncatedCellCounts) > 0
      ? [`Truncated ${sum(truncatedRows.truncatedCellCounts)} cell(s)`]
      : [])
  ];

  return {
    sourceId,
    truncated: reasons.length > 0,
    reason: reasons.join("; "),
    originalSize: originalRowCount,
    returnedSize: truncatedRows.rows.length
  };
};

type TruncatedRows = {
  rows: unknown[][];
  truncatedCellCounts: number[];
};

const truncateRows = (rows: unknown[][], maxRows: number, maxCellChars: number): TruncatedRows => {
  const truncatedCellCounts: number[] = [];
  const boundedRows = rows.slice(0, maxRows).map((row) => {
    let truncatedCellCount = 0;
    const boundedRow = row.map((cell) => {
      const truncated = truncateCell(cell, maxCellChars);
      if (truncated.truncated) {
        truncatedCellCount += 1;
      }
      return truncated.value;
    });
    truncatedCellCounts.push(truncatedCellCount);
    return boundedRow;
  });
  return { rows: boundedRows, truncatedCellCounts };
};

const fitRowsToBudget = (
  truncatedRows: TruncatedRows,
  maxChars: number,
  buildContent: (rows: unknown[][]) => unknown
): TruncatedRows => {
  const rows = [...truncatedRows.rows];
  const truncatedCellCounts = [...truncatedRows.truncatedCellCounts];
  const reservedChars = 1024;

  while (rows.length > 0 && JSON.stringify(buildContent(rows)).length > Math.max(maxChars - reservedChars, 0)) {
    rows.pop();
    truncatedCellCounts.pop();
  }
  return { rows, truncatedCellCounts };
};

const fitSchemaToBudget = (
  datasourceId: string,
  inputTables: SchemaSummary["tables"],
  maxChars: number
): SchemaSummary["tables"] => {
  const tables = inputTables.map((table) => ({ ...table, columns: [...table.columns] }));
  const reservedChars = 1024;

  while (
    tables.length > 0 &&
    JSON.stringify({ datasource_id: datasourceId, tables }).length > Math.max(maxChars - reservedChars, 0)
  ) {
    const lastTable = tables.at(-1);
    if (lastTable && lastTable.columns.length > 0) {
      lastTable.columns.pop();
    } else {
      tables.pop();
    }
  }
  return tables;
};

const sum = (values: number[]): number => values.reduce((total, value) => total + value, 0);

const describeOmittedTables = (omittedTables: number, names: string[]): string => {
  const maxNames = AGENT_RUNTIME_LIMITS.toolObservationMaxNames;
  const maxNameChars = AGENT_RUNTIME_LIMITS.toolObservationMaxNameChars;
  const preview = names.slice(0, maxNames).map((name) =>
    name.length > maxNameChars ? `${name.slice(0, maxNameChars)}...` : name
  );
  const remaining = Math.max(names.length - preview.length, 0);
  return `Omitted ${omittedTables} table(s): ${preview.join(", ")}${remaining > 0 ? `, and ${remaining} more` : ""}`;
};

const truncateCell = (cell: unknown, maxCellChars: number): { truncated: boolean; value: unknown } => {
  if (typeof cell !== "string") {
    return { truncated: false, value: cell };
  }

  return truncateString(cell, maxCellChars) ?? { truncated: false, value: cell };
};
