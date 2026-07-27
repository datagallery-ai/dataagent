import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { createServer as createApiServer } from "../apps/api/dist/server.js";
import { createMetadataStore } from "../packages/metadata/dist/index.js";
import { createAuthenticatedTestClient } from "./lib/authenticated-test-client.mjs";

const root = mkdtempSync(join(tmpdir(), "datafoundry-auth-smoke-"));
process.env.DATAFOUNDRY_AUTH_MODE = "password";
process.env.AUTH_SESSION_SECRET = "smoke-session-secret-with-at-least-32-bytes";
process.env.AUTH_PUBLIC_BASE_URL = "http://127.0.0.1";
process.env.AUTH_EMAIL_DELIVERY = "test";
process.env.AUTH_REGISTRATION_MODE = "open";
process.env.EMBEDDING_API_KEY = "";
process.env.MASTRA_STORAGE_PATH = join(root, "mastra.sqlite");
process.env.STORAGE_ROOT_DIR = root;

const metadataStore = createMetadataStore({
  database_path: join(root, "metadata.sqlite"),
  secret_master_key: "auth-smoke-secret-master-key"
});
const server = await createApiServer({ metadataStore });
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const address = server.address();
assert(address && typeof address === "object");
const baseUrl = `http://127.0.0.1:${address.port}`;

const closeServer = async () => {
  await new Promise((resolve, reject) => {
    server.close((error) => error ? reject(error) : resolve());
    setImmediate(() => server.closeAllConnections?.());
  });
};

const client = createAuthenticatedTestClient({ baseUrl });

const requestJson = async (path, init = {}) => {
  const response = await client.fetch(path, init);
  const body = await response.json();
  return { body, response };
};

try {
  const anonymousMe = await requestJson("/api/v1/me");
  assert.equal(anonymousMe.response.status, 401);
  assert.equal(anonymousMe.body.error.code, "UNAUTHORIZED");

  const tooShort = await requestJson("/api/v1/auth/register", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      email: "short@example.com",
      password: "12345",
      displayName: "Too Short"
    })
  });
  assert.equal(tooShort.response.status, 400, JSON.stringify(tooShort.body));
  assert.equal(tooShort.body.error.code, "BAD_REQUEST");
  assert.match(tooShort.body.error.message, /at least 6 characters/i);

  const minLengthOk = await requestJson("/api/v1/auth/register", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      email: "minlen@example.com",
      password: "123456",
      displayName: "Min Length"
    })
  });
  assert.equal(minLengthOk.response.status, 201, JSON.stringify(minLengthOk.body));

  const registered = await requestJson("/api/v1/auth/register", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      email: "alice@example.com",
      password: "correct horse battery staple",
      displayName: "Alice Analyst"
    })
  });
  assert.equal(registered.response.status, 201, JSON.stringify(registered.body));
  assert.equal(typeof registered.body.data.verificationToken, "string");
  assert.equal(registered.body.data.user.email, "alice@example.com");
  assert.equal("devToken" in registered.body.data.user, false);

  const unverifiedLogin = await requestJson("/api/v1/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email: "alice@example.com", password: "correct horse battery staple" })
  });
  assert.equal(unverifiedLogin.response.status, 403);
  assert.equal(unverifiedLogin.body.error.code, "EMAIL_NOT_VERIFIED");

  const verified = await requestJson("/api/v1/auth/verify-email", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token: registered.body.data.verificationToken })
  });
  assert.equal(verified.response.status, 200, JSON.stringify(verified.body));

  const loggedIn = await requestJson("/api/v1/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email: "alice@example.com", password: "correct horse battery staple" })
  });
  assert.equal(loggedIn.response.status, 200, JSON.stringify(loggedIn.body));
  assert(loggedIn.response.headers.getSetCookie().some((cookie) => /df_session=.*HttpOnly/i.test(cookie)));
  assert(loggedIn.response.headers.getSetCookie().some((cookie) => /^df_csrf=/i.test(cookie)));
  assert.equal(loggedIn.body.data.user.email, "alice@example.com");
  assert.equal("devToken" in loggedIn.body.data.user, false);
  assert.match(loggedIn.body.data.workspace.id, /^personal-/);

  const me = await requestJson("/api/v1/me");
  assert.equal(me.response.status, 200);
  assert.equal(me.body.data.user.email, "alice@example.com");
  assert.equal(me.body.data.workspace.id, loggedIn.body.data.workspace.id);

  const missingCsrf = await requestJson("/api/v1/datasources", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": "" },
    body: JSON.stringify({
      id: "alice-sqlite",
      name: "Alice SQLite",
      type: "sqlite",
      settings: { filePath: join(root, "alice.sqlite") }
    })
  });
  // Clear empty override so subsequent unsafe calls auto-attach cookie CSRF.
  assert.equal(missingCsrf.response.status, 403);
  assert.equal(missingCsrf.body.error.code, "CSRF_INVALID");

  const csrf = client.cookies.csrfToken();
  assert(csrf, "Expected login to set a readable CSRF cookie");
  const aliceDatasource = await requestJson("/api/v1/datasources", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      id: "alice-sqlite",
      name: "Alice SQLite",
      type: "sqlite",
      settings: { filePath: join(root, "alice.sqlite") }
    })
  });
  assert.equal(aliceDatasource.response.status, 201, JSON.stringify(aliceDatasource.body));

  const forgot = await requestJson("/api/v1/auth/password/forgot", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email: "alice@example.com" })
  });
  assert.equal(forgot.response.status, 200);
  assert.equal(typeof forgot.body.data.resetToken, "string");

  const reset = await requestJson("/api/v1/auth/password/reset", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      token: forgot.body.data.resetToken,
      password: "new correct horse battery staple"
    })
  });
  assert.equal(reset.response.status, 200);

  const revokedMe = await requestJson("/api/v1/me");
  assert.equal(revokedMe.response.status, 401);

  client.cookies.clear();
  const relogged = await requestJson("/api/v1/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email: "alice@example.com", password: "new correct horse battery staple" })
  });
  assert.equal(relogged.response.status, 200);

  const logout = await requestJson("/api/v1/auth/logout", {
    method: "POST"
  });
  assert.equal(logout.response.status, 200);

  const afterLogout = await requestJson("/api/v1/me");
  assert.equal(afterLogout.response.status, 401);

  console.log(`Auth smoke OK: workspace=${loggedIn.body.data.workspace.id}`);
} finally {
  await closeServer();
}
