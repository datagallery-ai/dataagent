import type {
  AdapterExecutionInput,
  AdapterPreviewInput,
  AdapterSqlInput,
  DataSourceAdapter,
  SchemaSummary,
  TableResult
} from "../types.js";
import type * as MsSqlModule from "mssql";
import type * as OdbcModule from "odbc";
import type * as OracleDbModule from "oracledb";

export class SqlServerAdapter implements DataSourceAdapter {
  constructor(protected readonly config: Record<string, unknown>) {}

  async inspectSchema(input: AdapterExecutionInput = {}): Promise<Omit<SchemaSummary, "datasource_id">> {
    throwIfAborted(input.signal);
    const schema = stringConfig(this.config, "schema", "dbo");
    const rows = await this.query(`
      SELECT table_name, column_name, data_type, is_nullable
      FROM information_schema.columns
      WHERE table_schema = @schema
      ORDER BY table_name, ordinal_position
    `, { schema }, input.signal);
    return schemaRowsToSummary(rows, "TABLE_NAME", "COLUMN_NAME", "DATA_TYPE", "IS_NULLABLE");
  }

  async previewTable(input: AdapterPreviewInput): Promise<TableResult> {
    throwIfAborted(input.signal);
    const schema = stringConfig(this.config, "schema", "dbo");
    return rowsToTableResult(await this.query(
      `SELECT TOP (${input.limit}) * FROM ${quoteSqlServerIdentifier(schema)}.${quoteSqlServerIdentifier(input.table)}`,
      {},
      input.signal
    ));
  }

  async runSqlReadonly(input: AdapterSqlInput): Promise<TableResult> {
    throwIfAborted(input.signal);
    return rowsToTableResult(await this.query(applyTopLimit(input.sql, input.limit), {}, input.signal));
  }

  protected async query(
    sql: string,
    parameters: Record<string, string | number>,
    signal?: AbortSignal | undefined
  ): Promise<Record<string, unknown>[]> {
    const mssql = await loadMsSql();
    const pool = new mssql.ConnectionPool({
      server: stringConfig(this.config, "host"),
      port: numberConfig(this.config, "port", 1433),
      database: stringConfig(this.config, "database"),
      user: stringConfig(this.config, "username"),
      password: stringConfig(this.config, "password"),
      requestTimeout: numberConfig(this.config, "timeoutMs", 30000),
      connectionTimeout: numberConfig(this.config, "timeoutMs", 30000),
      options: {
        encrypt: booleanConfig(this.config, "encrypt", true),
        trustServerCertificate: booleanConfig(this.config, "trustServerCertificate", false),
        readOnlyIntent: true
      }
    });
    const abort = (): void => {
      void pool.close().catch(() => undefined);
    };
    try {
      signal?.addEventListener("abort", abort, { once: true });
      await pool.connect();
      throwIfAborted(signal);
      const request = pool.request();
      Object.entries(parameters).forEach(([key, value]) => request.input(key, value));
      const result = await request.query(sql);
      throwIfAborted(signal);
      return result.recordset.filter(isRecord);
    } finally {
      signal?.removeEventListener("abort", abort);
      await pool.close();
    }
  }
}

export class OracleAdapter implements DataSourceAdapter {
  constructor(private readonly config: Record<string, unknown>) {}

  async inspectSchema(input: AdapterExecutionInput = {}): Promise<Omit<SchemaSummary, "datasource_id">> {
    throwIfAborted(input.signal);
    const schema = stringConfig(this.config, "schema", stringConfig(this.config, "username")).toUpperCase();
    const rows = await this.query(`
      SELECT table_name, column_name, data_type, nullable AS is_nullable
      FROM all_tab_columns
      WHERE owner = :schema
      ORDER BY table_name, column_id
    `, { schema }, input.signal);
    return schemaRowsToSummary(rows, "TABLE_NAME", "COLUMN_NAME", "DATA_TYPE", "IS_NULLABLE");
  }

  async previewTable(input: AdapterPreviewInput): Promise<TableResult> {
    throwIfAborted(input.signal);
    const schema = stringConfig(this.config, "schema", stringConfig(this.config, "username"));
    return rowsToTableResult(await this.query(
      `SELECT * FROM ${quoteOracleIdentifier(schema)}.${quoteOracleIdentifier(input.table)} WHERE ROWNUM <= :limit`,
      { limit: input.limit },
      input.signal
    ));
  }

  async runSqlReadonly(input: AdapterSqlInput): Promise<TableResult> {
    throwIfAborted(input.signal);
    return rowsToTableResult(await this.query(applyRowNumLimit(input.sql, input.limit), {}, input.signal));
  }

  private async query(
    sql: string,
    bindParams: Record<string, string | number>,
    signal?: AbortSignal | undefined
  ): Promise<Record<string, unknown>[]> {
    const oracle = await loadOracleDb();
    const connection = await oracle.getConnection({
      user: stringConfig(this.config, "username"),
      password: stringConfig(this.config, "password"),
      connectString: stringConfig(this.config, "connectString")
    });
    const abort = (): void => {
      void connection.break();
    };
    try {
      signal?.addEventListener("abort", abort, { once: true });
      const result = await connection.execute(sql, bindParams, {
        outFormat: oracle.OUT_FORMAT_OBJECT,
        maxRows: numberConfig(this.config, "maxRows", 1000)
      });
      throwIfAborted(signal);
      return Array.isArray(result.rows) ? result.rows.filter(isRecord) : [];
    } finally {
      signal?.removeEventListener("abort", abort);
      await connection.close();
    }
  }
}

export class AccessAdapter implements DataSourceAdapter {
  constructor(private readonly config: Record<string, unknown>) {}

  async inspectSchema(input: AdapterExecutionInput = {}): Promise<Omit<SchemaSummary, "datasource_id">> {
    throwIfAborted(input.signal);
    return await this.withConnection(async (connection) => {
      const tableRows = await connection.tables(null, null, null, "TABLE");
      const tables = [];
      for (const row of tableRows.filter(isRecord)) {
        const tableName = odbcString(row, ["TABLE_NAME", "table_name"]);
        if (!tableName) {
          continue;
        }
        const columnRows = await connection.columns(null, null, tableName, null);
        tables.push({
          name: tableName,
          columns: columnRows.filter(isRecord).map((column) => ({
            name: odbcString(column, ["COLUMN_NAME", "column_name"]) ?? "",
            type: odbcString(column, ["TYPE_NAME", "type_name", "DATA_TYPE", "data_type"]) ?? "TEXT",
            nullable: odbcNullable(column)
          }))
        });
      }
      return { tables };
    }, input.signal);
  }

  async previewTable(input: AdapterPreviewInput): Promise<TableResult> {
    throwIfAborted(input.signal);
    return rowsToTableResult(await this.query(
      `SELECT TOP ${input.limit} * FROM ${quoteAccessIdentifier(input.table)}`,
      input.signal
    ));
  }

  async runSqlReadonly(input: AdapterSqlInput): Promise<TableResult> {
    throwIfAborted(input.signal);
    return rowsToTableResult(await this.query(applyAccessLimit(input.sql, input.limit), input.signal));
  }

  private async query(sql: string, signal?: AbortSignal | undefined): Promise<Record<string, unknown>[]> {
    return await this.withConnection(async (connection) => {
      const rows = await connection.query<Record<string, unknown>>(sql);
      return rows.filter(isRecord);
    }, signal);
  }

  private async withConnection<T>(
    callback: (connection: OdbcModule.Connection) => Promise<T>,
    signal?: AbortSignal | undefined
  ): Promise<T> {
    const odbc = await loadOdbc();
    const connection = await odbc.connect({
      connectionString: accessConnectionString(this.config),
      connectionTimeout: numberConfig(this.config, "timeoutMs", 30000) / 1000,
      loginTimeout: numberConfig(this.config, "timeoutMs", 30000) / 1000
    });
    const abort = (): void => {
      void connection.close();
    };
    try {
      signal?.addEventListener("abort", abort, { once: true });
      await connection.setIsolationLevel(odbc.SQL_TXN_READ_COMMITTED);
      throwIfAborted(signal);
      return await callback(connection);
    } finally {
      signal?.removeEventListener("abort", abort);
      await connection.close();
    }
  }
}

const loadMsSql = async (): Promise<typeof MsSqlModule> => {
  const loaded = await import("mssql") as unknown as { default?: typeof MsSqlModule } & typeof MsSqlModule;
  return loaded.default ?? loaded;
};

const loadOracleDb = async (): Promise<typeof OracleDbModule> => {
  const loaded = await import("oracledb") as unknown as { default?: typeof OracleDbModule } & typeof OracleDbModule;
  return loaded.default ?? loaded;
};

const loadOdbc = async (): Promise<typeof OdbcModule> => {
  const loaded = await import("odbc") as unknown as { default?: typeof OdbcModule } & typeof OdbcModule;
  return loaded.default ?? loaded;
};

const applyTopLimit = (sql: string, limit: number): string => {
  if (/\bSELECT\s+TOP\s*\(/iu.test(sql)) {
    return sql;
  }

  return `SELECT TOP (${limit}) * FROM (${sql}) AS readonly_query`;
};

const applyRowNumLimit = (sql: string, limit: number): string => `SELECT * FROM (${sql}) WHERE ROWNUM <= ${limit}`;

const applyAccessLimit = (sql: string, limit: number): string => {
  if (/\bSELECT\s+TOP\s+\d+\b/iu.test(sql)) {
    return sql;
  }

  return `SELECT TOP ${limit} * FROM (${sql}) AS readonly_query`;
};

const accessConnectionString = (config: Record<string, unknown>): string => {
  const configured = optionalStringConfig(config, "connectionString");
  if (configured) {
    return configured;
  }
  const path = stringConfig(config, "path");
  return `Driver={Microsoft Access Driver (*.mdb, *.accdb)};DBQ=${path};`;
};

const quoteSqlServerIdentifier = (identifier: string): string => `[${identifier.replaceAll("]", "]]")}]`;

const quoteOracleIdentifier = (identifier: string): string => `"${identifier.replaceAll('"', '""').toUpperCase()}"`;

const quoteAccessIdentifier = (identifier: string): string => `[${identifier.replaceAll("]", "]]")}]`;

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

const odbcString = (row: Record<string, unknown>, keys: string[]): string | undefined => {
  const value = keys.map((key) => row[key]).find((candidate) => typeof candidate === "string");
  return typeof value === "string" && value.length > 0 ? value : undefined;
};

const odbcNullable = (row: Record<string, unknown>): boolean => {
  const value = row.NULLABLE ?? row.nullable;
  return value === true || value === 1 || value === "1" || value === "YES";
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
