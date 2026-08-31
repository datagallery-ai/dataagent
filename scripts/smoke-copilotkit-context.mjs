import { randomUUID } from "node:crypto";
import { EventType } from "@ag-ui/core";
import { extractDatasourceId, extractEffectiveRunConfig, extractLastUserText } from "../apps/api/dist/run-input.js";
import { createDataFoundry, createDataFoundryRunContext, DATA_AGENT_TOOL_NAMES } from "../packages/agent-runtime/dist/index.js";
import { TaskPlanProjector } from "../apps/api/dist/task-plan-projector.js";

const userId = randomUUID();

const baseInput = {
  threadId: "thread-smoke",
  runId: "run-smoke",
  messages: [],
  context: [],
  forwardedProps: {},
  state: {}
};

assert(
  extractDatasourceId({
    ...baseInput,
    forwardedProps: { datasourceId: "from-forwarded-camel" },
    state: { datasourceId: "from-state" },
    context: [{ description: "datasource_id", value: "from-context" }]
  }) === "from-forwarded-camel",
  "forwardedProps.datasourceId should win"
);
assert(
  extractDatasourceId({
    ...baseInput,
    forwardedProps: { datasource_id: "from-forwarded-snake" }
  }) === "from-forwarded-snake",
  "forwardedProps.datasource_id should be supported"
);
assert(
  extractDatasourceId({
    ...baseInput,
    state: { datasource_id: "from-state-snake" },
    context: [{ description: "datasource_id", value: "from-context" }]
  }) === "from-state-snake",
  "state.datasource_id should win over context"
);
assert(
  extractDatasourceId({
    ...baseInput,
    context: [{ description: "datasource_id", value: "from-context" }]
  }) === "from-context",
  "context datasource_id should be supported"
);
assert(
  extractLastUserText({
    ...baseInput,
    messages: [
      { role: "user", content: "older" },
      { role: "assistant", content: "answer" },
      { role: "user", content: "latest" }
    ]
  }) === "latest",
  "latest user string message should be selected"
);
assert(
  extractLastUserText({
    ...baseInput,
    messages: [
      {
        role: "user",
        content: [
          { type: "text", text: "分析" },
          { type: "text", text: "orders 表" }
        ]
      }
    ]
  }) === "分析\norders 表",
  "multi-part user text should be joined"
);

const effectiveConfig = extractEffectiveRunConfig({
  ...baseInput,
  forwardedProps: {
    run_config: {
      activeDatasourceId: "orders-db",
      enabledDatasourceIds: ["orders-db", "warehouse"],
      activeSkillId: "data-analysis",
      goal: { objective: "Produce a verified orders analysis", maxRuns: 4 }
    }
  }
}, "default-db");
assert(effectiveConfig.activeDatasourceId === "orders-db", "run_config active datasource should be resolved");
assert(effectiveConfig.enabledDatasourceIds.length === 2, "run_config enabled datasources should be preserved");
assert(effectiveConfig.goal?.maxRuns === 4, "trusted goal config should be parsed and bounded");

// R-019: mentioned is parsed, clamped to enabled*Ids, and out-of-scope IDs are excluded.
const mentionedConfig = extractEffectiveRunConfig({
  ...baseInput,
  forwardedProps: {
    run_config: {
      activeDatasourceId: "orders-db",
      enabledDatasourceIds: ["orders-db", "warehouse"],
      enabledKnowledgeIds: ["kb-orders", "kb-faq"],
      enabledMcpServerIds: ["mcp-local"],
      enabledSkillIds: ["data-analysis"],
      mentioned: {
        db: ["orders-db", "ghost-db"],
        kb: ["kb-faq", "kb-ghost"],
        mcp: ["mcp-ghost"],
        skill: ["data-analysis"]
      }
    }
  }
}, "default-db");
assert(mentionedConfig.mentioned, "mentioned should be parsed into EffectiveRunConfig");
assert(JSON.stringify(mentionedConfig.mentioned.db) === JSON.stringify(["orders-db"]), "mentioned.db should clamp to enabled datasources");
assert(JSON.stringify(mentionedConfig.mentioned.kb) === JSON.stringify(["kb-faq"]), "mentioned.kb should clamp to enabled knowledge bases");
assert(JSON.stringify(mentionedConfig.mentioned.mcp) === JSON.stringify([]), "mentioned.mcp should clamp to enabled MCP servers (all out-of-scope dropped)");
assert(JSON.stringify(mentionedConfig.mentioned.skill) === JSON.stringify(["data-analysis"]), "mentioned.skill should clamp to enabled skills");
const excluded = mentionedConfig.mentioned.excluded ?? [];
assert(excluded.length === 3, "three out-of-scope mentioned IDs should be collected as excluded (ghost-db, kb-ghost, mcp-ghost)");

// R-019 backward compat: no mentioned field ⇒ mentioned is undefined (no regression).
const noMentionConfig = extractEffectiveRunConfig({
  ...baseInput,
  forwardedProps: { run_config: { activeDatasourceId: "orders-db", enabledDatasourceIds: ["orders-db"] } }
}, "default-db");
assert(noMentionConfig.mentioned === undefined, "omitted mentioned should yield undefined (backward compat)");

// R-024: pinnedPaths parsed, traversal/absolute/NUL dropped.
const pinnedConfig = extractEffectiveRunConfig({
  ...baseInput,
  forwardedProps: {
    run_config: {
      activeDatasourceId: "orders-db",
      enabledDatasourceIds: ["orders-db"],
      pinnedPaths: ["output/report.html", "/etc/passwd", "../escape", "ok/file.csv", "a\0b"]
    }
  }
}, "default-db");
assert(JSON.stringify(pinnedConfig.pinnedPaths) === JSON.stringify(["output/report.html", "ok/file.csv"]), "pinnedPaths should drop absolute/traversal/NUL entries");

const noDatasourceConfig = extractEffectiveRunConfig({
  ...baseInput,
  forwardedProps: {
    run_config: {
      enabledDatasourceIds: [],
      enabledKnowledgeIds: []
    }
  }
}, "default-db");
assert(noDatasourceConfig.activeDatasourceId === undefined, "empty enabled datasources should omit activeDatasourceId");
assert(noDatasourceConfig.enabledDatasourceIds.length === 0, "empty enabled datasources should stay empty");

const legacyDatasourceButEmptyEnabled = extractEffectiveRunConfig({
  ...baseInput,
  forwardedProps: {
    datasourceId: "default-db",
    run_config: {
      enabledDatasourceIds: []
    }
  }
}, "default-db");
assert(
  legacyDatasourceButEmptyEnabled.activeDatasourceId === undefined,
  "legacy datasourceId should not resurrect an active datasource when enabled list is empty"
);
assert(
  legacyDatasourceButEmptyEnabled.enabledDatasourceIds.length === 0,
  "explicit empty enabledDatasourceIds should win over legacy datasourceId"
);

assert(
  createDataFoundryRunContext({
    user_id: userId,
    session_id: "thread-smoke",
    run_id: "run-smoke",
    user_input: "你好",
    chat_mode: "copilotkit"
  }).enabled_datasource_ids === undefined,
  "no enabled datasources should omit enabled_datasource_ids from run context"
);

const noDatasourceAgent = await createDataFoundry({
  dataGateway: { listDataSources: async () => ({ datasources: [] }) },
  emitter: { emit: () => undefined },
  messages: [],
  modelProvider: { kind: "mastra-router", model: "openai/smoke", model_name: "smoke-model" },
  runContext: createDataFoundryRunContext({
    user_id: userId,
    session_id: "thread-smoke",
    run_id: "run-smoke",
    user_input: "你好",
    chat_mode: "copilotkit"
  })
});
const noDatasourceToolNames = Object.keys(await noDatasourceAgent.agent.listTools());
assert(
  DATA_AGENT_TOOL_NAMES.every((name) => !noDatasourceToolNames.includes(name)),
  "data tools should be omitted when no datasource is enabled"
);

const projector = new TaskPlanProjector({
  user_id: userId,
  session_id: "thread-smoke",
  run_id: "run-smoke",
  user_input: "analyze orders",
  chat_mode: "copilotkit",
  selected_datasource_id: "orders-db",
  enabled_datasource_ids: ["orders-db"],
  model_name: "smoke-model"
});
assert(projector.observe({
  type: EventType.TOOL_CALL_START,
  toolCallId: "task-call-1",
  toolCallName: "task_write"
}).length === 0, "task tool start should not emit a plan");
const projected = projector.observe({
  type: EventType.TOOL_CALL_RESULT,
  messageId: "tool-message-1",
  toolCallId: "task-call-1",
  content: JSON.stringify({
    tasks: [{ id: "inspect", content: "Inspect schema", activeForm: "Inspecting schema", status: "in_progress" }]
  })
});
assert(projected.length === 1, "task result should emit one PLAN snapshot");
assert(projected[0].activityType === "PLAN", "projected activity should use PLAN type");
assert(projected[0].content.tasks[0].status === "running", "in_progress should map to AG-UI running");

console.log("CopilotKit context smoke OK: run config, user text, and task PLAN projection are stable");

function assert(condition, message) {
  if (!condition) {
    throw new Error(message);
  }
}
