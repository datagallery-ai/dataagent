import { execSync, spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { loadEnvFile } from "node:process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  formatStackEndpoints,
  resolveStackRuntimeConfig,
  webProcessEnvironment,
} from "./stack-runtime-config.mjs";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");

export async function runStack({ mode, args = [] }) {
  loadRootEnv();
  const apiOnly = args.includes("--api");
  const webOnly = args.includes("--web");
  const startApi = !webOnly || apiOnly;
  const startWeb = !apiOnly || webOnly;
  const runtimeConfig = resolveStackRuntimeConfig();

  if (mode === "development") {
    execSync("node scripts/ensure-dev-environment.mjs", {
      cwd: root,
      stdio: "inherit",
      env: process.env,
      shell: true,
    });
    const ports = [
      ...(startApi ? [Number(runtimeConfig.API_PORT)] : []),
      ...(startWeb ? [Number(runtimeConfig.WEB_PORT)] : []),
    ];
    for (const port of ports) freePort(port);
  }

  const children = [];
  const startRuntime = startApi && !args.includes("--no-runtime") && !runtimeConfig.RUNTIME_SERVICE_URL;
  if (startRuntime) {
    if (mode === "development") {
      freePort(Number(runtimeConfig.RUNTIME_PORT));
    }
    const runtimeProcess = spawnDeepagentsRuntime(runtimeConfig, process.env);
    if (runtimeProcess) {
      children.push(runtimeProcess);
      runtimeConfig.RUNTIME_SERVICE_URL = `http://${runtimeConfig.RUNTIME_HOST}:${runtimeConfig.RUNTIME_PORT}`;
      await waitForRuntimeHealth(
        runtimeConfig.RUNTIME_SERVICE_URL,
        process.env.RUNTIME_SERVICE_TOKEN,
      );
    }
  }

  if (startApi) {
    const command =
      mode === "development"
        ? ["--workspace", "@datafoundry/api", "run", "dev"]
        : ["--prefix", "apps/api", "run", "start"];
    children.push(spawnProcess("DataFoundry API", "npm", command, { ...process.env, ...runtimeConfig }));
  }
  if (startWeb) {
    const webScript = mode === "development" ? "dev" : "start";
    const command =
      mode === "development"
        ? ["--workspace", "@datafoundry/web", "run", webScript]
        : ["--prefix", "apps/web", "run", webScript];
    const webEnv = {
      ...process.env,
      ...runtimeConfig,
      ...webProcessEnvironment(runtimeConfig),
    };
    children.push(spawnProcess("DataFoundry Web", "npm", command, webEnv));
  }

  if (children.length === 0) {
    throw new Error("Nothing to start. Use --api and/or --web.");
  }

  console.log(formatStackEndpoints(runtimeConfig, {
    startApi,
    startWeb,
    startRuntime: Boolean(runtimeConfig.RUNTIME_SERVICE_URL) && startApi && !args.includes("--no-runtime"),
  }));
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
  for (const { child, label } of children) {
    child.on("exit", (code, signal) => {
      if (shuttingDown || signal) return;
      console.error(`[stack] ${label} exited with code ${code ?? "unknown"}.`);
      shutdown("SIGTERM");
      process.exitCode = code && code !== 0 ? code : 1;
    });
  }
}

function loadRootEnv() {
  const envPath = join(root, ".env");
  if (existsSync(envPath)) loadEnvFile(envPath);
}

function spawnProcess(label, command, args, env, cwd = root) {
  const child = spawn(command, args, {
    cwd,
    stdio: "inherit",
    env,
    shell: process.platform === "win32",
  });
  child.on("error", (error) => console.error(`[stack] Unable to start ${label}: ${error.message}`));
  return { child, label };
}

async function waitForRuntimeHealth(url, token, timeoutMs = 180000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    try {
      const response = await fetch(`${url}/health`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (response.ok) {
        return true;
      }
    } catch {
      // process still starting or uv is syncing
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  console.warn(`[stack] Deep Agents runtime did not become healthy at ${url}; API will still start.`);
  return false;
}

function spawnDeepagentsRuntime(config, env) {
  const serviceDir = join(root, "services", "deepagents-runtime");
  if (!existsSync(join(serviceDir, "pyproject.toml"))) {
    console.warn("[stack] Deep Agents runtime package missing; API will use the in-process stub.");
    return null;
  }
  return spawnProcess(
    "Deep Agents Runtime",
    "uv",
    ["run", "deepagents-runtime"],
    {
      ...env,
      RUNTIME_HOST: config.RUNTIME_HOST,
      RUNTIME_PORT: config.RUNTIME_PORT,
    },
    serviceDir,
  );
}

function freePort(port) {
  try {
    if (process.platform === "win32") {
      const output = execSync(`netstat -ano | findstr :${port}`, {
        encoding: "utf8",
        shell: true,
        stdio: ["ignore", "pipe", "ignore"],
      });
      const pids = new Set();
      for (const line of output.split(/\r?\n/u)) {
        if (!/\bLISTENING\b/u.test(line)) continue;
        const pid = line.trim().split(/\s+/u).at(-1);
        if (pid && /^\d+$/u.test(pid) && pid !== "0") pids.add(pid);
      }
      for (const pid of pids) execSync(`taskkill /F /PID ${pid}`, { stdio: "ignore", shell: true });
      return;
    }
    execSync(`fuser -k ${port}/tcp 2>/dev/null || true`, { cwd: root, stdio: "ignore", shell: true });
  } catch {
    // The port was already free.
  }
}
