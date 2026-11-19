import assert from "node:assert/strict";
import { execFile, spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import { mkdtemp, readdir, rm } from "node:fs/promises";
import { createServer } from "node:http";
import { createRequire } from "node:module";
import os from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

const { HttpAgent } = createRequire(new URL("../apps/web/package.json", import.meta.url))("@ag-ui/client");

const ROOT = fileURLToPath(new URL("../", import.meta.url));
const exec = promisify(execFile);
const ANSWER = "已连接打包运行时";

async function listen(server) {
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  return server.address().port;
}

async function reservePort() {
  const server = createServer();
  const port = await listen(server);
  await new Promise(resolve => server.close(resolve));
  return port;
}

function service(command, args, options) {
  const child = spawn(command, args, { ...options, stdio: ["ignore", "pipe", "pipe"] });
  let output = "";
  child.stdout.on("data", chunk => { output += chunk; });
  child.stderr.on("data", chunk => { output += chunk; });
  child.on("error", error => { output += error.message; });
  return { child, output: () => output };
}

async function waitFor(url, process) {
  const deadline = Date.now() + 60_000;
  while (Date.now() < deadline) {
    if (process.child.exitCode !== null || process.child.signalCode) break;
    try {
      const response = await fetch(url, { signal: AbortSignal.timeout(1000) });
      await response.body?.cancel();
      if (response.ok) return;
    } catch { /* The service is still starting. */ }
    await delay(200);
  }
  throw new Error(`Service did not become available at ${url}\n${process.output()}`);
}

async function stop(process) {
  if (!process || process.child.exitCode !== null || process.child.signalCode) return;
  const closed = new Promise(resolve => process.child.once("close", resolve));
  process.child.kill("SIGTERM");
  const timer = setTimeout(() => process.child.kill("SIGKILL"), 5000);
  try { await closed; } finally { clearTimeout(timer); }
}

function modelServer() {
  return createServer(async (request, response) => {
    if (request.url !== "/v1/chat/completions") {
      response.writeHead(404).end();
      return;
    }
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    const body = JSON.parse(Buffer.concat(chunks));
    if (JSON.stringify(body.messages.filter(item => item.role === "user").at(-1)?.content).includes("provider-error")) {
      response.writeHead(500, { "content-type": "application/json" });
      response.end(JSON.stringify({ error: { message: "fixture provider unavailable", type: "server_error" } }));
      return;
    }
    const base = { id: "package-smoke", model: "package-model", created: 1 };
    if (body.stream) {
      response.writeHead(200, { "content-type": "text/event-stream" });
      for (const [delta, finish] of [[{ role: "assistant", content: ANSWER }, null], [{}, "stop"]]) {
        response.write(`data: ${JSON.stringify({ ...base, object: "chat.completion.chunk", choices: [{ index: 0, delta, finish_reason: finish }] })}\n\n`);
      }
      response.end("data: [DONE]\n\n");
    } else {
      response.writeHead(200, { "content-type": "application/json" });
      response.end(JSON.stringify({ ...base, object: "chat.completion", choices: [{ index: 0,
        message: { role: "assistant", content: ANSWER }, finish_reason: "stop" }] }));
    }
  });
}

function input(content) {
  return { threadId: randomUUID(), runId: randomUUID(), messages: [{ id: randomUUID(), role: "user", content }] };
}

function checkEvents(events, fails) {
  const terminal = events.filter(event => ["RUN_FINISHED", "RUN_ERROR"].includes(event.type));
  assert.deepEqual(terminal.map(event => event.type), [fails ? "RUN_ERROR" : "RUN_FINISHED"]);
  if (!fails) assert.equal(events.filter(event => event.type === "TEXT_MESSAGE_CONTENT").map(event => event.delta).join(""), ANSWER);
}

async function main() {
  for (const file of ["apps/web/.next/BUILD_ID", "apps/tui/dist/index.js"]) {
    assert(existsSync(path.join(ROOT, file)), `Run npm run build first: missing ${file}`);
  }
  const temporary = await mkdtemp(path.join(os.tmpdir(), "dataagent-package-"));
  const provider = modelServer();
  let backend, web;
  let passed = false;
  const previousHome = process.env.DATAAGENT_HOME;
  try {
    const providerPort = await listen(provider);
    const env = { ...process.env, DATAAGENT_HOME: path.join(temporary, "home"), DATAAGENT_A2A_AUTH_TOKEN: "",
      LANGSMITH_TRACING: "false", LANGCHAIN_TRACING_V2: "false", OTEL_SDK_DISABLED: "true" };
    delete env.PYTHONPATH;
    delete env.PYTHONHOME;
    process.env.DATAAGENT_HOME = env.DATAAGENT_HOME;
    const run = (command, args, options = {}) => exec(command, args, {
      cwd: ROOT, env, timeout: 180_000, maxBuffer: 2_000_000, ...options,
    });
    const distribution = path.join(temporary, "dist");
    console.log("[smoke:agentkit-package] build wheel and sdist");
    await run(process.execPath, [path.join(ROOT, "scripts/package-agentkit.mjs"), distribution]);
    const constraints = path.join(distribution, "constraints.txt");
    const wheel = (await readdir(distribution)).find(name => name.endsWith(".whl"));
    assert(wheel);
    assert((await readdir(distribution)).some(name => name.endsWith(".tar.gz")));
    const venv = path.join(temporary, "venv");
    await run("uv", ["venv", "--python", env.UV_PYTHON || "3.12", venv]);
    const bin = path.join(venv, process.platform === "win32" ? "Scripts" : "bin");
    const python = path.join(bin, process.platform === "win32" ? "python.exe" : "python");
    const cli = path.join(bin, process.platform === "win32" ? "dataagent.exe" : "dataagent");
    console.log("[smoke:agentkit-package] install into a clean environment");
    await run("uv", ["pip", "install", "--python", python, "-c", constraints, path.join(distribution, wheel)]);
    const apiPort = await reservePort();
    const api = `http://127.0.0.1:${apiPort}`;
    await run(python, ["-I", "-c", `
import json, yaml, os, sys
from pathlib import Path
import dataagent, serve.app, a2a_server
import pymysql, psycopg2, cryptography
from importlib.util import find_spec
from importlib.metadata import version
from dataagent.bootstrap.paths import builtin_plugins_path, initialize_home
assert Path(dataagent.__file__).is_relative_to(Path(sys.prefix))
assert version('datagallery-dataagent') == dataagent.__version__
assert all(find_spec(name) is None for name in ('pyspark', 'av', 'PIL'))
for name in dataagent.__all__: getattr(dataagent, name)
plugins = builtin_plugins_path()
for name in ('data-qa', 'nl2sql', 'de-agent'):
    assert (plugins / name / '.plugin.yaml').is_file()
assert (plugins / 'nl2sql' / 'prompts' / 'routing.md').is_file()
home = Path(os.environ['DATAAGENT_HOME'])
initialize_home(home)
config = yaml.safe_load((home / 'config.yaml').read_text())
config['models']['primary'] = dict(provider='openai', name='package-model', base_url=${JSON.stringify(`http://127.0.0.1:${providerPort}/v1`)}, api_key='fixture-package-key', max_retries=0)
config['server'] = dict(host='127.0.0.1', port=${apiPort})
config['plugins']['enabled'] = ['data-qa']
config['plugins']['config'] = {'data-qa': {'database': 'fixture'}}
(home / 'config.yaml').write_text(yaml.safe_dump(config, allow_unicode=True))
`], { cwd: temporary });
    await run(cli, ["--help"], { cwd: temporary });
    console.log("[smoke:agentkit-package] installed SDK invocation");
    await run(python, ["-I", "-c", `
import asyncio
from dataagent import LaunchOptions, create_agent, prepare_runtime
async def main():
    runtime = prepare_runtime(LaunchOptions())
    async with create_agent(runtime) as agent:
        result = await agent.ainvoke({'messages': [{'role': 'user', 'content': 'package sdk test'}]})
        assert result['messages'][-1].content == ${JSON.stringify(ANSWER)}
asyncio.run(main())
`], { cwd: temporary });
    console.log("[smoke:agentkit-package] start installed runtime outside the checkout");
    backend = service(cli, ["serve"], { cwd: temporary, env });
    await waitFor(api + "/ready", backend);
    assert.equal((await fetch(api + "/healthz")).status, 200);
    assert.equal((await fetch(api + "/.well-known/agent-card.json")).status, 200);
    const a2a = await fetch(api + "/a2a/jsonrpc", { method: "POST",
      headers: { "content-type": "application/json", "A2A-Version": "1.0" },
      body: JSON.stringify({ jsonrpc: "2.0", id: "package-a2a", method: "SendMessage", params: {
        message: { role: "ROLE_USER", messageId: randomUUID(), contextId: randomUUID(), parts: [{ text: "package a2a test" }] },
      } }),
    });
    assert.equal(a2a.status, 200);
    assert.equal((await a2a.json()).result.task.status.state, "TASK_STATE_COMPLETED");
    const webPort = await reservePort();
    const browser = `http://127.0.0.1:${webPort}`;
    web = service(process.execPath, [path.join(ROOT, "apps/web/start.mjs"), "start", "--hostname", "127.0.0.1", "--port", String(webPort)], {
      cwd: path.join(ROOT, "apps/web"), env: { ...env, API_PROXY_TARGET: api },
    });
    await waitFor(browser + "/data-tasks", web);
    console.log("[smoke:agentkit-package] Web proxy: real AG-UI success and provider error");
    let webThread;
    for (const fails of [false, true]) {
      const request = input(fails ? "provider-error" : "package web test");
      if (!fails) webThread = request.threadId;
      const agent = new HttpAgent({ url: browser + "/api/v1/runs", threadId: request.threadId, initialMessages: request.messages });
      const events = [];
      try { await agent.runAgent({ runId: request.runId }, { onEvent: ({ event }) => { events.push(event); } }); }
      catch (error) { if (!fails) throw error; }
      checkEvents(events, fails);
    }
    const { runTui } = await import("../apps/tui/dist/index.js");
    let sourceStarted = false;
    const startBackend = async () => {
      sourceStarted = true;
      throw new Error("Attach mode must not launch source runtime");
    };
    console.log("[smoke:agentkit-package] TUI attach: real AG-UI success, error, and session history");
    assert.equal(await runTui({ argv: ["--runtime-url", api], env,
      startBackend,
      renderApp: async ({ client, tui }) => {
        const restored = await tui.sessions.getSession(webThread);
        assert(restored.messages.some(message => message.content === ANSWER));
        const continuation = { ...input("continue from Web in TUI"), threadId: webThread };
        const continued = [];
        for await (const event of client.runAgent(continuation)) continued.push(event);
        checkEvents(continued, false);
        assert.equal((await tui.sessions.getSession(webThread)).messages.length, 4);
        for (const fails of [false, true]) {
          const request = input(fails ? "provider-error" : "package tui test");
          const events = [];
          for await (const event of client.runAgent(request)) events.push(event);
          checkEvents(events, fails);
          const session = await tui.sessions.getSession(request.threadId);
          assert.equal(session.status, fails ? "error" : "completed");
        }
        return "exit";
      },
    }), 0);
    console.log("[smoke:agentkit-package] stopped backend: verify frontend connection errors");
    await stop(backend);
    const unavailable = await fetch(browser + "/api/v1/sessions");
    assert.equal(unavailable.status, 502);
    assert.equal((await unavailable.json()).error.code, "UPSTREAM_UNAVAILABLE");
    let rendered = false;
    assert.equal(await runTui({ argv: ["--runtime-url", api], env, startBackend,
      renderApp: async () => { rendered = true; return "exit"; },
    }), 1);
    assert.equal(rendered, false, "An unavailable service cannot open a conversation");
    console.log("[smoke:agentkit-package] unconfigured model: healthy service, unavailable conversation");
    const unconfigured = { ...env, DATAAGENT_HOME: path.join(temporary, "unconfigured") };
    for (const key of ["LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY"]) delete unconfigured[key];
    // Keep the same listen address while allowing the CLI to initialize its own templates.
    await run(python, ["-I", "-c", `
import json, yaml, os
from pathlib import Path
from dataagent.bootstrap.paths import initialize_home
home = Path(os.environ['DATAAGENT_HOME'])
initialize_home(home)
config = yaml.safe_load((home / 'config.yaml').read_text())
config['server'] = dict(host='127.0.0.1', port=${apiPort})
(home / 'config.yaml').write_text(yaml.safe_dump(config, allow_unicode=True))
`], { cwd: temporary, env: unconfigured });
    backend = service(cli, ["serve"], { cwd: temporary, env: unconfigured });
    await waitFor(api + "/healthz", backend);
    assert.equal((await fetch(api + "/ready")).status, 503);
    assert.equal((await fetch(browser + "/api/v1/runs", { method: "POST",
      headers: { "content-type": "application/json" }, body: JSON.stringify(input("not ready")),
    })).status, 503);
    assert.equal(await runTui({ argv: ["--runtime-url", api], env, startBackend,
      renderApp: async () => { rendered = true; return "exit"; },
    }), 1);
    assert.equal(rendered, false, "A model must be configured before conversations");
    assert.equal(sourceStarted, false, "Attach mode must not launch source runtime");
    passed = true;
    console.log("[smoke:agentkit-package] ok");
  } finally {
    await stop(web);
    await stop(backend);
    await new Promise(resolve => provider.close(resolve));
    if (previousHome === undefined) delete process.env.DATAAGENT_HOME;
    else process.env.DATAAGENT_HOME = previousHome;
    if (passed) await rm(temporary, { recursive: true, force: true });
    else console.error(`[smoke:agentkit-package] retained environment and logs: ${temporary}`);
  }
}

await main();
