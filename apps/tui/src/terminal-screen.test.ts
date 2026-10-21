import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { describe, it } from "node:test";
import {
  enterAlternateScreen,
  restoreTerminalForParentShell,
  type TerminalScreenInput,
  type TerminalScreenOutput,
} from "./terminal-screen.js";

function mockStdout(): TerminalScreenOutput & { chunks: string[] } {
  const chunks: string[] = [];
  return {
    isTTY: true,
    chunks,
    write(chunk: string | Uint8Array) {
      chunks.push(String(chunk));
      return true;
    },
  };
}

function mockStdin(options: {
  isRaw?: boolean;
  queued?: Array<string | null>;
}): TerminalScreenInput & {
  resumed: boolean;
  paused: boolean;
  rawMode: boolean | undefined;
} {
  const queued = [...(options.queued ?? [])];
  const state = {
    isTTY: true as const,
    isRaw: options.isRaw ?? false,
    resumed: false,
    paused: true,
    rawMode: undefined as boolean | undefined,
    setRawMode(mode: boolean) {
      state.rawMode = mode;
      state.isRaw = mode;
    },
    resume() {
      state.resumed = true;
      state.paused = false;
    },
    pause() {
      state.paused = true;
    },
    read() {
      return queued.length > 0 ? queued.shift()! : null;
    },
  };
  return state;
}

async function waitForExit(
  child: ReturnType<typeof spawn>,
  timeoutMs = 15_000,
): Promise<{ code: number | null; stdout: string; stderr: string }> {
  let stdout = "";
  let stderr = "";
  // Drain both pipes so a long --help write cannot fill the buffer and deadlock.
  child.stdout?.setEncoding("utf8");
  child.stderr?.setEncoding("utf8");
  child.stdout?.on("data", (chunk) => {
    stdout += chunk;
  });
  child.stderr?.on("data", (chunk) => {
    stderr += chunk;
  });
  return await new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      reject(new Error(`child did not exit within ${timeoutMs}ms\n${stderr}`));
    }, timeoutMs);
    child.on("error", (error) => {
      clearTimeout(timer);
      reject(error);
    });
    child.on("exit", (code) => {
      clearTimeout(timer);
      resolve({ code, stdout, stderr });
    });
  });
}

describe("restoreTerminalForParentShell", () => {
  it("does not resume TTY stdin when no session was armed", () => {
    // Clear any armed state left by a prior test in this file.
    restoreTerminalForParentShell(mockStdin({}), mockStdout());
    const stdin = mockStdin({ queued: ["x"] });
    const stdout = mockStdout();
    restoreTerminalForParentShell(stdin, stdout);
    assert.equal(stdin.resumed, false);
    assert.equal(stdin.paused, true);
    assert.deepEqual(stdout.chunks, []);
  });

  it("drains and pauses stdin after an armed TTY session", () => {
    restoreTerminalForParentShell(mockStdin({}), mockStdout());
    const stdout = mockStdout();
    enterAlternateScreen(stdout);
    const stdin = mockStdin({ queued: ["e", "x", "i", "t", null] });
    restoreTerminalForParentShell(stdin, stdout);
    assert.equal(stdin.resumed, true);
    assert.equal(stdin.paused, true);
    assert.ok(stdout.chunks.length > 0);
  });

  it("leaves raw mode and pauses even if the session flag was already cleared", () => {
    restoreTerminalForParentShell(mockStdin({}), mockStdout());
    const stdin = mockStdin({ isRaw: true, queued: ["a"] });
    const stdout = mockStdout();
    restoreTerminalForParentShell(stdin, stdout);
    assert.equal(stdin.rawMode, false);
    assert.equal(stdin.resumed, true);
    assert.equal(stdin.paused, true);
  });
});

describe("direct-launch TTY early exits", () => {
  for (const [name, argv, expectedCode] of [
    ["--help", ["--help"], 0],
    ["argument error", ["--v2", "--config"], 1],
  ] as const) {
    it(`${name} exits under TTY stdin without hanging`, async () => {
      const indexUrl = new URL("./index.js", import.meta.url).href;
      const screenUrl = new URL("./terminal-screen.js", import.meta.url).href;
      const child = spawn(
        process.execPath,
        [
          "--input-type=module",
          "-e",
          `
            Object.defineProperty(process.stdin, "isTTY", { configurable: true, value: true });
            Object.defineProperty(process.stdout, "isTTY", { configurable: true, value: true });
            const { runTui } = await import(${JSON.stringify(indexUrl)});
            const { restoreTerminalForParentShell } = await import(${JSON.stringify(screenUrl)});
            const code = await runTui({ argv: ${JSON.stringify([...argv])} });
            restoreTerminalForParentShell();
            process.exit(code);
          `,
        ],
        { stdio: ["pipe", "pipe", "pipe"], windowsHide: true },
      );
      const { code, stdout, stderr } = await waitForExit(child);
      assert.equal(code, expectedCode, `${stderr}\n${stdout}`);
    });
  }
});
