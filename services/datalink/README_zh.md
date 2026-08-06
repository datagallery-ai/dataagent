<h1 align="center">DataLink 🧬</h1>

<p align="center">
  面向数据 Agent 的统一数据地图——连接不同来源、不同形态的数据，<br/>提供可查询、可扩展、带有关系与置信度的数据上下文。
</p>

<p align="center">
  <a href="README.md">English</a> · 简体中文
</p>

<p align="center">
  <a href="#-快速开始"><strong>快速开始</strong></a>
  ·
  <a href="docs/README.md"><strong>文档</strong></a>
  ·
  <a href="docs/DESIGN.md"><strong>设计文档</strong></a>
  ·
  <a href="docs/INTERFACE.md"><strong>接口参考</strong></a>
  ·
  <a href="#-贡献指南"><strong>贡献指南</strong></a>
  ·
  <a href="#-许可证"><strong>许可证</strong></a>
</p>

## ✨ 为什么需要 DataLink

数据 Agent 能看到 schema，却很难仅凭 schema 判断数据之间的真实联系。表名和列头一目了然，但哪些字段可以 JOIN、哪些字段表达同一个业务概念、哪些关系来自明确约束、哪些只是候选线索，仍然需要额外的上下文。

DataLink 将数据结构、内容特征、业务概念和关联线索组织成一张**统一数据地图**。它的设计不绑定某一种数据形态：

- 🗺️ **统一组织** — 每种数据类型保留自己的结构特征，同时进入同一套关系模型和查询接口。
- 🏗️ **分层表达** — 数据结构层记录数据在哪里、长什么样；业务概念层记录数据表示什么、与哪些对象有关。
- 🔎 **关系感知** — 显式边（外键、血缘）和推断边（可 JOIN、概念同义、相关性、分布相似）都带置信度。Agent 按确定性筛选，不必靠猜。
- 🧩 **按类型扩展** — 新数据类型可以定义自己的 Connector、Extractor、Profiler、节点和画像策略，并复用关系表达、图存储、检索与 Agent 接入能力。
- 📦 **Agent 开箱即用** — 检索 API 通过 MCP 工具和 REST API 暴露。MCP server 提供 `datalink_explore` 供 Agent 即时查询；REST API 与 CLI 命令一一对应，方便管理和集成。

> **DataLink 是一张会持续校准的数据地图。** 首先通过静态分析完成 schema 提取、内容画像、算法推断和概念映射，建立数据地图的基线，帮助系统理解数据的结构、特征，以及数据之间可能存在的关系。当数据 Agent 实际使用数据时，它的执行轨迹——例如哪些表被 JOIN、哪些列被共同查询、哪些分析路径被反复走过——会提供静态分析难以获得的使用证据。这些证据可以进一步丰富和校准数据地图，并为关系提供更贴近实际使用的置信度评估。因此 DataLink 中数据关系构建的最终形态是双向的：静态治理提供基线地图，Agent 轨迹则用真实使用证据来丰富和校准地图。

> 现阶段，DataLink 主要支持关系型数据库、CSV 和 Parquet，核心处理对象仍是 Table/Column。PPT、PDF、Markdown、图片等非结构化数据类型将作为后续扩展方向，逐步接入统一的数据地图。

<p align="center">
  <img src="docs/DataMap.png" alt="DataLink 统一数据地图：连接表格、记录、文档、幻灯片和图片中的相关数据点" width="900"/>
</p>

上图展示了统一数据地图的设计目标：DataLink 组织、标注并对外提供跨数据源、跨格式的数据关系；其中表格和记录代表当前主要实现，文档、幻灯片和图片展示后续扩展方向。

## 🗺️ 工作原理

<p align="center">
  <img src="docs/DL-zh.png" alt="DataLink 架构图" width="720"/>
</p>

构建管线把已接入的数据源组织成可查询的数据地图：

```
Connector → Extractor → Profiler → Inferrer → Semantic Mapper → Graph
```

当前管线以表格数据为主要实现，同时按照可扩展的数据类型处理流程划分职责：

1. **Connector** — 当前连接关系型数据库和 CSV/Parquet 文件，提取 schema 与样本数据；未来可为其他类型提供专用连接器。
2. **Extractor** — 当前生成 Table/Column 等结构节点并建立 `contains`、`foreign_key` 等显式关系；其他类型可定义自己的结构节点。
3. **Profiler** — 当前计算字段类型、分布、基数、样本等列画像；其他类型可扩展对应的内容与结构画像。
4. **Inferrer** — 以置信度分数发现隐式关系——`joinable`、`semantic_synonym`、`correlated`、`distribution_similar`、`co_occurs`。
5. **Mapper** — 利用元数据或 LLM 推断，将结构节点关联到 Concept/Entity 节点，产出 `represents` 边，为跨来源、跨类型的概念归一提供统一入口。
6. **Graph** — 将节点、关系和画像存入 SQLite（邻接表），通过统一接口供 Agent 查询。

**核心设计理念**：数据库里的“营收”列、PPT 里的“营收”图表、PDF 里关于“营收”的段落，未来都可以关联到同一个 Concept 节点。不同类型保留自己的结构表达，但共享业务概念、关系模型和查询方式。当前版本已经在表和列上实现了这套流程，并以此验证统一数据地图的核心能力。

## ⚡ 快速开始

使用 [uv](https://docs.astral.sh/uv/) 安装：

```bash
uv pip install -e .
```

或使用 pip：

```bash
pip install -e .
```

配置 LLM 提供商：

```bash
cp datalink_config.example.json datalink_config.json
```

编辑 `datalink_config.json`，填入 LLM API Key：

```json
{
  "llm": {
    "model": "gpt-4o",
    "api_key": "sk-...",
    "base_url": "https://api.openai.com/v1"
  },
  "embedding": {
    "model": "text-embedding-3-small",
    "similarity_threshold": 0.75
  },
  "merge_llm_temperature": 0.0,
  "graph_db_path": "datalink.db"
}
```

OpenAI 兼容提供商（DeepSeek、Qwen 等）只需改 `base_url`：

```json
{
  "llm": {
    "model": "deepseek-chat",
    "api_key": "...",
    "base_url": "https://api.deepseek.com"
  }
}
```

Embedding 粗筛能提升合并准确性、降低 token 开销。`embedding.model` 留空则跳过粗筛，全靠 LLM 判断：

```json
{
  "embedding": {
    "model": "",
    "similarity_threshold": 0.75
  }
}
```

从数据构建图谱：

```bash
# 从数据库添加表（空图谱上等于首次构建）
datalink add-table --source "postgresql://user:pass@localhost/mydb"

# MySQL — 直接用 mysql:// 或 mysql+pymysql://（驱动自动检测）
datalink add-table --source "mysql://user:pass@localhost/mydb"

# 从 CSV 文件添加表
datalink add-table --source "./data/*.csv"

# 添加指定表
datalink add-table --source "postgresql://..." --table orders

# 底层数据变更后重建图谱（pipeline 失败时旧数据保留）
datalink rebuild

# 移除一张表及其关联节点/边
datalink remove-table --table orders
```

> **去重**：重复添加已存在的表（同数据源路径 + 同表名）会自动跳过。如需重新添加，先 `remove-table` 再 `add-table`。

探索图谱：

```bash
# 搜索节点
datalink search "customer" --type column

# 查找两节点之间的路径
datalink path --from column:postgres:users:email --to column:postgres:orders:customer_email

# 一键探索——一次调用回答完整问题
datalink explore "用户和订单是如何关联的"

# 图谱概览统计
datalink info

# 以 JSON 输出完整图谱
datalink show
```

## 🧩 用 DataLink 可以做什么

| 应用场景 | DataLink 的作用 |
| --- | --- |
| **NL2SQL 准确性** | `joinable` 和 `semantic_synonym` 边让 Agent 直接找到正确的 JOIN，不用反复试错。 |
| **跨格式洞察** | "营收"这一概念可以把数据库列、PPT 图表、PDF 段落连在一起——Agent 看到的是完整拼图。 |
| **数据血缘与发现** | `derived_from`、`references` 和 `find_paths` 让血缘和隐含关系变得可查询。 |
| **Schema 探索** | `search_nodes` 和 `extract_subgraph` 让 Agent 快速摸清陌生数据集的结构。 |
| **MCP 驱动的 Agent** | 运行 `datalink serve`，任何 MCP 客户端（Claude、Cursor、Copilot）都能直接调用检索工具。 |
| **统计问答** | 列级画像（分布、基数、模式）无需查询源数据就能回答"这数据长什么样？"。 |

## 🛠️ 架构

DataLink 采用分层、解耦的模块设计。当前实现以表格数据为主，但模块边界为更多数据类型预留了扩展入口：

| 模块 | 职责 | 可扩展？ |
| --- | --- | --- |
| `connector` | 连接数据源，提取 schema 与样本 | ✅ 新增数据源类型 |
| `extractor` | 按数据类型创建结构节点 | ✅ 新增结构词汇 |
| `profiler` | 计算适配类型的指纹 | ✅ 新增画像策略 |
| `inferrer` | 以置信度发现隐式关系 | — 跨类型共享 |
| `mapper` | 将结构节点关联到 Concept/Entity | — 共享基础能力 |
| `graph` | SQLite 存储、CRUD、四种检索接口 | — 共享引擎 |
| `builder` | 编排构建管线 | — 共享编排 |
| `cli` | Typer CLI——add-table、rebuild、show、search、explore、path、info、serve、api、config | — 共享界面 |
| `mcp` | MCP Server，将检索暴露为 Agent 工具（默认只暴露 datalink_explore） | — 共享界面 |
| `api` | FastAPI REST API——与 CLI 命令一一对应的 HTTP 端点 | — 共享界面 |

**扩展新数据类型**（例如 PPT）时，需要实现相应的 Connector、Extractor、Profiler，并按该类型补充节点模型和适用的关系推断策略。图存储、关系表达、查询接口及 Agent 接入方式可以在现有框架上复用或扩展。

## 🔌 MCP 工具

启动 MCP 服务器：

```bash
# 默认 SSE transport
datalink serve --port 8080

# 推荐 streamable-http transport（更稳定）
datalink serve --port 8080 --transport streamable-http
```

MCP 服务器只暴露**一个核心检索工具**：

| 工具 | 描述 |
| --- | --- |
| `datalink_explore` | 万能检索——一次调用回答完整数据问题 |

写操作（add-table、rebuild、remove-table）和 show 通过 **REST API** 提供——它们是离线/管理操作，不属于 Agent 的检索上下文。

```bash
# 启动 REST API 服务器（默认端口 8081）
datalink api

# 示例：通过 REST API 添加表
curl -X POST http://localhost:8081/add-table -H "Content-Type: application/json" \
  -d '{"source": "./data/", "source_type": "csv"}'

# 示例：通过 REST API 检索
curl -X POST http://localhost:8081/explore -H "Content-Type: application/json" \
  -d '{"query": "客户 订单"}'
```

**`datalink_explore` 输出示例：**

```
matched: 3 columns across 2 tables

## orders — 3 columns, 10,000 rows, source: csv
### customer_id (integer, identifier)
- meaning: FK 引用 customers.id
- data: null 0%, unique 8,921, range [1–999]
- also related: customers.id (FK, conf 1.00)

## customers — 5 columns, 999 rows, source: csv
### id (integer, identifier)
- meaning: 主键，被 orders.customer_id 引用
- data: null 0%, unique 999, range [1–999]
### email (varchar, email_address)
- meaning: 联系邮箱，每个客户唯一
- data: null 2%, unique 979, samples ["a@b.com", "c@d.com"]

## Cross-table query patterns
orders.customer_id → customers.id (FK, conf 1.00)
  SQL: SELECT * FROM orders JOIN customers ON orders.customer_id = customers.id
```

**辅助工具**（通过 `datalink_config.json` 的 `mcp_tools` 字段或 `DATALINK_MCP_TOOLS` 环境变量启用）：

| 工具 | 描述 |
| --- | --- |
| `datalink_search_nodes` | 按名称或字段角色搜索节点 |
| `datalink_get_node` | 获取节点详情、邻接边和画像 |
| `datalink_find_paths` | 查找两节点之间的关系路径 |
| `datalink_extract_subgraph` | 提取指定节点周围的邻域子图 |
| `datalink_list_datasets` | 列出所有数据源及基本统计 |
| `datalink_list_pending_edges` | 列出引用缺失节点的悬空边 |

## 🛣️ 路线图

<table>
  <tr>
    <td><strong>PPT 连接器</strong><br/>Slide、TextBox、Image、Chart 作为结构节点；布局与文本画像；跨幻灯片含义链接。</td>
    <td><strong>PDF / Markdown 连接器</strong><br/>Section、Paragraph、Table、Figure 作为结构节点；内容与结构画像。</td>
  </tr>
  <tr>
    <td><strong>图片连接器</strong><br/>Image 节点 + 视觉模型画像（描述、检测物体、场景）；视觉概念映射。</td>
    <td><strong>增量更新</strong><br/>检测数据源变更，只对受影响节点重新画像、更新边，无需全量重建。</td>
  </tr>
  <tr>
    <td><strong>向量相似检索 ✅</strong><br/>对节点名称、描述和概念内容做向量相似度检索，让 `search_nodes` 和 `explore` 成为混合检索（全文 + 向量），可配置、可重建。</td>
    <td><strong>REST API ✅</strong><br/>FastAPI 适配器，与 CLI 命令一一对应的 HTTP 端点。通过 REST 管理图谱（add-table、rebuild、remove-table）和查询数据（explore、search 等）。</td>
  </tr>
  <tr>
    <td><strong>Agent 轨迹驱动的边关系</strong><br/>从 Agent 的执行轨迹中提取真实的数据关系——哪些表被 JOIN、哪些列被一起查询、Agent 走了哪些路径。轨迹证据用真实使用行为丰富静态治理基线，产出 `frequently_joined`、`co_occurs_in_query` 等使用接地边，完成双向关系构建的闭环。</td>
    <td></td>
  </tr>
</table>

## 📚 文档

<table>
  <tr>
    <td><a href="docs/README.md"><strong>文档索引</strong></a><br/>浏览全部文档。</td>
    <td><a href="docs/DESIGN.md"><strong>设计文档</strong></a><br/>双层架构、边类型、构建管线与检索 API。</td>
  </tr>
  <tr>
    <td><a href="docs/INTERFACE.md"><strong>接口参考</strong></a><br/>CLI 命令、REST API 端点、MCP 工具与检索 API 规格。</td>
    <td><a href="docs/pipeline/"><strong>管线详情</strong></a><br/>逐步构建管线文档。</td>
  </tr>
</table>

## 🛠️ 开发

```bash
uv sync
uv run pytest
uv run ruff check src/ tests/
```

针对正在开发的模块跑定向测试：

```bash
uv run pytest tests/test_profiler.py
uv run pytest tests/test_inferrer.py
```

## 🤝 贡献指南

DataLink 正在积极开发中。小而聚焦的 PR 最容易通过审查。

1. 涉及架构变更、新数据类型连接器、边类型提案或检索 API 扩展，请先开 Issue 或 Discussion。
2. PR 聚焦于一个模块边界。
3. 对改动模块跑 `uv run pytest` 和 `uv run ruff check`。
4. 变更影响 CLI 命令、MCP 工具、边含义或检索行为时，请同步更新文档。
5. 不要提交凭证、数据库文件或生成的图谱存储。

## 🧪 项目状态

DataLink 处于早期开发阶段（v0.1.0）。当前可用能力主要面向**表格数据**：关系型数据库、CSV 和 Parquet 已支持结构提取、列画像、关系推断、概念映射、图存储与 Agent 检索。统一数据地图是项目的长期设计方向；PPT、PDF、Markdown、图片等非表格连接器仍在路线图中，需要继续补齐对应的节点模型和处理组件。

## 📄 许可证

Apache License 2.0。详见 [LICENSE.txt](LICENSE.txt)。
