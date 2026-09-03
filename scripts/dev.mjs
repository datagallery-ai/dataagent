#!/usr/bin/env node
/**
 * Start the Python API and Next.js web app.
 *
 * Usage:
 *   npm run dev          # start API and web
 *   npm run dev -- --api # API only
 *   npm run dev -- --web # web only
 */
import { runStack } from "./stack-runner.mjs";

await runStack({ mode: "development", args: process.argv.slice(2) });
