#!/usr/bin/env node
import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const serviceDir = join(root, "services", "deepagents-runtime");

if (!existsSync(join(serviceDir, "pyproject.toml"))) {
  console.error("[deepagents-runtime] missing services/deepagents-runtime/pyproject.toml");
  process.exit(1);
}

const child = spawn("uv", ["run", "deepagents-runtime"], {
  cwd: serviceDir,
  env: process.env,
  stdio: "inherit",
  shell: process.platform === "win32",
});

child.on("error", (error) => {
  console.error(`[deepagents-runtime] unable to start: ${error.message}`);
  console.error("Install uv, then run: cd services/deepagents-runtime && uv sync");
  process.exit(1);
});

child.on("exit", (code, signal) => {
  if (signal) {
    process.kill(process.pid, signal);
    return;
  }
  process.exit(code ?? 0);
});
