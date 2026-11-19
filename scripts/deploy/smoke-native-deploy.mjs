import { parse, stringify } from "yaml";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createServer } from "node:net";
import { cp, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { captureOutput, createLogger } from "../lib/logging.mjs";

const ROOT = path.join(path.dirname(fileURLToPath(import.meta.url)), "../..");
const FIXTURE_SECRET = "fixture-deploy-secret-at-least-32-chars";

async function reservePort() {
  return new Promise((resolve, reject) => {
    const server = createServer();
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      const port = typeof address === "object" && address ? address.port : null;
      server.close((error) => {
        if (error) reject(error);
        else resolve(port);
      });
    });
    server.on("error", reject);
  });
}

function run(command, args, options = {}) {
  const result = spawnSync(command, args, {
    cwd: options.cwd,
    env: options.env ?? process.env,
    encoding: "utf8",
    timeout: options.timeout ?? 20 * 60_000
  });
  if (result.status !== 0) {
    const output = `${result.stdout ?? ""}\n${result.stderr ?? ""}`;
    throw new Error(`${command} ${args.join(" ")} failed (${result.status}):\n${output}`);
  }
  return result;
}

async function httpGet(url) {
  const response = await fetch(url, { redirect: "manual" });
  return { status: response.status, ok: response.status >= 200 && response.status < 400 };
}

function shouldCopy(source) {
  const relative = path.relative(ROOT, source);
  if (!relative || relative.startsWith("..")) return true;
  const parts = relative.split(path.sep);
  const blocked = new Set([
    ".git",
    "node_modules",
    "storage",
    ".next",
    "dist",
    ".venv",
    "venv",
    ".worktrees",
    "coverage"
  ]);
  if (parts.some((part) => blocked.has(part))) return false;
  if (relative === ".env" || relative === path.join("apps", "web", ".env.local")) return false;
  // Stale tsbuildinfo from the source tree can make tsc skip emit in the temp checkout (no dist/).
  if (path.basename(source).endsWith(".tsbuildinfo")) return false;
  return true;
}

async function copyCheckout(destination) {
  await cp(ROOT, destination, {
    recursive: true,
    filter: (source) => shouldCopy(source)
  });
  await mkdir(path.join(destination, "storage"), { recursive: true });
  await writeFile(path.join(destination, "storage", "sentinel.txt"), "native-deploy-sentinel\n");
}

function scanForSecrets(text) {
  if (text.includes(FIXTURE_SECRET)) {
    throw new Error("Fixture secret leaked into logs or status output");
  }
}


async function main() {
  const webPort = await reservePort();
  const apiPort = await reservePort();
  const tempRoot = await mkdtemp(path.join(os.tmpdir(), "datagallery-native-deploy-"));
  let stopped = false;
  let failed = false;

  const env = {
    ...process.env,
    CI: "true",
    SKIP_POSTINSTALL_BUILD: "0",
    DATAAGENT_HOME: path.join(tempRoot, "dataagent-home")
  };
  for (const key of ["LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY"]) {
    delete env[key];
  }
  env.UV_PROJECT_ENVIRONMENT = path.join(tempRoot, "runtime/agentkit/.venv");

  try {
    console.log(`[smoke:native-deploy] checkout=${tempRoot}`);
    console.log(`[smoke:native-deploy] web=${webPort} api=${apiPort}`);
    await copyCheckout(tempRoot);
    const home = path.join(tempRoot, "dataagent-home");
    await mkdir(home, { recursive: true });
    const template = parse(await readFile(path.join(tempRoot, "runtime/agentkit/dataagent/bootstrap/templates/config.yaml"), "utf8"));
    template.server = { host: "127.0.0.1", port: apiPort };
    await writeFile(
      path.join(home, "config.yaml"),
      `${stringify(template)}\n`
    );

    // Inject fixture secret into the shared redacting backend log used in production.
    const redactProbe = captureOutput(createLogger({ file: "backend", env: { DATAAGENT_HOME: home } }));
    redactProbe.append(
      [
        `LLM_API_KEY=${FIXTURE_SECRET}`,
        `Authorization: Bearer ${FIXTURE_SECRET}`,
        `{"apiKey":"${FIXTURE_SECRET}"}`,
        `https://user:${FIXTURE_SECRET}@example.com/callback`,
        "npm ci output",
        ""
      ].join("\n")
    );
    redactProbe.flush();
    const probeLog = await readFile(
      path.join(home, "runtime/logs/backend.log"),
      "utf8"
    );
    scanForSecrets(probeLog);
    assert.match(probeLog, /LLM_API_KEY=\[REDACTED\]/);
    assert.match(probeLog, /Authorization:.*REDACTED/i);

    run("bash", ["./deploy.sh", "deploy", "--non-interactive", "--web-port", String(webPort)], {
      cwd: tempRoot,
      env,
      timeout: 25 * 60_000
    });

    run("uv", ["run", "--no-sync", "--project", path.join(tempRoot, "runtime/agentkit"),
      "python", "-c", "import importlib.util; import pymysql, psycopg2, av, PIL; assert all(importlib.util.find_spec(name) is None for name in ('pytest', 'pyspark'))"],
    { cwd: tempRoot, env });

    const status = run("bash", ["./deploy.sh", "status"], { cwd: tempRoot, env });
    scanForSecrets(`${status.stdout}\n${status.stderr}`);
    assert.match(status.stdout, /进程\s+running/);

    const healthz = await httpGet(`http://127.0.0.1:${apiPort}/healthz`);
    const ready = await httpGet(`http://127.0.0.1:${apiPort}/ready`);
    const web = await httpGet(`http://127.0.0.1:${webPort}/`);
    assert.equal(healthz.status, 200);
    // No LLM_* in the fixture env, so the Python control plane stays up but not ready.
    assert.equal(ready.status, 503);
    assert.ok(web.ok, `Web should be reachable, got ${web.status}`);

    const envText = await readFile(path.join(tempRoot, ".env"), "utf8");
    assert.doesNotMatch(envText, /^LLM_API_KEY=.+$/m);
    assert.doesNotMatch(envText, /^LLM_MODEL=.+$/m);

    run("bash", ["./deploy.sh", "restart", "--web-port", String(webPort)], { cwd: tempRoot, env, timeout: 10 * 60_000 });
    const statusAfterRestart = run("bash", ["./deploy.sh", "status"], { cwd: tempRoot, env });
    scanForSecrets(`${statusAfterRestart.stdout}\n${statusAfterRestart.stderr}`);
    assert.match(statusAfterRestart.stdout, /进程\s+running/);

    const sentinel = await readFile(path.join(tempRoot, "storage", "sentinel.txt"), "utf8");
    assert.equal(sentinel, "native-deploy-sentinel\n");

    run("bash", ["./deploy.sh", "stop"], { cwd: tempRoot, env });
    run("bash", ["./deploy.sh", "stop"], { cwd: tempRoot, env });
    stopped = true;

    const finalStatus = run("bash", ["./deploy.sh", "status"], { cwd: tempRoot, env });
    scanForSecrets(`${finalStatus.stdout}\n${finalStatus.stderr}`);
    assert.match(finalStatus.stdout, /进程\s+stopped/);
    assert.equal(
      await readFile(path.join(tempRoot, "storage", "sentinel.txt"), "utf8"),
      "native-deploy-sentinel\n"
    );

    for (const relative of [
      "dataagent-home/runtime/logs/backend.log",
      "dataagent-home/runtime/logs/web.log",
      "dataagent-home/runtime/logs/tui.log"
    ]) {
      try {
        const logs = await readFile(path.join(tempRoot, relative), "utf8");
        scanForSecrets(logs);
      } catch (error) {
        if (error?.code !== "ENOENT") throw error;
      }
    }

    console.log("[smoke:native-deploy] ok");
  } catch (error) {
    failed = true;
    console.error(`[smoke:native-deploy] logs retained at ${path.join(tempRoot, "dataagent-home/runtime/logs")}`);
    throw error;
  } finally {
    if (!stopped) {
      try {
        spawnSync("bash", ["./deploy.sh", "stop"], {
          cwd: tempRoot,
          env,
          encoding: "utf8",
          timeout: 60_000
        });
      } catch {
        // best effort
      }
    }
    if (!failed) await rm(tempRoot, { recursive: true, force: true });
  }
}

await main();
