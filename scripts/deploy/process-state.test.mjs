import assert from "node:assert/strict";
import { mkdtemp, readFile, stat, writeFile, mkdir } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import {
  deploymentPaths,
  isProcessAlive,
  readDeploymentState,
  rotateRuntimeLog,
  startManagedStack,
  stopManagedStack,
  validateDeploymentState,
  verifyManagedProcessForStop,
  writeDeploymentState
} from "./process-state.mjs";

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

test("rotateRuntimeLog keeps five archives at 20 MiB", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "df-log-"));
  const logPath = path.join(root, "datafoundry.log");
  await writeFile(logPath, "x".repeat(20 * 1024 * 1024 + 1));
  for (let i = 1; i <= 5; i += 1) {
    await writeFile(`${logPath}.${i}`, `old-${i}`);
  }
  await rotateRuntimeLog(logPath, { maxBytes: 20 * 1024 * 1024, retain: 5 });
  await assert.rejects(() => readFile(`${logPath}.6`, "utf8"), /ENOENT/);
  assert.match(await readFile(`${logPath}.1`, "utf8"), /x/);
  assert.equal(await readFile(logPath, "utf8"), "");
});

test("start and stop managed stack are idempotent and refuse foreign pids", async (t) => {
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

  const foreignRoot = await mkdtemp(path.join(os.tmpdir(), "df-foreign-"));
  await writeDeploymentState(foreignRoot, {
    pid: process.pid,
    launchId: "not-this-process",
    status: "running",
    startedAt: new Date().toISOString(),
    ports: { web: 3000, api: 8787 }
  });
  await assert.rejects(() => stopManagedStack(foreignRoot), /does not identify DataFoundry|launch marker/i);

  await stopManagedStack(root);
  assert.equal(isProcessAlive(started.pid), false);
  assert.equal(await readDeploymentState(root), null);

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
