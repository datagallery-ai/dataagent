import { askUserTool, submitPlanTool } from "@mastra/core/harness";
import {
  GovernedToolFactory,
  ToolObservationDispatcher,
  createCustomEvent,
  createToolObservationBoundary,
  createDataFoundryRunContext
} from "../packages/agent-runtime/dist/testing.js";
import {
  extractInteractionResume,
  InteractionRuntimeAdapter
} from "../apps/api/dist/interaction-runtime-adapter.js";
import { createMetadataStore } from "../packages/metadata/dist/index.js";
import { rmSync } from "node:fs";
import { createVerifiedTestIdentity } from "./lib/metadata-test-identity.mjs";

const storageDir = `storage/collaboration-smoke/${Date.now()}`;
const store = createMetadataStore({ database_path: `${storageDir}/metadata.sqlite` });
const __testIdentity = createVerifiedTestIdentity(store);
const userId = __testIdentity.userId;
const workspaceId = __testIdentity.workspaceId;


const sessionId = "collaboration-session";
const runId = "collaboration-run";

try {
  store.sessions.create({ user_id: userId, id: sessionId, title: "collaboration smoke" });
  store.runs.create({
    user_id: userId,
    id: runId,
    session_id: sessionId,
    request_fingerprint: "initial-request",
    user_input: "ask me",
    status: "running"
  });
  const runContext = createDataFoundryRunContext({
    user_id: userId,
    session_id: sessionId,
    run_id: runId,
    user_input: "ask me",
    chat_mode: "copilotkit",
    selected_datasource_id: "unused",
    enabled_datasource_ids: ["unused"]
  });
  const runScope = {
    modelName: runContext.model_name,
    resourceId: runContext.user_id,
    runId: runContext.run_id,
    sessionId: runContext.session_id
  };
  const { packager } = createToolObservationBoundary({
    identity: {
      resourceId: runContext.user_id,
      runId: runContext.run_id,
      sessionId: runContext.session_id
    }
  });
  const factory = new GovernedToolFactory(new ToolObservationDispatcher(packager, runScope));
  const askUser = factory.governTool("ask_user", askUserTool);
  const submitPlan = factory.governTool("submit_plan", submitPlanTool);

  let askSuspendPayload;
  const suspended = await askUser.execute(
    { question: "Which datasource?", options: [{ label: "orders" }] },
    { agent: { suspend: async (payload) => { askSuspendPayload = payload; } } }
  );
  assert(suspended === undefined, "ask_user should preserve Mastra suspension");
  assert(askSuspendPayload.question === "Which datasource?", "ask_user suspend payload should be preserved");

  const interrupt = {
    type: "mastra_suspend",
    toolCallId: "call-ask-1",
    toolName: "ask_user",
    suspendPayload: askSuspendPayload,
    args: { question: "Which datasource?" },
    resumeSchema: { type: "string" },
    runId
  };
  const runtime = new InteractionRuntimeAdapter(store, userId, sessionId, runId);
  const requested = runtime.capture(createCustomEvent("on_interrupt", JSON.stringify(interrupt)));
  assert(requested?.event.name === "interaction.requested", "interrupt should project to interaction.requested");
  const stored = store.interactions.getByToolCall({
    user_id: userId,
    run_id: runId,
    tool_call_id: interrupt.toolCallId
  });
  assert(
    stored.interrupt_event_json?.includes("mastra_suspend"),
    "interrupt_event_json should be persisted for restored resume"
  );
  const pending = store.interactions.listPendingBySession({
    user_id: userId,
    session_id: sessionId
  });
  assert(pending.length === 1, "pending interaction should be listed by session");

  const resume = extractInteractionResume({
    threadId: sessionId,
    runId,
    messages: [],
    tools: [],
    context: [],
    state: {},
    forwardedProps: { command: { resume: "orders", interruptEvent: JSON.stringify(interrupt) } }
  });
  assert(Boolean(resume), "resume command should parse");
  const answer = await askUser.execute(
    { question: "Which datasource?", options: [{ label: "orders" }] },
    { agent: { resumeData: "orders" } }
  );
  assert(answer.content.includes("orders"), "governed ask_user result should reach the model");
  const resolved = runtime.resolve(resume);
  assert(resolved.name === "interaction.resolved", "resume should project to interaction.resolved");
  runtime.resolve(resume);

  const changedResume = extractInteractionResume({
    threadId: sessionId,
    runId,
    messages: [],
    tools: [],
    context: [],
    state: {},
    forwardedProps: { command: { resume: "customers", interruptEvent: JSON.stringify(interrupt) } }
  });
  await assertRejects(() => runtime.resolve(changedResume), "INTERACTION_RESUME_MISMATCH");

  let planSuspendPayload;
  await submitPlan.execute(
    { title: "Plan", plan: "1. Inspect\n2. Query" },
    { agent: { suspend: async (payload) => { planSuspendPayload = payload; } } }
  );
  const planInterrupt = {
    type: "mastra_suspend",
    toolCallId: "call-plan-1",
    toolName: "submit_plan",
    suspendPayload: planSuspendPayload,
    args: { title: "Plan" },
    resumeSchema: { type: "object" },
    runId
  };
  runtime.capture(createCustomEvent("on_interrupt", JSON.stringify(planInterrupt)));
  const cancellation = extractInteractionResume({
    threadId: sessionId,
    runId,
    messages: [],
    tools: [],
    context: [],
    state: {},
    forwardedProps: { command: { resume: false, interruptEvent: JSON.stringify(planInterrupt) } }
  });
  assert(runtime.cancel(cancellation).value.status === "canceled", "resume=false should cancel the interaction");

  const planResult = await submitPlan.execute(
    { title: "Plan", plan: "1. Inspect\n2. Query" },
    { agent: { resumeData: { action: "approved" } } }
  );
  assert(planResult.content.includes("approved"), "submit_plan approval should reach the model");
  console.log("Collaboration tools smoke OK: suspend, resume idempotency, cancel, adapters, and plan approval");
} finally {
  store.close();
  rmSync(storageDir, { force: true, recursive: true });
}

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}

async function assertRejects(thunk, expectedMessage) {
  try {
    await thunk();
  } catch (error) {
    if (error instanceof Error && error.message.includes(expectedMessage)) {
      return;
    }
    throw error;
  }
  throw new Error(`Expected rejection with ${expectedMessage}`);
}
