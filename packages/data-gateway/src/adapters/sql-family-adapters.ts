import type {
  AdapterExecutionInput,
  AdapterPreviewInput,
  AdapterSqlInput,
  DataSourceAdapter,
  SchemaSummary,
  TableResult
} from "../types.js";
import { createConnection, type Connection, type RowDataPacket } from "mysql2/promise";
import { Pool, type PoolClient } from "pg";

export class PostgreSqlAdapter implements DataSourceAdapter {
  constructor(private readonly config: Record<string, unknown>) {}

  async inspectSchema(input: AdapterExecutionInput = {}): Promise<Omit<SchemaSummary, "datasource_id">> {
    throwIfAborted(input.signal);
    const schema = stringConfig(this.config, "schema", "public");
    const rows = await this.query(`
      SELECT table_name, column_name, data_type, is_nullable
      FROM information_schema.columns
      WHERE table_schema = $1
      ORDER BY table_name, ordinal_position
    `, [schema], input.signal);
    return schemaRowsToSummary(rows, "table_name", "column_name", "data_type", "is_nullable");
  }

  async previewTable(input: AdapterPreviewInput): Promise<TableResult> {
    throwIfAborted(input.signal);
    const schema = stringConfig(this.config, "schema", "public");
    return rowsToTableResult(await this.query(
      `SELECT * FROM ${quoteIdentifier(schema)}.${quoteIdentifier(input.table)} LIMIT $1`,
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
    values: unknown[] = [],
    signal?: AbortSignal | undefined
  ): Promise<Record<string, unknown>[]> {
    throwIfAborted(signal);
    const pool = new Pool({
      host: stringConfig(this.config, "host"),
      port: numberConfig(this.config, "port", 5432),
      database: stringConfig(this.config, "database"),
      user: stringConfig(this.config, "username"),
      password: stringConfig(this.config, "password"),
      max: 1,
      statement_timeout: numberConfig(this.config, "timeoutMs", 30000)
    });
    let client: PoolClient | undefined;
    let aborted = false;
    const abort = (): void => {
      aborted = true;
      client?.release(true);
    };
    try {
      signal?.addEventListener("abort", abort, { once: true });
      client = await pool.connect();
      throwIfAborted(signal);
      await client.query("BEGIN READ ONLY");
      const result = await client.query(sql, values);
      throwIfAborted(signal);
      await client.query("ROLLBACK");
      return result.rows.filter(isRecord);
    } finally {
      signal?.removeEventListener("abort", abort);
      if (client) {
        client.release(aborted);
      }
      await pool.end();
    }
  }
}

export class MySqlAdapter implements DataSourceAdapter {
  constructor(private readonly config: Record<string, unknown>) {}

  async inspectSchema(input: AdapterExecutionInput = {}): Promise<Omit<SchemaSummary, "datasource_id">> {
    throwIfAborted(input.signal);
    const database = stringConfig(this.config, "database");
    const rows = await this.query(`
      SELECT table_name, column_name, data_type, is_nullable
      FROM information_schema.columns
      WHERE table_schema = ?
      ORDER BY table_name, ordinal_position
    `, [database], input.signal);
    return schemaRowsToSummary(rows, "TABLE_NAME", "COLUMN_NAME", "DATA_TYPE", "IS_NULLABLE");
  }

  async previewTable(input: AdapterPreviewInput): Promise<TableResult> {
    throwIfAborted(input.signal);
    return rowsToTableResult(await this.query(
      `SELECT * FROM ${quoteMysqlIdentifier(input.table)} LIMIT ?`,
      [input.limit],
      input.signal
    ));
  }

  async runSqlReadonly(input: AdapterSqlInput): Promise<TableResult> {
    throwIfAborted(input.signal);
    return rowsToTableResult(await this.query(applyStandardLimit(input.sql, input.limit), [], input.signal));
  }

  protected async query(
    sql: string,
    values: unknown[] = [],
    signal?: AbortSignal | undefined
  ): Promise<Record<string, unknown>[]> {
    throwIfAborted(signal);
    const connection = await createConnection({
      host: stringConfig(this.config, "host"),
      port: numberConfig(this.config, "port", 3306),
      database: stringConfig(this.config, "database"),
      user: stringConfig(this.config, "username"),
      password: stringConfig(this.config, "password"),
      connectTimeout: numberConfig(this.config, "timeoutMs", 30000)
    });
    const abort = (): void => {
      void connection.destroy();
    };
    try {
      signal?.addEventListener("abort", abort, { once: true });
      // SET TRANSACTION must run before a transaction starts (MySQL/MariaDB).
      await connection.query("SET TRANSACTION READ ONLY");
      await connection.beginTransaction();
      const [rows] = await connection.query<RowDataPacket[]>(sql, values);
      await connection.rollback();
      return rows.filter(isRecord);
    } finally {
      signal?.removeEventListener("abort", abort);
      await connection.end();
    }
  }
}

export class GaussDbAdapter extends PostgreSqlAdapter {}

export class StarRocksAdapter extends MySqlAdapter {}

export class RedshiftAdapter extends PostgreSqlAdapter {}

export class GreenplumAdapter extends PostgreSqlAdapter {}

export class DorisAdapter extends MySqlAdapter {}

export class MariaDbAdapter extends MySqlAdapter {}

export class TiDbAdapter extends MySqlAdapter {}

export class OceanBaseAdapter extends MySqlAdapter {}

const applyStandardLimit = (sql: string, limit: number): string => {
  if (/\bLIMIT\s+\d+\b/iu.test(sql)) {
    return sql;
  }

  return `SELECT * FROM (${sql}) AS readonly_query LIMIT ${limit}`;
};

const rowsToTableResult = (rows: unknown[]): TableResult => {
  const objectRows = rows.filter(isRecord);
  const columns = Array.from(new Set(objectRows.flatMap((row) => Object.keys(row))));

  return objectRowsToTableResult(objectRows, columns);
};

const objectRowsToTableResult = (rows: Record<string, unknown>[], columns: string[]): TableResult => ({
  columns,
  rows: rows.map((row) => columns.map((column) => row[column] ?? null)),
  row_count: rows.length
});

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

const quoteIdentifier = (identifier: string): string => `"${identifier.replaceAll('"', '""')}"`;

const quoteMysqlIdentifier = (identifier: string): string => `\`${identifier.replaceAll("`", "``")}\``;

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

const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null;
