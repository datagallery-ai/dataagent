import assert from "node:assert/strict";
import { mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { it } from "node:test";
import { runTui } from "./index.js";
import { store } from "./state/store.js";
import { runV2Tui, v2Transport } from "./v2.js";
import { restoreV2Messages, V2SessionClient } from "./protocol/v2-session-client.js";

it("V2 startup bypasses auth and legacy config, retaining actual model/Home", async () => {
  const directory = await mkdtemp(join(tmpdir(), "dataagent-v2-tui-"));
  const previous = process.env.DATAAGENT_V2_TOKEN;
  process.env.DATAAGENT_V2_TOKEN = "temporary-token";
  try {
    const calls: string[] = [];
    const code = await runTui({
      argv: ["--v2", "--runtime-url", "http://127.0.0.1:8790/dataagent/stream", "--resume", "saved"],
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
        assert.equal(options.authController, undefined);
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
  } finally {
    if (previous === undefined) delete process.env.DATAAGENT_V2_TOKEN;
    else process.env.DATAAGENT_V2_TOKEN = previous;
  }
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

for (const stateDir of [undefined, "relative/state"]) {
  it(`V2 rejects missing or relative stateDir (${stateDir}) without rendering`, async () => {
    await assert.rejects(runV2Tui({
      runtimeUrl: "http://127.0.0.1:8790/dataagent/stream",
      fetchImpl: async () => Response.json({
        protocol: "dataagent-v2", status: "ready", instanceId: "test", model: "test",
        home: "/workspace", stateDir, timeoutSeconds: 180,
      }),
      renderApp: async () => { assert.fail("Invalid health must not enter Ink"); },
    }), /Invalid V2 health response/);
  });
}

for (const flags of [
  ["--workspace", "/project"], ["--config", "/config.json"], ["--env-file", "/provider.env"],
]) {
  it(`V2 external connection rejects local ${flags[0]} before any request`, async () => {
    const code = await runTui({
      argv: ["--v2", "--runtime-url", "http://127.0.0.1:8790/dataagent/stream", ...flags],
      fetchImpl: async () => { assert.fail("Invalid arguments must not connect"); },
      renderApp: async () => { assert.fail("Invalid arguments must not render"); },
    });
    assert.equal(code, 1);
  });
}

it("V2 rejects missing path values before starting", async () => {
  for (const args of [["--workspace"], ["--config"], ["--env-file"]]) {
    assert.equal(await runTui({ argv: ["--v2", ...args] }), 1);
  }
});

it("V2 requests cannot escape the configured origin", async () => {
  let calls = 0;
  const transport = v2Transport("http://127.0.0.1:8790/dataagent/stream", async () => {
    calls++; return Response.json({});
  });
  await assert.rejects(transport("https://example.com/healthz"), /another origin/);
  assert.equal(calls, 0);
  assert.throws(() => v2Transport("http://example.com/dataagent/stream", transport), /loopback/);
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
