import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { existsSync } from "node:fs";
import { mkdir, open, readFile, rename, rm, stat, writeFile } from "node:fs/promises";
import path from "node:path";

const SENSITIVE_KEY = /KEY|SECRET|TOKEN|PASSWORD|COOKIE|AUTHORIZATION/i;

export function deploymentPaths(root) {
  return {
    logsDir: path.join(root, "storage/logs"),
    runDir: path.join(root, "storage/run"),
    runtimeLog: path.join(root, "storage/logs/datafoundry.log"),
    pidFile: path.join(root, "storage/run/datafoundry.pid"),
    deploymentJson: path.join(root, "storage/run/deployment.json")
  };
}

export function validateDeploymentState(state) {
  if (!state || typeof state !== "object" || Array.isArray(state)) {
    throw new Error("deployment state must be an object");
  }
  for (const key of Object.keys(state)) {
    if (SENSITIVE_KEY.test(key)) {
      throw new Error(`sensitive field ${key}`);
    }
  }
  return state;
}

async function writeAtomicJson(filePath, value) {
  await mkdir(path.dirname(filePath), { recursive: true });
  const tempPath = `${filePath}.${process.pid}.tmp`;
  await writeFile(tempPath, `${JSON.stringify(value, null, 2)}\n`, { encoding: "utf8", mode: 0o600 });
  await rename(tempPath, filePath);
}

export async function writeDeploymentState(root, state) {
  validateDeploymentState(state);
  const paths = deploymentPaths(root);
  await mkdir(paths.runDir, { recursive: true });
  await writeAtomicJson(paths.deploymentJson, state);
  await writeFile(paths.pidFile, `${state.pid}\n`, { encoding: "utf8", mode: 0o600 });
}

export async function readDeploymentState(root) {
  const filePath = deploymentPaths(root).deploymentJson;
  try {
    const raw = await readFile(filePath, "utf8");
    return validateDeploymentState(JSON.parse(raw));
  } catch (error) {
    if (error?.code === "ENOENT") return null;
    throw error;
  }
}

export function isProcessAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

export async function rotateRuntimeLog(logPath, options = {}) {
  const maxBytes = options.maxBytes ?? 20 * 1024 * 1024;
  const retain = options.retain ?? 5;
  await mkdir(path.dirname(logPath), { recursive: true });
  try {
    const info = await stat(logPath);
    if (info.size <= maxBytes) return;
  } catch (error) {
    if (error?.code === "ENOENT") {
      await writeFile(logPath, "", { encoding: "utf8", mode: 0o600 });
      return;
    }
    throw error;
  }

  await rm(`${logPath}.${retain}`, { force: true });
  for (let i = retain - 1; i >= 1; i -= 1) {
    const from = `${logPath}.${i}`;
    const to = `${logPath}.${i + 1}`;
    if (existsSync(from)) await rename(from, to);
  }
  await rename(logPath, `${logPath}.1`);
  await writeFile(logPath, "", { encoding: "utf8", mode: 0o600 });
}

async function readLaunchIdFromProcAsync(pid) {
  try {
    const environ = await readFile(`/proc/${pid}/environ`);
    const match = /DATAFOUNDRY_LAUNCH_ID=([^\0]+)/.exec(environ.toString("utf8"));
    return match?.[1] ?? null;
  } catch {
    return null;
  }
}

export async function verifyManagedProcessForStop(pid, expectedLaunchId, options = {}) {
  if (process.platform !== "linux") {
    return { allowed: true };
  }

  if (!expectedLaunchId) {
    return { allowed: false, reason: "missing-expected-launch-id" };
  }

  const readLaunchId = options.readLaunchId ?? readLaunchIdFromProcAsync;
  const launchId = await readLaunchId(pid);

  if (launchId === expectedLaunchId) {
    return { allowed: true };
  }
  if (launchId && launchId !== expectedLaunchId) {
    return { allowed: false, reason: "launch-id-mismatch" };
  }

  // Never fall back to cmdline heuristics — an unverifiable launch marker must refuse stop.
  return { allowed: false, reason: "launch-id-unverified" };
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export async function startManagedStack(root, options = {}) {
  const existing = await readDeploymentState(root);
  if (existing?.pid && isProcessAlive(existing.pid)) {
    throw new Error(`DataFoundry is already running with pid ${existing.pid}`);
  }

  const paths = deploymentPaths(root);
  await mkdir(paths.logsDir, { recursive: true });
  await mkdir(paths.runDir, { recursive: true });
  await rotateRuntimeLog(paths.runtimeLog);

  const launchId = options.launchId ?? randomBytes(16).toString("hex");
  const command = options.command ?? "npm";
  const args = options.args ?? ["run", "start"];
  const env = {
    ...(options.env ?? process.env),
    DATAFOUNDRY_LAUNCH_ID: launchId
  };

  const logFd = await open(paths.runtimeLog, "a", 0o600);
  let child;
  try {
    child = spawn(command, args, {
      cwd: root,
      env,
      detached: true,
      stdio: ["ignore", logFd.fd, logFd.fd]
    });
  } finally {
    await logFd.close();
  }

  child.unref();

  const state = {
    pid: child.pid,
    pgid: child.pid,
    launchId,
    status: "starting",
    startedAt: new Date().toISOString(),
    commitSha: options.commitSha ?? null,
    ports: options.ports ?? null
  };
  await writeDeploymentState(root, state);
  return state;
}

export async function stopManagedStack(root, options = {}) {
  const state = await readDeploymentState(root);
  if (!state?.pid) return { stopped: false, reason: "not-running" };
  if (!isProcessAlive(state.pid)) {
    await clearDeploymentState(root);
    return { stopped: false, reason: "stale" };
  }

  if (process.platform === "linux") {
    const verification = await verifyManagedProcessForStop(state.pid, state.launchId);
    if (!verification.allowed) {
      throw new Error(
        `Refusing to signal pid ${state.pid}: launch marker does not identify DataFoundry`
      );
    }
  }

  try {
    process.kill(-state.pid, "SIGTERM");
  } catch {
    process.kill(state.pid, "SIGTERM");
  }

  const timeoutMs = options.timeoutMs ?? 15_000;
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (!isProcessAlive(state.pid)) {
      await clearDeploymentState(root);
      return { stopped: true, reason: "terminated" };
    }
    await sleep(100);
  }

  try {
    process.kill(-state.pid, "SIGKILL");
  } catch {
    try {
      process.kill(state.pid, "SIGKILL");
    } catch {
      // process may have exited between checks
    }
  }

  const killTimeoutMs = options.killTimeoutMs ?? 5_000;
  const killDeadline = Date.now() + killTimeoutMs;
  while (Date.now() < killDeadline) {
    if (!isProcessAlive(state.pid)) {
      await clearDeploymentState(root);
      return { stopped: true, reason: "killed" };
    }
    await sleep(100);
  }

  if (!isProcessAlive(state.pid)) {
    await clearDeploymentState(root);
    return { stopped: true, reason: "killed" };
  }

  throw new Error(`Timed out waiting for pid ${state.pid} to exit after SIGKILL`);
}

async function clearDeploymentState(root) {
  const paths = deploymentPaths(root);
  await rm(paths.deploymentJson, { force: true });
  await rm(paths.pidFile, { force: true });
}
