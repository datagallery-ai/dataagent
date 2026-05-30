## 场景数据与元数据准备

本文是数据库安装流程的第三步，用于准备一套可用于 NL2SQL 教程的零售示例数据和 Semantic Service metadata。完成后，你会得到：

- 一个本地 SQLite 业务库：`~/demo_retail.sqlite`
- 一个可导入 Semantic Service 的元数据文件：`~/demo_seed_sqlite.json`

如果你只是想启动 MySQL / PostgreSQL / Elasticsearch，请先看 [数据库服务部署](service-deployment.md)。如果你还没有启动 Semantic Service，请先完成 [Semantic Service 部署指南](semantic-service-deployment.md)，再回到本文导入元数据。

## 1. 示例结构说明

示例使用两张表：

| 表 | 作用 | 关键字段 |
| --- | --- | --- |
| `retail_customers` | 客户维表 | `customer_id`、`customer_name`、`city` |
| `retail_orders` | 订单事实表 | `order_id`、`customer_id`、`order_amount`、`order_date` |

两张表通过 `retail_orders.customer_id = retail_customers.customer_id` 关联。这个结构足够覆盖常见的 NL2SQL 问题，例如：

- 查询总成交额。
- 按城市统计 GMV。
- 查看最近订单。
- 统计客户订单数量。

## 2. 生成 SQLite 假数据

下面的脚本只依赖 Python 标准库，会在当前用户目录生成 `demo_retail.sqlite`。

```bash
cat > ~/create_demo_retail.py << 'EOF'
import sqlite3
from pathlib import Path

db_path = Path.home() / "demo_retail.sqlite"
if db_path.exists():
    db_path.unlink()

conn = sqlite3.connect(db_path)
cur = conn.cursor()

cur.executescript(
    """
    CREATE TABLE retail_customers (
        customer_id TEXT PRIMARY KEY,
        customer_name TEXT NOT NULL,
        city TEXT NOT NULL
    );

    CREATE TABLE retail_orders (
        order_id TEXT PRIMARY KEY,
        customer_id TEXT NOT NULL,
        order_amount REAL NOT NULL,
        order_date TEXT NOT NULL,
        FOREIGN KEY (customer_id) REFERENCES retail_customers(customer_id)
    );
    """
)

customers = [
    ("C001", "Alice Zhang", "Beijing"),
    ("C002", "Bob Li", "Shanghai"),
    ("C003", "Carol Wang", "Shenzhen"),
    ("C004", "David Chen", "Beijing"),
    ("C005", "Eva Zhao", "Hangzhou"),
]

orders = [
    ("O1001", "C001", 1280.50, "2025-01-03"),
    ("O1002", "C002", 860.00, "2025-01-05"),
    ("O1003", "C001", 320.00, "2025-01-08"),
    ("O1004", "C003", 2199.00, "2025-01-11"),
    ("O1005", "C004", 640.00, "2025-01-12"),
    ("O1006", "C005", 1580.75, "2025-01-16"),
    ("O1007", "C002", 450.00, "2025-01-18"),
    ("O1008", "C004", 980.20, "2025-01-21"),
]

cur.executemany("INSERT INTO retail_customers VALUES (?, ?, ?)", customers)
cur.executemany("INSERT INTO retail_orders VALUES (?, ?, ?, ?)", orders)
conn.commit()

for table in ("retail_customers", "retail_orders"):
    count = cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    print(f"{table}: {count} rows")

conn.close()
print(f"created: {db_path}")
EOF

python ~/create_demo_retail.py
```

验证数据：

```bash
python - << 'EOF'
import sqlite3
from pathlib import Path

db_path = Path.home() / "demo_retail.sqlite"
conn = sqlite3.connect(db_path)
for row in conn.execute(
    """
    SELECT c.city, ROUND(SUM(o.order_amount), 2) AS gmv
    FROM retail_orders o
    JOIN retail_customers c ON o.customer_id = c.customer_id
    GROUP BY c.city
    ORDER BY gmv DESC
    """
):
    print(row)
conn.close()
EOF
```

如果能看到城市和 GMV 汇总结果，说明业务数据已经准备好。

## 3. 生成 Semantic Service metadata

业务数据只告诉数据库“有哪些表和数据”，还没有告诉 NL2SQL“这些表和字段是什么意思”。下面生成的 `demo_seed_sqlite.json` 会描述：

- 表级语义：订单事实表、客户维表。
- 字段语义：订单金额、下单日期、客户城市等。
- 表字段归属关系：哪些字段属于哪张表。
- Join 关系：订单表如何关联客户表。
- Golden SQL 示例：按城市汇总 GMV 的标准 SQL。

```bash
cat > ~/demo_seed_sqlite.json << 'EOF'
{
  "entities": [
    {
      "typeName": "data_table",
      "attributes": {
        "qualifiedName": "demo_db.retail_customers@sqlite",
        "name": "retail_customers",
        "databaseName": "demo_db",
        "schemaName": "main",
        "tableName": "retail_customers",
        "tableNameEn": "retail_customers",
        "sourceType": "sqlite",
        "tableNameCh": "零售客户表",
        "tableDescription": "存储客户基础信息",
        "llmContext": "客户维表 retail_customers，包含客户 ID、客户姓名和所在城市，可与订单表按 customer_id 关联。",
        "layer": "DIM",
        "status": "Active"
      }
    },
    {
      "typeName": "data_table",
      "attributes": {
        "qualifiedName": "demo_db.retail_orders@sqlite",
        "name": "retail_orders",
        "databaseName": "demo_db",
        "schemaName": "main",
        "tableName": "retail_orders",
        "tableNameEn": "retail_orders",
        "sourceType": "sqlite",
        "tableNameCh": "零售订单表",
        "tableDescription": "存储零售订单明细",
        "llmContext": "订单事实表 retail_orders，包含订单 ID、客户 ID、订单金额和下单日期，可用于销售额、GMV、订单数等统计。",
        "layer": "DWD",
        "status": "Active"
      }
    },
    {
      "typeName": "data_column",
      "attributes": {
        "qualifiedName": "demo_db.retail_customers.customer_id@sqlite",
        "databaseName": "demo_db",
        "tableNameEn": "retail_customers",
        "columnNameEn": "customer_id",
        "sourceType": "sqlite",
        "value_type": "string",
        "llmContext": "客户唯一标识，可与 retail_orders.customer_id 关联。",
        "status": "Active"
      }
    },
    {
      "typeName": "data_column",
      "attributes": {
        "qualifiedName": "demo_db.retail_customers.customer_name@sqlite",
        "databaseName": "demo_db",
        "tableNameEn": "retail_customers",
        "columnNameEn": "customer_name",
        "sourceType": "sqlite",
        "value_type": "string",
        "llmContext": "客户姓名。",
        "status": "Active"
      }
    },
    {
      "typeName": "data_column",
      "attributes": {
        "qualifiedName": "demo_db.retail_customers.city@sqlite",
        "databaseName": "demo_db",
        "tableNameEn": "retail_customers",
        "columnNameEn": "city",
        "sourceType": "sqlite",
        "value_type": "string",
        "llmContext": "客户所在城市，可用于按城市统计订单金额或客户数量。",
        "status": "Active"
      }
    },
    {
      "typeName": "data_column",
      "attributes": {
        "qualifiedName": "demo_db.retail_orders.order_id@sqlite",
        "databaseName": "demo_db",
        "tableNameEn": "retail_orders",
        "columnNameEn": "order_id",
        "sourceType": "sqlite",
        "value_type": "string",
        "llmContext": "订单唯一标识。",
        "status": "Active"
      }
    },
    {
      "typeName": "data_column",
      "attributes": {
        "qualifiedName": "demo_db.retail_orders.customer_id@sqlite",
        "databaseName": "demo_db",
        "tableNameEn": "retail_orders",
        "columnNameEn": "customer_id",
        "sourceType": "sqlite",
        "value_type": "string",
        "llmContext": "下单客户 ID，可与 retail_customers.customer_id 关联。",
        "status": "Active"
      }
    },
    {
      "typeName": "data_column",
      "attributes": {
        "qualifiedName": "demo_db.retail_orders.order_amount@sqlite",
        "databaseName": "demo_db",
        "tableNameEn": "retail_orders",
        "columnNameEn": "order_amount",
        "sourceType": "sqlite",
        "value_type": "number",
        "llmContext": "订单实付金额，单位为元，可用于销售额、GMV、客单价等统计。",
        "status": "Active"
      }
    },
    {
      "typeName": "data_column",
      "attributes": {
        "qualifiedName": "demo_db.retail_orders.order_date@sqlite",
        "databaseName": "demo_db",
        "tableNameEn": "retail_orders",
        "columnNameEn": "order_date",
        "sourceType": "sqlite",
        "value_type": "date",
        "llmContext": "订单下单日期，格式为 YYYY-MM-DD，可用于按日、月、年筛选和统计。",
        "status": "Active"
      }
    },
    {
      "typeName": "sql_process",
      "attributes": {
        "sqlId": "demo_retail_city_gmv",
        "expression": "SELECT c.city, SUM(o.order_amount) AS gmv FROM retail_orders o JOIN retail_customers c ON o.customer_id = c.customer_id GROUP BY c.city ORDER BY gmv DESC",
        "relatedTables": ["main.retail_orders", "main.retail_customers"],
        "intent": "按城市汇总 GMV",
        "query": "各城市成交额排名",
        "llmContext": "关联订单表和客户表，按客户城市汇总订单金额。",
        "status": "Active"
      }
    }
  ],
  "relationships": [
    {
      "typeName": "table_has_column",
      "end1": { "typeName": "data_column", "uniqueAttributes": { "qualifiedName": "demo_db.retail_customers.customer_id@sqlite" } },
      "end2": { "typeName": "data_table", "uniqueAttributes": { "qualifiedName": "demo_db.retail_customers@sqlite" } }
    },
    {
      "typeName": "table_has_column",
      "end1": { "typeName": "data_column", "uniqueAttributes": { "qualifiedName": "demo_db.retail_customers.customer_name@sqlite" } },
      "end2": { "typeName": "data_table", "uniqueAttributes": { "qualifiedName": "demo_db.retail_customers@sqlite" } }
    },
    {
      "typeName": "table_has_column",
      "end1": { "typeName": "data_column", "uniqueAttributes": { "qualifiedName": "demo_db.retail_customers.city@sqlite" } },
      "end2": { "typeName": "data_table", "uniqueAttributes": { "qualifiedName": "demo_db.retail_customers@sqlite" } }
    },
    {
      "typeName": "table_has_column",
      "end1": { "typeName": "data_column", "uniqueAttributes": { "qualifiedName": "demo_db.retail_orders.order_id@sqlite" } },
      "end2": { "typeName": "data_table", "uniqueAttributes": { "qualifiedName": "demo_db.retail_orders@sqlite" } }
    },
    {
      "typeName": "table_has_column",
      "end1": { "typeName": "data_column", "uniqueAttributes": { "qualifiedName": "demo_db.retail_orders.customer_id@sqlite" } },
      "end2": { "typeName": "data_table", "uniqueAttributes": { "qualifiedName": "demo_db.retail_orders@sqlite" } }
    },
    {
      "typeName": "table_has_column",
      "end1": { "typeName": "data_column", "uniqueAttributes": { "qualifiedName": "demo_db.retail_orders.order_amount@sqlite" } },
      "end2": { "typeName": "data_table", "uniqueAttributes": { "qualifiedName": "demo_db.retail_orders@sqlite" } }
    },
    {
      "typeName": "table_has_column",
      "end1": { "typeName": "data_column", "uniqueAttributes": { "qualifiedName": "demo_db.retail_orders.order_date@sqlite" } },
      "end2": { "typeName": "data_table", "uniqueAttributes": { "qualifiedName": "demo_db.retail_orders@sqlite" } }
    },
    {
      "typeName": "table_join_relationship",
      "end1": { "typeName": "data_table", "uniqueAttributes": { "qualifiedName": "demo_db.retail_orders@sqlite" } },
      "end2": { "typeName": "data_table", "uniqueAttributes": { "qualifiedName": "demo_db.retail_customers@sqlite" } },
      "attributes": {
        "join_type": "INNER JOIN",
        "expression": "{source}.customer_id = {target}.customer_id",
        "cardinality": "N:1",
        "intent": "订单客户外键关联"
      }
    },
    {
      "typeName": "column_join_relationship",
      "end1": { "typeName": "data_column", "uniqueAttributes": { "qualifiedName": "demo_db.retail_orders.customer_id@sqlite" } },
      "end2": { "typeName": "data_column", "uniqueAttributes": { "qualifiedName": "demo_db.retail_customers.customer_id@sqlite" } },
      "attributes": {
        "join_type": "INNER JOIN",
        "expression": "{source} = {target}",
        "cardinality": "N:1",
        "intent": "订单表客户 ID 关联客户表客户 ID"
      }
    }
  ]
}
EOF
```

## 4. 导入 Semantic Service

确认 Semantic Service 已启动，并把端口替换成你的实际端口。下面示例使用 `32000`，如果你按默认端口启动，则改成 `31000`。

```bash
SEMANTIC_SERVICE_URL="http://localhost:32000"

curl -sS -o /dev/null -w "HTTP %{http_code}\n" \
  "$SEMANTIC_SERVICE_URL/api/metaVisor/v3/types/typedefs"
```

返回 `HTTP 200` 后，导入元数据：

```bash
curl -X POST \
  -H "Content-Type: application/json" \
  -d @"$HOME/demo_seed_sqlite.json" \
  "$SEMANTIC_SERVICE_URL/api/metaVisor/v3/entity/bulk"
```

验证是否能检索到表：

```bash
curl -X POST \
  -H "Content-Type: application/json" \
  -d '{"typeName":"data_table","query":"retail_orders","limit":5}' \
  "$SEMANTIC_SERVICE_URL/api/metaVisor/v3/search/basic"
```

如果返回中出现 `demo_db.retail_orders@sqlite`，说明元数据已导入成功。

## 5. 和 DataAgent 配置对齐

DataAgent 读取真实业务数据，Semantic Service 提供表、字段、关系等语义信息。两边必须使用同一套名称：

```yaml
DATABASE:
  db_id: "demo_db"
  engine: "sqlite"
  config:
    path: "/home/your_user/demo_retail.sqlite"

METAVISOR:
  metavisor_url: "http://localhost:32000"
  valuematch_url: "http://localhost:8000"
```

对齐关系如下：

| DataAgent 配置 | 元数据字段 | 本文示例 |
| --- | --- | --- |
| `DATABASE.db_id` | `databaseName` | `demo_db` |
| `DATABASE.engine` | `sourceType` 与 `qualifiedName` 后缀 | `sqlite`、`@sqlite` |
| `DATABASE.config.path` | SQLite 文件路径 | `~/demo_retail.sqlite` |
| SQLite 表名 | `tableNameEn` | `retail_orders`、`retail_customers` |

SQLite 文件路径只写在 DataAgent YAML 中，Semantic Service 不保存这个路径。

## 6. 下一步

完成本文后，可以继续阅读下面两个案例教程：

- [构建 NL2SQL 专用 Agent](../../case/build-an-nl2sql-application.md)
- [构建数据分析 Agent](../../case/build-a-dataagent-from-scratch.md)

如果要换成自己的业务库，替换三处即可：

1. 用自己的数据库替换 `demo_retail.sqlite`。
2. 按真实表、字段、join 关系改写 `demo_seed_sqlite.json`。
3. 保持 DataAgent 的 `DATABASE.db_id`、`DATABASE.engine` 与元数据中的 `databaseName`、`sourceType` 一致。
