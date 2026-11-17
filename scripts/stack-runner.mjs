import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { loadEnvFile } from "node:process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { captureOutput, createLogger } from "./lib/logging.mjs";

import {
  formatStackEndpoints,
  httpOrigin,
  pythonControlPlaneLaunch,
  resolveStackRuntimeConfig,
  webLaunch,
} from "./stack-runtime-config.mjs";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");

export function pythonEnvironmentSyncCommand(env = process.env) {
  const python = String(env.UV_PYTHON || env.PYTHON_VERSION || "3.12").trim();
  if (!/^\d+(?:\.\d+){1,2}$/.test(python)) {
    throw new Error(`Python version must look like 3.12, received ${python}`);
  }
  return `uv sync --project runtime/agentkit --frozen --no-dev --python ${python}`;
}

export const PYTHON_ENVIRONMENT_SYNC = pythonEnvironmentSyncCommand({ UV_PYTHON: "3.12" });

export async function controlPlaneIsRunning(baseUrl, fetchImpl = globalThis.fetch) {
  try {
    const response = await fetchImpl(`${baseUrl}/healthz`, { signal: AbortSignal.timeout(800) });
    if (!response.ok) return false;
    const body = await response.json();
    return body?.success === true && !!body.data && typeof body.data === "object" && "conversation" in body.data;
  } catch {
    return false;
  }
}

export async function runStack({ args = [] }) {
  loadRootEnv();
  const apiOnly = args.includes("--api");
  const webOnly = args.includes("--web");
  const oneClick = !apiOnly && !webOnly;
  const runtimeConfig = resolveStackRuntimeConfig(process.env, args);
  const apiBase = httpOrigin(runtimeConfig.apiHost, runtimeConfig.apiPort);
  const apiUp = apiOnly ? false : await controlPlaneIsRunning(apiBase);
  if (oneClick) {
    await ensurePythonEnvironment();
    await ensureArtifact("web");
    await ensureArtifact("tui");
  }

  const startApi = apiOnly || !apiUp;
  const startWeb = !apiOnly;

  const children = [];
  if (startApi) {
    const launch = pythonControlPlaneLaunch(root, process.env, false, runtimeConfig.configPath);
    children.push(spawnProcess("DataGallery API", launch.command, launch.args, launch.env));
  }
  if (startWeb) {
    const launch = webLaunch(runtimeConfig, process.env);
    children.push(spawnProcess("DataGallery Web", launch.command, launch.args, launch.env));
  }

  if (children.length === 0) {
    throw new Error("Nothing to start. Use --api and/or --web.");
  }

  const logger = createLogger({ file: "backend", component: "launcher" });
  const endpoints = formatStackEndpoints(runtimeConfig, {
    startApi: startApi || apiUp,
    startWeb,
    tuiReady: oneClick,
  });
  logger.info(endpoints, { stage: "start" });
  console.log(endpoints);
  let shuttingDown = false;
  const shutdown = (signal) => {
    if (shuttingDown) return;
    shuttingDown = true;
    for (const { child } of children) {
      if (!child.killed) child.kill(signal);
    }
  };

  process.on("SIGINT", () => shutdown("SIGINT"));
  process.on("SIGTERM", () => shutdown("SIGTERM"));
  for (const { child, label, logger: childLogger } of children) {
    child.on("exit", (code, signal) => {
      if (shuttingDown || signal) return;
      const message = `[stack] ${label} exited with code ${code ?? "unknown"}.`;
      childLogger.error(message, { stage: "run" });
      console.error(message);
      shutdown("SIGTERM");
      process.exitCode = code && code !== 0 ? code : 1;
    });
  }
}

export async function ensurePythonEnvironment(rootDir = root, run = runInherited) {
  if (existsSync(join(rootDir, "runtime/agentkit/.venv"))) return false;
  await run(pythonEnvironmentSyncCommand());
  return true;
}

function runInherited(command) {
  return runPreparation(command, "backend", "install");
}

async function ensureArtifact(kind) {
  const ready = kind === "web"
    ? existsSync(join(root, "apps/web/.next/BUILD_ID"))
    : existsSync(join(root, "apps/tui/dist/index.js"));
  if (ready) return;
  await runPreparation(`npm run build:${kind}`, kind, "build");
}

function runPreparation(command, file, stage) {
  const { child } = spawnProcess(file, command, [], process.env, { file, stage, shell: true });
  return new Promise((resolve, reject) => {
    child.on("error", reject);
    child.on("close", code => code === 0 ? resolve() : reject(new Error(`${command} failed with code ${code}`)));
  });
}

function loadRootEnv() {
  const envPath = join(root, ".env");
  if (existsSync(envPath)) loadEnvFile(envPath);
}

function spawnProcess(label, command, args, env, { file = label === "DataGallery Web" ? "web" : "backend", stage = "run", shell = process.platform === "win32" } = {}) {
  const logger = createLogger({ file, component: "launcher" });
  const stdout = captureOutput(logger, { stage, echo: process.stdout });
  const stderr = captureOutput(logger, { stage, echo: process.stderr });
  const child = spawn(command, args, {
    cwd: root,
    stdio: ["inherit", "pipe", "pipe"],
    env: { ...env, DATAAGENT_LOG_CAPTURE: file },
    shell,
  });
  child.stdout.setEncoding("utf8").on("data", chunk => stdout.append(chunk));
  child.stderr.setEncoding("utf8").on("data", chunk => stderr.append(chunk));
  child.on("close", () => { stdout.flush(); stderr.flush(); });
  child.on("error", (error) => logger.error(`Unable to start ${label}`, error, { stage: "start" }));
  return { child, label, logger };
}
