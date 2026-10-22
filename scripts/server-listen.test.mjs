import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { writeServerListen } from "./server-listen.mjs";

const agentkit = fileURLToPath(new URL("../runtime/agentkit", import.meta.url));

function tempHome() {
  return mkdtempSync(join(tmpdir(), "dataagent-listen-"));
}

function loadPrimaryModel(home) {
  const result = spawnSync(
    "uv",
    ["run", "--project", agentkit, "--python", "3.12", "python", "-"],
    {
      cwd: agentkit,
      encoding: "utf8",
      input: [
        "import json, os",
        "from dataagent.bootstrap import LaunchOptions, prepare_runtime",
        "runtime = prepare_runtime(LaunchOptions(cwd=os.environ['DATAAGENT_HOME']))",
        "model = runtime.settings.models['primary']",
        "print(json.dumps({",
        "    'name': model.name,",
        "    'base_url': model.base_url,",
        "    'host': runtime.settings.server.host,",
        "    'port': runtime.settings.server.port,",
        "}))",
        "",
      ].join("\n"),
      env: {
        ...process.env,
        DATAAGENT_HOME: home,
        LLM_MODEL: "review-model",
        LLM_BASE_URL: "https://example.invalid/v1",
        LLM_API_KEY: "test-key",
      },
    },
  );
  assert.equal(result.status, 0, result.stderr || result.stdout);
  return JSON.parse(result.stdout);
}

test("a new Home gets the full template and can load its model", () => {
  const home = tempHome();
  try {
    writeServerListen({ DATAAGENT_HOME: home }, { host: "127.0.0.1", port: "8801" });
    const doc = JSON.parse(readFileSync(join(home, "config.json"), "utf8"));
    assert.equal(doc.schema_version, 3);
    assert.equal(doc.models.primary.provider, "openai");
    assert.equal(doc.models.primary.name, "$env{LLM_MODEL}");
    assert.equal(doc.models.primary.base_url, "$env{LLM_BASE_URL}");
    assert.equal(doc.models.primary.api_key, "$env{LLM_API_KEY}");
    assert.deepEqual(doc.dataagent.hooks.before_agent, []);
    assert.deepEqual(doc.plugins.enabled, []);
    assert.equal(doc.server.host, "127.0.0.1");
    assert.equal(doc.server.port, 8801);

    const loaded = loadPrimaryModel(home);
    assert.equal(loaded.name, "review-model");
    assert.equal(loaded.base_url, "https://example.invalid/v1");
    assert.equal(loaded.host, "127.0.0.1");
    assert.equal(loaded.port, 8801);
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
});

test("an existing config only updates the listen fields", () => {
  const home = tempHome();
  const file = join(home, "config.json");
  const original = {
    schema_version: 3,
    models: {
      primary: { name: "kept-model", base_url: "https://kept.example/v1", api_key: "kept-secret" },
    },
    plugins: { enabled: ["demo"], paths: ["./demo"] },
    dataagent: { hooks: { before_agent: [{ entrypoint: "hooks/audit.py:handle" }] } },
    server: { host: "0.0.0.0", port: 1 },
  };
  try {
    writeFileSync(file, JSON.stringify(original));
    writeServerListen({ DATAAGENT_HOME: home }, { host: "127.0.0.1", port: 8810 });
    const doc = JSON.parse(readFileSync(file, "utf8"));
    assert.deepEqual(doc.models, original.models);
    assert.deepEqual(doc.plugins, original.plugins);
    assert.deepEqual(doc.dataagent, original.dataagent);
    assert.equal(doc.schema_version, 3);
    assert.deepEqual(doc.server, { host: "127.0.0.1", port: 8810 });
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
});

test("invalid JSON is left byte for byte unchanged", () => {
  const home = tempHome();
  const file = join(home, "config.json");
  const original = Buffer.from('{\n  "models": {"primary": {"name": "kept"}},\n}\n');
  try {
    writeFileSync(file, original);
    assert.throws(
      () => writeServerListen({ DATAAGENT_HOME: home }, { host: "127.0.0.1", port: "8802" }),
      /not valid JSON and was left unchanged/,
    );
    assert.deepEqual(readFileSync(file), original);
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
});

test("a JSON value that is not an object is left unchanged", () => {
  const home = tempHome();
  const file = join(home, "config.json");
  const original = Buffer.from("[1, 2]\n");
  try {
    writeFileSync(file, original);
    assert.throws(
      () => writeServerListen({ DATAAGENT_HOME: home }, { host: "127.0.0.1", port: "8803" }),
      /not a JSON object and was left unchanged/,
    );
    assert.deepEqual(readFileSync(file), original);
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
});
