#!/usr/bin/env node
/**
 * Password-mode user isolation smoke via the Next.js same-origin proxy.
 */
import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { createAuthenticatedTestClient } from "./lib/authenticated-test-client.mjs";

const baseUrl = process.env.SMOKE_BASE_URL ?? "http://localhost:3000";

const createDatasource = async (client, id, name, filePath) =>
  client.fetchJson("/api/v1/datasources", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      id,
      name,
      type: "sqlite",
      settings: { filePath },
    }),
  });

const patchSessionTitle = async (client, sessionId, title) =>
  client.fetchJson(`/api/v1/sessions/${encodeURIComponent(sessionId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });

const stamp = Date.now();
const root = mkdtempSync(join(tmpdir(), "df-password-front-"));
const aliceDb = join(root, "alice.sqlite");
const bobDb = join(root, "bob.sqlite");

try {
  const aliceClient = createAuthenticatedTestClient({ baseUrl });
  const alice = await aliceClient.registerAndLogin({
    email: `alice-front-${stamp}@example.com`,
    displayName: "Alice Front",
  });
  const aliceDs = await createDatasource(aliceClient, "alice-front-db", "Alice Front DB", aliceDb);
  assert.equal(aliceDs.response.status, 201, JSON.stringify(aliceDs.body));

  const aliceSessionId = crypto.randomUUID();
  const aliceTitle = await patchSessionTitle(aliceClient, aliceSessionId, "Alice isolated session");
  assert.equal(aliceTitle.response.status, 200, JSON.stringify(aliceTitle.body));

  const aliceList = await aliceClient.fetchJson("/api/v1/datasources");
  assert.equal(aliceList.response.status, 200);
  assert.equal(aliceList.body.data.some((item) => item.id === "alice-front-db"), true);
  const aliceSessions = await aliceClient.fetchJson("/api/v1/sessions?limit=20");
  assert.equal(aliceSessions.response.status, 200);
  assert.equal(
    aliceSessions.body.data.sessions.some((item) => item.title === "Alice isolated session"),
    true,
  );

  const bobClient = createAuthenticatedTestClient({ baseUrl });
  const bob = await bobClient.registerAndLogin({
    email: `bob-front-${stamp}@example.com`,
    displayName: "Bob Front",
  });
  const bobDs = await createDatasource(bobClient, "bob-front-db", "Bob Front DB", bobDb);
  assert.equal(bobDs.response.status, 201, JSON.stringify(bobDs.body));

  const bobList = await bobClient.fetchJson("/api/v1/datasources");
  assert.equal(bobList.response.status, 200);
  assert.equal(bobList.body.data.some((item) => item.id === "bob-front-db"), true);
  assert.equal(bobList.body.data.some((item) => item.id === "alice-front-db"), false);

  const bobSessions = await bobClient.fetchJson("/api/v1/sessions?limit=20");
  assert.equal(bobSessions.response.status, 200);
  assert.equal(
    bobSessions.body.data.sessions.some((item) => item.title === "Alice isolated session"),
    false,
  );

  // Intentionally omit X-CSRF-Token while keeping the session cookie.
  const missingCsrf = await fetch(`${baseUrl}/api/v1/datasources/bob-front-db`, {
    method: "DELETE",
    headers: {
      Cookie: `df_session=${encodeURIComponent(bob.cookies.df_session)}; df_csrf=${encodeURIComponent(bob.cookies.df_csrf)}`,
    },
  });
  const missingCsrfBody = await missingCsrf.json();
  assert.equal(missingCsrf.status, 403);
  assert.equal(missingCsrfBody.error.code, "CSRF_INVALID");

  const me = await bobClient.verifyCurrentUser();
  assert.equal(me.user.email, bob.email);
  assert.notEqual(me.user.id, alice.userId);

  console.log(
    `Password frontend isolation smoke OK via ${baseUrl}: alice=${alice.userId.slice(0, 8)} bob=${bob.userId.slice(0, 8)}`,
  );
} catch (error) {
  console.error(error);
  process.exitCode = 1;
}
