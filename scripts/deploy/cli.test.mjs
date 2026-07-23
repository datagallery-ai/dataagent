import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { mkdtemp, readdir, readFile, writeFile, mkdir } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import {
  configureDeploymentInteractively,
  collectManagedPorts,
  createDeployLogWriter,
  inspectWritablePath,
  resolveAuthPublicBaseUrl,
  resolveTuiRuntimeUrl,
  runDeploymentDoctor
} from "./cli.mjs";

function createAsk(answers) {
  const queue = [...answers];
  return async () => {
    if (queue.length === 0) throw new Error("unexpected prompt");
    return queue.shift();
  };
}

test("first run selects Web and API ports with defaults", async () => {
  const lines = [];
  const root = await mkdtemp(path.join(os.tmpdir(), "df-cli-"));
  await mkdir(path.join(root, "apps/web"), { recursive: true });
  const result = await configureDeploymentInteractively({
    root,
    sourceText: "",
    reconfigure: false,
    nonInteractive: false,
    ask: createAsk(["1", "1", ""]),
    print: (line) => lines.push(String(line)),
    probe: async () => ({ available: true, owner: null })
  });
  assert.equal(result.env.WEB_PORT, "3000");
  assert.equal(result.env.API_PORT, "8787");
});

test("existing complete config skips prompts unless reconfigure", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "df-cli-"));
  await mkdir(path.join(root, "apps/web"), { recursive: true });
  const source = [
    "WEB_PORT=3310",
    "API_PORT=8877",
    "AUTH_SESSION_SECRET=existing-session-secret-value",
    "SECRET_MASTER_KEY=existing-master-secret-value",
    "AUTH_PUBLIC_BASE_URL=http://127.0.0.1:3310",
    "DATAFOUNDRY_AUTH_MODE=password",
    "AUTH_EMAIL_DELIVERY=test",
    "WEB_HOST=0.0.0.0",
    "API_HOST=127.0.0.1",
    "STORAGE_ROOT_DIR=storage",
    "METADATA_DB_PATH=storage/metadata/workbench.sqlite"
  ].join("\n");
  let asked = 0;
  const result = await configureDeploymentInteractively({
    root,
    sourceText: source,
    reconfigure: false,
    nonInteractive: false,
    ask: async () => {
      asked += 1;
      throw new Error("must not prompt");
    },
    print: () => {},
    probe: async () => ({ available: true, owner: null })
  });
  assert.equal(asked, 0);
  assert.equal(result.env.WEB_PORT, "3310");
});

test("partial .env is not treated as complete before fill and still prompts", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "df-cli-partial-"));
  await mkdir(path.join(root, "apps/web"), { recursive: true });
  let asked = 0;
  const result = await configureDeploymentInteractively({
    root,
    sourceText: "FOO=bar\nCUSTOM=keep-me\n",
    reconfigure: false,
    nonInteractive: false,
    ask: createAsk(["1", "1", ""]),
    print: () => {
      asked += 1;
    },
    probe: async () => ({ available: true, owner: null })
  });
  assert.match(result.envText, /CUSTOM=keep-me/);
  assert.ok(asked > 0);
});

test("non-interactive never calls ask and warns on loopback public URL with remote bind", async () => {
  const lines = [];
  const root = await mkdtemp(path.join(os.tmpdir(), "df-cli-"));
  await mkdir(path.join(root, "apps/web"), { recursive: true });
  const result = await configureDeploymentInteractively({
    root,
    sourceText: "",
    reconfigure: false,
    nonInteractive: true,
    processEnv: { WEB_HOST: "0.0.0.0" },
    ask: async () => assert.fail("must not prompt"),
    print: (line) => lines.push(String(line)),
    probe: async () => ({ available: true, owner: null })
  });
  assert.equal(result.env.WEB_PORT, "3000");
  assert.match(lines.join("\n"), /本机访问|local-machine|仅适合本机/i);
});

test("port menu rejects n with explicit hint", async () => {
  const lines = [];
  const root = await mkdtemp(path.join(os.tmpdir(), "df-cli-"));
  await mkdir(path.join(root, "apps/web"), { recursive: true });
  const ask = createAsk(["n", "1", "1", ""]);
  await configureDeploymentInteractively({
    root,
    sourceText: "",
    reconfigure: false,
    nonInteractive: false,
    ask,
    print: (line) => lines.push(String(line)),
    probe: async () => ({ available: true, owner: null })
  });
  assert.match(lines.join("\n"), /请输入 1 或 2/);
});

test("port menu aborts after too many invalid choices", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "df-cli-"));
  await mkdir(path.join(root, "apps/web"), { recursive: true });
  await assert.rejects(
    () =>
      configureDeploymentInteractively({
        root,
        sourceText: "",
        reconfigure: false,
        nonInteractive: false,
        ask: createAsk(["n", "n", "n", "n", "n"]),
        print: () => {},
        probe: async () => ({ available: true, owner: null })
      }),
    /too many invalid choices/
  );
});

test("reconfigure creates backup and keeps secrets", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "df-cli-"));
  await mkdir(path.join(root, "apps/web"), { recursive: true });
  const source = [
    "WEB_PORT=3000",
    "API_PORT=8787",
    "AUTH_SESSION_SECRET=existing-session-secret-value",
    "SECRET_MASTER_KEY=existing-master-secret-value",
    "AUTH_PUBLIC_BASE_URL=http://127.0.0.1:3000",
    "DATAFOUNDRY_AUTH_MODE=password",
    "AUTH_EMAIL_DELIVERY=test",
    "WEB_HOST=0.0.0.0",
    "API_HOST=127.0.0.1",
    "STORAGE_ROOT_DIR=storage",
    "METADATA_DB_PATH=storage/metadata/workbench.sqlite"
  ].join("\n");
  await writeFile(path.join(root, ".env"), `${source}\n`);
  const result = await configureDeploymentInteractively({
    root,
    sourceText: source,
    reconfigure: true,
    nonInteractive: false,
    ask: createAsk(["1", "1", ""]),
    print: () => {},
    probe: async () => ({ available: true, owner: null }),
    write: true,
    timestamp: "20260722-150000"
  });
  assert.match(result.envText, /AUTH_SESSION_SECRET=existing-session-secret-value/);
  assert.equal(
    await readFile(path.join(root, ".env.backup-20260722-150000"), "utf8"),
    `${source}\n`
  );
});

test("collectManagedPorts gathers env and deployment state ports", () => {
  const ports = collectManagedPorts(
    { WEB_PORT: "3310", API_PORT: "8877" },
    { ports: { web: 3310, api: 8877 } }
  );
  assert.deepEqual([...ports].sort((a, b) => a - b), [3310, 8877]);
});

test("reconfigure reuses managed listening ports during update preflight", async () => {
  const probes = [];
  const root = await mkdtemp(path.join(os.tmpdir(), "df-cli-"));
  await mkdir(path.join(root, "apps/web"), { recursive: true });
  await mkdir(path.join(root, "storage/run"), { recursive: true });
  const source = [
    "WEB_PORT=3310",
    "API_PORT=8877",
    "AUTH_SESSION_SECRET=existing-session-secret-value",
    "SECRET_MASTER_KEY=existing-master-secret-value",
    "AUTH_PUBLIC_BASE_URL=http://127.0.0.1:3310",
    "DATAFOUNDRY_AUTH_MODE=password",
    "AUTH_EMAIL_DELIVERY=test",
    "WEB_HOST=0.0.0.0",
    "API_HOST=127.0.0.1",
    "STORAGE_ROOT_DIR=storage",
    "METADATA_DB_PATH=storage/metadata/workbench.sqlite"
  ].join("\n");
  await writeFile(
    path.join(root, "storage/run/deployment.json"),
    `${JSON.stringify({
      pid: 99999,
      launchId: "launch-1",
      status: "healthy",
      startedAt: "2026-07-22T00:00:00.000Z",
      ports: { web: 3310, api: 8877 }
    })}\n`
  );
  const result = await configureDeploymentInteractively({
    root,
    sourceText: source,
    reconfigure: true,
    nonInteractive: true,
    ask: async () => assert.fail("must not prompt"),
    print: () => {},
    probe: async (port) => {
      probes.push(port);
      return { available: false, owner: "datafoundry pid=99999" };
    }
  });
  assert.equal(result.env.WEB_PORT, "3310");
  assert.equal(result.env.API_PORT, "8877");
  assert.ok(probes.includes(3310));
  assert.ok(probes.includes(8877));
});

test("resolveAuthPublicBaseUrl preserves HTTPS proxy URLs without explicit port", () => {
  assert.equal(
    resolveAuthPublicBaseUrl("https://prod.example.com", "3000", "3001"),
    "https://prod.example.com"
  );
  assert.equal(
    resolveAuthPublicBaseUrl("http://127.0.0.1:3000", "3000", "3001"),
    "http://127.0.0.1:3001"
  );
  assert.equal(
    resolveAuthPublicBaseUrl("https://prod.example.com:8443", "8443", "3001"),
    "https://prod.example.com:8443"
  );
  assert.equal(
    resolveAuthPublicBaseUrl("https://prod.example.com:3000", "3000", "3001"),
    "https://prod.example.com:3000"
  );
  assert.equal(
    resolveAuthPublicBaseUrl("http://example.com:3000", "3000", "3310"),
    "http://example.com:3000"
  );
});

test("reconfigure keeps HTTPS public URL when ports are unchanged", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "df-cli-"));
  await mkdir(path.join(root, "apps/web"), { recursive: true });
  const source = [
    "WEB_PORT=3000",
    "API_PORT=8787",
    "AUTH_SESSION_SECRET=existing-session-secret-value",
    "SECRET_MASTER_KEY=existing-master-secret-value",
    "AUTH_PUBLIC_BASE_URL=https://prod.example.com",
    "DATAFOUNDRY_AUTH_MODE=password",
    "AUTH_EMAIL_DELIVERY=test",
    "WEB_HOST=0.0.0.0",
    "API_HOST=127.0.0.1",
    "STORAGE_ROOT_DIR=storage",
    "METADATA_DB_PATH=storage/metadata/workbench.sqlite"
  ].join("\n");
  const result = await configureDeploymentInteractively({
    root,
    sourceText: source,
    reconfigure: true,
    nonInteractive: true,
    ask: async () => assert.fail("must not prompt"),
    print: () => {},
    probe: async () => ({ available: true, owner: null })
  });
  assert.equal(result.env.AUTH_PUBLIC_BASE_URL, "https://prod.example.com");
});

test("createDeployLogWriter redacts secrets and updates deploy-latest.log", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "df-cli-log-"));
  const writer = await createDeployLogWriter(root, { timestamp: "20260722143000" });
  await writer.append("AUTH_SESSION_SECRET=fixture-deploy-secret-at-least-32-chars\nnpm ci output\n");
  await writer.finalize();
  const logText = await readFile(path.join(root, "storage/logs/deploy-20260722143000.log"), "utf8");
  const latestText = await readFile(path.join(root, "storage/logs/deploy-latest.log"), "utf8");
  assert.doesNotMatch(logText, /fixture-deploy-secret-at-least-32-chars/);
  assert.match(logText, /npm ci output/);
  assert.equal(latestText, logText);
  assert.equal(writer.logPath, "storage/logs/deploy-20260722143000.log");
});

test("createDeployLogWriter redacts secrets split across append chunks", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "df-cli-log-chunk-"));
  const writer = await createDeployLogWriter(root, { timestamp: "20260722143001" });
  await writer.append("AUTH_SESSION_SECRET=fixture-deploy-secret-");
  await writer.append("at-least-32-chars\nnpm ci output\n");
  await writer.finalize();
  const logText = await readFile(path.join(root, "storage/logs/deploy-20260722143001.log"), "utf8");
  assert.doesNotMatch(logText, /fixture-deploy-secret-at-least-32-chars/);
  assert.match(logText, /AUTH_SESSION_SECRET=\*{4,8}/);
  assert.match(logText, /npm ci output/);
});

test("runDeploymentDoctor reports os, deps, config, ports, permissions, disk, pid, and health", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "df-cli-doctor-"));
  await mkdir(path.join(root, "storage"), { recursive: true });
  await writeFile(
    path.join(root, ".env"),
    [
      "WEB_PORT=3000",
      "API_PORT=8787",
      "AUTH_SESSION_SECRET=existing-session-secret-value",
      "SECRET_MASTER_KEY=existing-master-secret-value",
      "AUTH_PUBLIC_BASE_URL=http://127.0.0.1:3000"
    ].join("\n")
  );
  const beforeEntries = await readdir(path.join(root, "storage"));
  const lines = [];
  const result = await runDeploymentDoctor(root, {
    print: (line) => lines.push(line),
    run: async (command, args = []) => {
      const key = [command, ...args].join(" ");
      if (key === "node --version") return { stdout: "v22.14.0\n" };
      if (key === "npm --version") return { stdout: "10.9.0\n" };
      throw new Error(`missing mock for ${key}`);
    },
    probe: async () => ({ available: true, owner: null })
  });
  const joined = lines.join("\n");
  assert.match(joined, /^os: /m);
  assert.match(joined, /^arch: /m);
  assert.match(joined, /dependency node: ok/);
  assert.match(joined, /config: ok/);
  assert.match(joined, /port web 3000: available/);
  assert.match(joined, /permissions storage: writable/);
  assert.match(joined, /disk free:/);
  assert.match(joined, /pid: none/);
  assert.match(joined, /health: skipped/);
  assert.ok(result.lines.length >= 8);
  const afterEntries = await readdir(path.join(root, "storage"));
  assert.deepEqual(afterEntries, beforeEntries);
  assert.ok(afterEntries.every((entry) => !entry.startsWith(".doctor-write-")));
});

test("inspectWritablePath checks access without creating files", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "df-writable-"));
  const missingChild = path.join(root, "storage", "nested");
  const result = await inspectWritablePath(missingChild);
  assert.equal(result.writable, true);
  assert.equal(result.exists, false);
  assert.equal(existsSync(missingChild), false);
  assert.equal(existsSync(path.join(root, "storage")), false);
});

test("resolveTuiRuntimeUrl defaults to deployed API port", () => {
  assert.equal(
    resolveTuiRuntimeUrl({ API_PORT: "8877" }),
    "http://127.0.0.1:8877/api/copilotkit"
  );
  assert.equal(
    resolveTuiRuntimeUrl({ API_PORT: "8877" }, "http://example/api/copilotkit"),
    "http://example/api/copilotkit"
  );
});
