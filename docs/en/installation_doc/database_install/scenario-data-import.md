## Scenario Data and Metadata Preparation

This is step 3 of the database installation flow. It prepares retail sample data and Semantic Service metadata for NL2SQL tutorials. When you finish, you will have:

- A local SQLite business database: `~/demo_retail.sqlite`
- A metadata file ready for Semantic Service import: `~/demo_seed_sqlite.json`

If you only need to start MySQL / PostgreSQL / Elasticsearch, read [Database Service Deployment](service-deployment.md) first. If Semantic Service is not running yet, complete [Semantic Service Deployment Guide](semantic-service-deployment.md), then return here to import metadata.

## 1. Sample Schema Overview

The sample uses two tables:

| Table | Role | Key columns |
| --- | --- | --- |
| `retail_customers` | Customer dimension | `customer_id`, `customer_name`, `city` |
| `retail_orders` | Order fact table | `order_id`, `customer_id`, `order_amount`, `order_date` |

The tables join on `retail_orders.customer_id = retail_customers.customer_id`. This schema covers common NL2SQL questions such as:

- Total transaction amount.
- GMV by city.
- Recent orders.
- Order count per customer.

## 2. Generate SQLite Sample Data

The script below uses only the Python standard library and creates `demo_retail.sqlite` in your home directory.

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

Verify the data:

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

If you see city and GMV aggregates, the business data is ready.

## 3. Generate Semantic Service Metadata

Business data tells the database which tables and rows exist; it does not yet tell NL2SQL what they mean. The generated `demo_seed_sqlite.json` describes:

- Table semantics: order fact table, customer dimension.
- Column semantics: order amount, order date, customer city, and so on.
- Table–column ownership: which columns belong to which table.
- Join relationships: how orders link to customers.
- Golden SQL example: standard SQL for GMV by city.

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

## 4. Import into Semantic Service

Confirm Semantic Service is running and replace the port with yours. The example below uses `32000`; if you started with the default port, use `31000`.

```bash
SEMANTIC_SERVICE_URL="http://localhost:32000"

curl -sS -o /dev/null -w "HTTP %{http_code}\n" \
  "$SEMANTIC_SERVICE_URL/api/metaVisor/v3/types/typedefs"
```

After you see `HTTP 200`, import metadata:

```bash
curl -X POST \
  -H "Content-Type: application/json" \
  -d @"$HOME/demo_seed_sqlite.json" \
  "$SEMANTIC_SERVICE_URL/api/metaVisor/v3/entity/bulk"
```

Verify that tables can be retrieved:

```bash
curl -X POST \
  -H "Content-Type: application/json" \
  -d '{"typeName":"data_table","query":"retail_orders","limit":5}' \
  "$SEMANTIC_SERVICE_URL/api/metaVisor/v3/search/basic"
```

If the response includes `demo_db.retail_orders@sqlite`, metadata import succeeded.

## 5. Align with DataAgent Configuration

DataAgent reads real business data; Semantic Service supplies table, column, and relationship semantics. Both sides must use the same names:

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

Alignment:

| DataAgent config | Metadata field | This guide |
| --- | --- | --- |
| `DATABASE.db_id` | `databaseName` | `demo_db` |
| `DATABASE.engine` | `sourceType` and `qualifiedName` suffix | `sqlite`, `@sqlite` |
| `DATABASE.config.path` | SQLite file path | `~/demo_retail.sqlite` |
| SQLite table names | `tableNameEn` | `retail_orders`, `retail_customers` |

The SQLite file path is only in DataAgent YAML; Semantic Service does not store it.

## 6. Next Steps

After this guide, continue with:

- [Build a Dedicated NL2SQL Agent](../../case/build-an-nl2sql-application.md)
- [Build a Data Analysis Agent](../../case/build-a-dataagent-from-scratch.md)

To use your own business database, change three things:

1. Replace `demo_retail.sqlite` with your database.
2. Rewrite `demo_seed_sqlite.json` for your real tables, columns, and join relationships.
3. Keep DataAgent `DATABASE.db_id` and `DATABASE.engine` aligned with metadata `databaseName` and `sourceType`.
