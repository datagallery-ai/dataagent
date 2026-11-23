import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { syncPythonEnvironment } from "./python-environment.mjs";
import { venvPython } from "../ui/shared/python-environment.mjs";

for (const dev of [true, false]) {
  test(`native preparation ${dev ? "uses default groups" : "excludes development dependencies"}`, async () => {
    const root = path.join(tmpdir(), "project with spaces");
    const env = { UV_PYTHON: "/opt/python/bin/python", UV_PROJECT_ENVIRONMENT: "custom env" };
    const calls = [];
    await syncPythonEnvironment({ root, env, dev, extras: ["spark", "video"], run: async (args, options) => {
      calls.push(args);
      assert.equal(options.cwd, root);
      assert.equal(options.env, env);
    } });
    assert.deepEqual(calls, [["sync", "--project", path.join(root, "."),
      ...(dev ? [] : ["--no-dev"]), "--extra", "spark", "--extra", "video"]]);
    assert.equal(venvPython(path.join(root, "."), env, "win32"), path.join(root, "custom env/Scripts/python.exe"));
  });
}

test("interpreter and extra values are never evaluated by a shell", async t => {
  if (process.platform === "win32") return t.skip("POSIX fixture executable");
  const root = await mkdtemp(path.join(tmpdir(), "dataagent-python-args-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const argv = path.join(root, "argv.json");
  const marker = path.join(root, "unexpected");
  await writeFile(path.join(root, "uv"), `#!${process.execPath}\nrequire('node:fs').writeFileSync(process.env.ARGV_FILE, JSON.stringify({args:process.argv.slice(2),python:process.env.UV_PYTHON}));\n`, { mode: 0o755 });
  const python = `/opt/python; touch ${marker}`;
  const extra = `video; touch ${marker}`;
  await syncPythonEnvironment({ root, extras: [extra], env: { ...process.env, PATH: root, ARGV_FILE: argv, UV_PYTHON: python } });
  const call = JSON.parse(await readFile(argv, "utf8"));
  assert.equal(call.python, python);
  assert.equal(call.args.at(-1), extra);
  assert.equal(existsSync(marker), false);
});
