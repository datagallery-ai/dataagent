import assert from "node:assert/strict";
import test from "node:test";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readdirSync, readFileSync, statSync } from "node:fs";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { randomUUID } from "node:crypto";

import { createServer as createApiServer } from "../apps/api/dist/server.js";
import { createMetadataStore } from "../packages/metadata/dist/index.js";
import {
  loadPasswordAuthConfig,
  validateAuthPublicUrl
} from "../apps/api/dist/auth/config.js";
import {
  appendAuthCookies,
  appendClearAuthCookies
} from "../apps/api/dist/auth/cookies.js";

const SCRIPTS_ROOT = dirname(fileURLToPath(import.meta.url));

const FORMAL_HTTP_AUTH_TARGETS = [
  "run-dacomp6-complex-case.mjs",
  "seed-dtc-growth-demo.mjs",
  "seed-local-fixtures.mjs",
  "smoke-agent-protocol-deepseek.mjs",
  "smoke-ask-user-interrupt.mjs",
  "smoke-auth.mjs",
  "smoke-config-api.mjs",
  "smoke-copilotkit-run.mjs",
  "smoke-copilotkit.mjs",
  "smoke-interaction-run-id.mjs",
  "smoke-password-frontend-isolation.mjs",
  "smoke-server-datasources-e2e.mjs",
  "test-builtin-dtc-growth-datasource.mjs",
  "verify-token-usage-display.mjs"
];

const PUBLIC_HTTP_TARGETS = [
  "deploy/health.mjs",
  "deploy/smoke-native-deploy.mjs"
];

const DIRECT_METADATA_FIXTURE_TARGETS = [];

const AUTH_FOUNDATION_HARNESS_TARGETS = [
  "auth-foundation.test.mjs",
  "lib/authenticated-test-client.test.mjs"
];

const FORBIDDEN_DEV_AUTH_PATTERNS = [
  /X-Dev-Token/i,
  /\bdev-token\b/,
  /Authorization\s*:\s*["']?Bearer\s+dev\b/i
];

function listMjsFiles(dir) {
  /** @type {string[]} */
  const out = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    const st = statSync(full);
    if (st.isDirectory()) {
      out.push(...listMjsFiles(full));
    } else if (entry.endsWith(".mjs")) {
      out.push(full);
    }
  }
  return out;
}

function isHttpScript(source) {
  return /\bfetch\s*\(|\bhttps?\.request\s*\(/.test(source);
}

const SECRET = "auth-foundation-session-secret-32b!";

function baseEnv(overrides = {}) {
  return {
    DATAFOUNDRY_AUTH_MODE: "password",
    AUTH_SESSION_SECRET: SECRET,
    AUTH_PUBLIC_BASE_URL: "http://127.0.0.1:3000",
    AUTH_EMAIL_DELIVERY: "test",
    AUTH_REGISTRATION_MODE: "open",
    ...overrides
  };
}

test("validateAuthPublicUrl allows loopback HTTP without secure cookies", () => {
  for (const raw of [
    "http://127.0.0.1:3000",
    "http://localhost:3000",
    "http://[::1]:3000"
  ]) {
    const result = validateAuthPublicUrl(raw);
    assert.equal(result.loopback, true);
    assert.equal(result.cookieSecure, false);
    assert.ok(result.publicBaseUrl.startsWith("http://"));
  }
});

test("validateAuthPublicUrl allows HTTPS with secure cookies and path", () => {
  const result = validateAuthPublicUrl("https://example.com/datafoundry");
  assert.equal(result.loopback, false);
  assert.equal(result.cookieSecure, true);
  assert.equal(result.publicBaseUrl, "https://example.com/datafoundry");
  assert.equal(result.cookiePath, "/datafoundry");
});

test("validateAuthPublicUrl uses root cookie path for origin-only URLs", () => {
  const result = validateAuthPublicUrl("https://example.com");
  assert.equal(result.cookiePath, "/");
  assert.equal(result.publicBaseUrl, "https://example.com");
});

test("validateAuthPublicUrl rejects non-loopback HTTP", () => {
  assert.throws(
    () => validateAuthPublicUrl("http://192.168.1.10:3000"),
    /AUTH_PUBLIC_BASE_URL|loopback|HTTPS/i
  );
});

test("validateAuthPublicUrl rejects illegal URL shapes", () => {
  assert.throws(() => validateAuthPublicUrl("ftp://localhost:3000"));
  assert.throws(() => validateAuthPublicUrl("http://user:pass@localhost:3000"));
  assert.throws(() => validateAuthPublicUrl("http://localhost:3000#frag"));
});

test("password config matrix", () => {
  const cases = [
    {
      name: "loopback 127 with test mail",
      env: baseEnv({ AUTH_PUBLIC_BASE_URL: "http://127.0.0.1:3000", AUTH_EMAIL_DELIVERY: "test" }),
      ok: true,
      cookieSecure: false
    },
    {
      name: "loopback localhost with test mail",
      env: baseEnv({ AUTH_PUBLIC_BASE_URL: "http://localhost:3000", AUTH_EMAIL_DELIVERY: "test" }),
      ok: true,
      cookieSecure: false
    },
    {
      name: "loopback ipv6 with test mail",
      env: baseEnv({ AUTH_PUBLIC_BASE_URL: "http://[::1]:3000", AUTH_EMAIL_DELIVERY: "test" }),
      ok: true,
      cookieSecure: false
    },
    {
      name: "lan http rejected",
      env: baseEnv({ AUTH_PUBLIC_BASE_URL: "http://192.168.1.10:3000", AUTH_EMAIL_DELIVERY: "test" }),
      ok: false
    },
    {
      name: "https smtp allowed",
      env: baseEnv({
        AUTH_PUBLIC_BASE_URL: "https://example.com/datafoundry",
        AUTH_EMAIL_DELIVERY: "smtp",
        SMTP_HOST: "smtp.example.com",
        SMTP_FROM: "noreply@example.com"
      }),
      ok: true,
      cookieSecure: true,
      cookiePath: "/datafoundry"
    },
    {
      name: "https test mail rejected",
      env: baseEnv({
        AUTH_PUBLIC_BASE_URL: "https://example.com",
        AUTH_EMAIL_DELIVERY: "test"
      }),
      ok: false
    },
    {
      name: "invalid registration mode rejected",
      env: baseEnv({ AUTH_REGISTRATION_MODE: "maybe" }),
      ok: false
    },
    {
      name: "missing registration mode rejected in password mode",
      env: baseEnv({ AUTH_REGISTRATION_MODE: "" }),
      ok: false
    },
    {
      name: "invalid email delivery rejected",
      env: baseEnv({ AUTH_EMAIL_DELIVERY: "console" }),
      ok: false
    }
  ];

  for (const item of cases) {
    if (item.ok) {
      const config = loadPasswordAuthConfig(item.env);
      assert.equal(config.cookieSecure, item.cookieSecure, item.name);
      assert.equal(config.cookiePath, item.cookiePath ?? "/", item.name);
      assert.ok(config.registrationMode === "open" || config.registrationMode === "closed", item.name);
    } else {
      assert.throws(() => loadPasswordAuthConfig(item.env), Error, item.name);
    }
  }
});

async function captureSetCookie(write) {
  const server = createServer((req, res) => {
    write(res);
    res.end("ok");
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  assert(address && typeof address === "object");
  try {
    const response = await fetch(`http://127.0.0.1:${address.port}/`);
    return response.headers.getSetCookie();
  } finally {
    await new Promise((resolve, reject) => {
      server.close((error) => (error ? reject(error) : resolve()));
    });
  }
}

test("appendAuthCookies and clear use the same secure flag", async () => {
  const secureCookies = await captureSetCookie((res) => {
    appendAuthCookies(res, {
      sessionToken: "session",
      csrfToken: "csrf",
      maxAgeSeconds: 60,
      path: "/",
      secure: true
    });
  });
  assert.ok(secureCookies.every((cookie) => /;\s*Secure/i.test(cookie)));

  const clearSecure = await captureSetCookie((res) => {
    appendClearAuthCookies(res, { path: "/", secure: true });
  });
  assert.ok(clearSecure.every((cookie) => /;\s*Secure/i.test(cookie)));

  const insecureCookies = await captureSetCookie((res) => {
    appendAuthCookies(res, {
      sessionToken: "session",
      csrfToken: "csrf",
      maxAgeSeconds: 60,
      path: "/",
      secure: false
    });
  });
  assert.ok(insecureCookies.every((cookie) => !/;\s*Secure/i.test(cookie)));

  const clearInsecure = await captureSetCookie((res) => {
    appendClearAuthCookies(res, { path: "/", secure: false });
  });
  assert.ok(clearInsecure.every((cookie) => !/;\s*Secure/i.test(cookie)));
});

test("cookie helpers derive Path from deployment prefix", async () => {
  const cookies = await captureSetCookie((res) => {
    appendAuthCookies(res, {
      sessionToken: "session",
      csrfToken: "csrf",
      maxAgeSeconds: 60,
      path: "/datafoundry",
      secure: true
    });
  });
  assert.ok(cookies.every((cookie) => /;\s*Path=\/datafoundry(?:;|$)/i.test(cookie)));

  const cleared = await captureSetCookie((res) => {
    appendClearAuthCookies(res, { path: "/datafoundry", secure: true });
  });
  assert.ok(cleared.every((cookie) => /;\s*Path=\/datafoundry(?:;|$)/i.test(cookie)));
});

test("cookie helpers no longer read NODE_ENV for Secure", async () => {
  const previous = process.env.NODE_ENV;
  process.env.NODE_ENV = "production";
  try {
    const cookies = await captureSetCookie((res) => {
      appendAuthCookies(res, {
        sessionToken: "session",
        csrfToken: "csrf",
        maxAgeSeconds: 60,
        path: "/",
        secure: false
      });
    });
    assert.ok(cookies.every((cookie) => !/;\s*Secure/i.test(cookie)));
  } finally {
    if (previous === undefined) {
      delete process.env.NODE_ENV;
    } else {
      process.env.NODE_ENV = previous;
    }
  }
});

async function withPasswordApi(envOverrides, run) {
  const root = mkdtempSync(join(tmpdir(), "datafoundry-auth-foundation-"));
  const previous = { ...process.env };
  Object.assign(process.env, {
    DATAFOUNDRY_AUTH_MODE: "password",
    AUTH_SESSION_SECRET: SECRET,
    AUTH_PUBLIC_BASE_URL: "http://127.0.0.1:3000",
    AUTH_EMAIL_DELIVERY: "test",
    AUTH_REGISTRATION_MODE: "open",
    EMBEDDING_API_KEY: "",
    MASTRA_STORAGE_PATH: join(root, "mastra.sqlite"),
    STORAGE_ROOT_DIR: root,
    ...envOverrides
  });

  const metadataStore = createMetadataStore({
    database_path: join(root, "metadata.sqlite"),
    secret_master_key: "auth-foundation-secret-master-key"
  });
  const server = await createApiServer({ metadataStore });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  assert(address && typeof address === "object");
  const baseUrl = `http://127.0.0.1:${address.port}`;

  try {
    await run({ baseUrl, metadataStore });
  } finally {
    await new Promise((resolve, reject) => {
      server.close((error) => (error ? reject(error) : resolve()));
      setImmediate(() => server.closeAllConnections?.());
    });
    for (const key of Object.keys(process.env)) {
      if (!(key in previous)) {
        delete process.env[key];
      }
    }
    Object.assign(process.env, previous);
  }
}

test("GET /api/v1/auth/status is public and returns safe fields only", async () => {
  await withPasswordApi({ AUTH_REGISTRATION_MODE: "open" }, async ({ baseUrl }) => {
    const response = await fetch(`${baseUrl}/api/v1/auth/status`);
    assert.equal(response.status, 200);
    const body = await response.json();
    assert.deepEqual(Object.keys(body.data).sort(), ["publicBaseUrl", "registrationEnabled"]);
    assert.equal(body.data.publicBaseUrl, "http://127.0.0.1:3000");
    assert.equal(body.data.registrationEnabled, true);
    const serialized = JSON.stringify(body);
    assert.doesNotMatch(serialized, /AUTH_SESSION_SECRET|smtp|secret|8787|master/i);
  });
});

test("closed registration rejects register with REGISTRATION_CLOSED", async () => {
  await withPasswordApi({ AUTH_REGISTRATION_MODE: "closed" }, async ({ baseUrl, metadataStore }) => {
    const status = await fetch(`${baseUrl}/api/v1/auth/status`);
    const statusBody = await status.json();
    assert.equal(statusBody.data.registrationEnabled, false);

    const email = `${randomUUID()}@example.test`;
    const response = await fetch(`${baseUrl}/api/v1/auth/register`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        email,
        password: "correct-horse",
        displayName: "Closed User"
      })
    });
    assert.equal(response.status, 403);
    const body = await response.json();
    assert.equal(body.error.code, "REGISTRATION_CLOSED");
    assert.equal(metadataStore.users.findByEmail({ email }), undefined);
  });
});

test("open registration still allows register", async () => {
  await withPasswordApi({ AUTH_REGISTRATION_MODE: "open" }, async ({ baseUrl }) => {
    const email = `${randomUUID()}@example.test`;
    const response = await fetch(`${baseUrl}/api/v1/auth/register`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        email,
        password: "correct-horse",
        displayName: "Open User"
      })
    });
    assert.equal(response.status, 201, await response.clone().text());
  });
});

async function registerVerify(baseUrl, { email, password, displayName }) {
  const registered = await fetch(`${baseUrl}/api/v1/auth/register`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password, displayName })
  });
  assert.equal(registered.status, 201, await registered.clone().text());
  const body = await registered.json();
  const verified = await fetch(`${baseUrl}/api/v1/auth/verify-email`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ token: body.data.verificationToken })
  });
  assert.equal(verified.status, 200, await verified.clone().text());
  return body;
}

test("CSRF_INVALID and recoverable csrf refresh", async () => {
  await withPasswordApi({}, async ({ baseUrl }) => {
    const email = `${randomUUID()}@example.test`;
    const password = "correct-horse-battery";
    await registerVerify(baseUrl, { email, password, displayName: "Csrf User" });

    const login = await fetch(`${baseUrl}/api/v1/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password })
    });
    assert.equal(login.status, 200);
    const setCookies = login.headers.getSetCookie();
    const cookieHeader = setCookies.map((item) => item.split(";", 1)[0]).join("; ");
    const oldCsrf = setCookies
      .find((item) => item.startsWith("df_csrf="))
      ?.split(";", 1)[0]
      ?.slice("df_csrf=".length);
    assert.ok(oldCsrf);

    const missing = await fetch(`${baseUrl}/api/v1/datasources`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Cookie: cookieHeader
      },
      body: JSON.stringify({
        id: `ds-${randomUUID()}`,
        name: "No CSRF",
        type: "sqlite",
        settings: { filePath: ":memory:" }
      })
    });
    assert.equal(missing.status, 403);
    assert.equal((await missing.json()).error.code, "CSRF_INVALID");

    const anonymousRefresh = await fetch(`${baseUrl}/api/v1/auth/csrf/refresh`, {
      method: "POST"
    });
    assert.equal(anonymousRefresh.status, 401);

    const refresh = await fetch(`${baseUrl}/api/v1/auth/csrf/refresh`, {
      method: "POST",
      headers: { Cookie: cookieHeader }
    });
    assert.equal(refresh.status, 200, await refresh.clone().text());
    assert.equal(refresh.headers.get("cache-control"), "no-store");
    const refreshBody = await refresh.json();
    const newCsrf = refreshBody.data.csrfToken;
    assert.equal(typeof newCsrf, "string");
    assert.notEqual(newCsrf, decodeURIComponent(oldCsrf));
    const refreshedCookies = refresh.headers.getSetCookie();
    assert.ok(refreshedCookies.some((item) => item.startsWith("df_csrf=")));
    const refreshedCookieHeader = [
      ...cookieHeader
        .split("; ")
        .filter((part) => !part.startsWith("df_csrf=")),
      `df_csrf=${encodeURIComponent(newCsrf)}`
    ].join("; ");

    const oldStillInvalid = await fetch(`${baseUrl}/api/v1/datasources`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Cookie: cookieHeader,
        "X-CSRF-Token": decodeURIComponent(oldCsrf)
      },
      body: JSON.stringify({
        id: `ds-${randomUUID()}`,
        name: "Old CSRF",
        type: "sqlite",
        settings: { filePath: ":memory:" }
      })
    });
    assert.equal(oldStillInvalid.status, 403);
    assert.equal((await oldStillInvalid.json()).error.code, "CSRF_INVALID");

    const withNew = await fetch(`${baseUrl}/api/v1/datasources`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Cookie: refreshedCookieHeader,
        "X-CSRF-Token": newCsrf
      },
      body: JSON.stringify({
        id: `ds-${randomUUID()}`,
        name: "New CSRF",
        type: "sqlite",
        settings: { filePath: ":memory:" }
      })
    });
    assert.equal(withNew.status, 201, await withNew.clone().text());
  });
});

test("login anti-enumeration and client session lifetimes", async () => {
  await withPasswordApi({}, async ({ baseUrl, metadataStore }) => {
    const password = "correct-horse-battery";
    const verifiedEmail = `${randomUUID()}@example.test`;
    const unverifiedEmail = `${randomUUID()}@example.test`;

    await registerVerify(baseUrl, {
      email: verifiedEmail,
      password,
      displayName: "Verified"
    });

    const unverified = await fetch(`${baseUrl}/api/v1/auth/register`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        email: unverifiedEmail,
        password,
        displayName: "Unverified"
      })
    });
    assert.equal(unverified.status, 201);

    const missing = await fetch(`${baseUrl}/api/v1/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: `${randomUUID()}@example.test`, password: "whatever-password" })
    });
    assert.equal(missing.status, 401);
    const missingBody = await missing.json();
    assert.equal(missingBody.error.message, "Invalid email or password.");

    const wrongPassword = await fetch(`${baseUrl}/api/v1/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: verifiedEmail, password: "wrong-password" })
    });
    assert.equal(wrongPassword.status, 401);
    const wrongBody = await wrongPassword.json();
    assert.equal(wrongBody.error.message, "Invalid email or password.");

    const unverifiedWrong = await fetch(`${baseUrl}/api/v1/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: unverifiedEmail, password: "wrong-password" })
    });
    assert.equal(unverifiedWrong.status, 401);
    assert.equal((await unverifiedWrong.json()).error.message, "Invalid email or password.");

    const unverifiedCorrect = await fetch(`${baseUrl}/api/v1/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: unverifiedEmail, password })
    });
    assert.equal(unverifiedCorrect.status, 403);
    assert.equal((await unverifiedCorrect.json()).error.code, "EMAIL_NOT_VERIFIED");

    const illegalClient = await fetch(`${baseUrl}/api/v1/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: verifiedEmail, password, client: "mobile" })
    });
    assert.equal(illegalClient.status, 400);

    const webLogin = await fetch(`${baseUrl}/api/v1/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: verifiedEmail, password, client: "web" })
    });
    assert.equal(webLogin.status, 200, await webLogin.clone().text());
    const webBody = await webLogin.json();
    assert.equal(typeof webBody.data.session.expiresAt, "string");
    const webExpires = Date.parse(webBody.data.session.expiresAt);
    const webDelta = webExpires - Date.now();
    assert.ok(Math.abs(webDelta - 30 * 24 * 60 * 60 * 1000) < 120_000);

    const webCookie = webLogin.headers.getSetCookie().find((item) => item.startsWith("df_session="));
    assert.match(webCookie ?? "", /Max-Age=2592000/i);

    const tuiLogin = await fetch(`${baseUrl}/api/v1/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: verifiedEmail, password, client: "tui" })
    });
    assert.equal(tuiLogin.status, 200, await tuiLogin.clone().text());
    const tuiBody = await tuiLogin.json();
    const tuiExpires = Date.parse(tuiBody.data.session.expiresAt);
    const tuiDelta = tuiExpires - Date.now();
    assert.ok(Math.abs(tuiDelta - 7 * 24 * 60 * 60 * 1000) < 120_000);
    const tuiCookie = tuiLogin.headers.getSetCookie().find((item) => item.startsWith("df_session="));
    assert.match(tuiCookie ?? "", /Max-Age=604800/i);

    const sessions = metadataStore.authSessions.listByUser
      ? metadataStore.authSessions.listByUser({ user_id: webBody.data.user.id })
      : undefined;
    if (Array.isArray(sessions) && sessions.length > 0) {
      const matching = sessions.find((session) => session.expires_at === tuiBody.data.session.expiresAt)
        ?? sessions.find((session) => session.expires_at === webBody.data.session.expiresAt);
      assert.ok(matching, "expected session expires_at to match login response");
    }
  });
});

test("scripts/**/*.mjs HTTP auth classification gate", () => {
  const classified = new Set([
    ...FORMAL_HTTP_AUTH_TARGETS,
    ...PUBLIC_HTTP_TARGETS,
    ...DIRECT_METADATA_FIXTURE_TARGETS,
    ...AUTH_FOUNDATION_HARNESS_TARGETS
  ]);
  const httpScripts = listMjsFiles(SCRIPTS_ROOT)
    .map((fullPath) => ({
      fullPath,
      relativePath: relative(SCRIPTS_ROOT, fullPath).replaceAll("\\", "/")
    }))
    .filter(({ fullPath }) => isHttpScript(readFileSync(fullPath, "utf8")));

  const unclassified = httpScripts
    .map((item) => item.relativePath)
    .filter((path) => !classified.has(path))
    .sort();
  assert.deepEqual(
    unclassified,
    [],
    `Unclassified HTTP scripts must be listed in FORMAL_HTTP_AUTH_TARGETS, PUBLIC_HTTP_TARGETS, DIRECT_METADATA_FIXTURE_TARGETS, or AUTH_FOUNDATION_HARNESS_TARGETS:\n${unclassified.join("\n")}`
  );

  const formalViolations = [];
  for (const relativePath of FORMAL_HTTP_AUTH_TARGETS) {
    const fullPath = join(SCRIPTS_ROOT, relativePath);
    const source = readFileSync(fullPath, "utf8");
    for (const pattern of FORBIDDEN_DEV_AUTH_PATTERNS) {
      if (pattern.test(source)) {
        formalViolations.push(`${relativePath} matches ${pattern}`);
      }
    }
    if (!source.includes("authenticated-test-client")) {
      formalViolations.push(
        `${relativePath} must import scripts/lib/authenticated-test-client (shared password-auth client)`
      );
    }
    const syntaxCheck = spawnSync(process.execPath, ["--check", fullPath], {
      encoding: "utf8"
    });
    if (syntaxCheck.status !== 0) {
      const detail = [syntaxCheck.stderr, syntaxCheck.stdout].filter(Boolean).join("\n").trim();
      formalViolations.push(
        `${relativePath} failed node --check${detail ? `:\n${detail}` : ""}`
      );
    }
  }
  assert.deepEqual(
    formalViolations,
    [],
    `FORMAL_HTTP_AUTH_TARGETS must not use development auth bypasses, must use the shared client, and must pass node --check:\n${formalViolations.join("\n")}`
  );
});
