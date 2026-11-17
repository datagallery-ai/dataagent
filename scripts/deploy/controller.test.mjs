import assert from "node:assert/strict";
import test from "node:test";
import { createDeploymentController } from "./controller.mjs";

test("deploy update path uses exact maintenance-window ordering", async () => {
  const controller = createDeploymentController({
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
      return true;
    },
    async stop() {},
    async installProject() {},
    async buildWeb() {},
    async buildTui() {},
    async verifyPorts() {},
    async start() {},
    async waitForHealth() {},
    async markHealthy() {},
    async reportFailure() {}
  });

  const result = await controller.run({ command: "deploy", reconfigure: false, nonInteractive: true });
  assert.deepEqual(result.calls, [
    "load-config",
    "preflight",
    "configure",
    "check-dependencies",
    "select-ports",
    "write-config",
    "stop-old",
    "install",
    "build-web",
    "build-tui",
    "verify-ports-again",
    "start",
    "wait-for-health",
    "mark-healthy"
  ]);
  assert.ok(!result.calls.includes("start-tui"));
});

test("build failure after stop never starts and reports maintenance window", async () => {
  let failure;
  const controller = createDeploymentController({
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
      return true;
    },
    async stop() {},
    async installProject() {},
    async buildWeb() {
      const error = new Error("build failed");
      error.logPath = "runtime/logs/backend.log";
      throw error;
    },
    async buildTui() {},
    async verifyPorts() {},
    async start() {},
    async waitForHealth() {},
    async markHealthy() {},
    async reportFailure(details) {
      failure = details;
    }
  });

  const storage = { sentinel: Buffer.from("keep-me") };
  await assert.rejects(
    () => controller.run({ command: "deploy", reconfigure: false, nonInteractive: true }),
    /build failed/
  );
  assert.ok(!failure.calls.includes("start"));
  assert.ok(failure.calls.includes("stop-old"));
  assert.equal(failure.maintenanceWindow, true);
  assert.equal(failure.serviceStopped, true);
  assert.equal(failure.stage, "build-web");
  assert.equal(storage.sentinel.toString(), "keep-me");
});

test("stop and help dispatch", async () => {
  const calls = [];
  const controller = createDeploymentController({
    async stop() {
      calls.push("stop");
    },
    async printHelp() {
      calls.push("help");
    }
  });
  await controller.run({ command: "stop" });
  await controller.run({ command: "help" });
  assert.deepEqual(calls, ["stop", "help"]);
});

test("optional tui command checks readiness and API then starts foreground client", async () => {
  const calls = [];
  const controller = createDeploymentController({
    async loadConfiguration() {
      calls.push("load-config");
      return { env: {} };
    },
    async ensureTuiReady() {
      calls.push("ensure-tui-ready");
    },
    async checkApiForTui() {
      calls.push("check-api-for-tui");
    },
    async startTui(_config, parsed) {
      calls.push(`start-tui:${parsed.runtimeUrl ?? "default"}`);
    }
  });
  await controller.run({
    command: "tui",
    reconfigure: false,
    nonInteractive: false,
    runtimeUrl: "http://127.0.0.1:9000/api/v1/runs"
  });
  assert.deepEqual(calls, [
    "load-config",
    "ensure-tui-ready",
    "check-api-for-tui",
    "start-tui:http://127.0.0.1:9000/api/v1/runs"
  ]);
});

test("tui aborts before start when API check fails", async () => {
  const calls = [];
  const controller = createDeploymentController({
    async loadConfiguration() {
      return { env: {} };
    },
    async ensureTuiReady() {
      calls.push("ensure-tui-ready");
    },
    async checkApiForTui() {
      calls.push("check-api-for-tui");
      throw new Error("API is not healthy");
    },
    async startTui() {
      calls.push("start-tui");
    }
  });
  await assert.rejects(() => controller.run({ command: "tui", runtimeUrl: null }), /API is not healthy/);
  assert.deepEqual(calls, ["ensure-tui-ready", "check-api-for-tui"]);
  assert.ok(!calls.includes("start-tui"));
});
