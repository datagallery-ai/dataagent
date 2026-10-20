import assert from "node:assert/strict";
import test from "node:test";
import {
  formatStackEndpoints,
  pythonControlPlaneLaunch,
  resolveStackRuntimeConfig,
  webProcessEnvironment,
} from "./stack-runtime-config.mjs";

test("uses native deployment defaults", () => {
  const config = resolveStackRuntimeConfig({});
  assert.equal(config.API_HOST, "127.0.0.1");
  assert.equal(config.API_PORT, "8787");
  assert.equal(config.WEB_HOST, "127.0.0.1");
  assert.equal(config.WEB_PORT, "3000");
});

test("projects configured host and port into the Next.js child", () => {
  const config = resolveStackRuntimeConfig({ WEB_HOST: "127.0.0.1", WEB_PORT: "3310" });
  assert.deepEqual(webProcessEnvironment(config), { HOSTNAME: "127.0.0.1", PORT: "3310" });
});

test("prints actual configured endpoints", () => {
  const config = resolveStackRuntimeConfig({ API_PORT: "8877", WEB_PORT: "3310" });
  const output = formatStackEndpoints(config, { startApi: true, startWeb: true });
  assert.match(output, /http:\/\/127\.0\.0\.1:8877/);
  assert.match(output, /http:\/\/127\.0\.0\.1:3310/);
});

test("rejects invalid ports", () => {
  assert.throws(() => resolveStackRuntimeConfig({ WEB_PORT: "70000" }), /WEB_PORT/);
});

test("dev and start:api both default the API to loopback", () => {
  const dev = resolveStackRuntimeConfig({});
  const startApi = pythonControlPlaneLaunch("/repo", {});
  assert.equal(dev.API_HOST, "127.0.0.1");
  assert.equal(startApi.env.API_HOST, "127.0.0.1");
  assert.equal(startApi.env.API_PORT, "8787");
});

test("an explicit API_HOST is preserved for public bind", () => {
  const dev = resolveStackRuntimeConfig({ API_HOST: "0.0.0.0", API_PORT: "9000" });
  const startApi = pythonControlPlaneLaunch("/repo", { API_HOST: "0.0.0.0", API_PORT: "9000" });
  assert.equal(dev.API_HOST, "0.0.0.0");
  assert.equal(startApi.env.API_HOST, "0.0.0.0");
  assert.equal(startApi.env.API_PORT, "9000");
});
