# DataLink 对外接口使用说明

本文档列出所有外界能感知的接口——CLI 命令、REST API 和 MCP 工具，包括参数、行为、配置方式和典型用法。

---

## 接口分层

DataLink 的接口分为两层：

- **在线检索**：agent 在对话中即时查询数据图谱。通过 MCP 工具（`datalink_explore`）或 REST API 读操作完成。
- **离线管理**：建图、改图、调试等操作。通过 CLI 命令或 REST API 写操作完成。

| 层 | 接口类型 | 典型用途 |
|---|---|---|
| **在线检索** | MCP `datalink_explore`、REST API `/explore` 等 | Agent 查询数据：什么表可以 JOIN、某列是什么意思 |
| **离线管理** | CLI 命令、REST API `/add-table` 等 | 管理员建图、重建索引、调试图谱状态 |

MCP 默认只暴露 `datalink_explore`（检索），不再包含写操作。写操作通过 REST API 或 CLI 完成。

---

## CLI 命令

通过 `datalink` 命令调用。所有命令均支持 `--db` 指定数据库路径（默认 `datalink.db`）。

### 写操作

#### `datalink add-table` — 添加表/数据源

向图谱添加来自一个数据源的表。这是构建图谱的核心入口。

- 传 `--table` → 只添加指定的那张表
- **不传 `--table` → 添加该数据源的所有表**
- 在空图谱上调用时，效果等同于首次构建
- **已有表自动跳过**：如果要添加的表已存在于图谱中（同数据源路径 + 同表名），该表会被跳过而非重复注册。返回结果中会标注 `skipped_tables`。如需重新添加，请先 `remove-table` 删除再重新添加。

```bash
# 首次建图谱：添加整个 CSV 目录的所有表
datalink add-table --source ./data/

# 首次建图谱：从数据库添加所有表
datalink add-table --source "postgresql://user:pass@localhost/mydb"

# 增量添加：只加一张表
datalink add-table --source ./new_data/ --table new_orders

# 增量添加：添加整个新数据源的所有表
datalink add-table --source "postgresql://user:pass@localhost/another_db"
```

| 参数 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `--source` / `-s` | 是 | — | 数据源路径：CSV/Parquet 文件/目录，或数据库连接串 |
| `--table` / `-t` | 否 | `""` | 表名（空 = 添加所有表） |
| `--db` / `-d` | 否 | `datalink.db` | 图谱数据库路径 |

此命令通过 `DataLinkConfig.load()` 读取配置，`datalink_config.json` 中的阈值和 LLM 设置都会生效。

数据源类型自动检测：以 `postgresql://` / `postgresql+psycopg2://` / `mysql://` / `mysql+pymysql://` 等（支持 `dialect+driver://` 格式）开头 → 数据库；以 `.csv` 或目录结尾 → CSV；以 `.parquet` / `.pq` 结尾 → Parquet。

数据库驱动自动回退：如果连接串使用裸 scheme（如 `mysql://`）未指定 driver，SQLAlchemy 默认使用 `mysqlclient`（C 扩展）。若该驱动未安装，系统自动尝试已安装的替代驱动（`mysql://` → `mysql+pymysql://`，`postgresql://` → `postgresql+psycopg2://`）。建议直接使用 `dialect+driver://` 格式（如 `mysql+pymysql://`）以确保确定性。

数据库 schema 处理：
- PostgreSQL：默认使用 `public` schema；如需指定其他 schema，可在连接串的数据库名后加 `.schema_name`（如 `postgresql://user:pass@host/mydb.myschema`）。
- MySQL/MariaDB：不使用 PostgreSQL 式 schema，URL path 就是数据库名（如 `mysql://.../appdb`）。系统不会对 MySQL URL path 中的 dot 做 schema 拆分——连接串 `/appdb.codex_install_test` 不是合法的 MySQL 用法。要添加特定表，使用 `--table` 参数。
- SQLite：使用 SQLAlchemy 连接串，例如 `sqlite:////absolute/path/to/file.sqlite`（Linux 绝对路径 4 个斜杠）或 `sqlite:///C:/path/to/file.db`（Windows）。文件名中的 `.db` / `.sqlite` 不会被当成 schema；连接后 `schema_name` 自动为 `None`。表名含连字符等特殊字符时，COUNT/采样 SQL 会自动加引号。

#### `datalink rebuild` — 重建图谱

从已有数据源重建数据地图。**不需要传任何数据源信息**，系统会从 SQLite 中读取所有已有表的元数据。

三种重建模式：

- **`full`**（默认）：清空图谱，重走完整 pipeline（extract → profile → infer → map → embed）。这是原来的 rebuild 行为，现在也包括 embedding 向量构建。适用场景：数据源内容大幅变化、需要全面刷新。

- **`vec`**：只重建 embedding 向量索引。不改变图谱数据、不调用 LLM——仅用当前配置的 embedding 模型重新计算所有节点的可检索文本向量。适用场景：用户更换了 embedding 模型，需要更新向量索引。

- **`profile`**：重新统计所有 table/column 的 profile 值，并重建依赖 profile 的推断边（joinable、distribution_similar、semantic_synonym、correlated）。不涉及概念层（REPRESENTS/HAS_CONCEPT）、不调用 LLM。适用场景：数据源的数据量有变化但概念结构不需要更新——profile 变化可能影响推断边（如枚举值变了 → joinable overlap 变了 → 边可能增减），所以 profile 模式会一并重建这些边。

**安全性保证（mode=full）**：旧数据只在新 pipeline 成功完成后才会被清除。如果 pipeline 失败（如 LLM 超时），旧图谱数据完好保留，再次执行 rebuild 即可恢复。

```bash
# 完整重建（默认）
datalink rebuild

# 只重建向量索引
datalink rebuild --mode vec

# 只重算统计值
datalink rebuild --mode profile

# 指定数据库路径
datalink rebuild --db /path/to/graph.db
```

| 参数 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `--mode` / `-m` | 否 | `full` | 重建模式：`full`、`vec`、`profile` |
| `--db` / `-d` | 否 | `datalink.db` | 图谱数据库路径 |

此命令通过 `DataLinkConfig.load()` 读取配置。如果图谱中没有任何表，会报错提示先用 `add-table` 初始化。

`mode=vec` 需要配置了 embedding 模型才能使用，否则会报错提示先配置 `embedding.model`。

#### `datalink remove-table` — 删除表

从图谱中删除一张表及其所有列、边、Profile，可选清理孤立的 Concept/Entity。孤立清理分两阶段：先删无结构层 `represents` 锚定的 Concept，再删无存活 Concept 连接的 Entity（`has_concept` 边不算锚定）。

```bash
datalink remove-table --table orders
datalink remove-table --table "table:csv:orders"
```

| 参数 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `--table` / `-t` | 是 | — | 表名或完整表 ID（以 `table:` 开头） |
| `--db` / `-d` | 否 | `datalink.db` | 图谱数据库路径 |

传表名时会自动查找对应 ID。删除后自动清理引用已移除节点的 pending edges。

### 读操作

#### `datalink show` — 展示完整图谱

读取当前图谱数据库中的所有节点和边，以 JSON 格式输出。适合导出图谱数据、调试、或快速查看整个图谱状态。

```bash
datalink show
datalink show --db /path/to/graph.db
```

| 参数 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `--db` / `-d` | 否 | `datalink.db` | 图谱数据库路径 |

CLI 不做凭证遮蔽（终端输出只给操作者本人看）。

**注意**：`show` 是离线调试工具，不适合 agent 在对话中使用（输出量可能很大）。REST API `/show` 返回遮蔽后的 JSON。

以下读操作 CLI 命令与 REST API 端点一一对应。

#### `datalink explore` — 万能检索

**推荐优先使用。** 给一个 query，一次返回相关节点详情 + 关系链 + 数据指纹。对应 REST API `/explore`。

```bash
datalink explore "revenue customer_id orders"
datalink explore "email" --focus data_profile
datalink explore "how is orders connected to customers" --focus join_paths
```

| 参数 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `query` | 是 | — | 关键词、名称、自然语言描述 |
| `--focus` / `-f` | 否 | 均衡 | 聚焦方向：`join_paths`、`schema`、`data_profile` |
| `--max-nodes` / `-n` | 否 | 自动 | 详情节点上限（0 = 自动按项目大小） |
| `--db` / `-d` | 否 | `datalink.db` | 图谱数据库路径 |

#### `datalink search` — 搜索节点

按名称子串搜索节点，返回位置列表（无详情）。`explore` 通常更好。对应 REST API `/search`。

```bash
datalink search "customer" --type column --limit 20
```

| 参数 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `query` | 是 | — | 搜索关键词 |
| `--type` / `-t` | 否 | 全类型 | 过滤节点类型：`column`、`table`、`concept`、`entity` |
| `--limit` / `-l` | 否 | 10 | 最大结果数 |
| `--db` / `-d` | 否 | `datalink.db` | 图谱数据库路径 |

#### `datalink get-node` — 节点详情

获取单个节点的详细信息，包括邻接边和完整 Profile。对应 REST API `/get-node`。

节点 ID 支持短别名格式：`frpm.County Name`、`frpm`、`County Name` 等（自动解析为完整 ID）。

节点不存在时命令以退出码 1 终止（而非退出码 0），便于脚本和 Agent 判断。

边信息包含方向标识：出边显示为 `→`（当前节点指向对方），入边显示为 `←`（对方指向当前节点）。

对于表类型节点，`contains` 边（表→列）不再展开输出——列名列表已包含在 `column_ids` 属性中，展示了完整的列名。

```bash
datalink get-node column:csv:orders:customer_id
datalink get-node frpm.County Name
datalink get-node column:csv:orders:customer_id --no-edges
```

| 参数 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `node_id` | 是 | — | 节点 ID（支持完整 ID、遮蔽 ID、短别名） |
| `--edges` / `--no-edges` | 否 | 显示 | 是否包含邻接边信息 |
| `--db` / `-d` | 否 | `datalink.db` | 图谱数据库路径 |

#### `datalink path` — 两节点间路径

查找两个节点之间的连接路径（BFS，按置信度排序）。对应 REST API `/path`。

节点 ID 支持短别名格式：`frpm.County Name`、`frpm`、`County Name` 等（自动解析为完整 ID）。

```bash
datalink path --from column:csv:orders:customer_id --to column:csv:customers:id
datalink path --from col1 --to col2 --edge-types joinable,foreign_key
datalink path --from frpm.CDSCode --to schools.CDSCode --limit 5
```

| 参数 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `--from` | 是 | — | 起始节点 ID（支持完整 ID、遮蔽 ID、短别名） |
| `--to` | 是 | — | 目标节点 ID（同上） |
| `--depth` / `-d` | 否 | 3 | 最大路径深度 |
| `--limit` / `-l` | 否 | 3 | 最大返回路径数 |
| `--edge-types` | 否 | 全类型 | 逗号分隔的边类型（如 `joinable,foreign_key`） |
| `--db` | 否 | `datalink.db` | 图谱数据库路径 |

#### `datalink extract-subgraph` — 子图扩展

从指定节点按 hop 扩展子图，返回周围节点和边。对应 REST API `/extract-subgraph`。

```bash
datalink extract-subgraph column:csv:orders:customer_id --hops 2
datalink extract-subgraph "node1,node2,node3" --hops 1
```

| 参数 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `node_ids` | 是 | — | 逗号分隔的起始节点 ID |
| `--hops` / `-h` | 否 | 2 | 扩展层数 |
| `--db` / `-d` | 否 | `datalink.db` | 图谱数据库路径 |

#### `datalink info` — 图谱概览

显示图谱统计：节点/边/Profile 数量，按类型分布。对应 REST API `/info`。

```bash
datalink info
datalink info --db /path/to/graph.db
```

| 参数 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `--db` / `-d` | 否 | `datalink.db` | 图谱数据库路径 |

#### `datalink list-datasets` — 表列表

列出所有表/数据集及其基本统计（列数、边数）。对应 REST API `/list-datasets`。

```bash
datalink list-datasets
datalink list-datasets --db /path/to/graph.db
```

| 参数 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `--db` / `-d` | 否 | `datalink.db` | 图谱数据库路径 |

#### `datalink pending-edges` — 悬空边列表

列出引用尚未入库节点的边（典型：跨数据源 FK）。对应 REST API `/pending-edges`。

```bash
datalink pending-edges --type foreign_key
datalink pending-edges --node column:csv:orders:region
```

| 参数 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `--node` / `-n` | 否 | 全部 | 过滤涉及某节点的 pending edges |
| `--type` / `-t` | 否 | 全类型 | 过滤边类型（如 `foreign_key`、`joinable`） |
| `--limit` / `-l` | 否 | 50 | 最大结果数 |
| `--db` / `-d` | 否 | `datalink.db` | 图谱数据库路径 |

### 服务与配置

#### `datalink serve` — 启动 MCP Server

启动 MCP Server 供 AI Agent 连接。默认只暴露 `datalink_explore`（纯检索）。

```bash
# 默认 SSE transport（传统模式）
datalink serve --port 8080

# 推荐 streamable-http transport（更稳定）
datalink serve --port 8080 --transport streamable-http
```

| 参数 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `--port` / `-p` | 否 | 8080 | 监听端口 |
| `--db` / `-d` | 否 | `datalink.db` | 图谱数据库路径 |
| `--transport` / `-t` | 否 | `sse` | Transport 协议：`sse`（传统）或 `streamable-http`（推荐） |

#### `datalink api` — 启动 REST API Server

启动 REST API Server，提供所有 CLI 命令的 HTTP 等价接口。

```bash
# 默认端口 8081（避免和 MCP 的 8080 冲突）
datalink api

# 指定端口和绑定地址
datalink api --port 9000 --host 127.0.0.1
```

| 参数 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `--port` / `-p` | 否 | 8081 | 监听端口 |
| `--host` | 否 | `0.0.0.0` | 绑定地址（`127.0.0.1` = 仅本地） |

#### `datalink config` — 写配置

修改 `datalink_config.json`，仅传入有值的字段会被更新。

```bash
datalink config --llm-model deepseek-chat --llm-base-url https://api.deepseek.com/v1
datalink config --mcp-tools "datalink_search_nodes,datalink_get_node"
datalink config --db /data/my_graph.db
```

| 参数 | 必填 | 默认值 | 说明 |
|---|---|---|---|
| `--llm-model` | 否 | 不改 | LLM 模型名 |
| `--llm-api-key` | 否 | 不改 | LLM API Key（也可用 `OPENAI_API_KEY` 环境变量） |
| `--llm-base-url` | 否 | 不改 | LLM API 地址（OpenAI-compatible） |
| `--db` / `-d` | 否 | 不改 | 图谱数据库路径 |
| `--mcp-tools` / `-t` | 否 | 不改 | MCP 辅助工具白名单（逗号分隔全名，空字符串 = 仅核心工具） |

---

## REST API

通过 `datalink api` 启动 REST API server。所有端点名称与 CLI 命令保持一致。

### 凭证遮蔽

REST API 的所有检索端点默认启用凭证遮蔽（和 MCP 一致）。数据库连接串中的用户名和密码会被替换为 `***`。

`/show` 端点也默认遮蔽。`/config` 端点的 `GET` 响应中 API key 被遮蔽为 `***`。

### 写操作（离线管理）

#### `POST /add-table` — 添加表/数据源

与 CLI `datalink add-table` 等价。

```json
{
  "source": "./data/",
  "table": null,
  "source_type": "csv"
}
```

```json
{
  "source": "postgresql://user:pass@localhost/mydb",
  "table": "products",
  "source_type": "database",
  "schema_name": null
}
```

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `source` | string | 是 | — | 数据源路径或数据库连接串 |
| `table` | string | 否 | null | 指定单表名，null → 添加所有表 |
| `source_type` | string | 否 | `"csv"` | `"csv"`、`"parquet"`、`"database"` |
| `schema_name` | string | 否 | null | 数据库 schema 名。PostgreSQL 默认 `public` |

返回：JSON，含 `status`（`"success"` 或 `"skipped"`）、`added_tables`、`skipped_tables`、`stats`。

#### `POST /rebuild` — 重建图谱

与 CLI `datalink rebuild` 等价。

```json
{"mode": "full"}
```

```json
{"mode": "vec"}
```

```json
{"mode": "profile"}
```

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `mode` | string | 否 | `"full"` | `"full"`、`"vec"`、`"profile"` |

返回：JSON，含 `status` 和对应模式的统计信息。

#### `POST /remove-table` — 删除表

与 CLI `datalink remove-table` 等价。支持传表名（自动查找 ID）或完整表 ID。

```json
{
  "table_id": "orders",
  "cleanup_orphans": true
}
```

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `table_id` | string | 是 | — | 表名或完整表 ID |
| `cleanup_orphans` | bool | 否 | true | 是否清理孤立 Concept/Entity |

返回：JSON，含 `status`、`removed_columns`、`removed_orphans`、`stats`。

#### `GET /show` — 展示完整图谱

与 CLI `datalink show` 等价，但返回遮蔽后的 JSON。

无参数。

返回遮蔽后的 JSON：`{"nodes": [...], "edges": [...]}`。

### 读操作（在线检索）

#### `POST /explore` — 万能检索

与 CLI `datalink explore` 等价。**Agent 应优先调用此端点。**

```json
{
  "query": "revenue customer_id orders",
  "focus": "join_paths",
  "max_nodes": null
}
```

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `query` | string | 是 | — | 关键词、名称、自然语言描述 |
| `max_nodes` | int | 否 | null | 详情节点上限（null → 自动） |
| `focus` | string | 否 | null | `"join_paths"`、`"schema"`、`"data_profile"` 或 null |

返回：格式化文本（不是 JSON），和 MCP `datalink_explore` 返回格式一致。

#### `POST /search` — 搜索节点

```json
{
  "query": "customer",
  "node_type": "column",
  "limit": 10
}
```

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `query` | string | 是 | — | 搜索关键词 |
| `node_type` | string | 否 | null | `"column"`、`"table"`、`"concept"`、`"entity"` |
| `limit` | int | 否 | 10 | 最大结果数 |

返回：JSON 列表。

#### `POST /get-node` — 节点详情

```json
{
  "node_id": "column:csv:orders:customer_id",
  "include_edges": true
}
```

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `node_id` | string | 是 | — | 节点 ID（完整 ID、遮蔽 ID、短别名均可） |
| `include_edges` | bool | 否 | true | 是否包含邻接边 |

返回：JSON，含节点详情 + 边列表 + profile + pending edges。对于表类型节点，`contains` 边不再返回（列名列表已包含在 `column_ids` 属性中）。响应大小超过 25000 chars 时自动裁剪低置信度边。

#### `POST /path` — 两节点间路径

```json
{
  "source_id": "column:csv:orders:customer_id",
  "target_id": "column:csv:customers:id",
  "max_depth": 3,
  "limit": 3,
  "edge_types": null
}
```

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `source_id` | string | 是 | — | 起始节点 ID（支持短别名） |
| `target_id` | string | 是 | — | 目标节点 ID（支持短别名） |
| `max_depth` | int | 否 | 3 | 最大路径深度 |
| `limit` | int | 否 | 3 | 最大返回路径数 |
| `edge_types` | string | 否 | null | 逗号分隔的边类型 |

#### `POST /extract-subgraph` — 子图扩展

```json
{
  "node_ids": "column:csv:orders:customer_id,column:csv:orders:order_id",
  "max_hops": 2
}
```

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `node_ids` | string | 是 | — | 逗号分隔的起始节点 ID |
| `max_hops` | int | 否 | 2 | 扩展层数 |

#### `GET /list-datasets` — 表列表

无参数。返回 JSON 列表。

#### `POST /pending-edges` — 悬空边列表

```json
{
  "node_id": null,
  "edge_type": null,
  "limit": 50
}
```

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `node_id` | string | 否 | null | 过滤涉及某节点 |
| `edge_type` | string | 否 | null | 过滤边类型 |
| `limit` | int | 否 | 50 | 最大结果数 |

#### `GET /info` — 图谱概览

无参数。返回 JSON，含节点/边/Profile 统计。

### 配置

#### `GET /config` — 获取当前配置

返回 JSON（API key 被遮蔽为 `***`）。

#### `PATCH /config` — 更新配置

```json
{
  "llm_model": "deepseek-chat",
  "llm_base_url": "https://api.deepseek.com/v1",
  "embedding_model": "text-embedding-3-small"
}
```

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `llm_model` | string | 否 | LLM 模型名 |
| `llm_api_key` | string | 否 | LLM API Key |
| `llm_base_url` | string | 否 | LLM API 地址 |
| `graph_db_path` | string | 否 | 图谱数据库路径 |
| `mcp_tools` | string | 否 | MCP 辅助工具白名单 |
| `embedding_model` | string | 否 | Embedding 模型名 |
| `embedding_api_key` | string | 否 | Embedding API Key |
| `embedding_base_url` | string | 否 | Embedding API 地址 |

返回更新后的配置 JSON（API key 遮蔽）。仅传入有值的字段会被更新。

---

## MCP 工具

MCP Server 通过 `datalink serve` 启动，供 AI Agent 通过 MCP 协议调用。

**默认只暴露 `datalink_explore`**（万能检索）。写操作（add_table、rebuild、remove_table）和 show 已移至 REST API。

### 凭证遮蔽

所有 MCP 检索工具默认启用凭证遮蔽（`mask_credential=True`）。数据库连接串中的用户名和密码会被替换为 `***`，防止在 Agent 输出中泄露敏感信息。

支持裸 scheme 和 `dialect+driver` 格式（如 `postgresql+psycopg2://`、`mysql+pymysql://`）。

```
原始：postgresql://admin:s3cret@db.example.com:5432/mydb
遮蔽：postgresql://***:***@db.example.com:5432/mydb
```

遮蔽后的 ID 可以直接作为后续接口的输入参数使用——系统会自动将遮蔽 ID 反查为真实 ID。

### 返回格式

所有 MCP 工具返回**自然语言文本**（不是 JSON），以节省 token 消耗。文本中的节点引用使用短格式（如 `frpm.County Name`）而非完整 URI ID。每个返回值末尾附一个 ID 映射表，将短名映射回完整 ID，方便后续工具调用时回传 node_id 参数。

### 短别名格式

所有需要 node_id 参数的 MCP 工具支持短别名输入，无需使用完整 URI ID：

| 格式 | 示例 | 说明 |
|---|---|---|
| 完整 ID | `column:sqlite:///.../db.sqlite:frpm:County Name` | 原始格式 |
| 遮蔽 ID | `column:sqlite:///***:***@.../db.sqlite:frpm:County Name` | 凭证遮蔽后，自动反查 |
| 短别名 | `frpm.County Name` | table.column 格式，自动解析 |
| 裸名 | `frpm`、`County Name` | 自动搜索匹配 |

无效格式：`profile:column:...`（Profile 不是独立节点）、带错拼的缩写（如 `schools.FundingType` vs `schools.Funding Type`）。

解析失败时返回格式化提示，包含可用的 ID 格式和推荐的替代工具。

### 默认暴露

#### `datalink_explore` — 万能检索

**Agent 应优先调用此工具回答所有数据问题。**

```json
{
  "query": "revenue customer_id orders",
  "focus": "join_paths",
  "max_nodes": null
}
```

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `query` | string | 是 | 关键词、名称或自然语言 |
| `max_nodes` | int | 否 | 详情节点上限（null → 自动） |
| `focus` | string | 否 | `"join_paths"`、`"schema"`、`"data_profile"` 或 null |
| `mask_credential` | bool | 否 | 是否遮蔽凭证（默认 `true`） |

返回：格式化文本字符串（不是 JSON），按表分组，每列自包含。

### 可选暴露（辅助工具）

以下 6 个工具默认不注册，Agent 不可见。需要通过配置启用。

#### 启用方式

两种途径，**环境变量优先**：

1. 环境变量（临时覆盖，适合 CI）：
   ```bash
   DATALINK_MCP_TOOLS=datalink_search_nodes,datalink_get_node datalink serve
   ```

2. 配置文件（持久化）：
   ```json`
   { "mcp_tools": "datalink_search_nodes,datalink_get_node,datalink_find_paths" }
   ```
   或 CLI：
   ```bash
   datalink config --mcp-tools "datalink_search_nodes,datalink_get_node"
   ```

#### 辅助工具列表

| 工具名 | 能力 | 说明 |
|---|---|---|
| `datalink_search_nodes` | 按名称/semantic_type 搜索节点 | 返回位置列表（无详情），`explore` 通常更好 |
| `datalink_get_node` | 单节点详情 + 全部邻接边 + 完整 Profile | 需要看某一列完整 profile 时使用 |
| `datalink_find_paths` | 两节点间路径（可指定 edge_type，默认 limit=3） | 需要精确 JOIN 规划时使用 |
| `datalink_extract_subgraph` | 从指定节点按 hop 扩展子图 | 需要精确子图导出时使用 |
| `datalink_list_datasets` | 所有表及统计概览 | 需要全局"项目里有什么数据"时使用 |
| `datalink_list_pending_edges` | 悬空边列表 | 调试跨源 FK 状态时使用 |

各辅助工具参数详见原文档。

---

## 接口对照表

| 功能 | CLI 命令 | REST API | MCP 工具 | 默认可用 |
|---|---|---|---|---|
| 万能检索 | `datalink explore` | `POST /explore` | `datalink_explore` | ✅ MCP / REST |
| 展示完整图谱 | `datalink show` | `GET /show` | — | ✅ CLI / REST |
| 添加表/数据源 | `datalink add-table` | `POST /add-table` | — | ✅ CLI / REST |
| 重建图谱 | `datalink rebuild` | `POST /rebuild` | — | ✅ CLI / REST |
| 删除表 | `datalink remove-table` | `POST /remove-table` | — | ✅ CLI / REST |
| 搜索节点 | `datalink search` | `POST /search` | `datalink_search_nodes` | ❌ MCP需启用 |
| 节点详情 | `datalink get-node` | `POST /get-node` | `datalink_get_node` | ❌ MCP需启用 |
| 路径发现 | `datalink path` | `POST /path` | `datalink_find_paths` | ❌ MCP需启用 |
| 子图扩展 | `datalink extract-subgraph` | `POST /extract-subgraph` | `datalink_extract_subgraph` | ❌ MCP需启用 |
| 图谱概览 | `datalink info` | `GET /info` | — | ✅ CLI / REST |
| 表列表 | `datalink list-datasets` | `GET /list-datasets` | `datalink_list_datasets` | ❌ MCP需启用 |
| 悬空边 | `datalink pending-edges` | `POST /pending-edges` | `datalink_list_pending_edges` | ❌ MCP需启用 |
| 启动 MCP | `datalink serve` | — | — | ✅ CLI |
| 启动 REST API | `datalink api` | — | — | ✅ CLI |
| 配置 | `datalink config` | `GET/PATCH /config` | — | ✅ CLI / REST |

---

## 配置文件

`datalink_config.json`（当前目录），可通过 `datalink config` 命令或手动编辑。

```json
{
  "llm": {
    "model": "gpt-4o",
    "api_key": "",
    "base_url": "https://api.openai.com/v1",
    "temperature": 0.1,
    "max_tokens": 16384,
    "timeout": 120.0
  },
  "embedding": {
    "model": "",
    "api_key": "",
    "base_url": "",
    "timeout": 60.0,
    "similarity_threshold": 0.75
  },
  "merge_llm_temperature": 0.0,
  "merge_batch_interval": 10,
  "mapping_batch_size": 15,
  "graph_db_path": "datalink.db",
  "sample_size": 1000,
  "confidence_threshold": 0.3,
  "joinable_overlap_threshold": 0.1,
  "correlation_threshold": 0.5,
  "mcp_tools": ""
}
```

| 字段 | 默认值 | 说明 |
|---|---|---|
| `llm.model` | `"gpt-4o"` | LLM 模型名 |
| `llm.api_key` | `""` | API Key（也可用 `OPENAI_API_KEY` 环境变量） |
| `llm.base_url` | `"https://api.openai.com/v1"` | OpenAI-compatible API 地址 |
| `llm.temperature` | `0.1` | LLM 温度 |
| `llm.max_tokens` | `16384` | LLM 最大 token 数 |
| `llm.timeout` | `120.0` | LLM API HTTP 超时秒数（自托管网关 504 时增大此值） |
| `embedding.model` | `""` | Embedding 模型名（空=跳过向量检索和粗筛） |
| `embedding.api_key` | `""` | 空=回退到 `llm.api_key` |
| `embedding.base_url` | `""` | 空=回退到 `llm.base_url` |
| `embedding.similarity_threshold` | `0.75` | Embedding 粗筛 cosine 相似度阈值（同时用于向量检索的最低相似度过滤） |
| `embedding.timeout` | `60.0` | Embedding API HTTP 超时秒数 |

**Embedding 用途**：配置后生效于两个场景：
1. **图谱消歧**（merge_with_existing）：embedding 预筛候选合并对，降低 LLM 调用开销（原有功能）
2. **混合检索**（search_nodes / explore）：向量相似检索 + 全文检索合并，提升召回率（新功能）

配置 `embedding.model` 后需运行 `datalink rebuild --mode vec`（或在 `add-table`/`rebuild` 时自动构建）来生成向量索引。更换模型后需再次 `rebuild --mode vec` 以重建向量。
| `merge_llm_temperature` | `0.0` | merge LLM 调用温度（低=更确定性） |
| `merge_batch_interval` | `10` | 分批推理时每 N 个 batch 才做一次 merge |
| `mapping_batch_size` | `15` | LLM mapping 每批列数（减小如 5 可降低单次推理时间和 prompt 大小，适合网关 timeout 较短的情况） |
| `graph_db_path` | `"datalink.db"` | SQLite 数据库路径 |
| `sample_size` | `1000` | 采样行数 |
| `confidence_threshold` | `0.3` | 推断边最低置信度 |
| `joinable_overlap_threshold` | `0.1` | JOIN 推断重叠率阈值 |
| `correlation_threshold` | `0.5` | 相关系数阈值 |
| `mcp_tools` | `""` | MCP 辅助工具白名单（逗号分隔全名，空 = 仅核心工具） |

`mcp_tools` 环境变量 `DATALINK_MCP_TOOLS` 优先级高于配置文件，适合临时覆盖。
