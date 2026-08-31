import type {
  AdapterExecutionInput,
  AdapterPreviewInput,
  AdapterSqlInput,
  DataSourceAdapter,
  SchemaSummary,
  TableResult
} from "../types.js";
import { demoDuckDbPath } from "../demo-duckdb.js";
import { DuckDbAdapter } from "./local-sql-adapters.js";
import { mkdirSync, readFileSync } from "node:fs";
import { dirname } from "node:path";
import readXlsxFile from "read-excel-file/node";
import type * as DuckDbModule from "duckdb";

const INSERT_BATCH_SIZE = 500;

export class DuckDbDemoAdapter implements DataSourceAdapter {
  constructor(private readonly config: Record<string, unknown>) {}

  async inspectSchema(input: AdapterExecutionInput = {}): Promise<Omit<SchemaSummary, "datasource_id">> {
    throwIfAborted(input.signal);
    return await (await this.fileAdapter(input.signal)).inspectSchema(input);
  }

  async previewTable(input: AdapterPreviewInput): Promise<TableResult> {
    throwIfAborted(input.signal);
    return await (await this.fileAdapter(input.signal)).previewTable(input);
  }

  async runSqlReadonly(input: AdapterSqlInput): Promise<TableResult> {
    throwIfAborted(input.signal);
    return await (await this.fileAdapter(input.signal)).runSqlReadonly(input);
  }

  private async fileAdapter(signal?: AbortSignal | undefined): Promise<DuckDbAdapter> {
    const path = await ensureDemoDuckDbFile(this.config, signal);
    return new DuckDbAdapter({ ...this.config, mode: "file", path });
  }
}

export class CsvAdapter implements DataSourceAdapter {
  constructor(private readonly config: Record<string, unknown>) {}

  async inspectSchema(input: AdapterExecutionInput = {}): Promise<Omit<SchemaSummary, "datasource_id">> {
    throwIfAborted(input.signal);
    const table = this.readTable(100, input.signal);

    return {
      tables: [
        {
          name: table.name,
          columns: table.columns.map((column) => ({ name: column, type: inferCsvColumnType(table.rows, column) }))
        }
      ]
    };
  }

  async previewTable(input: AdapterPreviewInput): Promise<TableResult> {
    throwIfAborted(input.signal);
    const table = this.readTable(input.limit, input.signal);

    if (input.table !== table.name) {
      throw new Error(`Table not found: ${input.table}`);
    }

    return objectRowsToTableResult(table.rows, table.columns);
  }

  async runSqlReadonly(input: AdapterSqlInput): Promise<TableResult> {
    throwIfAborted(input.signal);
    return await executeReadonlySqlOnTables([this.readTable(undefined, input.signal)], input.sql, input.limit, input.signal);
  }

  private readTable(limit?: number | undefined, signal?: AbortSignal | undefined): DatasetTable {
    throwIfAborted(signal);
    const filePath = stringConfig(this.config, "file_path");
    const raw = readFileSync(filePath, "utf8");
    throwIfAborted(signal);
    const parsedRows = parseCsv(raw, limit === undefined ? Number.POSITIVE_INFINITY : limit + 1);
    const columns = parsedRows[0] ?? [];
    const rows = parsedRows.slice(1).map((row) => columnsToObject(columns, row));

    return {
      name: stringConfig(this.config, "table_name", "dataset"),
      columns,
      rows
    };
  }
}

export class XlsxAdapter implements DataSourceAdapter {
  constructor(private readonly config: Record<string, unknown>) {}

  async inspectSchema(input: AdapterExecutionInput = {}): Promise<Omit<SchemaSummary, "datasource_id">> {
    throwIfAborted(input.signal);
    const table = await this.readTable(100, input.signal);

    return {
      tables: [
        {
          name: table.name,
          columns: table.columns.map((column) => ({ name: column, type: inferCsvColumnType(table.rows, column) }))
        }
      ]
    };
  }

  async previewTable(input: AdapterPreviewInput): Promise<TableResult> {
    throwIfAborted(input.signal);
    const table = await this.readTable(input.limit, input.signal);

    if (input.table !== table.name) {
      throw new Error(`Table not found: ${input.table}`);
    }

    return objectRowsToTableResult(table.rows, table.columns);
  }

  async runSqlReadonly(input: AdapterSqlInput): Promise<TableResult> {
    throwIfAborted(input.signal);
    return await executeReadonlySqlOnTables([await this.readTable(undefined, input.signal)], input.sql, input.limit, input.signal);
  }

  private async readTable(limit?: number | undefined, signal?: AbortSignal | undefined): Promise<DatasetTable> {
    throwIfAborted(signal);
    const filePath = stringConfig(this.config, "file_path");
    const rows = normalizeXlsxRows(await readXlsxFile(filePath, { dateFormat: "yyyy-mm-dd" }));
    throwIfAborted(signal);
    const columns = (rows[0] ?? []).map((value: unknown) => String(value ?? ""));
    const objectRows = rows.slice(1, limit === undefined ? undefined : limit + 1).map((row) => columnsToObject(columns, row));

    return {
      name: stringConfig(this.config, "table_name", "dataset"),
      columns,
      rows: objectRows
    };
  }
}

type DatasetTable = {
  name: string;
  columns: string[];
  rows: Record<string, unknown>[];
};

const ensureDemoDuckDbFile = async (
  config: Record<string, unknown>,
  signal?: AbortSignal | undefined
): Promise<string> => {
  const path = stringConfig(config, "path", demoDuckDbPath());
  mkdirSync(dirname(path), { recursive: true });
  const duckdb = await loadDuckDb();
  const database = new duckdb.Database(path);
  const connection = database.connect();

  try {
    const rows = await duckDbAll(connection, [
      "SELECT COUNT(*) AS count",
      "FROM information_schema.tables",
      "WHERE table_schema = 'main' AND table_name = 'orders'"
    ].join(" "), signal);
    if (recordNumberLoose(rows[0], "count") === 0) {
      const tables = demoTables(config);
      if (tables.length === 0) {
        throw new Error("Demo DuckDB datasource has no tables to seed.");
      }
      for (const table of tables) {
        await loadTableIntoDuckDb(connection, table, signal);
      }
    }
  } finally {
    await duckDbClose(connection);
    await duckDbCloseDatabase(database);
  }

  return path;
};

const executeReadonlySqlOnTables = async (
  tables: DatasetTable[],
  sql: string,
  limit: number,
  signal?: AbortSignal | undefined
): Promise<TableResult> => {
  const duckdb = await loadDuckDb();
  const database = new duckdb.Database(":memory:");
  const connection = database.connect();

  try {
    for (const table of tables) {
      await loadTableIntoDuckDb(connection, table, signal);
    }

    const rows = await duckDbAll(connection, applyStandardLimit(sql, limit), signal);
    return rowsToTableResult(rows);
  } finally {
    await duckDbClose(connection);
    await duckDbCloseDatabase(database);
  }
};

const loadTableIntoDuckDb = async (
  connection: DuckDbModule.Connection,
  table: DatasetTable,
  signal?: AbortSignal | undefined
): Promise<void> => {
  throwIfAborted(signal);
  const columnTypes = new Map(table.columns.map((column) => [column, inferDuckDbColumnType(table.rows, column)]));
  const columnDefinitions = table.columns
    .map((column) => `${quoteIdentifier(column)} ${columnTypes.get(column) ?? "VARCHAR"}`)
    .join(", ");
  await duckDbRun(connection, `CREATE TABLE ${quoteIdentifier(table.name)} (${columnDefinitions})`, signal);

  if (table.rows.length === 0) {
    return;
  }

  for (let offset = 0; offset < table.rows.length; offset += INSERT_BATCH_SIZE) {
    const batchRows = table.rows.slice(offset, offset + INSERT_BATCH_SIZE);
    const rowsSql = batchRows
      .map((row) => `(${table.columns.map((column) => sqlLiteral(row[column], columnTypes.get(column))).join(", ")})`)
      .join(", ");
    await duckDbRun(connection, `INSERT INTO ${quoteIdentifier(table.name)} VALUES ${rowsSql}`, signal);
  }
};

const loadDuckDb = async (): Promise<typeof DuckDbModule> => {
  const loaded = await import("duckdb") as unknown as { default?: typeof DuckDbModule } & typeof DuckDbModule;
  return loaded.default ?? loaded;
};

const duckDbRun = async (
  connection: DuckDbModule.Connection,
  sql: string,
  signal?: AbortSignal | undefined
): Promise<void> =>
  await new Promise((resolve, reject) => {
    const abort = (): void => {
      reject(signal?.reason instanceof Error ? signal.reason : new Error("RUN_CANCELLED"));
    };
    signal?.addEventListener("abort", abort, { once: true });
    connection.run(sql, (error) => {
      signal?.removeEventListener("abort", abort);
      if (error) {
        reject(error);
      } else {
        resolve();
      }
    });
  });

const duckDbAll = async (
  connection: DuckDbModule.Connection,
  sql: string,
  signal?: AbortSignal | undefined
): Promise<DuckDbModule.TableData> =>
  await new Promise((resolve, reject) => {
    const abort = (): void => {
      reject(signal?.reason instanceof Error ? signal.reason : new Error("RUN_CANCELLED"));
    };
    signal?.addEventListener("abort", abort, { once: true });
    connection.all(sql, (error, rows) => {
      signal?.removeEventListener("abort", abort);
      if (error) {
        reject(error);
      } else {
        resolve(rows);
      }
    });
  });

const duckDbClose = async (connection: DuckDbModule.Connection): Promise<void> =>
  await new Promise((resolve, reject) => {
    connection.close((error) => error ? reject(error) : resolve());
  });

const duckDbCloseDatabase = async (database: DuckDbModule.Database): Promise<void> =>
  await new Promise((resolve, reject) => {
    database.close((error) => error ? reject(error) : resolve());
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

  return objectRowsToTableResult(objectRows, columns);
};

const recordNumberLoose = (row: unknown, key: string): number => {
  if (!isRecord(row)) {
    return 0;
  }

  const value = row[key] ?? row[key.toUpperCase()] ?? row[key.toLowerCase()];
  const numericValue = typeof value === "bigint" ? Number(value) : Number(value);
  return Number.isFinite(numericValue) ? numericValue : 0;
};

const inferDuckDbColumnType = (rows: Record<string, unknown>[], column: string): string => {
  const values = rows
    .map((row) => row[column])
    .filter((value) => value !== null && value !== undefined && value !== "");

  if (values.length > 0 && values.every((value) => typeof value === "number" || Number.isFinite(Number(value)))) {
    return "DOUBLE";
  }

  if (values.length > 0 && values.every((value) => typeof value === "boolean")) {
    return "BOOLEAN";
  }

  return "VARCHAR";
};

const sqlLiteral = (value: unknown, columnType?: string | undefined): string => {
  if (value === null || value === undefined || value === "") {
    return "NULL";
  }

  if (columnType === "DOUBLE") {
    const numericValue = Number(value);
    return Number.isFinite(numericValue) ? String(numericValue) : "NULL";
  }

  if (columnType === "BOOLEAN") {
    return value === true ? "TRUE" : "FALSE";
  }

  return `'${String(value).replaceAll("'", "''")}'`;
};

const normalizeXlsxRows = (value: unknown): unknown[][] => {
  if (Array.isArray(value) && value.every(Array.isArray)) {
    return value;
  }

  if (Array.isArray(value) && value.length > 0 && isRecord(value[0]) && Array.isArray(value[0].data)) {
    const firstSheetRows = value[0].data;

    if (firstSheetRows.every(Array.isArray)) {
      return firstSheetRows;
    }
  }

  throw new Error("Unsupported XLSX row format");
};

const objectRowsToTableResult = (rows: Record<string, unknown>[], columns: string[]): TableResult => ({
  columns,
  rows: rows.map((row) => columns.map((column) => row[column] ?? null)),
  row_count: rows.length
});

const inferCsvColumnType = (rows: Record<string, unknown>[], column: string): string => {
  const values = rows
    .map((row) => row[column])
    .filter((value) => value !== null && value !== undefined && value !== "");

  if (values.length > 0 && values.every((value) => Number.isFinite(Number(value)))) {
    return "DOUBLE";
  }

  return "TEXT";
};

const demoTables = (config: Record<string, unknown>): DatasetTable[] => {
  const configuredTables = config.tables;

  if (Array.isArray(configuredTables)) {
    return configuredTables.filter(isDatasetTable);
  }

  const rows = [
    {
      order_id: "o_001", channel: "search", category: "electronics", user_type: "new",
      gmv: 1280, created_at: "2026-05-18"
    },
    {
      order_id: "o_002", channel: "social", category: "beauty", user_type: "returning",
      gmv: 640, created_at: "2026-05-19"
    },
    {
      order_id: "o_003", channel: "direct", category: "home", user_type: "returning",
      gmv: 920, created_at: "2026-05-20"
    }
  ];

  return [
    {
      name: "orders",
      columns: ["order_id", "channel", "category", "user_type", "gmv", "created_at"],
      rows
    }
  ];
};

const parseCsv = (raw: string, maxRows: number): string[][] => {
  const rows: string[][] = [];
  let current = "";
  let currentRow: string[] = [];
  let inQuotes = false;

  for (let index = 0; index < raw.length; index += 1) {
    const char = raw[index];
    const nextChar = raw[index + 1];

    if (char === '"' && nextChar === '"') {
      current += '"';
      index += 1;
      continue;
    }

    if (char === '"') {
      inQuotes = !inQuotes;
      continue;
    }

    if (char === "," && !inQuotes) {
      currentRow.push(current);
      current = "";
      continue;
    }

    if ((char === "\n" || char === "\r") && !inQuotes) {
      if (char === "\r" && nextChar === "\n") {
        index += 1;
      }

      currentRow.push(current);
      rows.push(currentRow);
      current = "";
      currentRow = [];

      if (rows.length >= maxRows) {
        return rows;
      }

      continue;
    }

    current += char;
  }

  if (current.length > 0 || currentRow.length > 0) {
    currentRow.push(current);
    rows.push(currentRow);
  }

  return rows;
};

const columnsToObject = (columns: string[], row: readonly unknown[]): Record<string, unknown> =>
  Object.fromEntries(columns.map((column, index) => [column, row[index] ?? null]));

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

const quoteIdentifier = (identifier: string): string => `"${identifier.replaceAll('"', '""')}"`;

const throwIfAborted = (signal?: AbortSignal | undefined): void => {
  if (signal?.aborted) {
    throw signal.reason instanceof Error ? signal.reason : new Error("RUN_CANCELLED");
  }
};

const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null;

const isDatasetTable = (value: unknown): value is DatasetTable => {
  if (!isRecord(value)) {
    return false;
  }

  return typeof value.name === "string" && Array.isArray(value.columns) && Array.isArray(value.rows);
};
