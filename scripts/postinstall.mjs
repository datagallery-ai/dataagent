#!/usr/bin/env node
/**
 * After npm install, sync the Python control-plane environment.
 * CI sets SKIP_POSTINSTALL_BUILD=1 and does not need this download.
 */
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { existsSync } from "node:fs";
import { loadEnvFile } from "node:process";

import {
  isSupportedNodeVersion,
  detectMixedPlatformInstall,
  missingNativeCssPackages,
  nodeVersionMessage,
} from "./platform-hints.mjs";
import { syncPythonEnvironment } from "./python-environment.mjs";

if (process.env.SKIP_POSTINSTALL_BUILD === "1") {
  process.exit(0);
}

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
if (existsSync(join(root, ".env"))) loadEnvFile(join(root, ".env"));
if (!isSupportedNodeVersion(process.versions.node)) {
  throw new Error(`[postinstall] ${nodeVersionMessage(process.versions.node)}`);
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

await syncPythonEnvironment({ root, dev: false });
