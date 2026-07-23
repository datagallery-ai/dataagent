import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createServer } from "node:net";
import { cp, mkdir, mkdtemp, readFile, readdir, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createDeployLogWriter } from "./cli.mjs";

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

async function preserveFailureLogs(tempRoot) {
  const sourceDir = path.join(tempRoot, "storage/logs");
  const targetDir = path.join(ROOT, "storage/logs");
  try {
    const entries = await readdir(sourceDir);
    await mkdir(targetDir, { recursive: true });
    for (const entry of entries) {
      const content = await readFile(path.join(sourceDir, entry), "utf8");
      scanForSecrets(content);
      await writeFile(path.join(targetDir, entry), content, { encoding: "utf8", mode: 0o600 });
    }
  } catch (error) {
    if (error?.code !== "ENOENT") {
      console.error(`[smoke:native-deploy] failed to preserve logs: ${error.message}`);
    }
  }
}

async function main() {
  const webPort = await reservePort();
  const apiPort = await reservePort();
  const tempRoot = await mkdtemp(path.join(os.tmpdir(), "datafoundry-native-deploy-"));
  let stopped = false;

  const env = {
    ...process.env,
    WEB_HOST: "127.0.0.1",
    WEB_PORT: String(webPort),
    API_HOST: "127.0.0.1",
    API_PORT: String(apiPort),
    AUTH_PUBLIC_BASE_URL: `http://127.0.0.1:${webPort}`,
    AUTH_SESSION_SECRET: FIXTURE_SECRET,
    SECRET_MASTER_KEY: FIXTURE_SECRET,
    CI: "true",
    SKIP_POSTINSTALL_BUILD: "1"
  };

  try {
    console.log(`[smoke:native-deploy] checkout=${tempRoot}`);
    console.log(`[smoke:native-deploy] web=${webPort} api=${apiPort}`);
    await copyCheckout(tempRoot);

    // Inject fixture secret into the same redacting deploy-log path used in production.
    const redactProbe = await createDeployLogWriter(tempRoot, { timestamp: "smoke-redact-probe" });
    await redactProbe.append(
      [
        `AUTH_SESSION_SECRET=${FIXTURE_SECRET}`,
        `Authorization: Bearer ${FIXTURE_SECRET}`,
        `{"apiKey":"${FIXTURE_SECRET}"}`,
        `https://user:${FIXTURE_SECRET}@example.com/callback`,
        "npm ci output",
        ""
      ].join("\n")
    );
    await redactProbe.finalize();
    const probeLog = await readFile(
      path.join(tempRoot, "storage/logs/deploy-smoke-redact-probe.log"),
      "utf8"
    );
    scanForSecrets(probeLog);
    assert.match(probeLog, /AUTH_SESSION_SECRET=\*+/);
    assert.match(probeLog, /Authorization: Bearer \*+/i);

    run("bash", ["./deploy.sh", "deploy", "--non-interactive"], {
      cwd: tempRoot,
      env,
      timeout: 25 * 60_000
    });

    const status = run("bash", ["./deploy.sh", "status"], { cwd: tempRoot, env });
    scanForSecrets(`${status.stdout}\n${status.stderr}`);
    assert.match(status.stdout, /进程\s+running/);

    const healthz = await httpGet(`http://127.0.0.1:${apiPort}/healthz`);
    const ready = await httpGet(`http://127.0.0.1:${apiPort}/ready`);
    const web = await httpGet(`http://127.0.0.1:${webPort}/`);
    assert.equal(healthz.status, 200);
    assert.equal(ready.status, 200);
    assert.ok(web.ok, `Web should be reachable, got ${web.status}`);

    const envText = await readFile(path.join(tempRoot, ".env"), "utf8");
    assert.match(envText, new RegExp(`^AUTH_SESSION_SECRET=${FIXTURE_SECRET}$`, "m"));
    assert.doesNotMatch(envText, /^LLM_API_KEY=.+$/m);
    assert.doesNotMatch(envText, /^LLM_MODEL=.+$/m);

    run("bash", ["./deploy.sh", "restart"], { cwd: tempRoot, env, timeout: 10 * 60_000 });
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
      "storage/logs/datafoundry.log",
      "storage/logs/deploy-latest.log",
      "storage/logs/deploy-smoke-redact-probe.log"
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
    await preserveFailureLogs(tempRoot);
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
    await rm(tempRoot, { recursive: true, force: true });
  }
}

await main();
