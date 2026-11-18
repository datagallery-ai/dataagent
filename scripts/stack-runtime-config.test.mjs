import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { readServerListen } from "./server-listen.mjs";
import { controlPlaneIsRunning, ensurePythonEnvironment } from "./stack-runner.mjs";
import { venvPython } from "./python-environment.mjs";
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

test("startup uses the selected interpreter without exporting a synchronization policy", () => {
  const env = { UV_PROJECT_ENVIRONMENT: "custom env" };
  const launch = pythonControlPlaneLaunch("/project", env);
  assert.equal(launch.command, venvPython("/project/runtime/agentkit", env));
  assert.deepEqual(launch.args, ["-m", "serve", "serve"]);
  assert.equal(launch.env, env);
  assert.equal(launch.env.UV_NO_SYNC, undefined);
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
  const home = mkdtempSync(join(tmpdir(), "dataagent-listen-"));
  writeFileSync(join(home, "config.json"), JSON.stringify({
    server: { host: "0.0.0.0", port: 8877 },
  }));
  try {
    const config = resolveStackRuntimeConfig({
      DATAAGENT_HOME: home,
    }, ["--web-port", "3310"]);
    const output = formatStackEndpoints(config, { startApi: true, startWeb: true });
    assert.match(output, /http:\/\/0\.0\.0\.0:8877/);
    assert.match(output, /http:\/\/127\.0\.0\.1:3310/);
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
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

test("one click syncs the python environment only when it is missing", async () => {
  const rootDir = mkdtempSync(join(tmpdir(), "dataagent-start-"));
  const calls = [];
  try {
    assert.equal(await ensurePythonEnvironment(rootDir, (...command) => calls.push(command)), true);
    const python = venvPython(join(rootDir, "runtime/agentkit"));
    mkdirSync(join(python, ".."), { recursive: true });
    writeFileSync(python, "fixture");
    assert.equal(await ensurePythonEnvironment(rootDir, (command) => calls.push(command)), false);
  } finally {
    rmSync(rootDir, { recursive: true, force: true });
  }
  assert.deepEqual(calls, [[process.execPath, ["scripts/python-environment.mjs", "--no-dev"]]]);
});

test("api and web scripts use the same stack entry as one-click start", () => {
  const scripts = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8")).scripts;
  const postinstall = readFileSync(new URL("./postinstall.mjs", import.meta.url), "utf8");
  assert.equal(scripts.build, "npm run build:web && npm run build:tui");
  assert.equal(postinstall.includes("syncPythonEnvironment"), true);
  assert.equal(scripts["prepare:python"], "node scripts/python-environment.mjs");
  assert.match(scripts["test:product"], /uv run --no-project --no-sync/);
  assert.equal(scripts["build:web"], "npm --workspace @datagallery/web run build");
  assert.equal(scripts["build:tui"], "npm --workspace @datagallery/tui run build");
  assert.equal(scripts["test:tui"], "npm --workspace @datagallery/tui run test");
  assert.match(scripts["test:deploy"], /scripts\/password-only-cutover\.test\.mjs/);
  assert.equal(scripts["start:api"], "node scripts/start.mjs --api");
  assert.equal(scripts["start:web"], "node scripts/start.mjs --web");
  assert.equal(scripts.start, "node scripts/start.mjs");
  assert.equal(scripts["start:tui"], "node -- apps/tui/dist/index.js");
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
  assert.deepEqual(startApi.args.slice(-2), ["serve", "serve"]);
  const manualReload = pythonControlPlaneLaunch("/repo", env, true);
  assert.equal(manualReload.env, env);
  assert.deepEqual(manualReload.args.slice(-3), ["serve", "serve", "--reload"]);
});

test("--config replaces Home server and is passed to dataagent serve", () => {
  const home = mkdtempSync(join(tmpdir(), "dataagent-listen-"));
  const selected = join(home, "selected.json");
  writeFileSync(join(home, "config.json"), JSON.stringify({
    server: { host: "10.1.2.3", port: 1 },
  }));
  writeFileSync(selected, JSON.stringify({
    server: { host: "0.0.0.0", port: 8877 },
  }));
  const env = { DATAAGENT_HOME: home };
  try {
    const config = resolveStackRuntimeConfig(env, ["--config", selected]);
    const startApi = pythonControlPlaneLaunch("/repo", env, false, config.configPath);
    assert.equal(config.apiHost, "0.0.0.0");
    assert.equal(config.apiPort, "8877");
    assert.deepEqual(startApi.args.slice(-4), ["serve", "serve", "--config", selected]);
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
  assert.throws(() => resolveStackRuntimeConfig({}, ["--config"]), /--config requires a value/);
});

test("start anchors a relative --config to the caller before the stack cwd changes", () => {
  const outside = realpathSync(mkdtempSync(join(tmpdir(), "df-caller-")));
  const repo = realpathSync(mkdtempSync(join(tmpdir(), "df-repo-")));
  const selected = join(outside, "team.json");
  writeFileSync(selected, JSON.stringify({ server: { host: "0.0.0.0", port: 8877 } }));
  const previous = process.cwd();
  try {
    process.chdir(outside);
    const config = resolveStackRuntimeConfig({}, ["--api", "--config", "team.json"]);
    assert.equal(config.configPath, selected);
    assert.equal(config.apiPort, "8877");
    process.chdir(repo);
    const launch = pythonControlPlaneLaunch(repo, {}, false, config.configPath);
    assert.deepEqual(launch.args.slice(-2), ["--config", selected]);
    assert.equal(readServerListen({}, config.configPath).port, "8877");
    writeFileSync(join(repo, "team.json"), JSON.stringify({ server: { host: "127.0.0.1", port: 1 } }));
    assert.equal(readServerListen({}, "team.json").port, "1");
    assert.equal(readServerListen({}, config.configPath).port, "8877");
  } finally {
    process.chdir(previous);
    rmSync(outside, { recursive: true, force: true });
    rmSync(repo, { recursive: true, force: true });
  }
});

test("server.host and server.port come from config.json", () => {
  const home = mkdtempSync(join(tmpdir(), "dataagent-listen-"));
  writeFileSync(join(home, "config.json"), JSON.stringify({
    server: { host: "0.0.0.0", port: 8877 },
  }));
  const env = { DATAAGENT_HOME: home };
  try {
    const dev = resolveStackRuntimeConfig(env);
    const startApi = pythonControlPlaneLaunch("/repo", env);
    assert.equal(dev.apiHost, "0.0.0.0");
    assert.equal(dev.apiPort, "8877");
    assert.equal(webLaunch(dev).env.API_PROXY_TARGET, "http://0.0.0.0:8877");
    writeFileSync(join(home, "config.json"), JSON.stringify({
      server: { host: "::", port: 8877 },
    }));
    const ipv6 = resolveStackRuntimeConfig(env);
    assert.equal(webLaunch(ipv6).env.API_PROXY_TARGET, "http://[::]:8877");
    assert.match(formatStackEndpoints(ipv6, { startApi: true, startWeb: false, tuiReady: false }), /http:\/\/\[::\]:8877/);
    assert.equal(startApi.env, env);
    assert.deepEqual(startApi.args.slice(-2), ["serve", "serve"]);
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
});

test("unknown stack arguments fail instead of being dropped", () => {
  for (const args of [["--env-file", "prod.env"], ["--user", "alice"], ["--config=team.json"], ["extra"]]) {
    assert.throws(() => resolveStackRuntimeConfig({}, args), /Unknown argument/, args.join(" "));
  }
  assert.doesNotThrow(() => resolveStackRuntimeConfig({ DATAAGENT_HOME: "/no/such/dataagent-home" }, ["--", "--api", "--web-port", "3311"]));
});
