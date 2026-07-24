import assert from "node:assert/strict";
import { mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";
import { bindTransportAuthRequired } from "./auth/index.js";
import { TuiSessionStore } from "./auth/session-store.js";
import { runTui } from "./index.js";

describe("runTui auth wiring", () => {
  it("parses --no-auto-login and shares one transport fetch across clients and preflight", async () => {
    const dir = await mkdtemp(join(tmpdir(), "tui-wiring-"));
    const sessionStore = new TuiSessionStore({ filePath: join(dir, "auth.json") });
    await sessionStore.save({
      apiBaseUrl: "http://127.0.0.1:8787",
      cookies: { df_session: "old-sess", df_csrf: "old-csrf" },
      user: { id: "u0", email: "old@example.com" },
      workspace: { id: "w0" },
      expiresAt: "2099-01-01T00:00:00.000Z",
    });
    const fetchCalls: string[] = [];
    const cookieSeen: string[] = [];
    let logoutCookie: string | null = null;

    const fetchImpl: typeof fetch = async (input, init) => {
      const url = String(input);
      const headers = new Headers(init?.headers);
      fetchCalls.push(`${String(init?.method ?? "GET")} ${url}`);
      const cookie = headers.get("cookie");
      if (cookie) {
        cookieSeen.push(`${url} => ${cookie}`);
      }

      if (url.includes("/api/v1/auth/status")) {
        return json(200, {
          success: true,
          data: { publicBaseUrl: "http://127.0.0.1:3000", registrationEnabled: false },
        });
      }
      if (url.includes("/api/v1/auth/login")) {
        return json(
          200,
          {
            success: true,
            data: {
              user: { id: "u1", email: "user@example.com" },
              workspace: { id: "w1" },
              session: { expiresAt: "2099-01-01T00:00:00.000Z" },
            },
          },
          ["df_session=s; Path=/", "df_csrf=c; Path=/"],
        );
      }
      if (url.includes("/api/v1/auth/logout")) {
        logoutCookie = cookie;
        return json(200, { success: true, data: { ok: true } });
      }
      if (url.includes("/healthz")) {
        return new Response("ok", { status: 200 });
      }
      if (url.includes("/api/v1/run-defaults")) {
        assert.match(cookie ?? "", /df_session=/);
        return json(200, {
          success: true,
          data: { activeDatasourceId: "ds-1" },
        });
      }
      return json(404, { success: false, error: { code: "NOT_FOUND", message: "x" } });
    };

    const answers = ["1", "user@example.com", "password"];
    let idx = 0;

    const code = await runTui({
      argv: ["--no-auto-login", "--runtime-url", "http://127.0.0.1:8787/api/copilotkit"],
      fetchImpl,
      sessionStore,
      prompt: {
        question: async () => answers[idx++] ?? "3",
        password: async () => answers[idx++] ?? "",
        close: () => {},
      },
      stdout: { write: () => true } as unknown as NodeJS.WritableStream,
      renderApp: async ({ configClient, client, authController, initialDatasourceId }) => {
        const configFetch = (configClient as unknown as { fetchImpl: typeof fetch }).fetchImpl;
        const clientFetch = (client as unknown as { fetchImpl: typeof fetch }).fetchImpl;
        assert.equal(configFetch, clientFetch);
        assert.equal(typeof authController.logout, "function");
        assert.equal(initialDatasourceId, "ds-1");
        return "exit";
      },
    });

    assert.equal(code, 0);
    assert.ok(fetchCalls.some((call) => call.includes("GET ") && call.includes("/api/v1/auth/status")));
    assert.ok(fetchCalls.some((call) => call.includes("POST ") && call.includes("/api/v1/auth/login")));
    assert.ok(fetchCalls.some((call) => call.includes("POST ") && call.includes("/api/v1/auth/logout")));
    assert.match(logoutCookie ?? "", /df_session=old-sess/);
    assert.ok(fetchCalls.some((call) => call.includes("/api/v1/run-defaults")));
    assert.ok(cookieSeen.some((entry) => entry.includes("/api/v1/run-defaults")));
  });

  it("retries when API is unreachable then continues login", async () => {
    const dir = await mkdtemp(join(tmpdir(), "tui-unreachable-"));
    const sessionStore = new TuiSessionStore({ filePath: join(dir, "auth.json") });
    let statusCalls = 0;
    const answers = ["r", "1", "user@example.com", "password"];
    let idx = 0;

    const code = await runTui({
      argv: ["--no-auto-login", "--runtime-url", "http://127.0.0.1:8787/api/copilotkit"],
      sessionStore,
      fetchImpl: async (input, init) => {
        const url = String(input);
        if (url.includes("/api/v1/auth/status")) {
          statusCalls += 1;
          if (statusCalls === 1) {
            throw new TypeError("fetch failed");
          }
          return json(200, {
            success: true,
            data: { publicBaseUrl: "http://127.0.0.1:3000", registrationEnabled: false },
          });
        }
        if (url.includes("/api/v1/auth/login")) {
          return json(
            200,
            {
              success: true,
              data: {
                user: { id: "u1", email: "user@example.com" },
                workspace: { id: "w1" },
                session: { expiresAt: "2099-01-01T00:00:00.000Z" },
              },
            },
            ["df_session=s; Path=/", "df_csrf=c; Path=/"],
          );
        }
        if (url.includes("/healthz")) {
          return new Response("ok", { status: 200 });
        }
        if (url.includes("/api/v1/run-defaults")) {
          return json(200, { success: true, data: { activeDatasourceId: "ds-1" } });
        }
        return json(404, { success: false, error: { code: "NOT_FOUND", message: "x" } });
      },
      prompt: {
        question: async () => answers[idx++] ?? "q",
        password: async () => answers[idx++] ?? "",
        close: () => {},
      },
      stdout: { write: () => true } as unknown as NodeJS.WritableStream,
      renderApp: async () => "exit",
    });

    assert.equal(code, 0);
    assert.equal(statusCalls, 2);
  });

  it("switches runtime URL from recovery menu without restarting", async () => {
    const dir = await mkdtemp(join(tmpdir(), "tui-other-url-"));
    const sessionStore = new TuiSessionStore({ filePath: join(dir, "auth.json") });
    const statusHosts: string[] = [];
    const answers = ["o", "http://127.0.0.1:9999/api/copilotkit", "1", "user@example.com", "password"];
    let idx = 0;

    const code = await runTui({
      argv: ["--no-auto-login", "--runtime-url", "http://127.0.0.1:8787/api/copilotkit"],
      sessionStore,
      fetchImpl: async (input) => {
        const url = String(input);
        if (url.includes("/api/v1/auth/status")) {
          statusHosts.push(new URL(url).host);
          if (url.includes("127.0.0.1:8787")) {
            throw new TypeError("fetch failed");
          }
          return json(200, {
            success: true,
            data: { publicBaseUrl: "http://127.0.0.1:3000", registrationEnabled: false },
          });
        }
        if (url.includes("/api/v1/auth/login")) {
          assert.match(url, /127\.0\.0\.1:9999/);
          return json(
            200,
            {
              success: true,
              data: {
                user: { id: "u1", email: "user@example.com" },
                workspace: { id: "w1" },
                session: { expiresAt: "2099-01-01T00:00:00.000Z" },
              },
            },
            ["df_session=s; Path=/", "df_csrf=c; Path=/"],
          );
        }
        if (url.includes("/healthz")) {
          assert.match(url, /127\.0\.0\.1:9999/);
          return new Response("ok", { status: 200 });
        }
        if (url.includes("/api/v1/run-defaults")) {
          assert.match(url, /127\.0\.0\.1:9999/);
          return json(200, { success: true, data: { activeDatasourceId: "ds-9" } });
        }
        return json(404, { success: false, error: { code: "NOT_FOUND", message: "x" } });
      },
      prompt: {
        question: async () => answers[idx++] ?? "q",
        password: async () => answers[idx++] ?? "",
        close: () => {},
      },
      stdout: { write: () => true } as unknown as NodeJS.WritableStream,
      renderApp: async ({ initialDatasourceId }) => {
        assert.equal(initialDatasourceId, "ds-9");
        return "exit";
      },
    });

    assert.equal(code, 0);
    assert.deepEqual(statusHosts, ["127.0.0.1:8787", "127.0.0.1:9999"]);
  });

  it("replays preflight 401 via sticky auth-required after App binds", async () => {
    const dir = await mkdtemp(join(tmpdir(), "tui-preflight-401-"));
    const sessionStore = new TuiSessionStore({ filePath: join(dir, "auth.json") });
    await sessionStore.save({
      apiBaseUrl: "http://127.0.0.1:8787",
      cookies: { df_session: "sess", df_csrf: "csrf" },
      user: { id: "u0", email: "cached@example.com" },
      workspace: { id: "w0" },
      expiresAt: "2099-01-01T00:00:00.000Z",
    });

    let meCalls = 0;
    let runDefaultsCalls = 0;
    let appStarts = 0;
    const answers = ["1", "fresh@example.com", "password"];
    let idx = 0;

    const code = await runTui({
      argv: ["--runtime-url", "http://127.0.0.1:8787/api/copilotkit"],
      sessionStore,
      fetchImpl: async (input) => {
        const url = String(input);
        if (url.includes("/api/v1/auth/status")) {
          return json(200, {
            success: true,
            data: { publicBaseUrl: "http://127.0.0.1:3000", registrationEnabled: false },
          });
        }
        if (url.includes("/api/v1/me")) {
          meCalls += 1;
          return json(200, {
            success: true,
            data: {
              user: { id: "u0", email: "cached@example.com" },
              workspace: { id: "w0" },
            },
          });
        }
        if (url.includes("/api/v1/auth/login")) {
          return json(
            200,
            {
              success: true,
              data: {
                user: { id: "u1", email: "fresh@example.com" },
                workspace: { id: "w1" },
                session: { expiresAt: "2099-01-01T00:00:00.000Z" },
              },
            },
            ["df_session=fresh; Path=/", "df_csrf=fresh-c; Path=/"],
          );
        }
        if (url.includes("/healthz")) {
          return new Response("ok", { status: 200 });
        }
        if (url.includes("/api/v1/run-defaults")) {
          runDefaultsCalls += 1;
          if (runDefaultsCalls === 1) {
            return json(401, { error: { code: "UNAUTHORIZED" } });
          }
          return json(200, { success: true, data: { activeDatasourceId: "ds-1" } });
        }
        return json(404, { success: false, error: { code: "NOT_FOUND", message: "x" } });
      },
      prompt: {
        question: async () => answers[idx++] ?? "3",
        password: async () => answers[idx++] ?? "",
        close: () => {},
      },
      stdout: { write: () => true } as unknown as NodeJS.WritableStream,
      renderApp: async ({ transport }) => {
        appStarts += 1;
        assert.ok(transport);
        let exitReason: "exit" | "auth-required" = "exit";
        const unbind = bindTransportAuthRequired(transport, () => {
          exitReason = "auth-required";
        });
        unbind();
        if (appStarts === 1) {
          assert.equal(exitReason, "auth-required");
          return "auth-required";
        }
        assert.equal(exitReason, "exit");
        return "exit";
      },
    });

    assert.equal(code, 0);
    assert.equal(appStarts, 2);
    assert.ok(meCalls >= 1);
    assert.ok(runDefaultsCalls >= 2);
  });

  it("re-enters login when renderApp exits with auth-required", async () => {
    const dir = await mkdtemp(join(tmpdir(), "tui-auth-required-"));
    const sessionStore = new TuiSessionStore({ filePath: join(dir, "auth.json") });
    const answers = ["1", "a@example.com", "pw1", "1", "b@example.com", "pw2"];
    let idx = 0;
    let appStarts = 0;

    const code = await runTui({
      argv: ["--no-auto-login", "--runtime-url", "http://127.0.0.1:8787/api/copilotkit"],
      sessionStore,
      fetchImpl: async (input) => {
        const url = String(input);
        if (url.includes("/api/v1/auth/status")) {
          return json(200, {
            success: true,
            data: { publicBaseUrl: "http://127.0.0.1:3000", registrationEnabled: false },
          });
        }
        if (url.includes("/api/v1/auth/login")) {
          return json(
            200,
            {
              success: true,
              data: {
                user: { id: "u1", email: "user@example.com" },
                workspace: { id: "w1" },
                session: { expiresAt: "2099-01-01T00:00:00.000Z" },
              },
            },
            ["df_session=s; Path=/", "df_csrf=c; Path=/"],
          );
        }
        if (url.includes("/api/v1/auth/logout")) {
          return json(200, { success: true, data: { ok: true } });
        }
        if (url.includes("/healthz")) {
          return new Response("ok", { status: 200 });
        }
        if (url.includes("/api/v1/run-defaults")) {
          return json(200, { success: true, data: {} });
        }
        return json(404, { success: false, error: { code: "NOT_FOUND", message: "x" } });
      },
      prompt: {
        question: async () => answers[idx++] ?? "3",
        password: async () => answers[idx++] ?? "",
        close: () => {},
      },
      stdout: { write: () => true } as unknown as NodeJS.WritableStream,
      renderApp: async () => {
        appStarts += 1;
        return appStarts === 1 ? "auth-required" : "exit";
      },
    });

    assert.equal(code, 0);
    assert.equal(appStarts, 2);
  });

  it("rejects removed offline demo mode", async () => {
    const code = await runTui({ argv: [`--${"demo"}`] });
    assert.equal(code, 1);
  });
});

function json(status: number, body: unknown, setCookies: string[] = []): Response {
  const headers = new Headers({ "content-type": "application/json" });
  for (const cookie of setCookies) {
    headers.append("set-cookie", cookie);
  }
  return new Response(JSON.stringify(body), { status, headers });
}
