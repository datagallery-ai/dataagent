import assert from "node:assert/strict";
import { mkdtemp } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, it } from "node:test";
import {
  canHidePasswordEcho,
  createSecurePrompt,
  resolveReadlineTerminal,
  runInteractiveLogin,
  type PromptFn,
} from "./interactive-login.js";
import { TuiAuthError } from "./auth-client.js";
import { TuiSessionStore } from "./session-store.js";
import { PassThrough } from "node:stream";

describe("runInteractiveLogin", () => {
  it("supports login, registration guidance, and exit without echoing passwords", async () => {
    const dir = await mkdtemp(join(tmpdir(), "tui-interactive-"));
    const store = new TuiSessionStore({ filePath: join(dir, "auth.json") });
    const output: string[] = [];
    const passwords: string[] = [];
    const answers = ["2", "", "1", "user@example.com", "secret-password", "3"];
    let answerIndex = 0;
    const prompt: PromptFn = {
      question: async () => answers[answerIndex++] ?? "3",
      password: async () => {
        const value = answers[answerIndex++] ?? "";
        passwords.push(value);
        return value;
      },
      close: () => {},
    };

    let openedUrl: string | undefined;
    const first = await runInteractiveLogin({
      apiBaseUrl: "http://127.0.0.1:8787",
      status: {
        publicBaseUrl: "http://127.0.0.1:3000/app",
        registrationEnabled: true,
      },
      deps: {
        prompt,
        stdout: {
          write(chunk: string) {
            output.push(String(chunk));
            return true;
          },
        } as NodeJS.WritableStream,
        fetchImpl: async (input) => {
          const path = new URL(String(input)).pathname;
          if (path === "/api/v1/auth/login") {
            return json(
              200,
              {
                success: true,
                data: {
                  user: { id: "u1", email: "user@example.com" },
                  workspace: { id: "w1" },
                  session: { expiresAt: "2099-01-01T00:00:00.000Z" },
                },
              },
              ["df_session=s; Path=/", "df_csrf=c; Path=/"],
            );
          }
          return json(404, { success: false, error: { code: "NOT_FOUND", message: "x" } });
        },
        sessionStore: store,
        openBrowser: (url) => {
          openedUrl = url;
          return { ok: true, command: "xdg-open", args: [url], url };
        },
      },
    });

    assert.equal(first.kind, "authenticated");
    assert.equal(openedUrl, "http://127.0.0.1:3000/app/register");
    assert.equal(passwords[0], "secret-password");
    assert.equal(output.join("").includes("secret-password"), false);

    const exitResult = await runInteractiveLogin({
      apiBaseUrl: "http://127.0.0.1:8787",
      status: { publicBaseUrl: "http://127.0.0.1:3000", registrationEnabled: false },
      deps: {
        prompt: {
          question: async () => "3",
          password: async () => "",
          close: () => {},
        },
        stdout: { write: () => true } as unknown as NodeJS.WritableStream,
        fetchImpl: async () => json(500, {}),
        sessionStore: store,
      },
    });
    assert.equal(exitResult.kind, "exit");
  });

  it("does not auto-loop login after rate limiting", async () => {
    const dir = await mkdtemp(join(tmpdir(), "tui-rate-"));
    const store = new TuiSessionStore({ filePath: join(dir, "auth.json") });
    let loginCalls = 0;
    const answers = ["1", "user@example.com", "pw", "3"];
    let idx = 0;
    const result = await runInteractiveLogin({
      apiBaseUrl: "http://127.0.0.1:8787",
      status: { publicBaseUrl: "http://127.0.0.1:3000", registrationEnabled: false },
      deps: {
        prompt: {
          question: async () => answers[idx++] ?? "3",
          password: async () => answers[idx++] ?? "",
          close: () => {},
        },
        stdout: { write: () => true } as unknown as NodeJS.WritableStream,
        sessionStore: store,
        fetchImpl: async () => {
          loginCalls += 1;
          return json(429, {
            success: false,
            error: { code: "RATE_LIMITED", message: "Too many requests" },
          });
        },
      },
    });
    assert.equal(result.kind, "exit");
    assert.equal(loginCalls, 1);
  });

  it("createSecurePrompt rejects overlapping readers on the same stdin", async () => {
    const stdin = new PassThrough();
    const stdoutChunks: string[] = [];
    const stdout = new PassThrough();
    stdout.on("data", (chunk) => stdoutChunks.push(String(chunk)));
    const prompt = createSecurePrompt({ stdin, stdout });

    const first = prompt.question("Q1: ");
    await assert.rejects(() => prompt.question("Q2: "), /already waiting/i);
    stdin.write("one\n");
    assert.equal(await first, "one");
    await prompt.close();
  });

  it("uses stdin TTY (not stdout) for terminal mode under piped stdout", () => {
    const stdin = Object.assign(new PassThrough(), {
      isTTY: true,
      isRaw: false,
      setRawMode(mode: boolean) {
        this.isRaw = mode;
      },
    });
    const stdout = Object.assign(new PassThrough(), { isTTY: false });
    assert.equal(resolveReadlineTerminal(stdin), true);
    assert.equal(Boolean((stdout as { isTTY?: boolean }).isTTY), false);
    assert.equal(canHidePasswordEcho(stdin), true);
  });

  it("refuses password when stdin cannot hide echo", async () => {
    const stdin = new PassThrough(); // not a TTY
    const stdout = Object.assign(new PassThrough(), { isTTY: true });
    const prompt = createSecurePrompt({ stdin, stdout });
    await assert.rejects(() => prompt.password("Password: "), /hide echo/i);
    await prompt.close();
  });

  it("reads password with stdin TTY + piped stdout without echoing to stdout", async () => {
    const modes: boolean[] = [];
    const stdin = Object.assign(new PassThrough(), {
      isTTY: true,
      isRaw: false,
      setRawMode(mode: boolean) {
        this.isRaw = mode;
        modes.push(mode);
      },
    });
    const stdoutChunks: string[] = [];
    const stdout = Object.assign(new PassThrough(), { isTTY: false });
    stdout.on("data", (chunk) => stdoutChunks.push(String(chunk)));
    const prompt = createSecurePrompt({ stdin, stdout });

    const pending = prompt.password("Password: ");
    // Allow readline to attach before writing.
    await new Promise((resolve) => setImmediate(resolve));
    stdin.write("s3cret\n");
    assert.equal(await pending, "s3cret");
    assert.equal(stdoutChunks.join("").includes("s3cret"), false);
    assert.ok(modes.includes(true));
    assert.equal(stdin.isRaw, false);
    await prompt.close();
  });

  it("returns unreachable on login network/timeout errors", async () => {
    const dir = await mkdtemp(join(tmpdir(), "tui-unreachable-"));
    const store = new TuiSessionStore({ filePath: join(dir, "auth.json") });
    const answers = ["1", "user@example.com", "pw"];
    let idx = 0;
    const result = await runInteractiveLogin({
      apiBaseUrl: "http://127.0.0.1:8787",
      status: { publicBaseUrl: "http://127.0.0.1:3000", registrationEnabled: false },
      deps: {
        prompt: {
          question: async () => answers[idx++] ?? "3",
          password: async () => answers[idx++] ?? "",
          close: () => {},
        },
        stdout: { write: () => true } as unknown as NodeJS.WritableStream,
        sessionStore: store,
        fetchImpl: async () => {
          throw new TuiAuthError(0, "TIMEOUT", "Auth request timed out after 15ms");
        },
      },
    });
    assert.equal(result.kind, "unreachable");
  });
});

function json(status: number, body: unknown, setCookies: string[] = []): Response {
  const headers = new Headers({ "content-type": "application/json" });
  for (const cookie of setCookies) {
    headers.append("set-cookie", cookie);
  }
  return new Response(JSON.stringify(body), { status, headers });
}
