import assert from "node:assert/strict";
import test from "node:test";
import { createDeploymentController } from "./controller.mjs";
import { redactSensitiveText } from "./config.mjs";
import { formatDeploymentFailure } from "./cli.mjs";

function createFakeDeployDeps(overrides = {}) {
  const calls = [];
  const storage = { sentinel: Buffer.from("storage-sentinel-bytes") };
  let running = Boolean(overrides.running);
  let statusState = { process: "stopped", apiHealth: "unhealthy", apiReady: "unhealthy", web: "unreachable", ok: false };

  const deps = {
    calls,
    storage,
    async loadConfiguration() {
      calls.push("load-config");
      return { env: { WEB_PORT: "3000", API_PORT: "8787" }, envText: "", webText: "" };
    },
    async preflight(_config, options) {
      calls.push("preflight");
      if (options?.command === "start" && overrides.missingBuild) {
        throw new Error("请先运行 ./deploy.sh deploy");
      }
    },
    async configure(config) {
      calls.push("configure");
      return config;
    },
    async checkDependencies() {
      calls.push("check-dependencies");
    },
    async selectPorts(config) {
      calls.push("select-ports");
      return config;
    },
    async writeConfiguration() {
      calls.push("write-config");
    },
    async isRunning() {
      return running;
    },
    async stop() {
      calls.push("stop");
      running = false;
      statusState = { ...statusState, process: "stopped", ok: false };
    },
    async installProject() {
      calls.push("install");
    },
    async buildTypeScript() {
      calls.push("build-typescript");
      if (overrides.failBuild) {
        const error = new Error("TypeScript build failed");
        error.logPath = "storage/logs/deploy-20260722-143000.log";
        throw error;
      }
    },
    async buildWeb() {
      calls.push("build-web");
      if (overrides.failWebBuild) {
        const error = new Error("Web build failed");
        error.logPath = "storage/logs/deploy-20260722-143000.log";
        throw error;
      }
    },
    async buildTui() {
      calls.push("build-tui");
      if (overrides.failTuiBuild) {
        const error = new Error("TUI build failed");
        error.logPath = "storage/logs/deploy-20260722-143000.log";
        throw error;
      }
    },
    async ensureTuiReady() {
      calls.push("ensure-tui-ready");
    },
    async checkApiForTui() {
      calls.push("check-api-for-tui");
      if (overrides.tuiApiUnhealthy) {
        throw new Error("API is not healthy at http://127.0.0.1:8787/healthz");
      }
    },
    async startTui() {
      calls.push("start-tui");
    },

    async verifyPorts() {
      calls.push("verify-ports-again");
      if (overrides.portConflictAfterBuild) {
        throw new Error("Web port 3000 is already in use");
      }
    },
    async start() {
      calls.push("start");
      running = true;
      statusState = {
        process: "running",
        apiHealth: "healthy",
        apiReady: "ready",
        web: "reachable",
        ok: true
      };
    },
    async waitForHealth() {
      calls.push("wait-for-health");
    },
    async markHealthy() {
      calls.push("mark-healthy");
    },
    async status() {
      calls.push("status");
      return statusState;
    },
    async logs() {
      calls.push("logs");
    },
    async doctor() {
      calls.push("doctor");
    },
    async printHelp() {
      calls.push("help");
    },
    async reportFailure(failure) {
      calls.push("report-failure");
      deps.lastFailure = failure;
    },
    ...overrides.deps
  };
  return deps;
}

test("fresh deploy runs configure through healthy state", async () => {
  const deps = createFakeDeployDeps({ running: false });
  const controller = createDeploymentController(deps);
  const result = await controller.run({ command: "deploy", reconfigure: false, nonInteractive: true });
  assert.ok(result.ok);
  assert.deepEqual(result.calls, [
    "load-config",
    "preflight",
    "configure",
    "check-dependencies",
    "select-ports",
    "write-config",
    "install",
    "build-typescript",
    "build-web",
    "build-tui",
    "verify-ports-again",
    "start",
    "wait-for-health",
    "mark-healthy"
  ]);
  assert.ok(!result.calls.includes("stop-old"));
  assert.ok(!result.calls.includes("start-tui"));
});

test("update deploy stops only after preflight and before install/build", async () => {
  const deps = createFakeDeployDeps({ running: true });
  const controller = createDeploymentController(deps);
  const result = await controller.run({ command: "deploy", reconfigure: false, nonInteractive: true });
  const stopIndex = result.calls.indexOf("stop-old");
  const installIndex = result.calls.indexOf("install");
  assert.ok(stopIndex > result.calls.indexOf("preflight"));
  assert.ok(stopIndex > result.calls.indexOf("write-config"));
  assert.ok(installIndex > stopIndex);
  assert.ok(result.calls.indexOf("build-typescript") > stopIndex);
});

test("build failure keeps storage intact and reports maintenance window", async () => {
  const deps = createFakeDeployDeps({ running: true, failWebBuild: true });
  const controller = createDeploymentController(deps);
  await assert.rejects(
    () => controller.run({ command: "deploy", reconfigure: false, nonInteractive: true }),
    /Web build failed/
  );
  assert.equal(deps.storage.sentinel.toString(), "storage-sentinel-bytes");
  assert.equal(deps.lastFailure.maintenanceWindow, true);
  assert.equal(deps.lastFailure.oldServiceRunning, false);
  assert.equal(deps.lastFailure.configurationChanged, true);
  assert.equal(deps.lastFailure.stage, "build-web");
  assert.equal(deps.lastFailure.retryCommand, "./deploy.sh deploy");
  assert.equal(deps.lastFailure.doctorCommand, "./deploy.sh doctor");
  assert.ok(!deps.calls.includes("start"));
  const formatted = formatDeploymentFailure({
    ...deps.lastFailure,
    summary: "Web build failed"
  });
  assert.match(formatted, /现有数据未被修改/);
  assert.match(formatted, /维护窗口/);
  assert.doesNotMatch(formatted, /已恢复|rolled back|restored/i);
});

test("port conflict after build prevents start and never kills listeners", async () => {
  const deps = createFakeDeployDeps({ running: false, portConflictAfterBuild: true });
  let killed = false;
  deps.killListener = () => {
    killed = true;
  };
  const controller = createDeploymentController(deps);
  await assert.rejects(
    () => controller.run({ command: "deploy", reconfigure: false, nonInteractive: true }),
    /already in use/
  );
  assert.ok(!deps.calls.includes("start"));
  assert.equal(killed, false);
});

test("start with missing build artifact fails with deploy hint", async () => {
  const deps = createFakeDeployDeps({ missingBuild: true });
  const controller = createDeploymentController(deps);
  await assert.rejects(() => controller.run({ command: "start" }), /请先运行 \.\/deploy\.sh deploy/);
  assert.ok(!deps.calls.includes("install"));
  assert.ok(!deps.calls.includes("build-typescript"));
});

test("start with incomplete config fails with deploy hint and never starts", async () => {
  const deps = createFakeDeployDeps();
  deps.preflight = async (_config, options) => {
    deps.calls.push("preflight");
    if (options?.command === "start") {
      throw new Error("缺少合法完整的 .env 配置，请先运行 ./deploy.sh deploy");
    }
  };
  const controller = createDeploymentController(deps);
  await assert.rejects(() => controller.run({ command: "start" }), /缺少合法完整的 \.env|请先运行 \.\/deploy\.sh deploy/);
  assert.ok(!deps.calls.includes("start"));
});

test("status reports stopped and unhealthy components", async () => {
  const deps = createFakeDeployDeps();
  deps.status = async () => ({
    process: "stopped",
    apiHealth: "unhealthy",
    apiReady: "unhealthy",
    web: "unreachable",
    ok: false
  });
  const controller = createDeploymentController(deps);
  const summary = await controller.run({ command: "status" });
  assert.equal(summary.process, "stopped");
  assert.equal(summary.apiHealth, "unhealthy");
  assert.equal(summary.web, "unreachable");
});

test("restart does not install or build", async () => {
  const deps = createFakeDeployDeps({ running: true });
  const controller = createDeploymentController(deps);
  await controller.run({ command: "restart" });
  assert.ok(deps.calls.includes("stop"));
  assert.ok(deps.calls.includes("start"));
  assert.ok(!deps.calls.includes("install"));
  assert.ok(!deps.calls.includes("build-typescript"));
  assert.ok(!deps.calls.includes("build-web"));
});

test("stop twice succeeds both times", async () => {
  const deps = createFakeDeployDeps({ running: true });
  const controller = createDeploymentController(deps);
  await controller.run({ command: "stop" });
  await controller.run({ command: "stop" });
  assert.equal(deps.calls.filter((call) => call === "stop").length, 2);
});

test("runtime and deploy logs redact fixture secrets", () => {
  const text = redactSensitiveText(
    "AUTH_SESSION_SECRET=fixture-deploy-secret-at-least-32-chars\nLLM_API_KEY=sk-test\nWEB_PORT=3000\n"
  );
  assert.doesNotMatch(text, /fixture-deploy-secret-at-least-32-chars/);
  assert.doesNotMatch(text, /sk-test/);
  assert.match(text, /WEB_PORT=3000/);
});

test("deploy failure uses beginDeployLog path", async () => {
  let failure;
  const controller = createDeploymentController({
    async beginDeployLog() {
      return "storage/logs/deploy-20260722143000.log";
    },
    async loadConfiguration() {
      return { env: {} };
    },
    async preflight() {},
    async configure(config) {
      return config;
    },
    async checkDependencies() {},
    async selectPorts(config) {
      return config;
    },
    async writeConfiguration() {},
    async isRunning() {
      return false;
    },
    async stop() {},
    async installProject() {},
    async buildTypeScript() {
      throw new Error("TypeScript build failed");
    },
    async buildWeb() {},
    async buildTui() {},
    async verifyPorts() {},
    async start() {},
    async waitForHealth() {},
    async markHealthy() {},
    async reportFailure(details) {
      failure = details;
    }
  });

  await assert.rejects(
    () => controller.run({ command: "deploy", reconfigure: false, nonInteractive: true }),
    /TypeScript build failed/
  );
  assert.equal(failure.logPath, "storage/logs/deploy-20260722143000.log");
});

test("deploy never auto-starts TUI client", async () => {
  const deps = createFakeDeployDeps({ running: false });
  const controller = createDeploymentController(deps);
  const result = await controller.run({ command: "deploy", reconfigure: false, nonInteractive: true });
  assert.ok(result.calls.includes("build-tui"));
  assert.ok(!result.calls.includes("ensure-tui-ready"));
  assert.ok(!result.calls.includes("check-api-for-tui"));
  assert.ok(!result.calls.includes("start-tui"));
});

test("optional tui command requires healthy API before start", async () => {
  const deps = createFakeDeployDeps({ tuiApiUnhealthy: true });
  const controller = createDeploymentController(deps);
  await assert.rejects(() => controller.run({ command: "tui", runtimeUrl: null }), /API is not healthy/);
  assert.ok(deps.calls.includes("ensure-tui-ready"));
  assert.ok(deps.calls.includes("check-api-for-tui"));
  assert.ok(!deps.calls.includes("start-tui"));
});
