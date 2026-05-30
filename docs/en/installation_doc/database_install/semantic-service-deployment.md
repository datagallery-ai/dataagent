# Semantic Service Deployment Guide

Semantic Service is the metadata service used by NL2SQL. It does not store real business data; it stores which tables exist in your database, what columns mean, how tables join, and which fields support metric aggregation.

In short:

- Real business data stays in SQLite, MySQL, PostgreSQL, and other databases.
- Semantic Service stores the "database manual" for models.
- Before querying, DataAgent / NL2SQL Agent fetches tables, columns, join relationships, and value-matching information from Semantic Service.

If you want to understand NL2SQL first, read [Build a Dedicated NL2SQL Agent](../../case/build-an-nl2sql-application.md). When you are ready to connect your own database, deploy Semantic Service using this guide.

If you are following the database installation guides from the start, complete these first:

1. [Pull Docker Images](image-pull.md): prepare Docker images.
2. [Deploy Database Services](service-deployment.md): start MySQL, PostgreSQL, Elasticsearch, and other base services.
3. [Scenario Data Import](scenario-data-import.md): optional sample business data.

This guide follows those steps and answers "how does NL2SQL know what tables and columns mean?" Earlier guides prepare real data and base services; this one prepares the Semantic Service metadata service.

## 1. What You Need at the End

After deployment, you need two service URLs in Agent YAML:

```yaml
METAVISOR:
  metavisor_url: "http://localhost:32000"
  valuematch_url: "http://localhost:8000"
```

Also ensure Agent database settings match metadata imported into Semantic Service:

```yaml
DATABASE:
  db_id: "sales_db"
  engine: "sqlite"
  config:
    path: "/path/to/sales.sqlite"
```

Three fields matter most:

| Field | Plain-language meaning |
| --- | --- |
| `DATABASE.db_id` | Name of this database registered in Semantic Service, for example `sales_db`. |
| `DATABASE.engine` | Real database type, for example `sqlite`, `mysql`, or `postgres`. |
| `METAVISOR.metavisor_url` | Semantic Service URL; NL2SQL reads table and column descriptions through it. |

`db_id` and `engine` must match imported Semantic Service metadata; otherwise NL2SQL cannot resolve tables and columns.

## 2. Overall Flow

The integration breaks down into five steps:

```text
1. 准备 PostgreSQL
   用来存放 Semantic Service 的元数据

2. 启动 Semantic Service
   提供 HTTP 接口，供 DataAgent 查询元数据

3. 验证服务可访问
   curl 健康检查返回 HTTP 200

4. 导入业务库元数据
   告诉 Semantic Service：有哪些表、字段、关系

5. 修改 Agent YAML
   配置 DATABASE 和 METAVISOR，然后运行 NL2SQL
```

This guide follows that order.

## 3. Prepare the Environment

You need:

| Tool | Purpose | Recommendation |
| --- | --- | --- |
| Docker | Start PostgreSQL quickly | Docker reduces installation friction. |
| Java 8+ | Run Semantic Service | Check with `java -version`. |
| Semantic Service distribution | The service itself | `semantic-layer-0.1.0.tar.gz` (download in section 6). |
| A business database | Data source NL2SQL queries | Start with a SQLite file if you are new. |

Check Java:

```bash
java -version
```

If Java is missing, install Java 8 or newer.

## 4. Start PostgreSQL

Semantic Service needs a database for metadata and vector indexes. The default deployment recommends PostgreSQL with pgvector; in enterprise environments, you can also connect Semantic Service to an existing GaussVector database as the semantic-retrieval backend:

```bash
docker run -d \
  --name semantic-service-pg \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=semantic_layer \
  -p 54321:5432 \
  pgvector/pgvector:pg16
```

This starts PostgreSQL with:

| Setting | Value |
| --- | --- |
| Database name | `semantic_layer` |
| Username | `postgres` |
| Password | `postgres` |
| Host port | `54321` |

Verify connectivity:

```bash
PGPASSWORD=postgres psql -h localhost -p 54321 -U postgres -d semantic_layer -c "SELECT version();"
```

If you see the PostgreSQL version, the database is up.

### 4.1 Enable Required Extensions

Run:

```bash
PGPASSWORD=postgres psql -h localhost -p 54321 -U postgres -d semantic_layer -c 'CREATE EXTENSION IF NOT EXISTS "uuid-ossp";'
PGPASSWORD=postgres psql -h localhost -p 54321 -U postgres -d semantic_layer -c 'CREATE EXTENSION IF NOT EXISTS vector;'
PGPASSWORD=postgres psql -h localhost -p 54321 -U postgres -d semantic_layer -c 'CREATE EXTENSION IF NOT EXISTS pg_trgm;'
```

These extensions support UUIDs, vector search, and fuzzy text search. You only need to confirm the commands succeed. If you use GaussVector, make sure the target database has the required vector type, distance operators, and index capabilities enabled, and put the connection details in the Semantic Service configuration.

## 5. Initialize Semantic Service Schema

The Semantic Service distribution usually includes initialization SQL named like:

```text
create_semantic_layer.sql
```

Find that file in the distribution, then run:

```bash
PGPASSWORD=postgres psql -h localhost -p 54321 -U postgres -d semantic_layer \
  -f /path/to/create_semantic_layer.sql
```

Replace `/path/to/create_semantic_layer.sql` with the path on your machine.

Verify tables were created:

```bash
PGPASSWORD=postgres psql -h localhost -p 54321 -U postgres -d semantic_layer -c "\dt"
```

If you see tables such as `data_table`, `data_column`, `semantic_term`, and `sql_process`, initialization succeeded.

!!! warning
    Some initialization scripts drop and recreate the database. Read the SQL before running it in production so you do not delete existing data.

## 6. Extract and Configure Semantic Service

Download and extract `semantic-layer-0.1.0.tar.gz`:

```bash
wget https://datagallery.obs.cn-southwest-2.myhuaweicloud.com/semantic-service/semantic-layer-0.1.0.tar.gz
mkdir -p ~/semantic-service
tar xzf semantic-layer-0.1.0.tar.gz -C ~/semantic-service
cd ~/semantic-service/semantic-layer-0.1.0
```

Typical layout:

```text
semantic-layer-0.1.0/
├── bin/
│   ├── start.sh
│   └── stop.sh
├── conf/
│   └── semantic-service-application.properties
├── lib/
└── webapp/
```

Edit:

```text
conf/semantic-service-application.properties
```

Configure the database connection first:

```properties
semantic_service.db.url=jdbc:postgresql://localhost:54321/semantic_layer
semantic_service.db.user=postgres
semantic_service.db.password=postgres
```

If PostgreSQL is not on localhost, change `localhost:54321` to the actual host and port.

## 7. Vector Model Setup

Semantic Service can use embedding models for semantic search. The generated vectors can be stored in pgvector, GaussVector, or a compatible vector backend for table-description, column-description, and semantic-keyword recall. On first deploy, if you have no model yet, disable vector features and bring the service up first:

```properties
semantic_service.vector.embedding.service.enable=false
```

This simplifies deployment and is enough to validate the service and metadata import flow.

When a local model is ready, configure its path:

```properties
semantic_service.vector.embedding.model.name=BAAI/bge-base-zh-v1.5
semantic_service.vector.embedding.model.path=/opt/models/BAAI/bge-base-zh-v1.5
semantic_service.vector.embedding.dimensions=768
semantic_service.vector.embedding.cache.size=1000
```

Model output dimension, the configured `dimensions`, and the database `vector(N)` or GaussVector vector-field dimension must match.

## 8. Start Semantic Service

From the extracted service directory:

```bash
./bin/start.sh -p 32000
```

Port `32000` is an example; use any free port.

Stop the service:

```bash
./bin/stop.sh
```

If startup fails, check whether the port is in use:

```bash
lsof -nP -iTCP:32000 -sTCP:LISTEN
```

## 9. Verify the Service

Run:

```bash
curl -sS -o /dev/null -w "HTTP %{http_code}\n" \
  http://localhost:32000/api/metaVisor/v3/types/typedefs
```

Expected:

```text
HTTP 200
```

If the status is not 200, check logs:

```bash
tail -100 logs/jetty-console.log
tail -100 logs/application.log
```

Common causes:

- Wrong PostgreSQL host, username, or password.
- Initialization SQL not run; tables missing.
- Port already in use.
- Incorrect vector model path.

## 10. Import Business Database Metadata

After the service starts, import the "business database manual." Without it, NL2SQL can reach Semantic Service but cannot see your tables and columns.

Semantic Service stores metadata, not business rows. For example, if your database has an `orders` table, tell Semantic Service:

- Which database it belongs to: `sales_db`
- Table name: `orders`
- Columns: `order_id`, `order_amount`, `order_time`
- What each column means
- How tables join

### 10.1 Minimal Table Metadata Example

Minimal example for one table:

```json
{
  "entities": [
    {
      "typeName": "data_table",
      "attributes": {
        "qualifiedName": "sales_db.orders@sqlite",
        "databaseName": "sales_db",
        "schemaName": "main",
        "tableName": "orders",
        "tableNameEn": "orders",
        "sourceType": "sqlite",
        "llmContext": "订单表，记录订单金额、下单时间和客户信息",
        "status": "Active"
      }
    }
  ],
  "relationships": []
}
```

Save as `metadata.json` and import:

```bash
curl -X POST \
  -H "Content-Type: application/json" \
  -d @metadata.json \
  "http://localhost:32000/api/metaVisor/v3/entity/bulk"
```

### 10.2 Import Columns and Relationships Too

Tables alone are not enough. Column descriptions and relationships make NL2SQL useful.

Column entities are usually `data_column`; table–column links use `table_has_column`. Table joins use `table_join_relationship`.

Naming convention:

| Object | `qualifiedName` example |
| --- | --- |
| Table | `sales_db.orders@sqlite` |
| Column | `sales_db.orders.order_amount@sqlite` |

Describe business meaning clearly in column metadata. Example:

```json
{
  "typeName": "data_column",
  "attributes": {
    "qualifiedName": "sales_db.orders.order_amount@sqlite",
    "databaseName": "sales_db",
    "tableNameEn": "orders",
    "columnNameEn": "order_amount",
    "sourceType": "sqlite",
    "value_type": "number",
    "llmContext": "订单实付金额，单位为元，可用于销售额、GMV 等统计",
    "status": "Active"
  }
}
```

## 11. Align with DataAgent Configuration

The most common mistake is mismatched names.

If DataAgent YAML has:

```yaml
DATABASE:
  db_id: "sales_db"
  engine: "sqlite"
  config:
    path: "/path/to/sales.sqlite"
```

Semantic Service metadata should satisfy:

| DataAgent | Semantic Service metadata |
| --- | --- |
| `db_id: sales_db` | `databaseName: "sales_db"` |
| `engine: sqlite` | `sourceType: "sqlite"` |
| Table `orders` | `tableNameEn: "orders"` |
| SQLite engine | `qualifiedName` suffix `@sqlite` |

The SQLite file path lives only in DataAgent YAML; Semantic Service does not store `.sqlite` paths.

## 12. Use in DataAgent

After deployment and metadata import, configure NL2SQL Agent or main Agent YAML:

```yaml
DATABASE:
  db_id: "sales_db"
  engine: "sqlite"
  config:
    path: "/path/to/sales.sqlite"

METAVISOR:
  metavisor_url: "http://localhost:32000"
  valuematch_url: "http://localhost:8000"
```

Then follow the case tutorials:

- [Build a Dedicated NL2SQL Agent](../../case/build-an-nl2sql-application.md)
- [Build a Data Analysis Agent](../../case/build-a-dataagent-from-scratch.md)

## 13. Common Issues

### 13.1 `curl` does not return HTTP 200

Check:

- Service is running.
- Port is correct.
- PostgreSQL is reachable.
- Logs for database or model errors.

### 13.2 NL2SQL cannot find tables

Usually config and metadata are misaligned. Verify:

- `DATABASE.db_id` equals metadata `databaseName`.
- `DATABASE.engine` equals metadata `sourceType`.
- `qualifiedName` suffix is correct, for example `@sqlite` for SQLite.
- Table and column names match the real database.

### 13.3 Generated SQL does not match business definitions

Enrich field and metric semantics:

- Write clear meanings in `data_column.llmContext`.
- Add units, aggregation rules, and filters for metric fields.
- Add `table_join_relationship` for common joins.

### 13.4 Vector model errors

To get deployment working first, set:

```properties
semantic_service.vector.embedding.service.enable=false
```

After the service and metadata import work, configure a local model.

## 14. Checklist

- [ ] PostgreSQL container is running.
- [ ] `semantic_layer` database is reachable.
- [ ] Initialization SQL has been executed.
- [ ] JDBC URL in Semantic Service config is correct.
- [ ] `/api/metaVisor/v3/types/typedefs` returns HTTP 200.
- [ ] At least one table and its column metadata are imported.
- [ ] DataAgent `DATABASE.db_id` matches metadata `databaseName`.
- [ ] DataAgent `DATABASE.engine` matches metadata `sourceType`.
- [ ] DataAgent `METAVISOR.metavisor_url` points to Semantic Service.
