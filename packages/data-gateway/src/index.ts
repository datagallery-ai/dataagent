import { LocalArtifactService, type CreateArtifactInput } from "@datafoundry/artifacts";
import type { ArtifactSummary, DataSourceSummary } from "@datafoundry/contracts";
import type { DataSourceRecord, MetadataStore } from "@datafoundry/metadata";
import type { FileAssetService } from "@datafoundry/files";
import {
  AccessAdapter,
  OracleAdapter,
  SqlServerAdapter
} from "./adapters/enterprise-sql-adapters.js";
import {
  CsvAdapter,
  XlsxAdapter
} from "./adapters/local-dataset-adapters.js";
import {
  DuckDbAdapter,
  SQLiteAdapter
} from "./adapters/local-sql-adapters.js";
import {
  DatabricksSqlAdapter,
  PrestoAdapter,
  SparkSqlAdapter,
  TrinoAdapter
} from "./adapters/lakehouse-adapters.js";
import {
  MongoDbAdapter,
  RedisAdapter
} from "./adapters/nosql-adapters.js";
import {
  DorisAdapter,
  GaussDbAdapter,
  GreenplumAdapter,
  MariaDbAdapter,
  MySqlAdapter,
  OceanBaseAdapter,
  PostgreSqlAdapter,
  RedshiftAdapter,
  StarRocksAdapter,
  TiDbAdapter
} from "./adapters/sql-family-adapters.js";
import {
  BigQueryAdapter,
  ClickHouseAdapter,
  SnowflakeAdapter
} from "./adapters/warehouse-adapters.js";
import {
  ElasticsearchAdapter,
  OpenSearchAdapter
} from "./adapters/search-adapters.js";
import { createAdapterRegistry, createRegisteredAdapter } from "./adapter-registry.js";
import { guardReadonlySql, stripQuotedSql } from "./readonly-guard.js";
import { SUPPORTED_DATA_SOURCE_TYPES } from "./supported-types.js";
import type {
  AdapterExecutionInput,
  AdapterPreviewInput,
  AdapterSqlInput,
  DataGateway,
  DataGatewayPolicy,
  DataSourceAdapter,
  DataSourceRuntimePolicy,
  DataSourceType,
  InspectSchemaInput,
  ListDataSourcesInput,
  PreviewTableInput,
  RegisterDataSourceInput,
  RunSqlReadonlyInput,
  SchemaSummary,
  SqlExecutionResult,
  SupportedDataSourceType,
  TableResult,
  TestConnectInput
} from "./types.js";
import { randomUUID } from "node:crypto";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

export { createDemoDuckDbConfig, demoDuckDbPath } from "./demo-duckdb.js";

export type {
  AdapterExecutionInput,
  AdapterPreviewInput,
  AdapterSqlInput,
  ConfigurableParam,
  DataGateway,
  DataGatewayPolicy,
  DataSourceAdapter,
  DataSourceRuntimePolicy,
  DataSourceType,
  InspectSchemaInput,
  ListDataSourcesInput,
  PreviewTableInput,
  RegisterDataSourceInput,
  RunSqlReadonlyInput,
  SchemaSummary,
  SqlExecutionResult,
  SupportedDataSourceType,
  TableResult,
  TestConnectInput
} from "./types.js";

const DEFAULT_DATA_GATEWAY_POLICY: DataGatewayPolicy = {
  defaultLimit: 100,
  maxLimit: 1000,
  timeoutMs: 10000
};

const sqlDialectForDataSourceType = (type: DataSourceType): string =>
  type === "csv" || type === "xlsx" ? "duckdb" : type;

export class LocalDataGateway implements DataGateway {
  private readonly artifactService: LocalArtifactService;

  constructor(
    private readonly metadataStore: MetadataStore,
    private readonly policy: DataGatewayPolicy = DEFAULT_DATA_GATEWAY_POLICY,
    fileAssetService?: FileAssetService
  ) {
    this.artifactService = new LocalArtifactService(metadataStore, fileAssetService);
  }

  async listDataSources(input: ListDataSourcesInput): Promise<DataSourceSummary[]> {
    return this.metadataStore.dataSources.list(input).map(dataSourceRecordToSummary);
  }

  async supportTypes(): Promise<SupportedDataSourceType[]> {
    return SUPPORTED_DATA_SOURCE_TYPES;
  }

  async registerDataSource(input: RegisterDataSourceInput): Promise<DataSourceSummary> {
    const record = this.metadataStore.dataSources.create({
      user_id: input.user_id,
      id: input.id,
      name: input.name,
      type: input.type,
      config: input.config,
      ...(input.description ? { description: input.description } : {})
    });

    return dataSourceRecordToSummary(record);
  }

  async testConnect(input: TestConnectInput): Promise<{ ok: boolean; message: string }> {
    throwIfAborted(input.signal);
    const dataSource = this.metadataStore.dataSources.get(input);
    const adapter = this.createAdapter(dataSource, input.workspace_id);
    await adapter.inspectSchema({ signal: input.signal });
    this.metadataStore.dataSources.touchTest({
      user_id: input.user_id,
      datasource_id: input.datasource_id,
      status: "ready"
    });

    return { ok: true, message: "Connection test passed." };
  }

  async inspectSchema(input: InspectSchemaInput): Promise<SchemaSummary> {
    throwIfAborted(input.signal);
    const dataSource = this.metadataStore.dataSources.get(input);
    const adapter = this.createAdapter(dataSource, input.workspace_id);
    const resourcePolicy = dataSourcePolicy(dataSource);
    const schema = await adapter.inspectSchema({ signal: input.signal });
    const tableNames = allowedTableSet(input.table_names, resourcePolicy.tableAllowlist);

    return {
      datasource_id: input.datasource_id,
      dialect: sqlDialectForDataSourceType(dataSource.type),
      tables: tableNames.size > 0 ? schema.tables.filter((table) => tableNames.has(table.name)) : schema.tables
    };
  }

  async previewTable(input: PreviewTableInput): Promise<TableResult> {
    throwIfAborted(input.signal);
    const dataSource = this.metadataStore.dataSources.get(input);
    const adapter = this.createAdapter(dataSource);
    const resourcePolicy = dataSourcePolicy(dataSource);
    assertTableAllowed(input.table, resourcePolicy);
    if (resourcePolicy.allowSample === false) {
      throw new Error(`SAMPLE_BLOCKED:${input.datasource_id}:${input.table}`);
    }
    const result = await adapter.previewTable({
      table: input.table,
      limit: Math.min(
        input.limit ?? Math.min(20, this.policy.defaultLimit),
        this.policy.maxLimit,
        resourcePolicy.maxRows ?? this.policy.maxLimit,
        resourcePolicy.maxSampleRows ?? this.policy.maxLimit
      ),
      signal: input.signal
    });
    return maskTableResult(result, resourcePolicy.maskFields);
  }

  async runSqlReadonly(input: RunSqlReadonlyInput): Promise<SqlExecutionResult> {
    throwIfAborted(input.signal);
    const startedAt = Date.now();
    const auditLogId = randomUUID();
    let auditLogCreated = false;
    const dataSource = this.metadataStore.dataSources.get(input);
    const guard = guardReadonlySql(input.sql);

    if (!guard.allowed) {
      this.metadataStore.sqlAuditLogs.create({
        user_id: input.user_id,
        id: auditLogId,
        datasource_id: input.datasource_id,
        sql_text: input.sql,
        status: "blocked",
        blocked_reason: guard.reason,
        ...(input.run_id ? { run_id: input.run_id } : {}),
        elapsed_ms: Date.now() - startedAt
      });
      auditLogCreated = true;
      throw new Error(`SQL_BLOCKED: ${guard.reason}`);
    }

    const resourcePolicy = dataSourcePolicy(dataSource);
    assertSqlTablesAllowed(guard.normalized_sql, resourcePolicy);
    const limit = Math.min(
      input.limit ?? this.policy.defaultLimit,
      this.policy.maxLimit,
      resourcePolicy.maxRows ?? this.policy.maxLimit
    );
    const timeoutMs = Math.min(
      input.timeout_ms ?? this.policy.timeoutMs,
      this.policy.timeoutMs,
      resourcePolicy.timeoutMs ?? this.policy.timeoutMs
    );

    try {
      const workspaceId = input.workspace_id ?? this.policy.workspaceId ?? "default";
      const adapter = this.createAdapter(dataSource, workspaceId);
      const result = await withTimeout(
        adapter.runSqlReadonly({
          sql: guard.normalized_sql,
          limit,
          signal: input.signal
        }),
        timeoutMs,
        input.signal
      );
      const maskedResult = normalizeTableResult(maskTableResult(result, resourcePolicy.maskFields));
      const elapsedMs = Date.now() - startedAt;
      const audit = this.metadataStore.sqlAuditLogs.create({
        user_id: input.user_id,
        id: auditLogId,
        datasource_id: input.datasource_id,
        sql_text: guard.normalized_sql,
        status: "succeeded",
        row_count: maskedResult.row_count,
        elapsed_ms: elapsedMs,
        ...(input.run_id ? { run_id: input.run_id } : {})
      });
      auditLogCreated = true;
      const run = input.run_id
        ? this.metadataStore.runs.get({ user_id: input.user_id, run_id: input.run_id })
        : undefined;
      const artifact = run ? await this.createSqlResultArtifact({
        auditId: audit.id,
        correlation: input.correlation,
        datasourceId: input.datasource_id,
        result: maskedResult,
        run,
        userId: input.user_id,
        workspaceId
      }) : undefined;
      if (run) {
        this.metadataStore.queryHistory.create({
          user_id: input.user_id,
          workspace_id: workspaceId,
          session_id: run.session_id,
          run_id: run.id,
          datasource_id: input.datasource_id,
          sql_text: guard.normalized_sql,
          row_count: maskedResult.row_count,
          elapsed_ms: elapsedMs
        });
      }

      return {
        ...maskedResult,
        audit_log_id: audit.id,
        elapsed_ms: elapsedMs,
        ...(artifact ? { artifact_id: artifact.id, artifact } : {})
      };
    } catch (error) {
      const elapsedMs = Date.now() - startedAt;
      const isTimeout = error instanceof Error && error.message === "SQL_TIMEOUT";
      const isCancelled = error instanceof Error && isAbortError(error);
      if (!auditLogCreated) {
        this.metadataStore.sqlAuditLogs.create({
          user_id: input.user_id,
          id: auditLogId,
          datasource_id: input.datasource_id,
          sql_text: guard.normalized_sql,
          status: isCancelled ? "canceled" : isTimeout ? "timeout" : "failed",
          blocked_reason: error instanceof Error ? error.message : "Unknown SQL execution error",
          elapsed_ms: elapsedMs,
          ...(input.run_id ? { run_id: input.run_id } : {})
        });
      }
      throw error;
    }
  }

  async createArtifact(input: CreateArtifactInput): Promise<ArtifactSummary> {
    return this.artifactService.createArtifact(input);
  }

  private async createSqlResultArtifact(input: {
    auditId: string;
    correlation?: RunSqlReadonlyInput["correlation"];
    datasourceId: string;
    result: TableResult;
    run: { id: string; session_id: string };
    userId: string;
    workspaceId?: string;
  }): Promise<ArtifactSummary> {
    const metadata = {
      audit_log_id: input.auditId,
      datasource_id: input.datasourceId,
      full_result: true,
      row_count: input.result.row_count,
      ...(input.correlation?.tool_call_id ? { tool_call_id: input.correlation.tool_call_id } : {}),
      ...(input.correlation?.step_id ? { step_id: input.correlation.step_id } : {})
    };
    const name = `SQL result ${input.auditId}.csv`;
    try {
      const path = writeSqlResultCsv(input.auditId, input.result);
      return await this.artifactService.createArtifactFromFile({
        user_id: input.userId,
        workspace_id: input.workspaceId ?? this.policy.workspaceId ?? "default",
        session_id: input.run.session_id,
        run_id: input.run.id,
        type: "table",
        name,
        source_path: path,
        preview_json: input.result,
        metadata
      });
    } catch {
      return this.artifactService.createArtifact({
        user_id: input.userId,
        session_id: input.run.session_id,
        run_id: input.run.id,
        type: "table",
        name,
        preview_json: input.result,
        metadata_json: { ...metadata, full_result: false }
      });
    }
  }

  private createAdapter(dataSource: DataSourceRecord, workspaceId = this.policy.workspaceId ?? "default"): DataSourceAdapter {
    const credentials = dataSource.credential_ref
      ? this.metadataStore.secrets.get({
          ref: dataSource.credential_ref,
          workspace_id: workspaceId,
          user_id: dataSource.user_id
        })
      : {};
    const resourcePolicy = dataSourcePolicy(dataSource);
    return createAdapter(dataSource, credentials, {
      timeoutMs: Math.min(this.policy.timeoutMs, resourcePolicy.timeoutMs ?? this.policy.timeoutMs)
    });
  }
}

const DATA_SOURCE_ADAPTER_REGISTRY = createAdapterRegistry({
  access: (config) => new AccessAdapter(config),
  bigquery: (config) => new BigQueryAdapter(config),
  clickhouse: (config) => new ClickHouseAdapter(config),
  csv: (config) => new CsvAdapter(config),
  databricks: (config) => new DatabricksSqlAdapter(config),
  doris: (config) => new DorisAdapter(config),
  duckdb: (config) => new DuckDbAdapter(config),
  elasticsearch: (config) => new ElasticsearchAdapter(config),
  gaussdb: (config) => new GaussDbAdapter(config),
  greenplum: (config) => new GreenplumAdapter(config),
  mariadb: (config) => new MariaDbAdapter(config),
  mongodb: (config) => new MongoDbAdapter(config),
  mysql: (config) => new MySqlAdapter(config),
  oceanbase: (config) => new OceanBaseAdapter(config),
  opensearch: (config) => new OpenSearchAdapter(config),
  oracle: (config) => new OracleAdapter(config),
  postgresql: (config) => new PostgreSqlAdapter(config),
  presto: (config) => new PrestoAdapter(config),
  redis: (config) => new RedisAdapter(config),
  redshift: (config) => new RedshiftAdapter(config),
  snowflake: (config) => new SnowflakeAdapter(config),
  spark: (config) => new SparkSqlAdapter(config),
  sqlite: (config) => new SQLiteAdapter(config),
  sqlserver: (config) => new SqlServerAdapter(config),
  starrocks: (config) => new StarRocksAdapter(config),
  tidb: (config) => new TiDbAdapter(config),
  trino: (config) => new TrinoAdapter(config),
  xlsx: (config) => new XlsxAdapter(config)
});

const createAdapter = (
  dataSource: DataSourceRecord,
  credentials: Record<string, unknown> = {},
  effectivePolicy: { timeoutMs?: number } = {}
): DataSourceAdapter => {
  const config = { ...parseConfig(dataSource), ...credentials, ...effectivePolicy };
  return createRegisteredAdapter(DATA_SOURCE_ADAPTER_REGISTRY, dataSource.type, config);
};

const dataSourceRecordToSummary = (record: DataSourceRecord): DataSourceSummary => ({
  id: record.id,
  name: record.name,
  type: record.type,
  status: record.status === "deleted" ? "disabled" : record.status,
  ...(record.description ? { description: record.description } : {})
});

const parseConfig = (dataSource: DataSourceRecord): Record<string, unknown> =>
  JSON.parse(dataSource.config_json) as Record<string, unknown>;

const dataSourcePolicy = (dataSource: DataSourceRecord): DataSourceRuntimePolicy => {
  const config = parseConfig(dataSource);
  const queryPolicy = isRecord(config.queryPolicy) ? config.queryPolicy : {};
  const introspectionPolicy = isRecord(config.introspection) ? config.introspection : {};
  const samplePolicy = isRecord(config.samplePolicy) ? config.samplePolicy : {};
  const maxRows = positiveNumber(queryPolicy.maxRows);
  const timeoutMs = positiveNumber(queryPolicy.timeoutMs);
  const maxSampleRows = positiveNumber(samplePolicy.maxSampleRows);
  return {
    ...(typeof samplePolicy.allowSample === "boolean" ? { allowSample: samplePolicy.allowSample } : {}),
    ...(maxRows !== undefined ? { maxRows } : {}),
    ...(maxSampleRows !== undefined ? { maxSampleRows } : {}),
    maskFields: stringArray(config.maskFields),
    tableAllowlist: stringArray(introspectionPolicy.tableAllowlist),
    ...(timeoutMs !== undefined ? { timeoutMs } : {})
  };
};

const allowedTableSet = (requestedTables: string[] | undefined, policyTables: string[]): Set<string> => {
  const requested = requestedTables ?? [];
  if (requested.length > 0 && policyTables.length > 0) {
    return new Set(requested.filter((table) => policyTables.includes(table)));
  }
  return new Set(requested.length > 0 ? requested : policyTables);
};

const assertTableAllowed = (table: string, policy: DataSourceRuntimePolicy): void => {
  if (policy.tableAllowlist.length > 0 && !policy.tableAllowlist.includes(table)) {
    throw new Error(`TABLE_NOT_ALLOWED:${table}`);
  }
};

const assertSqlTablesAllowed = (sql: string, policy: DataSourceRuntimePolicy): void => {
  if (policy.tableAllowlist.length === 0) {
    return;
  }
  const tableNames = extractSqlTableNames(sql);
  const blocked = tableNames.filter((table) => !policy.tableAllowlist.includes(table));
  if (blocked.length > 0) {
    throw new Error(`TABLE_NOT_ALLOWED:${blocked.join(",")}`);
  }
};

const extractSqlTableNames = (sql: string): string[] => {
  const stripped = stripQuotedSql(sql);
  const names = new Set<string>();
  const pattern = /\b(?:FROM|JOIN)\s+((?:"[^"]+"|`[^`]+`|[\w-]+)(?:\s*\.\s*(?:"[^"]+"|`[^`]+`|[\w-]+))?)/giu;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(stripped)) !== null) {
    const rawName = match[1];
    if (rawName) {
      names.add(unqualifiedTableName(rawName));
    }
  }
  return [...names];
};

const unqualifiedTableName = (value: string): string => {
  const segment = value.split(".").at(-1) ?? value;
  return unquoteIdentifier(segment.trim().replace(/^`|`$/gu, ""));
};

const maskTableResult = (result: TableResult, maskFields: string[]): TableResult => {
  if (maskFields.length === 0) {
    return result;
  }
  const maskedColumnNames = new Set(maskFields.map((field) => field.toLowerCase()));
  const maskedIndexes = result.columns
    .map((column, index) => ({ column, index }))
    .filter(({ column }) => maskedColumnNames.has(column.toLowerCase()))
    .map(({ index }) => index);
  if (maskedIndexes.length === 0) {
    return result;
  }
  return {
    ...result,
    rows: result.rows.map((row) =>
      row.map((value, index) => maskedIndexes.includes(index) && value !== null ? "[MASKED]" : value))
  };
};

const normalizeTableResult = (result: TableResult): TableResult => ({
  ...result,
  rows: result.rows.map((row) => row.map(jsonSafeValue))
});

const jsonSafeValue = (value: unknown): unknown => {
  if (value === null || value === undefined) {
    return null;
  }
  if (typeof value === "bigint") {
    const numberValue = Number(value);
    return Number.isSafeInteger(numberValue) ? numberValue : value.toString();
  }
  if (value instanceof Date) {
    return value.toISOString();
  }
  if (Array.isArray(value)) {
    return value.map(jsonSafeValue);
  }
  if (typeof value === "object") {
    const jsonValue = value as { toJSON?: () => unknown };
    if (typeof jsonValue.toJSON === "function") {
      return jsonSafeValue(jsonValue.toJSON());
    }
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>).map(([key, item]) => [key, jsonSafeValue(item)])
    );
  }
  return value;
};

const stringArray = (value: unknown): string[] =>
  Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string" && item.trim().length > 0)
      .map((item) => item.trim())
    : [];

const positiveNumber = (value: unknown): number | undefined =>
  typeof value === "number" && Number.isFinite(value) && value > 0 ? value : undefined;

const writeSqlResultCsv = (auditId: string, result: TableResult): string => {
  const root = process.env.SQL_RESULT_EXPORT_ROOT ?? join(process.env.STORAGE_ROOT_DIR ?? "storage", "sql-results");
  mkdirSync(root, { recursive: true });
  const path = join(root, `${auditId}.csv`);
  const escape = (value: unknown): string => {
    const text = value === null || value === undefined ? "" : String(value);
    return /[",\n\r]/u.test(text) ? `"${text.replace(/"/gu, '""')}"` : text;
  };
  const lines = [
    result.columns.map(escape).join(","),
    ...result.rows.map((row) => row.map(escape).join(","))
  ];
  writeFileSync(path, `${lines.join("\n")}\n`, "utf8");
  return path;
};

const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null;

const withTimeout = async <T>(
  promise: Promise<T>,
  timeoutMs: number,
  signal?: AbortSignal | undefined
): Promise<T> => {
  throwIfAborted(signal);
  let timer: ReturnType<typeof setTimeout> | undefined;
  let abortListener: (() => void) | undefined;
  try {
    const timeout = new Promise<T>((_, reject) => {
      timer = setTimeout(() => reject(new Error("SQL_TIMEOUT")), timeoutMs);
    });
    const aborted = signal
      ? new Promise<T>((_, reject) => {
          abortListener = () => reject(signal.reason instanceof Error ? signal.reason : new Error("RUN_CANCELLED"));
          signal.addEventListener("abort", abortListener, { once: true });
        })
      : undefined;
    return await Promise.race(aborted ? [promise, timeout, aborted] : [promise, timeout]);
  } finally {
    if (timer) {
      clearTimeout(timer);
    }
    if (signal && abortListener) {
      signal.removeEventListener("abort", abortListener);
    }
  }
};

const throwIfAborted = (signal?: AbortSignal | undefined): void => {
  if (signal?.aborted) {
    throw signal.reason instanceof Error ? signal.reason : new Error("RUN_CANCELLED");
  }
};

const isAbortError = (error: Error): boolean =>
  error.name === "AbortError"
  || error.message === "RUN_CANCELLED"
  || error.message.startsWith("RUN_CANCELLED")
  || error.message.startsWith("RUN_TIMEOUT:")
  || error.message === "RUN_SUBSCRIBER_CLOSED";

const unquoteIdentifier = (identifier: string): string =>
  identifier.replace(/^"/u, "").replace(/"$/u, "").replaceAll('""', '"');
