# Supported data sources

This reference is for users preparing to connect data sources. After reading it, you can choose a trial path, review supported types, and know which fields are treated as credentials.

DataFoundry manages data sources through Data Gateway. Clients register, test, and select sources; the backend handles connections, schema checks, read-only queries, security policy, and audit.

## Recommended trial path

| Stage | Types | Best for |
| --- | --- | --- |
| First trial | Built-in DTC Growth Review (SQLite) | Run the analysis path without preparing a database. |
| Local files | SQLite, CSV, Excel, DuckDB file | File analysis, upload, and table outputs. |
| Common server databases | PostgreSQL, MySQL | Real connections, schema fetch, and read-only SQL. |
| External services | Cloud warehouses, search, NoSQL, lakehouse | Requires service, network, account, and credentials. |

## Types and fields

`GET /api/v1/datasource-types` returns supported types, fields, and enablement. The frontend should render forms from the API response.

| Type | Fields | Credential fields | Notes |
| --- | --- | --- | --- |
| `duckdb` | `mode`, `path` | None | `mode=demo` uses built-in data; `mode=file` points to a DuckDB file. |
| `sqlite` | `path` | None | Local SQLite file. |
| `csv` | `file_path` | None | CSV file as tabular source. |
| `xlsx` | `file_path` | None | Excel file. |
| `postgresql` | `host`, `port`, `database`, `schema`, `username`, `password` | `password` | Read-only PostgreSQL connection. |
| `mysql` | `host`, `port`, `database`, `schema`, `username`, `password` | `password` | Read-only MySQL connection. |
| `clickhouse` | `host`, `port`, `database`, `username`, `password`, `secure` | `password` | ClickHouse HTTP JSON interface. |
| `snowflake` | `account`, `warehouse`, `database`, `schema`, `role`, `username`, `password` | `password` | Snowflake warehouse. |
| `bigquery` | `projectId`, `dataset`, `location`, `credentialsJson`, `keyFilename` | `credentialsJson` | BigQuery warehouse. |
| `sqlserver` | `host`, `port`, `database`, `schema`, `username`, `password` | `password` | Microsoft SQL Server. |
| `oracle` | `connectString`, `schema`, `username`, `password` | `password` | Oracle Database. |
| `mongodb` | `uri`, `database`, `sampleSize` | `uri` | Collections mapped as table-like objects. |
| `gaussdb` | `host`, `port`, `database`, `schema`, `username`, `password` | `password` | GaussDB PostgreSQL-compatible. |
| `access` | `connectionString`, `path` | `connectionString` | Microsoft Access over ODBC. |
| `redis` | `url`, `keyPattern`, `database` | `url` | Keyspace mapped as `redis_keys` pseudo-table. |
| `starrocks` | `host`, `port`, `database`, `schema`, `username`, `password` | `password` | StarRocks MySQL-compatible. |
| `trino` | `host`, `port`, `catalog`, `schema`, `username`, `password`, `secure` | `password` | Trino REST API. |
| `presto` | `host`, `port`, `catalog`, `schema`, `username`, `password`, `secure` | `password` | Presto REST API. |
| `spark` | `host`, `port`, `catalog`, `schema`, `transport`, `auth`, `username`, `password` | `password` | Spark Thrift Server. |
| `databricks` | `host`, `path`, `warehouseId`, `token`, `catalog`, `schema` | `token` | Databricks SQL Warehouse. |
| `redshift` | `host`, `port`, `database`, `schema`, `username`, `password` | `password` | Amazon Redshift. |
| `elasticsearch` | `node`, `url`, `indexPattern`, `username`, `password`, `apiKey` | `node`, `url`, `password`, `apiKey` | Indexes mapped as table-like objects. |
| `opensearch` | `node`, `url`, `indexPattern`, `username`, `password`, `apiKey` | `node`, `url`, `password`, `apiKey` | Indexes mapped as table-like objects. |
| `doris` | `host`, `port`, `database`, `schema`, `username`, `password` | `password` | Apache Doris. |
| `mariadb` | `host`, `port`, `database`, `schema`, `username`, `password` | `password` | MariaDB. |
| `tidb` | `host`, `port`, `database`, `schema`, `username`, `password` | `password` | TiDB. |
| `oceanbase` | `host`, `port`, `database`, `schema`, `username`, `password` | `password` | OceanBase. |
| `greenplum` | `host`, `port`, `database`, `schema`, `username`, `password` | `password` | Greenplum. |

## Discover type schema

```bash
curl http://127.0.0.1:8787/api/v1/datasource-types
```

The response `parameters[]` lists field names, types, required flags, defaults, and options.

## Credential boundaries

- Credentials may be submitted when creating or updating a data source.
- Read APIs return `secretRef`, `hasSecret`, or equivalent markers only.
- Agent runs receive data source IDs and selection info only.
- SQL execution applies read-only limits, timeouts, row limits, and audit through Data Gateway.

## Non-SQL types

MongoDB, Redis, Elasticsearch, and OpenSearch do not expose native commands or DSL. Data Gateway maps collections, keyspaces, or indexes into restricted table-like objects so the agent uses a unified tool boundary.

## Further reading

- Connection steps: [Data sources guide](../guides/data-sources.md)
- Configuration API: [Configuration API reference](configuration-api.md)
- REST endpoints: [REST API reference](rest-api.md)
