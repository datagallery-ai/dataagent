# Semantic Service 部署指南

Semantic Service（Semantic Layer REST 服务）是 DataAgent 的**外部可选组件**，为 NL2SQL 提供表、字段、JOIN 关系、SQL Few-shot 和向量语义检索等元数据能力。它不保存真实业务数据，只保存“给模型看的数据库说明书”。

- 真实业务数据仍在 SQLite、MySQL、PostgreSQL 等业务库中。
- Semantic Service 保存语义元数据，供 DataAgent / NL2SQL Agent 在生成 SQL 前查询。
- 启动 Agent **不需要** Semantic Service；只有 NL2SQL 或数据库语义增强场景才需要部署。

若你尚未跑通 Agent 本体，请先看 [快速开始](../../quick_start/quick_start.md)。部署完成后，继续 [场景数据导入](scenario-data-import.md) 导入 demo 元数据，再进入 [NL2SQL 案例](../../case/build-an-nl2sql-application.md)。

> **工作目录约定**：解压 tar 包后 `cd` 进入**服务包根目录**（目录名通常与压缩包一致，下文以 `semantic-layer-<version>/` 表示）。除 Docker、模型下载外，后续命令均在此目录执行。

## 1. 部署目标

完成后将得到：

- 一个运行中的 Semantic Layer REST 服务
- 一个 PostgreSQL 语义层数据库（含 pgvector 向量索引）
- 可用的表检索、列检索、向量检索、SQL Few-shot、JOIN 关系查询接口

默认端口**示例**（可通过环境变量修改）：

| 服务 | 地址（示例） |
| --- | --- |
| Semantic Layer | `http://localhost:${SEMANTIC_PORT}` |
| REST v3 Base URL | `$BASE`（见下方） |
| PostgreSQL | `localhost:${PG_PORT}` |

在后续操作前先 export（**示例取值**：`SEMANTIC_PORT=32000`，`PG_PORT=54321`）：

```bash
export SEMANTIC_PORT="${SEMANTIC_PORT:-32000}"
export PG_PORT="${PG_PORT:-54321}"
export BASE="http://localhost:${SEMANTIC_PORT}/api/metaVisor/v3"
```

> `.properties` 配置文件**不支持** shell 变量，其中的 JDBC 端口须写**与 `$PG_PORT` 相同的数字**（示例即 `54321`）。

### 1.1 两条试用路径

| 路径 | 适用场景 | 关键步骤 |
| --- | --- | --- |
| **完整试用（推荐）** | 体验向量搜索、SQL Few-shot 等全部能力 | 准备 PG → 下载模型 → 方案 A 配置 → 启动 → [场景数据导入](scenario-data-import.md) |
| **轻量试用** | 仅验证服务启动、元数据 CRUD、部分文本搜索 | 准备 PG → 跳过模型 → 方案 B 配置 → 启动 → [场景数据导入](scenario-data-import.md) |

### 1.2 从零到服务就绪的推荐顺序

| 步骤 | 章节 | 做什么 | 完成标志 |
| --- | --- | --- | --- |
| 1 | §2 前置条件 | 检查 Java 21+、`curl` | `java -version` 正常 |
| 2 | §3 下载并解压服务包 | 下载并解压服务包 | 存在 `bin/start.sh` |
| 3 | §4.1 Docker 启动 PG | Docker 启动 PostgreSQL | `docker ps` 可见容器 |
| 4 | §5 准备向量模型 | 下载向量模型（完整路径） | `.pt` 与 `tokenizer.json` 存在 |
| 5 | §6 配置服务 | 编辑 `conf/*.properties` | JDBC 与向量路径正确 |
| 6 | §7 启动服务 | `./bin/start.sh -p $SEMANTIC_PORT` | REST 返回 200 |
| 7 | §8 验证服务 | 检查 REST 接口 | `types/typedefs` 返回 200 |

导入 demo 业务库与元数据见 [场景数据导入](scenario-data-import.md)。

## 2. 前置条件

| 依赖 | 要求 | 说明 |
| --- | --- | --- |
| Linux / macOS | 推荐 Linux | 示例命令以 Linux 为主 |
| Java | **JDK 21+** | 服务包运行需要 Java 21 |
| PostgreSQL | 13+ | 需支持 `uuid-ossp`、`vector`、`pg_trgm` |
| pgvector | 0.5+ | 用于语义向量检索 |
| curl | 任意较新版本 | 调用 REST API |
| wget | 任意较新版本 | 下载服务包与模型（无 wget 时可用 curl） |
| Docker | **推荐** | 一条命令拉起 PostgreSQL 16 + pgvector |
| 磁盘空间 | ≥ 2 GB 可用 | 服务包 + 模型（~228 MB）+ PostgreSQL 数据 |

检查 Java：

```bash
java -version
```

期望输出含 **21** 或更高版本。

`bin/start.sh` 使用当前 shell **PATH 中的 `java`**。若默认 Java 不是 21，启动前将其 `bin` 目录置于 PATH 最前：

```bash
export PATH="/path/to/jdk-21/bin:$PATH"
java -version
```

## 3. 下载并解压服务包

**服务包下载地址**（版本以实际分发链接为准）：

```text
https://datagallery.obs.cn-southwest-2.myhuaweicloud.com/semantic-service/semantic-layer-0.1.0.tar.gz
```

可选：并行预下载向量模型（完整路径，约 228 MB）与 PostgreSQL 镜像，节省等待时间：

```bash
export DOWNLOAD_DIR="${DOWNLOAD_DIR:-./downloads}"
mkdir -p "$DOWNLOAD_DIR" && cd "$DOWNLOAD_DIR"

wget -c -O semantic-layer.tar.gz \
  'https://datagallery.obs.cn-southwest-2.myhuaweicloud.com/semantic-service/semantic-layer-0.1.0.tar.gz'

wget -c -O bge-base-zh-v1.5.tar.gz \
  https://datagallery.obs.cn-southwest-2.myhuaweicloud.com/models/BAAI/bge-base-zh-v1.5.tar.gz

docker pull pgvector/pgvector:pg16
```

下载并解压服务包：

```bash
export SERVICE_PKG_URL='https://datagallery.obs.cn-southwest-2.myhuaweicloud.com/semantic-service/semantic-layer-0.1.0.tar.gz'

wget -O semantic-layer.tar.gz "$SERVICE_PKG_URL"
tar xzf semantic-layer.tar.gz
cd semantic-layer-*
```

若无 `wget`，将 `wget -c -O file URL` 替换为 `curl -fsSL -C - -o file URL`。

解压后目录应类似：

```text
semantic-layer-<version>/
├── bin/
│   ├── start.sh
│   ├── stop.sh
│   └── create_semantic_layer.sql
├── conf/
│   ├── semantic-service-application.properties
│   └── ...
├── lib/
└── webapp/
```

## 4. 准备 PostgreSQL

Semantic Layer 的语义层数据存放在 **PostgreSQL** 中，且依赖 **pgvector** 扩展做向量检索。

**推荐做法**：若机器上已安装 Docker，直接拉取 `pgvector/pgvector` 镜像即可，**无需单独安装 PostgreSQL 和 pgvector**。

### 4.1 推荐：Docker 一键启动（含 pgvector）

**前置**：已安装 Docker，且当前用户有执行 `docker` 的权限。端口使用 §1 中的 `$PG_PORT`（示例 `54321`）。

```bash
docker run -d --name semantic-layer-pg \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=semantic_layer \
  -p "${PG_PORT}:5432" \
  --restart unless-stopped \
  pgvector/pgvector:pg16
```

| 项 | 值 | 含义 |
| --- | --- | --- |
| 镜像 | `pgvector/pgvector:pg16` | PostgreSQL 16 + pgvector |
| 容器名 | `semantic-layer-pg` | 便于 stop/start/logs |
| 宿主机端口 | `$PG_PORT` | 映射到容器内 `5432` |
| 库名 | `semantic_layer` | 与服务 JDBC 配置一致 |

验证容器：

```bash
docker ps | grep semantic-layer-pg
```

验证 pgvector 扩展（可选）：

```bash
docker exec -it semantic-layer-pg psql -U postgres -d semantic_layer -c \
  "SELECT extname, extversion FROM pg_extension WHERE extname IN ('vector','uuid-ossp','pg_trgm');"
```

首次部署时，**不必手动执行** `CREATE EXTENSION`：服务包内的 `bin/create_semantic_layer.sql` 会在 `start.sh` 初始化库时一并创建扩展和表。

常用运维：

```bash
docker logs -f semantic-layer-pg
docker stop semantic-layer-pg && docker start semantic-layer-pg
```

如需持久化数据，可增加 volume：

```bash
docker run -d --name semantic-layer-pg \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=semantic_layer \
  -p "${PG_PORT}:5432" \
  -v semantic-layer-pg-data:/var/lib/postgresql/data \
  --restart unless-stopped \
  pgvector/pgvector:pg16
```

### 4.2 备选：使用已有 PostgreSQL 实例

若已有 PostgreSQL（13+），请确认网络可达、账号具备建库建表权限，并在目标库中执行：

```sql
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
```

将 `conf/semantic-service-application.properties` 中的 JDBC URL 改为实际地址。

### 4.3 不推荐：从零在本机安装 PostgreSQL

快速体验场景 **不建议** 走 apt/yum 安装 PostgreSQL 再单独装 pgvector 的长路径。若既无 Docker、又无现成 PG，优先申请可跑 Docker 的环境。

## 5. 准备向量模型（可选）

完整语义检索（向量搜索、SQL Few-shot、导入时自动写入向量列）需要本地 embedding 模型 **`BAAI/bge-base-zh-v1.5`**。若仅需元数据 CRUD 和部分文本搜索，**可跳过本节**，直接在第 6 节关闭向量开关。

**顺序**：启用向量时，须**先**完成本节下载与校验，**再**在第 6 节填写 `model.path` 并启动服务。

### 5.1 下载与解压

官方模型包约 **228 MB**：

```text
https://datagallery.obs.cn-southwest-2.myhuaweicloud.com/models/BAAI/bge-base-zh-v1.5.tar.gz
```

```bash
export MODEL_ROOT=/opt/models
mkdir -p "$MODEL_ROOT" && cd "$MODEL_ROOT"

wget -O bge-base-zh-v1.5.tar.gz \
  https://datagallery.obs.cn-southwest-2.myhuaweicloud.com/models/BAAI/bge-base-zh-v1.5.tar.gz
tar xzf bge-base-zh-v1.5.tar.gz
```

解压后应包含 `bge-base-zh-v1.5/bge-base-zh-v1.5.pt` 与 `tokenizer.json`。

### 5.2 校验

```bash
ls -lh "$MODEL_ROOT/bge-base-zh-v1.5/bge-base-zh-v1.5.pt"
ls -lh "$MODEL_ROOT/bge-base-zh-v1.5/tokenizer.json"
```

### 5.3 复制 tokenizer 到 conf（推荐）

内网无法访问 HuggingFace 时，在**服务包根目录**执行：

```bash
cp "$MODEL_ROOT/bge-base-zh-v1.5/tokenizer.json" conf/tokenizer.json
```

## 6. 配置服务

编辑 `conf/semantic-service-application.properties`。

### 6.1 数据库连接

```properties
semantic_service.db.url=jdbc:postgresql://localhost:54321/semantic_layer
semantic_service.db.user=postgres
semantic_service.db.password=postgres
```

JDBC 端口须与 `$PG_PORT` 一致。可选：用 shell 写入：

```bash
sed -i "s|^semantic_service.db.url=.*|semantic_service.db.url=jdbc:postgresql://localhost:${PG_PORT}/semantic_layer|" \
  conf/semantic-service-application.properties
```

### 6.2 向量嵌入

**方案 A：完整向量能力**（已完成第 5 节时选用）

```properties
semantic_service.vector.embedding.service.enable=true
semantic_service.vector.embedding.model.name=BAAI/bge-base-zh-v1.5
semantic_service.vector.embedding.model.path=/opt/models/bge-base-zh-v1.5
semantic_service.vector.embedding.dimensions=768
semantic_service.vector.embedding.cache.size=1000
```

**方案 B：轻量模式**（跳过第 5 节时选用）

```properties
semantic_service.vector.embedding.service.enable=false
semantic_service.vector.embedding.model.path=
```

> 导入元数据时若向量开关为关闭，向量列不会写入。后续再打开向量后，需对实体执行更新或重新导入（见 [场景数据导入](scenario-data-import.md)）。

## 7. 启动服务

以下命令均在**服务包根目录**执行。

启动前确认 Java 21 已在 PATH 中：

```bash
java -version
./bin/start.sh -p "${SEMANTIC_PORT}"
```

说明：

- 脚本会读取 `conf/semantic-service-application.properties`，并在 PostgreSQL 中自动初始化 `semantic_layer` 库（执行 `bin/create_semantic_layer.sql`）。
- 若本机无 `psql` 客户端但 Docker PG 容器在运行，脚本会通过 `docker exec` 完成建库。
- 首次启动通常需 **1–2 分钟**；启用向量模型可能再额外 **1–3 分钟**。

启动成功时终端应出现 `Semantic Service is ready!`。

清库重建（测试环境）：

```bash
./bin/start.sh -p "${SEMANTIC_PORT}" -c
```

停止服务：

```bash
./bin/stop.sh
```

查看日志：

```bash
tail -f logs/application.log
```

若启用了向量模型，日志中应看到 `Model loaded OK: BAAI/bge-base-zh-v1.5`。

## 8. 验证服务可用

```bash
curl -sS -o /dev/null -w "%{http_code}\n" "$BASE/types/typedefs"
```

返回 `200` 表示 REST 服务可访问。

完整向量路径下，确认模型已加载：

```bash
grep -E 'Model loaded OK|Local embedding model initialized|Failed' logs/application.log | tail -5
```

## 9. 与 DataAgent 配置对齐

完成 [场景数据导入](scenario-data-import.md) 后，在 Agent YAML 中配置：

```yaml
DATABASE:
  db_id: "demo_db"
  engine: "sqlite"
  config:
    path: "/absolute/path/to/demo_retail.sqlite"

METAVISOR:
  metavisor_url: "http://localhost:32000"
  username: "example"
  password: "123456"
  valuematch_url: "http://localhost:8000"
```

| DataAgent | Semantic Service 元数据 |
| --- | --- |
| `DATABASE.db_id` | `databaseName` |
| `DATABASE.engine` | `sourceType` |
| SQLite 表名 | `tableNameEn` |
| `qualifiedName` 后缀 | `@sqlite` / `@mysql` / `@postgresql` |

SQLite 文件路径只写在 DataAgent YAML 中，Semantic Service 不保存 `.sqlite` 路径。

## 10. 常见问题

### 10.1 `types/typedefs` 返回 000 或连接失败

检查服务是否启动、`SEMANTIC_PORT` 是否正确，查看 `logs/application.log`。

### 10.2 启动失败，无法连接 PostgreSQL

确认 JDBC URL 端口与 `$PG_PORT`、Docker 映射端口一致。

### 10.3 向量搜索为空

常见原因：导入时 embedding 未开启、模型路径错误、模型未加载成功。检查日志中的 `Model loaded OK`。

### 10.4 `entity/bulk` 重复导入报 duplicate key

测试环境可 `./bin/stop.sh && ./bin/start.sh -p "${SEMANTIC_PORT}" -c` 清库后重试。

### 10.5 Java 版本导致 503 或 `UnsupportedClassVersionError`

服务需 **Java 21+**。调整 PATH 后 `./bin/stop.sh && ./bin/start.sh -p "${SEMANTIC_PORT}"`。

### 10.6 向量模型或 glibc 相关问题

较旧 Linux 可能出现 `GLIBC_2.xx not found` 或 503。暂不需要向量时使用第 6 节方案 B（`enable=false`）。

## 11. 下一步

- [场景数据导入](scenario-data-import.md)：创建 demo 业务库、导入元数据、验证检索 API
- [构建 NL2SQL 专用 Agent](../../case/build-an-nl2sql-application.md)
- [Semantic Service 使用指南](../../semantic_service/semantic-service-user-guide.md)

## 12. 检查清单

- [ ] PostgreSQL（pgvector）已启动，`semantic_layer` 可连接
- [ ] Java 21+ 已在 PATH 中
- [ ] Semantic Service JDBC 配置正确
- [ ] `$BASE/types/typedefs` 返回 HTTP 200
- [ ] （完整路径）日志含 `Model loaded OK`
- [ ] 已完成 [场景数据导入](scenario-data-import.md) 并验证检索接口
