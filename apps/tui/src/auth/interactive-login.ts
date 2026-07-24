import { createInterface, type Interface } from "node:readline/promises";
import { Writable } from "node:stream";
import { completeInteractiveLogin } from "./bootstrap.js";
import { buildRegisterUrl, openBrowserUrl, type BrowserOpenerDeps } from "./browser-opener.js";
import { TuiAuthError } from "./auth-client.js";
import type { TuiSessionStore } from "./session-store.js";
import type { AuthStatus, StoredTuiSession } from "./types.js";
import type { AuthenticatedTransport } from "./authenticated-transport.js";
import type { TuiAuthClient } from "./auth-client.js";
import type { TuiCookieJar } from "./cookie-jar.js";

export type PromptFn = {
  question(message: string): Promise<string>;
  password(message: string): Promise<string>;
  close(): Promise<void> | void;
};

export type InteractiveLoginDeps = {
  prompt?: PromptFn;
  stdout?: NodeJS.WritableStream;
  stdin?: NodeJS.ReadableStream;
  fetchImpl: typeof fetch;
  sessionStore: TuiSessionStore;
  browser?: BrowserOpenerDeps;
  openBrowser?: typeof openBrowserUrl;
};

export type InteractiveLoginSuccess = {
  kind: "authenticated";
  session: StoredTuiSession;
  transport: AuthenticatedTransport;
  authClient: TuiAuthClient;
  cookieJar: TuiCookieJar;
  warning?: string;
};

export type InteractiveLoginResult =
  | InteractiveLoginSuccess
  | { kind: "exit" }
  | { kind: "unreachable"; error: unknown };

export async function runInteractiveLogin(options: {
  apiBaseUrl: string;
  status: AuthStatus;
  previousSession?: StoredTuiSession;
  deps: InteractiveLoginDeps;
}): Promise<InteractiveLoginResult> {
  const stdout = options.deps.stdout ?? process.stdout;
  const prompt = options.deps.prompt ?? createSecurePrompt({
    stdin: options.deps.stdin ?? process.stdin,
    stdout,
  });
  const open = options.deps.openBrowser ?? openBrowserUrl;

  try {
    while (true) {
      writeLine(stdout, "");
      writeLine(stdout, "DataFoundry TUI login");
      writeLine(stdout, "  [1] Sign in with email and password");
      if (options.status.registrationEnabled) {
        writeLine(stdout, "  [2] Open web registration");
      }
      writeLine(stdout, "  [3] Exit");
      const choice = (await prompt.question("Select an option: ")).trim();

      if (choice === "3" || choice.toLowerCase() === "q" || choice.toLowerCase() === "exit") {
        return { kind: "exit" };
      }

      if (choice === "2" && options.status.registrationEnabled) {
        const registerUrl = buildRegisterUrl(options.status.publicBaseUrl);
        const opened = open(registerUrl, options.deps.browser);
        // Always print a copyable URL: spawn success does not guarantee a browser opened.
        if (opened.ok) {
          writeLine(stdout, `Opened registration page. If the browser did not open, visit:\n${registerUrl}`);
        } else {
          writeLine(stdout, `Could not open a browser (${opened.reason}). Visit:\n${registerUrl}`);
        }
        await prompt.question("Press Enter to return to login...");
        continue;
      }

      if (choice !== "1") {
        writeLine(stdout, "Please choose a valid option.");
        continue;
      }

      const email = (await prompt.question("Email: ")).trim();
      const password = await prompt.password("Password: ");
      if (!email || !password) {
        writeLine(stdout, "Email and password are required.");
        continue;
      }

      try {
        const result = await completeInteractiveLogin({
          apiBaseUrl: options.apiBaseUrl,
          email,
          password,
          fetchImpl: options.deps.fetchImpl,
          sessionStore: options.deps.sessionStore,
          ...(options.previousSession ? { previousSession: options.previousSession } : {}),
        });
        if (result.warning) {
          writeLine(stdout, result.warning);
        }
        writeLine(stdout, `Signed in as ${result.session.user.email}`);
        return {
          kind: "authenticated",
          session: result.session,
          transport: result.transport,
          authClient: result.authClient,
          cookieJar: result.cookieJar,
          ...(result.warning ? { warning: result.warning } : {}),
        };
      } catch (error) {
        if (isUnreachableAuthError(error)) {
          return { kind: "unreachable", error };
        }
        if (error instanceof TuiAuthError && error.status === 429) {
          writeLine(stdout, "Too many login attempts. Wait a moment and try again.");
          continue;
        }
        const message = error instanceof Error ? error.message : String(error);
        writeLine(stdout, `Login failed: ${sanitizeErrorMessage(message)}`);
      }
    }
  } finally {
    await prompt.close();
  }
}

/**
 * Prefer stdin TTY for readline `terminal` mode. Using stdout.isTTY breaks
 * password echo suppression under `tui | tee` (stdin TTY, stdout pipe).
 */
export function resolveReadlineTerminal(stdin: NodeJS.ReadableStream): boolean {
  return Boolean((stdin as NodeJS.ReadStream).isTTY);
}

/** Password entry requires an stdin TTY that can disable echo via setRawMode. */
export function canHidePasswordEcho(stdin: NodeJS.ReadableStream): boolean {
  const stream = stdin as NodeJS.ReadStream;
  return Boolean(stream.isTTY && typeof stream.setRawMode === "function");
}

/**
 * One readline at a time on stdin. Dual visible+muted interfaces can leave the
 * visible reader echoing while the muted reader collects a password.
 */
export function createSecurePrompt(options: {
  stdin: NodeJS.ReadableStream;
  stdout: NodeJS.WritableStream;
}): PromptFn {
  const stdin = options.stdin as NodeJS.ReadStream;
  const terminal = resolveReadlineTerminal(options.stdin);
  const mutedStdout = new Writable({
    write(chunk, _encoding, callback) {
      const text = String(chunk);
      if (text.includes("\n") || text.includes("\r")) {
        options.stdout.write(text.replace(/[^\r\n]/g, ""));
      }
      callback();
    },
  });

  let active: Interface | undefined;

  const withReader = async <T>(
    output: NodeJS.WritableStream,
    historySize: number | undefined,
    run: (rl: Interface) => Promise<T>,
  ): Promise<T> => {
    if (active) {
      throw new Error("Prompt is already waiting for input.");
    }
    const rl = createInterface({
      input: options.stdin as NodeJS.ReadableStream,
      output,
      terminal,
      ...(historySize !== undefined ? { historySize } : {}),
    });
    active = rl;
    try {
      return await run(rl);
    } finally {
      rl.close();
      if (active === rl) {
        active = undefined;
      }
    }
  };

  return {
    question: (message) => withReader(options.stdout, undefined, (rl) => rl.question(message)),
    password: async (message) => {
      if (!canHidePasswordEcho(options.stdin)) {
        throw new Error(
          "Password input requires an interactive stdin TTY that can hide echo. "
            + "Refusing to read a password that would be visible.",
        );
      }
      options.stdout.write(message);
      const previousRaw = Boolean(stdin.isRaw);
      stdin.setRawMode(true);
      try {
        const value = await withReader(mutedStdout, 0, (rl) => rl.question(""));
        options.stdout.write("\n");
        return value;
      } finally {
        stdin.setRawMode(previousRaw);
      }
    },
    close: () => {
      active?.close();
      active = undefined;
    },
  };
}

export function isUnreachableAuthError(error: unknown): boolean {
  if (error instanceof TuiAuthError) {
    return error.code === "TIMEOUT" || error.code === "NETWORK_ERROR";
  }
  if (!(error instanceof Error)) {
    return false;
  }
  if (error.name === "AbortError") {
    return true;
  }
  const message = error.message.toLowerCase();
  return (
    message.includes("fetch failed")
    || message.includes("network")
    || message.includes("econnrefused")
    || message.includes("enotfound")
    || message.includes("etimedout")
    || message.includes("econnreset")
    || message.includes("socket hang up")
  );
}

function writeLine(stdout: NodeJS.WritableStream, message: string): void {
  stdout.write(`${message}\n`);
}

function sanitizeErrorMessage(message: string): string {
  return message
    .replace(/df_session=[^;\s]+/gi, "df_session=[redacted]")
    .replace(/df_csrf=[^;\s]+/gi, "df_csrf=[redacted]");
}
