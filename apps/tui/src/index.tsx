#!/usr/bin/env node
import { render } from "ink";
import { randomUUID } from "node:crypto";
import { pathToFileURL } from "node:url";
import React from "react";
import {
  bindTransportAuthRequired,
  bootstrapTuiAuth,
  createAuthController,
  createSecurePrompt,
  runInteractiveLogin,
  type AppExitReason,
  type AuthCommandController,
  type AuthenticatedTransport,
} from "./auth/index.js";
import { ConfigClient } from "./config/index.js";
import { CopilotKitClient } from "./protocol/copilotkit-client.js";
import { store } from "./state/store.js";
import { validateRuntimeUrl } from "./runtime-url.js";
import {
  configBaseUrlFromRuntime,
  preflightDefaultDatasourceId,
  preflightRuntimeConnection,
} from "./startup-preflight.js";
import { installTerminalRedrawOptimizer } from "./terminal-redraw-optimizer.js";
import { withAlternateScreen } from "./terminal-screen.js";
import { App } from "./ui/App.js";
import { themeManager } from "./ui/themes/theme-manager.js";

export type RunTuiOptions = {
  argv?: string[];
  fetchImpl?: typeof fetch;
  stdout?: NodeJS.WritableStream;
  stdin?: NodeJS.ReadableStream;
  prompt?: import("./auth/interactive-login.js").PromptFn;
  sessionStore?: import("./auth/session-store.js").TuiSessionStore;
  renderApp?: typeof renderAuthenticatedApp;
};

async function renderAuthenticatedApp(options: {
  client: CopilotKitClient;
  configClient: ConfigClient;
  datasourceId: string | undefined;
  initialDatasourceId: string | undefined;
  initialResume?: { enabled: boolean; sessionId?: string | undefined };
  authController: AuthCommandController;
  transport?: AuthenticatedTransport;
  onExit: (reason: AppExitReason) => void;
}): Promise<AppExitReason> {
  let exitReason: AppExitReason = "exit";
  const restoreTerminalRedrawOptimizer = process.stdout.isTTY
    ? installTerminalRedrawOptimizer(process.stdout)
    : () => {};
  let unbindAuthRequired: (() => void) | undefined;

  try {
    await withAlternateScreen(async () => {
      const instance = render(
        React.createElement(App, {
          client: options.client,
          configClient: options.configClient,
          datasourceId: options.datasourceId,
          initialDatasourceId: options.initialDatasourceId,
          authController: options.authController,
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
        },
      );
      if (options.transport) {
        unbindAuthRequired = bindTransportAuthRequired(options.transport, () => {
          if (exitReason === "exit") {
            exitReason = "auth-required";
          }
          instance.unmount();
        });
      }
      await instance.waitUntilExit();
    });
  } finally {
    unbindAuthRequired?.();
    restoreTerminalRedrawOptimizer();
  }

  return exitReason;
}

function printHelp(): void {
  console.log(`
DataFoundry TUI - Terminal User Interface for DataFoundry

Usage:
  datafoundry-tui [options]

Options:
  --runtime-url <url>     CopilotKit runtime URL
                          (default: http://127.0.0.1:8787/api/copilotkit)
  --datasource-id <id>    Datasource ID
                          (default: backend run-defaults)
  --agent <name>          Agent name
                          (default: dataFoundry)
  --theme <name>          TUI color theme
                          (mist-dark or legacy-dark; env: DATAFOUNDRY_TUI_THEME)
  --resume [sessionId]    Resume the latest server session, or a specific session
  --no-auto-login         Ignore cached session and show the login menu
  --help, -h              Show this help message

Examples:
  datafoundry-tui
  datafoundry-tui --runtime-url http://localhost:8787/api/copilotkit
  datafoundry-tui --datasource-id my-database
  datafoundry-tui --theme mist-dark
  datafoundry-tui --resume
  datafoundry-tui --resume thread-001
  datafoundry-tui --no-auto-login

Notes:
  Session cache keys treat localhost, 127.0.0.1, and ::1 as different API endpoints.
  Keep --runtime-url consistent with the address you used when signing in.
`);
}

function getArg(args: string[], name: string, defaultValue: string): string {
  const index = args.indexOf(name);
  if (index !== -1 && index + 1 < args.length) {
    return args[index + 1]!;
  }
  return defaultValue;
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
  if (args.includes("--resume")) {
    return {
      enabled: true,
      ...(getOptionalArg(args, "--resume")
        ? { sessionId: getOptionalArg(args, "--resume") }
        : {}),
    };
  }
  const explicit = getOptionalArg(args, "--resume-session");
  return explicit ? { enabled: true, sessionId: explicit } : undefined;
}

export async function runTui(options: RunTuiOptions = {}): Promise<number> {
  const args = options.argv ?? process.argv.slice(2);
  const fetchImpl = options.fetchImpl ?? globalThis.fetch.bind(globalThis);
  const renderApp = options.renderApp ?? renderAuthenticatedApp;

  if (args.includes("--help") || args.includes("-h")) {
    printHelp();
    return 0;
  }

  const legacyOfflineDemoFlag = `--${"demo"}`;
  if (args.includes(legacyOfflineDemoFlag)) {
    console.error(
      "Offline demo mode has been removed. Sign in with a password account against a running API.",
    );
    return 1;
  }

  const requestedTheme = getOptionalArg(args, "--theme") ?? process.env.DATAFOUNDRY_TUI_THEME;
  if (requestedTheme && !themeManager.setActiveTheme(requestedTheme)) {
    const availableThemes = themeManager.getAvailableThemes().map((theme) => theme.name).join(", ");
    console.error(`Unknown TUI theme "${requestedTheme}". Available themes: ${availableThemes}.`);
    return 1;
  }

  const initialRuntime = validateRuntimeUrl(
    getArg(args, "--runtime-url", "http://127.0.0.1:8787/api/copilotkit"),
  );
  if (!initialRuntime.ok) {
    console.error(initialRuntime.reason);
    return 1;
  }
  let runtimeUrl = initialRuntime.url;
  let configBaseUrl = configBaseUrlFromRuntime(runtimeUrl);
  const explicitDatasourceId = getOptionalArg(args, "--datasource-id");
  const agent = getArg(args, "--agent", "dataFoundry");
  const noAutoLogin = args.includes("--no-auto-login");
  const initialResume = resolveResumeRequest(args);

  const stdout = options.stdout ?? process.stdout;
  const stdin = options.stdin ?? process.stdin;

  while (true) {
    let bootstrap: Awaited<ReturnType<typeof bootstrapTuiAuth>>;
    try {
      bootstrap = await bootstrapTuiAuth({
        apiBaseUrl: configBaseUrl,
        noAutoLogin,
        fetchImpl,
        ...(options.sessionStore ? { sessionStore: options.sessionStore } : {}),
      });
    } catch (error) {
      const recovered = await promptApiUnreachableRecovery({
        apiBaseUrl: configBaseUrl,
        error,
        stdout,
        stdin,
        ...(options.prompt ? { prompt: options.prompt } : {}),
      });
      if (recovered.action === "quit") {
        return 1;
      }
      if (recovered.action === "other-address") {
        runtimeUrl = recovered.runtimeUrl;
        configBaseUrl = configBaseUrlFromRuntime(runtimeUrl);
      }
      continue;
    }

    if (bootstrap.kind === "login-required") {
      const interactive = await runInteractiveLogin({
        apiBaseUrl: configBaseUrl,
        status: bootstrap.status,
        ...(bootstrap.previousSession
          ? { previousSession: bootstrap.previousSession }
          : {}),
        deps: {
          fetchImpl,
          sessionStore: bootstrap.sessionStore,
          ...(options.stdout ? { stdout: options.stdout } : {}),
          ...(options.stdin ? { stdin: options.stdin } : {}),
          ...(options.prompt ? { prompt: options.prompt } : {}),
        },
      });
      if (interactive.kind === "exit") {
        return 0;
      }
      if (interactive.kind === "unreachable") {
        const recovered = await promptApiUnreachableRecovery({
          apiBaseUrl: configBaseUrl,
          error: interactive.error,
          stdout,
          stdin,
          ...(options.prompt ? { prompt: options.prompt } : {}),
        });
        if (recovered.action === "quit") {
          return 1;
        }
        if (recovered.action === "other-address") {
          runtimeUrl = recovered.runtimeUrl;
          configBaseUrl = configBaseUrlFromRuntime(runtimeUrl);
        }
        continue;
      }
      bootstrap = {
        kind: "authenticated",
        session: interactive.session,
        transport: interactive.transport,
        authClient: interactive.authClient,
        cookieJar: interactive.cookieJar,
        sessionStore: bootstrap.sessionStore,
        ...(interactive.warning ? { warning: interactive.warning } : {}),
      };
    }

    if (bootstrap.kind !== "authenticated") {
      return 1;
    }

    const transportFetch = bootstrap.transport.fetch.bind(bootstrap.transport);
    const authController = createAuthController({
      apiBaseUrl: configBaseUrl,
      authClient: bootstrap.authClient,
      sessionStore: bootstrap.sessionStore,
    });

    const configClient = new ConfigClient({
      baseUrl: configBaseUrl,
      fetchImpl: transportFetch,
    });
    const client = new CopilotKitClient({
      runtimeUrl,
      agent,
      fetchImpl: transportFetch,
      onConnectionStatusChange: (status) => {
        store.setConnectionStatus(status === "reconnecting" ? "disconnected" : status);
      },
    });

    let initialDatasourceId = explicitDatasourceId;
    const [runtimeConnected, preflightDatasourceId] = await Promise.all([
      preflightRuntimeConnection(runtimeUrl, transportFetch),
      !initialDatasourceId
        ? preflightDefaultDatasourceId(configBaseUrl, transportFetch)
        : Promise.resolve(undefined),
    ]);

    if (!initialDatasourceId && preflightDatasourceId) {
      initialDatasourceId = preflightDatasourceId;
    }

    store.setConnectionStatus(runtimeConnected ? "connected" : "disconnected");
    if (!initialResume?.enabled) {
      store.setThreadId(randomUUID());
    }

    if (bootstrap.warning) {
      console.log(bootstrap.warning);
    }
    console.log(
      `Authenticated as ${bootstrap.session.user.email} (${bootstrap.session.workspace.id})`,
    );

    const exitReason = await renderApp({
      client,
      configClient,
      datasourceId: explicitDatasourceId,
      initialDatasourceId,
      authController,
      transport: bootstrap.transport,
      onExit: () => {},
      ...(initialResume ? { initialResume } : {}),
    });

    if ("dispose" in client && typeof client.dispose === "function") {
      client.dispose();
    }

    if (exitReason === "logout" || exitReason === "auth-required") {
      store.reset();
      if (exitReason === "auth-required") {
        console.log("Session expired or revoked. Please sign in again.");
      }
      // Re-enter auth menu; force interactive login for account switching clarity.
      continue;
    }
    return 0;
  }
}

type ApiUnreachableRecovery =
  | { action: "retry" }
  | { action: "quit" }
  | { action: "other-address"; runtimeUrl: string };

async function promptApiUnreachableRecovery(options: {
  apiBaseUrl: string;
  error: unknown;
  stdout: NodeJS.WritableStream;
  stdin: NodeJS.ReadableStream;
  prompt?: import("./auth/interactive-login.js").PromptFn;
}): Promise<ApiUnreachableRecovery> {
  const message = errorMessage(options.error);
  options.stdout.write(
    `Cannot reach API at ${options.apiBaseUrl}: ${message}\n`
      + "Options: [r]etry, [o]ther address, [q]uit\n",
  );

  const ownsPrompt = !options.prompt;
  const prompt = options.prompt ?? createSecurePrompt({
    stdin: options.stdin,
    stdout: options.stdout,
  });
  try {
    while (true) {
      const answer = (await prompt.question("Select an option: ")).trim().toLowerCase();
      if (answer === "r" || answer === "retry" || answer === "") {
        return { action: "retry" };
      }
      if (answer === "o" || answer === "other" || answer === "address") {
        const runtimeUrl = await promptRuntimeUrl(prompt, options.stdout);
        if (runtimeUrl) {
          return { action: "other-address", runtimeUrl };
        }
        continue;
      }
      if (answer === "q" || answer === "quit" || answer === "exit") {
        return { action: "quit" };
      }
      options.stdout.write("Please choose [r]etry, [o]ther address, or [q]uit.\n");
    }
  } finally {
    if (ownsPrompt) {
      await prompt.close();
    }
  }
}

async function promptRuntimeUrl(
  prompt: import("./auth/interactive-login.js").PromptFn,
  stdout: NodeJS.WritableStream,
): Promise<string | undefined> {
  while (true) {
    const raw = (await prompt.question("Enter runtime URL (empty to cancel): ")).trim();
    if (!raw) {
      return undefined;
    }
    const validated = validateRuntimeUrl(raw);
    if (validated.ok) {
      return validated.url;
    }
    stdout.write(`${validated.reason}\n`);
  }
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

const isDirectLaunch = process.argv[1]
  ? import.meta.url === pathToFileURL(process.argv[1]).href
  : false;

if (isDirectLaunch) {
  const code = await runTui();
  process.exitCode = code;
}
