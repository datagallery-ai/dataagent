#!/usr/bin/env node
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createServer } from "node:net";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { setTimeout as delay } from "node:timers/promises";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const serviceDir = join(root, "services", "deepagents-runtime");

const parseSse = (text) => {
  const events = [];
  for (const frame of text.split("\n\n")) {
    const line = frame.trim();
    if (!line.startsWith("data:")) continue;
    const payload = line.slice(5).trim();
    if (!payload || payload === "[DONE]") continue;
    events.push(JSON.parse(payload));
  }
  return events;
};

const readResponse = async (response) => {
  const text = Buffer.from(await response.arrayBuffer()).toString("utf8");
  return { ok: response.ok, status: response.status, events: parseSse(text), text };
};

const freePort = async () => {
  const server = createServer();
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const port = server.address().port;
  await new Promise((resolve) => server.close(resolve));
  return port;
};

const waitForHealth = async (url, token) => {
  for (let attempt = 0; attempt < 40; attempt += 1) {
    try {
      const response = await fetch(`${url}/health`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (response.ok) {
        return await response.json();
      }
    } catch {
      // process still starting
    }
    await delay(250);
  }
  throw new Error("DEEPAGENTS_RUNTIME_HEALTH_TIMEOUT");
};

const port = await freePort();
const token = process.env.RUNTIME_SERVICE_TOKEN;
const child = spawn("uv", ["run", "deepagents-runtime"], {
  cwd: serviceDir,
  env: {
    ...process.env,
    DEEPAGENTS_RUNTIME_MODEL: "fake",
    RUNTIME_HOST: "127.0.0.1",
    RUNTIME_PORT: String(port),
  },
  stdio: ["ignore", "pipe", "pipe"],
  shell: process.platform === "win32",
});

let stderr = "";
child.stderr.on("data", (chunk) => {
  stderr += chunk.toString();
});

try {
  const url = `http://127.0.0.1:${port}`;
  const health = await waitForHealth(url, token);
  assert.equal(health.status, "ok");
  assert.equal(health.provider, "deepagents");

  const headers = {
    Accept: "text/event-stream",
    "Content-Type": "application/json",
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };
  const textResponse = await fetch(`${url}/runs/stream`, {
    method: "POST",
    headers,
    body: JSON.stringify({
      threadId: "smoke-sdk",
      runId: "smoke-text",
      messages: [{ id: "m1", role: "user", content: "你好" }],
      systemPrompt: "test",
    }),
  });
  const textResult = await readResponse(textResponse);
  assert.equal(textResult.ok, true, `text stream failed: ${textResult.status} ${textResult.text.slice(0, 200)}`);
  assert.ok(textResult.events.some((event) => event.type === "TEXT_MESSAGE_CONTENT"));
  assert.ok(textResult.events.some((event) => event.type === "RUN_FINISHED"));
  assert.ok(textResult.events.some((event) => event.name === "runtime.bound"));

  const interruptResponse = await fetch(`${url}/runs/stream`, {
    method: "POST",
    headers,
    body: JSON.stringify({
      threadId: "smoke-sdk-ask",
      runId: "smoke-ask",
      messages: [{ id: "m1", role: "user", content: "please interrupt" }],
      systemPrompt: "test",
    }),
  });
  const interruptResult = await readResponse(interruptResponse);
  const interrupt = interruptResult.events.find((event) => event.name === "on_interrupt");
  assert.equal(interrupt?.value?.type, "agent_interrupt");

  const resumeResponse = await fetch(`${url}/runs/stream`, {
    method: "POST",
    headers,
    body: JSON.stringify({
      threadId: "smoke-sdk-ask",
      runId: "smoke-ask",
      messages: [{ id: "m1", role: "user", content: "please interrupt" }],
      systemPrompt: "test",
      resume: {
        interrupt: interrupt.value,
        response: { answer: "继续" },
      },
    }),
  });
  const resumeResult = await readResponse(resumeResponse);
  assert.ok(resumeResult.events.some((event) => event.type === "RUN_FINISHED"));

  console.log("smoke-deepagents-sdk: ok");
} catch (error) {
  console.error(stderr);
  throw error;
} finally {
  child.kill("SIGTERM");
}
