import assert from "node:assert/strict";
import test from "node:test";
import {
  formatStackEndpoints,
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

test("prints the Deep Agents runtime endpoint when enabled", () => {
  const config = resolveStackRuntimeConfig({ RUNTIME_SERVICE_PORT: "8791" });
  assert.equal(config.RUNTIME_PORT, "8791");
  const output = formatStackEndpoints(config, { startApi: true, startWeb: false, startRuntime: true });
  assert.match(output, /http:\/\/127\.0\.0\.1:8791/);
});

test("rejects invalid ports", () => {
  assert.throws(() => resolveStackRuntimeConfig({ WEB_PORT: "70000" }), /WEB_PORT/);
});
