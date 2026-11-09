import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { resolve } from "node:path";
import { it } from "node:test";
import { fileURLToPath } from "node:url";
import {
  ensureWindowsBackendPython,
  startBackend,
  windowsVenvPython,
} from "./backend.js";
import { terminalText } from "./utils/terminal-text.js";

const header = `
const identity = {protocol:'dataagent',instanceId:process.env.DATAAGENT_INSTANCE_ID};
const ready = {...identity,type:'ready',runtimeUrl:'http://127.0.0.1:8790/dataagent/stream'};
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
    assert.deepEqual(captured.slice(0, 6), [
      "run", "--project", fileURLToPath(new URL("../../../runtime/agentkit", import.meta.url)),
      "dataagent", "serve", "--stdio-ready",
    ]);
    assert(!captured.includes("--config"));
    assert(captured.indexOf("--env-file") > captured.indexOf("serve"));
    assert.equal(captured[captured.indexOf("--env-file") + 1], resolve(cwd, "provider.env"));
    assert.equal(captured.filter(arg => arg === "--workspace").length, 2);
    assert.equal(captured[captured.indexOf("--user") + 1], "alice");
    assert.equal(captured[captured.indexOf("--session") + 1], "saved");
    assert.equal(backend.runtimeUrl, "http://127.0.0.1:8790/dataagent/stream");
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

function fakeSyncChild(result: {
  code: number | null;
  stderr?: string;
  error?: Error;
}): ChildProcessWithoutNullStreams {
  const child = new EventEmitter() as ChildProcessWithoutNullStreams;
  const stderr = new EventEmitter() as ChildProcessWithoutNullStreams["stderr"];
  (stderr as { setEncoding: (encoding: BufferEncoding) => typeof stderr }).setEncoding = () => stderr;
  child.stderr = stderr;
  queueMicrotask(() => {
    if (result.error) {
      child.emit("error", result.error);
      return;
    }
    if (result.stderr) stderr.emit("data", result.stderr);
    child.emit("close", result.code);
  });
  return child;
}

it("Windows ensure always uv-syncs even when the venv interpreter already exists", async () => {
  const backendPackage = "C:\\fake\\agentkit";
  const python = windowsVenvPython(backendPackage);
  const syncCalls: string[][] = [];
  const path = await ensureWindowsBackendPython(
    backendPackage,
    { cwd: "C:\\cwd", env: { PATH: "x" } },
    {
      existsSync: (candidate) => candidate === python,
      spawn: ((command, args, options) => {
        assert.equal(command, "uv");
        syncCalls.push(args as string[]);
        assert.equal(options?.windowsHide, true);
        assert.deepEqual(options?.stdio, ["ignore", "ignore", "pipe"]);
        return fakeSyncChild({ code: 0 });
      }) as typeof spawn,
    },
  );
  assert.equal(path, python);
  assert.deepEqual(syncCalls, [["sync", "--project", backendPackage]]);
});

it("Windows ensure rejects when sync fails despite a pre-existing interpreter", async () => {
  const backendPackage = "/tmp/agentkit";
  const python = windowsVenvPython(backendPackage);
  await assert.rejects(
    ensureWindowsBackendPython(
      backendPackage,
      { cwd: "/tmp", env: {} },
      {
        // Interpreter present (empty/partial/outdated venv) but sync reports failure.
        existsSync: (candidate) => candidate === python,
        spawn: (() => fakeSyncChild({
          code: 1,
          stderr: "error: Failed to install pydantic==2.12.0",
        })) as unknown as typeof spawn,
      },
    ),
    /Failed to install pydantic/,
  );
});

it("Windows ensure rejects when sync succeeds but the interpreter is still missing", async () => {
  await assert.rejects(
    ensureWindowsBackendPython(
      "/tmp/agentkit",
      { cwd: "/tmp", env: {} },
      {
        existsSync: () => false,
        spawn: (() => fakeSyncChild({ code: 0 })) as unknown as typeof spawn,
      },
    ),
    /uv sync failed/,
  );
});
