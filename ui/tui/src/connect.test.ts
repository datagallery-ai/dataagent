import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { mkdtemp, rm, symlink, writeFile } from "node:fs/promises";
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";
import { it } from "node:test";
import { fileURLToPath } from "node:url";
import { configuredListen } from "./backend.js";
import { runTui } from "./index.js";
import { store } from "./state/store.js";
import { normalizeRuntimeUrl, resolveTuiLogPath, connectTui, sessionTransport } from "./connect.js";
import { restoreMessages, SessionClient } from "./protocol/session-client.js";
import { createInitialLiveRun, reduceLiveRunEvent } from "./state/live-run-state.js";

/** Success envelope used by every /api/v1 JSON route. */
const ok = (data: unknown) => Response.json({ success: true, data });

it("starts the CLI through the symlink used by npm bin", async (t) => {
  if (process.platform === "win32") return t.skip("npm uses command shims on Windows");
  const directory = await mkdtemp(join(tmpdir(), "dataagent-tui-bin-"));
  t.after(() => rm(directory, { recursive: true, force: true }));
  const entry = join(directory, "dataagent-cli");
  await symlink(fileURLToPath(new URL("./index.js", import.meta.url)), entry);
  const result = spawnSync(process.execPath, [entry, "--help"], { encoding: "utf8", timeout: 10_000 });
  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /--runtime-url/);
});

it("allows importing the CLI from a process with a non-file argument", () => {
  const url = new URL("./index.js", import.meta.url).href;
  const result = spawnSync(process.execPath, [
    "--input-type=module", "-e", `await import(${JSON.stringify(url)})`, "--", "--help",
  ], { encoding: "utf8", timeout: 10_000 });
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stdout, "");
});

function startedBackend(runtimeUrl = "http://127.0.0.1:8790/api/v1/runs") {
  const controller = new AbortController();
  return {
    runtimeUrl,
    instanceId: "00000000-0000-4000-8000-000000000001" as `${string}-${string}-${string}-${string}-${string}`,
    signal: controller.signal,
    stop: async () => {},
  };
}

it("no arguments start the backend", async () => {
  const directory = await mkdtemp(join(tmpdir(), "dataagent-default-"));
  let started = 0;
  let probed = "";
  const code = await runTui({
    argv: [],
    env: { DATAAGENT_HOME: directory },
    startBackend: async () => {
      started += 1;
      return startedBackend();
    },
    fetchImpl: async (url) => {
      probed = String(url);
      if (probed.endsWith("/healthz") && started === 0) throw new Error("connect ECONNREFUSED");
      return Response.json(readyHealth);
    },
    renderApp: async (options) => {
      assert.equal(options.tui?.model, "GLM-5.2");
      return "exit";
    },
  });
  assert.equal(code, 0);
  assert.equal(started, 1);
  assert.equal(probed, "http://127.0.0.1:8790/healthz");
});

it("an already running control plane is attached instead of started", async () => {
  const directory = await mkdtemp(join(tmpdir(), "dataagent-busy-"));
  const probed: string[] = [];
  const code = await runTui({
    argv: [],
    env: { DATAAGENT_HOME: directory },
    startBackend: async () => { throw new Error("must not start"); },
    fetchImpl: async (url) => {
      probed.push(String(url));
      return Response.json(readyHealth);
    },
    renderApp: async (options) => {
      assert.equal(options.tui?.model, "GLM-5.2");
      return "exit";
    },
  });
  assert.equal(code, 0);
  assert.deepEqual(probed, [
    "http://127.0.0.1:8790/healthz",
    "http://127.0.0.1:8790/healthz",
  ]);
});

it("config and workspace are forwarded to the backend", async () => {
  let captured: { configPath?: string | undefined; workspaces?: readonly string[] | undefined } | undefined;
  const code = await runTui({
    argv: ["--config", "cfg.json", "--workspace", "data"],
    startBackend: async (options) => {
      captured = options;
      return startedBackend();
    },
    fetchImpl: async () => Response.json(readyHealth),
    renderApp: async () => "exit",
  });
  assert.equal(code, 0);
  assert.equal(captured?.configPath, "cfg.json");
  assert.deepEqual(captured?.workspaces, ["data"]);
});

const readyHealth = {
  success: true,
  data: {
    status: "ok",
    conversation: {
      ready: true,
      protocol: "dataagent",
      instanceId: "00000000-0000-4000-8000-000000000001",
      model: "GLM-5.2",
      timeoutSeconds: 180,
    },
  },
};

for (const home of ["C:\\agent-home", "/srv/agent-home"]) {
  it(`accepts health paths from a backend host using ${home}`, async () => {
    const health = structuredClone(readyHealth);
    const conversation = { ...health.data.conversation, home,
      stateDir: home + "/runtime", workspaces: [{ name: "inputs", path: home + "/inputs" }] };
    assert.equal(await runTui({
      argv: ["--runtime-url", "http://127.0.0.1:8790"],
      startBackend: async () => assert.fail("Explicit attach must not start another runtime"),
      fetchImpl: async () => Response.json({ ...health, data: { ...health.data, conversation } }),
      renderApp: async ({ tui }) => {
        assert.equal(tui?.home, home);
        assert.match(tui?.mounts ?? "", /inputs:/);
        return "exit";
      },
    }), 0);
  });
}

it("attaches to server.host and server.port from config.yaml when already up", async () => {
  const directory = await mkdtemp(join(tmpdir(), "dataagent-host-"));
  await writeFile(join(directory, "config.yaml"), JSON.stringify({
    server: { host: "10.0.0.8", port: 8802 },
  }));
  const probed: string[] = [];
  const code = await runTui({
    argv: [],
    env: { DATAAGENT_HOME: directory },
    startBackend: async () => { throw new Error("must not start"); },
    fetchImpl: async (url) => {
      probed.push(String(url));
      return Response.json(readyHealth);
    },
    renderApp: async () => "exit",
  });
  assert.equal(code, 0);
  assert.deepEqual(probed, [
    "http://10.0.0.8:8802/healthz",
    "http://10.0.0.8:8802/healthz",
  ]);
});

it("attaches to the configured server.host", async () => {
  const directory = await mkdtemp(join(tmpdir(), "dataagent-attach-host-"));
  await writeFile(join(directory, "config.yaml"), JSON.stringify({
    server: { host: "10.0.0.8", port: 8802 },
  }));
  let requested = "";
  const code = await runTui({
    argv: ["--runtime-url", "http://10.0.0.8:8802"],
    env: { DATAAGENT_HOME: directory },
    startBackend: async () => { throw new Error("must not start"); },
    fetchImpl: async (url) => {
      requested = String(url);
      return Response.json(readyHealth);
    },
    renderApp: async () => "exit",
  });
  assert.equal(code, 0);
  assert.equal(requested, "http://10.0.0.8:8802/healthz");
});

it("attaches to the port from config.yaml", async () => {
  const directory = await mkdtemp(join(tmpdir(), "dataagent-port-"));
  await writeFile(join(directory, "config.yaml"), JSON.stringify({ server: { port: 8796 } }));
  const probed: string[] = [];
  const code = await runTui({
    argv: [],
    env: { DATAAGENT_HOME: directory },
    startBackend: async () => { throw new Error("must not start"); },
    fetchImpl: async (url) => {
      probed.push(String(url));
      return Response.json(readyHealth);
    },
    renderApp: async () => "exit",
  });
  assert.equal(code, 0);
  assert.deepEqual(probed, [
    "http://127.0.0.1:8796/healthz",
    "http://127.0.0.1:8796/healthz",
  ]);
});

function eventStream(events: object[]): Response {
  const body = events.map((event) => `data: ${JSON.stringify(event)}\r\n\r\n`).join("");
  return new Response(body, { headers: { "content-type": "text/event-stream" } });
}

it("a backend started by the TUI is reached through loopback when ready announces 0.0.0.0", async () => {
  const directory = await mkdtemp(join(tmpdir(), "dataagent-ready-wildcard-"));
  await writeFile(join(directory, "config.yaml"), JSON.stringify({
    server: { host: "0.0.0.0", port: 8790 },
  }));
  const requested: string[] = [];
  let started = 0;
  const code = await runTui({
    argv: [],
    env: { DATAAGENT_HOME: directory },
    startBackend: async () => {
      started += 1;
      return startedBackend("http://0.0.0.0:8790/api/v1/runs");
    },
    fetchImpl: async (url) => {
      const href = String(url);
      requested.push(href);
      assert.equal(href.includes("0.0.0.0"), false, href);
      if (href.endsWith("/healthz") && started === 0) throw new Error("connect ECONNREFUSED");
      if (href.endsWith("/healthz")) return Response.json(readyHealth);
      if (href === "http://127.0.0.1:8790/api/v1/sessions?limit=1") return ok({ sessions: [] });
      if (href === "http://127.0.0.1:8790/api/v1/runs") {
        return eventStream([
          { type: "RUN_STARTED", threadId: "thread", runId: "run" },
          { type: "RUN_FINISHED", threadId: "thread", runId: "run" },
        ]);
      }
      throw new Error(`unexpected ${href}`);
    },
    renderApp: async (options) => {
      assert.ok(options.tui);
      await options.tui.sessions.listSessions({ limit: 1 });
      const seen: string[] = [];
      for await (const event of options.client.runAgent({
        threadId: "thread",
        runId: "run",
        messages: [{ id: "m", role: "user", content: "hi" }],
      })) seen.push(event.type);
      assert.deepEqual(seen, ["RUN_STARTED", "RUN_FINISHED"]);
      return "exit";
    },
  });
  assert.equal(code, 0);
  assert.equal(started, 1);
  assert.deepEqual(requested, [
    "http://127.0.0.1:8790/healthz",
    "http://127.0.0.1:8790/healthz",
    "http://127.0.0.1:8790/api/v1/sessions?limit=1",
    "http://127.0.0.1:8790/api/v1/runs",
  ]);
});

it("a backend started with --config is reached through loopback when ready announces 0.0.0.0", async () => {
  const directory = await mkdtemp(join(tmpdir(), "dataagent-config-wildcard-"));
  const configPath = join(directory, "selected.yaml");
  await writeFile(configPath, JSON.stringify({ server: { host: "0.0.0.0", port: 8877 } }));
  const requested: string[] = [];
  const code = await runTui({
    argv: ["--config", configPath],
    env: { DATAAGENT_HOME: directory },
    startBackend: async () => startedBackend("http://0.0.0.0:8877/api/v1/runs"),
    fetchImpl: async (url) => {
      const href = String(url);
      requested.push(href);
      assert.equal(href.includes("0.0.0.0"), false, href);
      if (href.endsWith("/healthz")) return Response.json(readyHealth);
      if (href === "http://127.0.0.1:8877/api/v1/sessions?limit=1") return ok({ sessions: [] });
      throw new Error(`unexpected ${href}`);
    },
    renderApp: async (options) => {
      assert.ok(options.tui);
      await options.tui.sessions.listSessions({ limit: 1 });
      return "exit";
    },
  });
  assert.equal(code, 0);
  assert.deepEqual(requested, [
    "http://127.0.0.1:8877/healthz",
    "http://127.0.0.1:8877/api/v1/sessions?limit=1",
  ]);
});

it("an expanded $env host still starts the backend when nothing is listening", async () => {
  const directory = await mkdtemp(join(tmpdir(), "dataagent-env-host-"));
  await writeFile(join(directory, "config.yaml"), JSON.stringify({
    server: { host: "$env{SERVER_HOST}", port: 8790 },
  }));
  await writeFile(join(directory, ".env"), "SERVER_HOST=127.0.0.1\n");
  const requested: string[] = [];
  let started = 0;
  let rendered = false;
  const code = await runTui({
    argv: [],
    env: { DATAAGENT_HOME: directory, SERVER_HOST: "127.0.0.1" },
    startBackend: async () => {
      started += 1;
      return startedBackend("http://127.0.0.1:8790/api/v1/runs");
    },
    fetchImpl: async (url) => {
      const href = String(url);
      requested.push(href);
      if (started === 0) throw new Error("connect ECONNREFUSED");
      if (href.endsWith("/healthz")) return Response.json(readyHealth);
      throw new Error(`unexpected ${href}`);
    },
    renderApp: async (options) => {
      rendered = true;
      assert.ok(options.tui);
      return "exit";
    },
  });
  assert.equal(code, 0);
  assert.equal(started, 1);
  assert.equal(rendered, true);
  assert.deepEqual(requested, [
    "http://127.0.0.1:8790/healthz",
    "http://127.0.0.1:8790/healthz",
  ]);
});

it("a backend started by the TUI is reached through IPv6 loopback when ready announces ::", async () => {
  const directory = await mkdtemp(join(tmpdir(), "dataagent-ready-ipv6-"));
  await writeFile(join(directory, "config.yaml"), JSON.stringify({
    server: { host: "::", port: 8790 },
  }));
  const requested: string[] = [];
  let started = 0;
  const code = await runTui({
    argv: [],
    env: { DATAAGENT_HOME: directory },
    startBackend: async () => {
      started += 1;
      return startedBackend("http://[::]:8790/api/v1/runs");
    },
    fetchImpl: async (url) => {
      const href = String(url);
      requested.push(href);
      assert.equal(href.includes("[::]:"), false, href);
      if (href.endsWith("/healthz") && started === 0) throw new Error("connect ECONNREFUSED");
      if (href.endsWith("/healthz")) return Response.json(readyHealth);
      if (href === "http://[::1]:8790/api/v1/sessions?limit=1") return ok({ sessions: [] });
      if (href === "http://[::1]:8790/api/v1/runs") {
        return eventStream([
          { type: "RUN_STARTED", threadId: "thread", runId: "run" },
          { type: "RUN_FINISHED", threadId: "thread", runId: "run" },
        ]);
      }
      throw new Error(`unexpected ${href}`);
    },
    renderApp: async (options) => {
      assert.ok(options.tui);
      await options.tui.sessions.listSessions({ limit: 1 });
      const seen: string[] = [];
      for await (const event of options.client.runAgent({
        threadId: "thread",
        runId: "run",
        messages: [{ id: "m", role: "user", content: "hi" }],
      })) seen.push(event.type);
      assert.deepEqual(seen, ["RUN_STARTED", "RUN_FINISHED"]);
      return "exit";
    },
  });
  assert.equal(code, 0);
  assert.equal(started, 1);
  assert.deepEqual(requested, [
    "http://[::1]:8790/healthz",
    "http://[::1]:8790/healthz",
    "http://[::1]:8790/api/v1/sessions?limit=1",
    "http://[::1]:8790/api/v1/runs",
  ]);
});

it("reads the listen address from Home config.yaml", async () => {
  const directory = await mkdtemp(join(tmpdir(), "dataagent-v2-listen-"));
  await writeFile(join(directory, "config.yaml"), JSON.stringify({
    server: { host: "0.0.0.0", port: 8877 },
  }));
  assert.deepEqual(configuredListen({ DATAAGENT_HOME: directory }), {
    host: "0.0.0.0",
    port: "8877",
  });
});

it("connects a wildcard bind address through loopback and accepts another IP", async () => {
  const directory = await mkdtemp(join(tmpdir(), "dataagent-v2-client-host-"));
  await writeFile(join(directory, "config.yaml"), JSON.stringify({
    server: { host: "127.0.0.1", port: 8790 },
  }));
  const env = { DATAAGENT_HOME: directory };
  assert.equal(
    normalizeRuntimeUrl("http://0.0.0.0:8877/api/v1/runs", env),
    "http://127.0.0.1:8877/api/v1/runs",
  );
  assert.equal(
    normalizeRuntimeUrl("http://[::]:8790/api/v1/runs", env),
    "http://[::1]:8790/api/v1/runs",
  );
  assert.equal(
    normalizeRuntimeUrl("http://10.0.0.8:8802/api/v1/runs", env),
    "http://10.0.0.8:8802/api/v1/runs",
  );
});

it("a backend started with --config shares one loopback client URL and stops with the TUI", async () => {
  const directory = await mkdtemp(join(tmpdir(), "dataagent-ready-config-wildcard-"));
  const configPath = join(directory, "selected.yaml");
  await writeFile(configPath, JSON.stringify({ server: { host: "0.0.0.0", port: 8877 } }));
  const requested: string[] = [];
  let rendered = false;
  let stopped = false;
  const code = await runTui({
    argv: ["--config", configPath],
    env: { DATAAGENT_HOME: directory },
    startBackend: async () => {
      const backend = startedBackend("http://0.0.0.0:8877/api/v1/runs");
      return { ...backend, stop: async () => { stopped = true; } };
    },
    fetchImpl: async (url) => {
      const href = String(url);
      requested.push(href);
      assert.equal(href.includes("0.0.0.0"), false, href);
      if (href.endsWith("/healthz")) return Response.json(readyHealth);
      if (href === "http://127.0.0.1:8877/api/v1/sessions?limit=1") return ok({ sessions: [] });
      if (href === "http://127.0.0.1:8877/api/v1/runs") {
        return eventStream([
          { type: "RUN_STARTED", threadId: "thread", runId: "run" },
          { type: "RUN_FINISHED", threadId: "thread", runId: "run" },
        ]);
      }
      throw new Error(`unexpected ${href}`);
    },
    renderApp: async (options) => {
      rendered = true;
      assert.ok(options.tui);
      await options.tui.sessions.listSessions({ limit: 1 });
      const seen: string[] = [];
      for await (const event of options.client.runAgent({
        threadId: "thread",
        runId: "run",
        messages: [{ id: "m", role: "user", content: "hi" }],
      })) seen.push(event.type);
      assert.deepEqual(seen, ["RUN_STARTED", "RUN_FINISHED"]);
      return "exit";
    },
  });
  assert.equal(code, 0);
  assert.equal(rendered, true);
  assert.equal(stopped, true);
  assert.deepEqual(requested, [
    "http://127.0.0.1:8877/healthz",
    "http://127.0.0.1:8877/api/v1/sessions?limit=1",
    "http://127.0.0.1:8877/api/v1/runs",
  ]);
});

it("accepts an explicit HTTP port 80 and an omitted port", async () => {
  const directory = await mkdtemp(join(tmpdir(), "dataagent-port80-"));
  await writeFile(join(directory, "config.yaml"), JSON.stringify({
    server: { host: "127.0.0.1", port: 80 },
  }));
  const env = { DATAAGENT_HOME: directory };
  assert.equal(
    normalizeRuntimeUrl("http://127.0.0.1:80/api/v1/runs", env),
    "http://127.0.0.1:80/api/v1/runs",
  );
  assert.equal(
    normalizeRuntimeUrl("http://127.0.0.1/api/v1/runs", env),
    "http://127.0.0.1:80/api/v1/runs",
  );
});

it("attaches to a configured IPv6 listen address", async () => {
  const directory = await mkdtemp(join(tmpdir(), "dataagent-ipv6-"));
  await writeFile(join(directory, "config.yaml"), JSON.stringify({
    server: { host: "::1", port: 8790 },
  }));
  const env = { DATAAGENT_HOME: directory };
  assert.equal(
    normalizeRuntimeUrl("http://[::1]:8790/api/v1/runs", env),
    "http://[::1]:8790/api/v1/runs",
  );
  let requested = "";
  const code = await runTui({
    argv: ["--runtime-url", "http://[::1]:8790/api/v1/runs"],
    env,
    startBackend: async () => { throw new Error("must not start"); },
    fetchImpl: async (url) => {
      requested = String(url);
      return Response.json(readyHealth);
    },
    renderApp: async () => "exit",
  });
  assert.equal(code, 0);
  assert.equal(requested, "http://[::1]:8790/healthz");
});

it("an origin URL attaches without starting a backend", async () => {
  let requested = "";
  const code = await runTui({
    argv: ["--runtime-url", "http://127.0.0.1:8790"],
    startBackend: async () => { throw new Error("must not start"); },
    fetchImpl: async (url) => {
      requested = String(url);
      return Response.json(readyHealth);
    },
    renderApp: async () => "exit",
  });
  assert.equal(code, 0);
  assert.equal(requested, "http://127.0.0.1:8790/healthz");
});

it("an explicit stream URL attaches without starting a backend", async () => {
  const directory = await mkdtemp(join(tmpdir(), "dataagent-attach-"));
  const code = await runTui({
    argv: ["--runtime-url", "http://127.0.0.1:8790/api/v1/runs"],
    fetchImpl: async () => Response.json({
      protocol: "dataagent", ready: true, instanceId: "test", model: "GLM-5.2",
      home: directory, stateDir: join(directory, ".dataagent/state"), timeoutSeconds: 180,
    }),
    renderApp: async (options) => {
      assert.ok(options.tui);
      return "exit";
    },
  });
  assert.equal(code, 0);
});

it("Startup bypasses auth and legacy config, retaining actual model/Home", async () => {
  const directory = await mkdtemp(join(tmpdir(), "dataagent-tui-"));
  const calls: string[] = [];
  const code = await runTui({
    argv: ["--runtime-url", "http://127.0.0.1:8790/api/v1/runs", "--resume", "saved"],
    fetchImpl: async (url, init) => {
      calls.push(String(url));
      assert.equal(new Headers(init?.headers).get("authorization"), null);
      assert.equal(init?.redirect, "error");
      return Response.json({ protocol: "dataagent", ready: true, instanceId: "test", model: "GLM-5.2", home: directory,
        workspaces: [{ name: "inputs", path: "/business/中文 data" }],
        stateDir: join(directory, ".dataagent/state"), timeoutSeconds: 180 });
    },
    renderApp: async (options) => {
      assert.equal(options.configClient, undefined);
      assert.equal(options.tui?.model, "GLM-5.2");
      assert.equal(options.tui?.home, directory);
      assert.equal(options.tui?.mounts, "inputs: /business/中文 data");
      assert.equal(options.initialResume?.sessionId, "saved");
      assert.deepEqual(store.getState().workspaceConfig.skill, []);
      assert.deepEqual(store.getState().workspaceConfig.db, []);
      return "exit";
    },
  });
  assert.equal(code, 0);
  assert.deepEqual(calls, ["http://127.0.0.1:8790/healthz"]);
});

it("Output paths are session-relative, not fabricated virtual mounts", async () => {
  const sessions = new SessionClient("http://127.0.0.1:8790", async () => ok({
    files: [{ path: "reports/销售.md", size: 42, modifiedAt: "2026-09-23T00:00:00Z" }],
  }));
  const [file] = await sessions.listOutputs("test");
  assert.equal(file?.summary, "Session outputs: reports/销售.md");
  assert.deepEqual(file?.detail, {
    type: "file", path: "reports/销售.md", size: 42, mtime: "2026-09-23T00:00:00Z",
  });
});

it("A session that has not run yet lists no outputs", async () => {
  const sessions = new SessionClient("http://127.0.0.1:8790", async () => Response.json(
    { success: false, error: { code: "RESOURCE_NOT_FOUND", message: "Session not found" } },
    { status: 404 },
  ));
  assert.deepEqual(await sessions.listOutputs("new"), []);
});

it("A truncated output preview carries a visible notice", async () => {
  const sessions = new SessionClient("http://127.0.0.1:8790", async () => ok({
    type: "file", path: "large.md", content: "head", truncated: true,
  }));
  const preview = await sessions.getOutputPreview("one", "large.md") as { content: string };
  assert.match(preview.content, /^head\n\n\[Preview truncated at 64 KiB/);
});

it("Health check accepts without filesystem paths", async () => {
  let rendered = false;
  const code = await connectTui({
    runtimeUrl: "http://127.0.0.1:8790/api/v1/runs",
    fetchImpl: async () => Response.json({
      protocol: "dataagent", ready: true, instanceId: "test", model: "test",
      timeoutSeconds: 180,
    }),
    renderApp: async () => {
      rendered = true;
      return "exit";
    },
  });
  assert.equal(code, 0);
  assert.equal(rendered, true);
});

it("log path is Home runtime/logs/tui.log and ignores a separate log-path variable", () => {
  assert.equal(
    resolveTuiLogPath({ DATAAGENT_HOME: "/home/dev/.dataagent" }),
    "/home/dev/.dataagent/runtime/logs/tui.log",
  );
  assert.equal(
    resolveTuiLogPath({
      DATAAGENT_HOME: "/home/dev/.dataagent",
      DATAAGENT_TUI_LOG_PATH: "/home/dev/tui.log",
    }),
    "/home/dev/.dataagent/runtime/logs/tui.log",
  );
  assert.equal(
    resolveTuiLogPath({}),
    join(homedir(), ".dataagent", "runtime", "logs", "tui.log"),
  );
  assert.equal(
    resolveTuiLogPath({ DATAAGENT_HOME: "~/agent-home" }),
    join(homedir(), "agent-home", "runtime", "logs", "tui.log"),
  );
  const launch = mkdtempSync(join(tmpdir(), "tui-home-"));
  const previous = process.cwd();
  try {
    process.chdir(launch);
    assert.equal(
      resolveTuiLogPath({ DATAAGENT_HOME: "relative-home", INIT_CWD: "/ignored" }),
      join(process.cwd(), "relative-home", "runtime", "logs", "tui.log"),
    );
  } finally {
    process.chdir(previous);
  }
  assert.equal(resolveTuiLogPath({}).includes(tmpdir()), false);
});

it("A relative stateDir without rendering", async () => {
  await assert.rejects(connectTui({
    runtimeUrl: "http://127.0.0.1:8790/api/v1/runs",
    fetchImpl: async () => Response.json({
      protocol: "dataagent", ready: true, instanceId: "test", model: "test",
      home: "/workspace", stateDir: "relative/state", timeoutSeconds: 180,
    }),
    renderApp: async () => { assert.fail("Invalid health must not enter Ink"); },
  }), /Invalid health response/);
});

it("an unknown option does not start the backend", async () => {
  const errors: string[] = [];
  const original = console.error;
  console.error = (...args: unknown[]) => {
    errors.push(args.map((arg) => String(arg)).join(" "));
  };
  try {
    const code = await runTui({
      argv: ["--not-a-flag"],
      startBackend: async () => { throw new Error("must not start"); },
      fetchImpl: async () => { assert.fail("unknown option must not connect"); },
      renderApp: async () => { assert.fail("unknown option must not render"); },
    });
    assert.equal(code, 1);
    assert.match(errors.join("\n"), /Unknown option: --not-a-flag/);
  } finally {
    console.error = original;
  }
});

it("Missing path values before starting", async () => {
  for (const args of [["--workspace"], ["--config"], ["--env-file"]]) {
    assert.equal(await runTui({ argv: args }), 1);
  }
});

it("Requests cannot escape the configured origin", async () => {
  let calls = 0;
  const transport = sessionTransport("http://127.0.0.1:8790/api/v1/runs", async () => {
    calls++; return Response.json({});
  });
  await assert.rejects(transport("https://example.com/healthz"), /another origin/);
  assert.equal(calls, 0);
  assert.throws(
    () => sessionTransport("http://example.com/api/v1/runs", transport, { DATAAGENT_HOME: "/nonexistent-dataagent-home" }),
    /configured listen URL/,
  );
});

it("native AG-UI session history restores interleaved text and tool results", () => {
  const result = restoreMessages([
    { id: "u", role: "user", content: "Compute" },
    { id: "a1", role: "assistant", content: "Computing", toolCalls: [{ id: "tool", type: "function", function: { name: "common__summarize_numbers", arguments: '{"numbers":[1]}' } }] },
    { id: "t", role: "tool", toolCallId: "tool", content: '{"mean":1}' },
    { id: "a2", role: "assistant", content: "Mean: 1" },
  ]);
  assert.equal(result.messages.length, 2);
  assert.deepEqual(result.messages[1]?.elements.map((element) => element.type), ["text", "tool_call", "text"]);
  assert.equal(result.toolCalls[0]?.result, '{"mean":1}');
});

it("restored error ToolMessages remain failed instead of becoming successful", () => {
  const restored = restoreMessages([
    { id: "a", role: "assistant", toolCalls: [{ id: "call", type: "function", function: { name: "common__summarize_numbers", arguments: '{}' } }] },
    { id: "t", role: "tool", toolCallId: "call", content: "Invalid input", error: "error" },
  ]);
  assert.equal(restored.toolCalls[0]?.status, "failed");
  assert.equal(restored.toolCalls[0]?.result, "Invalid input");
});

for (const [payload, expected] of [
  [{ row_count: 5 }, "success"],
  [{ row_count: 5, error: null }, "success"],
  [{ error: "" }, "success"],
  [{ error: false }, "success"],
  [{ result: { row_count: 5, error: null } }, "success"],
  [{ error: "Query failed" }, "failed"],
  [{ result: { error: { message: "Query failed" } } }, "failed"],
  [{ status: "error", error: null }, "failed"],
  [{ status: "failed" }, "failed"],
  [{ isError: true, error: null }, "failed"],
  [{ result: { status: "error" } }, "failed"],
  [{ result: { isError: true } }, "failed"],
] as const) {
  it(`tool outcome agrees between live result and restored history: ${JSON.stringify(payload)}`, () => {
    const content = JSON.stringify(payload);
    let live = reduceLiveRunEvent(createInitialLiveRun(), {
      type: "TOOL_CALL_START", toolCallId: "call", toolCallName: "task",
    });
    live = reduceLiveRunEvent(live, { type: "TOOL_CALL_RESULT", toolCallId: "call", content });
    const restored = restoreMessages([
      { id: "a", role: "assistant", toolCalls: [{ id: "call", type: "function", function: { name: "task", arguments: '{"subagent_type":"nl2sql"}' } }] },
      { id: "t", role: "tool", toolCallId: "call", content },
    ]);
    assert.equal(live.toolCalls[0]?.status, expected, "streamed result");
    assert.equal(restored.toolCalls[0]?.status, expected, "restored result");
    assert.equal(live.events[0]?.summary === "执行失败。", expected === "failed");
    assert.equal(reduceLiveRunEvent(live, { type: "RUN_FINISHED" }).toolCalls[0]?.status, expected);
  });
}

for (const explicitConfig of [false, true]) it(`domain ready URL retains selected env file (explicit config=${explicitConfig})`, async () => {
  const directory = await mkdtemp(join(tmpdir(), "dataagent-selected-env-"));
  const config = join(directory, explicitConfig ? 'selected.yml' : 'config.yaml');
  const envFile = join(directory, 'selected.env');
  await writeFile(config, 'server:\n  host: backend.example.test\n  port: $env{REVIEW_API_PORT}\n');
  await writeFile(join(directory, '.env'), 'UNUSED=home\n');
  await writeFile(envFile, 'REVIEW_API_PORT=8811\n');
  if (explicitConfig) await writeFile(join(directory, 'config.yaml'), 'server:\n  host: wrong.example.test\n');
  let started = 0;
  let stopped = 0;
  let rendered = 0;
  const requested: string[] = [];
  const code = await runTui({
    argv: ['--env-file', envFile, ...(explicitConfig ? ['--config', config] : [])],
    env: { DATAAGENT_HOME: directory },
    startBackend: async (options) => {
      assert.ok(options);
      assert.equal(options.envFile, envFile);
      assert.equal(options.configPath, explicitConfig ? config : undefined);
      started += 1;
      return { ...startedBackend('http://backend.example.test:8811/api/v1/runs'), stop: async () => { stopped += 1; } };
    },
    fetchImpl: async (url, init) => {
      requested.push(String(url));
      assert.equal(init?.redirect, 'error');
      return Response.json(readyHealth);
    },
    renderApp: async () => { rendered += 1; return 'exit'; },
  });
  assert.equal(code, 0);
  assert.equal(started, 1);
  assert.equal(rendered, 1);
  assert.equal(stopped, 1);
  assert.deepEqual(requested, ['http://backend.example.test:8811/healthz']);
  const transport = sessionTransport('http://backend.example.test:8811/api/v1/runs', async () => {
    assert.fail('cross-origin request must not be sent');
  }, { DATAAGENT_HOME: directory }, explicitConfig ? config : undefined, envFile);
  await assert.rejects(() => transport('http://other.example.test:8811/healthz'), /another origin/);
});
