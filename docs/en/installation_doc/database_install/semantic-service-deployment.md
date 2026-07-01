# Semantic Service Deployment Guide

Semantic Service (Semantic Layer REST service) is an **optional external component** of DataAgent. It supplies metadata for NL2SQL: tables, columns, JOIN relationships, SQL Few-shot, and vector semantic search. It does not store real business data—only the "database manual" for models.

- Real business data stays in SQLite, MySQL, PostgreSQL, and other databases.
- Semantic Service stores semantic metadata for DataAgent / NL2SQL Agent to query before generating SQL.
- Starting an Agent **does not require** Semantic Service; deploy it only for NL2SQL or database semantic enhancement.

If you have not run the Agent yet, see [Quick Start](../../quick_start/quick_start.md). After deployment, continue with [Scenario Data Import](scenario-data-import.md), then [NL2SQL case study](../../case/build-an-nl2sql-application.md).

> **Working directory**: After extracting the tar package, `cd` into the **service package root** (directory name usually matches the archive; below we use `semantic-layer-<version>/`). Except for Docker and model download, run subsequent commands in that directory.

## 1. Deployment goals

When finished you will have:

- A running Semantic Layer REST service
- A PostgreSQL semantic-layer database with pgvector indexes
- Working table search, column search, vector search, SQL Few-shot, and JOIN path APIs

Default ports (**examples**; override via environment variables):

| Service | Address (example) |
| --- | --- |
| Semantic Layer | `http://localhost:${SEMANTIC_PORT}` |
| REST v3 base URL | `$BASE` (see below) |
| PostgreSQL | `localhost:${PG_PORT}` |

Before continuing, export ( **example values**: `SEMANTIC_PORT=32000`, `PG_PORT=54321`):

```bash
export SEMANTIC_PORT="${SEMANTIC_PORT:-32000}"
export PG_PORT="${PG_PORT:-54321}"
export BASE="http://localhost:${SEMANTIC_PORT}/api/semantic/v1"
```

> `.properties` files **do not** expand shell variables; JDBC port literals must match `$PG_PORT` (example: `54321`).

### 1.1 Two trial paths

| Path | Use case | Key steps |
| --- | --- | --- |
| **Full trial (recommended)** | Vector search, SQL Few-shot, all features | PG → model → config A → start → [Scenario data import](scenario-data-import.md) |
| **Lite trial** | Service startup, metadata CRUD, partial text search | PG → skip model → config B → start → [Scenario data import](scenario-data-import.md) |

### 1.2 Recommended order until service is ready

| Step | Section | Action | Done when |
| --- | --- | --- | --- |
| 1 | §2 Prerequisites | Check Java 21+, `curl` | `java -version` OK |
| 2 | §3 Download package | Download and extract | `bin/start.sh` exists |
| 3 | §4.1 Docker PG | Start PostgreSQL | Container in `docker ps` |
| 4 | §5 Vector model | Download model (full path) | `.pt` and `tokenizer.json` exist |
| 5 | §6 Configure | Edit `conf/*.properties` | JDBC and model paths correct |
| 6 | §7 Start | `./bin/start.sh -p $SEMANTIC_PORT` | REST returns 200 |
| 7 | §8 Verify | Hit REST endpoints | `types/typedefs` returns 200 |

Import demo business data and metadata in [Scenario Data Import](scenario-data-import.md).

## 2. Prerequisites

| Dependency | Requirement | Notes |
| --- | --- | --- |
| Linux / macOS | Linux recommended | Examples assume Linux |
| Java | **JDK 21+** | Required to run the service package |
| PostgreSQL | 13+ | Needs `uuid-ossp`, `vector`, `pg_trgm` |
| pgvector | 0.5+ | Vector semantic search |
| curl | Recent version | REST API calls |
| wget | Recent version | Downloads (or use curl) |
| Docker | **Recommended** | One command for PostgreSQL 16 + pgvector |
| Disk | ≥ 2 GB free | Package + model (~228 MB) + PG data |

Check Java:

```bash
java -version
```

Output should include **21** or higher.

`bin/start.sh` uses **`java` on PATH**. If the default is not 21:

```bash
export PATH="/path/to/jdk-21/bin:$PATH"
java -version
```

## 3. Download and extract service package

**Download URL** (version per your distribution link):

```text
https://datagallery.obs.cn-southwest-2.myhuaweicloud.com/semantic-service/semantic-layer-0.1.0.tar.gz
```

Optional: pre-download vector model (~228 MB) and PostgreSQL image in parallel:

```bash
export DOWNLOAD_DIR="${DOWNLOAD_DIR:-./downloads}"
mkdir -p "$DOWNLOAD_DIR" && cd "$DOWNLOAD_DIR"

wget -c -O semantic-layer.tar.gz \
  'https://datagallery.obs.cn-southwest-2.myhuaweicloud.com/semantic-service/semantic-layer-0.1.0.tar.gz'

wget -c -O bge-base-zh-v1.5.tar.gz \
  https://datagallery.obs.cn-southwest-2.myhuaweicloud.com/models/BAAI/bge-base-zh-v1.5.tar.gz

docker pull pgvector/pgvector:pg16
```

Download and extract:

```bash
export SERVICE_PKG_URL='https://datagallery.obs.cn-southwest-2.myhuaweicloud.com/semantic-service/semantic-layer-0.1.0.tar.gz'

wget -O semantic-layer.tar.gz "$SERVICE_PKG_URL"
tar xzf semantic-layer.tar.gz
cd semantic-layer-*
```

Replace `wget -c -O file URL` with `curl -fsSL -C - -o file URL` if wget is unavailable.

Expected layout:

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

## 4. Prepare PostgreSQL

Semantic layer data lives in **PostgreSQL** with **pgvector** for vector search.

**Recommended**: If Docker is available, use the `pgvector/pgvector` image—no separate PostgreSQL/pgvector install.

### 4.1 Recommended: Docker one command (with pgvector)

Requires Docker and permission to run `docker`. Use `$PG_PORT` from §1 (example `54321`).

```bash
docker run -d --name semantic-layer-pg \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=semantic_layer \
  -p "${PG_PORT}:5432" \
  --restart unless-stopped \
  pgvector/pgvector:pg16
```

| Item | Value | Meaning |
| --- | --- | --- |
| Image | `pgvector/pgvector:pg16` | PostgreSQL 16 + pgvector |
| Container | `semantic-layer-pg` | Easy stop/start/logs |
| Host port | `$PG_PORT` | Maps to container `5432` |
| Database | `semantic_layer` | Matches JDBC config |

Verify:

```bash
docker ps | grep semantic-layer-pg
```

Optional extension check:

```bash
docker exec -it semantic-layer-pg psql -U postgres -d semantic_layer -c \
  "SELECT extname, extversion FROM pg_extension WHERE extname IN ('vector','uuid-ossp','pg_trgm');"
```

You **do not need** manual `CREATE EXTENSION` on first deploy: `bin/create_semantic_layer.sql` runs during `start.sh` initialization.

Persistence with a volume:

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

### 4.2 Alternative: existing PostgreSQL

For PostgreSQL 13+, ensure connectivity and permissions, then:

```sql
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
```

Update JDBC URL in `conf/semantic-service-application.properties`.

### 4.3 Not recommended: bare-metal PostgreSQL install

For quick trials, avoid long apt/yum + pgvector compile paths. Prefer an environment with Docker.

## 5. Prepare vector model (optional)

Full semantic search (vector search, SQL Few-shot, vector columns on import) needs **`BAAI/bge-base-zh-v1.5`** locally. For metadata CRUD and partial text search only, **skip this section** and use lite config in §6.

**Order**: When enabling vectors, finish download/validation here **before** setting `model.path` and starting the service in §6.

### 5.1 Download and extract

Package ~ **228 MB**:

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

Expect `bge-base-zh-v1.5/bge-base-zh-v1.5.pt` and `tokenizer.json`.

### 5.2 Validate

```bash
ls -lh "$MODEL_ROOT/bge-base-zh-v1.5/bge-base-zh-v1.5.pt"
ls -lh "$MODEL_ROOT/bge-base-zh-v1.5/tokenizer.json"
```

### 5.3 Copy tokenizer to conf (recommended)

From the **service package root**:

```bash
cp "$MODEL_ROOT/bge-base-zh-v1.5/tokenizer.json" conf/tokenizer.json
```

## 6. Configure service

Edit `conf/semantic-service-application.properties`.

### 6.1 Database connection

```properties
semantic_service.db.url=jdbc:postgresql://localhost:54321/semantic_layer
semantic_service.db.user=postgres
semantic_service.db.password=postgres
```

Port must match `$PG_PORT`. Optional shell update:

```bash
sed -i "s|^semantic_service.db.url=.*|semantic_service.db.url=jdbc:postgresql://localhost:${PG_PORT}/semantic_layer|" \
  conf/semantic-service-application.properties
```

### 6.2 Vector embedding

**Option A: full vector capabilities** (after §5)

```properties
semantic_service.vector.embedding.service.enable=true
semantic_service.vector.embedding.model.name=BAAI/bge-base-zh-v1.5
semantic_service.vector.embedding.model.path=/opt/models/bge-base-zh-v1.5
semantic_service.vector.embedding.dimensions=768
semantic_service.vector.embedding.cache.size=1000
```

**Option B: lite mode** (skip §5)

```properties
semantic_service.vector.embedding.service.enable=false
semantic_service.vector.embedding.model.path=
```

> If embedding was off during import, vector columns stay empty until you re-enable embedding and update or re-import entities ([Scenario data import](scenario-data-import.md)).

## 7. Start service

Run from the **service package root**.

```bash
java -version
./bin/start.sh -p "${SEMANTIC_PORT}"
```

Notes:

- Reads `conf/semantic-service-application.properties` and initializes `semantic_layer` via `bin/create_semantic_layer.sql`.
- Without local `psql`, initialization can use `docker exec` when the PG container is running.
- First start usually **1–2 minutes**; vector model load may add **1–3 minutes**.

Success message: `Semantic Service is ready!`

Clean rebuild (test only):

```bash
./bin/start.sh -p "${SEMANTIC_PORT}" -c
```

Stop:

```bash
./bin/stop.sh
```

Logs:

```bash
tail -f logs/application.log
```

With vectors enabled, expect `Model loaded OK: BAAI/bge-base-zh-v1.5`.

## 8. Verify service

```bash
curl -sS -o /dev/null -w "%{http_code}\n" "$BASE/types/typedefs"
```

`200` means REST is reachable.

Full vector path—confirm model load:

```bash
grep -E 'Model loaded OK|Local embedding model initialized|Failed' logs/application.log | tail -5
```

## 9. Align with DataAgent configuration

After [Scenario data import](scenario-data-import.md), configure Agent YAML:

```yaml
DATABASE:
  db_id: "demo_db"
  engine: "sqlite"
  config:
    path: "/absolute/path/to/demo_retail.sqlite"

SEMANTIC_LAYER:
  base_url: "http://localhost:32000"
  username: "example"
  password: "123456"
  timeout: 30
  verify_ssl: false
```

| DataAgent | Semantic Service metadata |
| --- | --- |
| `DATABASE.db_id` | `databaseName` |
| `DATABASE.engine` | `sourceType` |
| SQLite table names | `tableNameEn` |
| `qualifiedName` suffix | `@sqlite` / `@mysql` / `@postgresql` |

SQLite file paths live only in DataAgent YAML; Semantic Service does not store them.

## 10. Common issues

### 10.1 `types/typedefs` returns 000 or connection failed

Check service process, `SEMANTIC_PORT`, and `logs/application.log`.

### 10.2 Startup fails: cannot connect to PostgreSQL

JDBC port must match `$PG_PORT` and Docker port mapping.

### 10.3 Empty vector search

Often: embedding off during import, wrong model path, or model not loaded. Check `Model loaded OK` in logs.

### 10.4 `entity/bulk` duplicate key

Test environments: `./bin/stop.sh && ./bin/start.sh -p "${SEMANTIC_PORT}" -c` then re-import.

### 10.5 Java version: 503 or `UnsupportedClassVersionError`

Requires **Java 21+**. Fix PATH, then restart.

### 10.6 Vector model or glibc issues

Older Linux may hit `GLIBC_2.xx not found` or 503. Use option B (`enable=false`) if vectors are not required yet.

## 11. Next steps

- [Scenario data import](scenario-data-import.md): demo business DB, metadata, API verification
- [Build a dedicated NL2SQL Agent](../../case/build-an-nl2sql-application.md)
- [Semantic Service user guide](../../semantic_service/semantic-service-user-guide.md)

## 12. Checklist

- [ ] PostgreSQL (pgvector) running; `semantic_layer` reachable
- [ ] Java 21+ on PATH
- [ ] JDBC config correct
- [ ] `$BASE/types/typedefs` returns HTTP 200
- [ ] (Full path) logs show `Model loaded OK`
- [ ] [Scenario data import](scenario-data-import.md) done and search APIs verified
