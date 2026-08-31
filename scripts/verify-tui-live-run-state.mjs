#!/usr/bin/env node
import assert from "node:assert/strict";

const {
  createInitialLiveRun,
  reduceLiveRunEvent,
} = await import("../apps/tui/dist/state/live-run-state.js");
const { store } = await import("../apps/tui/dist/state/store.js");
const { buildChatLines } = await import("../apps/tui/dist/ui/transcript-lines.js");

function collectRenderedToolCalls(lines) {
  const toolCalls = [];
  for (const line of lines) {
    visitReactNode(line.node, toolCalls);
  }
  return toolCalls;
}

function visitReactNode(node, toolCalls) {
  if (node === null || node === undefined || typeof node === "boolean") {
    return;
  }
  if (Array.isArray(node)) {
    for (const child of node) visitReactNode(child, toolCalls);
    return;
  }
  if (typeof node !== "object") {
    return;
  }

  const props = node.props;
  if (!props || typeof props !== "object") {
    return;
  }
  if (
    props.toolCall &&
    typeof props.toolCall === "object" &&
    typeof props.toolCall.id === "string"
  ) {
    toolCalls.push(props.toolCall);
  }
  visitReactNode(props.children, toolCalls);
}

function startSqlRun() {
  let run = createInitialLiveRun();
  run = reduceLiveRunEvent(run, { type: "RUN_STARTED" });
  run = reduceLiveRunEvent(run, {
    type: "TOOL_CALL_START",
    toolCallId: "tool-sql-1",
    toolCallName: "run_sql_readonly",
    args: { sql: "SELECT * FROM orders LIMIT 10" },
  });
  run = reduceLiveRunEvent(run, {
    type: "TOOL_CALL_END",
    toolCallId: "tool-sql-1",
    toolCallName: "run_sql_readonly",
  });
  return run;
}

{
  let run = startSqlRun();
  run = reduceLiveRunEvent(run, {
    type: "TOOL_CALL_RESULT",
    toolCallId: "tool-sql-1",
    content: JSON.stringify({ row_count: 1, elapsed_ms: 6 }),
  });

  assert.equal(run.toolCalls.length, 1);
  assert.equal(run.toolCalls[0].id, "tool-sql-1");
  assert.equal(run.toolCalls[0].name, "run_sql_readonly");
  assert.equal(run.toolCalls[0].status, "success");
  assert.equal(run.events[0].kind, "query");
  assert.equal(run.events[0].toolName, "run_sql_readonly");
}

{
  let run = startSqlRun();
  run = reduceLiveRunEvent(run, {
    type: "TOOL_CALL_RESULT",
    content: JSON.stringify({ error: "SQL execution failed" }),
  });

  assert.equal(run.toolCalls.length, 1);
  assert.equal(run.toolCalls[0].id, "tool-sql-1");
  assert.equal(run.toolCalls[0].name, "run_sql_readonly");
  assert.equal(run.toolCalls[0].status, "failed");
}

{
  let run = createInitialLiveRun();
  run = reduceLiveRunEvent(run, { type: "RUN_STARTED", runId: "late-start-run" });
  run = reduceLiveRunEvent(run, {
    type: "TOOL_CALL_START",
    toolCallId: "tool-late-start",
    toolCallName: "inspect_schema",
  });
  run = reduceLiveRunEvent(run, {
    type: "TOOL_CALL_RESULT",
    toolCallId: "tool-late-start",
    toolCallName: "inspect_schema",
    content: JSON.stringify({ tables: [] }),
  });

  const finishedAtMs = run.toolCalls[0].finishedAtMs;
  run = reduceLiveRunEvent(run, {
    type: "TOOL_CALL_START",
    toolCallId: "tool-late-start",
    toolCallName: "inspect_schema",
  });

  assert.equal(run.toolCalls.length, 1);
  assert.equal(run.toolCalls[0].status, "success");
  assert.equal(run.toolCalls[0].runId, "late-start-run");
  assert.equal(run.toolCalls[0].finishedAtMs, finishedAtMs);
}

{
  let run = createInitialLiveRun();
  run = reduceLiveRunEvent(run, { type: "RUN_STARTED", runId: "state-delta-run" });
  run = reduceLiveRunEvent(run, {
    type: "TOOL_CALL_START",
    toolCallId: "tool-state-delta",
    toolCallName: "run_sql_readonly",
  });
  run = reduceLiveRunEvent(run, {
    type: "STATE_DELTA",
    delta: [{ path: "/runStatus", value: "completed" }],
  });

  assert.equal(run.runStatus, "completed");
  assert.equal(run.agentResponseComplete, true);
  assert.equal(run.toolCalls[0].status, "success");
  assert(run.toolCalls[0].finishedAtMs > 0);
}

{
  let run = createInitialLiveRun();
  run = reduceLiveRunEvent(run, { type: "RUN_STARTED", runId: "run-current" });
  assert.equal(run.runId, "run-current");

  store.reset();
  store.handleLiveRunEvent({ type: "RUN_STARTED", runId: "run-current" });
  assert.equal(store.getState().agentResponseComplete, false);
  store.handleLiveRunEvent({
    type: "CUSTOM",
    name: "run.response.completed",
    value: { runId: "run-stale" },
  });
  assert.equal(store.getState().agentResponseComplete, false);
  store.handleLiveRunEvent({
    type: "CUSTOM",
    name: "run.response.completed",
    value: { runId: "run-current" },
  });
  assert.equal(store.getState().agentResponseComplete, true);
  assert.equal(store.getState().runStatus, "running");
  store.handleLiveRunEvent({ type: "RUN_FINISHED", runId: "run-stale" });
  assert.equal(store.getState().runStatus, "running");
  store.handleLiveRunEvent({ type: "RUN_FINISHED", runId: "run-current" });
  assert.equal(store.getState().runStatus, "completed");
}

{
  store.reset();
  store.handleLiveRunEvent({ type: "RUN_STARTED", runId: "client-run" });
  assert.equal(store.getState().runId, "client-run");

  store.handleLiveRunEvent({
    type: "STATE_SNAPSHOT",
    snapshot: { runId: "server-run", runStatus: "running" },
    _clientRunId: "client-run",
  });
  assert.equal(store.getState().runId, "server-run");

  store.handleLiveRunEvent({
    type: "CUSTOM",
    name: "run.response.completed",
    value: { runId: "server-run" },
  });
  assert.equal(store.getState().agentResponseComplete, true);

  store.handleLiveRunEvent({ type: "RUN_FINISHED", runId: "server-run" });
  assert.equal(store.getState().runStatus, "completed");
}

{
  store.reset();
  store.handleLiveRunEvent({ type: "RUN_STARTED", runId: "client-run-no-snapshot" });
  store.handleLiveRunEvent({
    type: "CUSTOM",
    name: "run.response.completed",
    value: { runId: "server-run-no-snapshot" },
    _clientRunId: "client-run-no-snapshot",
  });
  assert.equal(store.getState().agentResponseComplete, true);
  assert.equal(store.getState().runStatus, "running");
  store.handleLiveRunEvent({
    type: "RUN_FINISHED",
    runId: "server-run-no-snapshot",
    _clientRunId: "client-run-no-snapshot",
  });
  assert.equal(store.getState().runStatus, "completed");
}

{
  store.reset();
  store.handleLiveRunEvent({ type: "RUN_STARTED", runId: "tool-run-1" });
  store.addAssistantMessage("", true);
  store.handleLiveRunEvent({
    type: "TOOL_CALL_START",
    toolCallId: "tool-call-1",
    toolCallName: "inspect_schema",
  });
  store.handleLiveRunEvent({
    type: "TOOL_CALL_RESULT",
    toolCallId: "tool-call-1",
    toolCallName: "inspect_schema",
    content: JSON.stringify({ tables: [] }),
  });
  store.handleLiveRunEvent({ type: "RUN_FINISHED", runId: "tool-run-1" });

  const firstAssistant = store.getState().messages.find((message) => message.role === "assistant");
  const toolElement = firstAssistant?.elements.find((element) => element.type === "tool_call");
  assert.equal(toolElement?.toolCallId, "tool-call-1");
  assert.equal(toolElement?.toolCall?.status, "success");

  store.handleLiveRunEvent({ type: "RUN_STARTED", runId: "tool-run-2" });
  assert.equal(store.getState().toolCalls.length, 0);
  const retainedToolElement = store
    .getState()
    .messages.find((message) => message.role === "assistant")
    ?.elements.find((element) => element.type === "tool_call");
  assert.equal(retainedToolElement?.toolCall?.status, "success");

  store.addAssistantMessage("", true);
  store.handleLiveRunEvent({
    type: "TOOL_CALL_START",
    toolCallId: "tool-call-1",
    toolCallName: "inspect_schema",
  });

  const state = store.getState();
  const duplicateToolElements = state.messages
    .flatMap((message) => message.elements)
    .filter((element) => element.type === "tool_call" && element.toolCallId === "tool-call-1");
  assert.equal(duplicateToolElements.length, 2);
  assert.deepEqual(
    duplicateToolElements.map((element) => element.toolCall?.status),
    ["success", "running"],
  );

  const renderedToolCalls = collectRenderedToolCalls(buildChatLines({
    messages: state.messages,
    artifacts: [],
    toolCalls: state.toolCalls,
    columns: 100,
  })).filter((toolCall) => toolCall.id === "tool-call-1");
  assert.deepEqual(
    renderedToolCalls.map((toolCall) => toolCall.status),
    ["success", "running"],
  );
}

console.log("TUI live-run-state regression checks passed.");
