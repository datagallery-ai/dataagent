import { spawn, type ChildProcess } from "node:child_process";
import { platform } from "node:os";

export type BrowserOpenerDeps = {
  platform?: NodeJS.Platform;
  spawn?: typeof spawn;
};

export type OpenBrowserResult =
  | { ok: true; command: string; args: string[]; url: string }
  | { ok: false; reason: string; url: string };

export function buildRegisterUrl(publicBaseUrl: string): string {
  const base = new URL(publicBaseUrl);
  if (base.protocol !== "http:" && base.protocol !== "https:") {
    throw new Error("Registration URL must use http or https.");
  }
  base.pathname = `${base.pathname.replace(/\/?$/, "/")}register`;
  base.search = "";
  base.hash = "";
  return base.toString();
}

export function openBrowserUrl(
  url: string,
  deps: BrowserOpenerDeps = {},
): OpenBrowserResult {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return { ok: false, reason: "Invalid URL", url };
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    return { ok: false, reason: "URL must use http or https", url };
  }

  const osPlatform = deps.platform ?? platform();
  const spawnImpl = deps.spawn ?? spawn;
  const { command, args } = browserCommand(osPlatform, parsed.toString());
  const normalizedUrl = parsed.toString();

  try {
    const child: ChildProcess = spawnImpl(command, args, {
      shell: false,
      detached: true,
      stdio: "ignore",
    });
    // Missing binaries (e.g. xdg-open) emit 'error' asynchronously; without a
    // listener Node can crash the process even though spawn() returned.
    child.on("error", () => {
      // Caller always prints a copyable URL; swallow to avoid unhandled crash.
    });
    child.unref();
    return { ok: true, command, args, url: normalizedUrl };
  } catch (error) {
    const reason = error instanceof Error ? error.message : String(error);
    return { ok: false, reason, url: normalizedUrl };
  }
}

function browserCommand(
  osPlatform: NodeJS.Platform,
  url: string,
): { command: string; args: string[] } {
  if (osPlatform === "win32") {
    return {
      command: "rundll32",
      args: ["url.dll,FileProtocolHandler", url],
    };
  }
  if (osPlatform === "darwin") {
    return { command: "open", args: [url] };
  }
  return { command: "xdg-open", args: [url] };
}
