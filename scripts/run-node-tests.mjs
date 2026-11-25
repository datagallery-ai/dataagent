import { spawnSync } from "node:child_process";

// npm appends caller arguments after the script. Node reads options before the test files.
const args = process.argv.slice(2);
const separator = args.indexOf("--");
const files = args.slice(0, separator);
const options = args.slice(separator + 1);
const result = spawnSync(process.execPath, ["--test", ...options, ...files], { stdio: "inherit" });
if (result.error) throw result.error;
process.exitCode = result.status ?? 1;
