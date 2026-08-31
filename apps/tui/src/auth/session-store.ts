import { randomUUID } from "node:crypto";
import { lstat, mkdir, open, readFile, rename, unlink } from "node:fs/promises";
import { dirname, join } from "node:path";
import { homedir, platform } from "node:os";
import type { StoredTuiSession } from "./types.js";

export type SessionStorePlatformEnv = {
  platform?: NodeJS.Platform;
  homedir?: () => string;
  env?: NodeJS.ProcessEnv;
};

type SessionFile = {
  version: 1;
  sessions: Record<string, StoredTuiSession>;
};

type LockPayload = {
  pid: number;
  acquiredAt: number;
  token: string;
};

const LOCK_TIMEOUT_MS = 5_000;
const LOCK_RETRY_MS = 20;
/** Stale lock age after which a holder is assumed dead even if the PID still exists. */
const LOCK_STALE_MS = 10_000;

export function normalizeApiBaseUrl(apiBaseUrl: string): string {
  const url = new URL(apiBaseUrl);
  url.hash = "";
  url.search = "";
  if (url.pathname !== "/" && url.pathname.endsWith("/")) {
    url.pathname = url.pathname.replace(/\/+$/, "");
  }
  if (url.pathname === "/") {
    return `${url.protocol}//${url.host}`;
  }
  return `${url.protocol}//${url.host}${url.pathname}`;
}

export function resolveTuiAuthStorePath(env: SessionStorePlatformEnv = {}): string {
  const osPlatform = env.platform ?? platform();
  const home = (env.homedir ?? homedir)();
  const processEnv = env.env ?? process.env;

  if (osPlatform === "win32") {
    const appData = processEnv.APPDATA?.trim();
    if (!appData) {
      throw new Error("APPDATA is required to locate the TUI auth store on Windows.");
    }
    return join(appData, "DataFoundry", "tui-auth.json");
  }

  if (osPlatform === "darwin") {
    return join(home, "Library", "Application Support", "DataFoundry", "tui-auth.json");
  }

  const xdg = processEnv.XDG_CONFIG_HOME?.trim();
  if (xdg) {
    return join(xdg, "datafoundry", "tui-auth.json");
  }
  return join(home, ".config", "datafoundry", "tui-auth.json");
}

export class TuiSessionStore {
  private readonly filePath: string;
  private readonly platformEnv: SessionStorePlatformEnv;

  constructor(options?: { filePath?: string; platformEnv?: SessionStorePlatformEnv }) {
    this.platformEnv = options?.platformEnv ?? {};
    this.filePath = options?.filePath ?? resolveTuiAuthStorePath(this.platformEnv);
  }

  /** Absolute path of the on-disk session cache (for error messages / retry UX). */
  get path(): string {
    return this.filePath;
  }

  async load(apiBaseUrl: string): Promise<StoredTuiSession | undefined> {
    const key = normalizeApiBaseUrl(apiBaseUrl);
    return this.withFileLock(async () => {
      const file = await this.readOrRepairFile();
      const session = file.sessions[key];
      if (!session) {
        return undefined;
      }
      return {
        ...session,
        apiBaseUrl: key,
      };
    });
  }

  async save(session: StoredTuiSession): Promise<void> {
    await this.withFileLock(async () => {
      const key = normalizeApiBaseUrl(session.apiBaseUrl);
      const file = await this.readOrRepairFile();
      file.sessions[key] = {
        ...session,
        apiBaseUrl: key,
      };
      await this.writeFileAtomic(file);
    });
  }

  async remove(apiBaseUrl: string): Promise<void> {
    await this.withFileLock(async () => {
      const key = normalizeApiBaseUrl(apiBaseUrl);
      const file = await this.readOrRepairFile();
      if (!(key in file.sessions)) {
        return;
      }
      delete file.sessions[key];
      await this.writeFileAtomic(file);
    });
  }

  private async withFileLock<T>(fn: () => Promise<T>): Promise<T> {
    await this.ensureParentDir();
    const lockPath = `${this.filePath}.lock`;
    const started = Date.now();
    const token = randomUUID();

    while (true) {
      try {
        const handle = await open(lockPath, "wx");
        try {
          const payload: LockPayload = {
            pid: process.pid,
            acquiredAt: Date.now(),
            token,
          };
          await handle.writeFile(`${JSON.stringify(payload)}\n`, "utf8");
          await handle.sync();
          return await fn();
        } finally {
          await handle.close();
          await this.releaseLock(lockPath, token);
        }
      } catch (error) {
        if (isErrno(error) && error.code === "EEXIST") {
          const reclaimed = await this.tryReclaimStaleLock(lockPath);
          if (reclaimed) {
            continue;
          }
          if (Date.now() - started > LOCK_TIMEOUT_MS) {
            throw new Error(`Timed out waiting for TUI auth store lock: ${lockPath}`);
          }
          await sleep(LOCK_RETRY_MS);
          continue;
        }
        throw error;
      }
    }
  }

  private async tryReclaimStaleLock(lockPath: string): Promise<boolean> {
    let raw: string;
    try {
      raw = await readFile(lockPath, "utf8");
    } catch (error) {
      if (isErrno(error) && error.code === "ENOENT") {
        return true;
      }
      return false;
    }

    const payload = parseLockPayload(raw);
    if (!payload || !isLockStale(payload)) {
      return false;
    }

    try {
      await unlink(lockPath);
      return true;
    } catch (error) {
      if (isErrno(error) && error.code === "ENOENT") {
        return true;
      }
      return false;
    }
  }

  private async releaseLock(lockPath: string, token: string): Promise<void> {
    let raw: string;
    try {
      raw = await readFile(lockPath, "utf8");
    } catch (error) {
      if (isErrno(error) && error.code === "ENOENT") {
        return;
      }
      throw new Error(
        `Failed to read TUI auth store lock for release: ${lockPath}`,
        { cause: error },
      );
    }

    const payload = parseLockPayload(raw);
    if (payload && payload.token !== token) {
      // Another process reclaimed/replaced the lock; do not unlink.
      return;
    }

    try {
      await unlink(lockPath);
    } catch (error) {
      if (isErrno(error) && error.code === "ENOENT") {
        return;
      }
      throw new Error(
        `Failed to release TUI auth store lock: ${lockPath}`,
        { cause: error },
      );
    }
  }

  private async readOrRepairFile(): Promise<SessionFile> {
    try {
      await this.assertSafePath();
      const raw = await readFile(this.filePath, "utf8");
      const parsed = JSON.parse(raw) as unknown;
      if (!isSessionFile(parsed)) {
        await this.quarantineCorrupt();
        return { version: 1, sessions: {} };
      }
      return parsed;
    } catch (error) {
      if (isErrno(error) && error.code === "ENOENT") {
        return { version: 1, sessions: {} };
      }
      if (error instanceof SyntaxError) {
        await this.quarantineCorrupt();
        return { version: 1, sessions: {} };
      }
      throw error;
    }
  }

  private async writeFileAtomic(file: SessionFile): Promise<void> {
    await this.ensureParentDir();
    await this.assertSafePath({ allowMissing: true });
    const tempPath = `${this.filePath}.${randomUUID()}.tmp`;
    const payload = `${JSON.stringify(file, null, 2)}\n`;
    const handle = await open(tempPath, "w", 0o600);
    try {
      await handle.writeFile(payload, "utf8");
      await handle.sync();
    } finally {
      await handle.close();
    }
    await rename(tempPath, this.filePath);
    await chmodSafe(this.filePath, 0o600);
  }

  private async ensureParentDir(): Promise<void> {
    const dir = dirname(this.filePath);
    await mkdir(dir, { recursive: true, mode: 0o700 });
    await chmodSafe(dir, 0o700);
  }

  /** Must run under {@link withFileLock}. */
  private async quarantineCorrupt(): Promise<void> {
    const stamp = new Date().toISOString().replace(/[:.]/g, "-");
    const corruptPath = `${this.filePath}.${stamp}.corrupt`;
    try {
      await rename(this.filePath, corruptPath);
    } catch (error) {
      if (!(isErrno(error) && error.code === "ENOENT")) {
        // Another process may have moved it; continue to write a clean file.
      }
    }
    await this.writeFileAtomic({ version: 1, sessions: {} });
  }

  private async assertSafePath(options?: { allowMissing?: boolean }): Promise<void> {
    try {
      const info = await lstat(this.filePath);
      if (info.isSymbolicLink() || !info.isFile()) {
        throw new Error("TUI auth store path must be a regular file.");
      }
    } catch (error) {
      if (options?.allowMissing && isErrno(error) && error.code === "ENOENT") {
        return;
      }
      if (isErrno(error) && error.code === "ENOENT") {
        return;
      }
      throw error;
    }
  }
}

function parseLockPayload(raw: string): LockPayload | undefined {
  try {
    const parsed = JSON.parse(raw) as unknown;
    if (!isPlainObject(parsed)) {
      return undefined;
    }
    if (
      typeof parsed.pid !== "number"
      || !Number.isInteger(parsed.pid)
      || parsed.pid <= 0
      || typeof parsed.acquiredAt !== "number"
      || !Number.isFinite(parsed.acquiredAt)
      || typeof parsed.token !== "string"
      || !parsed.token
    ) {
      return undefined;
    }
    return {
      pid: parsed.pid,
      acquiredAt: parsed.acquiredAt,
      token: parsed.token,
    };
  } catch {
    return undefined;
  }
}

function isLockStale(payload: LockPayload): boolean {
  if (Date.now() - payload.acquiredAt > LOCK_STALE_MS) {
    return true;
  }
  return !isProcessAlive(payload.pid);
}

function isProcessAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

function isSessionFile(value: unknown): value is SessionFile {
  if (!isPlainObject(value)) {
    return false;
  }
  if (value.version !== 1 || !isPlainObject(value.sessions)) {
    return false;
  }
  for (const session of Object.values(value.sessions)) {
    if (!isStoredSession(session)) {
      return false;
    }
  }
  return true;
}

function isStoredSession(value: unknown): value is StoredTuiSession {
  if (!isPlainObject(value)) {
    return false;
  }
  if (typeof value.apiBaseUrl !== "string" || !value.apiBaseUrl.trim()) {
    return false;
  }
  if (!isPlainObject(value.cookies)) {
    return false;
  }
  for (const cookieValue of Object.values(value.cookies)) {
    if (typeof cookieValue !== "string") {
      return false;
    }
  }
  if (!isPlainObject(value.user)
    || typeof value.user.id !== "string"
    || !value.user.id.trim()
    || typeof value.user.email !== "string"
    || !value.user.email.trim()) {
    return false;
  }
  if (value.user.displayName !== undefined && typeof value.user.displayName !== "string") {
    return false;
  }
  if (!isPlainObject(value.workspace)
    || typeof value.workspace.id !== "string"
    || !value.workspace.id.trim()) {
    return false;
  }
  if (value.workspace.name !== undefined && typeof value.workspace.name !== "string") {
    return false;
  }
  if (typeof value.expiresAt !== "string" || Number.isNaN(Date.parse(value.expiresAt))) {
    return false;
  }
  return true;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function isErrno(error: unknown): error is NodeJS.ErrnoException {
  return Boolean(error && typeof error === "object" && "code" in error);
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => {
    setTimeout(resolve, ms);
  });
}

async function chmodSafe(path: string, mode: number): Promise<void> {
  if (platform() === "win32") {
    return;
  }
  try {
    const { chmod } = await import("node:fs/promises");
    await chmod(path, mode);
  } catch {
    // best effort on platforms without unix modes
  }
}
