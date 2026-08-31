/**
 * E2E agent run with user prompt "测试每一个tool" (calls real LLM).
 */
import { mkdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { MastraAgent } from "@ag-ui/mastra";
import { EventType } from "@ag-ui/client";
import {
  createDataFoundry,
  createDataFoundryRunContext,
  createModelProviderFromEnv,
  createTaskStateRuntime,
} from "../packages/agent-runtime/dist/index.js";
import { LocalDataGateway } from "../packages/data-gateway/dist/index.js";
import { createMetadataStore } from "../packages/metadata/dist/index.js";
import { LocalKnowledgeService } from "../packages/knowledge/dist/index.js";
import { createVerifiedTestIdentity } from "./lib/metadata-test-identity.mjs";

const USER_PROMPT = "测试每一个tool";

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
  console.error("LLM_API_KEY missing — set it in .env");
  process.exit(1);
}

const stamp = Date.now();
const storageDir = `storage/test-every-tool/${stamp}`;
mkdirSync(storageDir, { recursive: true });
const metadataPath = `${storageDir}/metadata.sqlite`;
const taskDbPath = `${storageDir}/task-state.sqlite`;
const store = createMetadataStore({ database_path: metadataPath });
const __testIdentity = createVerifiedTestIdentity(store);
const userId = __testIdentity.userId;
const workspaceId = __testIdentity.workspaceId;

const gateway = new LocalDataGateway(store);

const user_id = userId;
const session_id = `test-every-tool-session-${stamp}`;
const run_id = `test-every-tool-run-${stamp}`;
const datasource_id = "api-duckdb-demo";
const knowledge_id = "verify-kb";

const embedding = {
  provider: process.env.EMBEDDING_PROVIDER ?? "",
  model: process.env.EMBEDDING_MODEL ?? "",
  base_url: process.env.EMBEDDING_BASE_URL ?? "",
  api_key: process.env.EMBEDDING_API_KEY ?? "",
};

const knowledgeService = new LocalKnowledgeService(store, {
  embedding: {
    provider: embedding.provider,
    model: embedding.model,
    base_url: embedding.base_url,
    ...(embedding.api_key ? { api_key: embedding.api_key } : {}),
  },
});

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
  title: USER_PROMPT,
  selected_datasource_id: datasource_id,
});
store.runs.create({
  user_id,
  id: run_id,
  session_id,
  user_input: USER_PROMPT,
  status: "running",
  datasource_id,
});

const runContext = createDataFoundryRunContext({
  user_id,
  session_id,
  run_id,
  user_input: USER_PROMPT,
  chat_mode: "copilotkit",
  selected_datasource_id: datasource_id,
  enabled_datasource_ids: [datasource_id],
  enabled_knowledge_ids: [knowledge_id],
  model_name: modelProvider.model_name,
});

const toolCalls = new Map();
const customEvents = [];
let runError = null;

const record = (event) => {
  if (event.type === EventType.CUSTOM) {
    customEvents.push(event.name);
  }
  if (
    event.type === EventType.TOOL_CALL_START ||
    event.type === EventType.TOOL_CALL_RESULT
  ) {
    const id = event.toolCallId ?? "?";
    const name = event.toolCallName ?? event.name ?? "?";
    const entry = toolCalls.get(id) ?? { name, start: false, result: false, failed: false };
    if (event.type === EventType.TOOL_CALL_START) entry.start = true;
    if (event.type === EventType.TOOL_CALL_RESULT) {
      entry.result = true;
      const content = String(event.content ?? event.result ?? "");
      if (content.includes('"isError":true') || content.includes("status\":\"error")) {
        entry.failed = true;
      }
    }
    toolCalls.set(id, entry);
  }
  if (event.type === EventType.RUN_ERROR) {
    runError = event.message ?? JSON.stringify(event);
  }
};

const taskStateRuntime = await createTaskStateRuntime(taskDbPath);

try {
  const { agent, destroyWorkspace, governedMessages } = await createDataFoundry({
    dataGateway: gateway,
    knowledgeService,
    emitter: { emit: record },
    messages: [{ id: "msg-1", role: "user", content: USER_PROMPT }],
    modelProvider,
    runContext,
    taskStateRuntime,
    workspaceRoot: join(process.cwd(), "storage/workspaces"),
  });

  const mastraAgent = new MastraAgent({ agent, resourceId: user_id });

  console.log(`Starting agent run: "${USER_PROMPT}"\n`);

  await new Promise((resolve, reject) => {
    mastraAgent
      .run({
        threadId: session_id,
        runId: run_id,
        messages: governedMessages,
        tools: [],
        context: [],
        forwardedProps: { datasourceId: datasource_id },
        state: {},
      })
      .subscribe({
        next: record,
        error: reject,
        complete: resolve,
      });
  });

  await destroyWorkspace().catch(() => undefined);

  const toolsUsed = [...new Set([...toolCalls.values()].map((v) => v.name))].sort();
  const failed = [...toolCalls.values()].filter((v) => v.failed);

  console.log("\n=== 测试每一个tool — Summary ===");
  console.log(`Tools invoked (${toolsUsed.length}): ${toolsUsed.join(", ") || "(none)"}`);
  console.log(`Tool call count: ${toolCalls.size}`);
  console.log(`CUSTOM events: ${[...new Set(customEvents)].join(", ") || "(none)"}`);

  if (runError) {
    console.error(`RUN_ERROR: ${runError}`);
    process.exitCode = 1;
  } else if (failed.length > 0) {
    console.error(`Failed tool results: ${failed.map((f) => f.name).join(", ")}`);
    process.exitCode = 1;
  } else if (toolsUsed.length === 0) {
    console.error("No tools were invoked");
    process.exitCode = 1;
  } else {
    console.log("Agent run completed without RUN_ERROR.");
  }
} finally {
  await taskStateRuntime.close();
  store.close();
}
