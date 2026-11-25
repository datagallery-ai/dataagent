import assert from "node:assert/strict";
import { mkdtemp, readFile, stat, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import {
  ensureDeploymentEnvironment,
  redactSensitiveText,
  writeDeploymentConfiguration
} from "./config.mjs";

test("leaves an empty env file empty", () => {
  const result = ensureDeploymentEnvironment("");
  assert.equal(result.text, "");
  assert.deepEqual(result.env, {});
});

test("leaves existing env lines unchanged", () => {
  const source = "LLM_MODEL=qwen-plus\nCUSTOM_VALUE=keep-me\n";
  const result = ensureDeploymentEnvironment(source);
  assert.equal(result.text, source);
  assert.equal(result.env.LLM_MODEL, "qwen-plus");
  assert.equal(result.env.CUSTOM_VALUE, "keep-me");
});

test("reconfigure creates a backup and atomically writes .env", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "datagallery-config-"));
  await writeFile(path.join(root, ".env"), "LLM_API_KEY=old-secret\n");
  const result = ensureDeploymentEnvironment(await readFile(path.join(root, ".env"), "utf8"));
  const written = await writeDeploymentConfiguration(root, result.text, {
    backup: true,
    timestamp: "20260722-120000"
  });
  assert.equal(await readFile(written.backupPath, "utf8"), "LLM_API_KEY=old-secret\n");

  if (process.platform !== "win32") {
    for (const filePath of [
      path.join(root, ".env"),
      written.backupPath
    ]) {
      assert.equal((await stat(filePath)).mode & 0o777, 0o600, filePath);
    }
  }
});

test("redactSensitiveText masks secret-like keys", () => {
  const redacted = redactSensitiveText("LLM_API_KEY=super-secret\nNOTE=3000\n");
  assert.match(redacted, /LLM_API_KEY=\*+/);
  assert.match(redacted, /NOTE=3000/);
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
      "NOTE=3000"
    ].join("\n")
  );
  assert.doesNotMatch(redacted, /json-secret-value|also-secret|fixture-deploy-secret-at-least-32-chars|sk-abcdefghijklmnop|user:fixture|dbuser:dbpass/);
  assert.match(redacted, /Authorization: Bearer \*+/i);
  assert.match(redacted, /https:\/\/\*\*\*\*:\*\*\*\*@example\.com\/path/);
  assert.match(redacted, /postgres:\/\/\*\*\*\*:\*\*\*\*@localhost:5432\/app/);
  assert.match(redacted, /NOTE=3000/);
});

test("writeDeploymentConfiguration backs up existing secrets even without reconfigure", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "datagallery-config-secret-backup-"));
  const existing = "LLM_API_KEY=existing-secret-value\nNOTE=3000";
  await writeFile(path.join(root, ".env"), `${existing}\n`);

  const next = "LLM_API_KEY=existing-secret-value\nNOTE=3310";
  const written = await writeDeploymentConfiguration(root, `${next}\n`, {
    backup: false,
    backupExistingSecrets: true,
    timestamp: "20260726-120000"
  });
  assert.ok(written.backupPath);
  assert.match(await readFile(written.backupPath, "utf8"), /LLM_API_KEY=existing-secret-value/);
  assert.match(await readFile(path.join(root, ".env"), "utf8"), /NOTE=3310/);
});
