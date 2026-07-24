/**
 * Live smoke: trigger ask_user via CopilotKit and verify suspend stream events.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { createAuthenticatedTestClient } from "./lib/authenticated-test-client.mjs";

const envPath = join(process.cwd(), ".env");
try {
  for (const line of readFileSync(envPath, "utf8").split("\n")) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const eq = trimmed.indexOf("=");
    if (eq === -1) continue;
    const key = trimmed.slice(0, eq).trim();
    const value = trimmed.slice(eq + 1).trim();
    if (!process.env[key]) process.env[key] = value;
  }
} catch {
  // optional
}

if (!process.env.LLM_API_KEY) {
  console.error("LLM_API_KEY missing — cannot run live ask_user smoke.");
  process.exit(1);
}

const apiBase = `http://127.0.0.1:${process.env.API_PORT ?? "8787"}`;
const client = createAuthenticatedTestClient({ baseUrl: apiBase });
await client.registerAndLogin({ displayName: "Ask User Smoke" });
const threadId = `thread-ask-smoke-${Date.now()}`;
const runId = randomUUID();

const payload = {
  method: "agent/run",
  params: { agentId: "dataFoundry" },
  body: {
    threadId,
    runId,
    messages: [
      {
        id: randomUUID(),
        role: "user",
        content:
          "你必须调用 ask_user 工具，向用户提问「你想分析哪个数据源？」并提供两个选项 orders 和 customers。不要自行假设答案，不要执行 SQL，只调用 ask_user 然后等待。",
      },
    ],
    tools: [],
    context: [{ description: "datasource_id", value: "api-duckdb-demo" }],
    state: {},
    forwardedProps: { datasourceId: "api-duckdb-demo" },
  },
};

const response = await client.fetch("/api/copilotkit", {
  method: "POST",
  headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
  body: JSON.stringify(payload),
});

if (!response.ok) {
  console.error("HTTP", response.status, await response.text());
  process.exit(1);
}

const events = [];
const decoder = new TextDecoder();
let buffer = "";

for await (const chunk of response.body) {
  buffer += decoder.decode(chunk, { stream: true });
  const parts = buffer.split("\n\n");
  buffer = parts.pop() ?? "";
  for (const part of parts) {
    for (const line of part.split("\n")) {
      if (!line.startsWith("data: ")) continue;
      const raw = line.slice(6).trim();
      if (!raw || raw === "[DONE]") continue;
      try {
        events.push(JSON.parse(raw));
      } catch {
        // ignore non-json lines
      }
    }
  }
}

const customNames = events
  .filter((event) => event.type === "CUSTOM")
  .map((event) => event.name);
const hasOnInterrupt = customNames.includes("on_interrupt");
const hasInteractionRequested = customNames.includes("interaction.requested");
const hasRunFinished = events.some((event) => event.type === "RUN_FINISHED");
const runErrors = events.filter((event) => event.type === "RUN_ERROR");
const hasIncompleteStreamError = runErrors.some((event) =>
  String(event.message ?? "").includes("Run ended without emitting a terminal event"),
);
const suspendedDelta = events.some(
  (event) =>
    event.type === "STATE_DELTA" &&
    JSON.stringify(event.delta ?? []).includes("suspended"),
);

console.log("Event counts:", {
  total: events.length,
  custom: customNames.length,
  runErrors: runErrors.length,
});
console.log("Custom names:", [...new Set(customNames)]);
console.log("Checks:", {
  hasOnInterrupt,
  hasInteractionRequested,
  hasRunFinished,
  hasIncompleteStreamError,
  suspendedDelta,
});

if (runErrors.length > 0) {
  console.log("RUN_ERROR messages:", runErrors.map((event) => event.message));
}

const ok =
  hasOnInterrupt &&
  hasInteractionRequested &&
  hasRunFinished &&
  !hasIncompleteStreamError &&
  suspendedDelta;

if (!ok) {
  console.error("ask_user interrupt smoke FAILED");
  process.exit(1);
}

console.log("ask_user interrupt smoke OK");
