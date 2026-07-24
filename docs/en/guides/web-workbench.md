# Web workbench guide

This guide is for users trying DataFoundry in the browser. After reading it, you can create data tasks, select resources, stop runs, view trace and outputs, and restore server sessions.

## How to start

Default path is the **formal** stack (`password` + `build` / `start`). Formal test and real production share the same commands; email and public URL differ in the root `.env` — see [Quick start](../quick-start.md).

```bash
npm run build && npm run build:web
npm run start:api
npm run start:web
```

Open:

```text
http://127.0.0.1:3000/login
```

Then go to `/data-tasks` after sign-in.

Formal frontend settings (`apps/web/.env.local`; re-run `build:web` after changing `NEXT_PUBLIC_*`):

```bash
NEXT_PUBLIC_DATAFOUNDRY_AUTH_MODE=password
NEXT_PUBLIC_AGENT_RUNTIME_URL=
NEXT_PUBLIC_CONFIG_API_URL=
API_PROXY_TARGET=http://127.0.0.1:8787
```

The browser uses the same-origin Next BFF (`/api/v1/*`, `/api/copilotkit`). Point the BFF upstream with `API_PROXY_TARGET` (default `http://127.0.0.1:8787`).

Probes:

- Liveness: `GET /healthz`
- Readiness: `GET /ready` (includes `startup_ms` / `phases`)

Reverse-proxy sample (real production): [`deploy/nginx.datafoundry.conf.example`](https://github.com/datagallery-lab/datafoundry/blob/main/deploy/nginx.datafoundry.conf.example).

Contributor hot-reload (not formal; do not mix with `start:*`): [Quick start appendix](../quick-start.md).

## Identity and first-run guide

Formal (`password`) mode shows sign-in, register, password reset, and sign-out. Formal test prints verification links in the API console; real production sends email via SMTP.

The Web workbench sends the same identity to configuration REST requests and CopilotKit agent runs. When the user changes, the workbench remounts the data-task area so sessions, selected resources, files, outputs, live run state, and onboarding progress stay in the current user scope.

First-time users see the quick guide. Reopen it with the circular `?` near the user area. The guide can fill an example prompt and waits until you actually send a task before advancing to the console step.

## Layout

The Web workbench has three columns:

| Area | Purpose |
| --- | --- |
| Left | Manage sessions and workspace resources. |
| Center | Enter questions; view agent replies, step cards, and human confirmations. |
| Right | Overview, trace, outputs, step details, and workspace files. |

On narrow windows, the right console collapses into a drawer. Use the console entry at the top of the chat area to reopen it.

## Run your first task

1. Click **New data task** on the left.
2. Select the built-in **DTC Growth Review** data source.
3. Select **Server default** or your configured model next to the input box.
4. Enter a question and send.

Example:

```text
List the tables in this data source and describe the main fields of each table.
```

After sending, the center shows agent replies and step cards. The right console shows run status, trace, and outputs in sync.

## Left panel: sessions and resources

The left panel manages two kinds of content:

| Content | What you can do |
| --- | --- |
| Data tasks | Create, switch, rename, pin, and delete tasks. |
| Workspace resources | Manage data sources, knowledge bases, agent tools, and assets. |
| User menu | View the current user, open settings, sign out, or switch local development users. |

Resource entry points:

| Resource | Purpose |
| --- | --- |
| Data Sources | Add sources, test connections, fetch schema, browse table structure, and preview table data. |
| Knowledge | Create knowledge bases, upload documents, import file assets, rebuild indexes. |
| Agent Tools | Manage MCP servers and Skills. |
| Assets | View, download, and delete reusable workspace files. |

Model selection is not in the left resource area. Use the model selector next to the input box for the model used in the current analysis.

## Center: conversation, steps, and confirmations

The center panel handles questions and shows the agent analysis process. During a run you see:

- Streaming agent replies.
- Step cards for schema inspection, SQL execution, file reads, and similar actions.
- States such as waiting for input, running, completed, failed, or canceled.
- Human confirmation flows when the backend asks you to choose an option or add information.

Click a step card to open its details in the right console. Use this path to verify inputs, tool calls, and outputs for each step.

## Input box features

| Feature | Purpose |
| --- | --- |
| Model selection | Choose an LLM profile for this analysis. |
| Session resource toggles | Control data sources, knowledge bases, MCP servers, and Skills available in this session. |
| `@` mentions | Pin a data source, file, or resource for a single question. |
| File upload | Upload attachments needed for this analysis. |
| Stop run | Cancel the running agent run. |

If you submit another question while a run is active, the Web workbench keeps it in a prompt queue instead of mixing it into the current run. You can edit or remove queued prompts, send one immediately, or let the next prompt dispatch after the active run reaches a terminal state. Queues belong to the current task session.

Resource selection has three layers:

1. Workspace defaults: long-term configuration in the left resource panel.
2. Session resources: toggles below the input box; affect only the current task.
3. Question resources: `@` mentions for a single turn.

For a first trial, keep the defaults.

## Right panel: task console

### Overview

Overview shows the current question, step count, success rate, output count, token usage, and a live step list. Use it to tell whether the task is still running, failed, or already produced files or tables.

### Trace

Trace provides both a time-ordered execution list and a semantic DAG backed by persisted checkpoints. The graph connects runs, steps, tool calls, outputs, and their parent relationships, while the list remains the fastest way to scan chronological progress. Common entries:

1. Start run.
2. Inspect data source schema.
3. Run read-only SQL or read files.
4. Create table, chart, SQL, report, or file outputs.

When you need to explain where a result came from, start with trace, then open step details. Restored sessions rebuild this view from server records rather than treating the browser transcript as the source of truth.

### Outputs

Outputs are reusable results left by the agent:

| Type | Actions |
| --- | --- |
| Tables | Preview, search, sort, download, or export CSV/XLSX. |
| Charts | Preview, download, or export supported backend formats. |
| SQL | View queries executed by the agent. |
| Reports | View structured conclusions and Markdown content. |
| Files | Download, or add to the workspace and reference in later tasks. |

File outputs can be promoted to cross-session workspace files with **Add to workspace**. Table and chart download/export use `/api/v1/artifacts/:id/download` and `/api/v1/artifacts/:id/export`.

Open an output to reference it in a follow-up. You can reference the complete artifact or, for supported tables and text, select only the relevant region. The selected evidence appears beside the input and is resolved by the server before the next run starts.

### Details

Details show inputs, outputs, token usage, tool call arguments, and tool results for a single step. Open details from a center step card or from the trace list.

### Workspace files

The workspace files panel supports:

- Uploading files into the active session and promoting successful uploads into the workspace.
- Listing reusable files.
- Downloading file content.
- Deleting file references.

Chat attachments are uploaded from the input box via `/api/v1/chat/uploads`. After a file output is added to the workspace, it appears in the workspace file list.

## Data source browsing

The data source panel supports two read-only views:

| Feature | API |
| --- | --- |
| Schema browse | `GET /api/v1/datasources/:id/schema` |
| Table preview | `GET /api/v1/datasources/:id/tables/:table/preview` |

Schema browse supports search by table or field name. Table preview supports `schema`, `limit`, `offset`, and `orderBy` query parameters.

## Stop run

When a run takes too long or the question was wrong, use the stop button near the input box. The frontend calls:

```text
POST /api/v1/runs/:id/cancel
```

The backend cancels the active run or marks a persisted run as canceled. Events and outputs already produced remain available for troubleshooting.

## Session restore

The Web workbench restores history through server session APIs:

| API | Purpose |
| --- | --- |
| `GET /api/v1/sessions` | Read the left session list. |
| `PATCH /api/v1/sessions/:id` | Update session title. |
| `DELETE /api/v1/sessions/:id` | Permanently delete a session (including history and child branches); the sidebar delete action calls this. |
| `GET /api/v1/sessions/:id/conversation` | Restore conversation, tool calls, pending interactions, and run events. |
| `GET /api/v1/artifacts?sessionId=:id` | Restore outputs for that session. |

After refresh or reopening the workbench, restore prior tasks from the left task list. The center shows conversation history; the right console can replay run events and outputs. New questions continue the session context for that task.

### Re-ask and branch

For a completed historical turn, edit or re-ask the earlier question to create a persistent child branch. You can also branch from a checkpoint exposed by the restored conversation. DataFoundry keeps the original history, switches the workbench to the child session, and exposes branch navigation at the fork point so alternative analyses remain comparable.

Branch creation requires a terminal run or persisted checkpoint. A running or suspended turn is not treated as a stable fork boundary.

## Data Link

**Data Link** opens a workspace graph for tables, columns, concepts, entities, and their relationships. It is available when the workspace has a compatible Data Link or DataGraph MCP server with the expected tools. Use the graph to search entry nodes, expand related structure, and inspect semantic context before asking the agent to analyze it.

DataFoundry does not embed a semantic graph service. If no compatible MCP server is configured, the panel cannot provide graph data; configure the integration under Agent Tools first.

## Example questions

| Scenario | Example |
| --- | --- |
| Explore tables | `What tables are in this data source? What are the main fields?` |
| Metric aggregation | `Count orders and GMV by channel.` |
| Trend analysis | `Analyze GMV trend over the last 30 days and find the day with the largest swing.` |
| Follow-up | `Break that down by category.` |
| Named source | `Using the sales-pg data source, summarize orders over the last 7 days.` |

## Capability boundaries

- File upload is controlled by `chat.fileUpload` and `files` capabilities.
- Image input is controlled by `chat.imageInput`.
- Knowledge, MCP, and Skill behavior depends on workspace configuration and `knowledge`, `mcp`, and `skills` capabilities.
- Database queries go through Data Gateway on read-only analysis paths only.
- Credentials are submitted only when creating or updating resources; read APIs do not return plaintext secrets.

Continue with [Data sources guide](data-sources.md).
