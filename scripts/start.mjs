#!/usr/bin/env node
/**
 * Start the built control plane and web app.
 *
 * Usage:
 *   npm run start       # web and control plane, reusing a running control plane
 *   npm run start:web   # web only; connect to a separately started control plane
 *
 * Web defaults to 127.0.0.1:3000. Override on start or start:web:
 *   --web-host <host> --web-port <port>
 * Optional product config, used instead of Home config.yaml:
 *   --config <path>
 */
import { runStack } from "./stack-runner.mjs";
import { createLogger, redactLogText } from "../ui/shared/logging.mjs";

try {
  await runStack({ args: process.argv.slice(2) });
} catch (error) {
  createLogger({ file: "backend", component: "launcher" }).error("Stack startup failed", error, { stage: "start" });
  console.error(redactLogText(`[stack] ${error instanceof Error ? error.message : String(error)}`));
  process.exit(1);
}
