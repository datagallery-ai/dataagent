#!/usr/bin/env node
/**
 * Start the built control plane and web app.
 *
 * Usage:
 *   npm run start       # control plane if needed, web, and a built TUI
 *   npm run start:api   # control plane only
 *   npm run start:web   # web; starts the control plane when it is down
 *
 * Web defaults to 127.0.0.1:3000. Override on start or start:web:
 *   --web-host <host> --web-port <port>
 * Optional product config, used instead of Home config.yaml:
 *   --config <path>
 */
import { runStack } from "./stack-runner.mjs";
import { createLogger, redactLogText } from "./lib/logging.mjs";

try {
  await runStack({ args: process.argv.slice(2) });
} catch (error) {
  createLogger({ file: "backend", component: "launcher" }).error("Stack startup failed", error, { stage: "start" });
  console.error(redactLogText(`[stack] ${error instanceof Error ? error.message : String(error)}`));
  process.exit(1);
}
