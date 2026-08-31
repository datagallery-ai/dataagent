#!/usr/bin/env node
/**
 * After npm install, compile workspace packages so dev:api can import dist/ exports.
 * Skip in CI when explicitly disabled.
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

if (process.env.SKIP_POSTINSTALL_BUILD === "1") {
  process.exit(0);
}

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const nodeMajor = Number(process.versions.node.split(".")[0]);
if (!Number.isFinite(nodeMajor) || nodeMajor < MIN_NODE_MAJOR) {
  console.warn(`[postinstall] ${nodeVersionMessage(process.versions.node)} Skipping build.`);
  process.exit(0);
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

try {
  execSync("node scripts/seed-dtc-growth-demo.mjs", {
    cwd: root,
    stdio: "inherit",
    env: { ...process.env, SKIP_DTC_GROWTH_REGISTER: "1" },
    shell: true,
  });
} catch {
  console.warn(
    "[postinstall] DTC Growth fixture generation failed — run `npm run seed:dtc-growth-demo` manually.",
  );
}

try {
  execSync("npm run build", {
    cwd: root,
    stdio: "inherit",
    env: process.env,
    shell: true,
  });
} catch {
  console.warn(
    "[postinstall] build failed — run `npm run build` manually before `npm run dev:api`.",
  );
  process.exit(0);
}
