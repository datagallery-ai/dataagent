import type {
  AdapterExecutionInput,
  AdapterPreviewInput,
  AdapterSqlInput,
  DataSourceAdapter,
  SchemaSummary,
  TableResult
} from "../types.js";
import type * as MongoDbModule from "mongodb";
import type * as RedisModule from "redis";

export class MongoDbAdapter implements DataSourceAdapter {
  constructor(private readonly config: Record<string, unknown>) {}

  async inspectSchema(input: AdapterExecutionInput = {}): Promise<Omit<SchemaSummary, "datasource_id">> {
    throwIfAborted(input.signal);
    return await this.withDb(async (db) => {
      const collections = await db.listCollections().toArray();
      const sampleSize = numberConfig(this.config, "sampleSize", 20);
      const tables = [];
      for (const collectionInfo of collections.filter(isRecord)) {
        const name = requiredRecordString(collectionInfo, "name");
        const rows = await db.collection(name).find({}, { limit: sampleSize }).toArray();
        tables.push({
          name,
          columns: inferDocumentColumns(rows.filter(isRecord))
        });
      }
      return { tables };
    }, input.signal);
  }

  async previewTable(input: AdapterPreviewInput): Promise<TableResult> {
    throwIfAborted(input.signal);
    return await this.withDb(async (db) => {
      const rows = await db.collection(input.table).find({}, { limit: input.limit }).toArray();
      return rowsToTableResult(rows.filter(isRecord).map(flattenDocument));
    }, input.signal);
  }

  async runSqlReadonly(input: AdapterSqlInput): Promise<TableResult> {
    throwIfAborted(input.signal);
    const query = parseLimitedSimpleSelect(input.sql, input.limit);
    return await this.withDb(async (db) => {
      const rows = await db.collection(query.table).find({}, {
        limit: query.limit,
        projection: query.columns.length > 0 ? Object.fromEntries(query.columns.map((column) => [column, 1])) : {}
      }).toArray();
      return rowsToTableResult(rows.filter(isRecord).map(flattenDocument));
    }, input.signal);
  }

  private async withDb<T>(
    callback: (db: MongoDbModule.Db) => Promise<T>,
    signal?: AbortSignal | undefined
  ): Promise<T> {
    const mongodb = await loadMongoDb();
    const client = new mongodb.MongoClient(stringConfig(this.config, "uri"), {
      serverSelectionTimeoutMS: numberConfig(this.config, "timeoutMs", 30000)
    });
    const abort = (): void => {
      void client.close(true);
    };
    try {
      signal?.addEventListener("abort", abort, { once: true });
      await client.connect();
      throwIfAborted(signal);
      return await callback(client.db(stringConfig(this.config, "database")));
    } finally {
      signal?.removeEventListener("abort", abort);
      await client.close();
    }
  }
}

export class RedisAdapter implements DataSourceAdapter {
  constructor(private readonly config: Record<string, unknown>) {}

  async inspectSchema(input: AdapterExecutionInput = {}): Promise<Omit<SchemaSummary, "datasource_id">> {
    throwIfAborted(input.signal);
    return {
      tables: [
        {
          name: "redis_keys",
          columns: [
            { name: "key", type: "TEXT", nullable: false },
            { name: "type", type: "TEXT", nullable: false },
            { name: "ttl", type: "INTEGER", nullable: true },
            { name: "value", type: "TEXT", nullable: true }
          ]
        }
      ]
    };
  }

  async previewTable(input: AdapterPreviewInput): Promise<TableResult> {
    if (input.table !== "redis_keys") {
      throw new Error(`Table not found: ${input.table}`);
    }
    return await this.readKeys(input.limit, input.signal);
  }

  async runSqlReadonly(input: AdapterSqlInput): Promise<TableResult> {
    const query = parseLimitedSimpleSelect(input.sql, input.limit);
    if (query.table !== "redis_keys") {
      throw new Error(`Table not found: ${query.table}`);
    }
    return await this.readKeys(query.limit, input.signal);
  }

  private async readKeys(limit: number, signal?: AbortSignal | undefined): Promise<TableResult> {
    const redis = await loadRedis();
    const client = redis.createClient({
      url: stringConfig(this.config, "url"),
      database: numberConfig(this.config, "database", 0)
    }) as RedisReadonlyClient;
    const abort = (): void => {
      void client.disconnect();
    };
    try {
      signal?.addEventListener("abort", abort, { once: true });
      await client.connect();
      const rows: Record<string, unknown>[] = [];
      const pattern = stringConfig(this.config, "keyPattern", "*");
      for await (const batch of client.scanIterator({ MATCH: pattern, COUNT: Math.max(limit, 10) })) {
        const keys = Array.isArray(batch) ? batch : [batch];
        for (const key of keys) {
          if (rows.length >= limit) {
            return rowsToTableResult(rows);
          }
          rows.push(await this.redisKeyRow(client, key));
        }
      }
      return rowsToTableResult(rows);
    } finally {
      signal?.removeEventListener("abort", abort);
      await client.quit();
    }
  }

  private async redisKeyRow(client: RedisReadonlyClient, key: string): Promise<Record<string, unknown>> {
    const type = await client.type(key);
    const ttl = await client.ttl(key);
    return {
      key,
      type,
      ttl,
      value: await redisPreviewValue(client, key, type)
    };
  }
}

type RedisReadonlyClient = {
  connect(): Promise<unknown>;
  disconnect(): Promise<unknown>;
  quit(): Promise<unknown>;
  scanIterator(input: { COUNT: number; MATCH: string }): AsyncIterable<string[] | string>;
  type(key: string): Promise<string>;
  ttl(key: string): Promise<number>;
  get(key: string): Promise<string | null>;
  hGetAll(key: string): Promise<Record<string, string>>;
  lRange(key: string, start: number, stop: number): Promise<string[]>;
  sMembers(key: string): Promise<string[]>;
  zRange(key: string, start: number, stop: number): Promise<string[]>;
};

type LimitedSimpleSelect = {
  columns: string[];
  limit: number;
  table: string;
};

const loadMongoDb = async (): Promise<typeof MongoDbModule> => await import("mongodb");

const loadRedis = async (): Promise<typeof RedisModule> => await import("redis");

const parseLimitedSimpleSelect = (sql: string, defaultLimit: number): LimitedSimpleSelect => {
  const match = /^SELECT\s+(.+?)\s+FROM\s+([`"\w.-]+)(?:\s+LIMIT\s+(\d+))?$/iu.exec(sql);
  if (!match) {
    throw new Error("SIMPLE_SELECT_REQUIRED");
  }
  const rawColumns = match[1] ?? "*";
  const table = unquoteIdentifier((match[2] ?? "").trim());
  const limit = Math.min(Number(match[3] ?? defaultLimit), defaultLimit);
  return {
    columns: rawColumns.trim() === "*"
      ? []
      : rawColumns.split(",").map((column) => unquoteIdentifier(column.trim())).filter(Boolean),
    limit,
    table
  };
};

const inferDocumentColumns = (rows: Record<string, unknown>[]): SchemaSummary["tables"][number]["columns"] => {
  const columns = new Map<string, string>();
  rows.map(flattenDocument).forEach((row) => {
    Object.entries(row).forEach(([key, value]) => {
      if (!columns.has(key)) {
        columns.set(key, inferColumnType([row], key) || typeof value);
      }
    });
  });
  return [...columns.entries()].map(([name, type]) => ({ name, type: type.toUpperCase() }));
};

const flattenDocument = (row: Record<string, unknown>): Record<string, unknown> =>
  Object.fromEntries(Object.entries(row).map(([key, value]) => [
    key,
    isRecord(value) || Array.isArray(value) ? JSON.stringify(value) : value
  ]));

const redisPreviewValue = async (client: RedisReadonlyClient, key: string, type: string): Promise<string | null> => {
  if (type === "string") {
    return await client.get(key);
  }
  if (type === "hash") {
    return JSON.stringify(await client.hGetAll(key));
  }
  if (type === "list") {
    return JSON.stringify(await client.lRange(key, 0, 4));
  }
  if (type === "set") {
    return JSON.stringify((await client.sMembers(key)).slice(0, 5));
  }
  if (type === "zset") {
    return JSON.stringify(await client.zRange(key, 0, 4));
  }
  return null;
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

const inferColumnType = (rows: Record<string, unknown>[], column: string): string => {
  const value = rows.find((row) => row[column] !== null && row[column] !== undefined)?.[column];

  if (typeof value === "number") {
    return Number.isInteger(value) ? "INTEGER" : "DOUBLE";
  }

  if (typeof value === "boolean") {
    return "BOOLEAN";
  }

  return "TEXT";
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

const unquoteIdentifier = (identifier: string): string =>
  identifier.replace(/^"/u, "").replace(/"$/u, "").replaceAll('""', '"');

const requiredRecordString = (row: unknown, key: string): string => {
  if (!isRecord(row) || typeof row[key] !== "string") {
    throw new Error(`Expected string column: ${key}`);
  }

  return row[key];
};

const throwIfAborted = (signal?: AbortSignal | undefined): void => {
  if (signal?.aborted) {
    throw signal.reason instanceof Error ? signal.reason : new Error("RUN_CANCELLED");
  }
};

const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null;
