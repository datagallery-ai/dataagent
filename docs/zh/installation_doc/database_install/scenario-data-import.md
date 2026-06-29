# 场景数据与元数据准备

本文在 [Semantic Service 部署指南](semantic-service-deployment.md) 之后，用于准备 NL2SQL 教程所需的**零售示例业务库**和 **Semantic Service 元数据**。完成后你将得到：

- 一个本地 SQLite 示例业务库（教程运行时创建，**不是** Semantic Layer 服务包自带内容）
- 一份可 bulk 导入 Semantic Service 的 `demo_seed.json`

!!! note "业务库与语义服务的边界"
    - **SQLite 文件**：真实业务数据，由你在教程工作目录中创建；Agent 通过 `DATABASE.config.path` 读取（建议使用绝对路径）。
    - **Semantic Service**：只保存表、字段、JOIN、SQL 样例等**元数据**，不保存 SQLite 文件本身。
    - 逻辑库名 `demo_db` 与物理文件名 `demo_retail.sqlite` 可以不同，但元数据与 Agent 配置必须一致。

部署 Semantic Service 前请先完成 [Semantic Service 部署指南](semantic-service-deployment.md)，并设置：

```bash
export SEMANTIC_PORT="${SEMANTIC_PORT:-32000}"
export BASE="http://localhost:${SEMANTIC_PORT}/api/metaVisor/v3"
```

## 1. 示例结构说明

| 表 | 作用 | 关键字段 |
| --- | --- | --- |
| `retail_customers` | 客户维表 | `customer_id`、`customer_name`、`city` |
| `retail_orders` | 订单事实表 | `order_id`、`customer_id`、`order_amount`、`order_date` |

两张表通过 `retail_orders.customer_id = retail_customers.customer_id` 关联，可覆盖 GMV、按城市统计、订单量等 NL2SQL 问题。

## 2. 创建 demo SQLite 业务库

在**你选择的教程工作目录**执行（下文示例在该目录下创建 `data/demo_retail.sqlite`）：

```bash
mkdir -p data

python3 <<'PY'
import sqlite3
from pathlib import Path

db = Path("data/demo_retail.sqlite")
db.unlink(missing_ok=True)
conn = sqlite3.connect(db)
conn.executescript("""
PRAGMA foreign_keys = ON;

CREATE TABLE retail_customers (
    customer_id   INTEGER PRIMARY KEY,
    customer_name TEXT    NOT NULL,
    city          TEXT    NOT NULL
);

CREATE TABLE retail_orders (
    order_id      INTEGER PRIMARY KEY,
    customer_id   INTEGER NOT NULL,
    order_amount  REAL    NOT NULL,
    order_date    TEXT    NOT NULL,
    FOREIGN KEY (customer_id) REFERENCES retail_customers (customer_id)
);

INSERT INTO retail_customers (customer_id, customer_name, city) VALUES
    (1001, '张三', '北京'), (1002, '李四', '上海'), (1003, '王五', '广州'),
    (1004, '赵六', '深圳'), (1005, '钱七', '北京'), (1006, '孙八', '上海');

INSERT INTO retail_orders (order_id, customer_id, order_amount, order_date) VALUES
    (20001, 1001, 1280.50, '2025-01-05 10:20:00'),
    (20002, 1002,  860.00, '2025-01-12 14:35:00'),
    (20003, 1003, 1520.75, '2025-01-18 09:10:00'),
    (20004, 1001,  430.20, '2025-02-03 16:45:00'),
    (20005, 1004, 2100.00, '2025-02-08 11:00:00'),
    (20006, 1005,  675.80, '2025-02-15 13:25:00'),
    (20007, 1002,  990.40, '2025-03-02 08:50:00'),
    (20008, 1006, 1345.60, '2025-03-10 19:15:00'),
    (20009, 1003,  520.00, '2025-03-22 12:40:00'),
    (20010, 1004, 1875.30, '2025-04-06 17:05:00'),
    (20011, 1001,  760.00, '2025-04-14 10:30:00'),
    (20012, 1005,  945.25, '2025-04-20 15:55:00'),
    (20013, 1002, 1120.00, '2025-05-01 09:00:00'),
    (20014, 1006,  480.50, '2025-05-18 20:10:00'),
    (20015, 1003, 1650.00, '2025-05-25 11:45:00'),
    (20016, 1004,  320.75, '2025-06-03 14:20:00'),
    (20017, 1001, 1999.99, '2025-06-11 08:15:00'),
    (20018, 1005,  610.00, '2025-06-19 16:30:00');
""")
conn.commit()
print("Created:", db.resolve())
for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
    table = row[0]
    cnt = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    print(table, cnt)
conn.close()
PY
```

期望输出 `retail_customers 6` 与 `retail_orders 18`。记下 SQLite 的**绝对路径**，供 Agent 配置使用。

## 3. 准备并导入 demo 元数据

### 3.1 准备完整 JSON

在工作目录创建 `demo_seed.json`：

```bash
cat > demo_seed.json <<'JSON'
{
  "entities": [
    {
      "typeName": "data_table",
      "attributes": {
        "qualifiedName": "demo_db.retail_orders@sqlite",
        "tableId": "demo_db.retail_orders",
        "name": "retail_orders",
        "databaseName": "demo_db",
        "schemaName": "main",
        "tableName": "retail_orders",
        "tableNameEn": "retail_orders",
        "sourceType": "sqlite",
        "tableNameCh": "零售订单表",
        "tableDescription": "存储零售业务订单明细，含客户、金额、下单时间",
        "llmContext": "零售订单事实表 retail_orders，关联 retail_customers，可用于 GMV 与订单量分析；物理数据位于 data/demo_retail.sqlite",
        "layer": "DWD",
        "entityType": "PhysicalTable",
        "columnDescriptions": "order_id(bigint): 订单ID; customer_id(bigint): 客户ID; order_amount(numeric): 订单金额; order_date(timestamp): 下单时间",
        "status": "Active"
      }
    },
    {
      "typeName": "data_table",
      "attributes": {
        "qualifiedName": "demo_db.retail_customers@sqlite",
        "tableId": "demo_db.retail_customers",
        "name": "retail_customers",
        "databaseName": "demo_db",
        "schemaName": "main",
        "tableName": "retail_customers",
        "tableNameEn": "retail_customers",
        "sourceType": "sqlite",
        "tableNameCh": "零售客户表",
        "tableDescription": "存储零售客户主数据",
        "llmContext": "零售客户维度表 retail_customers，通过 customer_id 与订单表关联；物理数据位于 data/demo_retail.sqlite",
        "layer": "DWD",
        "entityType": "PhysicalTable",
        "columnDescriptions": "customer_id(bigint): 客户ID; customer_name(varchar): 客户姓名; city(varchar): 所在城市",
        "status": "Active"
      }
    },
    {
      "typeName": "data_column",
      "attributes": {
        "qualifiedName": "demo_db.retail_orders.order_id@sqlite",
        "tableId": "demo_db.retail_orders",
        "dbNameEn": "demo_db",
        "name": "order_id",
        "tableNameEn": "retail_orders",
        "columnNameEn": "order_id",
        "valueType": "bigint",
        "isPrimaryKey": true,
        "isForeignKey": false,
        "columnDescription": "订单主键",
        "columnDescriptionShort": "订单ID",
        "columnNameDesc": "订单ID",
        "llmContext": "零售订单主键 order_id",
        "status": "Active"
      }
    },
    {
      "typeName": "data_column",
      "attributes": {
        "qualifiedName": "demo_db.retail_orders.customer_id@sqlite",
        "tableId": "demo_db.retail_orders",
        "dbNameEn": "demo_db",
        "name": "customer_id",
        "tableNameEn": "retail_orders",
        "columnNameEn": "customer_id",
        "valueType": "bigint",
        "isPrimaryKey": false,
        "isForeignKey": true,
        "columnDescription": "下单客户ID，外键关联 retail_customers",
        "columnDescriptionShort": "客户ID",
        "columnNameDesc": "客户ID",
        "llmContext": "订单表外键 customer_id，关联客户维度",
        "status": "Active"
      }
    },
    {
      "typeName": "data_column",
      "attributes": {
        "qualifiedName": "demo_db.retail_orders.order_amount@sqlite",
        "tableId": "demo_db.retail_orders",
        "dbNameEn": "demo_db",
        "name": "order_amount",
        "tableNameEn": "retail_orders",
        "columnNameEn": "order_amount",
        "valueType": "numeric",
        "columnDescription": "订单成交金额，GMV 计算来源",
        "columnDescriptionShort": "订单金额",
        "columnNameDesc": "订单金额",
        "llmContext": "订单金额列 order_amount，用于 GMV、客单价等指标",
        "status": "Active"
      }
    },
    {
      "typeName": "data_column",
      "attributes": {
        "qualifiedName": "demo_db.retail_orders.order_date@sqlite",
        "tableId": "demo_db.retail_orders",
        "dbNameEn": "demo_db",
        "name": "order_date",
        "tableNameEn": "retail_orders",
        "columnNameEn": "order_date",
        "valueType": "timestamp",
        "columnDescription": "订单下单时间",
        "columnDescriptionShort": "下单时间",
        "columnNameDesc": "下单时间",
        "llmContext": "订单时间列 order_date，支持按日/月聚合",
        "status": "Active"
      }
    },
    {
      "typeName": "data_column",
      "attributes": {
        "qualifiedName": "demo_db.retail_customers.customer_id@sqlite",
        "tableId": "demo_db.retail_customers",
        "dbNameEn": "demo_db",
        "name": "customer_id",
        "tableNameEn": "retail_customers",
        "columnNameEn": "customer_id",
        "valueType": "bigint",
        "isPrimaryKey": true,
        "isForeignKey": false,
        "columnDescription": "客户主键",
        "columnDescriptionShort": "客户ID",
        "columnNameDesc": "客户ID",
        "llmContext": "客户表主键 customer_id",
        "status": "Active"
      }
    },
    {
      "typeName": "data_column",
      "attributes": {
        "qualifiedName": "demo_db.retail_customers.customer_name@sqlite",
        "tableId": "demo_db.retail_customers",
        "dbNameEn": "demo_db",
        "name": "customer_name",
        "tableNameEn": "retail_customers",
        "columnNameEn": "customer_name",
        "valueType": "varchar",
        "columnDescription": "客户姓名",
        "columnDescriptionShort": "客户姓名",
        "columnNameDesc": "客户姓名",
        "llmContext": "客户姓名列 customer_name",
        "status": "Active"
      }
    },
    {
      "typeName": "data_column",
      "attributes": {
        "qualifiedName": "demo_db.retail_customers.city@sqlite",
        "tableId": "demo_db.retail_customers",
        "dbNameEn": "demo_db",
        "name": "city",
        "tableNameEn": "retail_customers",
        "columnNameEn": "city",
        "valueType": "varchar",
        "columnDescription": "客户所在城市",
        "columnDescriptionShort": "城市",
        "columnNameDesc": "城市",
        "llmContext": "客户城市列 city，支持地域分析",
        "status": "Active"
      }
    },
    {
      "typeName": "data_column_value",
      "attributes": {
        "qualifiedName": "demo_db.retail_customers.city.value.北京@sqlite",
        "value": "北京",
        "description": "客户所在城市：北京",
        "columnNameEn": "city",
        "tableNameEn": "retail_customers",
        "dbNameEn": "demo_db",
        "valueType": "enum",
        "status": "Active"
      }
    },
    {
      "typeName": "data_column_value",
      "attributes": {
        "qualifiedName": "demo_db.retail_customers.city.value.上海@sqlite",
        "value": "上海",
        "description": "客户所在城市：上海",
        "columnNameEn": "city",
        "tableNameEn": "retail_customers",
        "dbNameEn": "demo_db",
        "valueType": "enum",
        "status": "Active"
      }
    },
    {
      "typeName": "sql_process",
      "attributes": {
        "qualifiedName": "sql.demo_retail_city_gmv@sqlite",
        "sqlId": "demo_retail_city_gmv",
        "name": "demo_retail_city_gmv",
        "expression": "SELECT c.city, SUM(o.order_amount) AS gmv FROM retail_orders o JOIN retail_customers c ON o.customer_id = c.customer_id GROUP BY 1 ORDER BY 2 DESC",
        "intent": "按城市汇总 GMV",
        "query": "各城市成交额排名",
        "relatedTables": ["retail_orders", "retail_customers"],
        "relatedTableIds": ["demo_db.retail_orders@sqlite", "demo_db.retail_customers@sqlite"],
        "status": "Active"
      }
    },
    {
      "typeName": "sql_process",
      "attributes": {
        "qualifiedName": "sql.demo_retail_monthly_order_count@sqlite",
        "sqlId": "demo_retail_monthly_order_count",
        "name": "demo_retail_monthly_order_count",
        "expression": "SELECT strftime('%Y-%m', order_date) AS month, COUNT(*) AS order_cnt FROM retail_orders GROUP BY 1 ORDER BY 1",
        "intent": "按月统计零售订单量",
        "query": "每月订单量是多少",
        "relatedTables": ["retail_orders"],
        "relatedTableIds": ["demo_db.retail_orders@sqlite"],
        "status": "Active"
      }
    },
    {
      "typeName": "metric_group",
      "attributes": {
        "qualifiedName": "business",
        "name": "经营指标",
        "groupCode": "business",
        "groupPath": "/经营指标",
        "level": 0,
        "domain": "retail",
        "description": "零售经营指标分组",
        "llmContext": "零售经营指标分组，包含 GMV、订单数等指标",
        "status": "Active"
      }
    },
    {
      "typeName": "metric_instance",
      "attributes": {
        "qualifiedName": "city_gmv",
        "name": "城市成交额",
        "instanceCode": "city_gmv",
        "metricName": "成交额",
        "metricCode": "gmv",
        "subjectEntity": "order",
        "dimensionScope": "city",
        "grain": "city",
        "scenario": "经营分析",
        "aggregationType": "SUM",
        "calculationFormula": "SUM(order_amount)",
        "sqlSnippet": "SELECT c.city, SUM(o.order_amount) AS gmv FROM retail_orders o JOIN retail_customers c ON o.customer_id = c.customer_id GROUP BY 1",
        "unit": "元",
        "synonyms": ["GMV", "成交额", "订单总金额"],
        "description": "按城市统计订单成交金额",
        "dataType": "DECIMAL",
        "llmContext": "城市成交额 GMV，按客户城市维度汇总 retail_orders.order_amount",
        "status": "Active"
      }
    },
    {
      "typeName": "udf_function",
      "attributes": {
        "name": "strftime",
        "qualifiedName": "sqlite.strftime",
        "category": "datetime",
        "type": "scalar",
        "prototype": "strftime(format, timestamp) -> string",
        "args": [
          {"name": "format", "type": "string"},
          {"name": "timestamp", "type": "timestamp"}
        ],
        "function_description": "SQLite 日期格式化函数，可用于按月、按日聚合。",
        "examples": [
          {"input": "strftime('%Y-%m', order_date)", "output": "2025-01"}
        ],
        "status": "Active"
      }
    },
    {
      "typeName": "semantic_term",
      "attributes": {
        "qualifiedName": "term.gmv@demo",
        "name": "GMV",
        "domain": "retail",
        "synonyms": ["成交额", "交易总额", "订单总金额"],
        "llmContext": "GMV 表示订单成交金额总和，通常由 order_amount 求和得到。",
        "status": "Active"
      }
    }
  ],
  "relationships": [
    {
      "typeName": "table_has_column",
      "end1": {"typeName": "data_column", "uniqueAttributes": {"qualifiedName": "demo_db.retail_orders.order_id@sqlite"}},
      "end2": {"typeName": "data_table", "uniqueAttributes": {"qualifiedName": "demo_db.retail_orders@sqlite"}}
    },
    {
      "typeName": "table_has_column",
      "end1": {"typeName": "data_column", "uniqueAttributes": {"qualifiedName": "demo_db.retail_orders.customer_id@sqlite"}},
      "end2": {"typeName": "data_table", "uniqueAttributes": {"qualifiedName": "demo_db.retail_orders@sqlite"}}
    },
    {
      "typeName": "table_has_column",
      "end1": {"typeName": "data_column", "uniqueAttributes": {"qualifiedName": "demo_db.retail_orders.order_amount@sqlite"}},
      "end2": {"typeName": "data_table", "uniqueAttributes": {"qualifiedName": "demo_db.retail_orders@sqlite"}}
    },
    {
      "typeName": "table_has_column",
      "end1": {"typeName": "data_column", "uniqueAttributes": {"qualifiedName": "demo_db.retail_orders.order_date@sqlite"}},
      "end2": {"typeName": "data_table", "uniqueAttributes": {"qualifiedName": "demo_db.retail_orders@sqlite"}}
    },
    {
      "typeName": "table_has_column",
      "end1": {"typeName": "data_column", "uniqueAttributes": {"qualifiedName": "demo_db.retail_customers.customer_id@sqlite"}},
      "end2": {"typeName": "data_table", "uniqueAttributes": {"qualifiedName": "demo_db.retail_customers@sqlite"}}
    },
    {
      "typeName": "table_has_column",
      "end1": {"typeName": "data_column", "uniqueAttributes": {"qualifiedName": "demo_db.retail_customers.customer_name@sqlite"}},
      "end2": {"typeName": "data_table", "uniqueAttributes": {"qualifiedName": "demo_db.retail_customers@sqlite"}}
    },
    {
      "typeName": "table_has_column",
      "end1": {"typeName": "data_column", "uniqueAttributes": {"qualifiedName": "demo_db.retail_customers.city@sqlite"}},
      "end2": {"typeName": "data_table", "uniqueAttributes": {"qualifiedName": "demo_db.retail_customers@sqlite"}}
    },
    {
      "typeName": "column_has_value",
      "end1": {"typeName": "data_column", "uniqueAttributes": {"qualifiedName": "demo_db.retail_customers.city@sqlite"}},
      "end2": {"typeName": "data_column_value", "uniqueAttributes": {"qualifiedName": "demo_db.retail_customers.city.value.北京@sqlite"}}
    },
    {
      "typeName": "column_has_value",
      "end1": {"typeName": "data_column", "uniqueAttributes": {"qualifiedName": "demo_db.retail_customers.city@sqlite"}},
      "end2": {"typeName": "data_column_value", "uniqueAttributes": {"qualifiedName": "demo_db.retail_customers.city.value.上海@sqlite"}}
    },
    {
      "typeName": "table_join_relationship",
      "end1": {"typeName": "data_table", "uniqueAttributes": {"qualifiedName": "demo_db.retail_orders@sqlite"}},
      "end2": {"typeName": "data_table", "uniqueAttributes": {"qualifiedName": "demo_db.retail_customers@sqlite"}},
      "attributes": {
        "join_type": "INNER JOIN",
        "expression": "{source}.customer_id = {target}.customer_id",
        "cardinality": "N:1",
        "intent": "订单客户外键关联"
      }
    },
    {
      "typeName": "column_join_relationship",
      "end1": {"typeName": "data_column", "uniqueAttributes": {"qualifiedName": "demo_db.retail_orders.customer_id@sqlite"}},
      "end2": {"typeName": "data_column", "uniqueAttributes": {"qualifiedName": "demo_db.retail_customers.customer_id@sqlite"}},
      "attributes": {
        "join_type": "INNER_JOIN",
        "expression": "{source}.customer_id = {target}.customer_id",
        "intent": "订单客户外键关联"
      }
    },
    {
      "typeName": "metric_instance_belongs_group",
      "end1": {"typeName": "metric_group", "uniqueAttributes": {"qualifiedName": "business"}},
      "end2": {"typeName": "metric_instance", "uniqueAttributes": {"qualifiedName": "city_gmv"}}
    },
    {
      "typeName": "metric_instance_realized_in_table",
      "end1": {"typeName": "metric_instance", "uniqueAttributes": {"qualifiedName": "city_gmv"}},
      "end2": {"typeName": "data_table", "uniqueAttributes": {"qualifiedName": "demo_db.retail_orders@sqlite"}}
    }
  ]
}
JSON
```

最小可用链路通常至少需要：`data_table`、`data_column`、`table_has_column`、`table_join_relationship`、`sql_process`。

### 3.2 bulk 导入

服务须已启动，且 `$BASE` 已 export：

```bash
curl -sS -X POST -H 'Content-Type: application/json' \
  -d @demo_seed.json \
  "$BASE/entity/bulk"
```

**导入前注意**：

- **完整向量路径**：建议日志出现 `Model loaded OK` 后再导入。
- **轻量路径**（`enable=false`）：可直接导入，但向量相关接口可能无结果。
- 若报 `duplicate key`，测试环境可清库重建后重试（见 [Semantic Service 部署指南](semantic-service-deployment.md)）。

## 4. 验证试用结果

```bash
curl -sS -X POST -H 'Content-Type: application/json' \
  "$BASE/search/basic" \
  -d '{"typeName":"data_table","query":"retail","limit":10}'

curl -sS "$BASE/advanced-search/table-columns-info?tableName=demo_db.retail_orders&limit=25&offset=0"

curl -sS "$BASE/advanced-search/table-relations-path?dbTable1=demo_db.retail_orders@sqlite&dbTable2=demo_db.retail_customers@sqlite"

curl -sS "$BASE/advanced-search/semantic-search-columns?keywords=订单&databaseName=demo_db&tableName=retail_orders&topK=3"

curl -sS "$BASE/advanced-search/sql-few-shots?query=各城市成交额&topK=3"

curl -sS -X POST -H 'Content-Type: application/json' \
  "$BASE/vector-search/vector" \
  -d '{"typeName":"data_table","queryText":"零售订单","limit":3}'
```

### 试用成功标准

| 检查项 | 期望 |
| --- | --- |
| `search/basic` 查 `retail` | 返回 `retail_orders`、`retail_customers` |
| `table-columns-info` | 返回 4 个订单列 |
| `table-relations-path` | 返回 orders ↔ customers JOIN |
| `semantic-search-columns` | 返回含 `order_id` 等列 |
| `sql-few-shots` / `vector-search` | 完整向量路径下有非空结果 |

## 5. 命名约定

| 概念 | 示例 | 说明 |
| --- | --- | --- |
| `databaseName` / `db_id` | `demo_db` | 逻辑库名，不一定等于 SQLite 文件名 |
| `sourceType` / `engine` | `sqlite` | 数据源类型 |
| 表 `qualifiedName` | `demo_db.retail_orders@sqlite` | `{db_id}.{table}@{sourceType}` |
| 列 `qualifiedName` | `demo_db.retail_orders.order_id@sqlite` | `{db_id}.{table}.{column}@{sourceType}` |

## 6. 与 DataAgent 配置对齐

```yaml
DATABASE:
  db_id: "demo_db"
  engine: "sqlite"
  config:
    path: "/absolute/path/to/data/demo_retail.sqlite"

METAVISOR:
  metavisor_url: "http://localhost:32000"
  username: "example"
  password: "123456"
  valuematch_url: "http://localhost:8000"
```

| DataAgent 配置 | 元数据字段 | 本文示例 |
| --- | --- | --- |
| `DATABASE.db_id` | `databaseName` | `demo_db` |
| `DATABASE.engine` | `sourceType` | `sqlite` |
| `DATABASE.config.path` | SQLite 绝对路径 | 见 §2 创建结果 |
| 表名 | `tableNameEn` | `retail_orders`、`retail_customers` |

## 7. 更新、补写向量与清库

- 更新实体：`PUT $BASE/entity/{typeName}/guid/{guid}`
- 按唯一键查 GUID：`GET $BASE/entity/uniqueAttribute/type/data_table?attr:qualifiedName=...`（`@` 转义为 `%40`）
- 测试环境清库：`./bin/stop.sh && ./bin/start.sh -p "${SEMANTIC_PORT}" -c`

若导入时 embedding 关闭，后续打开向量后需对实体执行更新或重新导入以补写向量列。

## 8. 生产环境最小导入顺序

1. `data_table` → 2. `data_column` → 3. `table_has_column` → 4. JOIN 关系 → 5. `sql_process` → 6. 指标/术语/UDF（按需）

字段建议始终填写：`qualifiedName`、`tableId`、`databaseName`、`columnNameEn`、`columnNameDesc`、`columnDescription`、`sourceType`、`status`。

## 9. 下一步

- [构建 NL2SQL 专用 Agent](../../case/build-an-nl2sql-application.md)
- [构建数据分析 Agent](../../case/build-a-dataagent-from-scratch.md)
- [Semantic Service 使用指南](../../semantic_service/semantic-service-user-guide.md)
