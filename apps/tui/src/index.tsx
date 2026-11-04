#!/usr/bin/env node
import { render } from "ink";
import { pathToFileURL } from "node:url";
import React from "react";
import { ConfigClient } from "./config/index.js";
import { installTerminalRedrawOptimizer } from "./terminal-redraw-optimizer.js";
import { restoreTerminalForParentShell, withAlternateScreen } from "./terminal-screen.js";
import { App } from "./ui/App.js";
import type { AgentClient } from "./protocol/types.js";
import type { TuiContext } from "./protocol/session-client.js";
import { normalizeRuntimeUrl, connectTui } from "./connect.js";
import { configuredListen, formatListenBase, startBackend } from "./backend.js";
import { terminalText } from "./utils/terminal-text.js";

export type AppExitReason = "exit";

export type RunTuiOptions = {
  argv?: string[];
  fetchImpl?: typeof fetch;
  renderApp?: typeof renderTuiApp;
  env?: NodeJS.ProcessEnv;
  startBackend?: typeof startBackend;
};

export type RenderAppOptions = {
  client: AgentClient;
  configClient?: ConfigClient | undefined;
  datasourceId: string | undefined;
  initialDatasourceId: string | undefined;
  initialResume?: { enabled: boolean; sessionId?: string | undefined };
  tui?: TuiContext | undefined;
  shutdownSignal?: AbortSignal | undefined;
  onExit: (reason: AppExitReason) => void;
};

async function renderTuiApp(options: RenderAppOptions): Promise<AppExitReason> {
  if (options.tui && !process.stdin.isTTY) {
    throw new Error("The TUI requires an interactive terminal. Use the serve command for headless access.");
  }
  let exitReason: AppExitReason = "exit";
  const restoreTerminalRedrawOptimizer = process.stdout.isTTY
    ? installTerminalRedrawOptimizer(process.stdout)
    : () => {};

  try {
    await withAlternateScreen(async () => {
      const instance = render(
        React.createElement(App, {
          client: options.client,
          configClient: options.configClient,
          datasourceId: options.datasourceId,
          initialDatasourceId: options.initialDatasourceId,
          tui: options.tui,
          onExit: (reason) => {
            exitReason = reason;
            options.onExit(reason);
          },
          ...(options.initialResume ? { initialResume: options.initialResume } : {}),
        }),
        {
          exitOnCtrlC: false,
          incrementalRendering:
            process.env.DATAFOUNDRY_TUI_INCREMENTAL_RENDERING !== "0",
          maxFps: 30,
          patchConsole: false,
          // "enabled" opts into CSI-u immediately. Ink's "auto" mode probes for
          // ~200ms and replays that buffer with stdin.unshift, which repeats
          // keystrokes typed during startup. Unsupported terminals ignore the
          // sequence. Ctrl+J remains the newline fallback. Ink restores the
          // protocol on exit/suspend.
          // Windows ConPTY: kitty keyboard has caused host/session teardown on exit.
          ...(options.tui && process.platform !== "win32"
            ? { kittyKeyboard: { mode: "enabled" as const } }
            : {}),
        },
      );
      const stop = () => {
        instance.unmount();
      };
      if (options.tui && process.platform !== "win32") {
        process.once("SIGTERM", stop);
        process.once("SIGHUP", stop);
      }
      if (options.tui) {
        options.shutdownSignal?.addEventListener("abort", stop, { once: true });
        if (options.shutdownSignal?.aborted) stop();
      }
      try {
        await instance.waitUntilExit();
      } finally {
        options.shutdownSignal?.removeEventListener("abort", stop);
        process.removeListener("SIGTERM", stop);
        process.removeListener("SIGHUP", stop);
      }
    });
  } finally {
    restoreTerminalRedrawOptimizer();
    restoreTerminalForParentShell();
  }

  return exitReason;
}

const LOCAL_START_FLAGS = ["--config", "--env-file", "--workspace", "--user", "--session"] as const;

function printHelp(): void {
  console.log(`
DataFoundry TUI - Terminal User Interface for DataFoundry

Usage:
  datafoundry-tui [options]

Options:
  --config <path>         Additional config above Home defaults
  --env-file <path>       Optional environment file, resolved by the backend
  --user <id>             Local profile (default: default)
  --workspace <path>      Read-only input; repeat to mount more than one
  --session <id>          Resume a session in the selected profile
  --runtime-url <url>     Attach to this control plane and do not start
                          a backend. Omit it to use config.json
                          server.host and server.port. The host may be
                          that name, a loopback address, or another IP.
                          0.0.0.0 and :: connect through loopback
  --resume [sessionId]    Resume a session
  --help, -h              Show this help message

Examples:
  datafoundry-tui
  datafoundry-tui --workspace /existing/data
  datafoundry-tui --runtime-url http://127.0.0.1:8790

Notes:
  With no arguments the TUI uses config.json server.host and server.port
  (default http://127.0.0.1:8790). It starts dataagent serve when that
  address is down, and connects when it is already up. --runtime-url
  attaches and does not start a backend. Its host may be server.host,
  a loopback address, or another IP. 0.0.0.0 and :: connect through
  loopback.
`);
}

function getRepeatedArg(args: string[], name: string): string[] {
  const values: string[] = [];
  for (let index = 0; index < args.length; index += 1) {
    if (args[index] !== name) continue;
    const next = args[index + 1];
    if (!next || next.startsWith("-")) throw new Error(`${name} requires a value`);
    values.push(next);
  }
  return values;
}

function getOptionalArg(args: string[], name: string): string | undefined {
  const index = args.indexOf(name);
  if (index === -1 || index + 1 >= args.length) {
    return undefined;
  }
  const next = args[index + 1];
  return next && !next.startsWith("-") ? next : undefined;
}

function resolveResumeRequest(
  args: string[],
): { enabled: boolean; sessionId?: string | undefined } | undefined {
  if (!args.includes("--resume")) return undefined;
  const sessionId = getOptionalArg(args, "--resume");
  return sessionId ? { enabled: true, sessionId } : { enabled: true };
}

const VALUE_FLAGS = ["--config", "--env-file", "--workspace", "--user", "--session", "--runtime-url"] as const;

/** Accept the startup flags. Anything else is an unknown option. */
export function parseTuiArgs(args: string[]): void {
  for (let index = 0; index < args.length; index += 1) {
    const arg = args[index];
    if (!arg?.startsWith("-")) throw new Error(`Unexpected argument: ${arg}`);
    if (arg === "--help" || arg === "-h") continue;
    if (arg === "--resume") {
      const value = args[index + 1];
      if (value && !value.startsWith("-")) index += 1;
      continue;
    }
    if ((VALUE_FLAGS as readonly string[]).includes(arg)) {
      const value = args[index + 1];
      if (!value || value.startsWith("-")) throw new Error(`${arg} requires a value`);
      index += 1;
      continue;
    }
    throw new Error(`Unknown option: ${arg}`);
  }
}

async function controlPlaneIsRunning(fetchImpl: typeof fetch, baseUrl: string): Promise<boolean> {
  try {
    const response = await fetchImpl(`${baseUrl}/healthz`, { signal: AbortSignal.timeout(800) });
    if (!response.ok) return false;
    const body = await response.json() as { success?: unknown; data?: { conversation?: unknown } };
    return body?.success === true && !!body.data && typeof body.data === "object" && "conversation" in body.data;
  } catch {
    return false;
  }
}

export async function runTui(options: RunTuiOptions = {}): Promise<number> {
  const args = options.argv ?? process.argv.slice(2);
  const fetchImpl = options.fetchImpl ?? globalThis.fetch.bind(globalThis);
  const renderApp = options.renderApp ?? renderTuiApp;
  const launchBackend = options.startBackend ?? startBackend;

  if (args.includes("--help") || args.includes("-h")) {
    printHelp();
    return 0;
  }

  try {
    parseTuiArgs(args);
    const env = options.env ?? process.env;
    const runtimeUrl = (() => {
      const explicit = getOptionalArg(args, "--runtime-url");
      return explicit ? normalizeRuntimeUrl(explicit, env) : undefined;
    })();
    const session = getOptionalArg(args, "--session");
    const resume = resolveResumeRequest(args);
    if (session && resume?.sessionId && resume.sessionId !== session) {
      throw new Error("--session and --resume name different sessions");
    }
    if (runtimeUrl && LOCAL_START_FLAGS.some((flag) => args.includes(flag))) {
      throw new Error("Local startup options cannot be combined with --runtime-url");
    }
    const localStartup = LOCAL_START_FLAGS.some((flag) => args.includes(flag));
    let attachUrl = runtimeUrl;
    if (!attachUrl && !localStartup) {
      const listen = configuredListen(env);
      const configuredUrl = formatListenBase(listen.host, listen.port);
      // $env{SERVER_HOST} is not a client URL until the backend expands it.
      // Normalization failure is a failed probe: start the backend anyway.
      let streamUrl: string | undefined;
      try {
        streamUrl = normalizeRuntimeUrl(configuredUrl, env);
      } catch {
        streamUrl = undefined;
      }
      const probeUrl = streamUrl ? new URL(streamUrl).origin : configuredUrl;
      if (await controlPlaneIsRunning(fetchImpl, probeUrl) && streamUrl) attachUrl = streamUrl;
    }
    const backend = attachUrl ? undefined : await launchBackend({
      configPath: getOptionalArg(args, "--config"),
      envFile: getOptionalArg(args, "--env-file"),
      workspaces: getRepeatedArg(args, "--workspace"),
      user: getOptionalArg(args, "--user"),
      session,
    });
    try {
      const code = await connectTui({
        runtimeUrl: backend?.runtimeUrl ?? attachUrl!,
        fetchImpl,
        renderApp,
        env,
        initialResume: session ? { enabled: true, sessionId: session } : resume,
        instanceId: backend?.instanceId,
        shutdownSignal: backend?.signal,
      });
      if (backend?.signal.aborted) throw backend.signal.reason;
      return code;
    } finally {
      await backend?.stop();
    }
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") return 130;
    console.error(`DataAgent: ${terminalText(error instanceof Error ? error.message : String(error))}`);
    return 1;
  }
}

const isDirectLaunch = process.argv[1]
  ? import.meta.url === pathToFileURL(process.argv[1]).href
  : false;

if (isDirectLaunch) {
  const code = await runTui();
  restoreTerminalForParentShell();
  process.exitCode = code;
  if (process.platform === "win32") {
    // Let ConPTY finish leaving raw mode before this process drops the console.
    await new Promise<void>((done) => setImmediate(done));
  }
}
