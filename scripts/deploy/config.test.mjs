import assert from "node:assert/strict";
import { mkdir, mkdtemp, readFile, stat, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import {
  assertNativeAuthPublicBaseUrl,
  assertNativeBindHosts,
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
  assert.equal(result.env.WEB_HOST, "127.0.0.1");
  assert.equal(result.env.API_HOST, "127.0.0.1");
  assert.equal(result.env.WEB_PORT, "3000");
  assert.equal(result.env.API_PORT, "8787");
  assert.equal(result.env.AUTH_PUBLIC_BASE_URL, "http://127.0.0.1:3000");
  assert.equal(result.env.AUTH_REGISTRATION_MODE, "open");
  assert.equal(result.env.AUTH_EMAIL_DELIVERY, "test");
  assert.equal(result.env.AUTH_SESSION_SECRET, "generated-secret-value");
  assert.equal(result.env.SECRET_MASTER_KEY, "generated-secret-value");
  assert.equal(result.env.LLM_API_KEY, undefined);
  assert.equal(result.env.DATAFOUNDRY_AUTH_MODE, undefined);
  assert.doesNotMatch(result.text, /DATAFOUNDRY_AUTH_MODE|NEXT_PUBLIC_DATAFOUNDRY_AUTH_MODE/);
  assert.doesNotMatch(result.text, /DATALINK_/);
});

test("strips legacy DATAFOUNDRY_AUTH_MODE from upgraded env text", () => {
  const source = [
    "WEB_PORT=3000",
    "API_PORT=8787",
    "AUTH_PUBLIC_BASE_URL=http://127.0.0.1:3000",
    "AUTH_SESSION_SECRET=existing-session-secret-value",
    "SECRET_MASTER_KEY=existing-master-secret-value",
    "AUTH_REGISTRATION_MODE=open",
    "DATAFOUNDRY_AUTH_MODE=password",
    "NEXT_PUBLIC_DATAFOUNDRY_AUTH_MODE=password",
    "CUSTOM_VALUE=keep-me"
  ].join("\n");
  const result = ensureDeploymentEnvironment(source, { generateSecrets: false });
  assert.equal(result.env.DATAFOUNDRY_AUTH_MODE, undefined);
  assert.equal(result.env.NEXT_PUBLIC_DATAFOUNDRY_AUTH_MODE, undefined);
  assert.equal(result.env.CUSTOM_VALUE, "keep-me");
  assert.doesNotMatch(result.text, /DATAFOUNDRY_AUTH_MODE|NEXT_PUBLIC_DATAFOUNDRY_AUTH_MODE/);
  assert.match(result.text, /^CUSTOM_VALUE=keep-me$/m);
  assert.deepEqual(result.removedKeys.sort(), [
    "DATAFOUNDRY_AUTH_MODE",
    "NEXT_PUBLIC_DATAFOUNDRY_AUTH_MODE"
  ]);
});

test("fills missing AUTH_REGISTRATION_MODE for legacy complete env", () => {
  const source = [
    "WEB_PORT=3000",
    "API_PORT=8787",
    "AUTH_PUBLIC_BASE_URL=http://127.0.0.1:3000",
    "AUTH_SESSION_SECRET=existing-session-secret-value",
    "SECRET_MASTER_KEY=existing-master-secret-value"
  ].join("\n");
  const result = ensureDeploymentEnvironment(source, { generateSecrets: false });
  assert.equal(result.env.AUTH_REGISTRATION_MODE, "open");
  assert.match(result.text, /^AUTH_REGISTRATION_MODE=open$/m);
  assert.equal(
    isCompleteDeploymentConfig(parseDeploymentEnvironment(source)),
    false,
    "legacy env without registration mode must not look complete"
  );
  assert.equal(isCompleteDeploymentConfig(result.env), true);
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
    API_HOST: "127.0.0.1",
    API_PORT: "8877"
  });
  assert.match(text, /NEXT_PUBLIC_AGENT_RUNTIME_URL=$/m);
  assert.match(text, /NEXT_PUBLIC_CONFIG_API_URL=$/m);
  assert.match(text, /API_PROXY_TARGET=http:\/\/127\.0\.0\.1:8877/);
  assert.doesNotMatch(text, /NEXT_PUBLIC_DATAFOUNDRY_AUTH_MODE/);
});

test("rejects non-loopback HTTP public URLs and wildcard bind hosts", () => {
  assert.throws(
    () => assertNativeAuthPublicBaseUrl("http://example.com"),
    /loopback|SSH|HTTPS/i
  );
  assert.doesNotThrow(() => assertNativeAuthPublicBaseUrl("http://127.0.0.1:3100"));
  assert.doesNotThrow(() => assertNativeAuthPublicBaseUrl("https://prod.example.com"));
  assert.throws(() => assertNativeBindHosts({ WEB_HOST: "0.0.0.0" }), /WEB_HOST/);
  assert.throws(() => assertNativeBindHosts({ API_HOST: "::" }), /API_HOST/);
  assert.doesNotThrow(() => assertNativeBindHosts({ WEB_HOST: "127.0.0.1", API_HOST: "127.0.0.1" }));
});

test("generates AUTH_PUBLIC_BASE_URL for a custom Web port", () => {
  const result = ensureDeploymentEnvironment("WEB_PORT=3100\n", {
    generateSecrets: false
  });
  assert.equal(result.env.AUTH_PUBLIC_BASE_URL, "http://127.0.0.1:3100");
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
      "postgres://dbuser:dbpass@localhost:5432/app",
      "token sk-abcdefghijklmnop",
      "WEB_PORT=3000"
    ].join("\n")
  );
  assert.doesNotMatch(redacted, /json-secret-value|also-secret|fixture-deploy-secret-at-least-32-chars|sk-abcdefghijklmnop|user:fixture|dbuser:dbpass/);
  assert.match(redacted, /Authorization: Bearer \*+/i);
  assert.match(redacted, /https:\/\/\*\*\*\*:\*\*\*\*@example\.com\/path/);
  assert.match(redacted, /postgres:\/\/\*\*\*\*:\*\*\*\*@localhost:5432\/app/);
  assert.match(redacted, /WEB_PORT=3000/);
});

test("writeDeploymentConfiguration backs up existing secrets even without reconfigure", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "datafoundry-config-secret-backup-"));
  await mkdir(path.join(root, "apps/web"), { recursive: true });
  const existing = [
    "AUTH_SESSION_SECRET=existing-session-secret-value",
    "SECRET_MASTER_KEY=existing-master-secret-value",
    "WEB_PORT=3000"
  ].join("\n");
  await writeFile(path.join(root, ".env"), `${existing}\n`);

  const next = [
    "AUTH_SESSION_SECRET=existing-session-secret-value",
    "SECRET_MASTER_KEY=existing-master-secret-value",
    "WEB_PORT=3310"
  ].join("\n");
  const written = await writeDeploymentConfiguration(root, `${next}\n`, "API_PROXY_TARGET=http://127.0.0.1:8787\n", {
    backup: false,
    backupExistingSecrets: true,
    timestamp: "20260726-120000"
  });
  assert.ok(written.backupPath);
  assert.match(await readFile(written.backupPath, "utf8"), /AUTH_SESSION_SECRET=existing-session-secret-value/);
  assert.match(await readFile(path.join(root, ".env"), "utf8"), /WEB_PORT=3310/);
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
      AUTH_REGISTRATION_MODE: "open",
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
    false
  );
  assert.equal(
    isCompleteDeploymentConfig({
      WEB_PORT: "3000",
      API_PORT: "8787",
      AUTH_PUBLIC_BASE_URL: "http://127.0.0.1:3000",
      AUTH_REGISTRATION_MODE: "open",
      AUTH_SESSION_SECRET: "existing-session-secret-value",
      SECRET_MASTER_KEY: "existing-master-secret-value"
    }),
    true
  );
});
