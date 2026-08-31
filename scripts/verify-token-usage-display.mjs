/**
 * Live API + frontend state verification for token_usage display.
 * Replays AG-UI events through live-run-state and prints overview/detail token stats.
 */
import { createAuthenticatedTestClient } from "./lib/authenticated-test-client.mjs";
import {
  createInitialLiveRun,
  deriveRunUsage,
  reduceLiveRunEvent,
  resolveTokenUsageForEvent,
} from "../apps/web/src/app/data-tasks/live-run-state.ts";

const API_BASE = process.env.API_BASE_URL ?? "http://127.0.0.1:8787";
const client = createAuthenticatedTestClient({ baseUrl: API_BASE });
await client.registerAndLogin({ displayName: "Token Usage Verify" });
const threadId = `token-verify-${Date.now()}`;
const runId = `token-verify-run-${Date.now()}`;

const response = await client.fetch("/api/copilotkit", {
  method: "POST",
  headers: {
    Accept: "text/event-stream",
    "Content-Type": "application/json",
  },
  body: JSON.stringify({
    method: "agent/run",
    params: { agentId: "dataFoundry" },
    body: {
      threadId,
      runId,
      messages: [
        {
          id: "msg-1",
          role: "user",
          content: "用 inspect_schema 检查 orders 表结构，不要执行 SQL。",
        },
      ],
      tools: [],
      context: [],
      state: {},
      forwardedProps: {
        datasourceId: "api-duckdb-demo",
        enabledDatasourceIds: ["api-duckdb-demo"],
      },
    },
  }),
});

if (!response.ok) {
  console.error("CopilotKit run failed:", response.status, await response.text());
  process.exit(1);
}

const text = await response.text();
const events = text
  .split("\n\n")
  .map((chunk) => chunk.trim())
  .filter((chunk) => chunk.startsWith("data: "))
  .map((chunk) => JSON.parse(chunk.slice("data: ".length)));

const tokenUsageEvents = events.filter(
  (event) => event.type === "CUSTOM" && event.name === "token_usage",
);
const correlationEvents = events.filter(
  (event) => event.type === "CUSTOM" && event.name === "token_usage.correlation",
);

let liveRun = createInitialLiveRun();
for (const event of events) {
  liveRun = reduceLiveRunEvent(liveRun, event);
}

const runUsage = deriveRunUsage(liveRun);
const queryEvents = liveRun.events.filter((event) => event.kind === "query" || event.kind === "inspect");

console.log("=== Token usage CUSTOM events from API ===");
console.log(`token_usage count: ${tokenUsageEvents.length}`);
console.log(`token_usage.correlation count: ${correlationEvents.length}`);
for (const [index, event] of tokenUsageEvents.entries()) {
  console.log(`  [${index + 1}]`, JSON.stringify(event.value));
}
for (const [index, event] of correlationEvents.entries()) {
  console.log(`  corr [${index + 1}]`, JSON.stringify(event.value));
}

console.log("\n=== Overview (deriveRunUsage) ===");
console.log(
  JSON.stringify({
    tokenUsageReported: runUsage.tokenUsageReported,
    inputTokens: runUsage.tokens.inputTokens,
    outputTokens: runUsage.tokens.outputTokens,
    total: runUsage.tokens.inputTokens + runUsage.tokens.outputTokens,
    models: runUsage.models,
    toolCalls: runUsage.toolCalls.total,
  }),
);

console.log("\n=== Detail (resolveTokenUsageForEvent per trace step) ===");
for (const event of queryEvents.length > 0 ? queryEvents : liveRun.events) {
  const usage = resolveTokenUsageForEvent(liveRun, event);
  console.log(
    `  ${event.kind} · ${event.title} (id=${event.id}, stepId=${event.stepId ?? "—"})`,
  );
  console.log(
    `    reported=${usage.reported} in=${usage.inputTokens} out=${usage.outputTokens} approximate=${usage.approximate ?? false}`,
  );
}

const detailWithTokens = liveRun.events.filter(
  (event) => resolveTokenUsageForEvent(liveRun, event).reported,
).length;
const overviewHasTokens = runUsage.tokenUsageReported;
const correlationForSql = correlationEvents.some((event) =>
  String(event.value?.step_id ?? "").startsWith("sql-"),
);
const inspectCorrelation = correlationEvents.some((event) =>
  String(event.value?.tool_name) === "inspect_schema",
);

console.log("\n=== Expectations ===");
console.log(`overview has tokens: ${overviewHasTokens} (expect true)`);
console.log(`detail steps with tokens: ${detailWithTokens} (expect > 0)`);
console.log(`inspect_schema correlation: ${inspectCorrelation} (expect true if inspect ran)`);
console.log(`finish totalUsage not summed: input < 500000 (actual ${runUsage.tokens.inputTokens})`);

if (!overviewHasTokens || detailWithTokens === 0) {
  console.error("\nVERIFY FAILED: token display expectations not met");
  process.exit(1);
}

if (runUsage.tokens.inputTokens > 500000) {
  console.error("\nVERIFY FAILED: overview tokens look inflated (finish totalUsage leak?)");
  process.exit(1);
}

console.log("\nVERIFY OK");
