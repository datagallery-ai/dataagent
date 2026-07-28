/**
 * Diagnose TOOL_CALL_* event delivery for server-side Mastra tools.
 * Logs whether TOOL_CALL_RESULT follows TOOL_CALL_END for each toolCallId.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { MastraAgent } from "@ag-ui/mastra";
import { EventType } from "@ag-ui/client";
import { ToolCallResultBridge } from "../apps/api/dist/tool-call-result-bridge.js";
import {
  createDataFoundry,
  createDataFoundryRunContext,
} from "../packages/agent-runtime/dist/index.js";
import { LocalDataGateway } from "../packages/data-gateway/dist/index.js";
import { createMetadataStore } from "../packages/metadata/dist/index.js";
import { createModelProviderFromEnv } from "../packages/agent-runtime/dist/index.js";

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

const modelProvider = createModelProviderFromEnv(process.env);
if (modelProvider.kind === "mock") {
  console.error("LLM_API_KEY missing — set it in datafoundry/.env to run this diagnostic.");
  process.exit(1);
}

const stamp = Date.now();
const metadataPath = `storage/diagnose-tool-result/${stamp}/metadata.sqlite`;
const store = createMetadataStore({ database_path: metadataPath });
const gateway = new LocalDataGateway(store);

const user_id = process.env.DATAFOUNDRY_USER_ID;
if (!user_id || !process.env.DATAFOUNDRY_WORKSPACE_ID) {
  throw new Error("diagnose-tool-result-events requires DATAFOUNDRY_USER_ID and DATAFOUNDRY_WORKSPACE_ID");
}
const workspace_id = process.env.DATAFOUNDRY_WORKSPACE_ID;
const session_id = `diag-session-${stamp}`;
const run_id = `diag-run-${stamp}`;
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
  title: "diagnose tool result",
  selected_datasource_id: datasource_id,
});
store.runs.create({
  user_id,
  id: run_id,
  session_id,
  user_input: "按 category 汇总 orders 表的 gmv 销售额",
  status: "running",
  datasource_id,
});

const runContext = createDataFoundryRunContext({
  user_id,
  session_id,
  run_id,
  user_input: "按 category 汇总 orders 表的 gmv 销售额",
  chat_mode: "copilotkit",
  selected_datasource_id: datasource_id,
  model_name: modelProvider.model_name,
});

const toolEvents = [];
const activitySteps = [];
const customEvents = [];
const bridge = new ToolCallResultBridge();
const endedToolCalls = new Map();
const resultToolCalls = new Map();

const recordToolEvent = (event, source = "stream") => {
  toolEvents.push(event);
  const type = event.type;
  const id = event.toolCallId ?? "(no-id)";
  const name = event.toolCallName ?? event.name ?? "?";
  const contentLen =
    typeof event.content === "string"
      ? event.content.length
      : typeof event.result === "string"
        ? event.result.length
        : 0;

  if (type === EventType.TOOL_CALL_END) {
    endedToolCalls.set(id, { name, at: toolEvents.length });
  }
  if (type === EventType.TOOL_CALL_RESULT) {
    resultToolCalls.set(id, { name, contentLen, at: toolEvents.length });
  }

  const tag = source === "bridge" ? " [bridged]" : "";
  console.log(
    `[${toolEvents.length}] ${type}${tag} id=${id} name=${name}` +
      (contentLen ? ` contentLen=${contentLen}` : ""),
  );
};

const deliverEvent = (event, source = "stream") => {
  if (
    event.type === EventType.ACTIVITY_SNAPSHOT &&
    event.activityType === "STEP"
  ) {
    activitySteps.push(event);
  }
  if (event.type === EventType.CUSTOM) {
    customEvents.push(event);
  }

  if (
    event.type === EventType.TOOL_CALL_START ||
    event.type === EventType.TOOL_CALL_ARGS ||
    event.type === EventType.TOOL_CALL_END ||
    event.type === EventType.TOOL_CALL_RESULT
  ) {
    recordToolEvent(event, source);
  }

  if (event.type === EventType.RUN_FINISHED || event.type === EventType.RUN_ERROR) {
    for (const bridged of bridge.flushPendingResults()) {
      deliverEvent(bridged, "bridge");
    }
  }

  for (const bridged of bridge.observe(event)) {
    deliverEvent(bridged, "bridge");
  }
};

const { agent } = createDataFoundry({
  dataGateway: gateway,
  emitter: {
    emit: (event) => deliverEvent(event, "activity"),
  },
  modelProvider,
  runContext,
});

const mastraAgent = new MastraAgent({ agent, resourceId: user_id });

const runInput = {
  threadId: session_id,
  runId: run_id,
  messages: [
    {
      id: "msg-user-1",
      role: "user",
      content: "按 category 汇总 orders 表的 gmv 销售额，先检查 schema 再写 SQL",
    },
  ],
  tools: [],
  context: [],
  forwardedProps: { datasourceId: datasource_id },
  state: {},
};

console.log("Starting agent run (this calls the real LLM)...\n");

await new Promise((resolve, reject) => {
  mastraAgent.run(runInput).subscribe({
    next: (event) => deliverEvent(event, "mastra"),
    error: (err) => reject(err),
    complete: () => resolve(undefined),
  });
});

console.log("\n=== Summary ===");
console.log(`TOOL events total: ${toolEvents.length}`);
console.log(`ACTIVITY STEP snapshots: ${activitySteps.length}`);
console.log(`CUSTOM events: ${customEvents.length}`);

for (const [id, info] of endedToolCalls) {
  const result = resultToolCalls.get(id);
  if (result) {
    console.log(`OK  ${id} (${info.name}): END@${info.at} -> RESULT@${result.at} len=${result.contentLen}`);
  } else {
    console.log(`MISSING RESULT  ${id} (${info.name}): END@${info.at} but no TOOL_CALL_RESULT`);
  }
}

for (const [id, info] of resultToolCalls) {
  if (!endedToolCalls.has(id)) {
    console.log(`ORPHAN RESULT  ${id} (${info.name}): RESULT without END`);
  }
}

const completedSteps = activitySteps.filter(
  (e) => e.content?.status === "completed" && e.content?.tool_name,
);
console.log("\nCompleted ACTIVITY steps:");
for (const step of completedSteps) {
  const c = step.content;
  const rowCount = c?.content?.row_count ?? c?.content?.tables?.length ?? "?";
  console.log(
    `  ${c.tool_name} step=${c.step_id} output=${c.output_type ?? "json"} rows/meta=${rowCount}`,
  );
}

const missing = [...endedToolCalls.keys()].filter((id) => !resultToolCalls.has(id));
if (missing.length > 0) {
  console.log("\nDIAGNOSIS: raw @ag-ui/mastra stream missing TOOL_CALL_RESULT for:");
  for (const id of missing) {
    console.log(`  - ${id} (${endedToolCalls.get(id)?.name})`);
  }
  console.log("Bridge can backfill from ACTIVITY STEP snapshots / RUN_FINISHED.");
  process.exitCode = 1;
} else {
  console.log("\nAll tool calls received TOOL_CALL_RESULT (including bridged events).");
}

store.close();
