/**
 * Contract tests for Mastra → AG-UI stream normalization (no LLM).
 */
import {
  normalizeMastraFullStream,
  wrapAgentForAgUi
} from "../packages/agent-runtime/dist/stream/mastra-stream-normalizer.js";

async function collectNormalized(chunks, hooks = {}) {
  const output = [];
  for await (const chunk of normalizeMastraFullStream(chunks, hooks)) {
    output.push(chunk);
  }
  return output;
}

async function* inputChunks() {
  yield { type: "text-start", runId: "r1", from: "agent" };
  yield { type: "tool-call-input-streaming-start", runId: "r1", from: "agent" };
  yield { type: "tool-call-delta", runId: "r1", from: "agent" };
  yield { type: "tool-call-input-streaming-end", runId: "r1", from: "agent" };
  yield { type: "text-delta", payload: { text: "hello" }, runId: "r1", from: "agent" };
  yield {
    type: "finish-step",
    payload: { usage: { inputTokens: 12, outputTokens: 4, totalTokens: 16 } },
    runId: "r1",
    from: "agent"
  };
  yield { type: "data-workspace-metadata", data: { toolName: "write_file", toolCallId: "tc-1" } };
  yield {
    type: "tool-error",
    payload: { toolCallId: "tc-1", toolName: "write_file", error: "File has not been read" },
    runId: "r1",
    from: "agent"
  };
  yield { type: "finish", payload: {}, runId: "r1", from: "agent" };
}

const dataChunks = [];
const quarantined = [];
const usageChunks = [];
const normalized = await collectNormalized(inputChunks(), {
  onChunk: (chunk) => {
    if (chunk.payload?.usage) usageChunks.push(chunk);
  },
  onDataChunk: (chunk) => dataChunks.push(chunk),
  onQuarantine: (chunk) => quarantined.push(chunk)
});

assert(normalized.length === 3, `expected 3 normalized chunks, got ${normalized.length}`);
assert(
  normalized.every((chunk) => chunk.payload !== undefined),
  "normalized stream must not contain payload-less chunks"
);
assert(
  normalized.some((chunk) => chunk.type === "tool-result"),
  "tool-error must be rewritten to tool-result"
);
const toolResult = normalized.find((chunk) => chunk.type === "tool-result");
assert(
  toolResult?.payload?.result?.isError === true,
  "rewritten tool-result must carry isError=true"
);
assert(dataChunks.length === 1, "data-workspace-metadata must be routed out-of-band");
assert(dataChunks[0].type === "data-workspace-metadata", "data chunk type preserved for hooks");
assert(usageChunks.length === 1, "normalizer hooks should observe provider usage chunks");
assert(quarantined.length === 0, "known chunks must not be quarantined");

const wrappedChunks = [];
const stubAgent = {
  getMemory: async () => undefined,
  stream: async () => ({
    fullStream: inputChunks()
  })
};
const wrapped = wrapAgentForAgUi(stubAgent, {
  onDataChunk: (chunk) => wrappedChunks.push(chunk)
});
const response = await wrapped.stream([], {});
for await (const _chunk of response.fullStream) {
  // drain
}
assert(wrappedChunks.length === 1, "wrapAgentForAgUi must normalize fullStream via hooks");

// Regression: @mastra/core reuses ONE messageId across every step-start of a
// multi-step loop. The normalizer must force distinct ids on reused step-starts
// so @ag-ui/mastra opens a new assistant message per iteration (otherwise the UI
// collapses all reasoning + tool calls into a single "step").
async function* reusedStepStarts() {
  yield { type: "start", payload: { messageId: "m" }, runId: "r", from: "agent" };
  yield { type: "step-start", payload: { messageId: "m", request: {} }, runId: "r", from: "agent" };
  yield { type: "tool-call", payload: { toolCallId: "t0", toolName: "a", args: {} }, runId: "r", from: "agent" };
  yield { type: "step-finish", payload: { messageId: "m", output: {} }, runId: "r", from: "agent" };
  yield { type: "step-start", payload: { messageId: "m", request: {} }, runId: "r", from: "agent" };
  yield { type: "tool-call", payload: { toolCallId: "t1", toolName: "b", args: {} }, runId: "r", from: "agent" };
  yield { type: "step-finish", payload: { messageId: "m", output: {} }, runId: "r", from: "agent" };
  yield { type: "step-start", payload: { messageId: "m", request: {} }, runId: "r", from: "agent" };
  yield { type: "finish", payload: { messageId: "m" }, runId: "r", from: "agent" };
}
const segmented = await collectNormalized(reusedStepStarts());
const stepStartIds = segmented
  .filter((chunk) => chunk.type === "step-start")
  .map((chunk) => chunk.payload?.messageId);
assert(stepStartIds.length === 3, `expected 3 step-start chunks, got ${stepStartIds.length}`);
assert(
  new Set(stepStartIds).size === 3,
  `reused step-start messageIds must be made distinct, got ${JSON.stringify(stepStartIds)}`
);
assert(stepStartIds[0] === "m", "first step-start must keep the original messageId");
assert(
  stepStartIds[1] !== "m" && stepStartIds[2] !== "m" && stepStartIds[1] !== stepStartIds[2],
  `subsequent reused step-starts must get fresh distinct ids, got ${JSON.stringify(stepStartIds)}`
);

// Runs that already emit distinct step-start ids must be left untouched.
async function* distinctStepStarts() {
  yield { type: "step-start", payload: { messageId: "a", request: {} }, runId: "r", from: "agent" };
  yield { type: "step-start", payload: { messageId: "b", request: {} }, runId: "r", from: "agent" };
}
const untouched = (await collectNormalized(distinctStepStarts()))
  .filter((chunk) => chunk.type === "step-start")
  .map((chunk) => chunk.payload?.messageId);
assert(
  JSON.stringify(untouched) === JSON.stringify(["a", "b"]),
  `distinct step-start ids must be preserved, got ${JSON.stringify(untouched)}`
);

console.log("Mastra stream normalizer contract tests OK");
process.exit(0);

function assert(condition, message) {
  if (!condition) {
    console.error(`ASSERT FAILED: ${message}`);
    process.exit(1);
  }
}
