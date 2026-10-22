import assert from "node:assert/strict";
import test from "node:test";
import {
  formatStackEndpoints,
  pythonControlPlaneLaunch,
  resolveStackRuntimeConfig,
  webProcessEnvironment,
} from "./stack-runtime-config.mjs";

test("uses native deployment defaults", () => {
  const config = resolveStackRuntimeConfig({ DATAAGENT_HOME: "/no/such/dataagent-home" });
  assert.equal(config.API_HOST, "127.0.0.1");
  assert.equal(config.API_PORT, "8790");
  assert.equal(config.WEB_HOST, "127.0.0.1");
  assert.equal(config.WEB_PORT, "3000");
});

test("projects configured host and port into the Next.js child", () => {
  const config = resolveStackRuntimeConfig({ WEB_HOST: "127.0.0.1", WEB_PORT: "3310" });
  assert.deepEqual(webProcessEnvironment(config), { HOSTNAME: "127.0.0.1", PORT: "3310" });
});

test("prints actual configured endpoints", () => {
  const config = resolveStackRuntimeConfig({
    DATAAGENT_HOME: "/no/such/dataagent-home",
    DATAAGENT_CONFIG: new URL("./fixtures/server-listen.json", import.meta.url).pathname,
    WEB_PORT: "3310",
  });
  const output = formatStackEndpoints(config, { startApi: true, startWeb: true });
  assert.match(output, /http:\/\/0\.0\.0\.0:8877/);
  assert.match(output, /http:\/\/127\.0\.0\.1:3310/);
});

test("rejects invalid ports", () => {
  assert.throws(() => resolveStackRuntimeConfig({ WEB_PORT: "70000" }), /WEB_PORT/);
});

test("dev and start:api both default the API to loopback", () => {
  const env = { DATAAGENT_HOME: "/no/such/dataagent-home" };
  const dev = resolveStackRuntimeConfig(env);
  const startApi = pythonControlPlaneLaunch("/repo", env);
  assert.equal(dev.API_HOST, "127.0.0.1");
  assert.equal(startApi.env.API_PORT, undefined);
  assert.deepEqual(startApi.args.slice(-2), ["dataagent", "serve"]);
});

test("server.host and server.port come from config.json", () => {
  const env = {
    DATAAGENT_HOME: "/no/such/dataagent-home",
    DATAAGENT_CONFIG: new URL("./fixtures/server-listen.json", import.meta.url).pathname,
  };
  const dev = resolveStackRuntimeConfig(env);
  const startApi = pythonControlPlaneLaunch("/repo", { ...env, API_HOST: "10.0.0.8", API_PORT: "1" });
  assert.equal(dev.API_HOST, "0.0.0.0");
  assert.equal(dev.API_PORT, "8877");
  assert.equal(startApi.env.API_HOST, undefined);
  assert.equal(startApi.env.API_PORT, undefined);
});
