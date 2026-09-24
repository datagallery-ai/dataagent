import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { randomUUID } from "node:crypto";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { z } from "zod";
import { terminalText } from "./utils/terminal-text.js";

const identity = { protocol: z.literal("dataagent-v2"), instanceId: z.string().min(1) };
const startupSchema = z.object({
  ...identity, type: z.literal("ready"), runtimeUrl: z.string(),
}).strict();

type BackendSpawner = (
  args: string[], options: { cwd: string; env: NodeJS.ProcessEnv },
) => ChildProcessWithoutNullStreams;

/** Start only the backend: this Node process is already the actual TUI. */
export async function startV2Backend(options: {
  configPath?: string | undefined;
  envFile?: string | undefined;
  workspace?: string | undefined;
  workspaces?: readonly string[] | undefined;
  user?: string | undefined;
  session?: string | undefined;
  spawnBackend?: BackendSpawner | undefined;
  readinessTimeoutMs?: number | undefined;
} = {}) {
  const cwd = process.env.INIT_CWD ?? process.cwd();
  const backendPackage = fileURLToPath(new URL("../../../runtime/agentkit", import.meta.url));
  const instanceId = randomUUID();
  const args = ["run", "--project", backendPackage, "dataagent", "serve", "--stdio-ready"];
  for (const [flag, path] of [
    ["--config", options.configPath], ["--env-file", options.envFile],
  ] as const) {
    if (path !== undefined) args.push(flag, resolve(cwd, path));
  }
  const workspaces = [...(options.workspace ? [options.workspace] : []), ...(options.workspaces ?? [])];
  for (const path of workspaces) args.push("--workspace", resolve(cwd, path));
  if (options.user) args.push("--user", options.user);
  if (options.session) args.push("--session", options.session);
  const launch: BackendSpawner = options.spawnBackend ?? ((argv, opts) =>
    spawn("uv", argv, { ...opts, detached: true, stdio: ["pipe", "pipe", "pipe"] }));
  const child = launch(args, {
    cwd, env: { ...process.env, DATAAGENT_V2_INSTANCE_ID: instanceId },
  });
  const shutdown = new AbortController();
  const signals = ["SIGINT", "SIGTERM", "SIGHUP"] as const;
  const interrupted = () => shutdown.abort(new DOMException("TUI interrupted", "AbortError"));
  for (const signal of signals) process.on(signal, interrupted);
  let closing = false;
  let finished = false;
  let stderr = "";
  const safe = (message: string) => terminalText(message
    .replace(/\bsk-[A-Za-z0-9_-]+/g, "[REDACTED]"));
  child.stderr.setEncoding("utf8").on("data", (chunk: string) => { stderr = (stderr + chunk).slice(-4000); });
  child.stdin.on("error", (error) => { if (!closing) shutdown.abort(error); });
  const closed = new Promise<void>((done) => {
    child.once("error", (error) => { finished = true; shutdown.abort(error); done(); });
    child.once("close", () => {
      finished = true;
      if (!closing) shutdown.abort(new Error(safe(
        "V2 backend exited. " + (stderr.trim() || "Inspect Home runtime/logs/backend.log."),
      )));
      done();
    });
  });
  let stopping: Promise<void> | undefined;
  const stop = (): Promise<void> => stopping ??= (async () => {
    closing = true;
    for (const signal of signals) process.removeListener(signal, interrupted);
    const kill = (signal: NodeJS.Signals) => {
      if (!child.pid || finished) return;
      try { process.kill(-child.pid, signal); }
      catch (error) { if ((error as NodeJS.ErrnoException).code !== "ESRCH") throw error; }
    };
    child.stdin.destroy();
    kill("SIGTERM");
    const timer = setTimeout(() => kill("SIGKILL"), 5000);
    try { await closed; } finally { clearTimeout(timer); }
  })();

  let timer: NodeJS.Timeout | undefined;
  let aborted: (() => void) | undefined;
  let readOutput: ((chunk: string) => void) | undefined;
  try {
    const runtimeUrl = await new Promise<string>((done, fail) => {
      let buffer = "";
      let settled = false;
      const reject = (error: unknown) => { settled = true; fail(error); };
      aborted = () => reject(shutdown.signal.reason);
      shutdown.signal.addEventListener("abort", aborted, { once: true });
      if (shutdown.signal.aborted) { aborted(); return; }
      timer = setTimeout(() => reject(new Error("V2 backend readiness timed out")), options.readinessTimeoutMs ?? 30_000);
      readOutput = (chunk) => {
        if (settled) return;
        buffer += chunk;
        try {
          while (buffer.includes("\n")) {
            const newline = buffer.indexOf("\n");
            const line = buffer.slice(0, newline);
            buffer = buffer.slice(newline + 1);
            if (Buffer.byteLength(line + "\n") > 65536) throw new Error("Startup message exceeds 64 KiB");
            const parsed = startupSchema.safeParse(JSON.parse(line));
            if (!parsed.success) throw new Error("Invalid backend startup message");
            const message = parsed.data;
            if (message.instanceId !== instanceId) throw new Error("Backend instance identity does not match this TUI");
            if (buffer.length) throw new Error("Unexpected startup output after ready");
            settled = true;
            done(message.runtimeUrl);
            return;
          }
          if (Buffer.byteLength(buffer) > 65536) throw new Error("Startup message exceeds 64 KiB");
        } catch (error) {
          // Parser diagnostics can contain input; never echo raw protocol content.
          reject(new Error(error instanceof SyntaxError ? "Invalid backend startup JSON" : safe(String(error))));
        }
      };
      child.stdout.setEncoding("utf8").on("data", readOutput);
    });
    return { runtimeUrl, instanceId, signal: shutdown.signal, stop };
  } catch (error) {
    shutdown.abort(error);
    await stop();
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new Error(safe(error instanceof Error ? error.message : String(error)));
  } finally {
    clearTimeout(timer);
    if (aborted) shutdown.signal.removeEventListener("abort", aborted);
    if (readOutput) child.stdout.removeListener("data", readOutput);
  }
}
