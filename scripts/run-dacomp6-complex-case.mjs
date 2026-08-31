/** Run one dacomp6 complex case against the local real-LLM Agent API. */
import assert from "node:assert/strict";
import { EventType } from "@ag-ui/client";

import { dacomp6ComplexCases, findDacomp6Case } from "./dacomp6-complex-cases.mjs";
import { createAuthenticatedTestClient } from "./lib/authenticated-test-client.mjs";

const baseUrl = (process.env.DATAFOUNDRY_BASE_URL ?? "http://127.0.0.1:8787").replace(/\/$/u, "");

const client = createAuthenticatedTestClient({ baseUrl });
await client.registerAndLogin({ displayName: "DACOMP Smoke" });
const caseId = process.argv[2] ?? "profit-root-cause";

if (caseId === "--list") {
  dacomp6ComplexCases.forEach((item) => console.log(`${item.id}\t${item.title}`));
  process.exit(0);
}

const scenario = findDacomp6Case(caseId);
if (!scenario) {
  throw new Error(`Unknown dacomp6 case: ${caseId}. Use --list to show available cases.`);
}

const stamp = Date.now();
const threadId = `dacomp6-${scenario.id}-${stamp}`;
const runId = `dacomp6-${scenario.id}-run-${stamp}`;
console.log(`Running ${scenario.title}`);
console.log(`Session: ${threadId}`);

const events = await runAgent({
  threadId,
  runId,
  messageId: `message-${stamp}`,
  prompt: scenario.prompt,
});
const terminal = events.findLast((event) =>
  event.type === EventType.RUN_FINISHED || event.type === EventType.RUN_ERROR
);
assert.equal(terminal?.type, EventType.RUN_FINISHED, `Agent run failed: ${JSON.stringify(terminal)}`);

const toolNames = [...new Set(events.flatMap((event) => {
  const name = event.toolCallName ?? event.toolName;
  return typeof name === "string" ? [name] : [];
}))];
const resultCount = events.filter((event) => event.type === EventType.TOOL_CALL_RESULT).length;
assert(resultCount >= 3, `Expected a complex multi-tool run, got ${resultCount} tool results.`);

const trace = await waitForTraceSections(threadId);
assert(trace.sections.length > 0, "Complex run completed without semantic Trace sections.");

console.log(`Tools (${toolNames.length}): ${toolNames.join(", ") || "unknown"}`);
console.log(`Tool results: ${resultCount}`);
console.log(`Trace sections (${trace.sections.length}):`);
trace.sections.forEach((section, index) => {
  console.log(`  ${index + 1}. ${section.title} [${section.status}] (${section.nodeIds.length} nodes)`);
});
console.log(`Open in DataFoundry: http://localhost:3000/data-tasks?thread=${encodeURIComponent(threadId)}`);
process.exit(0);

async function runAgent(input) {
  const response = await client.fetch("/api/copilotkit", {
    method: "POST",
    headers: requestHeaders("text/event-stream"),
    body: JSON.stringify({
      method: "agent/run",
      params: { agentId: "dataFoundry" },
      body: {
        threadId: input.threadId,
        runId: input.runId,
        state: {},
        messages: [{ id: input.messageId, role: "user", content: input.prompt }],
        tools: [],
        context: [],
        forwardedProps: {
          run_config: {
            activeDatasourceId: "custom-datasource",
            activeLlmProfileId: "server-default",
            enabledDatasourceIds: ["custom-datasource"],
            enabledKnowledgeIds: [],
            enabledMcpServerIds: [],
            enabledSkillIds: [],
          },
        },
      },
    }),
    signal: AbortSignal.timeout(12 * 60 * 1000),
  });
  if (!response.ok) {
    throw new Error(`Agent API failed (${response.status}): ${await response.text()}`);
  }
  return parseEventStream(await response.text());
}

async function waitForTraceSections(sessionId) {
  const deadline = Date.now() + 180_000;
  while (Date.now() < deadline) {
    const response = await client.fetch(
      `/api/v1/sessions/${encodeURIComponent(sessionId)}/trace-dag`,
      { headers: requestHeaders("application/json") },
    );
    if (response.ok) {
      const envelope = await response.json();
      const nodes = envelope.data?.nodes;
      const sections = envelope.data?.sections;
      const terminalNode = Array.isArray(nodes) ? nodes.find((node) => node.kind === "run-terminal") : undefined;
      const sectionsFinalized = Array.isArray(sections) && sections.length > 0
        && sections.every((section) => section.status === "completed")
        && terminalNode !== undefined
        && sections.some((section) => section.nodeIds.includes(terminalNode.id));
      if (sectionsFinalized) {
        return envelope.data;
      }
    }
    await new Promise((resolve) => setTimeout(resolve, 1_000));
  }
  throw new Error(`Timed out waiting for Trace sections in session ${sessionId}.`);
}

function parseEventStream(text) {
  return text
    .split("\n\n")
    .map((chunk) => chunk.trim())
    .filter((chunk) => chunk.startsWith("data: "))
    .map((chunk) => chunk.slice("data: ".length))
    .filter((chunk) => chunk !== "[DONE]")
    .map((chunk) => JSON.parse(chunk));
}

function requestHeaders(accept) {
  return {
    Accept: accept,
    "Content-Type": "application/json",
  };
}
