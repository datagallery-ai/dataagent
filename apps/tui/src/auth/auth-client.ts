import { isLoopbackHostname } from "../runtime-url.js";
import { TuiCookieJar } from "./cookie-jar.js";
import { normalizeApiBaseUrl } from "./session-store.js";
import type { AuthStatus, StoredTuiSession, TuiUser, TuiWorkspace } from "./types.js";

export const DEFAULT_AUTH_TIMEOUT_MS = 15_000;
const MAX_AUTH_REDIRECTS = 5;
const REDIRECT_STATUSES = new Set([301, 302, 303, 307, 308]);

export type TuiAuthClientOptions = {
  apiBaseUrl: string;
  cookieJar: TuiCookieJar;
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
};

export class TuiAuthError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, code: string, message: string) {
    super(message);
    this.name = "TuiAuthError";
    this.status = status;
    this.code = code;
  }
}

export class TuiAuthClient {
  readonly apiBaseUrl: string;
  readonly cookieJar: TuiCookieJar;
  readonly fetchImpl: typeof fetch;
  readonly timeoutMs: number;

  constructor(options: TuiAuthClientOptions) {
    this.apiBaseUrl = normalizeApiBaseUrl(options.apiBaseUrl);
    this.cookieJar = options.cookieJar;
    this.fetchImpl = options.fetchImpl ?? globalThis.fetch.bind(globalThis);
    this.timeoutMs = options.timeoutMs ?? DEFAULT_AUTH_TIMEOUT_MS;
  }

  async getStatus(): Promise<AuthStatus> {
    const body = await this.requestJson("GET", "/api/v1/auth/status");
    const data = asRecord(body.data);
    const publicBaseUrl = asString(data?.publicBaseUrl);
    if (!publicBaseUrl) {
      throw new TuiAuthError(500, "INVALID_STATUS", "Auth status missing publicBaseUrl.");
    }
    return {
      publicBaseUrl,
      registrationEnabled: Boolean(data?.registrationEnabled),
    };
  }

  async login(email: string, password: string): Promise<StoredTuiSession> {
    const { response, json: body } = await this.requestRaw("POST", "/api/v1/auth/login", {
      email,
      password,
      client: "tui",
    });
    if (!response.ok) {
      throw errorFromBody(response.status, body);
    }

    const data = asRecord(body.data);
    const user = parseUser(data?.user);
    const workspace = parseWorkspace(data?.workspace);
    const session = asRecord(data?.session);
    const expiresAt = asString(session?.expiresAt);
    if (!expiresAt || Number.isNaN(Date.parse(expiresAt))) {
      throw new TuiAuthError(
        500,
        "INVALID_LOGIN_RESPONSE",
        "Login response missing session.expiresAt.",
      );
    }

    return {
      apiBaseUrl: this.apiBaseUrl,
      cookies: this.cookieJar.snapshot(),
      user,
      workspace,
      expiresAt,
    };
  }

  async me(): Promise<TuiUser & { workspace: TuiWorkspace }> {
    const body = await this.requestJson("GET", "/api/v1/me");
    const data = asRecord(body.data);
    const user = parseUser(data?.user);
    const workspace = parseWorkspace(data?.workspace);
    return { ...user, workspace };
  }

  async refreshCsrf(): Promise<void> {
    const { response, json } = await this.requestRaw("POST", "/api/v1/auth/csrf/refresh");
    if (!response.ok) {
      throw errorFromBody(response.status, json);
    }
  }

  async logout(): Promise<void> {
    const { response, json } = await this.requestRaw("POST", "/api/v1/auth/logout");
    if (!response.ok && response.status !== 401) {
      throw errorFromBody(response.status, json);
    }
    this.cookieJar.clear();
  }

  private async requestJson(
    method: string,
    path: string,
    body?: unknown,
  ): Promise<{ data?: unknown; error?: { code?: string; message?: string } }> {
    const { response, json } = await this.requestRaw(method, path, body);
    if (!response.ok) {
      throw errorFromBody(response.status, json);
    }
    return json;
  }

  /**
   * Fetch + JSON parse under one AbortController so timeouts cover the full body read.
   * Auth requests never auto-follow redirects; hops are validated manually.
   */
  private async requestRaw(
    method: string,
    path: string,
    body?: unknown,
  ): Promise<{
    response: Response;
    json: { data?: unknown; error?: { code?: string; message?: string } };
  }> {
    return this.withTimeout(async (signal) => {
      const response = await this.performFetch(method, path, body, signal);
      this.cookieJar.absorbSetCookie(response.headers);
      const json = await readJson(response);
      return { response, json };
    });
  }

  private async withTimeout<T>(fn: (signal: AbortSignal) => Promise<T>): Promise<T> {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), this.timeoutMs);
    try {
      return await fn(controller.signal);
    } catch (error) {
      if (error instanceof TuiAuthError) {
        throw error;
      }
      if (isAbortError(error)) {
        throw new TuiAuthError(
          0,
          "TIMEOUT",
          `Auth request timed out after ${this.timeoutMs}ms`,
        );
      }
      const message = error instanceof Error ? error.message : String(error);
      throw new TuiAuthError(0, "NETWORK_ERROR", message || "Network request failed");
    } finally {
      clearTimeout(timeoutId);
    }
  }

  private async performFetch(
    method: string,
    path: string,
    body: unknown | undefined,
    signal: AbortSignal,
  ): Promise<Response> {
    let url = resolveApiUrl(this.apiBaseUrl, path);
    let currentMethod = method;
    let currentBody = body;
    for (let hop = 0; hop <= MAX_AUTH_REDIRECTS; hop += 1) {
      const headers = new Headers();
      const cookieHeader = this.cookieJar.headerValue();
      if (cookieHeader) {
        headers.set("cookie", cookieHeader);
      }
      if (currentMethod !== "GET" && currentMethod !== "HEAD") {
        headers.set("content-type", "application/json");
        const csrf = this.cookieJar.csrfToken();
        if (csrf) {
          headers.set("x-csrf-token", csrf);
        }
      }

      const response = await this.fetchImpl(url, {
        method: currentMethod,
        headers,
        signal,
        redirect: "manual",
        ...(currentBody !== undefined ? { body: JSON.stringify(currentBody) } : {}),
      });

      if (!REDIRECT_STATUSES.has(response.status)) {
        return response;
      }

      if (hop === MAX_AUTH_REDIRECTS) {
        throw new TuiAuthError(
          response.status,
          "UNSAFE_REDIRECT",
          "Auth request exceeded redirect hop limit.",
        );
      }

      const nextUrl = assertSafeAuthRedirect(url, response.headers.get("location"));
      // Absorb cookies from redirect responses before following.
      this.cookieJar.absorbSetCookie(response.headers);

      if (response.status === 301 || response.status === 302 || response.status === 303) {
        if (currentMethod !== "GET" && currentMethod !== "HEAD") {
          throw new TuiAuthError(
            response.status,
            "UNSAFE_REDIRECT",
            "Refusing method-changing auth redirect for non-GET request.",
          );
        }
        currentMethod = "GET";
        currentBody = undefined;
      }
      url = nextUrl;
    }

    throw new TuiAuthError(0, "UNSAFE_REDIRECT", "Auth request exceeded redirect hop limit.");
  }
}

export function assertSafeAuthRedirect(fromUrl: string, location: string | null): string {
  if (!location?.trim()) {
    throw new TuiAuthError(
      0,
      "UNSAFE_REDIRECT",
      "Auth redirect missing Location header.",
    );
  }

  let from: URL;
  let next: URL;
  try {
    from = new URL(fromUrl);
    next = new URL(location, from);
  } catch {
    throw new TuiAuthError(0, "UNSAFE_REDIRECT", "Auth redirect Location is not a valid URL.");
  }

  if (next.username || next.password) {
    throw new TuiAuthError(
      0,
      "UNSAFE_REDIRECT",
      "Auth redirect must not include credentials.",
    );
  }

  if (next.protocol !== "http:" && next.protocol !== "https:") {
    throw new TuiAuthError(
      0,
      "UNSAFE_REDIRECT",
      "Auth redirect must use http:// or https://.",
    );
  }

  // Protocol is part of origin — check downgrade before the generic cross-origin error.
  if (from.protocol === "https:" && next.protocol === "http:") {
    throw new TuiAuthError(
      0,
      "UNSAFE_REDIRECT",
      "Refusing HTTPS to HTTP auth redirect.",
    );
  }

  if (next.origin !== from.origin) {
    throw new TuiAuthError(
      0,
      "UNSAFE_REDIRECT",
      "Refusing cross-origin auth redirect.",
    );
  }

  if (next.protocol === "http:" && !isLoopbackHostname(next.hostname)) {
    throw new TuiAuthError(
      0,
      "UNSAFE_REDIRECT",
      "Refusing non-loopback HTTP auth redirect.",
    );
  }

  return next.toString();
}

function resolveApiUrl(apiBaseUrl: string, path: string): string {
  const base = new URL(apiBaseUrl);
  const prefix = base.pathname.replace(/\/?$/, "/");
  const resolved = new URL(path.replace(/^\/+/, ""), `https://resolve.invalid${prefix}`);
  base.pathname = resolved.pathname;
  base.search = resolved.search;
  base.hash = "";
  return base.toString();
}

function parseUser(value: unknown): TuiUser {
  const record = asRecord(value);
  const id = asString(record?.id);
  const email = asString(record?.email);
  if (!id || !email) {
    throw new TuiAuthError(500, "INVALID_USER", "Auth response missing user identity.");
  }
  const displayName = asString(record?.displayName);
  return {
    id,
    email,
    ...(displayName ? { displayName } : {}),
  };
}

function parseWorkspace(value: unknown): TuiWorkspace {
  const record = asRecord(value);
  const id = asString(record?.id);
  if (!id) {
    throw new TuiAuthError(500, "INVALID_WORKSPACE", "Auth response missing workspace id.");
  }
  const name = asString(record?.name);
  return {
    id,
    ...(name ? { name } : {}),
  };
}

function errorFromBody(
  status: number,
  body: { error?: { code?: string; message?: string } },
): TuiAuthError {
  return new TuiAuthError(
    status,
    body.error?.code ?? "HTTP_ERROR",
    body.error?.message ?? `Request failed with status ${status}`,
  );
}

async function readJson(
  response: Response,
): Promise<{ data?: unknown; error?: { code?: string; message?: string } }> {
  try {
    return await response.json() as {
      data?: unknown;
      error?: { code?: string; message?: string };
    };
  } catch (error) {
    // Timeouts must surface; only swallow malformed JSON bodies.
    if (isAbortError(error)) {
      throw error;
    }
    return {};
  }
}

function asRecord(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === "object" ? value as Record<string, unknown> : undefined;
}

function asString(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value : undefined;
}

function isAbortError(error: unknown): boolean {
  return Boolean(
    error
    && typeof error === "object"
    && "name" in error
    && (error as { name?: string }).name === "AbortError",
  );
}
