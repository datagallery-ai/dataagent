import type {
  AdapterExecutionInput,
  AdapterPreviewInput,
  AdapterSqlInput,
  DataSourceAdapter,
  SchemaSummary,
  TableResult
} from "../types.js";
import type * as ElasticSearchModule from "@elastic/elasticsearch";
import type * as OpenSearchModule from "@opensearch-project/opensearch";

export class ElasticsearchAdapter implements DataSourceAdapter {
  constructor(private readonly config: Record<string, unknown>) {}

  async inspectSchema(input: AdapterExecutionInput = {}): Promise<Omit<SchemaSummary, "datasource_id">> {
    throwIfAborted(input.signal);
    const elasticsearch = await loadElasticSearch();
    const client = new elasticsearch.Client(searchClientOptions(this.config));
    const mappings = await client.indices.getMapping({ index: searchIndexPattern(this.config) });
    return searchMappingsToSchema(mappings as unknown);
  }

  async previewTable(input: AdapterPreviewInput): Promise<TableResult> {
    throwIfAborted(input.signal);
    return await this.search(input.table, input.limit, input.signal);
  }

  async runSqlReadonly(input: AdapterSqlInput): Promise<TableResult> {
    throwIfAborted(input.signal);
    const query = parseLimitedSimpleSelect(input.sql, input.limit);
    return await this.search(query.table, query.limit, input.signal, query.columns);
  }

  private async search(
    index: string,
    limit: number,
    signal?: AbortSignal | undefined,
    columns: string[] = []
  ): Promise<TableResult> {
    const elasticsearch = await loadElasticSearch();
    const client = new elasticsearch.Client(searchClientOptions(this.config));
    const result = await client.search({
      index,
      size: limit,
      _source: columns.length > 0 ? columns : true,
      query: { match_all: {} }
    }, signal ? ({ signal } as never) : undefined);
    return searchHitsToTableResult(result as unknown);
  }
}

export class OpenSearchAdapter implements DataSourceAdapter {
  constructor(private readonly config: Record<string, unknown>) {}

  async inspectSchema(input: AdapterExecutionInput = {}): Promise<Omit<SchemaSummary, "datasource_id">> {
    throwIfAborted(input.signal);
    const opensearch = await loadOpenSearch();
    const client = new opensearch.Client(searchClientOptions(this.config));
    const mappings = await client.indices.getMapping({ index: searchIndexPattern(this.config) });
    return searchMappingsToSchema(mappings as unknown);
  }

  async previewTable(input: AdapterPreviewInput): Promise<TableResult> {
    throwIfAborted(input.signal);
    return await this.search(input.table, input.limit, input.signal);
  }

  async runSqlReadonly(input: AdapterSqlInput): Promise<TableResult> {
    throwIfAborted(input.signal);
    const query = parseLimitedSimpleSelect(input.sql, input.limit);
    return await this.search(query.table, query.limit, input.signal, query.columns);
  }

  private async search(
    index: string,
    limit: number,
    signal?: AbortSignal | undefined,
    columns: string[] = []
  ): Promise<TableResult> {
    const opensearch = await loadOpenSearch();
    const client = new opensearch.Client(searchClientOptions(this.config));
    const result = await client.search({
      index,
      size: limit,
      _source: columns.length > 0 ? columns : true,
      body: { query: { match_all: {} } }
    }, signal ? ({ signal } as never) : undefined);
    return searchHitsToTableResult(result as unknown);
  }
}

type LimitedSimpleSelect = {
  columns: string[];
  limit: number;
  table: string;
};

const loadElasticSearch = async (): Promise<typeof ElasticSearchModule> => await import("@elastic/elasticsearch");

const loadOpenSearch = async (): Promise<typeof OpenSearchModule> => await import("@opensearch-project/opensearch");

const searchClientOptions = (config: Record<string, unknown>): Record<string, unknown> => {
  const auth = optionalStringConfig(config, "apiKey")
    ? { apiKey: stringConfig(config, "apiKey") }
    : optionalStringConfig(config, "username") || optionalStringConfig(config, "password")
      ? {
          username: stringConfig(config, "username", ""),
          password: stringConfig(config, "password", "")
        }
      : undefined;
  return {
    node: searchNodeUrl(config),
    ...(auth ? { auth } : {})
  };
};

const searchNodeUrl = (config: Record<string, unknown>): string =>
  optionalStringConfig(config, "node")
  ?? optionalStringConfig(config, "url")
  ?? `${booleanConfig(config, "secure", false) ? "https" : "http"}://`
    + `${stringConfig(config, "host")}:${numberConfig(config, "port", 9200)}`;

const searchIndexPattern = (config: Record<string, unknown>): string => stringConfig(config, "indexPattern", "*");

const searchMappingsToSchema = (mappings: unknown): Omit<SchemaSummary, "datasource_id"> => {
  const root = isRecord(mappings) ? mappings : {};
  const tables = Object.entries(root).map(([index, mapping]) => ({
    name: index,
    columns: searchMappingColumns(mapping)
  }));
  return { tables };
};

const searchMappingColumns = (mapping: unknown): SchemaSummary["tables"][number]["columns"] => {
  const properties = searchMappingProperties(mapping);
  return Object.entries(flattenSearchProperties(properties)).map(([name, type]) => ({
    name,
    type: type.toUpperCase()
  }));
};

const searchMappingProperties = (mapping: unknown): Record<string, unknown> => {
  if (!isRecord(mapping)) {
    return {};
  }
  const directMappings = isRecord(mapping.mappings) ? mapping.mappings : mapping;
  return isRecord(directMappings.properties) ? directMappings.properties : {};
};

const flattenSearchProperties = (properties: Record<string, unknown>, prefix = ""): Record<string, string> => {
  const flattened: Record<string, string> = {};
  Object.entries(properties).forEach(([key, value]) => {
    const name = prefix ? `${prefix}.${key}` : key;
    if (isRecord(value) && isRecord(value.properties)) {
      Object.assign(flattened, flattenSearchProperties(value.properties, name));
    } else if (isRecord(value) && typeof value.type === "string") {
      flattened[name] = value.type;
    } else {
      flattened[name] = "object";
    }
  });
  return flattened;
};

const searchHitsToTableResult = (result: unknown): TableResult => {
  const hitsContainer = isRecord(result) && isRecord(result.hits) ? result.hits : {};
  const rawHits = isRecord(hitsContainer) && Array.isArray(hitsContainer.hits) ? hitsContainer.hits : [];
  const rows = rawHits.filter(isRecord).map((hit) => flattenDocument({
    _id: hit._id,
    ...(isRecord(hit._source) ? hit._source : {})
  }));
  return rowsToTableResult(rows);
};

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

const flattenDocument = (row: Record<string, unknown>): Record<string, unknown> =>
  Object.fromEntries(Object.entries(row).map(([key, value]) => [
    key,
    isRecord(value) || Array.isArray(value) ? JSON.stringify(value) : value
  ]));

const rowsToTableResult = (rows: unknown[]): TableResult => {
  const objectRows = rows.filter(isRecord);
  const columns = Array.from(new Set(objectRows.flatMap((row) => Object.keys(row))));

  return {
    columns,
    rows: objectRows.map((row) => columns.map((column) => row[column] ?? null)),
    row_count: objectRows.length
  };
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

const unquoteIdentifier = (identifier: string): string =>
  identifier.replace(/^"/u, "").replace(/"$/u, "").replaceAll('""', '"');

const throwIfAborted = (signal?: AbortSignal | undefined): void => {
  if (signal?.aborted) {
    throw signal.reason instanceof Error ? signal.reason : new Error("RUN_CANCELLED");
  }
};

const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null;
