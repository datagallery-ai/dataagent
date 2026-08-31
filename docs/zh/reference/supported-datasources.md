# 支持的数据源

这篇文档面向准备接入数据源的用户。读完后，你可以选择试用路径，查看支持类型，并知道哪些字段会作为凭据处理。

DataFoundry 通过 Data Gateway 管理数据源。客户端负责注册、测试和选择；后端负责真实连接、schema 检查、只读查询、安全策略和审计。

## 推荐试用路径

| 阶段 | 类型 | 适合场景 |
| --- | --- | --- |
| 第一次试用 | 内置 DTC Growth Review（SQLite） | 不准备数据库，直接跑通分析链路。 |
| 本地文件 | SQLite、CSV、Excel、DuckDB file | 验证文件分析、上传和表格产出。 |
| 常见服务端数据库 | PostgreSQL、MySQL | 验证真实连接、schema 抓取和只读 SQL。 |
| 外部服务 | 云数仓、搜索、NoSQL、湖仓 | 需要服务、网络、账号和凭据。 |

## 类型与字段

`GET /api/v1/datasource-types` 返回后端支持的类型、字段和是否启用。前端应按接口返回值渲染表单。

| 类型 | 字段 | 凭据字段 | 说明 |
| --- | --- | --- | --- |
| `duckdb` | `mode`, `path` | 无 | `mode=demo` 使用内置数据；`mode=file` 指向 DuckDB 文件。 |
| `sqlite` | `path` | 无 | 本地 SQLite 文件。 |
| `csv` | `file_path` | 无 | CSV 文件，作为表格数据源。 |
| `xlsx` | `file_path` | 无 | Excel 文件。 |
| `postgresql` | `host`, `port`, `database`, `schema`, `username`, `password` | `password` | PostgreSQL 只读连接。 |
| `mysql` | `host`, `port`, `database`, `schema`, `username`, `password` | `password` | MySQL 只读连接。 |
| `clickhouse` | `host`, `port`, `database`, `username`, `password`, `secure` | `password` | ClickHouse HTTP JSON 接口。 |
| `snowflake` | `account`, `warehouse`, `database`, `schema`, `role`, `username`, `password` | `password` | Snowflake 数据仓库。 |
| `bigquery` | `projectId`, `dataset`, `location`, `credentialsJson`, `keyFilename` | `credentialsJson` | BigQuery 数据仓库。 |
| `sqlserver` | `host`, `port`, `database`, `schema`, `username`, `password` | `password` | Microsoft SQL Server。 |
| `oracle` | `connectString`, `schema`, `username`, `password` | `password` | Oracle Database。 |
| `mongodb` | `uri`, `database`, `sampleSize` | `uri` | collection 映射为 table-like 对象。 |
| `gaussdb` | `host`, `port`, `database`, `schema`, `username`, `password` | `password` | GaussDB PostgreSQL-compatible。 |
| `access` | `connectionString`, `path` | `connectionString` | Microsoft Access over ODBC。 |
| `redis` | `url`, `keyPattern`, `database` | `url` | keyspace 映射为 `redis_keys` 伪表。 |
| `starrocks` | `host`, `port`, `database`, `schema`, `username`, `password` | `password` | StarRocks MySQL-compatible。 |
| `trino` | `host`, `port`, `catalog`, `schema`, `username`, `password`, `secure` | `password` | Trino REST API。 |
| `presto` | `host`, `port`, `catalog`, `schema`, `username`, `password`, `secure` | `password` | Presto REST API。 |
| `spark` | `host`, `port`, `catalog`, `schema`, `transport`, `auth`, `username`, `password` | `password` | Spark Thrift Server。 |
| `databricks` | `host`, `path`, `warehouseId`, `token`, `catalog`, `schema` | `token` | Databricks SQL Warehouse。 |
| `redshift` | `host`, `port`, `database`, `schema`, `username`, `password` | `password` | Amazon Redshift。 |
| `elasticsearch` | `node`, `url`, `indexPattern`, `username`, `password`, `apiKey` | `node`, `url`, `password`, `apiKey` | index 映射为 table-like 对象。 |
| `opensearch` | `node`, `url`, `indexPattern`, `username`, `password`, `apiKey` | `node`, `url`, `password`, `apiKey` | index 映射为 table-like 对象。 |
| `doris` | `host`, `port`, `database`, `schema`, `username`, `password` | `password` | Apache Doris。 |
| `mariadb` | `host`, `port`, `database`, `schema`, `username`, `password` | `password` | MariaDB。 |
| `tidb` | `host`, `port`, `database`, `schema`, `username`, `password` | `password` | TiDB。 |
| `oceanbase` | `host`, `port`, `database`, `schema`, `username`, `password` | `password` | OceanBase。 |
| `greenplum` | `host`, `port`, `database`, `schema`, `username`, `password` | `password` | Greenplum。 |

## 发现类型 schema

```bash
curl http://127.0.0.1:8787/api/v1/datasource-types
```

响应里的 `parameters[]` 会标出字段名、类型、必填状态、默认值和选项。

## 凭据边界

- 创建或更新数据源时可以提交凭据。
- 读接口只返回 `secretRef`、`hasSecret` 或同等标记。
- Agent run 只接收数据源 ID 和选择信息。
- SQL 执行由 Data Gateway 做只读限制、超时、行数限制和审计。

## 非 SQL 类型

MongoDB、Redis、Elasticsearch 和 OpenSearch 不暴露原生命令或 DSL。Data Gateway 会把 collection、keyspace 或 index 映射成受限的 table-like 对象，让 Agent 走统一工具边界。

## 延伸阅读

- 接入步骤：[数据源指南](../guides/data-sources.md)
- 配置接口：[配置 API 参考](configuration-api.md)
- REST 端点：[REST API 参考](rest-api.md)
