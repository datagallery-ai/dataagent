import assert from "node:assert/strict";
import test from "node:test";
import { randomUUID } from "node:crypto";

import {
  createAuthenticatedTestClient,
  resolveApiUrl
} from "./authenticated-test-client.mjs";

test("resolveApiUrl keeps deployment path prefix", () => {
  const url = resolveApiUrl("https://example.com/datafoundry", "api/v1/me");
  assert.equal(url.href, "https://example.com/datafoundry/api/v1/me");
});

test("resolveApiUrl strips search and hash from base", () => {
  const url = resolveApiUrl("https://example.com/datafoundry?x=1#frag", "/api/v1/me");
  assert.equal(url.href, "https://example.com/datafoundry/api/v1/me");
});

test("resolveApiUrl preserves query from relative path", () => {
  const url = resolveApiUrl("https://example.com/datafoundry", "/api/v1/sessions/s1/conversation?limit=10");
  assert.equal(url.href, "https://example.com/datafoundry/api/v1/sessions/s1/conversation?limit=10");
});

test("adds cookie and csrf to unsafe requests", async () => {
  const calls = [];
  const client = createAuthenticatedTestClient({
    baseUrl: "http://127.0.0.1:8787",
    fetchImpl: async (url, init) => {
      calls.push({ url, init });
      return new Response("{}", { status: 200 });
    }
  });
  client.cookies.replace({
    df_session: "session-secret",
    df_csrf: "csrf-secret"
  });

  await client.fetch("/api/v1/config", { method: "POST", body: "{}" });

  assert.equal(calls[0].init.headers.get("cookie"), "df_session=session-secret; df_csrf=csrf-secret");
  assert.equal(calls[0].init.headers.get("x-csrf-token"), "csrf-secret");
});

test("does not add csrf to safe GET requests", async () => {
  const calls = [];
  const client = createAuthenticatedTestClient({
    baseUrl: "http://127.0.0.1:8787",
    fetchImpl: async (url, init) => {
      calls.push({ url, init });
      return new Response("{}", { status: 200 });
    }
  });
  client.cookies.replace({
    df_session: "session-secret",
    df_csrf: "csrf-secret"
  });

  await client.fetch("/api/v1/me");

  assert.equal(calls[0].init.headers.get("cookie"), "df_session=session-secret; df_csrf=csrf-secret");
  assert.equal(calls[0].init.headers.get("x-csrf-token"), null);
});

test("absorbs Set-Cookie from multiple headers", async () => {
  const client = createAuthenticatedTestClient({
    baseUrl: "http://127.0.0.1:8787",
    fetchImpl: async () => {
      const headers = new Headers();
      headers.append("Set-Cookie", "df_session=session-a; Path=/; HttpOnly");
      headers.append("Set-Cookie", "df_csrf=csrf-a; Path=/");
      return new Response("{}", { status: 200, headers });
    }
  });

  await client.fetch("/api/v1/auth/login", { method: "POST", body: "{}" });
  assert.deepEqual(client.cookies.snapshot(), {
    df_session: "session-a",
    df_csrf: "csrf-a"
  });
});

test("auth errors omit cookie secrets", async () => {
  const client = createAuthenticatedTestClient({
    baseUrl: "http://127.0.0.1:8787",
    fetchImpl: async () =>
      new Response(JSON.stringify({ error: { code: "UNAUTHORIZED", message: "nope" } }), {
        status: 401,
        headers: { "Content-Type": "application/json" }
      })
  });
  client.cookies.replace({
    df_session: "session-secret",
    df_csrf: "csrf-secret"
  });

  await assert.rejects(
    () => client.fetch("/api/v1/me", { expectOk: true }),
    (error) => {
      const text = String(error);
      assert.equal(error.status, 401);
      assert.equal(error.code, "UNAUTHORIZED");
      assert.doesNotMatch(text, /session-secret|csrf-secret/);
      return true;
    }
  );
});

test("verifyCurrentUser calls GET /api/v1/me only", async () => {
  const calls = [];
  const client = createAuthenticatedTestClient({
    baseUrl: "http://127.0.0.1:8787/datafoundry",
    fetchImpl: async (url, init) => {
      calls.push({ url: String(url), method: init?.method ?? "GET" });
      return new Response(
        JSON.stringify({
          data: {
            user: { id: "u1", email: "a@example.test" },
            workspace: { id: "w1" }
          }
        }),
        { status: 200, headers: { "Content-Type": "application/json" } }
      );
    }
  });
  client.cookies.replace({ df_session: "s", df_csrf: "c" });

  const me = await client.verifyCurrentUser();
  assert.equal(me.user.id, "u1");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "http://127.0.0.1:8787/datafoundry/api/v1/me");
  assert.equal(calls[0].method, "GET");
  assert.ok(!calls.some((call) => call.url.includes("/api/v1/auth/me")));
});

test("registerAndLogin walks register verify login and me", async () => {
  const calls = [];
  const email = `${randomUUID()}@example.test`;
  const client = createAuthenticatedTestClient({
    baseUrl: "http://127.0.0.1:8787",
    fetchImpl: async (url, init) => {
      const href = String(url);
      const method = init?.method ?? "GET";
      calls.push({ href, method, body: init?.body });
      if (href.endsWith("/api/v1/auth/register") && method === "POST") {
        return new Response(
          JSON.stringify({
            data: {
              verificationToken: "verify-token",
              user: { id: "user-1", email }
            }
          }),
          {
            status: 201,
            headers: { "Content-Type": "application/json" }
          }
        );
      }
      if (href.endsWith("/api/v1/auth/verify-email") && method === "POST") {
        return new Response(JSON.stringify({ data: { ok: true } }), {
          status: 200,
          headers: { "Content-Type": "application/json" }
        });
      }
      if (href.endsWith("/api/v1/auth/login") && method === "POST") {
        const headers = new Headers({ "Content-Type": "application/json" });
        headers.append("Set-Cookie", "df_session=session-1; Path=/; HttpOnly");
        headers.append("Set-Cookie", "df_csrf=csrf-1; Path=/");
        return new Response(
          JSON.stringify({
            data: {
              user: { id: "user-1", email },
              workspace: { id: "personal-user-1" }
            }
          }),
          { status: 200, headers }
        );
      }
      if (href.endsWith("/api/v1/me") && method === "GET") {
        return new Response(
          JSON.stringify({
            data: {
              user: { id: "user-1", email },
              workspace: { id: "personal-user-1" }
            }
          }),
          { status: 200, headers: { "Content-Type": "application/json" } }
        );
      }
      return new Response(JSON.stringify({ error: { code: "NOT_FOUND", message: href } }), {
        status: 404,
        headers: { "Content-Type": "application/json" }
      });
    }
  });

  const result = await client.registerAndLogin({
    email,
    password: "correct-horse-battery",
    displayName: "Test User"
  });

  assert.equal(result.userId, "user-1");
  assert.equal(result.workspaceId, "personal-user-1");
  assert.equal(result.email, email);
  assert.deepEqual(result.cookies, {
    df_session: "session-1",
    df_csrf: "csrf-1"
  });
  assert.deepEqual(
    calls.map((call) => `${call.method} ${new URL(call.href).pathname}`),
    [
      "POST /api/v1/auth/register",
      "POST /api/v1/auth/verify-email",
      "POST /api/v1/auth/login",
      "GET /api/v1/me"
    ]
  );
  assert.ok(!calls.some((call) => call.href.includes("/api/v1/auth/me")));
});
