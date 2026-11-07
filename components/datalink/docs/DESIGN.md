# DataLink 设计文档

## 1. 项目概述

**DataLink** 是一个面向数据 Agent 的**统一数据地图系统**。它将数据结构、画像、业务概念和关联线索组织为可查询的关系网络，为数据发现、分析规划和 Agent 工具调用提供上下文。

项目的长期设计目标是接入表格、文档、幻灯片、图片等多种数据类型，使不同类型保留各自结构，同时共享关系表达、存储和查询能力。**当前版本的实际实现主要面向表格数据**，支持关系型数据库、CSV 和 Parquet，核心节点仍是 Table/Column；非表格类型需要继续补充连接器、节点模型和处理组件。

核心能力：

- 从 CSV/Parquet 文件或关系型数据库自动构建数据地图
- 维护分层表达：数据结构层（Table/Column）+ 业务概念层（Concept/Entity）
- 推断列之间的隐式关系（可 JOIN、同义、分布相似、统计相关）
- 通过 CLI 和 MCP Server 供人类与 AI Agent 查询

技术栈：Python 3.10+、Pydantic、Pandas、SQLAlchemy、SQLite、Typer、Rich、MCP（FastMCP）、OpenAI/Anthropic LLM。

---

## 2. 核心设计理念

### 2.1 分层数据地图

| 层级 | 节点类型 | 含义 | 示例 |
|------|----------|------|------|
| **数据结构层** | `TableNode` | 表/数据集 | `orders`, `customers` |
| **数据结构层** | `ColumnNode` | 列/字段 | `customer_id`, `amount` |
| **业务概念层** | `ConceptNode` | 可度量的概念/属性 | `revenue`, `person_identifier` |
| **业务概念层** | `EntityNode` | 可识别的实体/集合 | `customer`, `order` |

数据结构层来自 schema 与采样数据；业务概念层来自元数据注释或 LLM 推断，并通过 `represents` / `has_concept` 边与结构节点连接。这种分层方式用于统一组织数据，并不限制未来只能采用 Table/Column 节点。

### 2.2 边的分类体系

边按来源与用途分为四类（见 `models/edge.py`）：

| 类别 | 说明 | 典型边类型 | 置信度 |
|------|------|------------|--------|
| **A. 结构显式** | Schema/元数据直接给出 | `contains`, `foreign_key` | 1.0 |
| **B. 推断关系** | 算法推断 | `joinable`, `semantic_synonym`, `correlated`, `distribution_similar` | 0.0–1.0 |
| **C. 上下文** | 使用行为——从 Agent 执行轨迹中提取的关系（规划中） | `co_occurs_in_query`, `frequently_joined` | — |
| **D. 跨层关联** | 数据结构 ↔ 业务概念 | `represents`, `has_concept` | 0.5–1.0 |

推断边会按 `confidence_threshold`（默认 0.3）过滤后入库。

**边关系的双向构建：** DataLink 的边关系最终形态来自两个方向。一是*静态治理*——通过 schema 提取、内容画像、算法推断和概念映射，理解数据长什么样、*可能*存在什么关系（类别 A/B/D）。二是*轨迹驱动*——当 Agent 实际使用数据时，其执行轨迹（哪些表被 JOIN、哪些列被一起查询、走了哪些路径）揭示静态分析无法发现的真实关系（类别 C）。两者相辅相成：静态治理提供基线地图，轨迹证据用真实使用行为来丰富和校准地图。类别 C 的边类型尚未实现，将在后续版本中从 Agent 的 `datalink_explore` 调用和 SQL 生成记录中提取。

**Pending edges（悬空边）：** 当边的 `source_id` 或 `target_id` 对应的节点尚未入库（典型：跨数据源 FK 的目标表还没加入），边不会丢弃而是存入 `pending_edges` 表，标记 `missing_endpoints`（如 `["target"]`）。检索时这些边作为 `suggested_edges` 展示，不参与路径发现和子图扩展。当新数据源通过 `add_table` 加入后，Pipeline 自动调用 `resolve_pending_edges()` 将两端节点都存在的 pending 边移入 `edges` 主表。

### 2.3 设计原则

1. **统一地图，不抹平类型差异**：不同数据类型可以使用不同的节点结构和画像方法，但通过统一的关系模型、存储和查询接口协作。
2. **扩展点前置**：Connector、Extractor、Profiler 和关系推断按数据类型逐步扩展，公共能力尽量沉淀在 Graph、Retrieval、CLI、REST 和 MCP 层。
3. **采样而非全量扫描**：当前表格实现默认采样 1000 行做 Profiling，平衡速度与准确性。
4. **概念归一**：当前所有列通过 LLMMapper 推断 Concept/Entity；有 comment 的列将 comment 作为额外信号，以提高映射质量。
5. **增量维护**：支持 `add_table` / `rebuild` / `remove_table`，不必每次全量重建。
6. **Agent 友好**：通过 MCP、REST 和 CLI 提供搜索、路径发现和一键探索等能力。
7. **双向关系构建**：边关系既来自静态治理（schema、画像、推断），也来自 Agent 使用轨迹（哪些表被 JOIN、哪些列被一起查询）。静态治理提供基线地图，轨迹证据丰富和校准地图——两者相辅相成。当前只实现了静态治理方向，轨迹驱动方向将在路线图中推进。

---

## 3. 系统架构

```
┌─────────────────────────────────────────────────────────────────┐
│                        用户 / AI Agent                           │
└──────────────┬──────────────────────────────┬───────────────────┘
               │ CLI (Typer)                  │ MCP Server (FastMCP)
               ▼                              ▼
┌──────────────────────────────────────────────────────────────────┐
│                     builder/pipeline.py                          │
│                   BuildPipeline (编排层)                         │
└──┬────────┬────────┬────────┬────────┬────────┬────────┬─────────┘
   │        │        │        │        │        │        │
   ▼        ▼        ▼        ▼        ▼        ▼        ▼
connector   extractor     profiler  inferrer  mapper   graph
   │        │        │        │        │        │
   │        │        │        │        │        ├── storage.py  (SQLite CRUD)
   │        │        │        │        │        └── retrieval.py (查询 API)
   ▼        ▼        ▼        ▼        ▼
 CSV/    Table/    Column    Join/    Concept/
 DB      Column    Profile   Synonym  Entity
         节点      指纹       等推断   映射
```

### 目录结构

```
DataLink/
├── src/datalink/
│   ├── cli/           # 命令行入口
│   ├── mcp/           # MCP Server（Agent 集成）
│   ├── builder/       # 构建流水线编排
│   ├── connector/     # 数据源连接（文件/数据库）
│   ├── extractor/     # 结构层节点提取
│   ├── profiler/      # 列统计指纹
│   ├── inferrer/      # 隐式关系推断
│   ├── mapper/        # 概念层映射
│   ├── graph/         # 存储 + 检索
│   ├── models/        # Pydantic 数据模型
│   └── config.py      # 全局配置
├── tests/             # 单元测试（每模块对应）
├── datalink_config.json
└── pyproject.toml
```

---

## 4. 构建流水线（Build Pipeline）

`BuildPipeline`（`builder/pipeline.py`）是核心编排器，完整构建分 6 步：

以下步骤描述的是**当前表格数据实现**。未来接入其他类型时，可沿用相同职责划分，但需要为该类型实现适配组件，不能假设现有 `TabularExtractor`、`TabularProfiler` 和列级 Inferrer 可以直接复用。

```mermaid
flowchart LR
    A[1. Connect<br/>连接数据源] --> B[2. Extract<br/>提取结构节点]
    B --> C[3. Profile<br/>列统计指纹]
    C --> D[4. Infer<br/>推断隐式关系]
    D --> E[5. Map<br/>概念层映射]
    E --> F[6. Store<br/>写入 SQLite]
```

### Step 1: Connect（连接数据源）

- `FileConnector`：CSV/Parquet，每个文件视为一张表
- `DatabaseConnector`：SQLAlchemy 内省 schema、FK、comment，并采样数据
- 输出：`DatasourceInfo`（tables + sample_data）

### Step 2: Extract（提取结构层）

`TabularExtractor` 从 `DatasourceInfo` 生成：

- `TableNode`：ID 格式 `table:{source}:{table_name}`
- `ColumnNode`：ID 格式 `column:{source}:{table}:{column}`
- 显式边：`contains`（表→列）、`foreign_key`（列→列）

### Step 3: Profile（列指纹）

`TabularProfiler` 对每列计算 `ColumnProfile`：

- 基础统计：null_rate、cardinality、unique_rate
- 类型检测：integer / float / string / datetime 等
- 字段角色：email_address、identifier、monetary_value 等（规则 + 正则）
- 数值/字符串统计、top_values、直方图、sample_values（供 LLM 使用）

### Step 4: Infer（推断隐式关系）

| Inferrer | 边类型 | 逻辑 |
|----------|--------|------|
| `JoinableInferrer` | `joinable` | 跨表列值域重叠率 ≥ 阈值 |
| `SynonymInferrer` | `semantic_synonym` | 同 semantic_type / 列名同义词组 |
| `DistributionInferrer` | `distribution_similar` | 数值/分类分布相似度 |
| `CorrelationInferrer` | `correlated` | 仅对已 joinable 的数值列算 Pearson 相关 |

### Step 5: Map（概念层映射）

- `LLMMapper`：**所有列**统一走 LLM 推断 Concept/Entity（有 comment 的列将 comment 作为额外信号带入 prompt）
- `add_table` 时新增 Concept/Entity 通过 `merge_with_existing()` 与已有节点做消歧合并：**Embedding 粗筛 + LLM 精判**两阶段模型推理 + 边重定向 + 去重
  - Embedding 粗筛（可选）：计算新旧节点 cosine 相似度，≥阈值的配对进入候选列表
  - LLM 精判（必选）：候选列表 + 仅相关已有节点发给 LLM 确认合并决策（embedding 粗筛过滤掉了不相似的已有节点，不再全量注入）
  - 未配置 embedding 模型时全量已有节点注入 LLM 判断

### Step 6: Store（持久化）

`GraphStorage` 写入 SQLite（`datalink.db`），并记录 build 元数据。

### 构建模式

Pipeline 提供三种构建模式 + 三种重建子模式：

- **`add_datasource` / `add_table`**（添加表/数据源）：向图谱添加来自一个数据源的表。不指定表名时处理该数据源的所有表；在空图谱上调用等同于首次构建。**去重校验**：添加前会检查图谱中已存在的表（通过 table ID 匹配，ID = `table:{source}:{table_name}`），已存在的表自动跳过而非重复注册。返回结果中标注 `added_tables`（新添加的）和 `skipped_tables`（跳过的）。如需重新添加已有表，先 `remove_table` 再 `add_datasource`。添加后自动为新节点构建 embedding 向量（如果配置了 embedding 模型）。CLI `datalink add-table`、MCP `add_table`。文件路径在入口处自动 resolve 为绝对路径，确保节点 ID 全局唯一。
- **`rebuild`**（重建）：从 SQLite 中读取所有已有 TableNode 的元数据，自动重新连接数据源。支持三种模式（`--mode/-m`）：
  - **`full`**（默认）：重走完整 pipeline（extract → profile → infer → map → embed）。安全性保证：compute-first → clear-last 模式，旧数据只在新 pipeline 成功后才清除，失败则旧图谱完好保留。CLI `datalink rebuild`、MCP `rebuild(mode="full")`。
  - **`vec`**：只重建 embedding 向量索引。不改变图谱数据、不调用 LLM。适用场景：更换了 embedding 模型配置。CLI `datalink rebuild --mode vec`、MCP `rebuild(mode="vec")`。
  - **`profile`**：重新统计所有 table/column 的 profile 值，并重建依赖 profile 的推断边（joinable、distribution_similar、semantic_synonym、correlated）。不涉及概念层、不调用 LLM。适用场景：数据源数据量变化但概念结构不变。CLI `datalink rebuild --mode profile`、MCP `rebuild(mode="profile")`。
- **`remove_table`**（删除）：级联删除表/列/边/Profile/embedding，可选清理孤立 Concept/Entity；同时清理引用已移除节点的 pending edges。孤立概念节点清理分两阶段：Stage 1 删除无结构层 `represents` 锚定的 Concept；Stage 2 删除无存活 Concept 连接的 Entity。`has_concept` 边（概念层内部）不算锚定。CLI `datalink remove-table`、MCP `remove_table`。

Pipeline 内部还有一个 `init_build` 方法（先 clear_all 再调 add_datasource），供需要显式清空的场景使用，但 CLI/MCP 入口不需要它——空图谱上 `add-table` 自然就是初始构建。

---

## 5. 模块详解

### 5.1 connector — 数据源连接

| 文件 | 职责 |
|------|------|
| `base.py` | 抽象接口：`connect`, `get_datasource_info`, `get_sample_data`, `disconnect` |
| `file.py` | CSV/Parquet，Pandas 读入，无 FK |
| `database.py` | SQLAlchemy 内省，支持 PostgreSQL/MySQL/SQLite 等。**Driver fallback**：裸 scheme（`mysql://`）自动尝试 `pymysql` 等替代驱动；MySQL/MariaDB/SQLite 不做 URL path dot 拆分，且默认 `schema_name` 清为 `None`（避免 SQLite 查询 `public.sqlite_master`） |

### 5.2 extractor — 结构提取

`tabular.py`：将 `TableInfo` / `ColumnInfo` / `ForeignKeyInfo` 转为图谱节点与显式边，ID 规则稳定可复现。

### 5.3 profiler — 统计分析

`tabular.py`：基于采样数据生成 `ColumnProfile`，是 inferrer 与 mapper 的共同输入。

### 5.4 inferrer — 关系推断

四个独立 Inferrer，可单独测试与调参（阈值在 `DataLinkConfig` 中配置）。

### 5.5 mapper — 概念映射

- **LLMMapper**：所有列统一走 LLM 推理，有 comment 的列将 comment 作为额外信号带入 prompt；批量分析列名、类型、统计、样本值，输出 JSON 结构化 Concept/Entity；`add_table` 时通过 `merge_with_existing` 与已有 Concept/Entity 做消歧合并（Embedding 粗筛 + LLM 精判两阶段模型推理）

### 5.6 graph — 存储与检索

**storage.py**（SQLite）：

- 表：`nodes`, `edges`, `column_profiles`, `metadata`, `pending_edges`, `node_embeddings`
- CRUD、批量写入、`remove_table`、孤立节点清理、`remove_edges_by_types`、embedding CRUD、`get_graph_stats`

**retrieval.py**（检索 API）：

1. `explore` — **万能检索入口**（query → 格式化文本，内含节点详情 + 关系链 + 数据指纹）
   - 内部组合调用 search / get_node / find_paths / profile 等
   - 多维度匹配 + 沿边扩展召回
   - 自适应输出预算
   - focus 参数调节深度
2. `search_nodes` — 名称/semantic_type 搜索 + 向量相似检索（混合检索，embedding 未配置时退化为纯全文）（辅助方法，不默认暴露给 agent）
3. `get_node` — 节点详情 + 邻接边 + Profile（辅助方法）
4. `find_paths` — BFS 路径发现（辅助方法）
5. `extract_subgraph` — 从指定节点按 hop 扩展子图（辅助方法）
6. `list_datasets` — 列出所有表及统计（辅助方法）

### 5.7 cli — 命令行

CLI 命令与 MCP 工具一一对应：

| 命令 | 功能 | 对应 MCP |
|------|------|----------|
| `datalink add-table` | 添加表/数据源 | `add_table` |
| `datalink rebuild` | 重建图谱 | `rebuild` |
| `datalink remove-table` | 删除表 | `remove_table` |
| `datalink explore` | 万能检索 | `datalink_explore` |
| `datalink search` | 搜索节点 | `datalink_search_nodes` |
| `datalink get-node` | 节点详情 | `datalink_get_node` |
| `datalink path` | 两节点间路径 | `datalink_find_paths` |
| `datalink extract-subgraph` | 子图扩展 | `datalink_extract_subgraph` |
| `datalink info` | 图谱概览（全局统计） | — |
| `datalink list-datasets` | 表列表 | `datalink_list_datasets` |
| `datalink pending-edges` | 悬空边列表 | `datalink_list_pending_edges` |
| `datalink serve` | 启动 MCP Server | — |
| `datalink config` | 写配置 | — |

### 5.8 mcp — Agent 集成

基于 FastMCP，支持两种 transport 协议：
- **SSE**（传统，默认） — 长连接流式推送
- **streamable-http**（推荐） — 更稳定，适合长耗时操作

启动方式：`datalink serve --transport streamable-http`

默认暴露（命名与 CLI 一致）：

- `datalink_explore` — 万能检索，一个调用回答整个数据问题
- `add_table` — 添加表/数据源（table 可选，null 则加全部；空图谱上等于首次构建；已有表自动跳过）
- `rebuild` — 重建图谱（三种模式：full/vec/profile；pipeline 失败时旧数据保留）
- `remove_table` — 删除表

可选暴露（通过 `DATALINK_MCP_TOOLS` 环境变量，逗号分隔全名）：

- `datalink_search_nodes` — 精确名称搜索
- `datalink_get_node` — 单节点详情
- `datalink_find_paths` — 路径发现
- `datalink_extract_subgraph` — 子图扩展
- `datalink_list_datasets` — 表概览
- `datalink_list_pending_edges` — 悬空边列表

示例：`DATALINK_MCP_TOOLS=datalink_search_nodes,datalink_get_node`

Agent 可通过 MCP 的 `datalink_explore` 自主探索数据关系、规划 JOIN 路径，一次调用即可获得完整上下文。

---

## 6. 数据模型

### 6.1 核心模型（`models/`）

```
Node (基类)
├── ColumnNode   — table_id, dtype, semantic_type, profile_id, comment
├── TableNode    — source, source_type, row_count, column_ids
├── ConceptNode  — description, unit, dimension
└── EntityNode   — description

Edge — source_id, target_id, type, confidence, properties

ColumnProfile — 列统计指纹（dtype, semantic_type, 分布, top_values 等）

DatasourceConfig / DatasourceInfo — 连接配置与提取结果
```

### 6.2 存储 Schema（`graph/schema.sql`）

- **nodes**：统一存四类节点，`properties` 为 JSON
- **edges**：带 `confidence`，**无 FK 级联删除**（`INSERT OR REPLACE` on nodes 会触发内部 DELETE+INSERT，级联会破坏已有边关系；边清理由 `remove_table` / `cleanup_orphaned_semantic_nodes` 显式处理）
- **column_profiles**：与 column 节点 1:1，同样无 FK 级联（Profile 清理由 `remove_table` 显式处理）
- **node_embeddings**：node_id → embedding_vector (float32 BLOB) + embedding_model + searchable_text，独立于主表，`rebuild --mode vec` 时重建
- **metadata**：构建元信息

索引覆盖 type、name、source/target、confidence 等常见查询。

---

## 7. 配置说明

配置文件：`datalink_config.json`（也可通过 `DataLinkConfig.load()` 加载）

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `graph_db_path` | `datalink.db` | SQLite 数据库文件名（裸文件名或相对路径自动解析到 `~/.datalink/storage/`，绝对路径原样使用） |
| `sample_size` | 1000 | 采样行数 |
| `confidence_threshold` | 0.3 | 推断边最低置信度 |
| `joinable_overlap_threshold` | 0.1 | JOIN 推断重叠率阈值 |
| `correlation_threshold` | 0.5 | 相关系数绝对值阈值 |
| `llm.model` | `gpt-4o` | LLM 模型名 |
| `llm.api_key` | — | 也可从 `OPENAI_API_KEY` 环境变量读取 |
| `llm.base_url` | `https://api.openai.com/v1` | OpenAI-compatible API 地址 |
| `llm.timeout` | `120.0` | LLM API HTTP 超时秒数（自托管网关 504 时增大此值） |
| `embedding.model` | `""` | Embedding 模型名（空=跳过向量检索和粗筛） |
| `embedding.api_key` | `""` | 空=回退到 `llm.api_key` |
| `embedding.base_url` | `""` | 空=回退到 `llm.base_url` |
| `embedding.similarity_threshold` | `0.75` | Embedding 粗筛 cosine 相似度阈值（同时用于向量检索最低相似度过滤） |
| `embedding.timeout` | `60.0` | Embedding API HTTP 超时秒数 |

**Embedding 用途**：配置后生效于两个场景：
1. **图谱消歧**（merge_with_existing）：embedding 预筛候选合并对，降低 LLM 调用开销（原有功能）
2. **混合检索**（search_nodes / explore）：向量相似检索 + 全文检索合并，提升召回率（新功能）

配置 `embedding.model` 后需运行 `datalink rebuild --mode vec`（或在 `add-table`/`rebuild` 时自动构建）来生成向量索引。更换模型后需再次 `rebuild --mode vec` 以重建向量。
| `merge_llm_temperature` | `0.0` | merge LLM 调用温度（低=更确定性） |
| `merge_batch_interval` | `10` | 分批推理时每 N 个 batch 才做一次 merge（1=每个 batch 后都 merge） |
| `mapping_batch_size` | `15` | LLM mapping 每批列数（减小如 5 可降低单次推理时间，适合网关 timeout 短的情况） |

---

## 8. 凭证遮蔽

DataLink 的节点 ID 和 `source` 属性中会嵌入数据库连接串（如 `postgresql://admin:s3cret@host/db`）。当 DataLink 作为概念层供 Agent 使用时，这些凭证不应该出现在 Agent 的输出中。

### 8.1 遵循方案

采用 **方案 A**：输出层遮蔽 + 遮蔽 ID → 真实 ID 反查映射。

- 不修改 ID 生成和存储方式——数据库中始终使用真实 ID
- 所有检索接口加 `mask_credential=True` 参数，在输出时自动遮蔽连接串中的 `user:pass@` 部分
- 支持 `dialect+driver` 格式（如 `mysql+pymysql://`、`postgresql+psycopg2://`）的 URL 遮蔽
- 遮蔽后的 ID 可以作为后续接口的输入——系统维护 `{masked_id → real_id}` 映射并自动反查
- CLI 命令不做输出遮蔽（人用终端），但支持接收遮蔽 ID 作为输入

### 8.2 遮蔽范围

`mask_result()` 递归遍历整个输出结构，遮蔽以下字段中的凭证：

| 字段类型 | 具体字段 | 遮蔽方法 |
|---|---|---|
| ID 字段 | `id`, `source_id`, `target_id`, `other_id`, `table_id`, `profile_id` | `mask_id()` — 识别 ID 格式并精确遮蔽 source 段 |
| 凭证字段 | `source`, `connection_string` | `mask_credentials()` — 遮蔽 userinfo 部分 |
| ID 列表字段 | `column_ids` | 元素逐个 `mask_id()` |
| 嵌套 dict | `properties`, `other_node` | 递归 `mask_result()` |
| 其他字符串值 | 任意 key 的字符串值 | 兜底检测：如果包含 DB URL 模式，用 `_mask_embedded_urls()` 遮蔽 |

### 8.3 遵循规则

```
postgresql://admin:s3cret@db.example.com:5432/mydb → postgresql://***:***@db.example.com:5432/mydb
postgresql+psycopg2://admin:s3cret@host/db → postgresql+psycopg2://***:***@host/db
mysql+pymysql://root:pass@localhost/testdb → mysql+pymysql://***:***@localhost/testdb
postgresql://user@host/db → postgresql://***@host/db
sqlite:///path/to/db.sqlite → 不变（无 userinfo）
/home/user/data/orders.csv → 不变（不是 DB URL）
```

节点 ID 中对应的遮蔽效果：

```
table:postgresql://admin:s3cret@host/db:orders → table:postgresql://***:***@host/db:orders
column:postgresql://admin:s3cret@host/db:orders:col → column:postgresql://***:***@host/db:orders:col
table:/home/user/data:orders → 不变
```

### 8.4 实现位置

| 文件 | 作用 |
|---|---|
| `datalink/utils/credential.py` | `mask_credentials()`, `mask_id()`, `mask_result()`, `is_masked_id()`, `build_id_mapping()`, `resolve_masked_id()` |
| `datalink/graph/retrieval.py` | 所有公共检索方法加 `mask_credential` 参数；构造时构建 ID 映射；输入参数自动反查 |
| `datalink/mcp/server.py` | 所有检索 MCP 工具加 `mask_credential=True` 参数，传递给 retrieval |

### 8.5 默认值

| 调用方 | `mask_credential` 默认值 |
|---|---|
| MCP 工具 | `True`（Agent 场景安全优先） |
| CLI 命令 | 输出不做遮蔽（传 `mask_credential=False` 给 retrieval），但输入参数支持接收遮蔽 ID |
| `GraphRetrieval` 公共方法 | `True` |

---

## 9. 快速上手指南

### 9.1 环境准备

```bash
uv sync
```

### 9.2 构建图谱

```bash
# 从 CSV 目录构建（首次建图谱 = add-table 不传 --table）
datalink add-table --source ./data/

# 从数据库构建
datalink add-table --source "postgresql://user:pass@localhost/mydb"

datalink info
datalink search "customer"
```

### 9.2 重建图谱（数据源内容有变化时）

```bash
# 完整重建（默认）
datalink rebuild

# 只重建向量索引（更换了 embedding 模型时）
datalink rebuild --mode vec

# 只重算统计值 + 依赖统计的推断边（数据量变化但业务含义不变时）
datalink rebuild --mode profile
```

### 9.3 增量添加数据源

```bash
# 只添加指定表
datalink add-table --source ./new_data/ --table new_orders

# 添加整个数据源的所有表
datalink add-table --source "postgresql://user:pass@localhost/another_db"
```

### 9.4 探索关系

```bash
# 查找两列之间的连接路径
datalink path --from column:source:orders:customer_id --to column:source:users:id
```

### 9.5 Agent 集成

```bash
datalink serve --port 8080
```

在 Cursor 等支持 MCP 的 IDE 中配置 DataLink MCP Server，Agent 即可调用 `add_table`、`rebuild`、`explore` 等工具。

### 9.6 开发调试

```bash
# 运行测试
uv run pytest

# 代码检查
uv run ruff check src tests
```

测试覆盖：`test_connector`, `test_extractor`, `test_profiler`, `test_inferrer`, `test_mapper`, `test_builder`, `test_storage`, `test_retrieval`, `test_cli`, `test_models`。

---

## 10. 典型使用场景

### 场景 1：Agent 找「收入」相关字段

1. `search_nodes("revenue")` → 命中 Concept 或 Column
2. `get_node(concept_id)` → 查看 `represents` 边指向哪些 Column
3. `list_datasets()` → 确认字段所在表

### 场景 2：跨表 JOIN 规划

1. 定位两表的关键列 ID
2. `find_paths(col_a, col_b, edge_types="joinable,foreign_key,semantic_synonym")`
3. 按 confidence 选择 JOIN 路径

### 场景 3：增量接入新数据源

```bash
# 添加整个数据源（不指定表名）
datalink add-table --source "postgresql://user:pass@localhost/another_db"

# 或者只添加一张表
datalink add-table --source ./new_data/ --table new_orders
```

新表会与已有图谱做 joinable/synonym 等联合推断。

### 场景 4：数据更新后刷新图谱

```bash
# 完整重建（数据大幅变化）
datalink rebuild

# 只重算统计 + 推断边（数据量变化但概念结构不变）
datalink rebuild --mode profile

# 只重建向量（更换了 embedding 模型）
datalink rebuild --mode vec
```

系统自动从已有元数据中读取数据源信息，重新连接并重建。不需要重新告诉系统数据在哪。

---

## 11. 扩展点

| 扩展方向 | 建议入口 |
|----------|----------|
| 新数据源（JSON、API） | 实现 `BaseConnector` 子类 |
| 新节点类型（如 Dashboard） | 扩展 `NodeType` + extractor |
| 新推断规则 | 在 `inferrer/` 新增 Inferrer，在 pipeline 中注册 |
| 上下文边（查询共现） | 实现 `EdgeType.CO_OCCURS_IN_QUERY` 等 |
| 向量相似检索 | 在 `GraphRetrieval.search_nodes` 中增加 embedding |
| 非 SQLite 存储 | 实现与 `GraphStorage` 等价的存储层 |

---

## 11. 关键代码入口

| 用途 | 文件 |
|------|------|
| 构建编排 | `src/datalink/builder/pipeline.py` |
| CLI 入口 | `src/datalink/cli/main.py` |
| MCP 入口 | `src/datalink/mcp/server.py` |
| 图谱查询 | `src/datalink/graph/retrieval.py` |
| 数据模型 | `src/datalink/models/` |
| 配置 | `src/datalink/config.py` |

---

## 12. 已知限制

1. 当前只有表格数据连接器、Table/Column 结构节点、列画像和列级关系推断；PPT、PDF、Markdown、图片等类型尚未实现。
2. 文件源无 FK，跨文件关系依赖 inferrer；数据库源的跨数据源 FK 会存入 pending_edges 表，在新数据源加入后自动 resolve。
3. LLM 映射需要 API Key；无 Key 时整个业务概念层为空（Concept/Entity 都不生成）。
4. CorrelationInferrer 在 `add_table` 流程中不调用——无法获取已有表的 sample_data（未持久化）来做跨表 merge。
5. 上下文边（C 类）尚未实现。
6. 永远无法 resolve 的 pending edges（目标数据源永不加入数据地图）仅作为 suggested_edges 展示，不参与路径发现和子图扩展。
