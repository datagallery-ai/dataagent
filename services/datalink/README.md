<h1 align="center">DataLink 🧬</h1>

<p align="center">
  A unified data map for data agents — connecting data across sources and formats<br/>through queryable relationships, profiles, concepts, and confidence signals.
</p>

<p align="center">
  English · <a href="README_zh.md">简体中文</a>
</p>

<p align="center">
  <a href="#-quick-start"><strong>Quick Start</strong></a>
  ·
  <a href="docs/README.md"><strong>Docs</strong></a>
  ·
  <a href="docs/DESIGN.md"><strong>Design</strong></a>
  ·
  <a href="docs/INTERFACE.md"><strong>Interface Reference</strong></a>
  ·
  <a href="#-contributing"><strong>Contributing</strong></a>
  ·
  <a href="#-license"><strong>License</strong></a>
</p>

## ✨ Why DataLink

Modern data agents can see schemas, but schemas alone rarely explain how data should be used. Which fields can be joined, which fields refer to the same business concept, which relationships are explicit constraints, and which are only candidates all require additional context.

DataLink organizes structures, profiles, business concepts, and relationship signals into a **unified data map**. The design is not tied to one data format:

- 🗺️ **Unified organization** — each data type keeps its own structure while sharing a common relationship model and query surface.
- 🏗️ **Layered representation** — the structural layer records where data lives and what it looks like; the concept layer records what it represents and what it relates to.
- 🔎 **Relationship-aware** — explicit edges such as foreign keys and lineage, plus inferred edges such as joinability, concept equivalence, correlation, and distribution similarity, carry confidence scores.
- 🧩 **Extensible by data type** — new formats can define their own connectors, extractors, profilers, nodes, and profiling strategies while reusing graph storage, retrieval, and agent integration capabilities.
- 📦 **Agent-ready** — retrieval APIs exposed as MCP tools and REST API. The MCP server exposes `datalink_explore` for instant agent queries; the REST API mirrors all CLI commands for management and integration.

> **DataLink is a data map that continuously calibrates itself.** It first uses static analysis — schema extraction, content profiling, algorithmic inference, and concept mapping — to establish a baseline map, helping the system understand data structures, features, and possible relationships. When data agents actually use the data, their execution trajectories — for example, which tables are joined, which columns are queried together, and which analysis paths are repeatedly traversed — provide usage evidence that static analysis alone cannot capture. This evidence can further enrich and calibrate the data map while providing confidence estimates grounded in real usage. The ultimate form of relationship construction in DataLink is therefore two-directional: static governance provides the baseline map, while agent trajectories enrich and calibrate it with evidence from real usage.

> For now, DataLink focuses on relational databases, CSV, and Parquet, with Table/Column as its core objects. PPT, PDF, Markdown, and image connectors will be added progressively as future extensions for unstructured data types within the unified data map.

<p align="center">
  <img src="docs/DataMap.png" alt="DataLink unified data map connecting related data points across tables, records, documents, slides, and images" width="900"/>
</p>

The illustration shows the design goal of the unified data map: DataLink organizes, labels, and exposes relationships across sources and formats. Tables and records represent the current implementation focus, while documents, slides, and images illustrate future extension directions.

## 🗺️ How It Works

<p align="center">
  <img src="docs/DL-en.png" alt="DataLink Architecture" width="720"/>
</p>

The build pipeline organizes supported data sources into a queryable data map:

```
Connector → Extractor → Profiler → Inferrer → Mapper → Graph
```

The current pipeline is implemented primarily for tabular data, with responsibilities separated so additional data types can be added over time:

1. **Connector** currently connects to relational databases and CSV/Parquet files; future formats can provide dedicated connectors.
2. **Extractor** currently creates Table/Column nodes and explicit relationships such as `contains` and `foreign_key`; other formats can define their own structural nodes.
3. **Profiler** currently computes column types, distributions, cardinality, and samples; other formats can add suitable structure and content profiles.
4. **Inferrer** discovers implicit relationships with confidence scores — `joinable`, `semantic_synonym`, `correlated`, `distribution_similar`, `co_occurs`.
5. **Mapper** associates structural nodes with Concept/Entity nodes using metadata or LLM inference, providing a common entry point for concept normalization across sources and formats.
6. **Graph** stores nodes, relationships, and profiles in SQLite and exposes a unified query interface for agents.

**The design idea:** a revenue column in a database, a revenue chart in a slide deck, and a revenue paragraph in a PDF can eventually connect to the same Concept node. Each format keeps its own structure while sharing business concepts, relationships, and query behavior. The current release validates this approach through its table and column implementation.

## ⚡ Quick Start

Install with [uv](https://docs.astral.sh/uv/):

```bash
uv pip install -e .
```

Or with pip:

```bash
pip install -e .
```

Configure your LLM provider:

```bash
cp datalink_config.example.json datalink_config.json
```

Edit `datalink_config.json` — set your LLM API key:

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

OpenAI-compatible providers (DeepSeek, Qwen, etc.) work by changing `base_url`:

```json
{
  "llm": {
    "model": "deepseek-chat",
    "api_key": "...",
    "base_url": "https://api.deepseek.com"
  }
}
```

Embedding pre-filtering improves merge accuracy and reduces token cost. Leave `embedding.model` empty to skip pre-filtering and use pure LLM judgment:

```json
{
  "embedding": {
    "model": "",
    "similarity_threshold": 0.75
  }
}
```

Build a graph from your data:

```bash
# Add tables from a database (also works as initial build on empty graph)
datalink add-table --source "postgresql://user:pass@localhost/mydb"

# MySQL — use bare mysql:// or mysql+pymysql:// (driver auto-detected)
datalink add-table --source "mysql://user:pass@localhost/mydb"

# Add tables from CSV files
datalink add-table --source "./data/*.csv"

# Add a specific table
datalink add-table --source "postgresql://..." --table orders

# Rebuild the graph when underlying data has changed (old data preserved if pipeline fails)
datalink rebuild

# Remove a table and its nodes/edges
datalink remove-table --table orders
```

> **Deduplication**: Re-adding an existing table (same source path + same table name) is automatically skipped. Use `remove-table` first to re-add.

Explore the graph:

```bash
# Search for nodes
datalink search "customer" --type column

# Find paths between two nodes
datalink path --from column:postgres:users:email --to column:postgres:orders:customer_email

# One-call exploration — answers the whole question
datalink explore "how do users connect to orders"

# Graph overview stats
datalink info

# Show the full graph as JSON
datalink show
```

## 🧩 What You Can Build With It

| Use case | How DataLink helps |
| --- | --- |
| **NL2SQL accuracy** | `joinable` and `semantic_synonym` edges let agents find the right JOINs without trial-and-error. |
| **Cross-format insight** | A concept like "revenue" can link a database column, a PPT chart, and a PDF paragraph — agents see the full picture. |
| **Data lineage & discovery** | `derived_from`, `references`, and `find_paths` make lineage and hidden relationships queryable. |
| **Schema exploration** | `search_nodes` and `extract_subgraph` let agents understand unfamiliar datasets fast. |
| **MCP-powered agents** | Run `datalink serve` and any MCP client (Claude, Cursor, Copilot) can call retrieval tools directly. |
| **Statistical QA** | Column profiles (distribution, cardinality, patterns) answer "what does this data look like?" without querying the source. |

## 🛠️ Architecture

DataLink uses decoupled modules with explicit extension points. The current implementation is tabular-first, while the module boundaries leave room for additional data types:

| Module | Responsibility | Extensible? |
| --- | --- | --- |
| `connector` | Connect to data sources, extract schema + sample | ✅ add new source types |
| `extractor` | Create structural nodes per data type | ✅ add new structural vocabularies |
| `profiler` | Compute type-appropriate fingerprints | ✅ add new profiling strategies |
| `inferrer` | Discover implicit relationships with confidence | — shared across types |
| `mapper` | Associate structural nodes with Concept/Entity | — shared foundation |
| `graph` | SQLite storage, CRUD, four retrieval interfaces | — shared engine |
| `builder` | Orchestrate the build pipeline | — shared orchestration |
| `cli` | Typer CLI — add-table, rebuild, show, search, explore, path, info, serve, api, config | — shared interface |
| `mcp` | MCP Server exposing retrieval as agent tools | — shared interface |
| `api` | FastAPI REST API — mirrors all CLI commands as HTTP endpoints | — shared interface |

**Adding a new data type** such as PPT requires a connector, extractor, profiler, suitable node models, and applicable relationship rules. Graph storage, relationship representation, retrieval APIs, and agent integration can be reused or extended within the existing framework.

## 🔌 MCP Tools

Start the MCP server:

```bash
# Default SSE transport
datalink serve --port 8080

# Recommended streamable-http transport (more stable)
datalink serve --port 8080 --transport streamable-http
```

The MCP server exposes **one core tool** for agent retrieval:

| Tool | Description |
| --- | --- |
| `datalink_explore` | Universal retrieval — one call answers the whole data question |

Write operations (add-table, rebuild, remove-table) and show are available via the **REST API** instead — they are offline/management operations that don't belong in an agent's retrieval context.

```bash
# Start the REST API server (default port 8081)
datalink api

# Example: add a table via REST API
curl -X POST http://localhost:8081/add-table -H "Content-Type: application/json" \
  -d '{"source": "./data/", "source_type": "csv"}'

# Example: explore via REST API
curl -X POST http://localhost:8081/explore -H "Content-Type: application/json" \
  -d '{"query": "customer orders"}'
```

**`datalink_explore` output example:**

```
matched: 3 columns across 2 tables

## orders — 3 columns, 10,000 rows, source: csv
### customer_id (integer, identifier)
- meaning: FK referencing customers.id
- data: null 0%, unique 8,921, range [1–999]
- also related: customers.id (FK, conf 1.00)

## customers — 5 columns, 999 rows, source: csv
### id (integer, identifier)
- meaning: primary key, referenced by orders.customer_id
- data: null 0%, unique 999, range [1–999]
### email (varchar, email_address)
- meaning: contact email, unique per customer
- data: null 2%, unique 979, samples ["a@b.com", "c@d.com"]

## Cross-table query patterns
orders.customer_id → customers.id (FK, conf 1.00)
  SQL: SELECT * FROM orders JOIN customers ON orders.customer_id = customers.id
```

**Optional** (enable via `datalink_config.json` `mcp_tools` field or `DATALINK_MCP_TOOLS` env var):

| Tool | Description |
| --- | --- |
| `datalink_search_nodes` | Search nodes by name or field role |
| `datalink_get_node` | Get node details with connected edges and profile |
| `datalink_find_paths` | Find relationship paths between two nodes |
| `datalink_extract_subgraph` | Extract a neighborhood subgraph around given nodes |
| `datalink_list_datasets` | List all data sources with basic stats |
| `datalink_list_pending_edges` | List dangling edges referencing missing nodes |

## 🛣️ Roadmap

<table>
  <tr>
    <td><strong>PPT connector</strong><br/>Slide, TextBox, Image, and Chart nodes; layout and text profiling; cross-slide concept links.</td>
    <td><strong>PDF / Markdown connector</strong><br/>Section, Paragraph, Table, Figure as structural nodes; content & structure profiling.</td>
  </tr>
  <tr>
    <td><strong>Image connector</strong><br/>Image nodes with vision-model profiles for captions, detected objects, and scenes; visual concept mapping.</td>
    <td><strong>Incremental update</strong><br/>Detect source changes, re-profile affected nodes, update edges without full rebuild.</td>
  </tr>
  <tr>
    <td><strong>Embedding-backed search ✅</strong><br/>Vector similarity on node names, descriptions, and concept content for hybrid retrieval (text + vector) in `search_nodes` and `explore`. Configurable and rebuildable.</td>
    <td><strong>REST API ✅</strong><br/>FastAPI adapter mirrors all CLI commands as HTTP endpoints. Manage the graph (add-table, rebuild, remove-table) and query it (explore, search, etc.) via REST.</td>
  </tr>
  <tr>
    <td><strong>Agent trajectory–driven edges</strong><br/>Extract real-world relationships from agent execution traces — which tables get joined, which columns are queried together, which paths agents walk. Trajectory evidence enriches the static governance baseline with usage-grounded edges such as `frequently_joined` and `co_occurs_in_query`, completing the two-directional relationship construction.</td>
    <td></td>
  </tr>
</table>

## 📚 Documentation

<table>
  <tr>
    <td><a href="docs/README.md"><strong>Docs Index</strong></a><br/>Navigate all documentation.</td>
    <td><a href="docs/DESIGN.md"><strong>Design Document</strong></a><br/>Layered data map, relationship types, build pipeline, and retrieval APIs.</td>
  </tr>
  <tr>
    <td><a href="docs/INTERFACE.md"><strong>Interface Reference</strong></a><br/>CLI commands, REST API endpoints, MCP tools, and retrieval API specifications.</td>
    <td><a href="docs/pipeline/"><strong>Pipeline Details</strong></a><br/>Step-by-step build pipeline documentation.</td>
  </tr>
</table>

## 🛠️ Developer Loop

```bash
uv sync
uv run pytest
uv run ruff check src/ tests/
```

Run targeted tests for the module you're working on:

```bash
uv run pytest tests/test_profiler.py
uv run pytest tests/test_inferrer.py
```

## 🤝 Contributing

DataLink is under active development. Small, well-scoped contributions are easiest to review.

1. Open an issue or discussion for architectural changes, new data type connectors, edge type proposals, or retrieval API extensions.
2. Keep pull requests focused on one module boundary.
3. Run `uv run pytest` and `uv run ruff check` for the modules you touched.
4. Update docs when a change affects CLI commands, MCP tools, relationship definitions, or retrieval behavior.
5. Do not commit credentials, database files, or generated graph storage.

## 🧪 Status

DataLink is in early development (v0.1.0). Its working capabilities currently focus on **tabular data**: relational databases, CSV, and Parquet support structural extraction, column profiling, relationship inference, concept mapping, graph storage, and agent retrieval. The unified data map is the long-term direction; PPT, PDF, Markdown, and image support still requires dedicated node models and processing components.

## 📄 License

Apache License 2.0. See [LICENSE.txt](LICENSE.txt).
