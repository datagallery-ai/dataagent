import type { ConfigurableParam, SupportedDataSourceType } from "./types.js";

export const SUPPORTED_DATA_SOURCE_TYPES: SupportedDataSourceType[] = [
  {
    name: "duckdb",
    enabled: true,
    label: "DuckDB",
    description: "Local DuckDB database file.",
    parameters: [
      { name: "path", label: "Database Path", type: "file", required: true }
    ]
  },
  {
    name: "sqlite",
    enabled: true,
    label: "SQLite",
    description: "Local SQLite database file.",
    parameters: [{ name: "path", label: "Database Path", type: "file", required: true }]
  },
  {
    name: "csv",
    enabled: true,
    label: "CSV",
    description: "Uploaded or local CSV dataset.",
    parameters: [{ name: "file_path", label: "CSV File Path", type: "file", required: true }]
  },
  {
    name: "xlsx",
    enabled: true,
    label: "XLSX",
    description: "Uploaded or local Excel workbook.",
    parameters: [{ name: "file_path", label: "XLSX File Path", type: "file", required: true }]
  },
  {
    name: "postgresql",
    enabled: true,
    label: "PostgreSQL",
    description: "PostgreSQL read-only datasource.",
    parameters: serverDatabaseParameters(5432)
  },
  {
    name: "mysql",
    enabled: true,
    label: "MySQL",
    description: "MySQL read-only datasource.",
    parameters: serverDatabaseParameters(3306)
  },
  {
    name: "clickhouse",
    enabled: true,
    label: "ClickHouse",
    description: "ClickHouse read-only datasource over the HTTP JSON interface.",
    parameters: [
      { name: "host", label: "Host", type: "string", required: true },
      { name: "port", label: "Port", type: "number", required: true, default_value: 8123 },
      { name: "database", label: "Database", type: "string", required: true },
      { name: "username", label: "Username", type: "string", required: false, default_value: "default" },
      { name: "password", label: "Password", type: "password", required: false },
      { name: "secure", label: "Use HTTPS", type: "boolean", required: false, default_value: false }
    ]
  },
  {
    name: "snowflake",
    enabled: true,
    label: "Snowflake",
    description: "Snowflake read-only datasource.",
    parameters: [
      { name: "account", label: "Account", type: "string", required: true },
      { name: "warehouse", label: "Warehouse", type: "string", required: true },
      { name: "database", label: "Database", type: "string", required: true },
      { name: "schema", label: "Schema", type: "string", required: false, default_value: "PUBLIC" },
      { name: "role", label: "Role", type: "string", required: false },
      { name: "username", label: "Username", type: "string", required: true },
      { name: "password", label: "Password", type: "password", required: true }
    ]
  },
  {
    name: "bigquery",
    enabled: true,
    label: "BigQuery",
    description: "Google BigQuery read-only datasource.",
    parameters: [
      { name: "projectId", label: "Project ID", type: "string", required: true },
      { name: "dataset", label: "Dataset", type: "string", required: true },
      { name: "location", label: "Location", type: "string", required: false },
      { name: "credentialsJson", label: "Credentials JSON", type: "password", required: false },
      { name: "keyFilename", label: "Key File", type: "file", required: false }
    ]
  },
  {
    name: "sqlserver",
    enabled: true,
    label: "SQL Server",
    description: "Microsoft SQL Server read-only datasource.",
    parameters: serverDatabaseParameters(1433)
  },
  {
    name: "oracle",
    enabled: true,
    label: "Oracle",
    description: "Oracle Database read-only datasource.",
    parameters: [
      { name: "connectString", label: "Connect String", type: "string", required: true },
      { name: "schema", label: "Schema", type: "string", required: false },
      { name: "username", label: "Username", type: "string", required: true },
      { name: "password", label: "Password", type: "password", required: true }
    ]
  },
  {
    name: "mongodb",
    enabled: true,
    label: "MongoDB",
    description: "MongoDB read-only datasource with simple SELECT-to-find mapping.",
    parameters: [
      { name: "uri", label: "URI", type: "password", required: true },
      { name: "database", label: "Database", type: "string", required: true },
      { name: "sampleSize", label: "Schema Sample Size", type: "number", required: false, default_value: 20 }
    ]
  },
  {
    name: "gaussdb",
    enabled: true,
    label: "GaussDB",
    description: "GaussDB PostgreSQL-compatible read-only datasource.",
    parameters: serverDatabaseParameters(5432)
  },
  {
    name: "access",
    enabled: true,
    label: "Microsoft Access",
    description: "Microsoft Access read-only datasource over ODBC.",
    parameters: [
      { name: "connectionString", label: "ODBC Connection String", type: "password", required: false },
      { name: "path", label: "Access File Path", type: "file", required: false }
    ]
  },
  {
    name: "redis",
    enabled: true,
    label: "Redis",
    description: "Redis read-only keyspace datasource exposed as the redis_keys pseudo table.",
    parameters: [
      { name: "url", label: "URL", type: "password", required: true },
      { name: "keyPattern", label: "Key Pattern", type: "string", required: false, default_value: "*" },
      { name: "database", label: "Database", type: "number", required: false, default_value: 0 }
    ]
  },
  {
    name: "starrocks",
    enabled: true,
    label: "StarRocks",
    description: "StarRocks MySQL-compatible read-only datasource.",
    parameters: serverDatabaseParameters(9030)
  },
  {
    name: "trino",
    enabled: true,
    label: "Trino",
    description: "Trino read-only datasource over the Trino REST API.",
    parameters: trinoLikeParameters(8080)
  },
  {
    name: "presto",
    enabled: true,
    label: "Presto",
    description: "Presto read-only datasource over the Presto REST API.",
    parameters: trinoLikeParameters(8080)
  },
  {
    name: "spark",
    enabled: true,
    label: "Spark SQL",
    description: "Spark Thrift Server read-only datasource over HiveServer2 protocol.",
    parameters: [
      { name: "host", label: "Host", type: "string", required: true },
      { name: "port", label: "Port", type: "number", required: true, default_value: 10000 },
      { name: "catalog", label: "Catalog", type: "string", required: false },
      { name: "schema", label: "Schema", type: "string", required: false, default_value: "default" },
      { name: "transport", label: "Transport", type: "select", required: false, options: ["tcp", "http"] },
      { name: "auth", label: "Auth", type: "select", required: false, options: ["none", "plain"] },
      { name: "username", label: "Username", type: "string", required: false },
      { name: "password", label: "Password", type: "password", required: false }
    ]
  },
  {
    name: "databricks",
    enabled: true,
    label: "Databricks SQL",
    description: "Databricks SQL Warehouse read-only datasource.",
    parameters: [
      { name: "host", label: "Host", type: "string", required: true },
      { name: "path", label: "HTTP Path", type: "string", required: true },
      { name: "warehouseId", label: "Warehouse ID", type: "string", required: false },
      { name: "token", label: "Token", type: "password", required: true },
      { name: "catalog", label: "Catalog", type: "string", required: false },
      { name: "schema", label: "Schema", type: "string", required: false }
    ]
  },
  {
    name: "redshift",
    enabled: true,
    label: "Amazon Redshift",
    description: "Amazon Redshift PostgreSQL-compatible read-only datasource.",
    parameters: serverDatabaseParameters(5439)
  },
  {
    name: "elasticsearch",
    enabled: true,
    label: "Elasticsearch",
    description: "Elasticsearch read-only datasource with index-as-table mapping.",
    parameters: searchIndexParameters()
  },
  {
    name: "opensearch",
    enabled: true,
    label: "OpenSearch",
    description: "OpenSearch read-only datasource with index-as-table mapping.",
    parameters: searchIndexParameters()
  },
  {
    name: "doris",
    enabled: true,
    label: "Apache Doris",
    description: "Apache Doris MySQL-compatible read-only datasource.",
    parameters: serverDatabaseParameters(9030)
  },
  {
    name: "mariadb",
    enabled: true,
    label: "MariaDB",
    description: "MariaDB MySQL-compatible read-only datasource.",
    parameters: serverDatabaseParameters(3306)
  },
  {
    name: "tidb",
    enabled: true,
    label: "TiDB",
    description: "TiDB read-only datasource.",
    parameters: serverDatabaseParameters(4000)
  },
  {
    name: "oceanbase",
    enabled: true,
    label: "OceanBase",
    description: "OceanBase MySQL-compatible read-only datasource.",
    parameters: serverDatabaseParameters(2881)
  },
  {
    name: "greenplum",
    enabled: true,
    label: "Greenplum",
    description: "Greenplum PostgreSQL-compatible read-only datasource.",
    parameters: serverDatabaseParameters(5432)
  }
];

function serverDatabaseParameters(defaultPort: number): ConfigurableParam[] {
  return [
    { name: "host", label: "Host", type: "string", required: true },
    { name: "port", label: "Port", type: "number", required: true, default_value: defaultPort },
    { name: "database", label: "Database", type: "string", required: true },
    { name: "schema", label: "Schema", type: "string", required: false },
    { name: "username", label: "Username", type: "string", required: true },
    { name: "password", label: "Password", type: "password", required: true }
  ];
}

function trinoLikeParameters(defaultPort: number): ConfigurableParam[] {
  return [
    { name: "host", label: "Host", type: "string", required: true },
    { name: "port", label: "Port", type: "number", required: true, default_value: defaultPort },
    { name: "catalog", label: "Catalog", type: "string", required: true },
    { name: "schema", label: "Schema", type: "string", required: false, default_value: "default" },
    { name: "username", label: "Username", type: "string", required: false },
    { name: "password", label: "Password", type: "password", required: false },
    { name: "secure", label: "Use HTTPS", type: "boolean", required: false, default_value: false }
  ];
}

function searchIndexParameters(): ConfigurableParam[] {
  return [
    { name: "node", label: "Node URL", type: "password", required: false },
    { name: "url", label: "Node URL", type: "password", required: false },
    { name: "indexPattern", label: "Index Pattern", type: "string", required: false, default_value: "*" },
    { name: "username", label: "Username", type: "string", required: false },
    { name: "password", label: "Password", type: "password", required: false },
    { name: "apiKey", label: "API Key", type: "password", required: false }
  ];
}
