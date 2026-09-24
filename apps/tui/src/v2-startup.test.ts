import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { resolve } from "node:path";
import { it } from "node:test";
import { fileURLToPath } from "node:url";
import { startV2Backend } from "./v2-backend.js";
import { terminalText } from "./utils/terminal-text.js";

const header = `
const identity = {protocol:'dataagent-v2',instanceId:process.env.DATAAGENT_V2_INSTANCE_ID};
const ready = {...identity,type:'ready',runtimeUrl:'http://127.0.0.1:8790/dataagent/stream'};
const emit = value => process.stdout.write(JSON.stringify(value)+'\\n');
process.stdin.resume();
process.stdin.on('end',()=>process.exit(0));
`;
function fake(script: string) {
  return (args: string[], options: { cwd: string; env: NodeJS.ProcessEnv }) =>
    spawn(process.execPath, ["-e", header + script], { ...options, detached: true, stdio: ["pipe", "pipe", "pipe"] });
}

it("startup only forwards explicit paths, user and session without an interactive decision", async () => {
  let captured: string[] = [];
  const cwd = process.env.INIT_CWD ?? process.cwd();
  const backend = await startV2Backend({
    envFile: "provider.env", workspaces: [".", "data"], user: "alice", session: "saved",
    spawnBackend: (args, options) => {
      captured = args;
      assert.equal(options.cwd, cwd);
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
  const backend = await startV2Backend({ spawnBackend: (args, options) => {
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
  await assert.rejects(startV2Backend({
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
    await assert.rejects(startV2Backend({
      spawnBackend: (args, options) => {
        const child = fake(script)(args, options); pid = child.pid; return child;
      },
    }));
    assert(pid);
    assert.throws(() => process.kill(pid!, 0), { code: "ESRCH" });
  });
}

it("backend startup failure preserves a sanitized diagnostic", async () => {
  await assert.rejects(startV2Backend({
    spawnBackend: fake("process.stderr.write('model failed sk-abcdefghijklmnopqrstuv'); process.exit(1);"),
  }), /model failed \[REDACTED\]/);
  assert.equal(terminalText("/中文 path/\x1b[31mbad\n\x07"), "/中文 path/bad");
});
