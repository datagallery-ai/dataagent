/**
 * Deterministic runtime verification for task + collaboration tools (no LLM).
 */
import { rmSync } from "node:fs";
import { Mastra } from "@mastra/core/mastra";
import {
  askUserTool,
  submitPlanTool,
  taskCheckTool,
  taskCompleteTool,
  taskUpdateTool,
  taskWriteTool,
} from "@mastra/core/harness";
import {
  createDataFoundry,
  createTaskStateRuntime,
} from "../../packages/agent-runtime/dist/index.js";

const stamp = Date.now();
const storageDir = `storage/verify-tools/task-${stamp}`;
const databasePath = `${storageDir}/state.sqlite`;
const threadId = `verify-thread-${stamp}`;
const resourceId = userId;

function makeExecCtx(toolName, mastraCtx) {
  const customChunks = [];
  const execCtx = {
    ...mastraCtx,
    context: { requestContext: new Map() },
    agentName: "verify",
    name: toolName,
    writer: {
      custom: async (c) => customChunks.push(c),
      write: async (c) => customChunks.push({ write: c }),
    },
  };
  return { execCtx, customChunks };
}

function dataChunkTypes(customChunks) {
  return [
    ...new Set(
      customChunks
        .map((c) => (c && typeof c === "object" && "type" in c ? c.type : null))
        .filter((t) => typeof t === "string" && t.startsWith("data-")),
    ),
  ];
}

function truncateJson(value, max = 200) {
  const text = JSON.stringify(value);
  return text.length <= max ? text : `${text.slice(0, max - 3)}...`;
}

const results = [];
let exitCode = 0;

function record(row) {
  results.push(row);
  if (row.directExecOk === false && row.tool.startsWith("task_")) {
    exitCode = 1;
  }
}

try {
  const runtime = await createTaskStateRuntime(databasePath);
  const mastra = new Mastra({ storage: runtime.storage });
  const mastraCtx = { agent: { threadId, resourceId }, mastra };

  // --- task_write ---
  {
    const { execCtx, customChunks } = makeExecCtx("task_write", mastraCtx);
    const result = await taskWriteTool.execute(
      {
        tasks: [
          {
            id: "step1",
            content: "Inspect schema",
            activeForm: "Inspecting schema",
            status: "in_progress",
          },
          {
            id: "step2",
            content: "Run query",
            activeForm: "Running query",
            status: "pending",
          },
          {
            id: "step3",
            content: "Summarize results",
            activeForm: "Summarizing results",
            status: "pending",
          },
        ],
      },
      execCtx,
    );
    record({
      tool: "task_write",
      registered: true,
      directExecOk: result?.isError === false && result?.tasks?.length === 3,
      dataChunks: dataChunkTypes(customChunks),
      notes: truncateJson({ isError: result?.isError, taskCount: result?.tasks?.length }),
    });
  }

  // --- task_update ---
  {
    const { execCtx, customChunks } = makeExecCtx("task_update", mastraCtx);
    const result = await taskUpdateTool.execute(
      { id: "step2", status: "in_progress", activeForm: "Running query" },
      execCtx,
    );
    const step2 = result?.tasks?.find((t) => t.id === "step2");
    record({
      tool: "task_update",
      registered: true,
      directExecOk: result?.isError === false && step2?.status === "in_progress",
      dataChunks: dataChunkTypes(customChunks),
      notes: truncateJson({ step2Status: step2?.status }),
    });
  }

  // --- task_complete ---
  {
    const { execCtx, customChunks } = makeExecCtx("task_complete", mastraCtx);
    const result = await taskCompleteTool.execute({ id: "step1" }, execCtx);
    const step1 = result?.tasks?.find((t) => t.id === "step1");
    record({
      tool: "task_complete",
      registered: true,
      directExecOk: result?.isError === false && step1?.status === "completed",
      dataChunks: dataChunkTypes(customChunks),
      notes: truncateJson({ step1Status: step1?.status }),
    });
  }

  // --- task_check ---
  {
    const { execCtx, customChunks } = makeExecCtx("task_check", mastraCtx);
    const result = await taskCheckTool.execute({}, execCtx);
    const incomplete = result?.incompleteTasks?.length ?? result?.summary?.incomplete;
    record({
      tool: "task_check",
      registered: true,
      directExecOk:
        result?.isError === false &&
        result?.tasks?.length === 3 &&
        result?.summary?.completed === 1 &&
        incomplete === 2,
      dataChunks: dataChunkTypes(customChunks),
      notes: truncateJson({
        summary: result?.summary,
        incompleteTasks: result?.incompleteTasks?.map((t) => t.id),
      }),
    });
  }

  // --- ask_user (suspend contract) ---
  {
    let suspendPayload;
    let suspendCalled = false;
    const { execCtx, customChunks } = makeExecCtx("ask_user", {
      agent: {
        suspend: async (payload) => {
          suspendCalled = true;
          suspendPayload = payload;
        },
      },
    });
    const suspended = await askUserTool.execute(
      { question: "Which datasource?", options: [{ label: "orders" }] },
      execCtx,
    );
    const resumeResult = await askUserTool.execute(
      { question: "Which datasource?", options: [{ label: "orders" }] },
      { agent: { resumeData: "orders" } },
    );
    record({
      tool: "ask_user",
      registered: true,
      directExecOk: "suspends - N/A",
      dataChunks: dataChunkTypes(customChunks),
      notes: truncateJson({
        hasInputSchema: Boolean(askUserTool.inputSchema),
        hasSuspendSchema: Boolean(askUserTool.suspendSchema),
        suspendReturnsUndefined: suspended === undefined,
        suspendCalled,
        suspendPayload,
        resumeContent: resumeResult?.content,
      }),
    });
  }

  // --- submit_plan (suspend contract) ---
  {
    let suspendPayload;
    let suspendCalled = false;
    const { execCtx, customChunks } = makeExecCtx("submit_plan", {
      agent: {
        suspend: async (payload) => {
          suspendCalled = true;
          suspendPayload = payload;
        },
      },
    });
    const suspended = await submitPlanTool.execute(
      { title: "Plan", plan: "1. Inspect\n2. Query" },
      execCtx,
    );
    const approved = await submitPlanTool.execute(
      { title: "Plan", plan: "1. Inspect\n2. Query" },
      { agent: { resumeData: { action: "approved" } } },
    );
    record({
      tool: "submit_plan",
      registered: true,
      directExecOk: "suspends - N/A",
      dataChunks: dataChunkTypes(customChunks),
      notes: truncateJson({
        hasInputSchema: Boolean(submitPlanTool.inputSchema),
        hasSuspendSchema: Boolean(submitPlanTool.suspendSchema),
        suspendReturnsUndefined: suspended === undefined,
        suspendCalled,
        suspendPayloadKeys: suspendPayload ? Object.keys(suspendPayload) : [],
        approvedContent: approved?.content,
      }),
    });
  }

  // --- createDataFoundry registration ---
  {
    const configured = await createDataFoundry({
      dataGateway: {},
      emitter: { emit: () => undefined },
      messages: [],
      modelProvider: { kind: "openai-compatible", model: "openai/smoke", model_name: "smoke" },
      runContext: {
        user_id: resourceId,
        session_id: threadId,
        run_id: `verify-run-${stamp}`,
        user_input: "verify",
        chat_mode: "copilotkit",
        selected_datasource_id: "smoke-source",
        enabled_datasource_ids: ["smoke-source"],
        model_name: "smoke",
      },
      taskStateRuntime: runtime,
      workspaceRoot: `${storageDir}/workspaces`,
    });
    const toolNames = Object.keys(await configured.agent.listTools());
    const expected = [
      "task_write",
      "task_update",
      "task_complete",
      "task_check",
      "ask_user",
      "submit_plan",
    ];
    for (const name of expected) {
      const existing = results.find((r) => r.tool === name);
      if (existing) {
        existing.registered = toolNames.includes(name);
        if (!existing.registered) {
          exitCode = 1;
          existing.notes += ` | NOT in agent.listTools()`;
        }
      }
    }
    await configured.destroyWorkspace();
  }

  await runtime.close();
  console.log(JSON.stringify({ exitCode, storageDir, results }, null, 2));
  process.exitCode = exitCode;
} finally {
  rmSync(storageDir, { force: true, recursive: true });
}
