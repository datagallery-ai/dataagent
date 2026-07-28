/**
 * Deterministic runtime verification for the 4 data tools (no LLM).
 * Order: list_data_sources -> inspect_schema -> preview_table -> run_sql_readonly
 */
import { readFileSync } from "node:fs";
import { mkdirSync } from "node:fs";
import { join } from "node:path";
import {
  createDataFoundryRunContext,
  createDataFoundryToolRegistry,
} from "../../packages/agent-runtime/dist/index.js";
import { LocalDataGateway } from "../../packages/data-gateway/dist/index.js";
import { createMetadataStore } from "../../packages/metadata/dist/index.js";
import { createVerifiedTestIdentity } from "../lib/metadata-test-identity.mjs";

const envPath = join(process.cwd(), ".env");
try {
  const envText = readFileSync(envPath, "utf8");
  for (const line of envText.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const eq = trimmed.indexOf("=");
    if (eq === -1) continue;
    const key = trimmed.slice(0, eq).trim();
    const value = trimmed.slice(eq + 1).trim();
    if (!process.env[key]) process.env[key] = value;
  }
} catch {
  // optional .env
}

const stamp = Date.now();
const metadataPath = `storage/verify-tools/data-${stamp}/metadata.sqlite`;
mkdirSync(`storage/verify-tools/data-${stamp}`, { recursive: true });

const store = createMetadataStore({ database_path: metadataPath });
const __testIdentity = createVerifiedTestIdentity(store);
const userId = __testIdentity.userId;
const workspaceId = __testIdentity.workspaceId;

const gateway = new LocalDataGateway(store);

const user_id = userId;
const session_id = `verify-session-${stamp}`;
const run_id = `verify-run-${stamp}`;
const datasource_id = "api-duckdb-demo";

store.dataSources.create({
  user_id,
  id: datasource_id,
  name: "API DuckDB Demo",
  type: "duckdb",
  config: { mode: "demo" },
});
store.sessions.create({
  user_id,
  id: session_id,
  title: "verify data tools",
  selected_datasource_id: datasource_id,
});
store.runs.create({
  user_id,
  id: run_id,
  session_id,
  user_input: "verify data tools",
  status: "running",
  datasource_id,
});

const runContext = createDataFoundryRunContext({
  user_id,
  session_id,
  run_id,
  user_input: "verify data tools",
  chat_mode: "copilotkit",
  selected_datasource_id: datasource_id,
  enabled_datasource_ids: [datasource_id],
});

const events = [];
const registry = createDataFoundryToolRegistry({
  dataGateway: gateway,
  emitter: { emit: (e) => events.push(e) },
  runContext,
});

function truncateJson(value, max = 300) {
  const text = JSON.stringify(value);
  return text.length <= max ? text : `${text.slice(0, max - 3)}...`;
}

function describeShape(value) {
  if (value === null) return "null";
  if (Array.isArray(value)) return `array(len=${value.length})`;
  if (typeof value !== "object") return typeof value;
  const keys = Object.keys(value);
  const parts = keys.slice(0, 8).map((k) => {
    const v = value[k];
    if (Array.isArray(v)) return `${k}:array(${v.length})`;
    if (v && typeof v === "object") return `${k}:object`;
    return `${k}:${typeof v}`;
  });
  return `{ ${parts.join(", ")}${keys.length > 8 ? ", ..." : ""} }`;
}

function makeExecCtx(toolName) {
  const customChunks = [];
  const execCtx = {
    context: { requestContext: new Map() },
    mastra: undefined,
    agentName: "verify",
    name: toolName,
    writer: {
      custom: async (c) => customChunks.push(c),
      write: async (c) => customChunks.push({ write: c }),
    },
  };
  return { execCtx, customChunks };
}

async function runTool(toolName, args) {
  const tool = registry.mastraTools[toolName];
  if (!tool?.execute) {
    throw new Error(`Tool missing: ${toolName}`);
  }

  const { execCtx, customChunks } = makeExecCtx(toolName);
  const eventStart = events.length;

  let ok = true;
  let error = null;
  let result = null;

  try {
    result = await tool.execute(args, execCtx);
  } catch (err) {
    ok = false;
    error = err instanceof Error ? err.message : String(err);
  }

  const toolEvents = events.slice(eventStart);
  const eventTypes = [...new Set(toolEvents.map((e) => e.type))];
  const customEventNames = toolEvents
    .filter((e) => e.type === "CUSTOM")
    .map((e) => e.name ?? "(unnamed)");
  const dataChunkTypes = customChunks
    .map((c) => (c && typeof c === "object" && "type" in c ? c.type : null))
    .filter((t) => typeof t === "string" && t.startsWith("data-"));

  return {
    tool: toolName,
    ok,
    error,
    returnShape: ok ? describeShape(result) : "—",
    returnSample: ok ? truncateJson(result) : error,
    dataChunkTypes: [...new Set(dataChunkTypes)],
    agUiEventTypes: eventTypes,
    agUiCustomNames: [...new Set(customEventNames)],
    notes: [],
  };
}

const results = [];
let schema_id = null;

try {
  results.push(await runTool("list_data_sources", { enabled_only: true }));

  {
    const toolName = "inspect_schema";
    const tool = registry.mastraTools[toolName];
    const { execCtx, customChunks } = makeExecCtx(toolName);
    const eventStart = events.length;
    let ok = true;
    let error = null;
    let result = null;
    try {
      result = await tool.execute({ datasource_id }, execCtx);
      schema_id = result?.schema_id ?? null;
    } catch (err) {
      ok = false;
      error = err instanceof Error ? err.message : String(err);
    }
    const toolEvents = events.slice(eventStart);
    results.push({
      tool: toolName,
      ok,
      error,
      returnShape: ok ? describeShape(result) : "—",
      returnSample: ok ? truncateJson(result) : error,
      dataChunkTypes: [
        ...new Set(
          customChunks
            .map((c) => (c && typeof c === "object" && "type" in c ? c.type : null))
            .filter((t) => typeof t === "string" && t.startsWith("data-")),
        ),
      ],
      agUiEventTypes: [...new Set(toolEvents.map((e) => e.type))],
      agUiCustomNames: [
        ...new Set(
          toolEvents.filter((e) => e.type === "CUSTOM").map((e) => e.name ?? "(unnamed)"),
        ),
      ],
      notes: schema_id ? [`schema_id=${schema_id}`] : [],
    });
  }

  if (!schema_id) {
    results.push({
      tool: "preview_table",
      ok: false,
      error: "blocked: no schema_id from inspect_schema",
      returnShape: "—",
      returnSample: "blocked",
      dataChunkTypes: [],
      agUiEventTypes: [],
      agUiCustomNames: [],
      notes: ["skipped — inspect_schema did not yield schema_id"],
    });
    results.push({
      tool: "run_sql_readonly",
      ok: false,
      error: "blocked: no schema_id from inspect_schema",
      returnShape: "—",
      returnSample: "blocked",
      dataChunkTypes: [],
      agUiEventTypes: [],
      agUiCustomNames: [],
      notes: ["skipped — inspect_schema did not yield schema_id"],
    });
  } else {
    results.push(
      await runTool("preview_table", { schema_id, table: "orders", limit: 3 }),
    );
    results.push(
      await runTool("run_sql_readonly", {
        schema_id,
        sql: "SELECT * FROM orders LIMIT 3",
        limit: 10,
      }),
    );
  }

  let exitCode = 0;
  for (const r of results) {
    if (!r.ok) exitCode = 1;
  }

  console.log(JSON.stringify({ exitCode, metadataPath, results }, null, 2));
  process.exitCode = exitCode;
} finally {
  store.close();
}
