#!/usr/bin/env node
import assert from "node:assert/strict";
import { createRuntimeStubServer } from "../apps/api/dist/runtime/stub-server.js";
import { HttpRuntimeClient } from "../apps/api/dist/runtime/client.js";
import { createInProcessRuntime } from "../apps/api/dist/runtime/in-process.js";

const eventsOf = async (iterable) => {
  const events = [];
  for await (const event of iterable) {
    events.push(event);
  }
  return events;
};

const inProcess = createInProcessRuntime();
const health = await inProcess.health();
assert.equal(health.status, "ok");

const textEvents = await eventsOf(inProcess.startRun({
  threadId: "s1",
  runId: "r-text",
  messages: [{ id: "m1", role: "user", content: "你好" }],
  systemPrompt: "test"
}));
assert.ok(textEvents.some((event) => event.type === "TEXT_MESSAGE_CONTENT"));
assert.ok(textEvents.some((event) => event.type === "RUN_FINISHED"));
assert.ok(textEvents.some((event) => event.name === "runtime.bound"));

const toolEvents = await eventsOf(inProcess.startRun({
  threadId: "s1",
  runId: "r-tool",
  messages: [{ id: "m1", role: "user", content: "make a plan" }],
  systemPrompt: "test"
}));
assert.ok(toolEvents.some((event) => event.type === "TOOL_CALL_START" && event.toolCallName === "write_todos"));

const interruptEvents = await eventsOf(inProcess.startRun({
  threadId: "s1",
  runId: "r-ask",
  messages: [{ id: "m1", role: "user", content: "please interrupt" }],
  systemPrompt: "test"
}));
const interrupt = interruptEvents.find((event) => event.name === "on_interrupt");
assert.equal(interrupt?.value?.type, "agent_interrupt");

const resumeEvents = await eventsOf(inProcess.startRun({
  threadId: "s1",
  runId: "r-ask",
  messages: [{ id: "m1", role: "user", content: "please interrupt" }],
  systemPrompt: "test",
  resume: {
    interrupt: interrupt.value,
    response: { answer: "继续" }
  }
}));
assert.ok(resumeEvents.some((event) => event.type === "TOOL_CALL_RESULT"));

const server = createRuntimeStubServer();
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const address = server.address();
const client = new HttpRuntimeClient({ url: `http://127.0.0.1:${address.port}` });
const remoteHealth = await client.health();
assert.equal(remoteHealth.status, "ok");
const remoteEvents = await eventsOf(client.startRun({
  threadId: "s2",
  runId: "r-http",
  messages: [{ id: "m1", role: "user", content: "hello" }],
  systemPrompt: "test"
}));
assert.ok(remoteEvents.some((event) => event.type === "RUN_FINISHED"));
await client.cancelRun("r-http");
server.close();

console.log("smoke-deepagents-runtime: ok");
