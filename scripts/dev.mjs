#!/usr/bin/env node
/**
 * Build workspace packages and start API + web + Deep Agents runtime.
 *
 * Usage:
 *   npm run dev          # start API, web, and runtime
 *   npm run dev -- --api # API + runtime
 *   npm run dev -- --web # web only
 *   npm run dev -- --no-runtime
 */
import { runStack } from "./stack-runner.mjs";

await runStack({ mode: "development", args: process.argv.slice(2) });
