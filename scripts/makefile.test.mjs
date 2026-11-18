import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { copyFile, mkdir, mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";

const variables = ["UV_PYTHON", "PYTHON_VERSION", "UV_DEFAULT_INDEX", "UV_INDEX_URL", "PIP_INDEX_URL", "UV_INDEX", "UV_EXTRA_INDEX_URL", "PIP_EXTRA_INDEX_URL", "UV_FIND_LINKS", "PIP_FIND_LINKS", "npm_config_registry", "NPM_CONFIG_REGISTRY", "NPM_REGISTRY"];

async function fixture(t) {
  const root = await mkdtemp(path.join(tmpdir(), "dataagent-make-contract-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  await copyFile(new URL("../Makefile", import.meta.url), path.join(root, "Makefile"));
  await mkdir(path.join(root, "bin"));
  await writeFile(path.join(root, "bin/npm"), `#!${process.execPath}
console.log(JSON.stringify({ args: process.argv.slice(2), env: Object.fromEntries(${JSON.stringify(variables)}.filter(key => process.env[key] !== undefined).map(key => [key, process.env[key]])), skip: process.env.SKIP_POSTINSTALL_BUILD ?? null }));
`, { mode: 0o755 });
  const env = { ...process.env, PATH: `${path.join(root, "bin")}${path.delimiter}${process.env.PATH}`, SKIP_POSTINSTALL_BUILD: "1" };
  for (const key of variables) delete env[key];
  return { root, env };
}

function invoke(root, env, ...args) {
  const result = spawnSync("make", ["--silent", ...args], { cwd: root, env, encoding: "utf8", timeout: 5000 });
  assert.equal(result.status, 0, result.stderr);
  return result.stdout.trim().split("\n").filter(line => line.startsWith("{")).map(JSON.parse);
}

test("CI prepare, build, package, ut and st retain their command interface", async t => {
  if (process.platform === "win32") return t.skip("POSIX make fixture");
  const { root, env } = await fixture(t);
  for (const target of ["prepare", "build", "package", "ut", "st"]) {
    const [call] = invoke(root, env, target, "PYTHON_VERSION=3.12");
    assert.equal(call.env.UV_PYTHON, "3.12");
    if (target === "prepare") {
      assert.deepEqual(call.args, ["ci"]);
      assert.equal(call.skip, null);
    } else if (target === "ut" || target === "st") {
      assert.deepEqual(call.args.slice(0, 5), ["run", "test:product", "--", "--tier", target]);
    } else assert.equal(call.args[0], "run");
  }
});

test("legacy Make source parameters map to native tool configuration", async t => {
  if (process.platform === "win32") return t.skip("POSIX make fixture");
  const { root, env } = await fixture(t);
  const [call] = invoke(root, env, "prepare", "PYTHON_VERSION=3.12", "PIP_INDEX_URL=https://mirror.example/simple/", "PIP_EXTRA_INDEX_URL=https://extra.example/simple/", "PIP_FIND_LINKS=/opt/wheels", "NPM_REGISTRY=https://mirror.example/npm/");
  assert.equal(call.env.UV_DEFAULT_INDEX, "https://mirror.example/simple/");
  assert.equal(call.env.UV_INDEX, "https://extra.example/simple/");
  assert.equal(call.env.UV_FIND_LINKS, "/opt/wheels");
  assert.equal(call.env.npm_config_registry, "https://mirror.example/npm/");
  assert.equal(call.env.UV_INDEX_URL, undefined);
});

test("native tool selectors take precedence over compatibility aliases", async t => {
  if (process.platform === "win32") return t.skip("POSIX make fixture");
  const { root, env } = await fixture(t);
  const native = { UV_PYTHON: "/opt/python 3.12/bin/python", UV_DEFAULT_INDEX: "https://native.example/simple/", UV_INDEX: "https://native-extra.example/simple/", UV_FIND_LINKS: "/opt/native-wheels", npm_config_registry: "https://native.example/npm/" };
  const [call] = invoke(root, { ...env, ...native }, "prepare", "PYTHON_VERSION=3.11", "PIP_INDEX_URL=https://legacy.example/simple/", "PIP_EXTRA_INDEX_URL=https://legacy-extra.example/simple/", "PIP_FIND_LINKS=/opt/legacy-wheels", "NPM_REGISTRY=https://legacy.example/npm/");
  for (const [key, value] of Object.entries(native)) assert.equal(call.env[key], value);
});

test("Make preserves suite scope, tier and pytest filters", async t => {
  if (process.platform === "win32") return t.skip("POSIX make fixture");
  const { root, env } = await fixture(t);
  const [call] = invoke(root, env, "ut", "TIER=st", "SCOPE=agentkit", "PYTEST_ARGS=-k session and not live", "UV_RUN_OPTS=--no-sync");
  assert(call.args.includes("agentkit"));
  assert(call.args.includes("--pytest-args=-k session and not live"));
  assert.equal(call.args[call.args.indexOf("--tier") + 1], "ut");
  assert.equal(call.args.at(-1), "--no-sync");
});


test("uppercase native npm source configuration wins over its Make alias", async t => {
  if (process.platform === "win32") return t.skip("POSIX make fixture");
  const { root, env } = await fixture(t);
  const [call] = invoke(root, { ...env, NPM_CONFIG_REGISTRY: "https://native.example/npm/" }, "prepare", "NPM_REGISTRY=https://legacy.example/npm/");
  assert.equal(call.env.npm_config_registry, "https://native.example/npm/");
});
