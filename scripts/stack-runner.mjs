import { execFileSync, execSync, spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { loadEnvFile } from "node:process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { datalinkEnabled } from "./datalink-stack-config.mjs";
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
  if (datalinkEnabled()) {
    throw new Error(
      "DATALINK_ENABLED=true is no longer supported: the bundled DataLink service was removed. Set DATALINK_ENABLED=false (or unset it).",
    );
  }
  const startDatalink = false;
  const runtimeConfig = resolveStackRuntimeConfig();
  const datalinkEnv = runtimeConfig;

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
      ...(startDatalink
        ? [Number(datalinkEnv.DATALINK_MCP_PORT), Number(datalinkEnv.DATALINK_API_PORT)]
        : []),
    ];
    for (const port of ports) freePort(port);
  }

  if (startDatalink) ensureUvAvailable();

  const children = [];
  if (startDatalink) {
    children.push(spawnProcess("DataLink MCP", "uv", [
      "run", "--project", "services/datalink", "datalink", "serve",
      "--host", datalinkEnv.DATALINK_MCP_HOST,
      "--port", datalinkEnv.DATALINK_MCP_PORT,
      "--transport", "streamable-http",
      "--db", datalinkEnv.DATALINK_GRAPH_DB_PATH,
    ], datalinkEnv));
    children.push(spawnProcess("DataLink REST", "uv", [
      "run", "--project", "services/datalink", "datalink", "api",
      "--host", datalinkEnv.DATALINK_API_HOST,
      "--port", datalinkEnv.DATALINK_API_PORT,
    ], datalinkEnv));
  }
  if (startApi) {
    const command = mode === "development"
      ? ["--workspace", "@datafoundry/api", "run", "dev"]
      : ["--prefix", "apps/api", "run", "start"];
    children.push(spawnProcess("DataFoundry API", "npm", command, datalinkEnv));
  }
  if (startWeb) {
    const webScript = mode === "development" ? "dev" : "start";
    const command = mode === "development"
      ? ["--workspace", "@datafoundry/web", "run", webScript]
      : ["--prefix", "apps/web", "run", webScript];
    const webEnv = { ...datalinkEnv, ...webProcessEnvironment(runtimeConfig) };
    children.push(spawnProcess("DataFoundry Web", "npm", command, webEnv));
  }

  if (children.length === 0) {
    throw new Error("Nothing to start. Use --api and/or --web.");
  }

  console.log(formatStackEndpoints(runtimeConfig, { startApi, startDatalink, startWeb }));
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

function ensureUvAvailable() {
  try {
    execFileSync("uv", ["--version"], { stdio: "ignore" });
  } catch {
    throw new Error(
      "DATALINK_ENABLED=true requires uv and Python 3.10+. Install uv, then run `npm run install:datalink`.",
    );
  }
}

function spawnProcess(label, command, args, env) {
  const child = spawn(command, args, {
    cwd: root,
    stdio: "inherit",
    env,
    shell: process.platform === "win32",
  });
  child.on("error", (error) => console.error(`[stack] Unable to start ${label}: ${error.message}`));
  return { child, label };
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
