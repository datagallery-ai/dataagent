import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { randomUUID } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { z } from "zod";
import { terminalText } from "./utils/terminal-text.js";

const DEFAULT_SERVER_HOST = "127.0.0.1";
const DEFAULT_SERVER_PORT = "8790";

function homeDir(env: NodeJS.ProcessEnv): string | null {
  if (Object.prototype.hasOwnProperty.call(env, "DATAAGENT_HOME")) {
    return env.DATAAGENT_HOME?.trim() || null;
  }
  return join(homedir(), ".dataagent");
}

function readServer(file: string | null | undefined): { host?: unknown; port?: unknown } {
  if (!file || !existsSync(file)) return {};
  try {
    const server = JSON.parse(readFileSync(file, "utf8"))?.server;
    return server && typeof server === "object" ? server : {};
  } catch {
    return {};
  }
}

function bareHost(value: string): string {
  return value.trim().replace(/^\[(.*)\]$/u, "$1");
}

function pickHost(value: unknown, fallback: string): string {
  if (typeof value !== "string") return fallback;
  return bareHost(value) || fallback;
}

function pickPort(value: unknown, fallback: string): string {
  const port = Number(value);
  if (Number.isInteger(port) && port >= 1 && port <= 65535) return String(port);
  return fallback;
}

/** Listen address from Home config.json. */
export function configuredListen(env: NodeJS.ProcessEnv = process.env): { host: string; port: string } {
  const home = homeDir(env);
  const base = readServer(home ? join(home, "config.json") : null);
  return {
    host: pickHost(base.host, DEFAULT_SERVER_HOST),
    port: pickPort(base.port, DEFAULT_SERVER_PORT),
  };
}

export function formatListenBase(host: string, port: string): string {
  const bare = bareHost(host);
  const shown = bare.includes(":") ? `[${bare}]` : bare;
  return `http://${shown}:${port}`;
}

/** Host and port of an HTTP listen URL. A missing port is 80. */
export function readHttpListen(value: string): { host: string; port: string } | null {
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    return null;
  }
  const path = url.pathname === "/" ? "" : url.pathname.replace(/\/+$/u, "");
  if (url.protocol !== "http:" || url.username || url.password || url.search || url.hash) return null;
  if (path !== "" && path !== "/dataagent/stream") return null;
  return { host: bareHost(url.hostname), port: url.port || "80" };
}

const identity = { protocol: z.literal("dataagent"), instanceId: z.string().min(1) };
const startupSchema = z.object({
  ...identity, type: z.literal("ready"), runtimeUrl: z.string(),
}).strict();

type BackendSpawner = (
  args: string[], options: { cwd: string; env: NodeJS.ProcessEnv },
) => ChildProcessWithoutNullStreams;

function backendSpawnOptions(): {
  detached: boolean;
  stdio: ["pipe", "pipe", "pipe"];
  windowsHide: true;
} {
  return {
    // Unix: own process group so stop() can signal with a negative PID.
    // Windows: do not detach — a detached console process + force tree-kill on
    // /exit can take down the parent terminal session in ConPTY hosts.
    detached: process.platform !== "win32",
    stdio: ["pipe", "pipe", "pipe"],
    windowsHide: true,
  };
}

export function windowsVenvPython(backendPackage: string): string {
  return join(backendPackage, ".venv", "Scripts", "python.exe");
}

export type EnsureWindowsPythonDeps = {
  existsSync?: (path: string) => boolean;
  spawn?: typeof spawn;
};

/**
 * Sync agentkit deps, then return the venv interpreter for a direct Windows spawn.
 * Always runs `uv sync`: a pre-existing python.exe does not mean packages match the
 * lockfile (empty/partial venvs, interrupted installs, or outdated deps after pull).
 * Spawning python.exe afterward still avoids the dataagent.exe console flash.
 */
export function ensureWindowsBackendPython(
  backendPackage: string,
  options: { cwd: string; env: NodeJS.ProcessEnv },
  deps: EnsureWindowsPythonDeps = {},
): Promise<string> {
  const exists = deps.existsSync ?? existsSync;
  const spawnImpl = deps.spawn ?? spawn;
  const python = windowsVenvPython(backendPackage);
  return new Promise((done, fail) => {
    const child = spawnImpl("uv", ["sync", "--project", backendPackage], {
      cwd: options.cwd,
      env: options.env,
      stdio: ["ignore", "ignore", "pipe"],
      windowsHide: true,
    });
    let stderr = "";
    child.stderr.setEncoding("utf8").on("data", (chunk: string) => {
      stderr = (stderr + chunk).slice(-2000);
    });
    child.once("error", fail);
    child.once("close", (code) => {
      if (code === 0 && exists(python)) done(python);
      else fail(new Error(stderr.trim() || `uv sync failed with code ${code ?? "null"}`));
    });
  });
}

function spawnDefaultBackend(
  backendPackage: string,
  argv: string[],
  options: { cwd: string; env: NodeJS.ProcessEnv },
  windowsPython?: string,
): ChildProcessWithoutNullStreams {
  if (process.platform === "win32") {
    // `uv run dataagent` launches Scripts\dataagent.exe, a console shim that always
    // allocates its own window (windowsHide on the uv parent does not propagate).
    // Spawn the venv interpreter with -m serve so CREATE_NO_WINDOW applies.
    const serveAt = argv.indexOf("serve");
    const serveArgs = serveAt >= 0 ? argv.slice(serveAt) : ["serve", "--stdio-ready"];
    const python = windowsPython ?? windowsVenvPython(backendPackage);
    return spawn(python, ["-m", "serve", ...serveArgs], { ...options, ...backendSpawnOptions() });
  }
  return spawn("uv", argv, { ...options, ...backendSpawnOptions() });
}

/** Start only the backend: this Node process is already the actual TUI. */
export async function startBackend(options: {
  configPath?: string | undefined;
  envFile?: string | undefined;
  workspace?: string | undefined;
  workspaces?: readonly string[] | undefined;
  user?: string | undefined;
  session?: string | undefined;
  spawnBackend?: BackendSpawner | undefined;
  readinessTimeoutMs?: number | undefined;
} = {}) {
  const cwd = process.cwd();
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
  const env: NodeJS.ProcessEnv = { ...process.env, DATAAGENT_INSTANCE_ID: instanceId };
  const launchOpts = { cwd, env };
  let windowsPython: string | undefined;
  if (!options.spawnBackend && process.platform === "win32") {
    windowsPython = await ensureWindowsBackendPython(backendPackage, launchOpts);
  }
  const launch: BackendSpawner = options.spawnBackend ?? ((argv, opts) =>
    spawnDefaultBackend(backendPackage, argv, opts, windowsPython));
  const child = launch(args, launchOpts);
  const shutdown = new AbortController();
  const signals = ["SIGINT", "SIGTERM", "SIGHUP"] as const;
  const interrupted = () => shutdown.abort(new DOMException("TUI interrupted", "AbortError"));
  // Windows: installing SIGINT hooks changes the console Ctrl handler and can make
  // process teardown take down the parent ConPTY / PowerShell session on /exit.
  if (process.platform !== "win32") {
    for (const signal of signals) process.on(signal, interrupted);
  }
  let closing = false;
  let finished = false;
  let stderr = "";
  const safe = (message: string) => terminalText(message
    .replace(/\bsk-[A-Za-z0-9_-]+/g, "[REDACTED]"));
  child.stderr.setEncoding("utf8").on("data", (chunk: string) => { stderr = (stderr + chunk).slice(-4000); });
  child.stdin.on("error", (error) => { if (!closing) shutdown.abort(error); });
  const closed = new Promise<void>((done) => {
    child.once("error", (error) => {
      finished = true;
      if (!closing) shutdown.abort(error);
      done();
    });
    child.once("close", () => {
      finished = true;
      if (!closing) shutdown.abort(new Error(safe(
        "Backend exited. " + (stderr.trim() || "Inspect Home runtime/logs/backend.log."),
      )));
      done();
    });
  });
  let stopping: Promise<void> | undefined;
  const stop = (): Promise<void> => stopping ??= (async () => {
    closing = true;
    if (process.platform !== "win32") {
      for (const signal of signals) process.removeListener(signal, interrupted);
    }
    if (process.platform === "win32") {
      // Soft kill only — never taskkill /T (that flash-closed Cursor ConPTY tabs).
      try { child.stdin.end(); } catch { /* ignore */ }
      try { if (child.pid && !finished) child.kill(); } catch { /* ignore */ }
      try { child.unref(); } catch { /* ignore */ }
      await Promise.race([closed, new Promise<void>((done) => setTimeout(done, 1000))]);
      return;
    }
    const kill = (signal: NodeJS.Signals) => {
      if (!child.pid || finished) return;
      try {
        process.kill(-child.pid, signal);
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== "ESRCH") throw error;
      }
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
      timer = setTimeout(() => reject(new Error("Backend readiness timed out")), options.readinessTimeoutMs ?? 30_000);
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
