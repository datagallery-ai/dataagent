import assert from "node:assert/strict";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { it } from "node:test";
import { runTui } from "./index.js";
import { store } from "./state/store.js";
import { normalizeRuntimeUrl, resolveTuiLogPath, runV2Tui, v2Transport } from "./v2.js";
import { restoreV2Messages, V2SessionClient } from "./protocol/v2-session-client.js";
import { createInitialLiveRun, reduceLiveRunEvent } from "./state/live-run-state.js";

function startedBackend(runtimeUrl = "http://127.0.0.1:8790/dataagent/stream") {
  const controller = new AbortController();
  return {
    runtimeUrl,
    instanceId: "00000000-0000-4000-8000-000000000001" as `${string}-${string}-${string}-${string}-${string}`,
    signal: controller.signal,
    stop: async () => {},
  };
}

it("no arguments start the V2 backend", async () => {
  const directory = await mkdtemp(join(tmpdir(), "dataagent-v2-default-"));
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
      assert.equal(options.v2?.model, "GLM-5.2");
      return "exit";
    },
  });
  assert.equal(code, 0);
  assert.equal(started, 1);
  assert.equal(probed, "http://127.0.0.1:8790/healthz");
});

it("an already running control plane is attached instead of started", async () => {
  const directory = await mkdtemp(join(tmpdir(), "dataagent-v2-busy-"));
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
      assert.equal(options.v2?.model, "GLM-5.2");
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
      protocol: "dataagent-v2",
      status: "ready",
      instanceId: "00000000-0000-4000-8000-000000000001",
      model: "GLM-5.2",
      timeoutSeconds: 180,
    },
  },
};

it("attaches to server.host and server.port from config.json when already up", async () => {
  const directory = await mkdtemp(join(tmpdir(), "dataagent-v2-host-"));
  await writeFile(join(directory, "config.json"), JSON.stringify({
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
  const directory = await mkdtemp(join(tmpdir(), "dataagent-v2-attach-host-"));
  await writeFile(join(directory, "config.json"), JSON.stringify({
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

it("attaches to the port from config.json", async () => {
  const directory = await mkdtemp(join(tmpdir(), "dataagent-v2-port-"));
  await writeFile(join(directory, "config.json"), JSON.stringify({ server: { port: 8796 } }));
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

it("accepts an explicit HTTP port 80 and an omitted port", async () => {
  const directory = await mkdtemp(join(tmpdir(), "dataagent-v2-port80-"));
  await writeFile(join(directory, "config.json"), JSON.stringify({
    server: { host: "127.0.0.1", port: 80 },
  }));
  const env = { DATAAGENT_HOME: directory };
  assert.equal(
    normalizeRuntimeUrl("http://127.0.0.1:80/dataagent/stream", env),
    "http://127.0.0.1:80/dataagent/stream",
  );
  assert.equal(
    normalizeRuntimeUrl("http://127.0.0.1/dataagent/stream", env),
    "http://127.0.0.1:80/dataagent/stream",
  );
});

it("attaches to a configured IPv6 listen address", async () => {
  const directory = await mkdtemp(join(tmpdir(), "dataagent-v2-ipv6-"));
  await writeFile(join(directory, "config.json"), JSON.stringify({
    server: { host: "::1", port: 8790 },
  }));
  const env = { DATAAGENT_HOME: directory };
  assert.equal(
    normalizeRuntimeUrl("http://[::1]:8790/dataagent/stream", env),
    "http://[::1]:8790/dataagent/stream",
  );
  let requested = "";
  const code = await runTui({
    argv: ["--runtime-url", "http://[::1]:8790/dataagent/stream"],
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
  const directory = await mkdtemp(join(tmpdir(), "dataagent-v2-attach-"));
  const code = await runTui({
    argv: ["--runtime-url", "http://127.0.0.1:8790/dataagent/stream"],
    fetchImpl: async () => Response.json({
      protocol: "dataagent-v2", status: "ready", instanceId: "test", model: "GLM-5.2",
      home: directory, stateDir: join(directory, ".dataagent/state"), timeoutSeconds: 180,
    }),
    renderApp: async (options) => {
      assert.ok(options.v2);
      return "exit";
    },
  });
  assert.equal(code, 0);
});

it("V2 startup bypasses auth and legacy config, retaining actual model/Home", async () => {
  const directory = await mkdtemp(join(tmpdir(), "dataagent-v2-tui-"));
  const calls: string[] = [];
  const code = await runTui({
    argv: ["--runtime-url", "http://127.0.0.1:8790/dataagent/stream", "--resume", "saved"],
    fetchImpl: async (url, init) => {
      calls.push(String(url));
      assert.equal(new Headers(init?.headers).get("authorization"), null);
      assert.equal(init?.redirect, "error");
      return Response.json({ protocol: "dataagent-v2", status: "ready", instanceId: "test", model: "GLM-5.2", home: directory,
        workspaces: [{ name: "inputs", path: "/business/中文 data" }],
        stateDir: join(directory, ".dataagent/state"), timeoutSeconds: 180 });
    },
    renderApp: async (options) => {
      assert.equal(options.configClient, undefined);
      assert.equal(options.v2?.model, "GLM-5.2");
      assert.equal(options.v2?.home, directory);
      assert.equal(options.v2?.mounts, "inputs: /business/中文 data");
      assert.equal(options.initialResume?.sessionId, "saved");
      assert.deepEqual(store.getState().workspaceConfig.skill, []);
      assert.deepEqual(store.getState().workspaceConfig.db, []);
      return "exit";
    },
  });
  assert.equal(code, 0);
  assert.deepEqual(calls, ["http://127.0.0.1:8790/healthz"]);
});

it("V2 output paths are session-relative, not fabricated virtual mounts", async () => {
  const sessions = new V2SessionClient("http://127.0.0.1:8790", async () => Response.json({
    files: [{ path: "reports/销售.md", size: 42, modifiedAt: "2026-09-23T00:00:00Z" }],
  }));
  const [file] = await sessions.listOutputs("test");
  assert.equal(file?.summary, "Session outputs: reports/销售.md");
  assert.deepEqual(file?.detail, {
    type: "file", path: "reports/销售.md", size: 42, mtime: "2026-09-23T00:00:00Z",
  });
});

it("V2 accepts health without filesystem paths", async () => {
  let rendered = false;
  const code = await runV2Tui({
    runtimeUrl: "http://127.0.0.1:8790/dataagent/stream",
    fetchImpl: async () => Response.json({
      protocol: "dataagent-v2", status: "ready", instanceId: "test", model: "test",
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

it("log path ignores health stateDir and the shared temp directory", () => {
  assert.equal(
    resolveTuiLogPath({ DATAAGENT_HOME: "/home/dev/.dataagent" }),
    "/home/dev/.dataagent/runtime/logs/tui.log",
  );
  assert.equal(
    resolveTuiLogPath({ DATAAGENT_V2_LOG_PATH: "/home/dev/tui.log", DATAAGENT_HOME: "/ignored" }),
    "/home/dev/tui.log",
  );
  const fallback = resolveTuiLogPath({});
  assert.equal(fallback.endsWith(`${join(".datafoundry", "tui.log")}`), true);
  assert.equal(fallback.includes(tmpdir()), false);
  assert.throws(() => resolveTuiLogPath({ DATAAGENT_V2_LOG_PATH: "relative.log" }), /absolute/);
});

it("V2 rejects a relative stateDir without rendering", async () => {
  await assert.rejects(runV2Tui({
    runtimeUrl: "http://127.0.0.1:8790/dataagent/stream",
    fetchImpl: async () => Response.json({
      protocol: "dataagent-v2", status: "ready", instanceId: "test", model: "test",
      home: "/workspace", stateDir: "relative/state", timeoutSeconds: 180,
    }),
    renderApp: async () => { assert.fail("Invalid health must not enter Ink"); },
  }), /Invalid V2 health response/);
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

it("V2 rejects missing path values before starting", async () => {
  for (const args of [["--workspace"], ["--config"], ["--env-file"]]) {
    assert.equal(await runTui({ argv: args }), 1);
  }
});

it("V2 requests cannot escape the configured origin", async () => {
  let calls = 0;
  const transport = v2Transport("http://127.0.0.1:8790/dataagent/stream", async () => {
    calls++; return Response.json({});
  });
  await assert.rejects(transport("https://example.com/healthz"), /another origin/);
  assert.equal(calls, 0);
  assert.throws(
    () => v2Transport("http://example.com/dataagent/stream", transport, { DATAAGENT_HOME: "" }),
    /configured listen URL/,
  );
});

it("native AG-UI session history restores interleaved text and tool results", () => {
  const result = restoreV2Messages([
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
  const restored = restoreV2Messages([
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
    const restored = restoreV2Messages([
      { id: "a", role: "assistant", toolCalls: [{ id: "call", type: "function", function: { name: "task", arguments: '{"subagent_type":"nl2sql"}' } }] },
      { id: "t", role: "tool", toolCallId: "call", content },
    ]);
    assert.equal(live.toolCalls[0]?.status, expected, "streamed result");
    assert.equal(restored.toolCalls[0]?.status, expected, "restored result");
    assert.equal(live.events[0]?.summary === "执行失败。", expected === "failed");
    assert.equal(reduceLiveRunEvent(live, { type: "RUN_FINISHED" }).toolCalls[0]?.status, expected);
  });
}
