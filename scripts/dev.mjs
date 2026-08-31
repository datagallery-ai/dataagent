#!/usr/bin/env node
/**
 * Build workspace packages and start API + web dev servers (Linux, Windows, macOS).
 *
 * Usage:
 *   npm run dev          # start both
 *   npm run dev -- --api # API only
 *   npm run dev -- --web # web only
 */
import { spawn, execSync } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const args = process.argv.slice(2);
const apiOnly = args.includes("--api");
const webOnly = args.includes("--web");
const startApi = !webOnly || apiOnly;
const startWeb = !apiOnly || webOnly;

execSync("node scripts/ensure-dev-environment.mjs", {
  cwd: root,
  stdio: "inherit",
  env: process.env,
  shell: true,
});

for (const port of [8787, 3000]) {
  try {
    freePort(port);
  } catch {
    // Port may already be free; dev servers will fail loudly if it stays busy.
  }
}

/** @type {import('node:child_process').ChildProcess[]} */
const children = [];

if (startApi) {
  children.push(spawnNpm(["--workspace", "@datafoundry/api", "run", "dev"]));
}
if (startWeb) {
  children.push(spawnNpm(["--workspace", "@datafoundry/web", "run", "dev"]));
}

if (children.length === 0) {
  console.error("Nothing to start. Use --api and/or --web.");
  process.exit(1);
}

console.log(
  "\n[dev] " +
    (startApi ? "API → http://127.0.0.1:8787  " : "") +
    (startWeb ? "Web → http://localhost:3000/data-tasks" : "") +
    "\n",
);

function shutdown(signal) {
  for (const child of children) {
    if (!child.killed) child.kill(signal);
  }
}

process.on("SIGINT", () => shutdown("SIGINT"));
process.on("SIGTERM", () => shutdown("SIGTERM"));

for (const child of children) {
  child.on("exit", (code, signal) => {
    if (signal) return;
    if (code && code !== 0) {
      shutdown("SIGTERM");
      process.exit(code);
    }
  });
}

function spawnNpm(args) {
  return spawn("npm", args, {
    cwd: root,
    stdio: "inherit",
    env: process.env,
    shell: true,
  });
}

function freePort(port) {
  if (process.platform === "win32") {
    let output = "";
    try {
      output = execSync(`netstat -ano | findstr :${port}`, {
        encoding: "utf8",
        shell: true,
        stdio: ["ignore", "pipe", "ignore"],
      });
    } catch {
      return;
    }

    const pids = new Set();
    for (const line of output.split(/\r?\n/u)) {
      if (!/\bLISTENING\b/u.test(line)) continue;
      const pid = line.trim().split(/\s+/u).at(-1);
      if (pid && /^\d+$/u.test(pid) && pid !== "0") {
        pids.add(pid);
      }
    }

    for (const pid of pids) {
      execSync(`taskkill /F /PID ${pid}`, { stdio: "ignore", shell: true });
    }
    return;
  }

  execSync(`fuser -k ${port}/tcp 2>/dev/null || true`, {
    cwd: root,
    stdio: "ignore",
    shell: true,
  });
}
