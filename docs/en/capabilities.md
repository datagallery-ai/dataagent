# Capabilities

This guide helps you evaluate DataFoundry's capability scope. After reading it, you can tell what Web, TUI, and API each support, and which features depend on backend capabilities or external resource configuration.

Status is based on current code:

| Status | How to verify |
| --- | --- |
| Ready to try | After local startup, with a model key configured, the built-in DTC Growth Review runs end to end. |
| Requires configuration | Feature entry exists but needs a model key, database credentials, files, MCP server, or Skill package. |
| Capability-controlled | Read `GET /api/v1/capabilities` and enable or hide related entry points from the response. |
| Local development boundary | Default local identity and default workspace work out of the box; Web can switch local dev users for isolation testing. |
| Password auth boundary | Built-in password mode covers account registration, login, reset, session cookies, and CSRF. Production deployments still need secret management, audit export, access control policy, and operations monitoring. |

## Overview

| Capability | Web workbench | TUI | Backend/API | How to verify |
| --- | --- | --- | --- | --- |
| Natural-language data analysis | Ready to try | Ready to try | Ready to try | Configure an LLM key and ask questions with `dtc-growth-demo`. |
| Built-in DTC growth case | Ready to try | Ready to try | Ready to try | Data source list includes `DTC Growth Review`; each user gets a read-only workspace copy. |
| Data source registration and test | Ready to try | Select configured sources | Ready to try | `GET /api/v1/datasource-types`, `POST /api/v1/datasources/:id/test`. |
| Schema fetch and table preview | Ready to try | Via agent tool results | Ready to try | `POST /api/v1/datasources/:id/introspect`, `GET /schema`, `GET /tables/:table/preview`. |
| Read-only SQL analysis | Ready to try | Ready to try | Ready to try | Agent run inspects schema first, then runs queries through tools. |
| Model configuration | Requires configuration | Uses server model config | Requires configuration | `.env` or `/api/v1/model-profiles`. |
| Model connection test | Ready to try | Uses server model config | Ready to try | `POST /api/v1/model-profiles/:id/test`. |
| Analysis trace | Ready to try | Ready to try | Ready to try | View steps, tool calls, run events, and SQL audit. |
| Semantic Trace DAG | Ready to try | No graph view | Ready to try | Open the Web trace graph or `GET /api/v1/sessions/:id/trace-dag`. |
| Artifact outputs | Ready to try | View session outputs | Capability-controlled | `artifact.list`, `artifact.export`, `artifact.promote`. |
| Session history | Ready to try | Resume with `/resume` | Capability-controlled | `conversation.memory`, `conversation.title`. |
| Concurrent sessions and queued prompts | Ready to try | One active terminal flow | Ready to try | Start runs in separate sessions; submit another prompt during an active Web run. |
| Checkpoint branches | Ready to try | No branch controls | Ready to try | Re-ask from an earlier turn or `POST /api/v1/sessions/:id/branches`. |
| Evidence references | Ready to try | No selection UI | Ready to try | Reference an output or selection and inspect `evidenceRefs` diagnostics. |
| Data Link graph | Requires configuration | No graph view | Requires configuration | Configure a compatible Data Link MCP server, then open **Data Link**. |
| User identity | Local dev switcher and password auth screens | Uses backend identity | Ready to try | `GET /api/v1/me`, `/api/v1/dev/*`, `/api/v1/auth/*`. |
| Workspace files | Upload, view, download, delete, reuse | Use enabled files via run_config | Capability-controlled | `files`, `GET/POST /api/v1/files`, `POST /api/v1/files/:id/promote`. |
| Chat attachments | Ready to try | No attachment upload command | Capability-controlled | `chat.fileUpload`, `POST /api/v1/chat/uploads`. |
| Image input | Input controlled by switch | No image input command | Capability-controlled | `chat.imageInput`. |
| Knowledge bases | Requires configuration | Enabled resources via run_config | Capability-controlled | `knowledge`, `kb.chunking`, `kb.citationPolicy`. |
| MCP tools | Requires configuration | Enabled resources via run_config | Capability-controlled | `mcp`, `mcp.stdio`, `mcp.toolPolicy`. |
| Skills | Requires configuration | Select with `/skill` | Capability-controlled | `skills`, `skill.resourceBinding`. |
| Cancel run | Ready to try | No slash command | Ready to try | `POST /api/v1/runs/:id/cancel`. |

## Backend capability keys

`GET /api/v1/capabilities` returns the keys below. Clients use them to control UI, run configuration, and resource entry points:

| Key | Controls |
| --- | --- |
| `artifact.export` | Artifact export. |
| `artifact.list` | Session artifact list. |
| `artifact.promote` | Promote file artifacts into the workspace. |
| `chat.fileUpload` | Chat attachment upload. |
| `chat.imageInput` | Image input. |
| `conversation.memory` | Server-side session memory. |
| `conversation.title` | Session title persistence. |
| `interaction.resume` | Human interaction resume after refresh or session switch. |
| `datasource.fieldMasking` | Data source field masking configuration. |
| `datasource.extendedTypes` | Extended data source types. |
| `datasource.introspectionPolicy` | Schema introspection policy. |
| `datasource.queryPolicy` | Query row limit, timeout, and write-deny policy. |
| `datasource.samplePolicy` | Sample preview policy. |
| `datasource.server` | Server database connection fields. |
| `files` | Workspace file assets. |
| `kb.chunking` | Knowledge base chunking configuration. |
| `kb.citationPolicy` | Knowledge base citation policy. |
| `kb.scope` | Knowledge base scope. |
| `llm.advancedSampling` | Advanced model sampling parameters. |
| `llm.samplingParams` | Model sampling parameters. |
| `knowledge` | Knowledge resources in runtime. |
| `mcp` | MCP resources in runtime. |
| `mcp.stdio` | stdio MCP server configuration. |
| `mcp.toolPolicy` | MCP tool policy. |
| `skill.resourceBinding` | Skill resource binding. |
| `skills` | Skill resources in runtime. |

## Web workbench

The Web workbench suits local demos and daily analysis:

- Left panel: sessions and workspace resources.
- Center: conversation, step cards, and human confirmations.
- Right: overview, semantic trace, outputs, evidence-aware step details, and workspace files.
- Input box: model selection, resource toggles, `@` mentions, attachments, evidence chips, queued prompts, and stop run.
- Session list restores history via server `/api/v1/sessions`; earlier turns and checkpoints can create persistent branches.
- Data Link opens a workspace graph when a compatible MCP service is configured.

See [Web workbench guide](guides/web-workbench.md).

## TUI

The TUI suits remote servers and terminal workflows:

- Chat-first terminal UI with a separate `/outputs` page.
- `/datasource` to select a data source.
- `/skill` to select a Skill.
- `/resume` to restore server session history.
- Password sign-in with a local session cache (requires a running API).
- `Tab` completion, input history, and Chat view scrolling.

Registered commands are defined in [TUI guide](guides/tui.md).

## API and integration

The backend exposes two entry types:

| Entry | Purpose |
| --- | --- |
| `POST /api/copilotkit` | Start an agent run and return an AG-UI event stream. |
| `/api/v1/*` | Manage resources, files, sessions, outputs, and configuration. |

Integrators should manage resources through the configuration API and start analysis through Agent Runtime. Data source credentials are submitted only when creating or updating resources.

## Security boundaries

- Clients must not put database passwords, model API keys, or MCP tokens in the agent run body.
- Read APIs do not return plaintext credentials.
- SQL execution applies read-only limits, row limits, timeouts, and audit.
- Local development identity is for trials and integration development only.
- Password auth handles user sessions; production deployment still needs secret management, audit export, access control policy, and operations monitoring.

Continue with [Security](security.md).
