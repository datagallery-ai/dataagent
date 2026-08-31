/**
 * Regression: resume with CopilotKit runId != interrupt.runId must not crash API.
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
  console.error("LLM_API_KEY missing — cannot run interaction run-id smoke.");
  process.exit(1);
}

const apiBase = `http://127.0.0.1:${process.env.API_PORT ?? "8787"}`;
const client = createAuthenticatedTestClient({ baseUrl: apiBase });
await client.registerAndLogin({ displayName: "Interaction RunId Smoke" });
const threadId = `thread-run-id-smoke-${Date.now()}`;
const originalRunId = randomUUID();
const mismatchedResumeRunId = randomUUID();

const askPrompt =
  "你必须调用 ask_user 工具，向用户提问「继续吗？」并提供两个选项 Yes 和 No。不要执行 SQL，只调用 ask_user 然后等待。";

const suspendEvents = await runAgent({
  threadId,
  runId: originalRunId,
  messages: [{ id: randomUUID(), role: "user", content: askPrompt }],
});

const interruptRaw = suspendEvents.find(
  (event) => event.type === "CUSTOM" && event.name === "on_interrupt",
)?.value;
if (!interruptRaw) {
  console.error("Expected on_interrupt during suspend phase");
  process.exit(1);
}

const interrupt =
  typeof interruptRaw === "string" ? JSON.parse(interruptRaw) : interruptRaw;
if (interrupt.runId !== originalRunId) {
  console.error(
    `Unexpected interrupt.runId=${interrupt.runId}, expected ${originalRunId}`,
  );
  process.exit(1);
}

const resumeEvents = await runAgent({
  threadId,
  runId: mismatchedResumeRunId,
  messages: [
    { id: randomUUID(), role: "user", content: askPrompt },
    {
      id: randomUUID(),
      role: "assistant",
      content: interrupt.args?.question ?? "继续吗？",
      toolCalls: [
        {
          id: interrupt.toolCallId,
          type: "function",
          function: {
            name: interrupt.toolName,
            arguments: JSON.stringify(interrupt.args ?? {}),
          },
        },
      ],
    },
  ],
  forwardedProps: {
    datasourceId: "api-duckdb-demo",
    command: {
      resume: "No",
      interruptEvent:
        typeof interruptRaw === "string" ? interruptRaw : JSON.stringify(interrupt),
    },
  },
});

const runErrors = resumeEvents.filter((event) => event.type === "RUN_ERROR");
const mismatch = runErrors.some((event) =>
  String(event.message ?? "").includes("INTERACTION_RUN_MISMATCH"),
);

console.log("Checks:", {
  originalRunId,
  mismatchedResumeRunId,
  resumeEventCount: resumeEvents.length,
  runErrors: runErrors.length,
  mismatch,
  finished: resumeEvents.some((event) => event.type === "RUN_FINISHED"),
});

if (mismatch) {
  console.error("INTERACTION_RUN_MISMATCH still reproduced on resume");
  process.exit(1);
}

if (runErrors.length > 0) {
  console.log(
    "RUN_ERROR messages:",
    runErrors.map((event) => event.message),
  );
}

console.log("interaction run-id smoke OK");

async function runAgent(body) {
  const response = await client.fetch("/api/copilotkit", {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify({
      method: "agent/run",
      params: { agentId: "dataFoundry" },
      body: {
        tools: [],
        context: [{ description: "datasource_id", value: "api-duckdb-demo" }],
        state: {},
        forwardedProps: { datasourceId: "api-duckdb-demo", ...(body.forwardedProps ?? {}) },
        ...body,
      },
    }),
  });

  if (!response.ok) {
    throw new Error(`HTTP ${response.status}: ${await response.text()}`);
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
          // ignore
        }
      }
    }
  }
  return events;
}
