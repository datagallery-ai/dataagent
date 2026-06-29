# Semantic Service User Guide

Semantic Service organizes business data, tables, columns, metrics, ontology graphs, and business actions into semantic context that Agents can consume. Its goal is not to replace model reasoning, but to provide more reliable business semantics, data semantics, and query boundaries before inference, so Agents know what data is available, what fields mean, how objects relate, and which queries or actions are valid. The current open-source release focuses on data semantics required for NL2SQL.

At this stage, Semantic Service mainly provides MetaVisor enriched metadata and prioritizes GaussVector-oriented semantic-layer enhancements for vector indexing, recall ranking, and schema perception across table descriptions, column descriptions, metric definitions, and business keywords, helping NL2SQL recall and align candidate schemas before SQL generation; Ontology service capabilities are under development.

- **MetaVisor enriched metadata**: Currently available and used only for NL2SQL—both standalone NL2SQL Agents and main Agents calling NL2SQL sub-agents via `nl2sql_sub_agent_tool`. With prioritized GaussVector support, MetaVisor can turn business metadata into recallable, rankable, and reusable schema semantic indexes, giving the semantic layer stronger candidate schema discovery.
- **Ontology service**: Under development. The goal is to provide ontology/knowledge-graph semantics for business objects, relationships, attributes, paths, statistics, and server-side actions. Related services, Skills, CLI, and integration examples will be added when the capability stabilizes.

They solve different problems: MetaVisor helps NL2SQL understand where data lives, what fields mean, and how tables join; the planned Ontology service will help Agents understand what business objects are, how they relate, and which attributes, paths, statistics, or actions can be queried.

## 1. Capability Boundaries

| Component | Recommended Entry | Main Role |
| --- | --- | --- |
| MetaVisor | NL2SQL Agent / `nl2sql_sub_agent_tool` | Provides schema, field semantics, table relationships, and value matching for NL2SQL |
| Ontology service | Under development | Planned: ontology/knowledge-graph schema discovery, entity/relationship queries, path queries, statistical aggregation, and server-side action queries |

MetaVisor gives NL2SQL reliable data semantics before SQL generation: tables, columns, descriptions, types, sample values, and join relationships. The planned Ontology service will provide a business knowledge-graph view so Agents have structured ontology grounding for object relationships, business rules, path retrieval, aggregation, or server-side actions—instead of guessing entity labels, relationship names, attribute names, or UUIDs.

## 2. Overall Usage Patterns

In Agents, the Semantic Service can be used in three forms based on current and planned capabilities:

| Pattern | Suitable Scenario | Recommended Entry |
| --- | --- | --- |
| Standalone NL2SQL Agent | The user question is natural language to SQL; the Agent only handles schema perception, SQL generation, execution, and result return | `AGENT_CONFIG.type: "nl2sql"` + `METAVISOR` |
| Main Agent calling NL2SQL sub-agent | The main Agent plans the overall task and delegates NL2SQL only when a database query is needed | `nl2sql_sub_agent_tool` + main Agent `DATABASE` / `METAVISOR` |
| Ontology/graph queries | Tasks require querying business objects, relationships, attributes, paths, statistics, or server-side actions | Under development |

A complex data task may combine these capabilities, but keep responsibilities clear:

1. The main Agent understands user goals, decomposes tasks, and organizes the final answer.
2. SQL questions go to the NL2SQL Agent or `nl2sql_sub_agent_tool`, with MetaVisor supplying data semantics.
3. Ontology/graph questions belong to the under-development Ontology service; until integration stabilizes, have users provide explicit objects and constraints before follow-up queries.
4. Do not let the main Agent guess fields, table relationships, ontology labels, attribute names, or UUIDs.

## 3. MetaVisor Enriched Metadata

MetaVisor targets structured data. Its core value is turning database tables and columns into semantic context that models can understand and validate. In the NL2SQL flow, it mainly provides:

- Table-level semantics: table names, descriptions, business meaning.
- Column-level semantics: column names, descriptions, types, sample values.
- Relationship semantics: which tables can join and what the join keys are.
- Value matching: whether literals in SQL exist; if not, recall similar values.
- GaussVector semantic-index enhancement: GaussVector carries vectors for table descriptions, column descriptions, metric definitions, and business keywords, improving candidate schema recall and strengthening semantic matching during NL2SQL perception.

In this project, MetaVisor is not described as a general ReAct perception tool—it is part of NL2SQL only.

### 3.1 Semantic-Layer Enhancements with Prioritized GaussVector Support

In the semantic layer, GaussVector is the prioritized vector retrieval enhancement. MetaVisor embeds tables, columns, metric definitions, business descriptions, and related semantic text, then stores and retrieves those vectorized semantic assets through GaussVector. During schema perception, NL2SQL searches semantic indexes with the user question and extracted keywords to recall candidate tables, candidate columns, and table descriptions. These candidates are combined with join relationships, column types, and value matching before SQL generation.

The GaussVector-oriented enhancements upgrade business metadata from static documentation into searchable, rankable, and reusable semantic assets, giving natural-language data queries more stable candidate schema recall and reducing the need for the model to guess table or column names.

### 3.2 Using MetaVisor in an NL2SQL Agent

When `AGENT_CONFIG.type: "nl2sql"`, the NL2SQL Perceptor reads `METAVISOR.metavisor_url` to fetch schema and join info from MetaVisor; the Validator reads `METAVISOR.valuematch_url` for value matching when `metadata_match` is enabled.

Key configuration for a dedicated NL2SQL Agent:

```yaml
AGENT_CONFIG:
  type: "nl2sql"

DATABASE:
  db_id: "<your_db_id>"
  engine: "sqlite"
  config:
    path: "/path/to/your.sqlite"

METAVISOR:
  metavisor_url: "http://localhost:32000"
  username: "example"
  password: "123456"
  valuematch_url: "http://localhost:8000"
```

For full configuration, run commands, and troubleshooting, see [Build a Dedicated NL2SQL Agent](../case/build-an-nl2sql-application.md).

### 3.3 Using MetaVisor in an NL2SQL Sub-Agent

For a general ReAct main Agent that calls NL2SQL only when SQL is needed, register `nl2sql_sub_agent_tool`. This tool reads the built-in source config:

```text
dataagent/agents/nl2sql/nl2sql_agent.yaml
```

At runtime it **overwrites the temporary NL2SQL sub-agent YAML with `DATABASE` and `METAVISOR` from the main Agent config**, then starts the NL2SQL sub-agent via `sub_agent_tool`. So the main Agent needs its own `DATABASE` and `METAVISOR`; you do not edit the source NL2SQL YAML directly.

The main Agent only needs three configuration areas:

| Configuration | Role |
| --- | --- |
| `TOOLS.local_functions[].function: nl2sql_sub_agent_tool` | Register the NL2SQL sub-agent tool. |
| `DATABASE` | Business database for the main Agent; overwritten onto the NL2SQL sub-agent at runtime. |
| `METAVISOR` | Enriched metadata service; overwritten onto the NL2SQL sub-agent at runtime. |

`nl2sql_sub_agent_tool` does three key things:

1. Reads `dataagent/agents/nl2sql/nl2sql_agent.yaml` as the NL2SQL sub-agent base config.
2. Reads `DATABASE` and `METAVISOR` from the main Agent `config_manager` and overwrites the temporary sub-agent YAML.
3. If `config.llm_model` is set on the tool, reads `MODEL.<llm_model>` from the main Agent and writes it into the sub-agent config.

So the main Agent config is the runtime source of truth for the NL2SQL sub-agent:

| Main Agent Config | Effect on Sub-Agent |
| --- | --- |
| `DATABASE` | Overwrites NL2SQL sub-agent database config |
| `METAVISOR` | Overwrites NL2SQL sub-agent enriched metadata config |
| `TOOLS.local_functions[].config.llm_model` | Binds the model slot used by the sub-agent |
| `MODEL.<llm_model>` | Written into the temporary sub-agent YAML `MODEL` |

For full main Agent YAML, tool parameters, run instructions, and troubleshooting, see [Build a Data Analysis Agent](../case/build-a-dataagent-from-scratch.md).

### 3.4 Capabilities MetaVisor Provides to NL2SQL

NL2SQL calls MetaVisor and ValueMatch via `dataagent/agents/nl2sql/utils/metavisor_client.py`:

| Capability | Role |
| --- | --- |
| `get_table_list(db)` | List tables and descriptions in the database. |
| `get_table_columns_info(table_name)` | Get column names, descriptions, types, and sample values. |
| `get_joinable_tables(table_names)` | Get joinable column relationships between tables. |
| `semantic_search_column(db, keywords, top_k)` | Recall relevant columns by keyword semantics. |
| `vector_search_table_desc(db, keywords, top_k)` | Recall relevant tables by table-description vectors. |
| `semantic_search_tables(db, keywords, top_k)` | Recall relevant tables by keywords. |
| `check_value_exist(db, val)` | Check whether a literal in SQL exists. |
| `check_value_match(db, table, column, question, top_k)` | Recall similar values in a column to correct SQL literals. |

By default, if no fixed `user_schema` is provided, the NL2SQL Perceptor fetches schema from MetaVisor and converts tables, columns, and join info into model-readable SQL context.

## 4. Ontology Capabilities

The Ontology service targets business knowledge graphs. Unlike MetaVisor, ontology is not for SQL table/column context—it expresses business objects, relationships, attribute constraints, path rules, metric definitions, and server-side actions.

Ontology is an under-development Semantic Service capability. Service implementation, `ontology_service` Skill, CLI scripts, and query examples will be added when the capability stabilizes; this document describes goals and integration boundaries for now.

Planned Ontology capabilities include:

| Capability | Description |
| --- | --- |
| Schema discovery | Query entity types, relationship types, node attributes, and edge attributes in the current scenario. |
| Entity queries | List node instances by object type, or query node details by UUID. |
| Relationship queries | Query relationship types, edge instances, and one-hop relationships from source/target. |
| Attribute filtering | Filter nodes or edges by attribute conditions (name contains, numeric range, enum match, etc.). |
| Attribute explanation | Query attribute names, meanings, and values on nodes or edges to help Agents understand field semantics. |
| Path queries | Multi-hop queries, subgraph queries, or source-relationship-target pattern queries. |
| Statistical aggregation | Count, sort, and aggregate nodes or edges that match conditions. |
| Server-side actions | Query declared server actions and execute them once parameters are clear. |

### 4.1 Planned Integration Pattern

When the ontology service is open-sourced or integrated, expose it to the main Agent via deterministic tools or Skills—not by letting the model guess ontology labels, attribute names, UUIDs, or action parameters. Recommended flow:

1. Discover entity types, relationship types, and queryable attributes in the business scenario.
2. Parse candidate business objects, relationships, and filters from the user question.
3. Confirm object identifiers, attribute meanings, and relationship boundaries in the ontology service.
4. Run relationship queries, path queries, aggregation, or server-side actions on confirmed objects.
5. Return query basis and results to the main Agent for answers or as business constraints for follow-up NL2SQL queries.

This flow is a design direction under development. Commands, environment variables, and service URLs will be documented when the ontology capability stabilizes.

## 5. Capability Selection Guide

In production Agents, choose Semantic Service capabilities by task type:

| User Question Type | Recommended Approach |
| --- | --- |
| “Query a business table and aggregate metrics” | Main Agent calls `nl2sql_sub_agent_tool`; MetaVisor supplies schema and join info to NL2SQL. |
| “What related objects does this business object have?” | Under-development Ontology scenario; for now, have users provide explicit objects and relationship constraints. |
| “Confirm business objects first, then query table statistics” | Use business rules or manual constraints to clarify objects, then hand the query to the NL2SQL sub-agent; automatic ontology confirmation is planned later. |
| “Natural language to SQL only” | Use an NL2SQL Agent with `type: "nl2sql"`. |

Full walkthrough tutorials:

- [Build a Dedicated NL2SQL Agent](../case/build-an-nl2sql-application.md)
- [Build a Data Analysis Agent](../case/build-a-dataagent-from-scratch.md)

## 6. Configuration Checklist

- For standalone NL2SQL, confirm `AGENT_CONFIG.type: "nl2sql"`.
- When the main Agent calls an NL2SQL sub-agent, confirm `nl2sql_sub_agent_tool` is registered—not the generic `sub_agent_tool`.
- Put MetaVisor config under `METAVISOR` on the runtime Agent; for sub-agent scenarios, put it in the main Agent YAML.
- `DATABASE.db_id` must match the database name registered in MetaVisor.
- `METAVISOR.metavisor_url` should be `http://host:port`.
- `METAVISOR.valuematch_url` should be `host:port`.
- Ontology/knowledge-graph query capabilities are under development; configure `ontology_service`, `ONTOLOGY_URL`, or `SCENE` after integration stabilizes.

## 7. Related Code and Examples

- NL2SQL Agent config: `dataagent/agents/nl2sql/nl2sql_agent.yaml`
- NL2SQL Perceptor: `dataagent/agents/nl2sql/nodes/perceptor.py`
- MetaVisor / ValueMatch client: `dataagent/agents/nl2sql/utils/metavisor_client.py`
- NL2SQL Validator: `dataagent/agents/nl2sql/nodes/validator.py`
- NL2SQL sub-agent tool: `dataagent/actions/tools/local_tool/tools.py`
- Main Agent example calling NL2SQL sub-agent: `dataagent/core/flex/examples/nl2sql_flex_e2e_subagent.yaml`
