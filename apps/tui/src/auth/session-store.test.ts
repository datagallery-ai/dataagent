import assert from "node:assert/strict";
import { mkdtemp, readFile, stat, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";
import {
  normalizeApiBaseUrl,
  resolveTuiAuthStorePath,
  TuiSessionStore,
} from "./session-store.js";
import type { StoredTuiSession } from "./types.js";

async function sleep(ms: number): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, ms));
}

function sampleSession(apiBaseUrl: string, email = "a@example.com"): StoredTuiSession {
  return {
    apiBaseUrl,
    cookies: { df_session: "s", df_csrf: "c" },
    user: { id: "u1", email },
    workspace: { id: "w1", name: "Personal" },
    expiresAt: new Date(Date.now() + 60_000).toISOString(),
  };
}

describe("normalizeApiBaseUrl", () => {
  it("keeps deployment path and strips trailing slash", () => {
    assert.equal(
      normalizeApiBaseUrl("http://example.com/deploy/"),
      "http://example.com/deploy",
    );
  });

  it("treats localhost and 127.0.0.1 as different keys", () => {
    assert.notEqual(
      normalizeApiBaseUrl("http://localhost:8787"),
      normalizeApiBaseUrl("http://127.0.0.1:8787"),
    );
  });
});

describe("resolveTuiAuthStorePath", () => {
  it("uses APPDATA on Windows", () => {
    assert.equal(
      resolveTuiAuthStorePath({
        platform: "win32",
        env: { APPDATA: "C:\\Users\\me\\AppData\\Roaming" },
        homedir: () => "C:\\Users\\me",
      }),
      join("C:\\Users\\me\\AppData\\Roaming", "DataFoundry", "tui-auth.json"),
    );
  });

  it("uses XDG_CONFIG_HOME on Linux", () => {
    assert.equal(
      resolveTuiAuthStorePath({
        platform: "linux",
        env: { XDG_CONFIG_HOME: "/tmp/xdg" },
        homedir: () => "/home/me",
      }),
      join("/tmp/xdg", "datafoundry", "tui-auth.json"),
    );
  });

  it("uses Application Support on macOS", () => {
    assert.equal(
      resolveTuiAuthStorePath({
        platform: "darwin",
        env: {},
        homedir: () => "/Users/me",
      }),
      join("/Users/me", "Library", "Application Support", "DataFoundry", "tui-auth.json"),
    );
  });
});

describe("TuiSessionStore", () => {
  it("isolates sessions by base URL and replaces same URL account", async () => {
    const dir = await mkdtemp(join(tmpdir(), "tui-session-"));
    const store = new TuiSessionStore({ filePath: join(dir, "tui-auth.json") });
    await store.save(sampleSession("http://127.0.0.1:8787", "old@example.com"));
    await store.save(sampleSession("http://localhost:8787", "other@example.com"));
    await store.save(sampleSession("http://127.0.0.1:8787", "new@example.com"));

    const local = await store.load("http://127.0.0.1:8787/");
    const loop = await store.load("http://localhost:8787");
    assert.equal(local?.user.email, "new@example.com");
    assert.equal(loop?.user.email, "other@example.com");
  });

  it("treats corrupt JSON as missing and quarantines the file", async () => {
    const dir = await mkdtemp(join(tmpdir(), "tui-session-corrupt-"));
    const filePath = join(dir, "tui-auth.json");
    await writeFile(filePath, "{not-json", "utf8");
    const store = new TuiSessionStore({ filePath });
    assert.equal(await store.load("http://127.0.0.1:8787"), undefined);
    const files = await readFile(filePath, "utf8");
    assert.match(files, /"sessions": \{\}/);
  });

  it("quarantines files with shallow/invalid session objects", async () => {
    const dir = await mkdtemp(join(tmpdir(), "tui-session-shallow-"));
    const filePath = join(dir, "tui-auth.json");
    await writeFile(
      filePath,
      JSON.stringify({
        version: 1,
        sessions: {
          "http://127.0.0.1:8787": { user: "not-an-object" },
        },
      }),
      "utf8",
    );
    const store = new TuiSessionStore({ filePath });
    assert.equal(await store.load("http://127.0.0.1:8787"), undefined);
    assert.match(await readFile(filePath, "utf8"), /"sessions": \{\}/);
  });

  it("merges concurrent saves under file lock", async () => {
    const dir = await mkdtemp(join(tmpdir(), "tui-session-lock-"));
    const filePath = join(dir, "tui-auth.json");
    const store = new TuiSessionStore({ filePath });
    await Promise.all([
      store.save(sampleSession("http://127.0.0.1:8787", "a@example.com")),
      store.save(sampleSession("http://localhost:8787", "b@example.com")),
    ]);
    assert.equal((await store.load("http://127.0.0.1:8787"))?.user.email, "a@example.com");
    assert.equal((await store.load("http://localhost:8787"))?.user.email, "b@example.com");
  });

  it("writes atomically with restrictive unix permissions", async () => {
    if (process.platform === "win32") {
      return;
    }
    const dir = await mkdtemp(join(tmpdir(), "tui-session-mode-"));
    const filePath = join(dir, "tui-auth.json");
    const store = new TuiSessionStore({ filePath });
    await store.save(sampleSession("http://127.0.0.1:8787"));
    const fileMode = (await stat(filePath)).mode & 0o777;
    const dirMode = (await stat(dir)).mode & 0o777;
    assert.equal(fileMode, 0o600);
    assert.equal(dirMode, 0o700);
  });

  it("rejects symlink store paths on Windows-style safety check", async () => {
    if (process.platform === "win32") {
      return;
    }
    const dir = await mkdtemp(join(tmpdir(), "tui-session-symlink-"));
    const target = join(dir, "real.json");
    const link = join(dir, "tui-auth.json");
    await writeFile(target, JSON.stringify({ version: 1, sessions: {} }), "utf8");
    await symlink(target, link);
    const store = new TuiSessionStore({ filePath: link });
    await assert.rejects(
      () => store.save(sampleSession("http://127.0.0.1:8787")),
      /regular file/i,
    );
  });

  it("reclaims stale locks that record a dead pid", async () => {
    const dir = await mkdtemp(join(tmpdir(), "tui-session-stale-lock-"));
    const filePath = join(dir, "tui-auth.json");
    const lockPath = `${filePath}.lock`;
    await writeFile(
      lockPath,
      `${JSON.stringify({ pid: 2_147_483_647, acquiredAt: Date.now() - 60_000, token: "dead" })}\n`,
      "utf8",
    );
    const store = new TuiSessionStore({ filePath });
    await store.save(sampleSession("http://127.0.0.1:8787"));
    assert.equal((await store.load("http://127.0.0.1:8787"))?.user.email, "a@example.com");
  });

  it("keeps quarantine of corrupt files inside the same lock critical section", async () => {
    const dir = await mkdtemp(join(tmpdir(), "tui-session-quarantine-lock-"));
    const filePath = join(dir, "tui-auth.json");
    await writeFile(filePath, "{not-json", "utf8");
    const store = new TuiSessionStore({ filePath });

    // Concurrent load (quarantine) + save must not drop the saved session.
    await Promise.all([
      store.load("http://127.0.0.1:8787"),
      (async () => {
        await sleep(5);
        await store.save(sampleSession("http://127.0.0.1:8787", "kept@example.com"));
      })(),
    ]);

    assert.equal(
      (await store.load("http://127.0.0.1:8787"))?.user.email,
      "kept@example.com",
    );
  });
});
