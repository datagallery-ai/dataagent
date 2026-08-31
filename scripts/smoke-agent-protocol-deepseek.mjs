import assert from "node:assert/strict";
import "dotenv/config";

import {
  createModelProtocolClassifier,
  createRunProtocolBoundary
} from "../packages/agent-runtime/dist/testing.js";
import { createModelProvider } from "../packages/providers/dist/index.js";
import { createAuthenticatedTestClient } from "./lib/authenticated-test-client.mjs";

const provider = createModelProvider(process.env);
assert.notEqual(provider.kind, "mock", "DeepSeek credentials are required; fake LLM is not allowed");
const classifier = createModelProtocolClassifier(provider);
const candidates = [
  { protocolId: "general-task", protocolVersion: "1" },
  { protocolId: "data-analysis", protocolVersion: "1" }
];

const general = await classifier({
  candidates,
  value: { userText: "用两句话解释什么是数据仓库，不需要查询任何数据源" }
});
assert.equal(general.protocolId, "general-task");
assert.ok(general.confidence >= 0.75);

const eventTypes = [];
const analyticBoundary = await createRunProtocolBoundary({
  runId: `deepseek-protocol-${Date.now()}`,
  userInput: "比较订单在不同月份的趋势，找出下降最明显的月份",
  authorizedProtocolIds: ["general-task", "data-analysis"],
  initialContextPackageRef: { packageId: "deepseek-context", revision: 0 },
  tools: {},
  classifier,
  projectContext: () => ({ packageId: "deepseek-context", revision: 0 }),
  runtimeOptions: { onEvent: (event) => eventTypes.push(event.type) }
});
assert.equal(analyticBoundary.route.definition.id, "data-analysis");
assert.equal(analyticBoundary.route.source, "classifier");
assert.deepEqual(eventTypes.slice(0, 5), [
  "protocol.route.requested",
  "protocol.route.classified",
  "protocol.route.resolved",
  "protocol.run.started",
  "protocol.phase.entered"
]);

console.log(
  `Real DeepSeek protocol smoke OK: general=${general.confidence.toFixed(2)}, `
  + `analytic=${analyticBoundary.route.definition.id}.`
);

async function runApiScenarios(baseUrl) {
  const stamp = Date.now();
  const generalEvents = await runAgent(baseUrl, {
    threadId: `protocol-general-${stamp}`,
    runId: `protocol-general-run-${stamp}`,
    prompt: "用一句话解释什么是星型模型，不要查询数据源。",
    runConfig: {
      protocol: { id: "general-task", version: "1" },
      activeLlmProfileId: "server-default",
      enabledDatasourceIds: [],
      enabledKnowledgeIds: [],
      enabledMcpServerIds: [],
      enabledSkillIds: []
    }
  });
  assertProtocolTerminal(generalEvents, "protocol.run.completed");
  assert.equal(protocolEvent(generalEvents, "protocol.route.resolved")?.value?.payload?.source, "explicit");

  const dataEvents = await runAgent(baseUrl, {
    threadId: `protocol-data-${stamp}`,
    runId: `protocol-data-run-${stamp}`,
    prompt: "查询 orders 表，计算订单总数和 gmv 总和。必须先检查 schema，再执行只读 SQL，并说明结果。",
    runConfig: {
      protocol: { id: "data-analysis", version: "1" },
      activeDatasourceId: "local-sqlite-orders",
      activeLlmProfileId: "server-default",
      enabledDatasourceIds: ["local-sqlite-orders"],
      enabledKnowledgeIds: [],
      enabledMcpServerIds: [],
      enabledSkillIds: []
    }
  });
  assertProtocolTerminal(dataEvents, "protocol.run.degraded");
  const degradedDecision = protocolEvent(dataEvents, "protocol.run.degraded")?.value?.payload?.decision;
  assert.ok(
    degradedDecision?.reasons?.includes("LOCAL_SEMANTIC_LIMITED_TO_PHYSICAL_SCHEMA"),
    "Data E2E must disclose local semantic fallback when DataLink is disabled"
  );
  const extractedRequirements = protocolEvent(dataEvents, "analysis.requirements.extracted")
    ?.value?.payload?.requirements;
  assert.ok(Array.isArray(extractedRequirements) && extractedRequirements.length > 0);
  const groundingResult = dataEvents.find((event) =>
    event.type === "CUSTOM"
    && event.name === "protocol.action.succeeded"
    && event.value?.payload?.actionName === "analysis.contract.ground"
  )?.value?.payload?.result;
  assert.ok(
    Array.isArray(groundingResult?.structuredRequirementIds)
      && groundingResult.structuredRequirementIds.length === extractedRequirements.length,
    `Every extracted requirement must be schema-grounded: ${JSON.stringify(groundingResult)}`
  );
  assert.deepEqual(groundingResult.manualRequirementIds, []);
  const actionOrder = dataEvents
    .filter((event) => event.type === "CUSTOM" && event.name === "protocol.action.started")
    .map((event) => event.value?.payload?.actionName);
  assert.ok(actionOrder.indexOf("inspect_schema") >= 0);
  assert.ok(actionOrder.indexOf("semantic.context.resolve") > actionOrder.indexOf("inspect_schema"));
  assert.ok(actionOrder.indexOf("data.query.plan") > actionOrder.indexOf("semantic.context.resolve"));
  assert.ok(actionOrder.indexOf("data.query.validate") > actionOrder.indexOf("data.query.plan"));
  assert.ok(actionOrder.indexOf("run_sql_readonly") > actionOrder.indexOf("data.query.validate"));
  assert.ok(actionOrder.includes("analysis.evidence.bind"));
  const committedClaims = toolResults(dataEvents, "analysis_requirements_commit")
    .flatMap((event) => parseClaims(event.content));
  assert.ok(committedClaims.length > 0, "Data E2E must commit requirement claims");
  assert.ok(
    committedClaims.some((claim) => Array.isArray(claim.values) && claim.values.length > 0),
    "Structured claims must carry runtime-verified values"
  );

  const handoffEvents = await runAgent(baseUrl, {
    threadId: `protocol-handoff-${stamp}`,
    runId: `protocol-handoff-run-${stamp}`,
    prompt: "必须实际查询 orders 表的订单数。当前协议不适用时，先用 protocol_handoff 切换到 data-analysis@1。",
    runConfig: {
      protocol: { id: "general-task", version: "1" },
      activeDatasourceId: "local-sqlite-orders",
      activeLlmProfileId: "server-default",
      enabledDatasourceIds: ["local-sqlite-orders"],
      enabledKnowledgeIds: [],
      enabledMcpServerIds: [],
      enabledSkillIds: []
    }
  });
  assert.ok(protocolEvent(handoffEvents, "protocol.handoff.accepted"));
  assertProtocolTerminal(handoffEvents, "protocol.run.degraded");
  console.log(
    `Real Agent protocol E2E OK: general events=${generalEvents.length}, data actions=${actionOrder.length}, `
    + `handoff events=${handoffEvents.length}.`
  );
}


const clientsByBase = new Map();
async function createClientForBase(baseUrl) {
  const client = createAuthenticatedTestClient({ baseUrl });
  await client.registerAndLogin({ displayName: "Deepseek Protocol Smoke" });
  clientsByBase.set(baseUrl, client);
  return client;
}

async function runAgent(baseUrl, input) {
  const client = clientsByBase.get(baseUrl) ?? await createClientForBase(baseUrl);
  const response = await client.fetch("/api/copilotkit", {
    method: "POST",
    headers: {
      Accept: "text/event-stream",
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      method: "agent/run",
      params: { agentId: "dataFoundry" },
      body: {
        threadId: input.threadId,
        runId: input.runId,
        state: {},
        messages: [{ id: `${input.runId}:user`, role: "user", content: input.prompt }],
        tools: [],
        context: [],
        forwardedProps: { run_config: input.runConfig }
      }
    }),
    signal: AbortSignal.timeout(5 * 60 * 1000)
  });
  if (!response.ok) {
    throw new Error(`Agent API failed (${response.status}): ${await response.text()}`);
  }
  return parseEventStream(await response.text());
}

const parseEventStream = (text) => text
  .split("\n\n")
  .map((chunk) => chunk.trim())
  .filter((chunk) => chunk.startsWith("data: "))
  .map((chunk) => chunk.slice("data: ".length))
  .filter((chunk) => chunk !== "[DONE]")
  .map((chunk) => JSON.parse(chunk));

const protocolEvent = (events, name) => events.find((event) => event.type === "CUSTOM" && event.name === name);

const toolResults = (events, toolName) => {
  const names = new Map(events
    .filter((event) => event.type === "TOOL_CALL_START" && typeof event.toolCallId === "string")
    .map((event) => [event.toolCallId, event.toolCallName]));
  return events.filter((event) => event.type === "TOOL_CALL_RESULT"
    && (event.toolCallName ?? names.get(event.toolCallId)) === toolName);
};

const parseClaims = (content) => {
  if (typeof content !== "string") return [];
  try {
    const parsed = JSON.parse(content);
    return Array.isArray(parsed?.claims) ? parsed.claims : [];
  } catch {
    return [];
  }
};

const assertProtocolTerminal = (events, terminalName) => {
  const protocolTerminals = events.filter((event) => event.type === "CUSTOM" && event.name?.startsWith("protocol.run."))
    .map((event) => ({ name: event.name, decision: event.value?.payload?.decision }));
  const runError = events.findLast((event) => event.type === "RUN_ERROR");
  assert.ok(
    protocolEvent(events, terminalName),
    `Missing ${terminalName}; terminals=${JSON.stringify(protocolTerminals)}; runError=${JSON.stringify(runError)}`
  );
  assert.equal(events.findLast((event) => event.type === "RUN_ERROR"), undefined);
  assert.equal(events.findLast((event) => event.type === "RUN_FINISHED")?.type, "RUN_FINISHED");
};

if (process.env.PROTOCOL_E2E_API_URL) {
  await runApiScenarios(process.env.PROTOCOL_E2E_API_URL.replace(/\/$/u, ""));
}
process.exit(0);
