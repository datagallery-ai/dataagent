import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { describe, it } from "node:test";
import { buildRegisterUrl, openBrowserUrl } from "./browser-opener.js";

describe("buildRegisterUrl", () => {
  it("keeps deployment path prefix from publicBaseUrl", () => {
    assert.equal(
      buildRegisterUrl("https://example.com/deploy"),
      "https://example.com/deploy/register",
    );
    assert.equal(
      buildRegisterUrl("https://example.com/deploy/"),
      "https://example.com/deploy/register",
    );
  });
});

describe("openBrowserUrl", () => {
  it("uses rundll32 argument array on Windows without shell", () => {
    const calls: Array<{ command: string; args: string[]; options: object }> = [];
    const result = openBrowserUrl("http://127.0.0.1:3000/register", {
      platform: "win32",
      spawn: ((command: string, args: string[], options: object) => {
        calls.push({ command, args, options });
        return fakeChild();
      }) as typeof import("node:child_process").spawn,
    });
    assert.equal(result.ok, true);
    assert.deepEqual(calls[0]?.command, "rundll32");
    assert.deepEqual(calls[0]?.args, [
      "url.dll,FileProtocolHandler",
      "http://127.0.0.1:3000/register",
    ]);
    assert.equal((calls[0]?.options as { shell?: boolean }).shell, false);
  });

  it("uses open on macOS and xdg-open on Linux", () => {
    const mac = captureSpawn("darwin", "https://example.com/register");
    assert.deepEqual(mac, { command: "open", args: ["https://example.com/register"] });
    const linux = captureSpawn("linux", "https://example.com/register");
    assert.deepEqual(linux, { command: "xdg-open", args: ["https://example.com/register"] });
  });

  it("returns the full URL when spawn fails", () => {
    const result = openBrowserUrl("https://example.com/app/register", {
      platform: "linux",
      spawn: (() => {
        throw new Error("spawn failed");
      }) as typeof import("node:child_process").spawn,
    });
    assert.equal(result.ok, false);
    if (!result.ok) {
      assert.equal(result.url, "https://example.com/app/register");
    }
  });

  it("attaches an error listener so async spawn failures do not crash", () => {
    let errorListener: ((err: Error) => void) | undefined;
    const result = openBrowserUrl("https://example.com/register", {
      platform: "linux",
      spawn: ((() => {
        const child = fakeChild();
        const originalOn = child.on.bind(child);
        child.on = ((event: string, listener: (...args: unknown[]) => void) => {
          if (event === "error") {
            errorListener = listener as (err: Error) => void;
          }
          return originalOn(event, listener);
        }) as typeof child.on;
        return child;
      }) as unknown) as typeof import("node:child_process").spawn,
    });
    assert.equal(result.ok, true);
    if (result.ok) {
      assert.equal(result.url, "https://example.com/register");
    }
    assert.equal(typeof errorListener, "function");
    assert.doesNotThrow(() => errorListener?.(new Error("ENOENT")));
  });
});

function captureSpawn(osPlatform: NodeJS.Platform, url: string) {
  let captured: { command: string; args: string[] } | undefined;
  openBrowserUrl(url, {
    platform: osPlatform,
    spawn: ((command: string, args: string[]) => {
      captured = { command, args };
      return fakeChild();
    }) as typeof import("node:child_process").spawn,
  });
  return captured;
}

function fakeChild() {
  const child = new EventEmitter() as EventEmitter & {
    unref: () => void;
    stdin: null;
    stdout: null;
    stderr: null;
    pid: number;
  };
  child.unref = () => {};
  child.stdin = null;
  child.stdout = null;
  child.stderr = null;
  child.pid = 1;
  return child;
}
