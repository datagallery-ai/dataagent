import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import { loadEnvFile } from "node:process";
import { parseArgs } from "node:util";
import { fileURLToPath, pathToFileURL } from "node:url";

const ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), "..");

export function venvPython(project, env = process.env, platform = process.platform) {
  const environment = path.resolve(project, env.UV_PROJECT_ENVIRONMENT || ".venv");
  return path.join(environment, platform === "win32" ? "Scripts/python.exe" : "bin/python");
}

function runUv(args, options) {
  return new Promise((resolve, reject) => {
    const child = spawn("uv", args, { ...options, stdio: "inherit" });
    child.on("error", reject);
    child.on("close", code => code === 0 ? resolve() : reject(new Error(`uv sync failed with code ${code}`)));
  });
}

/** Delegate dependency selection, resolution and environment management to uv. */
export function syncPythonEnvironment({ root = ROOT, env = process.env, dev = true, extras = [], run = runUv } = {}) {
  return run(["sync", "--project", path.join(root, "runtime/agentkit"),
    ...(dev ? [] : ["--no-dev"]), ...extras.flatMap(extra => ["--extra", extra])], { cwd: root, env });
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const { values } = parseArgs({ options: { dev: { type: "boolean" }, "no-dev": { type: "boolean" }, extra: { type: "string", multiple: true } } });
  if (existsSync(path.join(ROOT, ".env"))) loadEnvFile(path.join(ROOT, ".env"));
  await syncPythonEnvironment({ dev: !values["no-dev"], extras: values.extra });
}
