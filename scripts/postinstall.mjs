#!/usr/bin/env node
/**
 * After npm install, sync the Python control-plane environment.
 * CI sets SKIP_POSTINSTALL_BUILD=1 and does not need this download.
 */
import { execSync } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  MIN_NODE_MAJOR,
  detectMixedPlatformInstall,
  missingNativeCssPackages,
  nodeVersionMessage,
} from "./platform-hints.mjs";
import { pythonEnvironmentSyncCommand } from "./stack-runner.mjs";

if (process.env.SKIP_POSTINSTALL_BUILD === "1") {
  process.exit(0);
}

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const nodeMajor = Number(process.versions.node.split(".")[0]);
if (!Number.isFinite(nodeMajor) || nodeMajor < MIN_NODE_MAJOR) {
  console.warn(`[postinstall] ${nodeVersionMessage(process.versions.node)}`);
}

const mixedInstall = detectMixedPlatformInstall(root);
if (mixedInstall) {
  console.warn(`[postinstall] warning: ${mixedInstall}`);
}

for (const pkg of missingNativeCssPackages(root)) {
  console.warn(
    `[postinstall] warning: ${pkg} missing — if Next/Tailwind fails later, ` +
      "remove node_modules and run npm install on this OS.",
  );
}

execSync(pythonEnvironmentSyncCommand(), {
  cwd: root,
  stdio: "inherit",
  env: process.env,
  shell: true,
});
