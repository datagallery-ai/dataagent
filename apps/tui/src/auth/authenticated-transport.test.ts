import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { AuthenticatedTransport } from "./authenticated-transport.js";
import { TuiCookieJar } from "./cookie-jar.js";

function jsonResponse(
  status: number,
  body: unknown,
  headers?: Record<string, string>,
): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "content-type": "application/json",
      ...headers,
    },
  });
}

describe("AuthenticatedTransport", () => {
  it("attaches cookies to all requests and CSRF only to unsafe methods", async () => {
    const jar = new TuiCookieJar();
    jar.replace({ df_session: "sess", df_csrf: "csrf-1" });
    const seen: Array<{ method: string; cookie?: string | null; csrf?: string | null }> = [];
    const transport = new AuthenticatedTransport({
      cookieJar: jar,
      refreshCsrf: async () => {},
      onSessionInvalid: async () => {},
      fetchImpl: async (input, init) => {
        const request = input instanceof Request ? input : new Request(input, init);
        seen.push({
          method: request.method,
          cookie: request.headers.get("cookie"),
          csrf: request.headers.get("x-csrf-token"),
        });
        return jsonResponse(200, { ok: true });
      },
    });

    await transport.fetch("http://127.0.0.1/api/v1/me");
    await transport.fetch("http://127.0.0.1/api/v1/datasources", {
      method: "POST",
      body: "{}",
    });

    assert.deepEqual(seen, [
      {
        method: "GET",
        cookie: "df_session=sess; df_csrf=csrf-1",
        csrf: null,
      },
      {
        method: "POST",
        cookie: "df_session=sess; df_csrf=csrf-1",
        csrf: "csrf-1",
      },
    ]);
  });

  it("invokes onSessionInvalid once on 401 and does not retry the business request", async () => {
    const jar = new TuiCookieJar();
    jar.replace({ df_session: "sess", df_csrf: "csrf-1" });
    let fetches = 0;
    let invalidCalls = 0;
    const transport = new AuthenticatedTransport({
      cookieJar: jar,
      refreshCsrf: async () => {
        throw new Error("should not refresh on 401");
      },
      onSessionInvalid: async () => {
        invalidCalls += 1;
      },
      fetchImpl: async () => {
        fetches += 1;
        return jsonResponse(401, { error: { code: "UNAUTHORIZED" } });
      },
    });

    const response = await transport.fetch("http://127.0.0.1/api/v1/me");
    assert.equal(response.status, 401);
    assert.equal(fetches, 1);
    assert.equal(invalidCalls, 1);
  });

  it("retries once only for CSRF_INVALID and uses response.clone for code checks", async () => {
    const jar = new TuiCookieJar();
    jar.replace({ df_session: "sess", df_csrf: "old" });
    let fetches = 0;
    let refreshed = 0;
    const transport = new AuthenticatedTransport({
      cookieJar: jar,
      refreshCsrf: async () => {
        refreshed += 1;
        jar.replace({ df_session: "sess", df_csrf: "new" });
      },
      onSessionInvalid: async () => {
        throw new Error("should not invalidate after successful retry");
      },
      fetchImpl: async (_input, init) => {
        fetches += 1;
        const headers = new Headers(init?.headers);
        if (fetches === 1) {
          return jsonResponse(403, { error: { code: "CSRF_INVALID", message: "bad" } });
        }
        assert.equal(headers.get("x-csrf-token"), "new");
        return jsonResponse(200, { success: true, data: { ok: true } });
      },
    });

    const response = await transport.fetch("http://127.0.0.1/api/v1/datasources", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ id: "x" }),
    });
    const body = await response.json() as { success: boolean };
    assert.equal(response.status, 200);
    assert.equal(body.success, true);
    assert.equal(fetches, 2);
    assert.equal(refreshed, 1);
  });

  it("does not retry other 403 responses", async () => {
    const jar = new TuiCookieJar();
    jar.replace({ df_session: "sess", df_csrf: "csrf" });
    let fetches = 0;
    const transport = new AuthenticatedTransport({
      cookieJar: jar,
      refreshCsrf: async () => {
        throw new Error("should not refresh");
      },
      onSessionInvalid: async () => {},
      fetchImpl: async () => {
        fetches += 1;
        return jsonResponse(403, { error: { code: "FORBIDDEN", message: "no" } });
      },
    });

    const response = await transport.fetch("http://127.0.0.1/api/v1/datasources", {
      method: "POST",
      body: "{}",
    });
    assert.equal(response.status, 403);
    assert.equal(fetches, 1);
  });

  it("invalidates session when Request body cannot be cloned for CSRF retry", async () => {
    const jar = new TuiCookieJar();
    jar.replace({ df_session: "sess", df_csrf: "csrf" });
    let invalid = 0;
    const stream = new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode("{}"));
        controller.close();
      },
    });
    const request = new Request("http://127.0.0.1/api/v1/datasources", {
      method: "POST",
      body: stream,
      // @ts-expect-error Node fetch duplex
      duplex: "half",
    });
    // Consume clone ability by locking in environments that disallow second clone.
    const transport = new AuthenticatedTransport({
      cookieJar: jar,
      refreshCsrf: async () => {},
      onSessionInvalid: async () => {
        invalid += 1;
      },
      fetchImpl: async () => jsonResponse(403, { error: { code: "CSRF_INVALID" } }),
    });

    // Force non-replayable by passing stream body via init.
    const response = await transport.fetch("http://127.0.0.1/api/v1/datasources", {
      method: "POST",
      body: new ReadableStream({
        start(controller) {
          controller.enqueue(new TextEncoder().encode("{}"));
          controller.close();
        },
      }),
      // @ts-expect-error Node fetch duplex
      duplex: "half",
    });
    assert.equal(response.status, 403);
    assert.equal(invalid, 1);
    void request;
  });

  it("single-flights concurrent CSRF refresh", async () => {
    const jar = new TuiCookieJar();
    jar.replace({ df_session: "sess", df_csrf: "old" });
    let refreshCalls = 0;
    let releaseRefresh!: () => void;
    const refreshGate = new Promise<void>((resolve) => {
      releaseRefresh = resolve;
    });
    const transport = new AuthenticatedTransport({
      cookieJar: jar,
      refreshCsrf: async () => {
        refreshCalls += 1;
        jar.replace({ df_session: "sess", df_csrf: "new" });
        await refreshGate;
      },
      onSessionInvalid: async () => {},
      fetchImpl: async (input, init) => {
        const method = String(init?.method ?? "GET");
        if (method === "POST" && jar.csrfToken() === "old") {
          return jsonResponse(403, { error: { code: "CSRF_INVALID" } });
        }
        return jsonResponse(200, { ok: true });
      },
    });

    const pending = Promise.all([
      transport.fetch("http://127.0.0.1/api/v1/a", { method: "POST", body: "{}" }),
      transport.fetch("http://127.0.0.1/api/v1/b", { method: "POST", body: "{}" }),
    ]);
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(refreshCalls, 1);
    releaseRefresh();
    const responses = await pending;
    assert.equal(refreshCalls, 1);
    assert.deepEqual(responses.map((response) => response.status), [200, 200]);
  });

  it("invalidates session when CSRF refresh fails", async () => {
    const jar = new TuiCookieJar();
    jar.replace({ df_session: "sess", df_csrf: "old" });
    let invalid = 0;
    let authRequired = 0;
    const transport = new AuthenticatedTransport({
      cookieJar: jar,
      refreshCsrf: async () => {
        throw new Error("refresh 401");
      },
      onSessionInvalid: async () => {
        invalid += 1;
      },
      fetchImpl: async () => jsonResponse(403, { error: { code: "CSRF_INVALID" } }),
    });
    transport.onAuthRequired(() => {
      authRequired += 1;
    });

    const response = await transport.fetch("http://127.0.0.1/api/v1/datasources", {
      method: "POST",
      body: "{}",
    });
    assert.equal(response.status, 403);
    assert.equal(invalid, 1);
    assert.equal(authRequired, 1);
  });

  it("notifies auth-required listeners once on 401", async () => {
    const jar = new TuiCookieJar();
    jar.replace({ df_session: "sess", df_csrf: "csrf" });
    let authRequired = 0;
    let invalidCalls = 0;
    const transport = new AuthenticatedTransport({
      cookieJar: jar,
      refreshCsrf: async () => {},
      onSessionInvalid: async () => {
        invalidCalls += 1;
      },
      fetchImpl: async () => jsonResponse(401, { error: { code: "UNAUTHORIZED" } }),
    });
    transport.onAuthRequired(() => {
      authRequired += 1;
    });

    await transport.fetch("http://127.0.0.1/api/v1/me");
    await transport.fetch("http://127.0.0.1/api/v1/me");
    assert.equal(authRequired, 1);
    assert.equal(invalidCalls, 1);
  });

  it("still notifies auth-required when onSessionInvalid cleanup throws", async () => {
    const jar = new TuiCookieJar();
    jar.replace({ df_session: "sess", df_csrf: "csrf" });
    let authRequired = 0;
    const transport = new AuthenticatedTransport({
      cookieJar: jar,
      refreshCsrf: async () => {},
      onSessionInvalid: async () => {
        throw new Error("disk locked");
      },
      fetchImpl: async () => jsonResponse(401, { error: { code: "UNAUTHORIZED" } }),
    });
    transport.onAuthRequired(() => {
      authRequired += 1;
    });

    const response = await transport.fetch("http://127.0.0.1/api/v1/me");
    assert.equal(response.status, 401);
    assert.equal(authRequired, 1);

    // Sticky replay for late subscribers must still work after a failed cleanup.
    let late = 0;
    transport.onAuthRequired(() => {
      late += 1;
    });
    assert.equal(late, 1);
  });

  it("dedups concurrent 401 cleanup before onSessionInvalid side effects", async () => {
    const jar = new TuiCookieJar();
    jar.replace({ df_session: "sess", df_csrf: "csrf" });
    let invalidCalls = 0;
    let releaseCleanup!: () => void;
    const cleanupGate = new Promise<void>((resolve) => {
      releaseCleanup = resolve;
    });
    const transport = new AuthenticatedTransport({
      cookieJar: jar,
      refreshCsrf: async () => {},
      onSessionInvalid: async () => {
        invalidCalls += 1;
        await cleanupGate;
      },
      fetchImpl: async () => jsonResponse(401, { error: { code: "UNAUTHORIZED" } }),
    });

    const pending = Promise.all([
      transport.fetch("http://127.0.0.1/api/v1/a"),
      transport.fetch("http://127.0.0.1/api/v1/b"),
    ]);
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(invalidCalls, 1);
    releaseCleanup();
    await pending;
    assert.equal(invalidCalls, 1);
  });

  it("retries CSRF_INVALID with jar token when single-flight already advanced it", async () => {
    const jar = new TuiCookieJar();
    jar.replace({ df_session: "sess", df_csrf: "old" });
    let refreshCalls = 0;
    let fetches = 0;
    const transport = new AuthenticatedTransport({
      cookieJar: jar,
      refreshCsrf: async () => {
        refreshCalls += 1;
        jar.replace({ df_session: "sess", df_csrf: "from-refresh" });
      },
      onSessionInvalid: async () => {
        throw new Error("should not invalidate");
      },
      fetchImpl: async (_input, init) => {
        fetches += 1;
        const headers = new Headers(init?.headers);
        if (fetches === 1) {
          // Simulate a peer request refreshing CSRF while this one is in flight.
          jar.replace({ df_session: "sess", df_csrf: "peer-new" });
          return jsonResponse(403, { error: { code: "CSRF_INVALID" } });
        }
        assert.equal(headers.get("x-csrf-token"), "peer-new");
        return jsonResponse(200, { ok: true });
      },
    });

    const response = await transport.fetch("http://127.0.0.1/api/v1/datasources", {
      method: "POST",
      body: "{}",
    });
    assert.equal(response.status, 200);
    assert.equal(refreshCalls, 0);
    assert.equal(fetches, 2);
  });

  it("replays sticky auth-required when listener subscribes after 401", async () => {
    const jar = new TuiCookieJar();
    jar.replace({ df_session: "sess", df_csrf: "csrf" });
    let invalidCalls = 0;
    const transport = new AuthenticatedTransport({
      cookieJar: jar,
      refreshCsrf: async () => {},
      onSessionInvalid: async () => {
        invalidCalls += 1;
      },
      fetchImpl: async () => jsonResponse(401, { error: { code: "UNAUTHORIZED" } }),
    });

    await transport.fetch("http://127.0.0.1/api/v1/run-defaults");
    assert.equal(invalidCalls, 1);

    let lateListenerCalls = 0;
    transport.onAuthRequired(() => {
      lateListenerCalls += 1;
    });
    assert.equal(lateListenerCalls, 1);

    let secondLateCalls = 0;
    transport.onAuthRequired(() => {
      secondLateCalls += 1;
    });
    assert.equal(secondLateCalls, 1);
    assert.equal(lateListenerCalls, 1);
  });

  it("keeps sensitive headers out of thrown error messages", async () => {
    const jar = new TuiCookieJar();
    jar.replace({ df_session: "super-secret", df_csrf: "csrf-secret" });
    const transport = new AuthenticatedTransport({
      cookieJar: jar,
      refreshCsrf: async () => {},
      onSessionInvalid: async () => {},
      fetchImpl: async () => {
        throw new Error("network down");
      },
    });

    await assert.rejects(
      () => transport.fetch("http://127.0.0.1/api/v1/me"),
      (error: unknown) => {
        const message = error instanceof Error ? error.message : String(error);
        assert.equal(message.includes("super-secret"), false);
        assert.equal(message.includes("csrf-secret"), false);
        return true;
      },
    );
  });
});
