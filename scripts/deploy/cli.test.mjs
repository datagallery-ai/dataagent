import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { mkdtemp, readdir, readFile, writeFile, mkdir, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { readServerListen } from "../../ui/shared/server-listen.mjs";
import { pythonControlPlaneLaunch, resolveStackRuntimeConfig } from "../stack-runtime-config.mjs";
import { parseDeployArgs } from "./args.mjs";
import {
  configureDeploymentInteractively,
  collectManagedPorts,
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

// Never read or write the developer's real Home from these tests.
process.env.DATAAGENT_HOME = mkdtempSync(path.join(os.tmpdir(), "dataagent-deploy-test-home-"));

function createAsk(answers) {
  const queue = [...answers];
  return async () => {
    if (queue.length === 0) throw new Error("unexpected prompt");
    return queue.shift();
  };
}

test("health probes use the configured web and api hosts", () => {
  const home = mkdtempSync(path.join(os.tmpdir(), "df-listen-"));
  writeFileSync(path.join(home, "config.yaml"), JSON.stringify({
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
      "http://127.0.0.1:8877/api/v1/runs"
    );
    writeFileSync(path.join(home, "config.yaml"), JSON.stringify({
      server: { host: "::", port: 8877 }
    }));
    assert.deepEqual(healthProbeUrls(env, { host: "::", port: "3000" }), {
      apiBaseUrl: "http://[::]:8877",
      webUrl: "http://[::]:3000"
    });
    assert.equal(
      resolveTuiRuntimeUrl(env),
      "http://[::1]:8877/api/v1/runs"
    );
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
});

test("later commands use the API address recorded at start", () => {
  const home = mkdtempSync(path.join(os.tmpdir(), "df-listen-"));
  writeFileSync(path.join(home, "config.yaml"), JSON.stringify({
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
      "http://127.0.0.1:8877/api/v1/runs"
    );
    assert.equal(
      resolveTuiRuntimeUrl(env, null, { host: "10.0.0.8", port: "8802" }),
      "http://10.0.0.8:8802/api/v1/runs"
    );
    assert.deepEqual(
      apiListenFromState({
        listen: { webHost: "127.0.0.1", apiHost: "0.0.0.0", configPath: "team.yaml" },
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
  const selected = path.join(outside, "review-team.yaml");
  writeFileSync(selected, JSON.stringify({ server: { host: "0.0.0.0", port: 8877 } }));
  const previous = process.cwd();
  try {
    process.chdir(outside);
    const parsed = parseDeployArgs(["start", "--config", "./review-team.yaml"]);
    assert.equal(parsed.config, selected);
    assert.deepEqual(readServerListen({}, parsed.config), { host: "0.0.0.0", port: "8877" });
    const startArgs = managedWebStartArgs({ host: "127.0.0.1", port: "3000" }, parsed.config);
    assert.deepEqual(startArgs.slice(-2), ["--config", selected]);

    process.chdir(repo);
    assert.deepEqual(readServerListen({}, parsed.config), { host: "0.0.0.0", port: "8877" });
    assert.throws(
      () => readServerListen({}, "review-team.yaml"),
      /Configuration file does not exist: review-team\.yaml/,
    );
    writeFileSync(
      path.join(repo, "review-team.yaml"),
      JSON.stringify({ server: { host: "127.0.0.1", port: 1 } }),
    );
    assert.equal(readServerListen({}, "review-team.yaml").port, "1");
    const child = resolveStackRuntimeConfig({}, startArgs);
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
    "--api", "--web", "--web-host", "127.0.0.1", "--web-port", "3310"
  ]);
  assert.deepEqual(managedWebStartArgs({}), [
    "--api", "--web", "--web-host", "127.0.0.1", "--web-port", "3000"
  ]);
  assert.deepEqual(managedWebStartArgs({ host: "127.0.0.1", port: "3000" }, "team.yaml"), [
    "--api", "--web", "--web-host", "127.0.0.1", "--web-port", "3000", "--config", "team.yaml"
  ]);
});

test("first run selects Web and API ports with defaults", async () => {
  const lines = [];
  const root = await mkdtemp(path.join(os.tmpdir(), "df-cli-"));
  await mkdir(path.join(root, "ui/web"), { recursive: true });
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
  await mkdir(path.join(root, "ui/web"), { recursive: true });
  const source = `LLM_MODEL=qwen-plus\nDATAAGENT_HOME=${path.join(root, "home")}\n`;
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
  await mkdir(path.join(root, "ui/web"), { recursive: true });
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
  await mkdir(path.join(root, "ui/web"), { recursive: true });
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
  await mkdir(path.join(root, "ui/web"), { recursive: true });
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
  await mkdir(path.join(root, "ui/web"), { recursive: true });
  const result = await configureDeploymentInteractively({
    root,
    sourceText: "",
    reconfigure: false,
    nonInteractive: true,
    processEnv: { UNRELATED: "1", DATAAGENT_HOME: path.join(root, "home") },
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
  await mkdir(path.join(root, "ui/web"), { recursive: true });
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
  await mkdir(path.join(root, "ui/web"), { recursive: true });
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
  await mkdir(path.join(root, "ui/web"), { recursive: true });
  const source = `LLM_MODEL=qwen-plus\nDATAAGENT_HOME=${path.join(root, "home")}\n`;
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
  await mkdir(path.join(root, "ui/web"), { recursive: true });
  const source = `LLM_MODEL=qwen-plus\nDATAAGENT_HOME=${path.join(root, "home")}\n`;

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
  await mkdir(path.join(root, "ui/web"), { recursive: true });
  await mkdir(path.join(root, "storage/run"), { recursive: true });
  const source = `LLM_MODEL=qwen-plus\nDATAAGENT_HOME=${path.join(root, "home")}\n`;
  const home = path.join(root, "dataagent-home");
  await mkdir(home, { recursive: true });
  await writeFile(
    path.join(home, "config.yaml"),
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
  const selected = path.join(root, "team.yaml");
  await mkdir(home, { recursive: true });
  await writeFile(
    path.join(home, "config.yaml"),
    `${JSON.stringify({ server: { host: "10.0.0.1", port: 8801 } })}\n`
  );
  await writeFile(
    selected,
    `${JSON.stringify({ server: { host: "0.0.0.0", port: 8877 } })}\n`
  );
  const result = await configureDeploymentInteractively({
    root,
    sourceText: `LLM_MODEL=qwen-plus\nDATAAGENT_HOME=${path.join(root, "home")}\n`,
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
  await mkdir(path.join(root, "ui/web"), { recursive: true });
  const source = `LLM_MODEL=qwen-plus\nDATAAGENT_HOME=${path.join(root, "home")}\n`;
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

test("runDeploymentDoctor reports os, deps, config, ports, permissions, disk, pid, and health", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "df-cli-doctor-"));
  await mkdir(path.join(root, "storage"), { recursive: true });
  await writeFile(
    path.join(root, ".env"),
    `LLM_MODEL=qwen-plus\nDATAAGENT_HOME=${path.join(root, "home")}\n`
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

test("doctor probes configured sources without downloads or credential disclosure", async t => {
  const root = await mkdtemp(path.join(os.tmpdir(), "df-doctor-sources-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const home = path.join(root, "home");
  await mkdir(home);
  await writeFile(path.join(home, "config.yaml"), JSON.stringify({ server: { host: "127.0.0.1", port: 38473 } }));
  const requests = [];
  const commands = [];
  const result = await runDeploymentDoctor(root, {
    env: { DATAAGENT_HOME: home, UV_PYTHON: "/opt/python 3.12/bin/python", UV_DEFAULT_INDEX: "https://mirror.example/simple/" },
    run: async (command, args) => {
      commands.push([command, ...args]);
      if (args.join(" ") === "config get registry") return { stdout: "https://svc:fixture-password@registry.example/npm/?token=fixture-query\n" };
      if (command === "uv" && args[0] === "python") return { stdout: "3.12.10\n" };
      return { stdout: command === "node" ? "v22.14.0" : command === "uv" ? "uv 0.8.0" : "10.9.0" };
    },
    fetch: async (url, options) => {
      assert.equal(options.method, "HEAD");
      assert.equal(url.username + url.password, "");
      if (url.host === "registry.example") {
        assert.equal(options.headers.Authorization, `Basic ${Buffer.from("svc:fixture-password").toString("base64")}`);
      }
      requests.push(url.href);
      return new Response(null, { status: 200 });
    },
    probe: async () => ({ available: true }),
  });
  assert.equal(requests.length, 2);
  assert.ok(commands.some(args => args.includes("/opt/python 3.12/bin/python") && args.includes("--no-python-downloads")));
  assert.match(result.lines.join("\n"), /source npm: https:\/\/registry\.example\/npm\/ HTTP 200/);
  assert.match(result.lines.join("\n"), /python local: 3.12.10/);
  assert.match(result.lines.join("\n"), /api=http:\/\/127\.0\.0\.1:38473/);
  assert.doesNotMatch(result.lines.join("\n"), /fixture-password|fixture-query/);
});

for (const override of [false, true]) {
  test(`doctor ${override ? "prefers process variables" : "uses project env defaults"} for Python and download sources`, async t => {
    const root = await mkdtemp(path.join(os.tmpdir(), "df-doctor-env-"));
    t.after(() => rm(root, { recursive: true, force: true }));
    await writeFile(path.join(root, ".env"), [
      'UV_DEFAULT_INDEX="https://file.example/simple/" # mirror',
      'UV_PYTHON="/file/python" # interpreter',
      "npm_config_registry=https://file.example/npm/",
      "DATAAGENT_NODE_PACKAGE_URL=https://file.example/node.rpm",
    ].join("\n"));
    const env = override ? {
      UV_DEFAULT_INDEX: "https://process.example/simple/",
      UV_PYTHON: "/process/python",
      npm_config_registry: "https://process.example/npm/",
      DATAAGENT_NODE_PACKAGE_URL: "https://process.example/node.rpm",
    } : {};
    env.DATAAGENT_HOME = path.join(root, "home");
    const expected = override ? "process" : "file";
    const requests = [];
    const result = await runDeploymentDoctor(root, {
      env,
      run: async (command, args, options) => {
        if (args.join(" ") === "config get registry") {
          assert.equal(options.env.UV_DEFAULT_INDEX, `https://${expected}.example/simple/`);
          return { stdout: options.env.npm_config_registry };
        }
        if (command === "uv" && args[0] === "python") {
          assert.equal(args[2], `/${expected}/python`);
          assert.equal(options.env.UV_PYTHON, `/${expected}/python`);
          return { stdout: "3.12.10" };
        }
        return { stdout: command === "node" ? "v22.12.0" : command === "uv" ? "uv 0.11.11" : "10.9.0" };
      },
      fetch: async url => { requests.push(url.href); return new Response(null, { status: 200 }); },
      probe: async () => ({ available: true }),
    });
    assert(requests.includes(`https://${expected}.example/simple/`));
    assert(requests.includes(`https://${expected}.example/npm/`));
    assert(!requests.includes("https://file.example/node.rpm"));
    assert.equal(requests.includes("https://process.example/node.rpm"), override);
    assert.match(result.lines.join("\n"), /python local: 3.12.10/);
  });
}

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
    `LLM_MODEL=qwen-plus\nDATAAGENT_HOME=${path.join(root, "home")}\n`
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
  await writeFile(path.join(home, "config.yaml"), JSON.stringify({
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
      listen: { webHost: "127.0.0.1", apiHost: "0.0.0.0", configPath: "team.yaml" }
    })}\n`
  );
  rmSync(path.join(home, "config.yaml"));

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
  writeFileSync(path.join(home, "config.yaml"), JSON.stringify({
    server: { host: "0.0.0.0", port: 8877 }
  }));
  try {
    assert.equal(
      resolveTuiRuntimeUrl({ DATAAGENT_HOME: home }),
      "http://127.0.0.1:8877/api/v1/runs"
    );
  } finally {
    rmSync(home, { recursive: true, force: true });
  }
  assert.equal(
    resolveTuiRuntimeUrl({}, "http://example/api/v1/runs"),
    "http://example/api/v1/runs"
  );
});

const COMPLETE_ENV = {
  LLM_MODEL: "qwen-plus"
};

async function preparePython(root) {
  const directory = process.platform === "win32" ? "Scripts" : "bin";
  const python = process.platform === "win32" ? "python.exe" : "python";
  const location = path.join(root, ".venv", directory);
  await mkdir(location, { recursive: true });
  await writeFile(path.join(location, python), "fixture");
}

test("start names missing .env variables and does not write .env.local", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "df-start-"));
  await mkdir(path.join(root, "ui/web/.next"), { recursive: true });
  await writeFile(path.join(root, "ui/web/.next/BUILD_ID"), "test");
  await preparePython(root);
  await prepareStartEnvironment(root, {});
  assert.equal(
    formatStartBlocked(["EXAMPLE"], ["ui/web/.next/BUILD_ID"]),
    "缺少合法完整的 .env 配置，缺：EXAMPLE；缺少文件：ui/web/.next/BUILD_ID。请先运行 ./deploy.sh deploy"
  );
});

test("start with a complete .env does not write a web env file", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "df-start-"));
  await mkdir(path.join(root, "ui/web/.next"), { recursive: true });
  await writeFile(path.join(root, "ui/web/.next/BUILD_ID"), "test");
  await preparePython(root);
  await prepareStartEnvironment(root, COMPLETE_ENV);
  assert.equal(existsSync(path.join(root, "ui/web/.env.local")), false);
  assert.equal(existsSync(path.join(root, ".env")), false);
});

test("start refuses a missing Python environment instead of preparing it", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "df-start-python-"));
  try {
    await mkdir(path.join(root, "ui/web/.next"), { recursive: true });
    await writeFile(path.join(root, "ui/web/.next/BUILD_ID"), "fixture");
    await assert.rejects(() => prepareStartEnvironment(root, {}), /\.venv.*python.*deploy/);
    assert.equal(existsSync(path.join(root, ".venv")), false);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("start preflight gives process environment precedence over the project env file", async t => {
  const root = await mkdtemp(path.join(os.tmpdir(), "df-start-env-priority-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const previous = process.env.UV_PROJECT_ENVIRONMENT;
  t.after(() => {
    if (previous === undefined) delete process.env.UV_PROJECT_ENVIRONMENT;
    else process.env.UV_PROJECT_ENVIRONMENT = previous;
  });
  await mkdir(path.join(root, "ui/web/.next"), { recursive: true });
  await writeFile(path.join(root, "ui/web/.next/BUILD_ID"), "fixture");
  await preparePython(root);
  process.env.UV_PROJECT_ENVIRONMENT = path.join(root, ".venv");
  await prepareStartEnvironment(root, { UV_PROJECT_ENVIRONMENT: path.join(root, "missing-disk-env") });
});

test("start reports missing dependencies without entering the installer", async (t) => {
  if (process.platform === "win32") return t.skip("fixture commands use bash");
  const root = await mkdtemp(path.join(os.tmpdir(), "df-start-dependencies-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  await preparePython(root);
  await mkdir(path.join(root, "ui/web/.next"), { recursive: true });
  await writeFile(path.join(root, "ui/web/.next/BUILD_ID"), "fixture");
  await mkdir(path.join(root, "bin"));
  await writeFile(path.join(root, "bin/npm"), "#!/bin/bash\nexit 1\n", { mode: 0o755 });
  await writeFile(path.join(root, "bin/uv"), "#!/bin/bash\necho 'uv 0.8.0'\n", { mode: 0o755 });
  const result = spawnSync(process.execPath, ["--input-type=module", "-e", `
    import { main } from ${JSON.stringify(new URL("./cli.mjs", import.meta.url).href)};
    process.exitCode = await main(["start"], {
      root: process.env.TEST_DEPLOY_ROOT,
      ask: async () => { throw new Error("unexpected install prompt"); },
    });
  `], {
    env: { ...process.env, TEST_DEPLOY_ROOT: root, DATAAGENT_HOME: path.join(root, "home"),
      PATH: `${path.join(root, "bin")}${path.delimiter}${process.env.PATH}` },
    encoding: "utf8", timeout: 5000,
  });
  assert.equal(result.status, 1, result.stderr);
  assert.match(result.stdout, /Missing dependencies: npm.*deploy/);
  assert.doesNotMatch(result.stdout, /unexpected install prompt/);
});

test("status uses the terminal Home before the project env Home", async t => {
  const root = await mkdtemp(path.join(os.tmpdir(), "df-status-env-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  for (const [home, port] of [["file-home", 8877], ["terminal-home", 9988]]) {
    await mkdir(path.join(root, home));
    await writeFile(path.join(root, home, "config.yaml"), JSON.stringify({ server: { host: "127.0.0.1", port } }));
  }
  await writeFile(path.join(root, ".env"), `DATAAGENT_HOME=${JSON.stringify(path.join(root, "file-home"))}\n`);
  const result = spawnSync(process.execPath, ["--input-type=module", "-e", `
    import assert from "node:assert/strict";
    import { main } from ${JSON.stringify(new URL("./cli.mjs", import.meta.url).href)};
    const requests = [];
    globalThis.fetch = async url => { requests.push(url); return new Response(null, { status: 200 }); };
    assert.equal(await main(["status"], { root: process.env.TEST_DEPLOY_ROOT, print() {} }), 0);
    assert.equal(requests[0], "http://127.0.0.1:9988/healthz");
  `], {
    env: { ...process.env, TEST_DEPLOY_ROOT: root, DATAAGENT_HOME: path.join(root, "terminal-home") },
    encoding: "utf8", timeout: 5000,
  });
  assert.equal(result.status, 0, result.stderr);
});
