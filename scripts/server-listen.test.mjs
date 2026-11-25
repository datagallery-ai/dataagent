import { writeServerListen } from "./deploy/server-config.mjs";
import { parse } from "yaml";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, statSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { homedir, tmpdir } from "node:os";
import { join, resolve } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { readServerListen, resolveHome } from "../ui/shared/server-listen.mjs";

const agentkit = fileURLToPath(new URL("../.", import.meta.url));

function tempHome() {
  return mkdtempSync(join(tmpdir(), "dataagent-listen-"));
}

function runPython(args, options) {
  return spawnSync(
    "uv",
    ["run", "--no-sync", "--project", agentkit, "python", ...args],
    { cwd: agentkit, encoding: "utf8", ...options },
  );
}

function loadPrimaryModel(home) {
  const result = runPython(
    ["-"],
    {
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

test("listen address comes from Home config.yaml", () => {
  const home = tempHome();
  try {
    writeFileSync(join(home, "config.yaml"), JSON.stringify({
      server: { host: "0.0.0.0", port: 8877 },
    }));
    assert.deepEqual(readServerListen({ DATAAGENT_HOME: home }), {
      host: "0.0.0.0",
      port: "8877",
    });
    // A blank value means unset, like the backend: both read ~/.dataagent.
    assert.equal(resolveHome({ DATAAGENT_HOME: "  " }), resolveHome({}));
    const selected = join(home, "selected.yaml");
    writeFileSync(selected, JSON.stringify({ plugins: { enabled: [] } }));
    assert.deepEqual(readServerListen({ DATAAGENT_HOME: home }, selected), {
      host: "127.0.0.1",
      port: "8790",
    });
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
});

test("a new Home gets the full template and can load its model", () => {
  const home = tempHome();
  try {
    writeServerListen({ DATAAGENT_HOME: home }, { host: "127.0.0.1", port: "8801" });
    const doc = parse(readFileSync(join(home, "config.yaml"), "utf8"));
    assert.equal(doc.schema_version, 4);
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
  const file = join(home, "config.yaml");
  const original = {
    schema_version: 4,
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
    const doc = parse(readFileSync(file, "utf8"));
    assert.deepEqual(doc.models, original.models);
    assert.deepEqual(doc.plugins, original.plugins);
    assert.deepEqual(doc.dataagent, original.dataagent);
    assert.equal(doc.schema_version, 4);
    assert.deepEqual(doc.server, { host: "127.0.0.1", port: 8810 });
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
});

test("invalid YAML is left byte for byte unchanged", () => {
  const home = tempHome();
  const file = join(home, "config.yaml");
  const original = Buffer.from('models: [SECRET_VALUE\n');
  try {
    writeFileSync(file, original);
    assert.throws(
      () => writeServerListen({ DATAAGENT_HOME: home }, { host: "127.0.0.1", port: "8802" }),
      /invalid YAML syntax/,
    );
    assert.deepEqual(readFileSync(file), original);
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
});

test("a JSON value that is not an object is left unchanged", () => {
  const home = tempHome();
  const file = join(home, "config.yaml");
  const original = Buffer.from("[1, 2]\n");
  try {
    writeFileSync(file, original);
    assert.throws(
      () => writeServerListen({ DATAAGENT_HOME: home }, { host: "127.0.0.1", port: "8803" }),
      /expected a nonempty mapping document/,
    );
    assert.deepEqual(readFileSync(file), original);
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
});

test("resolveHome follows the backend rule for blank, ~ and relative values", () => {
  const fallback = join(homedir(), ".dataagent");
  assert.equal(resolveHome({}), fallback);
  assert.equal(resolveHome({ DATAAGENT_HOME: "" }), fallback);
  assert.equal(resolveHome({ DATAAGENT_HOME: " \t" }), fallback);
  assert.equal(resolveHome({ DATAAGENT_HOME: "~" }), homedir());
  assert.equal(resolveHome({ DATAAGENT_HOME: "~/da" }), join(homedir(), "da"));
  assert.equal(resolveHome({ DATAAGENT_HOME: "rel/home" }), resolve(process.cwd(), "rel/home"));
});

test('block YAML comments and placeholders survive a private atomic edit', () => {
  const home = tempHome();
  const file = join(home, 'config.yaml');
  try {
    writeFileSync(file, '# preserved header\nmodels: # preserved models\n  primary:\n    api_key: "$env{SECRET}"\nserver:\n  host: localhost # retained host comment\n  port: 8790\n');
    writeServerListen({ DATAAGENT_HOME: home }, { host: '127.0.0.2', port: 8812 });
    const text = readFileSync(file, 'utf8');
    assert.match(text, /# preserved header/);
    assert.match(text, /# preserved models/);
    assert.match(text, /# retained host comment/);
    assert.match(text, /\$env\{SECRET\}/);
    assert.equal(statSync(file).mode & 0o777, 0o600);
    assert.deepEqual(readServerListen({ DATAAGENT_HOME: home }), { host: '127.0.0.2', port: '8812' });
  } finally { rmSync(home, { recursive: true, force: true }); }
});

test('server env uses Home dotenv and nonempty process values with explicit yml', () => {
  const home = tempHome();
  try {
    const file = join(home, 'team.yml');
    writeFileSync(file, 'server:\n  host: $env{BIND_HOST}\n  port: $env{BIND_PORT}\nmodels:\n  unused:\n    api_key: $env{ABSENT}\n');
    writeFileSync(join(home, '.env'), 'BIND_HOST=127.0.0.2\nBIND_PORT=8813\n');
    assert.deepEqual(readServerListen({ DATAAGENT_HOME: home, BIND_HOST: '', BIND_PORT: '8814' }, file), { host: '127.0.0.2', port: '8814' });
  } finally { rmSync(home, { recursive: true, force: true }); }
});

test('Home configuration reads and writes only YAML', () => {
  const home = tempHome();
  const env = { DATAAGENT_HOME: home };
  const json = join(home, 'config.json');
  try {
    writeFileSync(json, '{"server":{"port":9999}}');
    assert.deepEqual(readServerListen(env), { host: '127.0.0.1', port: '8790' });
    assert.throws(() => readServerListen(env, json), /YAML extension required/);
    const listen = { host: 'localhost', port: '8813' };
    writeServerListen(env, listen);
    assert.deepEqual(readServerListen(env), listen);
    assert.equal(readFileSync(json, 'utf8'), '{"server":{"port":9999}}');
  } finally { rmSync(home, { recursive: true, force: true }); }
});

const dotenvCases = [
  { name: 'file order', text: 'BASE_PORT=8811\nAPI_PORT=${BASE_PORT}\n' },
  { name: 'duplicate bindings and process precedence', text: 'BASE_PORT=8811\nAPI_PORT=${BASE_PORT}\nBASE_PORT=8812\nNEXT=${BASE_PORT}\n', env: { BASE_PORT: '8890', API_PORT: '8813' } },
  { name: 'forward references use process then defaults', text: 'API_PORT=${BASE_PORT:-8814}\nBASE_PORT=8812\n', env: { BASE_PORT: '8815' } },
  { name: 'missing default', text: 'API_PORT=${ABSENT:-8816}\n' },
  { name: 'empty and valueless shadow interpolation only', text: 'EMPTY=\nNONE\nCHECK=${EMPTY:-fallback}/${NONE:-fallback}\nAPI_PORT=8817\n', env: { EMPTY: 'process', NONE: 'process', API_PORT: '' } },
  { name: 'reserved bindings participate before filtering', text: 'DATAAGENT_HOME=ignored\nDATAAGENT_INSTANCE_ID=ignored\nCHECK=${DATAAGENT_HOME}/${DATAAGENT_INSTANCE_ID}\nAPI_PORT=8818\n' },
  { name: 'quotes comments multiline and export', text: '\uFEFFexport BASE_PORT=8819 # comment\nAPI_PORT="${BASE_PORT}"\n\'KEY\'=\'中文\nline\'\nESCAPED="a\\nb\\tend"\nHASH=value#kept\n' },
];
for (const item of dotenvCases) test(`Python/Node dotenv parity: ${item.name}`, async () => {
  const { effectiveEnvironment } = await import('../ui/shared/config-env.mjs');
  const home = tempHome();
  try {
    const config = join(home, 'config.yaml');
    writeFileSync(config, 'server:\n  host: backend.example.test\n  port: $env{API_PORT}\n');
    writeFileSync(join(home, '.env'), item.text);
    const env = { DATAAGENT_HOME: home, ...item.env };
    const result = runPython(['-c', `
import json, os, sys
from pathlib import Path
from dataagent.bootstrap.environment import load_env
from dataagent.bootstrap.startup import server_listen
from dataagent.bootstrap.options import LaunchOptions
payload = json.load(sys.stdin)
os.environ.clear()
os.environ.update(payload)
values = dict(load_env((Path(payload['DATAAGENT_HOME']) / '.env',)).values)
host, port = server_listen(LaunchOptions())
print(json.dumps({'values': values, 'listen': {'host': host, 'port': str(port)}}))
`], { input: JSON.stringify(env), encoding: 'utf8', env: { ...process.env, DATAAGENT_HOME: home } });
    assert.equal(result.status, 0, result.stderr);
    const expected = JSON.parse(result.stdout);
    assert.deepEqual({ ...effectiveEnvironment(env, home) }, expected.values);
    assert.deepEqual(readServerListen(env), expected.listen);
  } finally { rmSync(home, { recursive: true, force: true }); }
});

test('explicit env file replaces Home dotenv for Python and Node', () => {
  const home = tempHome();
  try {
    const config = join(home, 'selected.yml');
    const selected = join(home, 'selected.env');
    writeFileSync(config, 'server:\n  host: backend.example.test\n  port: $env{REVIEW_API_PORT}\n');
    writeFileSync(join(home, '.env'), 'REVIEW_API_PORT=9999\n');
    writeFileSync(selected, 'BASE_PORT=8811\nREVIEW_API_PORT=${BASE_PORT}\n');
    const result = runPython(['-c', `
import json, os, sys
from dataagent.bootstrap.startup import server_listen
from dataagent.bootstrap.options import LaunchOptions
os.environ.clear()
os.environ.update(json.load(sys.stdin))
host, port = server_listen(LaunchOptions(config=sys.argv[1], env_file=sys.argv[2]))
print(json.dumps({'host': host, 'port': str(port)}))
`, config, selected], { input: JSON.stringify({ DATAAGENT_HOME: home }), env: { ...process.env, DATAAGENT_HOME: home } });
    assert.equal(result.status, 0, result.stderr);
    assert.deepEqual(readServerListen({ DATAAGENT_HOME: home }, config, selected), JSON.parse(result.stdout));
    assert.throws(() => readServerListen({ DATAAGENT_HOME: home }, config, selected + '.missing'), /environment file does not exist/);
  } finally { rmSync(home, { recursive: true, force: true }); }
});
