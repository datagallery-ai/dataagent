import { spawn } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { setTimeout as delay } from "node:timers/promises";
import { createAuthenticatedTestClient } from "./lib/authenticated-test-client.mjs";

const apiDir = join(dirname(fileURLToPath(import.meta.url)), "../apps/api");

const apiPort = process.env.API_PORT ?? "8798";
const apiBaseUrl = `http://127.0.0.1:${apiPort}`;
const metadataDbPath = process.env.METADATA_DB_PATH ?? `storage/metadata/copilotkit-smoke-${Date.now()}.sqlite`;

const child = spawn("uv", ["run", "python", "-m", "datafoundry_api"], {
  cwd: apiDir,
  env: {
    ...process.env,
    API_HOST: "127.0.0.1",
    API_PORT: apiPort,
    METADATA_DB_PATH: metadataDbPath,
    AUTH_SESSION_SECRET: process.env.AUTH_SESSION_SECRET ?? "copilotkit-smoke-session-secret-32b!",
    AUTH_PUBLIC_BASE_URL: process.env.AUTH_PUBLIC_BASE_URL ?? "http://127.0.0.1:3000",
    AUTH_EMAIL_DELIVERY: process.env.AUTH_EMAIL_DELIVERY ?? "test",
    AUTH_REGISTRATION_MODE: process.env.AUTH_REGISTRATION_MODE ?? "open",
    DEEPAGENTS_RUNTIME_MODEL: process.env.DEEPAGENTS_RUNTIME_MODEL ?? "fake",
  },
  stdio: ["ignore", "pipe", "pipe"],
});

let output = "";
child.stdout.on("data", (chunk) => {
  output += chunk.toString();
});
child.stderr.on("data", (chunk) => {
  output += chunk.toString();
});

try {
  await waitForHealth(apiBaseUrl);
  const client = createAuthenticatedTestClient({ baseUrl: apiBaseUrl });
  await client.registerAndLogin({ displayName: "CopilotKit Smoke" });

  const optionsResponse = await client.fetch("/api/copilotkit", {
    method: "OPTIONS",
    headers: {
      Origin: "http://127.0.0.1:3000",
      "Access-Control-Request-Method": "POST",
    },
  });

  if (optionsResponse.status !== 204) {
    throw new Error(`Unexpected CopilotKit OPTIONS status: ${optionsResponse.status}`);
  }

  const envelopeResponse = await client.fetch("/api/copilotkit", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      method: "agent/run",
      params: { agentId: "dataFoundry" },
      body: { threadId: "t1", runId: "r1", messages: [] },
    }),
  });
  const envelopeBody = await envelopeResponse.json();
  if (envelopeResponse.status !== 400 || envelopeBody?.error?.code !== "BAD_REQUEST") {
    throw new Error(`Expected envelope rejection, got ${envelopeResponse.status} ${JSON.stringify(envelopeBody)}`);
  }

  const runResponse = await client.fetch("/api/copilotkit", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    },
    body: JSON.stringify({
      threadId: "smoke-thread",
      runId: "smoke-run",
      messages: [{ id: "m1", role: "user", content: "你好" }],
      state: {},
      tools: [],
      context: [],
      forwardedProps: {},
    }),
  });
  if (!runResponse.ok) {
    throw new Error(`Unexpected CopilotKit run status: ${runResponse.status} ${await runResponse.text()}`);
  }
  const contentType = runResponse.headers.get("content-type") ?? "";
  if (!contentType.includes("text/event-stream")) {
    throw new Error(`Expected text/event-stream, got ${contentType}`);
  }
  const streamText = await runResponse.text();
  if (!streamText.includes("RUN_STARTED") || !streamText.includes("RUN_FINISHED")) {
    throw new Error(`AG-UI stream missing terminal events: ${streamText.slice(0, 500)}`);
  }

  console.log("CopilotKit smoke OK: standard AG-UI RunAgentInput is accepted");
} finally {
  child.kill("SIGTERM");
}

async function waitForHealth(baseUrl) {
  const startedAt = Date.now();

  while (Date.now() - startedAt < 30000) {
    try {
      const response = await fetch(`${baseUrl}/healthz`);
      if (response.ok) {
        return;
      }
    } catch {
      // Retry until the Python API is ready.
    }
    await delay(300);
  }

  throw new Error(`API did not become healthy. Output:\n${output}`);
}
