import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { assertSafeAuthRedirect, TuiAuthClient, TuiAuthError } from "./auth-client.js";
import { TuiCookieJar } from "./cookie-jar.js";

describe("TuiAuthClient", () => {
  it("calls status/login/me/csrf/logout with expected URLs and methods", async () => {
    const jar = new TuiCookieJar();
    const calls: Array<{ url: string; method: string; body?: unknown }> = [];
    const client = new TuiAuthClient({
      apiBaseUrl: "http://127.0.0.1:8787",
      cookieJar: jar,
      fetchImpl: async (input, init) => {
        const url = String(input);
        const method = String(init?.method ?? "GET");
        const body = init?.body ? JSON.parse(String(init.body)) : undefined;
        calls.push({ url, method, body });
        if (url.endsWith("/api/v1/auth/status")) {
          return json(200, {
            success: true,
            data: { publicBaseUrl: "http://127.0.0.1:3000", registrationEnabled: true },
          });
        }
        if (url.endsWith("/api/v1/auth/login")) {
          return json(
            200,
            {
              success: true,
              data: {
                user: { id: "u1", email: "a@example.com" },
                workspace: { id: "w1" },
                session: { expiresAt: "2099-01-01T00:00:00.000Z" },
              },
            },
            [
              "df_session=sess; Path=/; HttpOnly",
              "df_csrf=csrf; Path=/",
            ],
          );
        }
        if (url.endsWith("/api/v1/me")) {
          return json(200, {
            success: true,
            data: { user: { id: "u1", email: "a@example.com" }, workspace: { id: "w1" } },
          });
        }
        if (url.endsWith("/api/v1/auth/csrf/refresh")) {
          return json(200, { success: true, data: { csrfToken: "new" } }, ["df_csrf=new; Path=/"]);
        }
        if (url.endsWith("/api/v1/auth/logout")) {
          return json(200, { success: true, data: { ok: true } });
        }
        return json(404, { success: false, error: { code: "NOT_FOUND", message: "missing" } });
      },
    });

    await client.getStatus();
    await client.login("a@example.com", "password");
    await client.me();
    await client.refreshCsrf();
    await client.logout();

    assert.deepEqual(
      calls.map((call) => ({ method: call.method, path: new URL(call.url).pathname })),
      [
        { method: "GET", path: "/api/v1/auth/status" },
        { method: "POST", path: "/api/v1/auth/login" },
        { method: "GET", path: "/api/v1/me" },
        { method: "POST", path: "/api/v1/auth/csrf/refresh" },
        { method: "POST", path: "/api/v1/auth/logout" },
      ],
    );
    assert.equal((calls[1]?.body as { client?: string } | undefined)?.client, "tui");
    assert.equal(
      calls.some((call) => call.url.includes("/api/v1/auth/me")),
      false,
    );
  });

  it("requires session.expiresAt from login response", async () => {
    const client = new TuiAuthClient({
      apiBaseUrl: "http://127.0.0.1:8787",
      cookieJar: new TuiCookieJar(),
      fetchImpl: async () =>
        json(200, {
          success: true,
          data: {
            user: { id: "u1", email: "a@example.com" },
            workspace: { id: "w1" },
            session: {},
          },
        }),
    });

    await assert.rejects(
      () => client.login("a@example.com", "password"),
      (error: unknown) => error instanceof TuiAuthError && error.code === "INVALID_LOGIN_RESPONSE",
    );
  });

  it("times out hung auth requests", async () => {
    const client = new TuiAuthClient({
      apiBaseUrl: "http://127.0.0.1:8787",
      cookieJar: new TuiCookieJar(),
      timeoutMs: 20,
      fetchImpl: async (_input, init) =>
        new Promise((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => {
            const error = new Error("aborted");
            error.name = "AbortError";
            reject(error);
          });
        }),
    });

    await assert.rejects(
      () => client.getStatus(),
      (error: unknown) => error instanceof TuiAuthError && error.code === "TIMEOUT",
    );
  });

  it("maps transport failures to NETWORK_ERROR", async () => {
    const client = new TuiAuthClient({
      apiBaseUrl: "http://127.0.0.1:8787",
      cookieJar: new TuiCookieJar(),
      fetchImpl: async () => {
        throw new TypeError("fetch failed");
      },
    });

    await assert.rejects(
      () => client.getStatus(),
      (error: unknown) => error instanceof TuiAuthError && error.code === "NETWORK_ERROR",
    );
  });

  it("uses redirect:manual and rejects HTTPS→HTTP auth redirects", async () => {
    const seen: Array<{ redirect?: RequestRedirect; url: string }> = [];
    const client = new TuiAuthClient({
      apiBaseUrl: "https://api.example.com",
      cookieJar: new TuiCookieJar(),
      fetchImpl: async (input, init) => {
        seen.push({
          url: String(input),
          ...(init?.redirect ? { redirect: init.redirect } : {}),
        });
        return new Response(null, {
          status: 308,
          headers: { location: "http://api.example.com/api/v1/auth/login" },
        });
      },
    });

    await assert.rejects(
      () => client.login("a@example.com", "password"),
      (error: unknown) =>
        error instanceof TuiAuthError
        && error.code === "UNSAFE_REDIRECT"
        && /HTTPS to HTTP/i.test(error.message),
    );
    assert.equal(seen[0]?.redirect, "manual");
    assert.equal(seen.length, 1);
  });

  it("rejects cross-origin auth redirects and does not forward the password body", async () => {
    let bodies = 0;
    const client = new TuiAuthClient({
      apiBaseUrl: "https://api.example.com",
      cookieJar: new TuiCookieJar(),
      fetchImpl: async (_input, init) => {
        if (init?.body) {
          bodies += 1;
        }
        return new Response(null, {
          status: 307,
          headers: { location: "https://evil.example.com/api/v1/auth/login" },
        });
      },
    });

    await assert.rejects(
      () => client.login("a@example.com", "password"),
      (error: unknown) =>
        error instanceof TuiAuthError
        && error.code === "UNSAFE_REDIRECT"
        && /cross-origin/i.test(error.message),
    );
    assert.equal(bodies, 1);
  });

  it("follows same-origin 307 and keeps timeout armed through body parse", async () => {
    let fetches = 0;
    let bodyReadStarted = false;
    let abortedDuringBody = false;
    const client = new TuiAuthClient({
      apiBaseUrl: "http://127.0.0.1:8787",
      cookieJar: new TuiCookieJar(),
      timeoutMs: 40,
      fetchImpl: async (input, init) => {
        fetches += 1;
        assert.equal(init?.redirect, "manual");
        if (fetches === 1) {
          return new Response(null, {
            status: 307,
            headers: { location: "http://127.0.0.1:8787/api/v1/auth/status" },
          });
        }
        const payload = JSON.stringify({
          success: true,
          data: { publicBaseUrl: "http://127.0.0.1:3000", registrationEnabled: false },
        });
        return new Response(
          new ReadableStream({
            start(controller) {
              bodyReadStarted = true;
              const timer = setTimeout(() => {
                controller.enqueue(new TextEncoder().encode(payload));
                controller.close();
              }, 80);
              init?.signal?.addEventListener("abort", () => {
                abortedDuringBody = true;
                clearTimeout(timer);
                try {
                  controller.error(Object.assign(new Error("aborted"), { name: "AbortError" }));
                } catch {
                  // already closed
                }
              });
            },
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        );
      },
    });

    await assert.rejects(
      () => client.getStatus(),
      (error: unknown) => error instanceof TuiAuthError && error.code === "TIMEOUT",
    );
    assert.equal(fetches, 2);
    assert.equal(bodyReadStarted, true);
    assert.equal(abortedDuringBody, true);
  });
});

describe("assertSafeAuthRedirect", () => {
  it("allows same-origin HTTPS hops and rejects downgrade/cross-origin", () => {
    assert.equal(
      assertSafeAuthRedirect(
        "https://api.example.com/api/v1/auth/login",
        "/api/v1/auth/login?next=1",
      ),
      "https://api.example.com/api/v1/auth/login?next=1",
    );
    assert.throws(
      () => assertSafeAuthRedirect(
        "https://api.example.com/login",
        "http://api.example.com/login",
      ),
      (error: unknown) => error instanceof TuiAuthError && error.code === "UNSAFE_REDIRECT",
    );
    assert.throws(
      () => assertSafeAuthRedirect(
        "https://api.example.com/login",
        "https://other.example.com/login",
      ),
      (error: unknown) => error instanceof TuiAuthError && error.code === "UNSAFE_REDIRECT",
    );
  });
});

function json(status: number, body: unknown, setCookies: string[] = []): Response {
  const headers = new Headers({ "content-type": "application/json" });
  for (const cookie of setCookies) {
    headers.append("set-cookie", cookie);
  }
  return new Response(JSON.stringify(body), { status, headers });
}
