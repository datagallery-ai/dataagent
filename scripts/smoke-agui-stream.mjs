/**
 * Integration smoke: synthetic Mastra fullStream with data-* + tool-error through @ag-ui/mastra.
 * Verifies wrapAgentForAgUi prevents run abort and delivers TOOL_CALL_RESULT.
 */
import { MastraAgent } from "@ag-ui/mastra";
import { EventType } from "@ag-ui/client";
import {
  createMastraStreamNormalizerHooks,
  wrapAgentForAgUi
} from "../packages/agent-runtime/dist/index.js";

async function* problematicFullStream() {
  yield { type: "text-delta", payload: { text: "workspace tool check" }, runId: "run-smoke", from: "agent" };
  yield {
    type: "tool-call",
    payload: {
      toolCallId: "tc-write-1",
      toolName: "write_file",
      args: { path: "smoke.txt", content: "hello" }
    },
    runId: "run-smoke",
    from: "agent"
  };
  yield { type: "data-workspace-metadata", data: { toolName: "write_file", toolCallId: "tc-write-1" } };
  yield {
    type: "finish-step",
    payload: { usage: { inputTokens: 20, outputTokens: 5, totalTokens: 25 } },
    runId: "run-smoke",
    from: "agent"
  };
  yield {
    type: "step-finish",
    payload: {
      output: {
        usage: {
          inputTokens: { total: 120 },
          outputTokens: { total: 34 },
          totalTokens: 154,
        },
        toolCalls: [{ toolCallId: "tc-write-1", toolName: "write_file" }],
      },
      model: { modelId: "qwen-plus" },
    },
    runId: "run-smoke",
    from: "agent",
  };
  yield {
    type: "tool-error",
    payload: {
      toolCallId: "tc-write-1",
      toolName: "write_file",
      error: "File \"smoke.txt\" has not been read."
    },
    runId: "run-smoke",
    from: "agent"
  };
  yield {
    type: "finish",
    payload: {
      totalUsage: {
        inputTokens: 100000,
        outputTokens: 2000,
        totalTokens: 102000
      }
    },
    runId: "run-smoke",
    from: "agent"
  };
}

const customEvents = [];
const emitter = {
  emit: (event) => customEvents.push(event)
};

const stubAgent = {
  getMemory: async () => undefined,
  stream: async () => ({
    fullStream: problematicFullStream()
  })
};

const agent = wrapAgentForAgUi(stubAgent, createMastraStreamNormalizerHooks(emitter));
const mastraAgent = new MastraAgent({ agent, resourceId: "smoke-user" });

const events = [];
await new Promise((resolve, reject) => {
  mastraAgent
    .run({
      threadId: "thread-smoke",
      runId: "run-smoke",
      messages: [{ id: "msg-1", role: "user", content: "write a file" }],
      tools: [],
      context: [],
      state: {}
    })
    .subscribe({
      next: (event) => events.push(event),
      error: (error) => reject(error),
      complete: () => resolve(undefined)
    });
});

const runFinished = events.some((event) => event.type === EventType.RUN_FINISHED);
const runError = events.some((event) => event.type === EventType.RUN_ERROR);
const toolResults = events.filter((event) => event.type === EventType.TOOL_CALL_RESULT);
const workspaceCustom = customEvents.filter((event) => event.name === "workspace.metadata");
const tokenUsageCustom = customEvents.filter((event) => event.name === "token_usage");

assert(runFinished, "run must reach RUN_FINISHED");
assert(!runError, "run must not emit RUN_ERROR for data-workspace-metadata chunks");
assert(toolResults.length >= 1, "tool-error rewrite must produce TOOL_CALL_RESULT");
assert(
  toolResults.some((event) => String(event.content ?? "").includes("isError") || String(event.content ?? "").includes("error")),
  "TOOL_CALL_RESULT must carry tool failure payload"
);
assert(workspaceCustom.length >= 1, "data-workspace-metadata must map to workspace.metadata CUSTOM event");
assert(tokenUsageCustom.length >= 2, "provider usage chunks must map to token_usage CUSTOM event");
assert(tokenUsageCustom[0].value?.input_tokens === 20, "token_usage must expose input_tokens");
assert(tokenUsageCustom.some((event) => event.value?.input_tokens === 120), "step-finish output.usage must map to token_usage");
assert(
  !tokenUsageCustom.some((event) => event.value?.input_tokens === 100000),
  "aggregate totalUsage must not be emitted as an incremental token_usage event"
);

console.log(
  `AG-UI stream smoke OK: events=${events.length}, toolResults=${toolResults.length}, `
    + `workspaceCustom=${workspaceCustom.length}, tokenUsageCustom=${tokenUsageCustom.length}`
);
process.exit(0);

function assert(condition, message) {
  if (!condition) {
    console.error(`ASSERT FAILED: ${message}`);
    console.error("Observed event types:", [...new Set(events.map((event) => event.type))]);
    console.error("CUSTOM names:", customEvents.map((event) => event.name));
    process.exit(1);
  }
}
