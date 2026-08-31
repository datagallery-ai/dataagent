import type {
  AdapterExecutionInput,
  AdapterPreviewInput,
  AdapterSqlInput,
  DataSourceAdapter,
  SchemaSummary,
  TableResult
} from "../types.js";
import type * as HiveDriverModule from "hive-driver";

type HiveSessionLike = {
  close(): Promise<unknown>;
  executeStatement(statement: string, options?: Record<string, unknown>): Promise<HiveOperationLike>;
  getColumns(request: Record<string, string>): Promise<HiveOperationLike>;
};

type HiveOperationLike = {
  close(): Promise<unknown>;
  setMaxRows(maxRows: number): void;
};

type TrinoCompatiblePage = {
  columns?: Array<{ name: string; type?: string }>;
  data?: unknown[][];
  nextUri?: string;
};

type DatabricksStatementResponse = {
  manifest?: {
    schema?: {
      columns?: Array<{ name: string; type_name?: string; type_text?: string }>;
    };
  };
  result?: {
    data_array?: unknown[][];
    next_chunk_internal_link?: string;
  };
  statement_id?: string;
  status?: {
    error?: { message?: string };
    state?: string;
  };
};

export class TrinoAdapter implements DataSourceAdapter {
  constructor(private readonly config: Record<string, unknown>) {}

  async inspectSchema(input: AdapterExecutionInput = {}): Promise<Omit<SchemaSummary, "datasource_id">> {
    throwIfAborted(input.signal);
    const schema = stringConfig(this.config, "schema", "default");
    const rows = await this.query(`
      SELECT table_name, column_name, data_type, is_nullable
      FROM information_schema.columns
      WHERE table_schema = ${sqlLiteral(schema)}
      ORDER BY table_name, ordinal_position
    `, input.signal);
    return schemaRowsToSummary(rows, "table_name", "column_name", "data_type", "is_nullable");
  }

  async previewTable(input: AdapterPreviewInput): Promise<TableResult> {
    throwIfAborted(input.signal);
    const catalog = stringConfig(this.config, "catalog");
    const schema = stringConfig(this.config, "schema", "default");
    return rowsToTableResult(await this.query(
      `SELECT * FROM ${quoteTrinoIdentifier(catalog)}.${quoteTrinoIdentifier(schema)}.${quoteTrinoIdentifier(input.table)}
       LIMIT ${input.limit}`,
      input.signal
    ));
  }

  async runSqlReadonly(input: AdapterSqlInput): Promise<TableResult> {
    throwIfAborted(input.signal);
    return rowsToTableResult(await this.query(applyStandardLimit(input.sql, input.limit), input.signal));
  }

  private async query(sql: string, signal?: AbortSignal | undefined): Promise<Record<string, unknown>[]> {
    return await queryTrinoCompatible(this.config, sql, "trino", signal);
  }
}

export class PrestoAdapter implements DataSourceAdapter {
  constructor(private readonly config: Record<string, unknown>) {}

  async inspectSchema(input: AdapterExecutionInput = {}): Promise<Omit<SchemaSummary, "datasource_id">> {
    throwIfAborted(input.signal);
    const schema = stringConfig(this.config, "schema", "default");
    const rows = await this.query(`
      SELECT table_name, column_name, data_type, is_nullable
      FROM information_schema.columns
      WHERE table_schema = ${sqlLiteral(schema)}
      ORDER BY table_name, ordinal_position
    `, input.signal);
    return schemaRowsToSummary(rows, "table_name", "column_name", "data_type", "is_nullable");
  }

  async previewTable(input: AdapterPreviewInput): Promise<TableResult> {
    throwIfAborted(input.signal);
    const catalog = stringConfig(this.config, "catalog");
    const schema = stringConfig(this.config, "schema", "default");
    return rowsToTableResult(await this.query(
      `SELECT * FROM ${quoteTrinoIdentifier(catalog)}.${quoteTrinoIdentifier(schema)}.${quoteTrinoIdentifier(input.table)}
       LIMIT ${input.limit}`,
      input.signal
    ));
  }

  async runSqlReadonly(input: AdapterSqlInput): Promise<TableResult> {
    throwIfAborted(input.signal);
    return rowsToTableResult(await this.query(applyStandardLimit(input.sql, input.limit), input.signal));
  }

  private async query(sql: string, signal?: AbortSignal | undefined): Promise<Record<string, unknown>[]> {
    return await queryTrinoCompatible(this.config, sql, "presto", signal);
  }
}

export class SparkSqlAdapter implements DataSourceAdapter {
  constructor(private readonly config: Record<string, unknown>) {}

  async inspectSchema(input: AdapterExecutionInput = {}): Promise<Omit<SchemaSummary, "datasource_id">> {
    throwIfAborted(input.signal);
    const catalog = optionalStringConfig(this.config, "catalog");
    const schema = stringConfig(this.config, "schema", "default");
    return await this.withSession(async (hive, session) => {
      const operation = await session.getColumns({
        ...(catalog ? { catalogName: catalog } : {}),
        schemaName: schema,
        tableName: "%",
        columnName: "%"
      });
      const rows = await sparkOperationRows(hive, operation, input.signal);
      return schemaRowsToSummary(rows, "TABLE_NAME", "COLUMN_NAME", "TYPE_NAME", "IS_NULLABLE");
    }, input.signal);
  }

  async previewTable(input: AdapterPreviewInput): Promise<TableResult> {
    throwIfAborted(input.signal);
    const catalog = optionalStringConfig(this.config, "catalog");
    const schema = stringConfig(this.config, "schema", "default");
    const table = [catalog, schema, input.table].filter((part): part is string => Boolean(part))
      .map(quoteTrinoIdentifier).join(".");
    return rowsToTableResult(await this.query(`SELECT * FROM ${table} LIMIT ${input.limit}`, input.signal));
  }

  async runSqlReadonly(input: AdapterSqlInput): Promise<TableResult> {
    throwIfAborted(input.signal);
    return rowsToTableResult(await this.query(applyStandardLimit(input.sql, input.limit), input.signal));
  }

  private async query(sql: string, signal?: AbortSignal | undefined): Promise<Record<string, unknown>[]> {
    return await this.withSession(async (hive, session) => {
      const operation = await session.executeStatement(sql, {
        runAsync: true,
        confOverlay: new Map([["spark.sql.thriftServer.interruptOnCancel", "true"]])
      });
      return await sparkOperationRows(hive, operation, signal);
    }, signal);
  }

  private async withSession<T>(
    callback: (hive: typeof HiveDriverModule, session: HiveSessionLike) => Promise<T>,
    signal?: AbortSignal | undefined
  ): Promise<T> {
    const hive = await loadHiveDriver();
    const client = new hive.HiveClient(hive.thrift.TCLIService, hive.thrift.TCLIService_types);
    const connection = stringConfig(this.config, "transport", "tcp") === "http"
      ? new hive.connections.HttpConnection()
      : new hive.connections.TcpConnection();
    const auth = sparkAuthProvider(hive, this.config);
    const abort = (): void => {
      client.close();
    };
    let session: HiveSessionLike | undefined;
    try {
      signal?.addEventListener("abort", abort, { once: true });
      await client.connect(sparkConnectionOptions(this.config) as never, connection, auth as never);
      throwIfAborted(signal);
      session = await client.openSession({
        client_protocol: hive.thrift.TCLIService_types.TProtocolVersion.HIVE_CLI_SERVICE_PROTOCOL_V10,
        ...optionalConfigString(this.config, "username")
      });
      return await callback(hive, session);
    } finally {
      signal?.removeEventListener("abort", abort);
      await session?.close().catch(() => undefined);
      client.close();
    }
  }
}

export class DatabricksSqlAdapter implements DataSourceAdapter {
  constructor(private readonly config: Record<string, unknown>) {}

  async inspectSchema(input: AdapterExecutionInput = {}): Promise<Omit<SchemaSummary, "datasource_id">> {
    throwIfAborted(input.signal);
    const schema = stringConfig(this.config, "schema", "default");
    const rows = await this.query(`
      SELECT table_name, column_name, data_type, is_nullable
      FROM information_schema.columns
      WHERE table_schema = ${sqlLiteral(schema)}
      ORDER BY table_name, ordinal_position
    `, input.signal);
    return schemaRowsToSummary(rows, "table_name", "column_name", "data_type", "is_nullable");
  }

  async previewTable(input: AdapterPreviewInput): Promise<TableResult> {
    throwIfAborted(input.signal);
    const catalog = optionalStringConfig(this.config, "catalog");
    const schema = stringConfig(this.config, "schema", "default");
    const table = [catalog, schema, input.table].filter((part): part is string => Boolean(part))
      .map(quoteTrinoIdentifier).join(".");
    return rowsToTableResult(await this.query(`SELECT * FROM ${table} LIMIT ${input.limit}`, input.signal));
  }

  async runSqlReadonly(input: AdapterSqlInput): Promise<TableResult> {
    throwIfAborted(input.signal);
    return rowsToTableResult(await this.query(applyStandardLimit(input.sql, input.limit), input.signal));
  }

  private async query(sql: string, signal?: AbortSignal | undefined): Promise<Record<string, unknown>[]> {
    return await queryDatabricksSql(this.config, sql, signal);
  }
}

const loadHiveDriver = async (): Promise<typeof HiveDriverModule> => {
  const loaded = await import("hive-driver") as unknown as { default?: typeof HiveDriverModule } & typeof HiveDriverModule;
  return loaded.default ?? loaded;
};

const queryTrinoCompatible = async (
  config: Record<string, unknown>,
  sql: string,
  protocol: "presto" | "trino",
  signal?: AbortSignal | undefined
): Promise<Record<string, unknown>[]> => {
  const headerPrefix = protocol === "trino" ? "X-Trino" : "X-Presto";
  const requestHeaders = trinoCompatibleHeaders(config, headerPrefix);
  const rows: Record<string, unknown>[] = [];
  let columns: string[] = [];
  let nextUri: string | undefined;
  let response = await fetch(trinoStatementUrl(config), {
    method: "POST",
    headers: requestHeaders,
    body: sql,
    ...(signal ? { signal } : {})
  });
  for (;;) {
    const page = await parseTrinoCompatibleResponse(response);
    if (Array.isArray(page.columns) && columns.length === 0) {
      columns = page.columns.map((column) => column.name);
    }
    if (Array.isArray(page.data)) {
      rows.push(...arrayRowsToRecords(columns, page.data));
    }
    nextUri = typeof page.nextUri === "string" ? page.nextUri : undefined;
    if (!nextUri) {
      return rows;
    }
    throwIfAborted(signal);
    response = await fetch(nextUri, { headers: requestHeaders, ...(signal ? { signal } : {}) });
  }
};

const trinoCompatibleHeaders = (config: Record<string, unknown>, prefix: "X-Presto" | "X-Trino"): HeadersInit => {
  const username = stringConfig(config, "username", "data-foundry");
  const password = optionalStringConfig(config, "password");
  return {
    "Accept": "application/json",
    "Content-Type": "text/plain; charset=utf-8",
    [`${prefix}-User`]: username,
    [`${prefix}-Source`]: stringConfig(config, "source", "open-data-foundry"),
    [`${prefix}-Catalog`]: stringConfig(config, "catalog"),
    [`${prefix}-Schema`]: stringConfig(config, "schema", "default"),
    ...(password ? { "Authorization": `Basic ${Buffer.from(`${username}:${password}`).toString("base64")}` } : {})
  };
};

const parseTrinoCompatibleResponse = async (response: Response): Promise<TrinoCompatiblePage> => {
  const body = await response.text();
  if (!response.ok) {
    throw new Error(`TRINO_COMPATIBLE_QUERY_FAILED:${response.status}:${body.slice(0, 500)}`);
  }
  const parsed: unknown = body ? JSON.parse(body) : {};
  if (!isRecord(parsed)) {
    throw new Error("TRINO_COMPATIBLE_JSON_RESULT_INVALID");
  }
  if (isRecord(parsed.error)) {
    const message = typeof parsed.error.message === "string" ? parsed.error.message : "unknown error";
    throw new Error(`TRINO_COMPATIBLE_QUERY_ERROR:${message}`);
  }
  return parsed as TrinoCompatiblePage;
};

const queryDatabricksSql = async (
  config: Record<string, unknown>,
  sql: string,
  signal?: AbortSignal | undefined
): Promise<Record<string, unknown>[]> => {
  const submitted = await databricksRequest(config, "/api/2.0/sql/statements", {
    statement: sql,
    warehouse_id: databricksWarehouseId(config),
    wait_timeout: "10s",
    disposition: "INLINE",
    format: "JSON_ARRAY",
    ...optionalConfigString(config, "catalog"),
    ...optionalConfigString(config, "schema")
  }, signal);
  const completed = await waitDatabricksStatement(config, submitted, signal);
  const columns = databricksColumns(completed);
  const rows = [...arrayRowsToRecords(columns, completed.result?.data_array ?? [])];
  let nextChunk = completed.result?.next_chunk_internal_link;
  while (nextChunk) {
    const chunk = await databricksRequest(config, nextChunk, undefined, signal);
    rows.push(...arrayRowsToRecords(columns, chunk.result?.data_array ?? []));
    nextChunk = chunk.result?.next_chunk_internal_link;
  }
  return rows;
};

const waitDatabricksStatement = async (
  config: Record<string, unknown>,
  response: DatabricksStatementResponse,
  signal?: AbortSignal | undefined
): Promise<DatabricksStatementResponse> => {
  let current = response;
  for (;;) {
    const state = current.status?.state;
    if (state === "SUCCEEDED") {
      return current;
    }
    if (state === "FAILED" || state === "CANCELED" || state === "CLOSED") {
      throw new Error(`DATABRICKS_SQL_FAILED:${current.status?.error?.message ?? state}`);
    }
    const statementId = current.statement_id;
    if (!statementId) {
      throw new Error("DATABRICKS_SQL_STATEMENT_ID_MISSING");
    }
    await delay(1000, signal);
    current = await databricksRequest(config, `/api/2.0/sql/statements/${statementId}`, undefined, signal);
  }
};

const databricksRequest = async (
  config: Record<string, unknown>,
  pathOrUrl: string,
  body?: Record<string, unknown>,
  signal?: AbortSignal | undefined
): Promise<DatabricksStatementResponse> => {
  const response = await fetch(databricksUrl(config, pathOrUrl), {
    method: body ? "POST" : "GET",
    headers: {
      "Accept": "application/json",
      "Authorization": `Bearer ${stringConfig(config, "token")}`,
      ...(body ? { "Content-Type": "application/json" } : {})
    },
    ...(body ? { body: JSON.stringify(body) } : {}),
    ...(signal ? { signal } : {})
  });
  const text = await response.text();
  if (!response.ok) {
    throw new Error(`DATABRICKS_SQL_REQUEST_FAILED:${response.status}:${text.slice(0, 500)}`);
  }
  const parsed: unknown = text ? JSON.parse(text) : {};
  if (!isRecord(parsed)) {
    throw new Error("DATABRICKS_SQL_JSON_RESULT_INVALID");
  }
  return parsed as DatabricksStatementResponse;
};

const databricksUrl = (config: Record<string, unknown>, pathOrUrl: string): string => {
  if (/^https?:\/\//iu.test(pathOrUrl)) {
    return pathOrUrl;
  }
  const host = stringConfig(config, "host").replace(/^https?:\/\//iu, "");
  return `https://${host}${pathOrUrl.startsWith("/") ? "" : "/"}${pathOrUrl}`;
};

const databricksWarehouseId = (config: Record<string, unknown>): string => {
  const configured = optionalStringConfig(config, "warehouseId");
  if (configured) {
    return configured;
  }
  const match = /\/warehouses\/([^/?#]+)/iu.exec(stringConfig(config, "path"));
  if (!match?.[1]) {
    throw new Error("DATABRICKS_WAREHOUSE_ID_REQUIRED");
  }
  return match[1];
};

const databricksColumns = (response: DatabricksStatementResponse): string[] =>
  (response.manifest?.schema?.columns ?? []).map((column) => column.name);

const delay = async (ms: number, signal?: AbortSignal | undefined): Promise<void> =>
  await new Promise((resolve, reject) => {
    const cleanup = (): void => {
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
    };
    const abort = (): void => {
      cleanup();
      reject(signal?.reason instanceof Error ? signal.reason : new Error("RUN_CANCELLED"));
    };
    const timer = setTimeout(() => {
      cleanup();
      resolve();
    }, ms);
    signal?.addEventListener("abort", abort, { once: true });
  });

const sparkOperationRows = async (
  hive: typeof HiveDriverModule,
  operation: HiveOperationLike,
  signal?: AbortSignal | undefined
): Promise<Record<string, unknown>[]> => {
  operation.setMaxRows(1000);
  try {
    const utils = new hive.HiveUtils(hive.thrift.TCLIService_types);
    await utils.waitUntilReady(operation as never);
    await utils.fetchAll(operation as never);
    throwIfAborted(signal);
    const result = utils.getResult(operation as never).getValue() as unknown;
    return Array.isArray(result) ? result.filter(isRecord) : [];
  } finally {
    await operation.close().catch(() => undefined);
  }
};

const sparkConnectionOptions = (config: Record<string, unknown>): Record<string, unknown> => ({
  host: stringConfig(config, "host"),
  port: numberConfig(config, "port", 10000),
  path: stringConfig(config, "path", "cliservice"),
  options: {
    connectTimeout: numberConfig(config, "timeoutMs", 30000),
    timeout: numberConfig(config, "timeoutMs", 30000)
  }
});

const sparkAuthProvider = (hive: typeof HiveDriverModule, config: Record<string, unknown>): unknown => {
  const authMode = stringConfig(config, "auth", "none");
  if (authMode !== "plain") {
    return new hive.auth.NoSaslAuthentication();
  }
  const credentials = {
    username: stringConfig(config, "username"),
    password: stringConfig(config, "password")
  };
  return stringConfig(config, "transport", "tcp") === "http"
    ? new hive.auth.PlainHttpAuthentication(credentials)
    : new hive.auth.PlainTcpAuthentication(credentials);
};

const arrayRowsToRecords = (columns: string[], data: unknown[][]): Record<string, unknown>[] =>
  data.map((row) => Object.fromEntries(columns.map((column, index) => [column, row[index] ?? null])));

const applyStandardLimit = (sql: string, limit: number): string => {
  if (/\bLIMIT\s+\d+\b/iu.test(sql)) {
    return sql;
  }

  return `SELECT * FROM (${sql}) AS readonly_query LIMIT ${limit}`;
};

const quoteTrinoIdentifier = (identifier: string): string => `"${identifier.replaceAll('"', '""')}"`;

const sqlLiteral = (value: string): string => `'${value.replaceAll("'", "''")}'`;

const trinoServerUrl = (config: Record<string, unknown>): string =>
  optionalStringConfig(config, "server")
  ?? optionalStringConfig(config, "url")
  ?? `${booleanConfig(config, "secure", false) ? "https" : "http"}://`
    + `${stringConfig(config, "host")}:${numberConfig(config, "port", 8080)}`;

const trinoStatementUrl = (config: Record<string, unknown>): string =>
  new URL("v1/statement", `${trinoServerUrl(config).replace(/\/$/u, "")}/`).toString();

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

const rowsToTableResult = (rows: unknown[]): TableResult => {
  const objectRows = rows.filter(isRecord);
  const columns = Array.from(new Set(objectRows.flatMap((row) => Object.keys(row))));

  return {
    columns,
    row_count: objectRows.length,
    rows: objectRows.map((row) => columns.map((column) => row[column] ?? null))
  };
};

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

const stringConfig = (config: Record<string, unknown>, key: string, defaultValue?: string): string => {
  const value = config[key];

  if (typeof value === "string" && value.length > 0) {
    return value;
  }

  if (defaultValue !== undefined) {
    return defaultValue;
  }

  throw new Error(`Missing string config: ${key}`);
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
  if (typeof value === "string" && value.trim() && Number.isFinite(Number(value))) {
    return Number(value);
  }
  return defaultValue;
};

const booleanConfig = (config: Record<string, unknown>, key: string, defaultValue: boolean): boolean => {
  const value = config[key];
  return typeof value === "boolean" ? value : defaultValue;
};

const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null;

const throwIfAborted = (signal?: AbortSignal | undefined): void => {
  if (signal?.aborted) {
    throw signal.reason instanceof Error ? signal.reason : new Error("RUN_CANCELLED");
  }
};
