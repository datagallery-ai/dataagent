import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { controlPlaneIsRunning, ensurePythonEnvironment, PYTHON_ENVIRONMENT_SYNC, pythonEnvironmentSyncCommand } from "./stack-runner.mjs";
import {
  formatStackEndpoints,
  pythonControlPlaneLaunch,
  resolveStackRuntimeConfig,
  webLaunch,
} from "./stack-runtime-config.mjs";

test("uses native deployment defaults", () => {
  const config = resolveStackRuntimeConfig({ DATAAGENT_HOME: "/no/such/dataagent-home" });
  assert.equal(config.apiHost, "127.0.0.1");
  assert.equal(config.apiPort, "8790");
  assert.equal(config.webHost, "127.0.0.1");
  assert.equal(config.webPort, "3000");
});

test("web launch passes host and port only as next arguments", () => {
  const config = resolveStackRuntimeConfig({}, ["--web-host", "127.0.0.2", "--web-port", "3310"]);
  const launch = webLaunch(config, { PATH: "/usr/bin", HOSTNAME: "10.0.0.8", PORT: "9" });
  assert.equal(launch.command, "npm");
  assert.deepEqual(launch.args, [
    "--prefix", "apps/web", "run", "start", "--",
    "--hostname", "127.0.0.2",
    "--port", "3310",
  ]);
  assert.equal(launch.env.PATH, "/usr/bin");
  assert.equal(launch.env.HOSTNAME, "10.0.0.8");
  assert.equal(launch.env.PORT, "9");
  assert.equal(launch.env.API_PROXY_TARGET, `http://${config.apiHost}:${config.apiPort}`);
});

test("prints actual configured endpoints", () => {
  const config = resolveStackRuntimeConfig({
    DATAAGENT_HOME: "/no/such/dataagent-home",
    DATAAGENT_CONFIG: new URL("./fixtures/server-listen.json", import.meta.url).pathname,
  }, ["--web-port", "3310"]);
  const output = formatStackEndpoints(config, { startApi: true, startWeb: true });
  assert.match(output, /http:\/\/0\.0\.0\.0:8877/);
  assert.match(output, /http:\/\/127\.0\.0\.1:3310/);
});

test("rejects invalid ports", () => {
  assert.throws(() => resolveStackRuntimeConfig({}, ["--web-port", "70000"]), /--web-port/);
});

test("web address comes from start flags", () => {
  const config = resolveStackRuntimeConfig({}, ["--web-host", "192.168.1.9", "--web-port", "3310"]);
  assert.equal(config.webHost, "192.168.1.9");
  assert.equal(config.webPort, "3310");
  const defaults = resolveStackRuntimeConfig({});
  assert.equal(defaults.webHost, "127.0.0.1");
  assert.equal(defaults.webPort, "3000");
});

test("web listen flags require a value", () => {
  assert.throws(() => resolveStackRuntimeConfig({}, ["--web-host"]), /--web-host requires a value/);
  assert.throws(() => resolveStackRuntimeConfig({}, ["--web-port", "--web"]), /--web-port requires a value/);
});

test("one click reports GUI, API, and a ready TUI", () => {
  const config = resolveStackRuntimeConfig({ DATAAGENT_HOME: "/no/such/dataagent-home" });
  const output = formatStackEndpoints(config, { startApi: true, startWeb: true, tuiReady: true });
  assert.match(output, /GUI: http:\/\/127\.0\.0\.1:3000/);
  assert.match(output, /API: http:\/\/127\.0\.0\.1:8790/);
  assert.match(output, /TUI: npm run start:tui/);
});

test("control plane probe accepts the conversation health envelope", async () => {
  const ready = await controlPlaneIsRunning("http://127.0.0.1:8790", async () => Response.json({
    success: true,
    data: { conversation: { ready: true } },
  }));
  const down = await controlPlaneIsRunning("http://127.0.0.1:8790", async () => {
    throw new Error("down");
  });
  assert.equal(ready, true);
  assert.equal(down, false);
});

test("one click syncs the python environment only when it is missing", () => {
  const rootDir = mkdtempSync(join(tmpdir(), "dataagent-start-"));
  const calls = [];
  try {
    assert.equal(ensurePythonEnvironment(rootDir, (command) => calls.push(command)), true);
    mkdirSync(join(rootDir, "runtime/agentkit/.venv"), { recursive: true });
    assert.equal(ensurePythonEnvironment(rootDir, (command) => calls.push(command)), false);
  } finally {
    rmSync(rootDir, { recursive: true, force: true });
  }
  assert.deepEqual(calls, [PYTHON_ENVIRONMENT_SYNC]);
});

test("api and web scripts use the same stack entry as one-click start", () => {
  const scripts = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8")).scripts;
  const postinstall = readFileSync(new URL("./postinstall.mjs", import.meta.url), "utf8");
  assert.equal(scripts.build, "npm run build:web && npm run build:tui");
  assert.equal(postinstall.includes("pythonEnvironmentSyncCommand"), true);
  assert.equal(pythonEnvironmentSyncCommand({}), PYTHON_ENVIRONMENT_SYNC);
  assert.equal(
    pythonEnvironmentSyncCommand({ UV_PYTHON: "3.12.8" }),
    "uv sync --project runtime/agentkit --frozen --no-dev --python 3.12.8",
  );
  assert.throws(() => pythonEnvironmentSyncCommand({ UV_PYTHON: "3.12;rm -rf /" }), /Python version/);
  assert.equal(scripts["build:web"], "npm --workspace @datagallery/web run build");
  assert.equal(scripts["build:tui"], "npm --workspace @datagallery/tui run build");
  assert.equal(scripts["test:tui"], "npm --workspace @datagallery/tui run test");
  assert.match(scripts["test:deploy"], /scripts\/password-only-cutover\.test\.mjs/);
  assert.equal(scripts["start:api"], "node scripts/start.mjs --api");
  assert.equal(scripts["start:web"], "node scripts/start.mjs --web");
  assert.equal(scripts.start, "node scripts/start.mjs");
  assert.equal(scripts.dev, undefined);
  assert.equal(scripts["dev:api"], undefined);
  assert.equal(scripts["dev:web"], undefined);
});

test("dev and start:api both default the API to loopback", () => {
  const env = { DATAAGENT_HOME: "/no/such/dataagent-home" };
  const dev = resolveStackRuntimeConfig(env);
  const startApi = pythonControlPlaneLaunch("/repo", env);
  assert.equal(dev.apiHost, "127.0.0.1");
  assert.equal(startApi.env, env);
  assert.deepEqual(startApi.args.slice(-2), ["dataagent", "serve"]);
  const manualReload = pythonControlPlaneLaunch("/repo", env, true);
  assert.equal(manualReload.env, env);
  assert.deepEqual(manualReload.args.slice(-3), ["dataagent", "serve", "--reload"]);
});

test("server.host and server.port come from config.json", () => {
  const env = {
    DATAAGENT_HOME: "/no/such/dataagent-home",
    DATAAGENT_CONFIG: new URL("./fixtures/server-listen.json", import.meta.url).pathname,
  };
  const dev = resolveStackRuntimeConfig(env);
  const startApi = pythonControlPlaneLaunch("/repo", env);
  assert.equal(dev.apiHost, "0.0.0.0");
  assert.equal(dev.apiPort, "8877");
  assert.equal(startApi.env.DATAAGENT_CONFIG, env.DATAAGENT_CONFIG);
});
