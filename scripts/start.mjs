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
 */
import { runStack } from "./stack-runner.mjs";

await runStack({ args: process.argv.slice(2) });
