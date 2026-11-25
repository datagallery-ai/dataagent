import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { copyFile, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

async function fixture(t) {
  const root = await mkdtemp(path.join(tmpdir(), "dataagent-make-contract-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  await copyFile(new URL("../Makefile", import.meta.url), path.join(root, "Makefile"));
  await copyFile(new URL("../LICENSE", import.meta.url), path.join(root, "LICENSE"));
  await mkdir(path.join(root, "scripts"));
  await copyFile(new URL("./package-tui.mjs", import.meta.url), path.join(root, "scripts/package-tui.mjs"));
  for (const folder of ["ui/shared", "ui/tui"]) {
    await mkdir(path.join(root, folder), { recursive: true });
  }
  for (const manifest of ["ui/package.json", "ui/tui/package.json"]) {
    await copyFile(new URL(`../${manifest}`, import.meta.url), path.join(root, manifest));
  }
  await writeFile(path.join(root, "ui/shared/logging.mjs"), "shared module");
  await mkdir(path.join(root, "bin"));
  for (const tool of ["npm", "uv"]) {
    await writeFile(path.join(root, "bin", tool), `#!${process.execPath}
const { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } = require("node:fs");
const { dirname } = require("node:path");
const args = process.argv.slice(2);
if (args[0] === "build" && args.includes("--clear")) rmSync("dist/server", { recursive: true, force: true });
if (args[0] === "pack") {
  const manifest = JSON.parse(readFileSync("package.json", "utf8"));
  if (!manifest.dependencies.yaml || !manifest.dependencies.ink || manifest.devDependencies ||
      !existsSync(manifest.bin["dataagent-cli"]) || manifest.bin.da !== manifest.bin["dataagent-cli"] ||
      !existsSync("ui/shared/logging.mjs") ||
      existsSync("ui/tui/dist/connect.test.js")) process.exit(1);
  writeFileSync("../archive/datagallery-tui.tgz", "TUI tarball");
}
const files = {
  "run build": ["ui/web/.next/BUILD_ID", "ui/web/.next/cache/cache.bin", "ui/web/public/logo.svg",
    "ui/tui/dist/index.js", "ui/tui/dist/connect.test.js"],
  "build --project": ["dist/server/dataagent.whl"],
}[args.slice(0, 2).join(" ")] ?? [];
for (const file of files) {
  mkdirSync(dirname(file), { recursive: true });
  writeFileSync(file, file);
}
console.log(JSON.stringify({ tool: "${tool}", args, python: process.env.UV_PYTHON }));
`, { mode: 0o755 });
  }
  const env = { ...process.env, PATH: `${path.join(root, "bin")}${path.delimiter}${process.env.PATH}` };
  // These fixtures assert standalone Make calls with their own command-line variables.
  for (const variable of ["MAKEFLAGS", "UV_PYTHON", "PYTHON_VERSION", "UV_RUN_OPTS",
    "SCOPE", "TIER", "PYTEST_ARGS", "NODE_TEST_ARGS"]) delete env[variable];
  return { root, env };
}

function invoke(root, env, ...args) {
  const result = spawnSync("make", ["--silent", ...args], { cwd: root, env, encoding: "utf8", timeout: 5000 });
  assert.equal(result.status, 0, result.stderr);
  return result.stdout.trim().split("\n").filter(line => line.startsWith("{")).map(JSON.parse);
}

test("CI prepare, build, ut and st use the configured Python version", async t => {
  if (process.platform === "win32") return t.skip("POSIX make fixture");
  const { root, env } = await fixture(t);
  await mkdir(path.join(root, "dist/server"), { recursive: true });
  await writeFile(path.join(root, "dist/server/old.whl"), "old version");
  for (const target of ["prepare", "build", "ut", "st"]) {
    const calls = invoke(root, env, target, "PYTHON_VERSION=3.12");
    const [call] = calls;
    assert.equal(call.python, "3.12");
    if (target === "prepare") {
      assert.deepEqual(calls, [
        { tool: "npm", args: ["install", "--include=dev"], python: "3.12" },
        { tool: "uv", args: ["sync", "--no-dev"], python: "3.12" },
      ]);
    } else if (target === "build") {
      assert.deepEqual(calls, [
        { tool: "uv", args: ["build", "--project", ".", "--wheel", "--clear", "--out-dir", "dist/server"], python: "3.12" },
        { tool: "npm", args: ["run", "build"], python: "3.12" },
        { tool: "npm", args: ["pack", "--pack-destination", "../archive"], python: "3.12" },
      ]);
      for (const file of [
        "ui/web/.next/BUILD_ID", "ui/tui/dist/index.js", "dist/server/dataagent.whl",
      ]) assert.equal(await readFile(path.join(root, file), "utf8"), file);
      assert.equal(await readFile(path.join(root, "dist/tui/datagallery-tui.tgz"), "utf8"), "TUI tarball");
      await assert.rejects(() => readFile(path.join(root, "dist/web")), { code: "ENOENT" });
      await assert.rejects(() => readFile(path.join(root, "dist/server/old.whl")), { code: "ENOENT" });
    } else {
      assert.deepEqual(calls, [{
        tool: "uv",
        args: ["run", "--no-project", "python", "scripts/run_product_tests.py",
          "--tier", target, "--pytest-args=", "--node-test-args="],
        python: "3.12",
      }]);
    }
  }
});

test("Make preserves suite scope, tier and pytest filters", async t => {
  if (process.platform === "win32") return t.skip("POSIX make fixture");
  const { root, env } = await fixture(t);
  const [call] = invoke(root, env, "ut", "TIER=st", "SCOPE=agentkit", "PYTEST_ARGS=-k 'session and not live'", "UV_RUN_OPTS=--no-sync");
  assert.equal(call.tool, "uv");
  assert(call.args.includes("agentkit"));
  assert(call.args.includes("--pytest-args=-k 'session and not live'"));
  assert.equal(call.args[call.args.indexOf("--tier") + 1], "ut");
  assert.equal(call.args.at(-1), "--no-sync");
});

test("UV_RUN_OPTS cannot replace a Make target's test selection", async t => {
  if (process.platform === "win32") return t.skip("POSIX make fixture");
  const { root, env } = await fixture(t);
  for (const target of ["st", "agentkit"]) {
    const result = spawnSync("make", ["--silent", target, "UV_RUN_OPTS=--no-sync --list --tier=ut"], {
      cwd: root, env, encoding: "utf8", timeout: 5000
    });
    assert.notEqual(result.status, 0);
    assert.equal(result.stdout, "");
    assert.match(result.stderr, /UV_RUN_OPTS only accepts --no-sync/);
  }
});
