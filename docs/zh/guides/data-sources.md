# 数据源指南

这篇文档面向准备接入数据源的用户。读完后，你可以在 Web 或 REST API 中注册、测试和选择数据源，并理解 TUI 如何使用已配置的数据源。

DataFoundry 通过 Data Gateway 管理数据源。数据源可以在 Web 工作台或 REST API 中注册和测试；TUI 可以列出并选择已配置的数据源。真正的数据分析仍由 Agent 通过受控工具执行。

## 核心原则

- 不暴露任意 SQL REST 直通接口。
- Agent 必须先检查 schema，再执行只读查询。
- SQL 执行经过 guard、limit、timeout、allowlist、mask 和 audit。
- 凭据只在创建或更新时提交，读接口只返回 `hasSecret` 或 `secretRef`，不会回传明文。
- 建议首次接入使用只读账号或测试库。

## 使用路径

典型接入流程如下：

```text
发现可用数据源类型
  -> 注册数据源
  -> 测试连接
  -> 抓取 schema
  -> 在 Web、TUI 或 Agent run 中选择数据源
  -> Agent 执行只读分析
```

## 在 Web 工作台中使用

1. 打开 Web 工作台。
2. 在左侧点击「数据源」。
3. 点击「新增数据源」。
4. 选择类型并填写连接信息。
5. 点击「测试连接」。
6. 执行 schema 检查，确认表和字段可见。
7. 回到数据任务，在输入框底部确认该数据源已启用。

首次体验可以使用内置 DTC Growth Review，不需要添加自有数据库。

## 在 TUI 中使用

TUI 当前支持列出和选择已配置数据源：

```text
/datasource
/datasource list
/datasource current
/datasource select <id>
/datasource <id>
```

新增、测试和 schema 抓取请使用 Web 工作台或 REST API。TUI 会把选中的数据源写入本次 run 的 `run_config`。

查看当前会话状态：

```text
/status
```

## 通过 API 使用

发现支持类型：

```bash
curl http://127.0.0.1:8787/api/v1/datasource-types
```

注册 PostgreSQL 示例：

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

注册后测试并抓取 schema：

```bash
curl -X POST http://127.0.0.1:8787/api/v1/datasources/sales-pg/test
curl -X POST http://127.0.0.1:8787/api/v1/datasources/sales-pg/introspect
curl http://127.0.0.1:8787/api/v1/datasources/sales-pg/schema
```

## 在 Agent run 中选择数据源

Web 和 TUI 会把本次运行选中的数据源传给后端。推荐通过 `run_config` 指定：

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

后端会把选中的数据源注入 run context。Agent 可以通过工具访问该数据源，但不能拿到凭据明文。

## 支持的数据源类型

| 类型 | 说明 |
| --- | --- |
| `duckdb` | 内置 demo 或 DuckDB 文件。 |
| `sqlite` | 本地 SQLite 文件。 |
| `csv` | 本地或上传 CSV 文件，默认作为单表使用。 |
| `xlsx` | Excel 文件，默认读取首个工作表。 |
| `postgresql` | PostgreSQL 数据库。 |
| `mysql` | MySQL 数据库。 |
| `clickhouse` | ClickHouse HTTP JSON interface。 |
| `snowflake` | Snowflake warehouse。 |
| `bigquery` | BigQuery dataset。 |
| `sqlserver` | SQL Server 数据库。 |
| `oracle` | Oracle 数据库。 |
| `mongodb` | MongoDB collection 以 table-like 方式进入工具边界。 |
| `redis` | Redis keys 以伪表方式进入工具边界。 |
| `gaussdb` | GaussDB PostgreSQL-compatible。 |
| `access` | Microsoft Access over ODBC。 |
| `starrocks` | StarRocks MySQL-compatible。 |
| `trino` / `presto` | Trino 或 Presto REST API。 |
| `spark` | Spark Thrift Server / HiveServer2。 |
| `databricks` | Databricks SQL Warehouse。 |
| `elasticsearch` / `opensearch` | index-as-table 查询边界。 |
| `redshift` | Amazon Redshift PostgreSQL-compatible。 |
| `doris` | Apache Doris MySQL-compatible。 |
| `mariadb` | MariaDB MySQL-compatible。 |
| `tidb` | TiDB。 |
| `oceanbase` | OceanBase MySQL-compatible。 |
| `greenplum` | Greenplum PostgreSQL-compatible。 |

外部服务需要对应服务、网络和凭据。对外演示建议优先使用内置 DTC Growth Review，或配置 DuckDB、SQLite、CSV / Excel、PostgreSQL 或 MySQL。

## 非 SQL 数据源边界

MongoDB、Redis、Elasticsearch 和 OpenSearch 不是 SQL 数据库。系统通过受限 table-like 映射进入统一工具边界：

| 类型 | 映射方式 | 查询边界 |
| --- | --- | --- |
| MongoDB | collection -> table | 支持简单 `SELECT * FROM collection LIMIT n`，不支持复杂 aggregation。 |
| Redis | `redis_keys` 伪表 | 支持简单 key 列表查询。 |
| Elasticsearch / OpenSearch | index -> table | 支持简单 `SELECT * FROM index LIMIT n`，schema 来自 mapping。 |

复杂 Mongo aggregation、Redis 命令和 Elasticsearch DSL 不通过当前 `run_sql_readonly` 暴露给 Agent。

## 安全建议

- 使用只读账号。
- 设置合理的 `maxRows` 和 `timeoutMs`。
- 对邮箱、手机号、身份证等字段配置 `maskFields`。
- 对敏感表使用 allowlist。
- 不要在问题文本或 AG-UI payload 中粘贴数据库密码、Token 或私钥。
