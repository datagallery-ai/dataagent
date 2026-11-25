import assert from "node:assert/strict";
import { copyFile, mkdtemp, readFile, rm, stat, writeFile, mkdir, symlink } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { managedWebStartArgs } from "./cli.mjs";
import {
  deploymentPaths,
  isProcessAlive,
  readDeploymentState,
  startManagedStack,
  stopManagedStack,
  validateDeploymentState,
  verifyManagedProcessForStop,
  writeDeploymentState
} from "./process-state.mjs";

for (const webOnly of [false, true]) test(webOnly ? "web-only startup leaves the API to its own launcher" : "managed startup launches the built Web and API", async (t) => {
  if (process.platform !== "linux") return t.skip("managed process verification uses procfs");
  const root = await mkdtemp(path.join(os.tmpdir(), "df-managed-launch-"));
  t.after(async () => {
    await stopManagedStack(root, { timeoutMs: 2000 });
    await rm(root, { recursive: true, force: true });
  });
  const repository = fileURLToPath(new URL("../../", import.meta.url));
  for (const file of ["scripts/start.mjs", "scripts/stack-runner.mjs", "scripts/stack-runtime-config.mjs",
    "ui/shared/server-listen.mjs", "ui/shared/config-yaml.mjs", "ui/shared/config-env.mjs", "ui/shared/python-environment.mjs", "ui/shared/logging.mjs", "ui/shared/log-redaction.mjs"]) {
    await mkdir(path.dirname(path.join(root, file)), { recursive: true });
    await copyFile(path.join(repository, file), path.join(root, file));
  }
  await symlink(path.join(repository, "node_modules"), path.join(root, "node_modules"), "dir");
  await mkdir(path.join(root, "bin"));
  await mkdir(path.join(root, "home"));
  await mkdir(path.join(root, "ui/web/.next"), { recursive: true });
  await writeFile(path.join(root, "ui/web/.next/BUILD_ID"), "fixture");
  await writeFile(path.join(root, "home/config.yaml"), webOnly ? "server: false" : JSON.stringify({ server: { host: "127.0.0.1", port: 1 } }));
  const log = path.join(root, "commands.jsonl");
  for (const command of webOnly ? ["npm"] : ["python", "npm"]) {
    const location = command === "python" ? path.join(root, ".venv/bin/python") : path.join(root, "bin", command);
    await mkdir(path.dirname(location), { recursive: true });
    await writeFile(location, `#!/usr/bin/env node
const fs = require('node:fs');
const path = require('node:path');
fs.appendFileSync(process.env.CALL_LOG, JSON.stringify({
  name: path.basename(process.argv[1]), args: process.argv.slice(2), noSync: process.env.UV_NO_SYNC,
  target: process.env.API_PROXY_TARGET,
}) + '\\n');
// Keep both fixture services alive until their launch commands have been recorded.
setInterval(() => {
  if (fs.readFileSync(process.env.CALL_LOG, 'utf8').trim().split('\\n').length >= ${webOnly ? 1 : 2}) process.exit(0);
}, 10);
setTimeout(() => process.exit(0), 1000);
`, { mode: 0o755 });
  }
  const started = await startManagedStack(root, {
    env: { ...process.env, DATAAGENT_HOME: path.join(root, "home"), CALL_LOG: log, UV_PROJECT_ENVIRONMENT: ".venv",
      API_PROXY_TARGET: "http://127.0.0.1:2",
      PATH: `${path.join(root, "bin")}${path.delimiter}${process.env.PATH}` },
    args: webOnly ? ["--web"] : managedWebStartArgs({ host: "127.0.0.1", port: "3310" }),
  });
  const deadline = Date.now() + 5000;
  while (isProcessAlive(started.pid) && Date.now() < deadline) {
    await new Promise(resolve => setTimeout(resolve, 20));
  }
  assert.equal(isProcessAlive(started.pid), false, "fixture services must exit after recording their commands");
  const calls = (await readFile(log, "utf8")).trim().split("\n").map(JSON.parse);
  assert.equal(calls.length, webOnly ? 1 : 2, JSON.stringify(calls));
  assert(calls.every(call => call.noSync === undefined));
  assert.equal(calls.some(call => call.name === "python" && call.args.includes("serve")), !webOnly);
  assert(calls.some(call => call.name === "npm" && call.args.includes("start") && call.target === "http://127.0.0.1:" + (webOnly ? 2 : 1)));
  assert(!calls.some(call => call.args.includes("sync") || call.args.some(arg => arg.startsWith("build"))));
});

test("validateDeploymentState rejects sensitive fields", () => {
  assert.throws(
    () => validateDeploymentState({ pid: 123, API_KEY: "must-not-leak" }),
    /sensitive field API_KEY/
  );
});

test("writeDeploymentState persists non-sensitive state atomically", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "df-state-"));
  const state = {
    pid: 12345,
    launchId: "launch-1",
    status: "starting",
    startedAt: "2026-07-22T00:00:00.000Z",
    ports: { web: 3000, api: 8787 }
  };
  await writeDeploymentState(root, state);
  assert.deepEqual(await readDeploymentState(root), state);
  const raw = await readFile(deploymentPaths(root).deploymentJson, "utf8");
  assert.doesNotMatch(raw, /SECRET|TOKEN|PASSWORD|KEY=/i);
});

test("isProcessAlive detects live and stale pids", () => {
  assert.equal(isProcessAlive(process.pid), true);
  assert.equal(isProcessAlive(99999999), false);
});


test("start and stop managed stack are idempotent", async (t) => {
  if (process.platform === "win32") {
    t.skip("process-group semantics are Unix-only");
    return;
  }

  const root = await mkdtemp(path.join(os.tmpdir(), "df-proc-"));
  await mkdir(path.join(root, "storage"), { recursive: true });

  const started = await startManagedStack(root, {
    command: process.execPath,
    args: ["-e", "setInterval(() => {}, 1000)"],
    env: { ...process.env },
    ports: { web: 3000, api: 8787 },
    commitSha: "deadbeef"
  });
  assert.equal(isProcessAlive(started.pid), true);
  assert.equal((await readDeploymentState(root)).status, "starting");
  assert.equal((await readDeploymentState(root)).launchId, started.launchId);
  const runtimeLogMode = (await stat(deploymentPaths(root).runtimeLog)).mode & 0o777;
  assert.equal(runtimeLogMode, 0o600);

  await assert.rejects(
    () => startManagedStack(root, {
      command: process.execPath,
      args: ["-e", "setInterval(() => {}, 1000)"]
    }),
    /already running/i
  );

  await stopManagedStack(root);
  assert.equal(isProcessAlive(started.pid), false);
  assert.equal(await readDeploymentState(root), null);

  await stopManagedStack(root);
});

test("launchId mismatch is treated as stale: clear state, never signal, allow restart", async (t) => {
  if (process.platform !== "linux") {
    t.skip("proc launchId verification is Linux-only");
    return;
  }

  const root = await mkdtemp(path.join(os.tmpdir(), "df-stale-launch-"));
  await mkdir(path.join(root, "storage"), { recursive: true });

  // Reuse this test process as a "foreign" live pid with mismatched launchId.
  await writeDeploymentState(root, {
    pid: process.pid,
    launchId: "stale-or-reused-launch-id",
    status: "healthy",
    startedAt: new Date().toISOString(),
    ports: { web: 3000, api: 8787 }
  });

  const healed = await stopManagedStack(root);
  assert.equal(healed.stopped, false);
  assert.match(String(healed.reason), /stale/i);
  assert.equal(await readDeploymentState(root), null);
  assert.equal(isProcessAlive(process.pid), true);

  const started = await startManagedStack(root, {
    command: process.execPath,
    args: ["-e", "setInterval(() => {}, 1000)"],
    env: { ...process.env },
    ports: { web: 3000, api: 8787 }
  });
  assert.equal(isProcessAlive(started.pid), true);
  assert.notEqual(started.pid, process.pid);

  await stopManagedStack(root);
  assert.equal(isProcessAlive(started.pid), false);
  assert.equal(await readDeploymentState(root), null);
});
test("startManagedStack self-heals stale launchId without signaling the foreign pid", async (t) => {
  if (process.platform !== "linux") {
    t.skip("proc launchId verification is Linux-only");
    return;
  }

  const root = await mkdtemp(path.join(os.tmpdir(), "df-start-heal-"));
  await mkdir(path.join(root, "storage"), { recursive: true });
  await writeDeploymentState(root, {
    pid: process.pid,
    launchId: "not-our-launch",
    status: "healthy",
    startedAt: new Date().toISOString(),
    ports: { web: 3000, api: 8787 }
  });

  const started = await startManagedStack(root, {
    command: process.execPath,
    args: ["-e", "setInterval(() => {}, 1000)"],
    env: { ...process.env },
    ports: { web: 3310, api: 8877 }
  });
  assert.equal(isProcessAlive(process.pid), true);
  assert.equal(isProcessAlive(started.pid), true);
  assert.notEqual(started.pid, process.pid);
  assert.equal((await readDeploymentState(root)).pid, started.pid);

  await stopManagedStack(root);
});
test("verifyManagedProcessForStop refuses stop when launch id is unreadable", async (t) => {
  if (process.platform !== "linux") {
    t.skip("proc verification is Linux-only");
    return;
  }

  const unverified = await verifyManagedProcessForStop(12345, "launch-1", {
    readLaunchId: async () => null
  });
  assert.equal(unverified.allowed, false);
  assert.equal(unverified.reason, "launch-id-unverified");

  const mismatch = await verifyManagedProcessForStop(process.pid, "expected-launch", {
    readLaunchId: async () => "other-launch"
  });
  assert.equal(mismatch.allowed, false);
  assert.equal(mismatch.reason, "launch-id-mismatch");

  const matched = await verifyManagedProcessForStop(12345, "launch-1", {
    readLaunchId: async () => "launch-1"
  });
  assert.equal(matched.allowed, true);

  const missingExpected = await verifyManagedProcessForStop(12345, "", {
    readLaunchId: async () => "launch-1"
  });
  assert.equal(missingExpected.allowed, false);
  assert.equal(missingExpected.reason, "missing-expected-launch-id");
});

test("stopManagedStack SIGKILLs and clears state when SIGTERM times out", async (t) => {
  if (process.platform === "win32") {
    t.skip("process-group semantics are Unix-only");
    return;
  }

  const root = await mkdtemp(path.join(os.tmpdir(), "df-kill-"));
  await mkdir(path.join(root, "storage"), { recursive: true });

  const started = await startManagedStack(root, {
    command: "bash",
    args: ["-c", "trap '' TERM; while true; do sleep 1; done"],
    env: { ...process.env },
    ports: { web: 3000, api: 8787 }
  });
  assert.equal(isProcessAlive(started.pid), true);

  const result = await stopManagedStack(root, { timeoutMs: 200, killTimeoutMs: 5_000 });
  assert.equal(result.stopped, true);
  assert.equal(isProcessAlive(started.pid), false);
  assert.equal(await readDeploymentState(root), null);
});
