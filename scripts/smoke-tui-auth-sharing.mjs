import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

import { createServer as createApiServer } from "../apps/api/dist/server.js";
import { createMetadataStore } from "../packages/metadata/dist/index.js";
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
process.env.DATAFOUNDRY_AUTH_MODE = "password";
process.env.AUTH_SESSION_SECRET = "tui-share-session-secret-with-32-bytes!";
process.env.AUTH_PUBLIC_BASE_URL = "http://127.0.0.1";
process.env.AUTH_EMAIL_DELIVERY = "test";
process.env.AUTH_REGISTRATION_MODE = "open";
process.env.EMBEDDING_API_KEY = "";
process.env.MASTRA_STORAGE_PATH = join(root, "mastra.sqlite");
process.env.STORAGE_ROOT_DIR = root;

const metadataStore = createMetadataStore({
  database_path: join(root, "metadata.sqlite"),
  secret_master_key: "tui-share-secret-master-key",
});
const server = await createApiServer({ metadataStore });
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const address = server.address();
assert(address && typeof address === "object");
const baseUrl = `http://127.0.0.1:${address.port}`;

const closeServer = async () => {
  await new Promise((resolve, reject) => {
    server.close((error) => (error ? reject(error) : resolve()));
    setImmediate(() => server.closeAllConnections?.());
  });
};

try {
  const web = createAuthenticatedTestClient({ baseUrl });
  const webIdentity = await web.registerAndLogin({
    email: "share@example.com",
    password: "correct horse battery staple",
    displayName: "Share User",
    client: "web",
  });

  const webSessionId = "web-created-session";
  const webCreate = await web.fetchJson(`/api/v1/sessions/${webSessionId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title: "Created by Web" }),
  });
  assert.equal(webCreate.response.status, 200, JSON.stringify(webCreate.body));

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
  assert.ok(
    resumeBody.data.sessions.some((session) => session.id === webSessionId),
    "TUI should see the Web-created session",
  );

  const conversation = await transport.fetch(
    `${baseUrl}/api/v1/sessions/${webSessionId}/conversation`,
  );
  assert.equal(conversation.status, 200);

  const tuiSessionId = "tui-created-session";
  const tuiCreate = await transport.fetch(`${baseUrl}/api/v1/sessions/${tuiSessionId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title: "Created by TUI" }),
  });
  const tuiCreateBody = await tuiCreate.json();
  assert.equal(tuiCreate.status, 200, JSON.stringify(tuiCreateBody));

  const webList = await web.fetchJson("/api/v1/sessions?limit=20");
  assert.equal(webList.response.status, 200);
  assert.ok(
    webList.body.data.sessions.some((session) => session.id === tuiSessionId),
    "Web should see the TUI-created session",
  );

  const anonymousAgui = await fetch(`${baseUrl}/api/copilotkit`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });
  assert.equal(anonymousAgui.status, 401);

  const authenticatedAgui = await transport.fetch(`${baseUrl}/api/copilotkit`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });
  const aguiBody = await authenticatedAgui.json();
  const acceptedAuth =
    (authenticatedAgui.status === 503 && aguiBody?.error?.code === "PROVIDER_CONFIG_MISSING")
    || (authenticatedAgui.status === 400 && aguiBody?.message === "Missing method field")
    || authenticatedAgui.status !== 401;
  assert.ok(
    acceptedAuth,
    `AG-UI should accept authenticated Cookie/CSRF, got ${authenticatedAgui.status} ${JSON.stringify(aguiBody)}`,
  );
  assert.notEqual(authenticatedAgui.status, 401);

  console.log("TUI/Web auth sharing smoke OK");
} finally {
  await closeServer();
}
