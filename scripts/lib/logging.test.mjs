import assert from "node:assert/strict";
import { chmodSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { execFileSync } from "node:child_process";
import { captureOutput, createLogger, redactLogText, registerLogSecrets } from "./logging.mjs";

test("large diagnostic redaction completes without catastrophic backtracking", () => {
  execFileSync(process.execPath, ["--input-type=module", "-e", `
    import { redactLogText } from ${JSON.stringify(new URL("./log-redaction.mjs", import.meta.url).href)};
    const result = redactLogText('x'.repeat(500000) + '\\nBearer fixture-large-token');
    if (result.includes('fixture-large-token') || result.length < 500000) process.exit(1);
  `], { timeout: 5000 });
});

test("all writers preserve correlation, full stacks and redact before persisting", () => {
  const home = mkdtempSync(path.join(tmpdir(), "dataagent-logs-"));
  try {
    const env = { DATAAGENT_HOME: home };
    registerLogSecrets(["review-short", "review-short-long-secret"]);
    for (const file of ["backend", "web", "tui"]) {
      const logger = createLogger({ file, env });
      const error = new Error("review-short-long-secret Bearer transport-key");
      error.stack = "Frame\n" + "x".repeat(5000) + "\nfinal-code-location";
      logger.error("Run failed", { threadId: "thread-1", runId: "run-1", code: "MODEL_ERROR", message: "failure" });
      logger.error("Transport failed", error, { threadId: "thread-1", runId: "run-2" });
      const text = readFileSync(logger.path, "utf8");
      const records = text.trim().split("\n").map(JSON.parse);
      assert.equal(records[0].runId, "run-1");
      assert.equal(records[0].code, "MODEL_ERROR");
      assert.equal(records[0].errorMessage, "failure");
      assert(records[1].error.stack.includes("\nfinal-code-location"));
      assert(!text.includes("long-secret") && !text.includes("transport-key"));
      if (process.platform !== "win32") assert.equal(statSync(logger.path).mode & 0o777, 0o600);
    }
    assert.deepEqual(readdirSync(path.join(home, "runtime/logs")).sort(), ["backend.log", "tui.log", "web.log"]);
  } finally { rmSync(home, { recursive: true, force: true }); }
});

test("captured output redacts split credentials and signed URLs, flushes tails and preserves component", () => {
  const home = mkdtempSync(path.join(tmpdir(), "dataagent-capture-"));
  try {
    const logger = createLogger({ file: "backend", component: "launcher", env: { DATAAGENT_HOME: home } });
    let echoed = "";
    const output = captureOutput(logger, { echo: { write: text => { echoed += text; } } });
    output.append("LLM_API_KEY=split-");
    output.append("secret\nhttps://obs.invalid/log?X-Amz-Signature=signed-secret&ok=1\n");
    output.append(JSON.stringify({ level: "ERROR", component: "plugin", message: "failure", runId: "run-3" }) + "\n");
    output.append("last-line");
    output.flush();
    const text = readFileSync(logger.path, "utf8");
    assert(!text.includes("split-secret") && !text.includes("signed-secret"));
    assert(!echoed.includes("split-secret") && !echoed.includes("signed-secret"));
    const records = text.trim().split("\n").map(JSON.parse);
    assert.equal(records[2].component, "plugin");
    assert.equal(records[2].runId, "run-3");
    assert.equal(records[3].message, "last-line");
  } finally { rmSync(home, { recursive: true, force: true }); }
});

test("rotation is bounded and circular or BigInt metadata cannot break error handling", () => {
  const home = mkdtempSync(path.join(tmpdir(), "dataagent-rotation-"));
  try {
    const logger = createLogger({ file: "tui", env: { DATAAGENT_HOME: home }, maxBytes: 250, backups: 2 });
    logger.info("legacy log");
    chmodSync(logger.path, 0o644);
    logger.warn("large".repeat(100));
    if (process.platform !== "win32") assert.equal(statSync(`${logger.path}.1`).mode & 0o777, 0o600);
    const metadata = { count: 1n }; metadata.self = metadata;
    for (let i = 0; i < 10; i++) assert.doesNotThrow(() => logger.warn("message".repeat(20), metadata));
    assert.deepEqual(readdirSync(path.dirname(logger.path)).sort(), ["tui.log", "tui.log.1", "tui.log.2"]);
  } finally { rmSync(home, { recursive: true, force: true }); }
});

test("quoted credentials with spaces and secrets loaded after initialization stay private", () => {
  for (const input of ['{"password":"first middle last"}', "{'cookie': 'session=first; id=last'}"]) {
    const text = redactLogText(input);
    assert(!text.includes("first") && !text.includes("last"));
  }
  const home = mkdtempSync(path.join(tmpdir(), "dataagent-late-env-"));
  try {
    const env = { DATAAGENT_HOME: home };
    const logger = createLogger({ file: "web", env });
    env.LLM_API_KEY = "fixture-late-env-value";
    logger.error("failure: fixture-late-env-value");
    assert(!readFileSync(logger.path, "utf8").includes(env.LLM_API_KEY));
  } finally { rmSync(home, { recursive: true, force: true }); }
});

test("ordinary environment values survive while real credentials remain redacted", () => {
  const home = mkdtempSync(path.join(tmpdir(), "dataagent-credential-selection-"));
  try {
    const logger = createLogger({ file: "web", env: {
      DATAAGENT_HOME: home, KEYBOARD_LAYOUT: "en", TOKENIZERS_PARALLELISM: "false",
      TOKENIZER_MODEL_URL: "http://10.0.0.8:8080", MODEL_TOKEN_LIMIT: "128",
      LLM_API_KEY: "fixture-private-api-key",
    } });
    logger.error("open target=http://10.0.0.8:8080 tokenizer=false limit=128 credential=fixture-private-api-key", { token_count: 128 });
    const record = JSON.parse(readFileSync(logger.path, "utf8"));
    assert(record.message.includes("open target=http://10.0.0.8:8080 tokenizer=false limit=128"));
    assert(!record.message.includes("fixture-private-api-key"));
    assert.equal(record.token_count, 128);
  } finally { rmSync(home, { recursive: true, force: true }); }
});

test("short credentials redact complete values without corrupting words or paths", () => {
  registerLogSecrets(["en"]);
  assert.equal(redactLogText("open /opt/en/client.py supplied en password=en"),
    "open /opt/en/client.py supplied [REDACTED] password=[REDACTED]");
  assert.equal(redactLogText("https://host/rpc?token_count=128&key=abc"),
    "https://host/rpc?token_count=128&key=[REDACTED]");
  assert.equal(redactLogText("https://user-only-token@host/path"), "https://[REDACTED]@host/path");
  assert.equal(redactLogText("denied \x1b[31men\x1b[0m"), "denied [REDACTED]");
  assert.equal(redactLogText(String.raw`open C:\opt\en\client.py en.config en/client.py`),
    String.raw`open C:\opt\en\client.py en.config en/client.py`);
});

test("short credentials remain private next to punctuation", () => {
  registerLogSecrets(["abc123"]);
  for (const suffix of [".", ":", ": denied", "...", ";", "!"]) {
    assert.equal(redactLogText(`Incorrect API key provided: abc123${suffix}`),
      `Incorrect API key provided: [REDACTED]${suffix}`);
    assert.equal(redactLogText(`provided:abc123${suffix}`), `provided:[REDACTED]${suffix}`);
  }
});
