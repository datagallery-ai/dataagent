import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, stat, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import {
  ensureDeploymentEnvironment,
  isCompleteDeploymentConfig,
  parseDeploymentEnvironment,
  redactSensitiveText,
  renderWebEnvironment,
  updateDeploymentEnvironment,
  writeDeploymentConfiguration
} from "./config.mjs";

test("creates safe defaults without model settings", () => {
  const result = ensureDeploymentEnvironment("", { randomSecret: () => "generated-secret-value" });
  assert.equal(result.env.WEB_PORT, "3000");
  assert.equal(result.env.API_PORT, "8787");
  assert.equal(result.env.AUTH_SESSION_SECRET, "generated-secret-value");
  assert.equal(result.env.SECRET_MASTER_KEY, "generated-secret-value");
  assert.equal(result.env.LLM_API_KEY, undefined);
});

test("preserves existing secrets and unrelated values", () => {
  const source = "AUTH_SESSION_SECRET=existing-session\nSECRET_MASTER_KEY=existing-master\nCUSTOM_VALUE=keep-me\n";
  const result = ensureDeploymentEnvironment(source, { randomSecret: () => "replacement" });
  assert.match(result.text, /AUTH_SESSION_SECRET=existing-session/);
  assert.match(result.text, /SECRET_MASTER_KEY=existing-master/);
  assert.match(result.text, /CUSTOM_VALUE=keep-me/);
});

test("renders same-origin Web BFF configuration", () => {
  const text = renderWebEnvironment({
    DATAFOUNDRY_AUTH_MODE: "password",
    API_HOST: "127.0.0.1",
    API_PORT: "8877"
  });
  assert.match(text, /NEXT_PUBLIC_AGENT_RUNTIME_URL=$/m);
  assert.match(text, /NEXT_PUBLIC_CONFIG_API_URL=$/m);
  assert.match(text, /API_PROXY_TARGET=http:\/\/127\.0\.0\.1:8877/);
});

test("reconfigure creates a backup and atomically writes both files", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "datafoundry-config-"));
  await mkdir(path.join(root, "apps/web"), { recursive: true });
  await writeFile(path.join(root, ".env"), "AUTH_SESSION_SECRET=old\nSECRET_MASTER_KEY=old-master\n");
  const result = ensureDeploymentEnvironment(await readFile(path.join(root, ".env"), "utf8"));
  const written = await writeDeploymentConfiguration(root, result.text, renderWebEnvironment(result.env), {
    backup: true,
    timestamp: "20260722-120000"
  });
  assert.equal(await readFile(written.backupPath, "utf8"), "AUTH_SESSION_SECRET=old\nSECRET_MASTER_KEY=old-master\n");
  assert.match(await readFile(path.join(root, "apps/web/.env.local"), "utf8"), /API_PROXY_TARGET=/);

  if (process.platform !== "win32") {
    for (const filePath of [
      path.join(root, ".env"),
      path.join(root, "apps/web/.env.local"),
      written.backupPath
    ]) {
      assert.equal((await stat(filePath)).mode & 0o777, 0o600, filePath);
    }
  }
});

test("updateDeploymentEnvironment upserts values while preserving comments", () => {
  const source = "# comment\nWEB_PORT=3000\nCUSTOM=keep\n";
  const text = updateDeploymentEnvironment(source, { WEB_PORT: "3310", API_PORT: "8877" });
  assert.match(text, /# comment/);
  assert.match(text, /WEB_PORT=3310/);
  assert.match(text, /API_PORT=8877/);
  assert.match(text, /CUSTOM=keep/);
});

test("redactSensitiveText masks secret-like keys", () => {
  const redacted = redactSensitiveText("LLM_API_KEY=super-secret\nAUTH_SESSION_SECRET=abc\nWEB_PORT=3000\n");
  assert.match(redacted, /LLM_API_KEY=\*+/);
  assert.match(redacted, /AUTH_SESSION_SECRET=\*+/);
  assert.match(redacted, /WEB_PORT=3000/);
  assert.doesNotMatch(redacted, /super-secret/);
});

test("redactSensitiveText masks JSON keys, bearer tokens, URL userinfo, and token prefixes", () => {
  const redacted = redactSensitiveText(
    [
      '{"apiKey":"json-secret-value","api_key":"also-secret"}',
      "Authorization: Bearer fixture-deploy-secret-at-least-32-chars",
      "https://user:fixture-deploy-secret-at-least-32-chars@example.com/path",
      "token sk-abcdefghijklmnop",
      "WEB_PORT=3000"
    ].join("\n")
  );
  assert.doesNotMatch(redacted, /json-secret-value|also-secret|fixture-deploy-secret-at-least-32-chars|sk-abcdefghijklmnop|user:fixture/);
  assert.match(redacted, /Authorization: Bearer \*+/i);
  assert.match(redacted, /https:\/\/\*\*\*\*:\*\*\*\*@example\.com\/path/);
  assert.match(redacted, /WEB_PORT=3000/);
});

test("ensureDeploymentEnvironment can skip secret generation", () => {
  const result = ensureDeploymentEnvironment("WEB_PORT=3000\n", { generateSecrets: false });
  assert.equal(result.env.AUTH_SESSION_SECRET, undefined);
  assert.equal(result.env.SECRET_MASTER_KEY, undefined);
  assert.ok(result.generatedKeys.includes("WEB_HOST"));
  assert.ok(!result.generatedKeys.includes("AUTH_SESSION_SECRET"));
});

test("isCompleteDeploymentConfig rejects placeholders and partial env", () => {
  assert.equal(isCompleteDeploymentConfig(parseDeploymentEnvironment("FOO=bar\n")), false);
  assert.equal(
    isCompleteDeploymentConfig({
      WEB_PORT: "3000",
      API_PORT: "8787",
      AUTH_PUBLIC_BASE_URL: "http://127.0.0.1:3000",
      AUTH_SESSION_SECRET: "change-me",
      SECRET_MASTER_KEY: "replace-me"
    }),
    false
  );
  assert.equal(
    isCompleteDeploymentConfig({
      WEB_PORT: "3000",
      API_PORT: "8787",
      AUTH_PUBLIC_BASE_URL: "http://127.0.0.1:3000",
      AUTH_SESSION_SECRET: "existing-session-secret-value",
      SECRET_MASTER_KEY: "existing-master-secret-value"
    }),
    true
  );
});
