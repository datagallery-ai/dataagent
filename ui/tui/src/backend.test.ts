import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { resolve } from "node:path";
import { it } from "node:test";
import { cpSync, copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { fileURLToPath } from "node:url";
import { join } from "node:path";
import {
  startBackend,
} from "./backend.js";
import { terminalText } from "./utils/terminal-text.js";

const header = `
const identity = {protocol:'dataagent',instanceId:process.env.DATAAGENT_INSTANCE_ID};
const ready = {...identity,type:'ready',runtimeUrl:'http://127.0.0.1:8790/api/v1/runs'};
const emit = value => process.stdout.write(JSON.stringify(value)+'\\n');
process.stdin.resume();
process.stdin.on('end',()=>process.exit(0));
`;
function fake(script: string) {
  return (args: string[], options: { cwd: string; env: NodeJS.ProcessEnv }) =>
    spawn(process.execPath, ["-e", header + script], {
      ...options, detached: true, stdio: ["pipe", "pipe", "pipe"], windowsHide: true,
    });
}

it("startup only forwards explicit paths, user and session without an interactive decision", async () => {
  let captured: string[] = [];
  const cwd = process.cwd();
  const backend = await startBackend({
    envFile: "provider.env", workspaces: [".", "data"], user: "alice", session: "saved",
    spawnBackend: (args, options) => {
      captured = args;
      assert.equal(options.cwd, cwd);
      assert.equal(typeof options.env.DATAAGENT_INSTANCE_ID, "string");
      return fake("emit(ready);")(args, options);
    },
  });
  try {
    assert.deepEqual(captured.slice(0, 2), ["serve", "--stdio-ready"]);
    assert(!captured.includes("--config"));
    assert(captured.indexOf("--env-file") > captured.indexOf("serve"));
    assert.equal(captured[captured.indexOf("--env-file") + 1], resolve(cwd, "provider.env"));
    assert.equal(captured.filter(arg => arg === "--workspace").length, 2);
    assert.equal(captured[captured.indexOf("--user") + 1], "alice");
    assert.equal(captured[captured.indexOf("--session") + 1], "saved");
    assert.equal(backend.runtimeUrl, "http://127.0.0.1:8790/api/v1/runs");
  } finally { await backend.stop(); }
});

it("startup JSON handles fragmentation with no input workspaces", async () => {
  const backend = await startBackend({ spawnBackend: (args, options) => {
    assert(!args.includes("--workspace"));
    return fake(`
      const text = JSON.stringify(ready)+'\\n';
      process.stdout.write(text.slice(0,10)); setTimeout(()=>process.stdout.write(text.slice(10)),20);
    `)(args, options);
  } });
  try { assert.equal(backend.signal.aborted, false); }
  finally { await backend.stop(); }
  await backend.stop(); // Idempotent cleanup.
});

it("readiness timeout cleans up the owned process", async () => {
  let pid: number | undefined;
  await assert.rejects(startBackend({
    readinessTimeoutMs: 150,
    spawnBackend: (args, options) => {
      const child = fake("")(args, options); pid = child.pid; return child;
    },
  }), /timed out/);
  assert(pid);
  assert.throws(() => process.kill(pid!, 0), { code: "ESRCH" });
});

for (const [name, script] of [
  ["missing type", "delete ready.type; emit(ready);"],
  ["unknown type", "emit({...ready,type:'mystery'});"],
  ["wrong identity", "emit({...ready,instanceId:'someone-else'});"],
  ["unknown field", "emit({...ready,extra:true});"],
  ["invalid JSON", "process.stdout.write('not-json\\n');"],
  ["oversized line", "process.stdout.write('x'.repeat(65537));"],
  ["duplicate ready", "process.stdout.write([ready,ready].map(JSON.stringify).join(String.fromCharCode(10))+String.fromCharCode(10));"],
] as const) {
  it(`rejects ${name} and cleans up the owned process`, async () => {
    let pid: number | undefined;
    await assert.rejects(startBackend({
      spawnBackend: (args, options) => {
        const child = fake(script)(args, options); pid = child.pid; return child;
      },
    }));
    assert(pid);
    assert.throws(() => process.kill(pid!, 0), { code: "ESRCH" });
  });
}

it("backend startup failure preserves a sanitized diagnostic", async () => {
  await assert.rejects(startBackend({
    spawnBackend: fake("process.stderr.write('model failed sk-abcdefghijklmnopqrstuv'); process.exit(1);"),
  }), /model failed \[REDACTED\]/);
  assert.equal(terminalText("/中文 path/\x1b[31mbad\n\x07"), "/中文 path/bad");
});

it("persists complete backend stderr beyond the display excerpt", async () => {
  const home = mkdtempSync(join(tmpdir(), "backend-output-"));
  const previous = process.env.DATAAGENT_HOME;
  process.env.DATAAGENT_HOME = home;
  try {
    await assert.rejects(startBackend({ spawnBackend: fake(
      "process.stderr.write('ROOT_CAUSE_START\\n' + 'x'.repeat(6000) + '\\nAuthorization: Bearer fixture-token\\n'); process.exit(1);",
    ) }));
    const log = readFileSync(join(home, "runtime/logs/backend.log"), "utf8");
    assert.match(log, /ROOT_CAUSE_START/);
    assert(!log.includes("fixture-token"));
    assert.equal(log.split("ROOT_CAUSE_START").length - 1, 1);
  } finally {
    if (previous === undefined) delete process.env.DATAAGENT_HOME;
    else process.env.DATAAGENT_HOME = previous;
    rmSync(home, { recursive: true, force: true });
  }
});

it("missing Python environments report the preparation command without creating an environment", async () => {
  const directory = mkdtempSync(join(tmpdir(), "tui-missing-python-"));
  const previous = process.env.UV_PROJECT_ENVIRONMENT;
  const environment = join(directory, "missing");
  process.env.UV_PROJECT_ENVIRONMENT = environment;
  try {
    await assert.rejects(startBackend(), /Python environment is missing.*npm run prepare:python/);
    assert.equal(existsSync(environment), false);
  } finally {
    if (previous === undefined) delete process.env.UV_PROJECT_ENVIRONMENT;
    else process.env.UV_PROJECT_ENVIRONMENT = previous;
    rmSync(directory, { recursive: true, force: true });
  }
});

it("startup uses the selected interpreter directly without invoking uv", async t => {
  if (process.platform === "win32") return t.skip("POSIX fixture executable");
  const directory = mkdtempSync(join(tmpdir(), "tui-selected-python-"));
  const previous = process.env.UV_PROJECT_ENVIRONMENT;
  process.env.UV_PROJECT_ENVIRONMENT = directory;
  mkdirSync(join(directory, "bin"));
  writeFileSync(join(directory, "bin/python"), `#!${process.execPath}
require('node:assert/strict').deepEqual(process.argv.slice(2), ['-m', 'dataagent', 'serve', '--stdio-ready']);
${header}
emit(ready);
`, { mode: 0o755 });
  try {
    const backend = await startBackend();
    await backend.stop();
  } finally {
    if (previous === undefined) delete process.env.UV_PROJECT_ENVIRONMENT;
    else process.env.UV_PROJECT_ENVIRONMENT = previous;
    rmSync(directory, { recursive: true, force: true });
  }
});

it("TUI loads project env defaults while preserving terminal model and interpreter settings", () => {
  const root = mkdtempSync(join(tmpdir(), "tui-project-env-"));
  const source = fileURLToPath(new URL("../../../", import.meta.url));
  try {
    for (const file of ["ui/tui/dist/backend.js", "ui/tui/dist/log-path.js", "ui/tui/dist/utils/terminal-text.js",
      "ui/shared/python-environment.mjs", "ui/shared/server-listen.mjs", "ui/shared/config-yaml.mjs",
      "ui/shared/config-env.mjs", "ui/shared/logging.mjs", "ui/shared/log-redaction.mjs"]) {
      mkdirSync(resolve(root, file, ".."), { recursive: true });
      copyFileSync(join(source, file), join(root, file));
    }
    symlinkSync(join(source, "node_modules"), join(root, "node_modules"), process.platform === "win32" ? "junction" : "dir");
    writeFileSync(join(root, "package.json"), '{"type":"module"}');
    writeFileSync(join(root, ".env"), "LLM_MODEL=project-model\nUV_PROJECT_ENVIRONMENT=project-env\n");
    mkdirSync(join(root, "home"));
    writeFileSync(join(root, "home/.env"), "LLM_MODEL=home-model\n");
    const script = `
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { startBackend } from './ui/tui/dist/backend.js';
const backend = await startBackend({ spawnBackend: (args, options) => {
  assert.equal(options.env.LLM_MODEL, process.env.EXPECTED_MODEL);
  assert.equal(options.env.UV_PROJECT_ENVIRONMENT, process.env.EXPECTED_ENVIRONMENT);
  return spawn(process.execPath, ['-e', ${JSON.stringify(header + "emit(ready);")}], {
    ...options, detached: process.platform !== 'win32', stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true,
  });
}});
await backend.stop();
`;
    for (const terminal of [false, true]) {
      const env: NodeJS.ProcessEnv = { ...process.env, DATAAGENT_HOME: join(root, "home"),
        EXPECTED_MODEL: terminal ? "terminal-model" : "project-model",
        EXPECTED_ENVIRONMENT: terminal ? "terminal-env" : "project-env" };
      delete env.LLM_MODEL;
      delete env.UV_PROJECT_ENVIRONMENT;
      if (terminal) {
        env.LLM_MODEL = "terminal-model";
        env.UV_PROJECT_ENVIRONMENT = "terminal-env";
      }
      const result = spawnSync(process.execPath, ["--input-type=module", "-e", script], {
        cwd: root, env, encoding: "utf8", timeout: 10_000,
      });
      assert.equal(result.status, 0, result.stderr);
    }
    assert.equal(readFileSync(join(root, "home/.env"), "utf8"), "LLM_MODEL=home-model\n");
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});


it("default runTui resolves root dotenv before probing and preserves startup overrides", () => {
  const root = mkdtempSync(join(tmpdir(), "tui-root-listen-"));
  const source = fileURLToPath(new URL("../../../", import.meta.url));
  try {
    cpSync(join(source, "ui/tui/dist"), join(root, "ui/tui/dist"), { recursive: true });
    symlinkSync(join(source, "ui/tui/node_modules"), join(root, "ui/tui/node_modules"), process.platform === "win32" ? "junction" : "dir");
    cpSync(join(source, "ui/shared"), join(root, "ui/shared"), { recursive: true });
    symlinkSync(join(source, "node_modules"), join(root, "node_modules"), process.platform === "win32" ? "junction" : "dir");
    writeFileSync(join(root, "package.json"), '{"type":"module"}');
    writeFileSync(join(root, ".env"), "PR670_BIND_HOST=127.0.0.1\n");
    mkdirSync(join(root, "home"));
    writeFileSync(join(root, "home/config.yaml"), "server:\n  host: $env{PR670_BIND_HOST}\n  port: 8790\n");
    writeFileSync(join(root, "selected.env"), "PR670_SELECTED_HOST=127.0.0.3\n");
    const script = `
import assert from 'node:assert/strict';
import { runTui } from './ui/tui/dist/index.js';
let started = 0;
let probed = '';
const mode = process.env.TEST_MODE;
const argv = mode === 'remote' ? ['--runtime-url', 'http://127.0.0.4:8790'] : mode === 'selected' ? ['--env-file', 'selected.env'] : [];
const code = await runTui({ argv, startBackend: async options => {
  started++;
  if (mode === 'selected') assert.equal(options.envFile, 'selected.env');
  throw new Error('fixture reached backend');
}, fetchImpl: async url => { probed = String(url); throw new Error('fixture offline'); } });
assert.equal(code, 1);
assert.equal(started, mode === 'remote' ? 0 : 1);
if (mode === 'root' || mode === 'process') assert.equal(probed, 'http://' + (mode === 'process' ? '127.0.0.2' : '127.0.0.1') + ':8790/healthz');
if (mode === 'remote') assert.equal(probed, 'http://127.0.0.4:8790/healthz');
`;
    for (const mode of ["root", "process", "selected", "remote"]) {
      const env: NodeJS.ProcessEnv = { ...process.env, DATAAGENT_HOME: join(root, "home"), TEST_MODE: mode };
      delete env.PR670_BIND_HOST;
      if (mode === "process") env.PR670_BIND_HOST = "127.0.0.2";
      const result = spawnSync(process.execPath, ["--input-type=module", "-e", script], { cwd: root, env, encoding: "utf8" });
      assert.equal(result.status, 0, result.stderr);
    }
  } finally { rmSync(root, { recursive: true, force: true }); }
});
