import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { readServerListen } from "../ui/shared/server-listen.mjs";
import { controlPlaneIsRunning } from "./stack-runner.mjs";
import { venvPython } from "../ui/shared/python-environment.mjs";
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
  assert.equal(launch.command, venvPython("/project/.", env));
  assert.deepEqual(launch.args, ["-m", "dataagent", "serve"]);
  assert.equal(launch.env, env);
  assert.equal(launch.env.UV_NO_SYNC, undefined);
});

test("web launch passes host and port only as next arguments", () => {
  const config = resolveStackRuntimeConfig({ DATAAGENT_HOME: "/no/such/test-home" }, ["--web-host", "127.0.0.2", "--web-port", "3310"]);
  const launch = webLaunch(config, { PATH: "/usr/bin", HOSTNAME: "10.0.0.8", PORT: "9" });
  assert.equal(launch.command, "npm");
  assert.deepEqual(launch.args, [
    "--prefix", "ui/web", "run", "start", "--",
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
  writeFileSync(join(home, "config.yaml"), JSON.stringify({
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
  assert.throws(() => resolveStackRuntimeConfig({ DATAAGENT_HOME: "/no/such/test-home" }, ["--web-port", " "]), /--web-port/);
  assert.throws(() => resolveStackRuntimeConfig({ DATAAGENT_HOME: "/no/such/test-home" }, ["--web-port", "70000"]), /--web-port/);
});

test("web address comes from start flags", () => {
  const config = resolveStackRuntimeConfig({ DATAAGENT_HOME: "/no/such/test-home" }, ["--web-host", "192.168.1.9", "--web-port", "3310"]);
  assert.equal(config.webHost, "192.168.1.9");
  assert.equal(config.webPort, "3310");
  const defaults = resolveStackRuntimeConfig({ DATAAGENT_HOME: "/no/such/test-home" });
  assert.equal(defaults.webHost, "127.0.0.1");
  assert.equal(defaults.webPort, "3000");
});

test("web listen flags require a value", () => {
  assert.throws(() => resolveStackRuntimeConfig({ DATAAGENT_HOME: "/no/such/test-home" }, ["--web-host"]), /--web-host requires a value/);
  assert.throws(() => resolveStackRuntimeConfig({ DATAAGENT_HOME: "/no/such/test-home" }, ["--web-port", "--web"]), /--web-port requires a value/);
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

test("API startup reports a missing environment without preparing it", (t) => {
  const root = mkdtempSync(join(tmpdir(), "dataagent-start-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const result = spawnSync(process.execPath, [fileURLToPath(new URL("./start.mjs", import.meta.url)), "--api"], {
    env: { ...process.env, UV_PROJECT_ENVIRONMENT: join(root, "missing"), DATAAGENT_HOME: root },
    encoding: "utf8", timeout: 5000,
  });
  assert.equal(result.status, 1, result.stderr);
  assert.match(result.stderr, /Python environment is missing.*Run uv sync/);
  assert.doesNotMatch(result.stdout, /DataGallery endpoints/);
});

test("root scripts expose frontend builds and startup", () => {
  const scripts = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8")).scripts;
  assert.equal(scripts.build, "npm run build:web && npm run build:tui");
  assert.equal(scripts.postinstall, undefined);
  assert.equal(scripts["prepare:python"], undefined);
  assert.equal(scripts["test:product"], undefined);
  assert.equal(scripts["build:web"], "npm --workspace @datagallery/web run build");
  assert.equal(scripts["build:tui"], "npm --workspace @datagallery/tui run build");
  assert.equal(scripts["test:tui"], "npm --workspace @datagallery/tui run test --");
  assert.match(scripts["test:deploy"], /scripts\/password-only-cutover\.test\.mjs/);
  assert.equal(scripts["start:web"], "node scripts/start.mjs --web");
  assert.equal(scripts.start, "node scripts/start.mjs");
  assert.equal(scripts["start:tui"], "node -- ui/tui/dist/index.js");
  assert.equal(scripts.dev, undefined);
  assert.equal(scripts["dev:api"], undefined);
  assert.equal(scripts["dev:web"], undefined);
});

test("the stack defaults the API to loopback and launches dataagent serve", () => {
  const env = { DATAAGENT_HOME: "/no/such/dataagent-home" };
  const dev = resolveStackRuntimeConfig(env);
  const startApi = pythonControlPlaneLaunch("/repo", env);
  assert.equal(dev.apiHost, "127.0.0.1");
  assert.equal(startApi.env, env);
  assert.deepEqual(startApi.args.slice(-2), ["dataagent", "serve"]);
});

test("--config replaces Home server and is passed to dataagent serve", () => {
  const home = mkdtempSync(join(tmpdir(), "dataagent-listen-"));
  const selected = join(home, "selected.yaml");
  writeFileSync(join(home, "config.yaml"), JSON.stringify({
    server: { host: "10.1.2.3", port: 1 },
  }));
  writeFileSync(selected, JSON.stringify({
    server: { host: "0.0.0.0", port: 8877 },
  }));
  const env = { DATAAGENT_HOME: home };
  try {
    const config = resolveStackRuntimeConfig(env, ["--config", selected]);
    const startApi = pythonControlPlaneLaunch("/repo", env, config.configPath);
    assert.equal(config.apiHost, "0.0.0.0");
    assert.equal(config.apiPort, "8877");
    assert.deepEqual(startApi.args.slice(-4), ["dataagent", "serve", "--config", selected]);
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
  assert.throws(() => resolveStackRuntimeConfig({ DATAAGENT_HOME: "/no/such/test-home" }, ["--config"]), /--config requires a value/);
});

test("start anchors a relative --config to the caller before the stack cwd changes", () => {
  const outside = realpathSync(mkdtempSync(join(tmpdir(), "df-caller-")));
  const repo = realpathSync(mkdtempSync(join(tmpdir(), "df-repo-")));
  const selected = join(outside, "team.yaml");
  writeFileSync(selected, JSON.stringify({ server: { host: "0.0.0.0", port: 8877 } }));
  const previous = process.cwd();
  try {
    process.chdir(outside);
    const config = resolveStackRuntimeConfig({ DATAAGENT_HOME: "/no/such/test-home" }, ["--api", "--config", "team.yaml"]);
    assert.equal(config.configPath, selected);
    assert.equal(config.apiPort, "8877");
    process.chdir(repo);
    const launch = pythonControlPlaneLaunch(repo, {}, config.configPath);
    assert.deepEqual(launch.args.slice(-2), ["--config", selected]);
    assert.equal(readServerListen({}, config.configPath).port, "8877");
    writeFileSync(join(repo, "team.yaml"), JSON.stringify({ server: { host: "127.0.0.1", port: 1 } }));
    assert.equal(readServerListen({}, "team.yaml").port, "1");
    assert.equal(readServerListen({}, config.configPath).port, "8877");
  } finally {
    process.chdir(previous);
    rmSync(outside, { recursive: true, force: true });
    rmSync(repo, { recursive: true, force: true });
  }
});

test("server.host and server.port come from config.yaml", () => {
  const home = mkdtempSync(join(tmpdir(), "dataagent-listen-"));
  writeFileSync(join(home, "config.yaml"), JSON.stringify({
    server: { host: "0.0.0.0", port: 8877 },
  }));
  const env = { DATAAGENT_HOME: home };
  try {
    const dev = resolveStackRuntimeConfig(env);
    const startApi = pythonControlPlaneLaunch("/repo", env);
    assert.equal(dev.apiHost, "0.0.0.0");
    assert.equal(dev.apiPort, "8877");
    assert.equal(webLaunch(dev).env.API_PROXY_TARGET, "http://0.0.0.0:8877");
    writeFileSync(join(home, "config.yaml"), JSON.stringify({
      server: { host: "::", port: 8877 },
    }));
    const ipv6 = resolveStackRuntimeConfig(env);
    assert.equal(webLaunch(ipv6).env.API_PROXY_TARGET, "http://[::]:8877");
    assert.match(formatStackEndpoints(ipv6, { startApi: true, startWeb: false }), /http:\/\/\[::\]:8877/);
    assert.equal(startApi.env, env);
    assert.deepEqual(startApi.args.slice(-2), ["dataagent", "serve"]);
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
});

test("unknown stack arguments fail instead of being dropped", () => {
  for (const args of [["--env-file", "prod.env"], ["--user", "alice"], ["--config=team.yaml"], ["extra"]]) {
    assert.throws(() => resolveStackRuntimeConfig({ DATAAGENT_HOME: "/no/such/test-home" }, args), /Unknown argument/, args.join(" "));
  }
  assert.doesNotThrow(() => resolveStackRuntimeConfig({ DATAAGENT_HOME: "/no/such/dataagent-home" }, ["--", "--api", "--web-port", "3311"]));
});
