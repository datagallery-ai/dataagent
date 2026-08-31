#!/usr/bin/env node
/**
 * Validates the local dev environment before starting API/web.
 * Supports Linux, Windows, and macOS — run npm install on the same OS you dev on.
 */
import { execSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  MIN_NODE_MAJOR,
  detectMixedPlatformInstall,
  localBinExists,
  missingNativeCssPackages,
  nodeVersionMessage,
} from "./platform-hints.mjs";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");

function fail(message) {
  console.error(`\n[dev-env] ${message}\n`);
  process.exit(1);
}

function warn(message) {
  console.warn(`[dev-env] warning: ${message}`);
}

function run(command) {
  execSync(command, {
    cwd: root,
    stdio: "inherit",
    env: process.env,
    shell: true,
  });
}

const nodeMajor = Number(process.versions.node.split(".")[0]);
if (!Number.isFinite(nodeMajor) || nodeMajor < MIN_NODE_MAJOR) {
  fail(nodeVersionMessage(process.versions.node));
}

if (!existsSync(join(root, "node_modules"))) {
  fail("node_modules missing. Run: npm install");
}

for (const bin of ["tsx", "next"]) {
  if (!localBinExists(root, bin)) {
    fail(`${bin} not found in node_modules/.bin — run: npm install`);
  }
}

const mixedInstall = detectMixedPlatformInstall(root);
if (mixedInstall) {
  warn(mixedInstall);
}

for (const pkg of missingNativeCssPackages(root)) {
  warn(
    `${pkg} missing — Tailwind/Next CSS may fail. ` +
      "Re-run npm install on this OS (do not mix Windows and WSL node_modules).",
  );
}

if (process.env.SKIP_DEV_BUILD === "1") {
  console.log("[dev-env] SKIP_DEV_BUILD=1 — skipping package build");
  process.exit(0);
}

console.log("[dev-env] building workspace packages (tsc -b)…");
run("npm run build");

const venvPython = join(root, ".venv", "bin", "python");
if (!existsSync(venvPython)) {
  warn(
    "Python venv missing at .venv — DataFoundry execute_command will not have numpy/pandas/sklearn. " +
      "Run: uv venv .venv --seed && uv pip install -r requirements.txt --python .venv/bin/python",
  );
}
