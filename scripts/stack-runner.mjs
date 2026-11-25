import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { loadEnvFile } from "node:process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { captureOutput, createLogger } from "../ui/shared/logging.mjs";

import {
  formatStackEndpoints,
  httpOrigin,
  pythonControlPlaneLaunch,
  resolveStackRuntimeConfig,
  webLaunch,
} from "./stack-runtime-config.mjs";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");

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
  const apiRequested = args.includes("--api");
  const webRequested = args.includes("--web");
  const fullStack = !apiRequested && !webRequested;
  const runtimeConfig = resolveStackRuntimeConfig(process.env, args);
  const apiUp = fullStack && await controlPlaneIsRunning(httpOrigin(runtimeConfig.apiHost, runtimeConfig.apiPort));
  const startApi = apiRequested || (fullStack && !apiUp);
  const startWeb = webRequested || !apiRequested;
  const api = startApi ? pythonControlPlaneLaunch(root, process.env, runtimeConfig.configPath) : null;
  if (api && !existsSync(api.command)) {
    throw new Error(`Python environment is missing at ${api.command}. Run uv sync in the project root.`);
  }
  if (startWeb && !existsSync(join(root, "ui/web/.next/BUILD_ID"))) {
    throw new Error("Web build is missing. Run npm run build:web before starting Web.");
  }

  const children = [];
  if (api) {
    children.push(spawnProcess("DataGallery API", api.command, api.args, api.env, { shell: false }));
  }
  if (startWeb) {
    const launch = webLaunch(runtimeConfig, process.env);
    children.push(spawnProcess("DataGallery Web", launch.command, launch.args, launch.env));
  }

  const logger = createLogger({ file: "backend", component: "launcher" });
  const endpoints = formatStackEndpoints(runtimeConfig, {
    startApi: startApi || apiUp,
    startWeb,
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
    child.on("error", (error) => {
      const message = `Unable to start ${label}`;
      childLogger.error(message, error, { stage: "start" });
      console.error(message);
      process.exitCode = 1;
      shutdown("SIGTERM");
    });
    child.on("exit", (code, signal) => {
      if (shuttingDown) return;
      const message = `[stack] ${label} exited (${signal ?? `code ${code}`}).`;
      childLogger.error(message, { stage: "run" });
      console.error(message);
      shutdown("SIGTERM");
      process.exitCode = code && code !== 0 ? code : 1;
    });
  }
}

function loadRootEnv() {
  const envPath = join(root, ".env");
  if (existsSync(envPath)) loadEnvFile(envPath);
}

function spawnProcess(label, command, args, env, { shell = process.platform === "win32" } = {}) {
  const file = label === "DataGallery Web" ? "web" : "backend";
  const logger = createLogger({ file, component: "launcher" });
  const stdout = captureOutput(logger, { echo: process.stdout });
  const stderr = captureOutput(logger, { echo: process.stderr });
  const child = spawn(command, args, {
    cwd: root,
    stdio: ["inherit", "pipe", "pipe"],
    env: { ...env, DATAAGENT_LOG_CAPTURE: file },
    shell,
  });
  child.stdout.setEncoding("utf8").on("data", chunk => stdout.append(chunk));
  child.stderr.setEncoding("utf8").on("data", chunk => stderr.append(chunk));
  child.on("close", () => { stdout.flush(); stderr.flush(); });
  return { child, label, logger };
}
