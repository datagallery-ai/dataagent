import assert from "node:assert/strict";
import test from "node:test";
import { formatDeploymentFailure } from "./cli.mjs";
import { createDeploymentController } from "./controller.mjs";

function deployment({ running = false, failsAt, error } = {}) {
  const calls = [];
  const config = { env: {} };
  const step = (name, result) => async () => {
    calls.push(name);
    if (name === failsAt) throw error;
    return result;
  };
  const controller = createDeploymentController({
    loadConfiguration: step("load-config", config),
    checkDependencies: step("check-dependencies"),
    preflight: step("preflight"),
    configure: step("configure", config),
    verifyPorts: (_config, { retries } = {}) =>
      step(retries === 0 ? "verify-ports" : "verify-ports-again")(),
    writeConfiguration: step("write-config"),
    isRunning: async () => running,
    stop: step("stop"),
    installProject: step("install"),
    buildWeb: step("build-web"),
    buildTui: step("build-tui"),
    start: step("start"),
    waitForHealth: step("wait-for-health"),
    markHealthy: step("mark-healthy"),
    startTui: step("start-tui"),
    reportFailure: step("report-failure"),
  });
  return { calls, run: (command) => controller.run({ command, nonInteractive: true }) };
}

for (const running of [false, true]) {
  test(`deploy verifies the configuration and ports before ${running ? "updating" : "installing"}`, async () => {
    const { calls, run } = deployment({ running });
    await run("deploy");
    assert.deepEqual(calls, [
      "load-config", "check-dependencies", "preflight", "configure", "verify-ports",
      "write-config", ...(running ? ["stop"] : []), "install", "build-web", "build-tui",
      "verify-ports-again", "start", "wait-for-health", "mark-healthy",
    ]);
  });
}

test("a build failure after stopping reports the maintenance window", async () => {
  const error = new Error("Web build failed");
  const { calls, run } = deployment({ running: true, failsAt: "build-web", error });
  await assert.rejects(run("deploy"), ({ failure }) => {
    assert.equal(failure.stage, "build-web");
    assert.match(formatDeploymentFailure(failure, "runtime/logs/web.log"), /维护窗口/);
    return true;
  });
  assert.deepEqual(calls.slice(-4), ["stop", "install", "build-web", "report-failure"]);
});

test("a port conflict after building prevents startup", async () => {
  const { calls, run } = deployment({
    failsAt: "verify-ports-again",
    error: new Error("Web port 3000 is already in use"),
  });
  await assert.rejects(run("deploy"), /already in use/);
  assert.deepEqual(calls.slice(-3), ["build-tui", "verify-ports-again", "report-failure"]);
});

test("restart checks the existing environment and ports before stopping", async () => {
  const { calls, run } = deployment();
  await run("restart");
  assert.deepEqual(calls, [
    "load-config", "preflight", "check-dependencies", "verify-ports",
    "stop", "verify-ports-again", "start", "wait-for-health", "mark-healthy",
  ]);
});

test("restart leaves the current service running when a dependency is missing", async () => {
  const { calls, run } = deployment({
    failsAt: "check-dependencies",
    error: new Error("dependency missing"),
  });
  await assert.rejects(run("restart"), /dependency missing/);
  assert.deepEqual(calls, ["load-config", "preflight", "check-dependencies"]);
});
