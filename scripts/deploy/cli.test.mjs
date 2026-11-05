import assert from "node:assert/strict";
import { existsSync, mkdtempSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { mkdtemp, readdir, readFile, writeFile, mkdir } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { readServerListen } from "../server-listen.mjs";
import { pythonControlPlaneLaunch, resolveStackRuntimeConfig } from "../stack-runtime-config.mjs";
import { parseDeployArgs } from "./args.mjs";
import {
  configureDeploymentInteractively,
  collectManagedPorts,
  createDeployLogWriter,
  formatStartBlocked,
  apiListenFromState,
  healthProbeUrls,
  inspectWritablePath,
  managedWebStartArgs,
  prepareStartEnvironment,
  resolveTuiRuntimeUrl,
  runDeploymentDoctor,
  webListenFromState
} from "./cli.mjs";

function createAsk(answers) {
  const queue = [...answers];
  return async () => {
    if (queue.length === 0) throw new Error("unexpected prompt");
    return queue.shift();
  };
}

test("health probes use the configured web and api hosts", () => {
  const home = mkdtempSync(path.join(os.tmpdir(), "df-listen-"));
  writeFileSync(path.join(home, "config.json"), JSON.stringify({
    server: { host: "0.0.0.0", port: 8877 }
  }));
  const env = { DATAAGENT_HOME: home };
  try {
    assert.deepEqual(healthProbeUrls(env, { host: "::1", port: "3000" }), {
      apiBaseUrl: "http://0.0.0.0:8877",
      webUrl: "http://[::1]:3000"
    });
    assert.equal(
      resolveTuiRuntimeUrl(env),
      "http://127.0.0.1:8877/dataagent/stream"
    );
    writeFileSync(path.join(home, "config.json"), JSON.stringify({
      server: { host: "::", port: 8877 }
    }));
    assert.deepEqual(healthProbeUrls(env, { host: "::", port: "3000" }), {
      apiBaseUrl: "http://[::]:8877",
      webUrl: "http://[::]:3000"
    });
    assert.equal(
      resolveTuiRuntimeUrl(env),
      "http://[::1]:8877/dataagent/stream"
    );
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
});

test("later commands use the API address recorded at start", () => {
  const home = mkdtempSync(path.join(os.tmpdir(), "df-listen-"));
  writeFileSync(path.join(home, "config.json"), JSON.stringify({
    server: { host: "127.0.0.1", port: 8790 }
  }));
  const env = { DATAAGENT_HOME: home };
  const saved = { host: "0.0.0.0", port: "8877" };
  try {
    assert.deepEqual(healthProbeUrls(env, { host: "127.0.0.1", port: "3000" }, saved), {
      apiBaseUrl: "http://0.0.0.0:8877",
      webUrl: "http://127.0.0.1:3000"
    });
    assert.equal(
      resolveTuiRuntimeUrl(env, null, saved),
      "http://127.0.0.1:8877/dataagent/stream"
    );
    assert.equal(
      resolveTuiRuntimeUrl(env, null, { host: "10.0.0.8", port: "8802" }),
      "http://10.0.0.8:8802/dataagent/stream"
    );
    assert.deepEqual(
      apiListenFromState({
        listen: { webHost: "127.0.0.1", apiHost: "0.0.0.0", configPath: "team.json" },
        ports: { web: 3000, api: 8877 }
      }),
      { host: "0.0.0.0", port: "8877" }
    );
    assert.equal(
      apiListenFromState({ ports: { api: 8787 }, listen: { webHost: "127.0.0.1" } }),
      null
    );
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
});

test("a running deployment remembers the web listen host", () => {
  assert.deepEqual(
    webListenFromState({ ports: { web: 3000 }, listen: { webHost: "::1" } }),
    { host: "::1", port: "3000" }
  );
  assert.deepEqual(
    webListenFromState({ ports: { web: 3000 } }, { host: "127.0.0.1", port: "3000" }),
    { host: "127.0.0.1", port: "3000" }
  );
});

test("a relative --config stays the caller's file after the stack cwd changes", () => {
  const outside = realpathSync(mkdtempSync(path.join(os.tmpdir(), "df-caller-")));
  const repo = realpathSync(mkdtempSync(path.join(os.tmpdir(), "df-repo-")));
  const selected = path.join(outside, "review-team.json");
  writeFileSync(selected, JSON.stringify({ server: { host: "0.0.0.0", port: 8877 } }));
  const previous = process.cwd();
  try {
    process.chdir(outside);
    const parsed = parseDeployArgs(["start", "--config", "./review-team.json"]);
    assert.equal(parsed.config, selected);
    assert.deepEqual(readServerListen({}, parsed.config), { host: "0.0.0.0", port: "8877" });
    const startArgs = managedWebStartArgs({ host: "127.0.0.1", port: "3000" }, parsed.config);
    assert.deepEqual(startArgs.slice(-2), ["--config", selected]);

    process.chdir(repo);
    assert.deepEqual(readServerListen({}, parsed.config), { host: "0.0.0.0", port: "8877" });
    assert.throws(
      () => readServerListen({}, "review-team.json"),
      /Configuration file does not exist: review-team\.json/,
    );
    writeFileSync(
      path.join(repo, "review-team.json"),
      JSON.stringify({ server: { host: "127.0.0.1", port: 1 } }),
    );
    assert.equal(readServerListen({}, "review-team.json").port, "1");
    const child = resolveStackRuntimeConfig({}, startArgs.slice(startArgs.indexOf("--")));
    assert.equal(child.configPath, selected);
    assert.equal(child.apiPort, "8877");
    assert.deepEqual(
      pythonControlPlaneLaunch(repo, {}, false, child.configPath).args.slice(-2),
      ["--config", selected],
    );
  } finally {
    process.chdir(previous);
    rmSync(outside, { recursive: true, force: true });
    rmSync(repo, { recursive: true, force: true });
  }
});

test("deploy start uses the same web host and port flags as npm run start", () => {
  assert.deepEqual(managedWebStartArgs({ host: "127.0.0.1", port: "3310" }), [
    "run", "start", "--", "--web-host", "127.0.0.1", "--web-port", "3310"
  ]);
  assert.deepEqual(managedWebStartArgs({}), [
    "run", "start", "--", "--web-host", "127.0.0.1", "--web-port", "3000"
  ]);
  assert.deepEqual(managedWebStartArgs({ host: "127.0.0.1", port: "3000" }, "team.json"), [
    "run", "start", "--", "--web-host", "127.0.0.1", "--web-port", "3000", "--config", "team.json"
  ]);
});

test("first run selects Web and API ports with defaults", async () => {
  const lines = [];
  const root = await mkdtemp(path.join(os.tmpdir(), "df-cli-"));
  await mkdir(path.join(root, "apps/web"), { recursive: true });
  const result = await configureDeploymentInteractively({
    root,
    sourceText: "",
    reconfigure: false,
    nonInteractive: false,
    ask: createAsk(["1", "1", ""]),
    print: (line) => lines.push(String(line)),
    probe: async () => ({ available: true, owner: null })
  });
  assert.equal(result.web.port, "3000");
  assert.equal(result.listen.port, "8790");
  assert.equal(result.envText.trim(), "");
});

test("existing complete config skips prompts unless reconfigure", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "df-cli-"));
  await mkdir(path.join(root, "apps/web"), { recursive: true });
  const source = "LLM_MODEL=qwen-plus\n";
  let asked = 0;
  const result = await configureDeploymentInteractively({
    root,
    sourceText: source,
    reconfigure: false,
    nonInteractive: false,
    ask: async () => {
      asked += 1;
      throw new Error("must not prompt");
    },
    print: () => {},
    probe: async () => ({ available: true, owner: null })
  });
  assert.equal(asked, 0);
  assert.equal(result.web.port, "3000");
  assert.match(result.envText, /LLM_MODEL=qwen-plus/);
});

test("an existing env file skips prompts and keeps its lines", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "df-cli-kept-"));
  await mkdir(path.join(root, "apps/web"), { recursive: true });
  const source = "NOTE=left-alone\n";
  let asked = 0;
  const result = await configureDeploymentInteractively({
    root,
    sourceText: source,
    reconfigure: false,
    nonInteractive: false,
    ask: async () => {
      asked += 1;
      throw new Error("must not prompt");
    },
    print: () => {},
    probe: async () => ({ available: true, owner: null })
  });
  assert.equal(asked, 0);
  assert.equal(result.web.port, "3000");
  assert.match(result.envText, /NOTE=left-alone/);
});

test("partial .env is not treated as complete before fill and still prompts", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "df-cli-partial-"));
  await mkdir(path.join(root, "apps/web"), { recursive: true });
  let asked = 0;
  const result = await configureDeploymentInteractively({
    root,
    sourceText: "FOO=bar\nCUSTOM=keep-me\n",
    reconfigure: false,
    nonInteractive: false,
    ask: createAsk(["1", "1", ""]),
    print: () => {
      asked += 1;
    },
    probe: async () => ({ available: true, owner: null })
  });
  assert.match(result.envText, /CUSTOM=keep-me/);
  assert.equal(asked, 0);
});

test("non-interactive never calls ask and uses loopback defaults", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "df-cli-"));
  await mkdir(path.join(root, "apps/web"), { recursive: true });
  const result = await configureDeploymentInteractively({
    root,
    sourceText: "",
    reconfigure: false,
    nonInteractive: true,
    ask: async () => assert.fail("must not prompt"),
    print: () => {},
    probe: async () => ({ available: true, owner: null })
  });
  assert.equal(result.web.port, "3000");
  assert.equal(result.web.host, "127.0.0.1");
  assert.equal(result.envText.trim(), "");
});

test("non-interactive does not copy process env into .env", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "df-cli-"));
  await mkdir(path.join(root, "apps/web"), { recursive: true });
  const result = await configureDeploymentInteractively({
    root,
    sourceText: "",
    reconfigure: false,
    nonInteractive: true,
    processEnv: { UNRELATED: "1" },
    ask: async () => assert.fail("must not prompt"),
    print: () => {},
    probe: async () => ({ available: true, owner: null })
  });
  assert.equal(result.web.host, "127.0.0.1");
  assert.doesNotMatch(result.envText, /UNRELATED/);
});

test("port menu rejects n with explicit hint", async () => {
  const lines = [];
  const root = await mkdtemp(path.join(os.tmpdir(), "df-cli-"));
  await mkdir(path.join(root, "apps/web"), { recursive: true });
  const ask = createAsk(["n", "1", "1", ""]);
  await configureDeploymentInteractively({
    root,
    sourceText: "",
    reconfigure: false,
    nonInteractive: false,
    ask,
    print: (line) => lines.push(String(line)),
    probe: async () => ({ available: true, owner: null })
  });
  assert.match(lines.join("\n"), /请输入 1 或 2/);
});

test("port menu aborts after too many invalid choices", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "df-cli-"));
  await mkdir(path.join(root, "apps/web"), { recursive: true });
  await assert.rejects(
    () =>
      configureDeploymentInteractively({
        root,
        sourceText: "",
        reconfigure: false,
        nonInteractive: false,
        ask: createAsk(["n", "n", "n", "n", "n"]),
        print: () => {},
        probe: async () => ({ available: true, owner: null })
      }),
    /too many invalid choices/
  );
});

test("reconfigure creates backup and keeps secrets", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "df-cli-"));
  await mkdir(path.join(root, "apps/web"), { recursive: true });
  const source = "LLM_MODEL=qwen-plus\n";
  await writeFile(path.join(root, ".env"), `${source}\n`);
  const result = await configureDeploymentInteractively({
    root,
    sourceText: source,
    reconfigure: true,
    nonInteractive: false,
    ask: createAsk(["1", "1", ""]),
    print: () => {},
    probe: async () => ({ available: true, owner: null }),
    write: true,
    timestamp: "20260722-150000"
  });
  assert.match(result.envText, /LLM_MODEL=qwen-plus/);
  assert.equal(
    await readFile(path.join(root, ".env.backup-20260722-150000"), "utf8"),
    `${source}\n`
  );
});

test("collectManagedPorts ignores bare .env ports without a verified running state", async () => {
  const bare = await collectManagedPorts(
    {},
    null
  );
  assert.deepEqual([...bare], []);

  const deadPid = await collectManagedPorts(
    {},
    {
      pid: 99999999,
      launchId: "launch-1",
      ports: { web: 3310, api: 8877 }
    }
  );
  assert.deepEqual([...deadPid], []);
});

test("collectManagedPorts marks state ports only when pid is alive and launchId verifies", async () => {
  const unmanaged = await collectManagedPorts(
    {},
    {
      pid: process.pid,
      launchId: "expected-launch",
      ports: { web: 3310, api: 8877 }
    },
    {
      readLaunchId: async () => "other-launch",
      forceLaunchIdCheck: true
    }
  );
  assert.deepEqual([...unmanaged], []);

  const managed = await collectManagedPorts(
    {},
    {
      pid: process.pid,
      launchId: "launch-1",
      ports: { web: 3310, api: 8877 }
    },
    {
      readLaunchId: async () => "launch-1",
      forceLaunchIdCheck: true
    }
  );
  assert.deepEqual([...managed].sort((a, b) => a - b), [3310, 8877]);
});

test("non-interactive fails when ports are occupied without a running managed state", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "df-cli-occupied-"));
  await mkdir(path.join(root, "apps/web"), { recursive: true });
  const source = "LLM_MODEL=qwen-plus\n";

  await assert.rejects(
    () =>
      configureDeploymentInteractively({
        root,
        sourceText: source,
        reconfigure: false,
        nonInteractive: true,
        ask: async () => assert.fail("must not prompt"),
        print: () => {},
        probe: async () => ({ available: false, owner: "nginx pid=42" })
      }),
    /already in use/i
  );
});

test("reconfigure reuses managed listening ports only for a verified running stack", async () => {
  const probes = [];
  const root = await mkdtemp(path.join(os.tmpdir(), "df-cli-"));
  await mkdir(path.join(root, "apps/web"), { recursive: true });
  await mkdir(path.join(root, "storage/run"), { recursive: true });
  const source = "LLM_MODEL=qwen-plus\n";
  const home = path.join(root, "dataagent-home");
  await mkdir(home, { recursive: true });
  await writeFile(
    path.join(home, "config.json"),
    `${JSON.stringify({ server: { host: "127.0.0.1", port: 8877 } })}\n`
  );
  await writeFile(
    path.join(root, "storage/run/deployment.json"),
    `${JSON.stringify({
      pid: process.pid,
      launchId: "launch-managed",
      status: "healthy",
      startedAt: "2026-07-22T00:00:00.000Z",
      ports: { web: 3310, api: 8877 }
    })}\n`
  );
  const result = await configureDeploymentInteractively({
    root,
    sourceText: source,
    reconfigure: true,
    nonInteractive: true,
    ask: async () => assert.fail("must not prompt"),
    print: () => {},
    probe: async (port) => {
      probes.push(port);
      return { available: false, owner: `datagallery pid=${process.pid}` };
    },
    collectManagedPortsOptions: {
      readLaunchId: async () => "launch-managed",
      forceLaunchIdCheck: true
    },
    processEnv: { DATAAGENT_HOME: home },
    web: { host: "127.0.0.1", port: "3310" }
  });
  assert.equal(result.web.port, "3310");
  assert.equal(result.listen.port, "8877");
  assert.ok(probes.includes(3310));
  assert.ok(probes.includes(8877));
});

test("deploy --config takes the API listen from that file", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "df-cli-"));
  const home = path.join(root, "home");
  const selected = path.join(root, "team.json");
  await mkdir(home, { recursive: true });
  await writeFile(
    path.join(home, "config.json"),
    `${JSON.stringify({ server: { host: "10.0.0.1", port: 8801 } })}\n`
  );
  await writeFile(
    selected,
    `${JSON.stringify({ server: { host: "0.0.0.0", port: 8877 } })}\n`
  );
  const result = await configureDeploymentInteractively({
    root,
    sourceText: "LLM_MODEL=qwen-plus\n",
    reconfigure: true,
    nonInteractive: true,
    ask: async () => assert.fail("must not prompt"),
    print: () => {},
    probe: async () => ({ available: true, owner: null }),
    processEnv: { DATAAGENT_HOME: home },
    configPath: selected,
    web: { host: "127.0.0.1", port: "3000" }
  });
  assert.equal(result.listen.host, "0.0.0.0");
  assert.equal(result.listen.port, "8877");
});

test("reconfigure leaves an existing public URL line unchanged", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "df-cli-"));
  await mkdir(path.join(root, "apps/web"), { recursive: true });
  const source = "LLM_MODEL=qwen-plus\n";
  const result = await configureDeploymentInteractively({
    root,
    sourceText: source,
    reconfigure: true,
    nonInteractive: true,
    ask: async () => assert.fail("must not prompt"),
    print: () => {},
    probe: async () => ({ available: true, owner: null })
  });
  assert.equal(result.env.LLM_MODEL, "qwen-plus");
  assert.match(result.envText, /LLM_MODEL=qwen-plus/);
});

test("createDeployLogWriter redacts secrets and updates deploy-latest.log", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "df-cli-log-"));
  const writer = await createDeployLogWriter(root, { timestamp: "20260722143000" });
  await writer.append("LLM_API_KEY=fixture-deploy-secret-at-least-32-chars\nnpm ci output\n");
  await writer.finalize();
  const logText = await readFile(path.join(root, "storage/logs/deploy-20260722143000.log"), "utf8");
  const latestText = await readFile(path.join(root, "storage/logs/deploy-latest.log"), "utf8");
  assert.doesNotMatch(logText, /fixture-deploy-secret-at-least-32-chars/);
  assert.match(logText, /npm ci output/);
  assert.equal(latestText, logText);
  assert.equal(writer.logPath, "storage/logs/deploy-20260722143000.log");
});

test("createDeployLogWriter redacts secrets split across append chunks", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "df-cli-log-chunk-"));
  const writer = await createDeployLogWriter(root, { timestamp: "20260722143001" });
  await writer.append("LLM_API_KEY=fixture-deploy-secret-");
  await writer.append("at-least-32-chars\nnpm ci output\n");
  await writer.finalize();
  const logText = await readFile(path.join(root, "storage/logs/deploy-20260722143001.log"), "utf8");
  assert.doesNotMatch(logText, /fixture-deploy-secret-at-least-32-chars/);
  assert.match(logText, /LLM_API_KEY=\*{4,8}/);
  assert.match(logText, /npm ci output/);
});

test("runDeploymentDoctor reports os, deps, config, ports, permissions, disk, pid, and health", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "df-cli-doctor-"));
  await mkdir(path.join(root, "storage"), { recursive: true });
  await writeFile(
    path.join(root, ".env"),
    "LLM_MODEL=qwen-plus\n"
  );
  const beforeEntries = await readdir(path.join(root, "storage"));
  const lines = [];
  const result = await runDeploymentDoctor(root, {
    print: (line) => lines.push(line),
    run: async (command, args = []) => {
      const key = [command, ...args].join(" ");
      if (key === "node --version") return { stdout: "v22.14.0\n" };
      if (key === "npm --version") return { stdout: "10.9.0\n" };
      throw new Error(`missing mock for ${key}`);
    },
    probe: async () => ({ available: true, owner: null })
  });
  const joined = lines.join("\n");
  assert.match(joined, /^os: /m);
  assert.match(joined, /^arch: /m);
  assert.match(joined, /dependency node: ok/);
  assert.match(joined, /config: ok/);
  assert.match(joined, /config web=http:\/\/127\.0\.0\.1:3000/);
  assert.match(joined, /port web 3000: available/);
  assert.match(joined, /permissions storage: writable/);
  assert.match(joined, /disk free:/);
  assert.match(joined, /pid: none/);
  assert.match(joined, /health: skipped/);
  assert.ok(result.lines.length >= 8);
  const afterEntries = await readdir(path.join(root, "storage"));
  assert.deepEqual(afterEntries, beforeEntries);
  assert.ok(afterEntries.every((entry) => !entry.startsWith(".doctor-write-")));
});

test("inspectWritablePath checks access without creating files", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "df-writable-"));
  const missingChild = path.join(root, "storage", "nested");
  const result = await inspectWritablePath(missingChild);
  assert.equal(result.writable, true);
  assert.equal(result.exists, false);
  assert.equal(existsSync(missingChild), false);
  assert.equal(existsSync(path.join(root, "storage")), false);
});

test("runDeploymentDoctor reports actionable fix for stale launchId state", async (t) => {
  if (process.platform !== "linux") {
    t.skip("proc launchId verification is Linux-only");
    return;
  }

  const root = await mkdtemp(path.join(os.tmpdir(), "df-cli-doctor-stale-"));
  await mkdir(path.join(root, "storage/run"), { recursive: true });
  await writeFile(
    path.join(root, ".env"),
    "LLM_MODEL=qwen-plus\n"
  );
  await writeFile(
    path.join(root, "storage/run/deployment.json"),
    `${JSON.stringify({
      pid: process.pid,
      launchId: "not-this-process",
      status: "healthy",
      startedAt: "2026-07-26T00:00:00.000Z",
      ports: { web: 3000, api: 8787 }
    })}\n`
  );

  const lines = [];
  await runDeploymentDoctor(root, {
    print: (line) => lines.push(line),
    run: async (command, args = []) => {
      const key = [command, ...args].join(" ");
      if (key === "node --version") return { stdout: "v22.14.0\n" };
      if (key === "npm --version") return { stdout: "10.9.0\n" };
      throw new Error(`missing mock for ${key}`);
    },
    probe: async () => ({ available: true, owner: null })
  });
  const joined = lines.join("\n");
  assert.match(joined, /stale/i);
  assert.match(joined, /\.\/deploy\.sh stop/);
  assert.match(joined, /permissions \.env: present mode=/);
});
test("runDeploymentDoctor reports the API address recorded at start", async () => {
  const home = await mkdtemp(path.join(os.tmpdir(), "df-cli-doctor-home-"));
  await writeFile(path.join(home, "config.json"), JSON.stringify({
    server: { host: "127.0.0.1", port: 8790 }
  }));
  const root = await mkdtemp(path.join(os.tmpdir(), "df-cli-doctor-saved-"));
  await mkdir(path.join(root, "storage/run"), { recursive: true });
  await writeFile(
    path.join(root, ".env"),
    `LLM_MODEL=qwen-plus\nDATAAGENT_HOME=${home}\n`
  );
  await writeFile(
    path.join(root, "storage/run/deployment.json"),
    `${JSON.stringify({
      pid: 99999999,
      launchId: "not-this-process",
      status: "healthy",
      startedAt: "2026-07-26T00:00:00.000Z",
      ports: { web: 3000, api: 8877 },
      listen: { webHost: "127.0.0.1", apiHost: "0.0.0.0", configPath: "team.json" }
    })}\n`
  );
  rmSync(path.join(home, "config.json"));

  const lines = [];
  await runDeploymentDoctor(root, {
    print: (line) => lines.push(line),
    run: async (command, args = []) => {
      const key = [command, ...args].join(" ");
      if (key === "node --version") return { stdout: "v22.14.0\n" };
      if (key === "npm --version") return { stdout: "10.9.0\n" };
      throw new Error(`missing mock for ${key}`);
    },
    probe: async () => ({ available: true, owner: null })
  });
  const joined = lines.join("\n");
  assert.match(joined, /config web=http:\/\/127\.0\.0\.1:3000 api=http:\/\/0\.0\.0\.0:8877/);
  assert.match(joined, /port api 8877: available/);
});

test("resolveTuiRuntimeUrl defaults to deployed API port", () => {
  const home = mkdtempSync(path.join(os.tmpdir(), "df-listen-"));
  writeFileSync(path.join(home, "config.json"), JSON.stringify({
    server: { host: "0.0.0.0", port: 8877 }
  }));
  try {
    assert.equal(
      resolveTuiRuntimeUrl({ DATAAGENT_HOME: home }),
      "http://127.0.0.1:8877/dataagent/stream"
    );
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
  assert.equal(
    resolveTuiRuntimeUrl({}, "http://example/api/copilotkit"),
    "http://example/api/copilotkit"
  );
});

const COMPLETE_ENV = {
  LLM_MODEL: "qwen-plus"
};

test("start names missing .env variables and does not write .env.local", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "df-start-"));
  await mkdir(path.join(root, "apps/web/.next"), { recursive: true });
  await writeFile(path.join(root, "apps/web/.next/BUILD_ID"), "test");
  await prepareStartEnvironment(root, {});
  assert.equal(
    formatStartBlocked(["EXAMPLE"], ["apps/web/.next/BUILD_ID"]),
    "缺少合法完整的 .env 配置，缺：EXAMPLE；缺少文件：apps/web/.next/BUILD_ID。请先运行 ./deploy.sh deploy"
  );
});

test("start with a complete .env does not write a web env file", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "df-start-"));
  await mkdir(path.join(root, "apps/web/.next"), { recursive: true });
  await writeFile(path.join(root, "apps/web/.next/BUILD_ID"), "test");
  await prepareStartEnvironment(root, COMPLETE_ENV);
  assert.equal(existsSync(path.join(root, "apps/web/.env.local")), false);
  assert.equal(existsSync(path.join(root, ".env")), false);
});
