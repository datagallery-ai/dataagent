import { execFileSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../", import.meta.url));
const output = path.resolve(root, process.argv[2] ?? "dist/agentkit");
const options = { cwd: root, stdio: "inherit" };

execFileSync("uv", ["build", "--project", ".", "--out-dir", output], options);
execFileSync("uv", ["export", "--project", ".", "--no-dev", "--all-extras",
  "--no-emit-project", "--no-hashes", "--output-file", path.join(output, "constraints.txt")], options);
