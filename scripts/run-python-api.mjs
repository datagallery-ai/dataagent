#!/usr/bin/env node
import { existsSync } from "node:fs";
import { spawn } from "node:child_process";
import { loadEnvFile } from "node:process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { pythonControlPlaneLaunch } from "./stack-runtime-config.mjs";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const envPath = join(root, ".env");
if (existsSync(envPath)) loadEnvFile(envPath);
const launch = pythonControlPlaneLaunch(
  root,
  process.env,
  process.env.npm_lifecycle_event === "dev:api",
);
const child = spawn(launch.command, launch.args, {
  cwd: root,
  env: launch.env,
  stdio: "inherit",
  shell: process.platform === "win32",
});
child.on("exit", (code, signal) => {
  if (signal) process.kill(process.pid, signal);
  process.exit(code ?? 1);
});
