# Data sources guide

This guide is for users preparing to connect data sources. After reading it, you can register, test, and select sources in Web or REST API, and understand how the TUI uses configured sources.

DataFoundry manages data sources through Data Gateway. Sources can be registered and tested in the Web workbench or REST API; the TUI can list and select configured sources. Analysis still runs through controlled agent tools.

## Core principles

- No arbitrary SQL REST passthrough.
- The agent must inspect schema before read-only queries.
- SQL execution goes through guard, limits, timeouts, allowlists, masking, and audit.
- Credentials are submitted only on create or update; read APIs return `hasSecret` or `secretRef`, not plaintext.
- Prefer read-only accounts or test databases for first integration.

## Typical path

```text
Discover supported data source types
  -> Register a data source
  -> Test connection
  -> Fetch schema
  -> Select source in Web, TUI, or agent run
  -> Agent runs read-only analysis
```

## In the Web workbench

1. Open the Web workbench.
2. Click **Data sources** on the left.
3. Click **Add data source**.
4. Choose a type and fill connection details.
5. Click **Test connection**.
6. Run schema inspection and confirm tables and fields are visible.
7. Return to the data task and confirm the source is enabled below the input box.

For a first run, use the built-in DTC Growth Review—no custom database required.

## In the TUI

The TUI currently supports listing and selecting configured sources:

```text
/datasource
/datasource list
/datasource current
/datasource select <id>
/datasource <id>
```

Create, test, and schema fetch use the Web workbench or REST API. The TUI writes the selected source into `run_config` for the run.

Check session state:

```text
/status
```

## Via API

Discover supported types:

```bash
curl http://127.0.0.1:8787/api/v1/datasource-types
```

Register PostgreSQL example:

```bash
curl -X POST http://127.0.0.1:8787/api/v1/datasources \
  -H "Content-Type: application/json" \
  -d '{
    "id": "sales-pg",
    "name": "Sales PostgreSQL",
    "type": "postgresql",
    "config": {
      "host": "127.0.0.1",
      "port": 5432,
      "database": "sales",
      "schema": "public",
      "username": "readonly"
    },
    "credentials": {
      "password": "replace-with-your-key"
    },
    "queryPolicy": {
      "maxRows": 1000,
      "timeoutMs": 10000,
      "denyWrite": true
    },
    "defaultEnabled": true
  }'
```

After registration, test and fetch schema:

```bash
curl -X POST http://127.0.0.1:8787/api/v1/datasources/sales-pg/test
curl -X POST http://127.0.0.1:8787/api/v1/datasources/sales-pg/introspect
curl http://127.0.0.1:8787/api/v1/datasources/sales-pg/schema
```

## Selecting a source in an agent run

Web and TUI pass selected sources to the backend. Prefer specifying via `run_config`:

```json
{
  "forwardedProps": {
    "run_config": {
      "activeDatasourceId": "sales-pg",
      "enabledDatasourceIds": ["sales-pg"]
    }
  }
}
```

The backend injects the selected source into run context. The agent can access it through tools but does not receive credential plaintext.

## Supported data source types

| Type | Notes |
| --- | --- |
| `duckdb` | Built-in demo or DuckDB file. |
| `sqlite` | Local SQLite file. |
| `csv` | Local or uploaded CSV; treated as a single table by default. |
| `xlsx` | Excel file; first sheet by default. |
| `postgresql` | PostgreSQL database. |
| `mysql` | MySQL database. |
| `clickhouse` | ClickHouse HTTP JSON interface. |
| `snowflake` | Snowflake warehouse. |
| `bigquery` | BigQuery dataset. |
| `sqlserver` | SQL Server database. |
| `oracle` | Oracle database. |
| `mongodb` | MongoDB collections as table-like objects in the tool boundary. |
| `redis` | Redis keys as pseudo-tables in the tool boundary. |
| `gaussdb` | GaussDB PostgreSQL-compatible. |
| `access` | Microsoft Access over ODBC. |
| `starrocks` | StarRocks MySQL-compatible. |
| `trino` / `presto` | Trino or Presto REST API. |
| `spark` | Spark Thrift Server / HiveServer2. |
| `databricks` | Databricks SQL Warehouse. |
| `elasticsearch` / `opensearch` | Index-as-table query boundary. |
| `redshift` | Amazon Redshift PostgreSQL-compatible. |
| `doris` | Apache Doris MySQL-compatible. |
| `mariadb` | MariaDB MySQL-compatible. |
| `tidb` | TiDB. |
| `oceanbase` | OceanBase MySQL-compatible. |
| `greenplum` | Greenplum PostgreSQL-compatible. |

External services need reachable hosts, network access, and credentials. For public demos, prefer the built-in DTC Growth Review, or configure DuckDB, SQLite, CSV / Excel, PostgreSQL, or MySQL.

## Non-SQL source boundaries

MongoDB, Redis, Elasticsearch, and OpenSearch are not SQL databases. The system maps them into a restricted table-like boundary:

| Type | Mapping | Query boundary |
| --- | --- | --- |
| MongoDB | collection -> table | Simple `SELECT * FROM collection LIMIT n`; no complex aggregation. |
| Redis | `redis_keys` pseudo-table | Simple key list queries. |
| Elasticsearch / OpenSearch | index -> table | Simple `SELECT * FROM index LIMIT n`; schema from mapping. |

Complex Mongo aggregation, Redis commands, and Elasticsearch DSL are not exposed to the agent through current `run_sql_readonly`.

## Security recommendations

- Use read-only accounts.
- Set reasonable `maxRows` and `timeoutMs`.
- Configure `maskFields` for email, phone, ID numbers, and similar fields.
- Use allowlists for sensitive tables.
- Do not paste database passwords, tokens, or private keys into question text or AG-UI payloads.
