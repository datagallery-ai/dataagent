import type {
  AdapterExecutionInput,
  AdapterPreviewInput,
  AdapterSqlInput,
  DataSourceAdapter,
  SchemaSummary,
  TableResult
} from "../types.js";
import type * as BigQueryModule from "@google-cloud/bigquery";
import type * as SnowflakeModule from "snowflake-sdk";

export class ClickHouseAdapter implements DataSourceAdapter {
  constructor(private readonly config: Record<string, unknown>) {}

  async inspectSchema(input: AdapterExecutionInput = {}): Promise<Omit<SchemaSummary, "datasource_id">> {
    throwIfAborted(input.signal);
    const database = stringConfig(this.config, "database");
    const rows = await this.query(`
      SELECT
        table AS table_name,
        name AS column_name,
        type AS data_type,
        if(startsWith(type, 'Nullable('), 'YES', 'NO') AS is_nullable
      FROM system.columns
      WHERE database = ${clickHouseLiteral(database)}
      ORDER BY table, position
    `, input.signal);
    return schemaRowsToSummary(rows, "table_name", "column_name", "data_type", "is_nullable");
  }

  async previewTable(input: AdapterPreviewInput): Promise<TableResult> {
    throwIfAborted(input.signal);
    const database = stringConfig(this.config, "database");
    return rowsToTableResult(await this.query(
      `SELECT * FROM ${quoteClickHouseIdentifier(database)}.${quoteClickHouseIdentifier(input.table)} LIMIT ${input.limit}`,
      input.signal
    ));
  }

  async runSqlReadonly(input: AdapterSqlInput): Promise<TableResult> {
    throwIfAborted(input.signal);
    return rowsToTableResult(await this.query(applyStandardLimit(input.sql, input.limit), input.signal));
  }

  private async query(sql: string, signal?: AbortSignal | undefined): Promise<Record<string, unknown>[]> {
    const requestSignal = combineAbortSignals(signal, AbortSignal.timeout(numberConfig(this.config, "timeoutMs", 30000)));
    const response = await fetch(this.endpointUrl(), {
      method: "POST",
      headers: this.headers(),
      body: `${withClickHouseJsonFormat(sql)}\n`,
      ...(requestSignal ? { signal: requestSignal } : {})
    });
    const body = await response.text();
    if (!response.ok) {
      throw new Error(`CLICKHOUSE_QUERY_FAILED:${response.status}:${body.slice(0, 500)}`);
    }
    const parsed: unknown = body ? JSON.parse(body) : {};
    if (!isRecord(parsed) || !Array.isArray(parsed.data)) {
      throw new Error("CLICKHOUSE_JSON_RESULT_INVALID");
    }
    return parsed.data.filter(isRecord);
  }

  private endpointUrl(): string {
    const configuredUrl = optionalStringConfig(this.config, "url");
    const url = configuredUrl
      ? new URL(configuredUrl)
      : new URL(
          `${booleanConfig(this.config, "secure", false) ? "https" : "http"}://`
          + `${stringConfig(this.config, "host")}:${numberConfig(this.config, "port", 8123)}`
        );
    url.searchParams.set("database", stringConfig(this.config, "database"));
    url.searchParams.set("default_format", "JSON");
    url.searchParams.set("readonly", "1");
    url.searchParams.set("max_execution_time", String(Math.ceil(numberConfig(this.config, "timeoutMs", 30000) / 1000)));
    return url.toString();
  }

  private headers(): Record<string, string> {
    const username = optionalStringConfig(this.config, "username") ?? "default";
    const password = optionalStringConfig(this.config, "password");
    return {
      "Content-Type": "text/plain; charset=utf-8",
      "X-ClickHouse-User": username,
      ...(password ? { "X-ClickHouse-Key": password } : {})
    };
  }
}

export class SnowflakeAdapter implements DataSourceAdapter {
  constructor(private readonly config: Record<string, unknown>) {}

  async inspectSchema(input: AdapterExecutionInput = {}): Promise<Omit<SchemaSummary, "datasource_id">> {
    throwIfAborted(input.signal);
    const schema = stringConfig(this.config, "schema", "PUBLIC");
    const rows = await this.query(`
      SELECT table_name, column_name, data_type, is_nullable
      FROM information_schema.columns
      WHERE table_schema = ?
      ORDER BY table_name, ordinal_position
    `, [schema.toUpperCase()], input.signal);
    return schemaRowsToSummary(rows, "TABLE_NAME", "COLUMN_NAME", "DATA_TYPE", "IS_NULLABLE");
  }

  async previewTable(input: AdapterPreviewInput): Promise<TableResult> {
    throwIfAborted(input.signal);
    const schema = stringConfig(this.config, "schema", "PUBLIC");
    return rowsToTableResult(await this.query(
      `SELECT * FROM ${quoteSnowflakeIdentifier(schema)}.${quoteSnowflakeIdentifier(input.table)} LIMIT ?`,
      [input.limit],
      input.signal
    ));
  }

  async runSqlReadonly(input: AdapterSqlInput): Promise<TableResult> {
    throwIfAborted(input.signal);
    return rowsToTableResult(await this.query(applyStandardLimit(input.sql, input.limit), [], input.signal));
  }

  private async query(
    sql: string,
    binds: SnowflakeModule.Binds = [],
    signal?: AbortSignal | undefined
  ): Promise<Record<string, unknown>[]> {
    const snowflake = await loadSnowflake();
    const connection = snowflake.createConnection({
      account: stringConfig(this.config, "account"),
      username: stringConfig(this.config, "username"),
      password: stringConfig(this.config, "password"),
      warehouse: stringConfig(this.config, "warehouse"),
      database: stringConfig(this.config, "database"),
      schema: stringConfig(this.config, "schema", "PUBLIC"),
      ...optionalConfigString(this.config, "role", "role"),
      timeout: numberConfig(this.config, "timeoutMs", 30000)
    });
    try {
      await snowflakeConnect(connection, signal);
      return await snowflakeExecute(connection, sql, binds, signal);
    } finally {
      await snowflakeDestroy(connection);
    }
  }
}

export class BigQueryAdapter implements DataSourceAdapter {
  constructor(private readonly config: Record<string, unknown>) {}

  async inspectSchema(input: AdapterExecutionInput = {}): Promise<Omit<SchemaSummary, "datasource_id">> {
    throwIfAborted(input.signal);
    const dataset = stringConfig(this.config, "dataset");
    const rows = await this.query(`
      SELECT table_name, column_name, data_type, is_nullable
      FROM ${quoteBigQueryIdentifier(this.projectId(), dataset, "INFORMATION_SCHEMA.COLUMNS")}
      ORDER BY table_name, ordinal_position
    `, input.signal);
    return schemaRowsToSummary(rows, "table_name", "column_name", "data_type", "is_nullable");
  }

  async previewTable(input: AdapterPreviewInput): Promise<TableResult> {
    throwIfAborted(input.signal);
    return rowsToTableResult(await this.query(
      `SELECT * FROM ${quoteBigQueryIdentifier(this.projectId(), stringConfig(this.config, "dataset"), input.table)}
       LIMIT ${input.limit}`,
      input.signal
    ));
  }

  async runSqlReadonly(input: AdapterSqlInput): Promise<TableResult> {
    throwIfAborted(input.signal);
    return rowsToTableResult(await this.query(applyStandardLimit(input.sql, input.limit), input.signal));
  }

  private projectId(): string {
    return stringConfig(this.config, "projectId");
  }

  private async query(sql: string, signal?: AbortSignal | undefined): Promise<Record<string, unknown>[]> {
    throwIfAborted(signal);
    const bigquery = await loadBigQuery();
    const credentialsJson = optionalStringConfig(this.config, "credentialsJson");
    const keyFilename = optionalStringConfig(this.config, "keyFilename");
    const client = new bigquery.BigQuery({
      projectId: this.projectId(),
      ...(credentialsJson ? { credentials: JSON.parse(credentialsJson) as Record<string, unknown> } : {}),
      ...(keyFilename ? { keyFilename } : {})
    });
    const location = optionalStringConfig(this.config, "location");
    const queryOptions: BigQueryModule.Query = {
      query: sql,
      useLegacySql: false,
      ...(location ? { location } : {})
    };
    const [rows] = await client.query(queryOptions) as unknown as [unknown[]];
    throwIfAborted(signal);
    return rows.filter(isRecord);
  }
}

const loadSnowflake = async (): Promise<typeof SnowflakeModule> => {
  const loaded = await import("snowflake-sdk") as unknown as { default?: typeof SnowflakeModule } & typeof SnowflakeModule;
  return loaded.default ?? loaded;
};

const loadBigQuery = async (): Promise<typeof BigQueryModule> => await import("@google-cloud/bigquery");

const snowflakeConnect = async (
  connection: SnowflakeModule.Connection,
  signal?: AbortSignal | undefined
): Promise<void> =>
  await new Promise((resolve, reject) => {
    const abort = (): void => {
      connection.destroy(() => undefined);
      reject(signal?.reason instanceof Error ? signal.reason : new Error("RUN_CANCELLED"));
    };
    signal?.addEventListener("abort", abort, { once: true });
    connection.connect((error) => {
      signal?.removeEventListener("abort", abort);
      error ? reject(error) : resolve();
    });
  });

const snowflakeExecute = async (
  connection: SnowflakeModule.Connection,
  sql: string,
  binds: SnowflakeModule.Binds,
  signal?: AbortSignal | undefined
): Promise<Record<string, unknown>[]> =>
  await new Promise((resolve, reject) => {
    const abort = (): void => {
      connection.destroy(() => undefined);
      reject(signal?.reason instanceof Error ? signal.reason : new Error("RUN_CANCELLED"));
    };
    signal?.addEventListener("abort", abort, { once: true });
    connection.execute({
      sqlText: sql,
      binds,
      complete: (error, _statement, rows) => {
        signal?.removeEventListener("abort", abort);
        if (error) {
          reject(error);
        } else {
          resolve((rows ?? []).filter(isRecord));
        }
      }
    });
  });

const snowflakeDestroy = async (connection: SnowflakeModule.Connection): Promise<void> =>
  await new Promise((resolve) => {
    connection.destroy(() => resolve());
  });

const applyStandardLimit = (sql: string, limit: number): string => {
  if (/\bLIMIT\s+\d+\b/iu.test(sql)) {
    return sql;
  }

  return `SELECT * FROM (${sql}) AS readonly_query LIMIT ${limit}`;
};

const rowsToTableResult = (rows: unknown[]): TableResult => {
  const objectRows = rows.filter(isRecord);
  const columns = Array.from(new Set(objectRows.flatMap((row) => Object.keys(row))));

  return {
    columns,
    rows: objectRows.map((row) => columns.map((column) => row[column] ?? null)),
    row_count: objectRows.length
  };
};

const schemaRowsToSummary = (
  rows: Record<string, unknown>[],
  tableKey: string,
  columnKey: string,
  typeKey: string,
  nullableKey: string
): Omit<SchemaSummary, "datasource_id"> => {
  const tables = new Map<string, SchemaSummary["tables"][number]>();
  rows.forEach((row) => {
    const tableName = requiredRecordStringLoose(row, tableKey);
    const table = tables.get(tableName) ?? { name: tableName, columns: [] };
    table.columns.push({
      name: requiredRecordStringLoose(row, columnKey),
      type: requiredRecordStringLoose(row, typeKey),
      nullable: requiredRecordStringLoose(row, nullableKey).toUpperCase() === "YES"
    });
    tables.set(tableName, table);
  });
  return { tables: [...tables.values()] };
};

const stringConfig = (config: Record<string, unknown>, key: string, defaultValue?: string): string => {
  const value = config[key];

  if (typeof value === "string" && value.length > 0) {
    return value;
  }

  if (defaultValue !== undefined) {
    return defaultValue;
  }

  throw new Error(`Missing config value: ${key}`);
};

const optionalStringConfig = (config: Record<string, unknown>, key: string): string | undefined => {
  const value = config[key];
  return typeof value === "string" && value.length > 0 ? value : undefined;
};

const optionalConfigString = (config: Record<string, unknown>, key: string, targetKey = key): Record<string, string> => {
  const value = optionalStringConfig(config, key);
  return value ? { [targetKey]: value } : {};
};

const numberConfig = (config: Record<string, unknown>, key: string, defaultValue: number): number => {
  const value = config[key];
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  if (typeof value === "string" && value.trim()) {
    return Number(value);
  }
  return defaultValue;
};

const booleanConfig = (config: Record<string, unknown>, key: string, defaultValue: boolean): boolean => {
  const value = config[key];
  return typeof value === "boolean" ? value : defaultValue;
};

const quoteClickHouseIdentifier = (identifier: string): string => `\`${identifier.replaceAll("`", "``")}\``;

const quoteSnowflakeIdentifier = (identifier: string): string => `"${identifier.replaceAll('"', '""')}"`;

const quoteBigQueryIdentifier = (...parts: string[]): string => `\`${parts.map((part) => part.replaceAll("`", "")).join(".")}\``;

const clickHouseLiteral = (value: string): string => `'${value.replaceAll("\\", "\\\\").replaceAll("'", "\\'")}'`;

const withClickHouseJsonFormat = (sql: string): string =>
  /\bFORMAT\s+\w+\s*$/iu.test(sql)
    ? sql.replace(/\bFORMAT\s+\w+\s*$/iu, "FORMAT JSON")
    : `${sql} FORMAT JSON`;

const requiredRecordStringLoose = (row: unknown, key: string): string => {
  if (!isRecord(row)) {
    throw new Error(`Expected string column: ${key}`);
  }
  const value = row[key] ?? row[key.toUpperCase()] ?? row[key.toLowerCase()];
  if (typeof value !== "string") {
    throw new Error(`Expected string column: ${key}`);
  }
  return value;
};

const throwIfAborted = (signal?: AbortSignal | undefined): void => {
  if (signal?.aborted) {
    throw signal.reason instanceof Error ? signal.reason : new Error("RUN_CANCELLED");
  }
};

const combineAbortSignals = (
  first?: AbortSignal | undefined,
  second?: AbortSignal | undefined
): AbortSignal | undefined => {
  const signals = [first, second].filter((signal): signal is AbortSignal => Boolean(signal));
  if (signals.length === 0) {
    return undefined;
  }
  if (signals.length === 1) {
    return signals[0];
  }
  if (signals.some((signal) => signal.aborted)) {
    const controller = new AbortController();
    const aborted = signals.find((signal) => signal.aborted);
    controller.abort(aborted?.reason ?? new Error("RUN_CANCELLED"));
    return controller.signal;
  }
  const controller = new AbortController();
  const abort = (event: Event): void => {
    const signal = event.target instanceof AbortSignal ? event.target : undefined;
    controller.abort(signal?.reason ?? new Error("RUN_CANCELLED"));
  };
  signals.forEach((signal) => signal.addEventListener("abort", abort, { once: true }));
  return controller.signal;
};

const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null;
