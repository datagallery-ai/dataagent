#!/usr/bin/env node
import { runStack } from "./stack-runner.mjs";

await runStack({ mode: "production", args: process.argv.slice(2) });
