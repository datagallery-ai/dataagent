import type { CreateArtifactInput } from "@datafoundry/artifacts";
import type { ArtifactSummary, DataSourceSummary } from "@datafoundry/contracts";

export type DataSourceType =
  | "duckdb"
  | "sqlite"
  | "csv"
  | "xlsx"
  | "postgresql"
  | "mysql"
  | "clickhouse"
  | "snowflake"
  | "bigquery"
  | "sqlserver"
  | "oracle"
  | "mongodb"
  | "gaussdb"
  | "access"
  | "redis"
  | "starrocks"
  | "trino"
  | "presto"
  | "spark"
  | "databricks"
  | "redshift"
  | "elasticsearch"
  | "opensearch"
  | "doris"
  | "mariadb"
  | "tidb"
  | "oceanbase"
  | "greenplum"
  | string;

export type ConfigurableParam = {
  name: string;
  label: string;
  type: "string" | "password" | "select" | "number" | "boolean" | "file";
  required: boolean;
  default_value?: string | number | boolean;
  options?: string[];
};

export type SupportedDataSourceType = {
  name: DataSourceType;
  enabled: boolean;
  label: string;
  description?: string;
  parameters: ConfigurableParam[];
};

export type ListDataSourcesInput = {
  user_id: string;
  enabled_only?: boolean;
};

export type RegisterDataSourceInput = {
  user_id: string;
  id: string;
  name: string;
  type: DataSourceType;
  config: Record<string, unknown>;
  description?: string;
};

export type TestConnectInput = {
  signal?: AbortSignal | undefined;
  user_id: string;
  workspace_id?: string;
  datasource_id: string;
};

export type InspectSchemaInput = {
  signal?: AbortSignal | undefined;
  user_id: string;
  workspace_id?: string;
  datasource_id: string;
  table_names?: string[];
};

export type PreviewTableInput = {
  signal?: AbortSignal | undefined;
  user_id: string;
  workspace_id?: string;
  datasource_id: string;
  table: string;
  limit?: number;
};

export type RunSqlReadonlyInput = {
  signal?: AbortSignal | undefined;
  user_id: string;
  workspace_id?: string;
  datasource_id: string;
  sql: string;
  run_id?: string;
  limit?: number;
  timeout_ms?: number;
  /**
   * Optional correlation handles (R-018). When provided, the produced table artifact
   * records them in `metadata_json` so the frontend Detail view can link the SQL result
   * back to the originating tool_call / step.
   */
  correlation?: {
    tool_call_id?: string;
    step_id?: string;
  };
};

export type SchemaSummary = {
  datasource_id: string;
  dialect?: string;
  tables: Array<{
    name: string;
    columns: Array<{
      name: string;
      type: string;
      nullable?: boolean;
    }>;
  }>;
};

export type TableResult = {
  columns: string[];
  rows: unknown[][];
  row_count: number;
};

export type SqlExecutionResult = TableResult & {
  audit_log_id: string;
  elapsed_ms: number;
  artifact_id?: string;
  artifact?: ArtifactSummary;
};

export interface DataGateway {
  listDataSources(input: ListDataSourcesInput): Promise<DataSourceSummary[]>;
  supportTypes(): Promise<SupportedDataSourceType[]>;
  registerDataSource(input: RegisterDataSourceInput): Promise<DataSourceSummary>;
  testConnect(input: TestConnectInput): Promise<{ ok: boolean; message: string }>;
  inspectSchema(input: InspectSchemaInput): Promise<SchemaSummary>;
  previewTable(input: PreviewTableInput): Promise<TableResult>;
  runSqlReadonly(input: RunSqlReadonlyInput): Promise<SqlExecutionResult>;
  createArtifact(input: CreateArtifactInput): Promise<ArtifactSummary>;
}

export type DataGatewayPolicy = {
  defaultLimit: number;
  maxLimit: number;
  timeoutMs: number;
  workspaceId?: string;
};

export type DataSourceRuntimePolicy = {
  allowSample?: boolean;
  maskFields: string[];
  maxRows?: number;
  maxSampleRows?: number;
  tableAllowlist: string[];
  timeoutMs?: number;
};

export type DataSourceAdapter = {
  inspectSchema(input?: AdapterExecutionInput): Promise<Omit<SchemaSummary, "datasource_id">>;
  previewTable(input: AdapterPreviewInput): Promise<TableResult>;
  runSqlReadonly(input: AdapterSqlInput): Promise<TableResult>;
};

export type AdapterExecutionInput = {
  signal?: AbortSignal | undefined;
};

export type AdapterPreviewInput = AdapterExecutionInput & {
  limit: number;
  table: string;
};

export type AdapterSqlInput = AdapterExecutionInput & {
  limit: number;
  sql: string;
};
