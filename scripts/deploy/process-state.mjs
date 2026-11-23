import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { mkdir, readFile, rename, rm, writeFile } from "node:fs/promises";
import path from "node:path";

import { createLogger, logPath } from "../../ui/shared/logging.mjs";

const SENSITIVE_KEY = /KEY|SECRET|TOKEN|PASSWORD|COOKIE|AUTHORIZATION/i;

export function deploymentPaths(root, env = process.env) {
  return {
    logsDir: path.dirname(logPath("backend", env)),
    runDir: path.join(root, "storage/run"),
    runtimeLog: logPath("backend", env),
    pidFile: path.join(root, "storage/run/datagallery.pid"),
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

async function readLaunchIdFromProcAsync(pid) {
  try {
    const environ = await readFile(`/proc/${pid}/environ`);
    const match = /DATAAGENT_LAUNCH_ID=([^\0]+)/.exec(environ.toString("utf8"));
    return match?.[1] ?? null;
  } catch {
    return null;
  }
}

export async function verifyManagedProcessForStop(pid, expectedLaunchId, options = {}) {
  if (process.platform !== "linux" && !options.forceLaunchIdCheck) {
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

async function clearDeploymentState(root) {
  const paths = deploymentPaths(root);
  await rm(paths.deploymentJson, { force: true });
  await rm(paths.pidFile, { force: true });
}

/**
 * Returns whether the recorded deployment process is a verified managed stack.
 * Alive pid + failed launchId verification is treated as stale (not running).
 */
export async function inspectManagedRuntime(root, options = {}) {
  const state = await readDeploymentState(root);
  if (!state?.pid) {
    return { state: null, running: false, stale: false, reason: "not-running" };
  }
  if (!isProcessAlive(state.pid)) {
    return { state, running: false, stale: true, reason: "dead-pid" };
  }

  if (process.platform === "linux" || options.forceLaunchIdCheck) {
    const verification = await verifyManagedProcessForStop(state.pid, state.launchId, options);
    if (!verification.allowed) {
      return {
        state,
        running: false,
        stale: true,
        reason: verification.reason ?? "stale-launch-id"
      };
    }
  }

  return { state, running: true, stale: false, reason: "running" };
}

export async function healStaleDeploymentState(root, options = {}) {
  const inspection = await inspectManagedRuntime(root, options);
  if (!inspection.stale) return inspection;
  await clearDeploymentState(root);
  return { ...inspection, state: null, healed: true };
}

export async function startManagedStack(root, options = {}) {
  const existing = await readDeploymentState(root);
  if (existing?.pid && isProcessAlive(existing.pid)) {
    const healed = await healStaleDeploymentState(root, options);
    if (!healed.stale) {
      throw new Error(`DataGallery is already running with pid ${existing.pid}`);
    }
  } else if (existing?.pid && !isProcessAlive(existing.pid)) {
    await clearDeploymentState(root);
  }

  const paths = deploymentPaths(root, options.env ?? process.env);
  await mkdir(paths.logsDir, { recursive: true });
  await mkdir(paths.runDir, { recursive: true });
  const logger = createLogger({ component: "launcher", env: options.env ?? process.env });
  logger.info("Starting managed stack", { stage: "start" });

  const launchId = options.launchId ?? randomBytes(16).toString("hex");
  const command = options.command ?? process.execPath;
  const requested = options.args ?? [];
  const args = options.command ? requested : [path.join(root, "scripts/start.mjs"), ...requested];
  const env = {
    ...(options.env ?? process.env),
    DATAAGENT_LAUNCH_ID: launchId
  };

  const child = spawn(command, args, {
    cwd: root, env, detached: true, stdio: "ignore",
  });
  await new Promise((resolve, reject) => {
    child.once("spawn", resolve);
    child.once("error", error => { logger.error("Managed stack could not start", error, { stage: "start" }); reject(error); });
  });
  child.unref();

  const state = {
    pid: child.pid,
    pgid: child.pid,
    launchId,
    status: "starting",
    startedAt: new Date().toISOString(),
    commitSha: options.commitSha ?? null,
    ports: options.ports ?? null,
    listen: options.listen ?? null
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

  if (process.platform === "linux" || options.forceLaunchIdCheck) {
    const verification = await verifyManagedProcessForStop(state.pid, state.launchId, options);
    if (!verification.allowed) {
      // PID reuse / foreign process: never signal, but clear stale state so start can recover.
      await clearDeploymentState(root);
      return {
        stopped: false,
        reason: "stale-launch-id",
        detail: verification.reason
      };
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
