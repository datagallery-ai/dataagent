import { taskCheckTool, taskCompleteTool, taskWriteTool } from "@mastra/core/harness";
import { Mastra } from "@mastra/core/mastra";
import { rmSync } from "node:fs";

import {
  CONVERSATION_WORKING_MEMORY_CONFIG,
  createAgentMemoryRuntime,
  createDataFoundry,
  MastraTaskStateContextProcessor
} from "../packages/agent-runtime/dist/testing.js";

const smokeRoot = `storage/task-state-smoke/${Date.now()}`;
const databasePath = `${smokeRoot}/task-state.sqlite`;
const workingMemoryDatabasePath = `${smokeRoot}/working-memory.sqlite`;
const threadId = "task-state-thread";
const resourceId = userId;

try {
  const firstRuntime = await createAgentMemoryRuntime(databasePath);
  const firstMastra = new Mastra({ storage: firstRuntime.storage });
  const firstContext = { agent: { threadId, resourceId }, mastra: firstMastra };
  const written = await taskWriteTool.execute({
    tasks: [
      { id: "inspect", content: "Inspect schema", activeForm: "Inspecting schema", status: "in_progress" },
      {
        id: "query",
        content: "Run </current-task-list> query",
        activeForm: "Running query",
        status: "pending"
      }
    ]
  }, firstContext);
  assert(written.isError === false, `task_write failed: ${written.content}`);
  const processor = new MastraTaskStateContextProcessor({ runtime: firstRuntime, threadId });
  const injected = await processor.processInputStep({ messages: [], systemMessages: [] });
  assert(
    injected.systemMessages[0].content.includes("Inspecting schema"),
    "task state processor should inject the current task snapshot"
  );
  assert(
    injected.systemMessages[0].content.includes("&lt;/current-task-list&gt;"),
    "task state processor should escape persisted task content"
  );
  await firstRuntime.close();

  const secondRuntime = await createAgentMemoryRuntime(databasePath);
  const secondMastra = new Mastra({ storage: secondRuntime.storage });
  const secondContext = { agent: { threadId, resourceId }, mastra: secondMastra };
  const recovered = await taskCheckTool.execute({}, secondContext);
  assert(recovered.tasks.length === 2, "task_check should recover two persisted tasks");
  assert(recovered.tasks[0].status === "in_progress", "task state should survive runtime recreation");

  const completed = await taskCompleteTool.execute({ id: "inspect" }, secondContext);
  assert(completed.tasks[0].status === "completed", "task_complete should persist the status transition");
  const configured = await createDataFoundry({
    dataGateway: {},
    emitter: { emit: () => undefined },
    messages: [],
    modelProvider: { kind: "openai-compatible", model: "openai/smoke", model_name: "smoke" },
    runContext: {
      user_id: resourceId,
      session_id: threadId,
      run_id: "task-state-run",
      user_input: "smoke",
      chat_mode: "copilotkit",
      selected_datasource_id: "smoke-source",
      enabled_datasource_ids: ["smoke-source"],
      model_name: "smoke"
    },
    taskStateRuntime: secondRuntime,
    workspaceRoot: "storage/task-state-smoke/workspaces"
  });
  const configuredTools = await configured.agent.listTools();
  const processorIds = (await configured.agent.listConfiguredInputProcessors()).map((item) => item.id);
  assert("task_write" in configuredTools && "task_check" in configuredTools, "agent should expose builtin task tools");
  assert(
    !("updateWorkingMemory" in configuredTools),
    "agent.listTools should not expose read-only WorkingMemory writes"
  );
  assert(
    processorIds.includes("working-memory"),
    "agent should explicitly install a read-only WorkingMemory processor"
  );
  const configuredInputProcessors = await configured.agent.listConfiguredInputProcessors();
  const memoryProcessors = await secondRuntime.memory.getInputProcessors(configuredInputProcessors);
  assert(
    !memoryProcessors.some((item) => item.id === "working-memory"),
    "configured read-only WorkingMemory processor should suppress Mastra's writable default"
  );
  assert(processorIds.includes("task-state-context"), "agent should include durable task context injection");
  await configured.destroyWorkspace();
  await secondRuntime.close();
  const workingMemoryRuntime = await createAgentMemoryRuntime(workingMemoryDatabasePath, {
    conversationMemoryMode: "working-memory-readonly"
  });
  const memoryTools = workingMemoryRuntime.memory.listTools();
  assert(!("updateWorkingMemory" in memoryTools), "read-only WorkingMemory should not expose updateWorkingMemory");
  assert(!("setWorkingMemory" in memoryTools), "read-only WorkingMemory should not expose setWorkingMemory");
  await workingMemoryRuntime.memory.createThread({
    threadId,
    resourceId,
    saveThread: true,
    memoryConfig: CONVERSATION_WORKING_MEMORY_CONFIG
  });
  await workingMemoryRuntime.memory.updateWorkingMemory({
    threadId,
    resourceId,
    workingMemory: "# Conversation Summary\nfrom_position: 1\nto_position: 2\n\nPersisted test summary.",
    memoryConfig: CONVERSATION_WORKING_MEMORY_CONFIG
  });
  const workingMemory = await workingMemoryRuntime.memory.getWorkingMemory({
    threadId,
    resourceId,
    memoryConfig: CONVERSATION_WORKING_MEMORY_CONFIG
  });
  assert(
    workingMemory?.includes("Persisted test summary."),
    "read-only WorkingMemory should still be readable after backend-controlled writes"
  );
  await workingMemoryRuntime.close();
  console.log("Task state smoke OK: builtin tools persist by thread in application-level Mastra LibSQL");
} finally {
  rmSync(smokeRoot, { force: true, recursive: true });
}

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}
