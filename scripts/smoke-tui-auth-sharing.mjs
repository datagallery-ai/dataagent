import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { setTimeout as delay } from "node:timers/promises";

import { createAuthenticatedTestClient } from "./lib/authenticated-test-client.mjs";

const tuiAuthUrl = pathToFileURL(
  join(process.cwd(), "apps/tui/dist/auth/index.js")
).href;
const {
  AuthenticatedTransport,
  TuiAuthClient,
  TuiCookieJar,
} = await import(tuiAuthUrl);

const root = mkdtempSync(join(tmpdir(), "datafoundry-tui-auth-share-"));
const apiPort = process.env.API_PORT ?? "8799";
const baseUrl = `http://127.0.0.1:${apiPort}`;

const child = spawn("uv", ["run", "python", "-m", "datafoundry_api"], {
  cwd: join(process.cwd(), "apps/api"),
  env: {
    ...process.env,
    API_HOST: "127.0.0.1",
    API_PORT: apiPort,
    AUTH_SESSION_SECRET: "tui-share-session-secret-with-32-bytes!",
    AUTH_PUBLIC_BASE_URL: "http://127.0.0.1:3000",
    AUTH_EMAIL_DELIVERY: "test",
    AUTH_REGISTRATION_MODE: "open",
    DEEPAGENTS_RUNTIME_MODEL: "fake",
    METADATA_DB_PATH: join(root, "metadata.sqlite"),
    STORAGE_ROOT_DIR: root,
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
  await waitForHealth(baseUrl);
  const web = createAuthenticatedTestClient({ baseUrl });
  const webIdentity = await web.registerAndLogin({
    email: "share@example.com",
    password: "correct horse battery staple",
    displayName: "Share User",
    client: "web",
  });

  const tuiJar = new TuiCookieJar();
  const tuiAuth = new TuiAuthClient({
    apiBaseUrl: baseUrl,
    cookieJar: tuiJar,
  });
  const tuiSession = await tuiAuth.login("share@example.com", "correct horse battery staple");
  assert.equal(tuiSession.user.id, webIdentity.userId);
  assert.equal(tuiSession.workspace.id, webIdentity.workspaceId);
  assert.ok(tuiSession.expiresAt);

  const transport = new AuthenticatedTransport({
    cookieJar: tuiJar,
    refreshCsrf: () => tuiAuth.refreshCsrf(),
    onSessionInvalid: async () => {
      tuiJar.clear();
    },
  });

  const meResponse = await transport.fetch(`${baseUrl}/api/v1/me`);
  const meBody = await meResponse.json();
  assert.equal(meResponse.status, 200);
  assert.equal(meBody.data.user.id, webIdentity.userId);
  assert.equal(meBody.data.workspace.id, webIdentity.workspaceId);

  const resumeList = await transport.fetch(`${baseUrl}/api/v1/sessions?limit=20`);
  const resumeBody = await resumeList.json();
  assert.equal(resumeList.status, 200, JSON.stringify(resumeBody));
  assert.deepEqual(resumeBody.data.sessions, []);

  const anonymousAgui = await fetch(`${baseUrl}/api/copilotkit`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });
  assert.equal(anonymousAgui.status, 401);

  const authenticatedAgui = await transport.fetch(`${baseUrl}/api/copilotkit`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/event-stream",
    },
    body: JSON.stringify({
      threadId: "tui-share",
      runId: "tui-share-run",
      messages: [{ id: "m1", role: "user", content: "你好" }],
      state: {},
      tools: [],
      context: [],
      forwardedProps: {},
    }),
  });
  assert.notEqual(authenticatedAgui.status, 401);
  const streamText = await authenticatedAgui.text();
  assert.ok(
    authenticatedAgui.ok,
    `AG-UI should accept authenticated Cookie/CSRF, got ${authenticatedAgui.status} ${streamText}`,
  );
  assert.match(streamText, /RUN_STARTED/);
  assert.match(streamText, /RUN_FINISHED/);

  console.log("TUI/Web auth sharing smoke OK");
} finally {
  child.kill("SIGTERM");
}

async function waitForHealth(baseUrl) {
  const startedAt = Date.now();
  while (Date.now() - startedAt < 30000) {
    try {
      const response = await fetch(`${baseUrl}/healthz`);
      if (response.ok) return;
    } catch {
      // Retry until the Python API is ready.
    }
    await delay(300);
  }
  throw new Error(`API did not become healthy. Output:\n${output}`);
}
