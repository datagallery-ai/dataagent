# Product overview

DataFoundry is a local-first data analysis workbench. An AI agent connects the full flow from asking questions, understanding data, running read-only queries, explaining results, to preserving deliverables.

It fits teams that need to understand data quickly, validate metrics, and explore business questions. Users describe problems in natural language; the agent inspects table structure, generates and runs read-only SQL, and surfaces the analysis process and results.

## Problems it addresses

Common friction in traditional data analysis:

- Unfamiliarity with database schemas, leading to repeated lookups or documentation searches.
- Clear business questions but no immediate ability to write correct SQL.
- Opaque analysis processes that make conclusions hard to trust.
- Results scattered across chat, SQL, tables, and screenshots, making review and export difficult.

DataFoundry puts these steps in one workbench: the user asks a question, the agent understands the data structure, runs queries within read-only boundaries, and shows tool calls, SQL, result tables, charts, or reports as traceable outputs.

## Core workflow

```text
Choose data source and model
  -> Ask an analysis question in natural language
  -> Agent inspects schema and plans analysis steps
  -> Run read-only queries or read relevant knowledge
  -> Show trace, SQL, tables, charts, and conclusions
  -> Export or reuse analysis outputs
```

## Entry points

DataFoundry currently offers two main entry points:

| Entry | Best for | Notes |
| --- | --- | --- |
| Web workbench | Product trials, customer demos, daily analysis | Three-column UI suited for trace, outputs, and multi-task sessions. |
| TUI | Terminal users, remote environments, script-friendly workflows | Chat, configuration, stats, and export inside the command line. |

The backend also exposes REST API and CopilotKit / AG-UI runtime endpoints for Web, TUI, and other clients.

## Capability boundaries

DataFoundry emphasizes data safety and traceability by default:

- Data queries run through controlled tools; there is no arbitrary SQL REST passthrough.
- The agent must inspect table structure before executing SQL.
- Queries are read-only by default, with SQL guard, row limits, timeouts, field masking, and audit.
- Data source credentials are submitted only on create or update; read APIs do not return plaintext secrets.

Public docs cover local trials, open-source integration, development demos, and the built-in password-auth path. Production deployments still need deployment-specific access policy, centralized secret management, monitoring, and operations design. For a first trial, use the built-in DTC Growth Review data source to experience the core flow.

## Next steps

- Quick trial: [Quick start](quick-start.md)
- Capability coverage: [Capabilities](capabilities.md)
- Graphical UI: [Web workbench guide](guides/web-workbench.md)
- Terminal UI: [TUI guide](guides/tui.md)
