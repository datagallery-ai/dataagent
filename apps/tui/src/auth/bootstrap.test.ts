import assert from "node:assert/strict";
import { mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";
import {
  bootstrapTuiAuth,
  completeInteractiveLogin,
  createAuthController,
  SESSION_EXPIRY_TOLERANCE_MS,
} from "./bootstrap.js";
import { TuiAuthClient } from "./auth-client.js";
import { TuiCookieJar } from "./cookie-jar.js";
import { TuiSessionStore } from "./session-store.js";
import type { StoredTuiSession } from "./types.js";

function statusOk(): Response {
  return json(200, {
    success: true,
    data: { publicBaseUrl: "http://127.0.0.1:3000", registrationEnabled: true },
  });
}

describe("bootstrapTuiAuth", () => {
  it("restores cached session via GET /api/v1/me", async () => {
    const dir = await mkdtemp(join(tmpdir(), "tui-bootstrap-"));
    const store = new TuiSessionStore({ filePath: join(dir, "tui-auth.json") });
    const session = sampleSession("http://127.0.0.1:8787");
    await store.save(session);
    const paths: string[] = [];

    const result = await bootstrapTuiAuth({
      apiBaseUrl: "http://127.0.0.1:8787",
      sessionStore: store,
      fetchImpl: async (input) => {
        const path = new URL(String(input)).pathname;
        paths.push(path);
        if (path === "/api/v1/auth/status") return statusOk();
        if (path === "/api/v1/me") {
          return json(200, {
            success: true,
            data: {
              user: { id: "u1", email: "a@example.com", displayName: "A" },
              workspace: { id: "w1", name: "Personal" },
            },
          });
        }
        return json(404, { success: false, error: { code: "NOT_FOUND", message: "x" } });
      },
    });

    assert.equal(result.kind, "authenticated");
    assert.deepEqual(paths, ["/api/v1/auth/status", "/api/v1/me"]);
  });

  it("skips /me only when expiresAt is beyond tolerance", async () => {
    const dir = await mkdtemp(join(tmpdir(), "tui-bootstrap-exp-"));
    const store = new TuiSessionStore({ filePath: join(dir, "tui-auth.json") });
    const now = Date.UTC(2026, 0, 1, 12, 0, 0);
    await store.save({
      ...sampleSession("http://127.0.0.1:8787"),
      expiresAt: new Date(now - SESSION_EXPIRY_TOLERANCE_MS - 1).toISOString(),
    });
    const paths: string[] = [];

    const result = await bootstrapTuiAuth({
      apiBaseUrl: "http://127.0.0.1:8787",
      sessionStore: store,
      now: () => now,
      fetchImpl: async (input) => {
        paths.push(new URL(String(input)).pathname);
        return statusOk();
      },
    });

    assert.equal(result.kind, "login-required");
    assert.deepEqual(paths, ["/api/v1/auth/status"]);
  });

  it("still calls /me when expiresAt is within tolerance", async () => {
    const dir = await mkdtemp(join(tmpdir(), "tui-bootstrap-tol-"));
    const store = new TuiSessionStore({ filePath: join(dir, "tui-auth.json") });
    const now = Date.UTC(2026, 0, 1, 12, 0, 0);
    await store.save({
      ...sampleSession("http://127.0.0.1:8787"),
      expiresAt: new Date(now - SESSION_EXPIRY_TOLERANCE_MS + 1_000).toISOString(),
    });
    const paths: string[] = [];

    await bootstrapTuiAuth({
      apiBaseUrl: "http://127.0.0.1:8787",
      sessionStore: store,
      now: () => now,
      fetchImpl: async (input) => {
        const path = new URL(String(input)).pathname;
        paths.push(path);
        if (path === "/api/v1/auth/status") return statusOk();
        return json(401, { success: false, error: { code: "UNAUTHORIZED", message: "gone" } });
      },
    });

    assert.ok(paths.includes("/api/v1/me"));
  });

  it("ignores cache restore when --no-auto-login is set but keeps previousSession", async () => {
    const dir = await mkdtemp(join(tmpdir(), "tui-bootstrap-noauto-"));
    const store = new TuiSessionStore({ filePath: join(dir, "tui-auth.json") });
    const cached = sampleSession("http://127.0.0.1:8787");
    await store.save(cached);
    const paths: string[] = [];

    const result = await bootstrapTuiAuth({
      apiBaseUrl: "http://127.0.0.1:8787",
      noAutoLogin: true,
      sessionStore: store,
      fetchImpl: async (input) => {
        paths.push(new URL(String(input)).pathname);
        return statusOk();
      },
    });

    assert.equal(result.kind, "login-required");
    assert.deepEqual(paths, ["/api/v1/auth/status"]);
    if (result.kind === "login-required") {
      assert.equal(result.previousSession?.user.email, cached.user.email);
      assert.equal(result.previousSession?.cookies.df_session, "sess");
    }
  });
});

describe("completeInteractiveLogin", () => {
  it("does not overwrite cache when login fails", async () => {
    const dir = await mkdtemp(join(tmpdir(), "tui-login-fail-"));
    const store = new TuiSessionStore({ filePath: join(dir, "tui-auth.json") });
    const previous = sampleSession("http://127.0.0.1:8787", "old@example.com");
    await store.save(previous);

    await assert.rejects(() =>
      completeInteractiveLogin({
        apiBaseUrl: "http://127.0.0.1:8787",
        email: "new@example.com",
        password: "bad",
        fetchImpl: async () =>
          json(401, {
            success: false,
            error: { code: "UNAUTHORIZED", message: "Invalid email or password." },
          }),
        sessionStore: store,
        previousSession: previous,
      }));

    const still = await store.load("http://127.0.0.1:8787");
    assert.equal(still?.user.email, "old@example.com");
  });

  it("best-effort logs out previous session then saves the new one", async () => {
    const dir = await mkdtemp(join(tmpdir(), "tui-login-ok-"));
    const store = new TuiSessionStore({ filePath: join(dir, "tui-auth.json") });
    const previous = sampleSession("http://127.0.0.1:8787", "old@example.com");
    previous.cookies = { df_session: "old-sess", df_csrf: "old-csrf" };
    await store.save(previous);
    const paths: string[] = [];

    const result = await completeInteractiveLogin({
      apiBaseUrl: "http://127.0.0.1:8787",
      email: "new@example.com",
      password: "good-password",
      sessionStore: store,
      previousSession: previous,
      fetchImpl: async (input, init) => {
        const path = new URL(String(input)).pathname;
        paths.push(`${String(init?.method ?? "GET")} ${path}`);
        if (path === "/api/v1/auth/login") {
          return json(
            200,
            {
              success: true,
              data: {
                user: { id: "u2", email: "new@example.com" },
                workspace: { id: "w2" },
                session: { expiresAt: "2099-01-01T00:00:00.000Z" },
              },
            },
            ["df_session=new-sess; Path=/", "df_csrf=new-csrf; Path=/"],
          );
        }
        if (path === "/api/v1/auth/logout") {
          return json(200, { success: true, data: { ok: true } });
        }
        return json(404, { success: false, error: { code: "NOT_FOUND", message: "x" } });
      },
    });

    assert.equal(result.session.user.email, "new@example.com");
    assert.ok(paths.includes("POST /api/v1/auth/logout"));
    const saved = await store.load("http://127.0.0.1:8787");
    assert.equal(saved?.user.email, "new@example.com");
  });

  it("keeps new account and warns when old logout fails", async () => {
    const dir = await mkdtemp(join(tmpdir(), "tui-login-warn-"));
    const store = new TuiSessionStore({ filePath: join(dir, "tui-auth.json") });
    const previous = sampleSession("http://127.0.0.1:8787", "old@example.com");
    previous.cookies = { df_session: "old-sess", df_csrf: "old-csrf" };

    const result = await completeInteractiveLogin({
      apiBaseUrl: "http://127.0.0.1:8787",
      email: "new@example.com",
      password: "good-password",
      sessionStore: store,
      previousSession: previous,
      fetchImpl: async (input) => {
        const path = new URL(String(input)).pathname;
        if (path === "/api/v1/auth/login") {
          return json(
            200,
            {
              success: true,
              data: {
                user: { id: "u2", email: "new@example.com" },
                workspace: { id: "w2" },
                session: { expiresAt: "2099-01-01T00:00:00.000Z" },
              },
            },
            ["df_session=new-sess; Path=/", "df_csrf=new-csrf; Path=/"],
          );
        }
        return json(503, { success: false, error: { code: "UNAVAILABLE", message: "down" } });
      },
    });

    assert.equal(result.session.user.email, "new@example.com");
    assert.match(result.warning ?? "", /previous remote session/i);
  });
});

describe("createAuthController", () => {
  it("distinguishes remote logout failure from local cleanup failure", async () => {
    const dir = await mkdtemp(join(tmpdir(), "tui-logout-"));
    const store = new TuiSessionStore({ filePath: join(dir, "tui-auth.json") });
    await store.save(sampleSession("http://127.0.0.1:8787"));

    const remoteFailed = createAuthController({
      apiBaseUrl: "http://127.0.0.1:8787",
      sessionStore: store,
      authClient: new TuiAuthClient({
        apiBaseUrl: "http://127.0.0.1:8787",
        cookieJar: new TuiCookieJar(),
        fetchImpl: async () => json(503, { success: false, error: { code: "DOWN", message: "x" } }),
      }),
    });
    const remote = await remoteFailed.logout();
    assert.equal(remote.kind, "remote-failed");

    const jar = new TuiCookieJar();
    jar.replace({ df_session: "sess", df_csrf: "csrf" });
    const localFailed = createAuthController({
      apiBaseUrl: "http://127.0.0.1:8787",
      sessionStore: {
        path: store.path,
        async remove() {
          throw new Error("disk full");
        },
      } as unknown as TuiSessionStore,
      authClient: new TuiAuthClient({
        apiBaseUrl: "http://127.0.0.1:8787",
        cookieJar: jar,
        fetchImpl: async () => json(200, { success: true, data: { ok: true } }),
      }),
    });
    const local = await localFailed.logout();
    assert.equal(local.kind, "local-cleanup-failed");
    if (local.kind === "local-cleanup-failed") {
      assert.equal(local.storePath, store.path);
      assert.match(local.error, /disk full/i);
    }
  });
});

function sampleSession(apiBaseUrl: string, email = "a@example.com"): StoredTuiSession {
  return {
    apiBaseUrl,
    cookies: { df_session: "sess", df_csrf: "csrf" },
    user: { id: "u1", email },
    workspace: { id: "w1" },
    expiresAt: "2099-01-01T00:00:00.000Z",
  };
}

function json(status: number, body: unknown, setCookies: string[] = []): Response {
  const headers = new Headers({ "content-type": "application/json" });
  for (const cookie of setCookies) {
    headers.append("set-cookie", cookie);
  }
  return new Response(JSON.stringify(body), { status, headers });
}
