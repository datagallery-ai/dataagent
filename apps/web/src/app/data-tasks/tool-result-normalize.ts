/** Normalize AG-UI / CopilotKit tool payloads for chat + console rendering. */

function parseJsonValue(value: string): unknown {
  try {
    return JSON.parse(value);
  } catch {
    return undefined;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function isSqlExecutionShape(value: unknown): value is {
  columns: unknown[];
  rows: unknown[];
  [key: string]: unknown;
} {
  if (!isRecord(value)) return false;
  return Array.isArray(value.columns) && Array.isArray(value.rows);
}

function unwrapObservationEnvelope(record: Record<string, unknown>): unknown {
  const observation = record.observation;
  if (typeof observation !== "string") return record;

  const trimmed = observation.trim();
  if (!trimmed) return record;

  if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
    const parsed = parseJsonValue(trimmed);
    return parsed ?? trimmed;
  }

  return trimmed;
}

function unwrapNestedSqlResult(record: Record<string, unknown>): unknown {
  if (!isSqlExecutionShape(record.result)) return record;
  return {
    ...(record.result as Record<string, unknown>),
    ...(typeof record.sql === "string" ? { sql: record.sql } : {}),
  };
}

/** Unwrap observation envelopes and nested SQL payloads before formatting. */
export function unwrapToolResultPayload(value: unknown): unknown {
  let current = value;

  if (typeof current === "string") {
    const trimmed = current.trim();
    if (!trimmed) return current;
    if (trimmed.startsWith("{") || trimmed.startsWith("[")) {
      const parsed = parseJsonValue(trimmed);
      current = parsed ?? current;
    } else {
      return current;
    }
  }

  if (!isRecord(current)) return value;

  if ("observation" in current) {
    current = unwrapObservationEnvelope(current);
    if (typeof current === "string") return current;
    if (!isRecord(current)) return current;
  }

  if ("result" in current) {
    return unwrapNestedSqlResult(current);
  }

  return current;
}

export function parseToolResultRecord(value: unknown): Record<string, unknown> | null {
  const normalized = unwrapToolResultPayload(value);
  if (isRecord(normalized)) return normalized;
  if (typeof normalized === "string") {
    const parsed = parseJsonValue(normalized.trim());
    return isRecord(parsed) ? parsed : null;
  }
  return null;
}

export function toolResultObservationText(value: unknown): string {
  const normalized = unwrapToolResultPayload(value);
  if (typeof normalized === "string") return normalized;
  if (isRecord(normalized) && typeof normalized.observation === "string") {
    return normalized.observation;
  }
  try {
    return JSON.stringify(normalized, null, 2);
  } catch {
    return String(normalized ?? "");
  }
}

export function parseSqlToolResult(value: unknown): {
  columns: string[];
  rows: unknown[];
  row_count?: number;
  audit_log_id?: string;
  elapsed_ms?: number;
  artifact_id?: string;
} | null {
  const record = parseToolResultRecord(value);
  if (!isSqlExecutionShape(record)) return null;
  return {
    columns: record.columns.filter((column): column is string => typeof column === "string"),
    rows: record.rows,
    ...(typeof record.row_count === "number" ? { row_count: record.row_count } : {}),
    ...(typeof record.audit_log_id === "string" ? { audit_log_id: record.audit_log_id } : {}),
    ...(typeof record.elapsed_ms === "number" ? { elapsed_ms: record.elapsed_ms } : {}),
    ...(typeof record.artifact_id === "string" ? { artifact_id: record.artifact_id } : {}),
  };
}

export function parseSchemaToolResult(value: unknown): {
  datasource_id?: string;
  tables?: Array<{ name: string; columns?: Array<{ name: string; type?: string; nullable?: boolean }> }>;
} | null {
  const record = parseToolResultRecord(value);
  if (!record || !Array.isArray(record.tables)) return null;
  const tables = record.tables
    .filter(isRecord)
    .map((table) => {
      if (typeof table.name !== "string" || !table.name.trim()) return null;
      const columns = Array.isArray(table.columns)
        ? table.columns
            .filter(isRecord)
            .map((column) => {
              if (typeof column.name !== "string" || !column.name.trim()) return null;
              return {
                name: column.name,
                ...(typeof column.type === "string" ? { type: column.type } : {}),
                ...(typeof column.nullable === "boolean" ? { nullable: column.nullable } : {}),
              };
            })
            .filter((column): column is { name: string; type?: string; nullable?: boolean } =>
              column !== null,
            )
        : undefined;
      return {
        name: table.name,
        ...(columns && columns.length > 0 ? { columns } : {}),
      };
    })
    .filter((table): table is { name: string; columns?: Array<{ name: string; type?: string; nullable?: boolean }> } =>
      table !== null,
    );
  if (tables.length === 0) return null;
  return {
    ...(typeof record.datasource_id === "string" ? { datasource_id: record.datasource_id } : {}),
    tables,
  };
}

/** Read SQL text from tool args payloads or nested AG-UI result wrappers. */
export function sqlFromToolPayload(
  args: Record<string, unknown> | null | undefined,
  result: unknown,
): string | undefined {
  const fromArgs = typeof args?.sql === "string" && args.sql.trim() ? args.sql : undefined;
  if (fromArgs) return fromArgs;
  const record = parseToolResultRecord(result);
  if (record && typeof record.sql === "string" && record.sql.trim()) {
    return record.sql;
  }
  return undefined;
}
