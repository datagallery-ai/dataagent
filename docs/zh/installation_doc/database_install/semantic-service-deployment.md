# Semantic Service 部署指南

Semantic Service 是给 NL2SQL 使用的“元数据服务”。它不保存真实业务数据，而是保存数据库里有哪些表、字段是什么意思、表之间怎么关联、哪些字段可以用于指标统计等信息。

简单说：

- 真实业务数据仍然在你的 SQLite、MySQL、PostgreSQL 等数据库里。
- Semantic Service 保存“给模型看的数据库说明书”。
- DataAgent / NL2SQL Agent 查询数据库前，会先从 Semantic Service 获取表、字段、join 关系和值匹配信息。

如果你只是想先理解 NL2SQL，可以先看 [构建 NL2SQL 专用 Agent](../../case/build-an-nl2sql-application.md)。如果你已经准备接入自己的数据库，再按本文部署 Semantic Service。

如果你是按数据库安装指导从头开始，建议先完成：

1. [数据库镜像拉取](image-pull.md)：准备 Docker 镜像。
2. [数据库服务部署](service-deployment.md)：启动 MySQL、PostgreSQL、Elasticsearch 等基础服务。
3. [场景数据导入](scenario-data-import.md)：可选，导入示例业务数据。

本文接在这些步骤之后，解决的是“让 NL2SQL 知道表和字段是什么意思”的问题。也就是说，前几篇准备真实数据和基础服务，本文准备 Semantic Service metadata 服务。

## 1. 你最终需要得到什么

部署完成后，你需要拿到两个服务地址，并写进 Agent YAML：

```yaml
METAVISOR:
  metavisor_url: "http://localhost:32000"
  valuematch_url: "http://localhost:8000"
```

还需要确保 Agent 的数据库配置和导入到 Semantic Service 的元数据一致：

```yaml
DATABASE:
  db_id: "sales_db"
  engine: "sqlite"
  config:
    path: "/path/to/sales.sqlite"
```

三个字段最关键：

| 字段 | 小白解释 |
| --- | --- |
| `DATABASE.db_id` | 这套数据库在 Semantic Service 中注册的名字，例如 `sales_db`。 |
| `DATABASE.engine` | 真实数据库类型，例如 `sqlite`、`mysql`、`postgres`。 |
| `METAVISOR.metavisor_url` | Semantic Service 地址，NL2SQL 会通过它读取表和字段说明。 |

`db_id` 和 `engine` 必须和你导入 Semantic Service 的元数据一致，否则 NL2SQL 会查不到对应表字段。

## 2. 先理解整体流程

整个接入过程可以拆成 5 步：

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

本文会按这个顺序展开。

## 3. 准备环境

你需要准备：

| 工具 | 用途 | 新手建议 |
| --- | --- | --- |
| Docker | 快速启动 PostgreSQL | 推荐使用 Docker，少踩安装坑。 |
| Java 8+ | 运行 Semantic Service | 先用 `java -version` 检查。 |
| Semantic Service 发行包 | 服务本体 | `semantic-layer-0.1.0.tar.gz`（见第 6 节下载）。 |
| 一个业务数据库 | NL2SQL 最终查询的数据源 | 新手建议先用 SQLite 文件。 |

检查 Java：

```bash
java -version
```

如果没有 Java，需要先安装 Java 8 或更高版本。

## 4. 启动 PostgreSQL

Semantic Service 需要数据库保存元数据和向量索引。默认部署推荐直接用带 pgvector 的 PostgreSQL 镜像；在企业环境中，也可以对接已有的 GaussVector 向量数据库作为语义检索底座：

```bash
docker run -d \
  --name semantic-service-pg \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=semantic_layer \
  -p 54321:5432 \
  pgvector/pgvector:pg16
```

这条命令会启动一个 PostgreSQL：

| 配置 | 值 |
| --- | --- |
| 数据库名 | `semantic_layer` |
| 用户名 | `postgres` |
| 密码 | `postgres` |
| 宿主机端口 | `54321` |

验证能否连接：

```bash
PGPASSWORD=postgres psql -h localhost -p 54321 -U postgres -d semantic_layer -c "SELECT version();"
```

如果能看到 PostgreSQL 版本信息，说明数据库已启动。

### 4.1 启用必要扩展

继续执行：

```bash
PGPASSWORD=postgres psql -h localhost -p 54321 -U postgres -d semantic_layer -c 'CREATE EXTENSION IF NOT EXISTS "uuid-ossp";'
PGPASSWORD=postgres psql -h localhost -p 54321 -U postgres -d semantic_layer -c 'CREATE EXTENSION IF NOT EXISTS vector;'
PGPASSWORD=postgres psql -h localhost -p 54321 -U postgres -d semantic_layer -c 'CREATE EXTENSION IF NOT EXISTS pg_trgm;'
```

这些扩展分别用于 UUID、向量检索和文本模糊检索。新手不需要理解内部细节，只要确认命令执行成功即可。如果使用 GaussVector，请确认目标库已经启用对应的向量类型、距离算子和索引能力，并保证连接信息写入 Semantic Service 配置。

## 5. 初始化 Semantic Service 表结构

Semantic Service 发行包通常会提供初始化 SQL，名字一般类似：

```text
create_semantic_layer.sql
```

请先在发行包中找到这个 SQL 文件，然后执行：

```bash
PGPASSWORD=postgres psql -h localhost -p 54321 -U postgres -d semantic_layer \
  -f /path/to/create_semantic_layer.sql
```

把 `/path/to/create_semantic_layer.sql` 替换成你本机真实路径。

执行后检查表是否创建成功：

```bash
PGPASSWORD=postgres psql -h localhost -p 54321 -U postgres -d semantic_layer -c "\dt"
```

如果能看到 `data_table`、`data_column`、`semantic_term`、`sql_process` 等表，说明初始化成功。

!!! warning
    有些初始化脚本会删库重建。生产环境执行前务必先阅读 SQL 内容，确认不会误删已有数据。

## 6. 解压并配置 Semantic Service

下载并解压 `semantic-layer-0.1.0.tar.gz`：

```bash
wget https://datagallery.obs.cn-southwest-2.myhuaweicloud.com/semantic-service/semantic-layer-0.1.0.tar.gz
mkdir -p ~/semantic-service
tar xzf semantic-layer-0.1.0.tar.gz -C ~/semantic-service
cd ~/semantic-service/semantic-layer-0.1.0
```

常见目录结构如下：

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

编辑配置文件：

```text
conf/semantic-service-application.properties
```

先配置数据库连接：

```properties
semantic_service.db.url=jdbc:postgresql://localhost:54321/semantic_layer
semantic_service.db.user=postgres
semantic_service.db.password=postgres
```

如果 PostgreSQL 不在本机，把 `localhost:54321` 改成实际地址。

## 7. 处理向量模型

Semantic Service 可以用 embedding 模型做语义搜索，生成的向量可写入 pgvector 或 GaussVector 等向量存储，用于表描述、字段描述和语义关键词召回。第一次部署时，如果你还没有准备模型，可以先关闭向量能力，先把服务跑起来：

```properties
semantic_service.vector.embedding.service.enable=false
```

这样做的好处是部署步骤更简单，适合先验证服务和元数据导入流程。

如果你已经准备好本地模型，再配置模型路径：

```properties
semantic_service.vector.embedding.model.name=BAAI/bge-base-zh-v1.5
semantic_service.vector.embedding.model.path=/opt/models/BAAI/bge-base-zh-v1.5
semantic_service.vector.embedding.dimensions=768
semantic_service.vector.embedding.cache.size=1000
```

注意：模型输出维度、配置里的 `dimensions`、数据库里的 `vector(N)` 或 GaussVector 向量字段维度必须一致。

## 8. 启动 Semantic Service

在解压后的服务目录中执行：

```bash
./bin/start.sh -p 32000
```

这里用 `32000` 作为示例端口。你也可以换成其他空闲端口。

停止服务：

```bash
./bin/stop.sh
```

如果启动失败，先检查端口是否被占用：

```bash
lsof -nP -iTCP:32000 -sTCP:LISTEN
```

## 9. 验证服务是否启动成功

执行：

```bash
curl -sS -o /dev/null -w "HTTP %{http_code}\n" \
  http://localhost:32000/api/metaVisor/v3/types/typedefs
```

如果返回：

```text
HTTP 200
```

说明 Semantic Service 已经能访问。

如果不是 200，查看日志：

```bash
tail -100 logs/jetty-console.log
tail -100 logs/application.log
```

常见原因包括：

- PostgreSQL 地址、用户名或密码配置错。
- 初始化 SQL 没执行，表不存在。
- 端口被占用。
- 向量模型路径配置错。

## 10. 导入业务库元数据

服务启动后，还需要导入“业务数据库说明书”。否则 NL2SQL 虽然能访问 Semantic Service，但查不到你的表和字段。

Semantic Service 存的是元数据，不存真实业务数据。例如你的业务库里有一张订单表 `orders`，你需要告诉 Semantic Service：

- 这张表属于哪个数据库：`sales_db`
- 表名是什么：`orders`
- 字段有哪些：`order_id`、`order_amount`、`order_time`
- 字段分别是什么意思
- 表之间怎么 join

### 10.1 最小表元数据示例

下面是一张表的最小示例：

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

保存为 `metadata.json` 后导入：

```bash
curl -X POST \
  -H "Content-Type: application/json" \
  -d @metadata.json \
  "http://localhost:32000/api/metaVisor/v3/entity/bulk"
```

### 10.2 字段和关系也要导入

只导入表还不够。真正让 NL2SQL 好用的是字段描述和表关系。

字段实体通常是 `data_column`，表字段关系通常是 `table_has_column`。表之间的 join 关系通常是 `table_join_relationship`。

命名建议：

| 对象 | `qualifiedName` 示例 |
| --- | --- |
| 表 | `sales_db.orders@sqlite` |
| 字段 | `sales_db.orders.order_amount@sqlite` |

字段描述应尽量写清楚业务含义。例如：

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

## 11. 和 DataAgent 配置对齐

导入元数据时，最容易出错的是名字对不上。

如果 DataAgent YAML 写的是：

```yaml
DATABASE:
  db_id: "sales_db"
  engine: "sqlite"
  config:
    path: "/path/to/sales.sqlite"
```

那么 Semantic Service 中的元数据应满足：

| DataAgent | Semantic Service 元数据 |
| --- | --- |
| `db_id: sales_db` | `databaseName: "sales_db"` |
| `engine: sqlite` | `sourceType: "sqlite"` |
| 表名 `orders` | `tableNameEn: "orders"` |
| SQLite 引擎 | `qualifiedName` 后缀使用 `@sqlite` |

SQLite 文件路径只写在 DataAgent YAML 中，Semantic Service 不保存 `.sqlite` 文件路径。

## 12. 在 DataAgent 中使用

完成部署和元数据导入后，在 NL2SQL Agent 或主 Agent YAML 中配置：

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

然后按 case 教程运行：

- [构建 NL2SQL 专用 Agent](../../case/build-an-nl2sql-application.md)
- [构建数据分析 Agent](../../case/build-a-dataagent-from-scratch.md)

## 13. 常见问题

### 13.1 `curl` 不是 HTTP 200

优先检查：

- 服务是否启动。
- 端口是否写错。
- PostgreSQL 是否能连接。
- 日志里是否有数据库或模型错误。

### 13.2 NL2SQL 查不到表

通常是配置和元数据没对齐。检查：

- `DATABASE.db_id` 是否等于元数据中的 `databaseName`。
- `DATABASE.engine` 是否等于元数据中的 `sourceType`。
- `qualifiedName` 后缀是否正确，例如 SQLite 用 `@sqlite`。
- 表名和字段名是否和真实数据库一致。

### 13.3 SQL 生成结果不符合业务口径

优先补充字段和指标语义：

- 给 `data_column.llmContext` 写清楚字段含义。
- 给指标字段补充单位、统计口径和过滤条件。
- 给常用 join 补充 `table_join_relationship`。

### 13.4 向量模型报错

如果只是先跑通部署，可以先设置：

```properties
semantic_service.vector.embedding.service.enable=false
```

等服务和元数据导入流程跑通后，再回头配置本地模型。

## 14. 新手检查清单

- [ ] PostgreSQL 容器已启动。
- [ ] `semantic_layer` 数据库可连接。
- [ ] 初始化 SQL 已执行。
- [ ] Semantic Service 配置中的 JDBC 地址正确。
- [ ] `/api/metaVisor/v3/types/typedefs` 返回 HTTP 200。
- [ ] 已导入至少一张表和它的字段元数据。
- [ ] DataAgent 的 `DATABASE.db_id` 和元数据 `databaseName` 一致。
- [ ] DataAgent 的 `DATABASE.engine` 和元数据 `sourceType` 一致。
- [ ] DataAgent 的 `METAVISOR.metavisor_url` 指向 Semantic Service 地址。
