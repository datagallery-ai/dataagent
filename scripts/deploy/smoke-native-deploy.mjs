import { parse, stringify } from "yaml";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createServer } from "node:net";
import { access, cp, mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { captureOutput, createLogger } from "../../ui/shared/logging.mjs";

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
  const response = await fetch(url, { redirect: "manual", headers: { connection: "close" } });
  await response.body?.cancel();
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
  if (relative === ".env" || relative === path.join("ui", "web", ".env.local")) return false;
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
    DATAAGENT_HOME: path.join(tempRoot, "dataagent-home")
  };
  for (const key of ["LLM_MODEL", "LLM_BASE_URL", "LLM_API_KEY"]) {
    delete env[key];
  }
  delete env.UV_PROJECT_ENVIRONMENT;

  try {
    console.log(`[smoke:native-deploy] checkout=${tempRoot}`);
    console.log(`[smoke:native-deploy] web=${webPort} api=${apiPort}`);
    await copyCheckout(tempRoot);
    const home = path.join(tempRoot, "dataagent-home");
    await mkdir(home, { recursive: true });
    const template = parse(await readFile(path.join(tempRoot, "dataagent/bootstrap/templates/config.yaml"), "utf8"));
    template.server = { host: "127.0.0.1", port: apiPort };
    const configPath = path.join(tempRoot, "deployment.yaml");
    await writeFile(
      configPath,
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
        "npm install output",
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

    const deployment = run("bash", ["./deploy.sh", "deploy", "--non-interactive", "--web-port", String(webPort), "--config", configPath], {
      cwd: tempRoot,
      env,
      timeout: 25 * 60_000
    });
    assert.match(deployment.stdout, /预检通过/);

    run("uv", ["run", "--no-sync", "--project", path.join(tempRoot, "."),
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

    const doctor = run("bash", ["./deploy.sh", "doctor"], { cwd: tempRoot, env });
    assert.ok(doctor.stdout.includes(`config web=http://127.0.0.1:${webPort} api=http://127.0.0.1:${apiPort}`));
    const outdated = path.join(tempRoot, "outdated.yaml");
    await writeFile(outdated, stringify({ ...template, schema_version: 3 }));
    const invalidTarget = ["--config", outdated];
    const invalidDoctor = run("bash", ["./deploy.sh", "doctor", ...invalidTarget], { cwd: tempRoot, env });
    assert.match(invalidDoctor.stdout, /config: unavailable .*schema_version/);
    assert.throws(
      () => run("bash", ["./deploy.sh", "restart", ...invalidTarget], { cwd: tempRoot, env }),
      /Backend configuration check failed: .*schema_version/
    );
    assert.equal((await httpGet(`http://127.0.0.1:${apiPort}/healthz`)).status, 200);

    env.UV_PROJECT_ENVIRONMENT = path.join(tempRoot, "replacement-venv");
    await assert.rejects(access(env.UV_PROJECT_ENVIRONMENT), { code: "ENOENT" });
    run("bash", ["./deploy.sh", "deploy", "--non-interactive"], { cwd: tempRoot, env, timeout: 25 * 60_000 });
    await access(path.join(env.UV_PROJECT_ENVIRONMENT, "bin/python"));
    const state = JSON.parse(await readFile(path.join(tempRoot, "storage/run/deployment.json"), "utf8"));
    assert.deepEqual(state.ports, { web: webPort, api: apiPort });
    assert.equal(state.configPath, configPath);

    const restart = ["./deploy.sh", "restart", "--web-host", "0.0.0.0"];
    const otherListener = createServer();
    await new Promise((resolve, reject) => {
      otherListener.once("error", reject);
      otherListener.listen(webPort, "127.0.0.2", resolve);
    });
    try {
      assert.throws(() => run("bash", restart, { cwd: tempRoot, env }), /Web port .* already in use/);
      assert.ok((await httpGet(`http://127.0.0.1:${webPort}/`)).ok);
    } finally {
      await new Promise(resolve => otherListener.close(resolve));
    }
    run("bash", restart, { cwd: tempRoot, env, timeout: 10 * 60_000 });
    const statusAfterRestart = run("bash", ["./deploy.sh", "status"], { cwd: tempRoot, env });
    scanForSecrets(`${statusAfterRestart.stdout}\n${statusAfterRestart.stderr}`);
    assert.match(statusAfterRestart.stdout, /进程\s+running/);
    assert.equal((await httpGet(`http://127.0.0.1:${apiPort}/healthz`)).status, 200);
    assert.ok((await httpGet(`http://127.0.0.1:${webPort}/`)).ok);
    assert.ok((await httpGet(`http://127.0.0.2:${webPort}/`)).ok);

    const ipv6Config = path.join(tempRoot, "ipv6.yaml");
    await writeFile(ipv6Config, stringify({ ...template, server: { host: "::1", port: webPort } }));
    run("bash", ["./deploy.sh", "restart", "--web-host", "127.0.0.1", "--config", ipv6Config], {
      cwd: tempRoot, env, timeout: 10 * 60_000
    });
    assert.equal((await httpGet(`http://[::1]:${webPort}/healthz`)).status, 200);
    assert.ok((await httpGet(`http://127.0.0.1:${webPort}/`)).ok);

    const sentinel = await readFile(path.join(tempRoot, "storage", "sentinel.txt"), "utf8");
    assert.equal(sentinel, "native-deploy-sentinel\n");

    run("bash", ["./deploy.sh", "stop"], { cwd: tempRoot, env });
    run("bash", ["./deploy.sh", "stop"], { cwd: tempRoot, env });
    stopped = true;

    const finalStatus = run("bash", ["./deploy.sh", "status"], { cwd: tempRoot, env });
    scanForSecrets(`${finalStatus.stdout}\n${finalStatus.stderr}`);
    assert.match(finalStatus.stdout, /进程\s+stopped/);

    const previewHome = path.join(tempRoot, "preview-home");
    const preview = run("bash", ["./deploy.sh", "doctor"], {
      cwd: tempRoot,
      env: {
        ...env, DATAAGENT_HOME: previewHome, LLM_MODEL: "fixture",
        LLM_BASE_URL: "http://127.0.0.1:1/v1", LLM_API_KEY: FIXTURE_SECRET
      }
    });
    assert.match(preview.stdout, /^config: ok$/m);
    await assert.rejects(access(previewHome), { code: "ENOENT" });
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
