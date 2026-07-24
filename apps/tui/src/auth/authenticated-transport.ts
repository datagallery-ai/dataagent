import type { TuiCookieJar } from "./cookie-jar.js";

const UNSAFE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

export type AuthenticatedTransportOptions = {
  cookieJar: TuiCookieJar;
  fetchImpl?: typeof fetch;
  refreshCsrf: () => Promise<void>;
  onSessionInvalid: () => Promise<void>;
};

export class AuthenticatedTransport {
  private readonly cookieJar: TuiCookieJar;
  private readonly fetchImpl: typeof fetch;
  private readonly refreshCsrf: () => Promise<void>;
  private readonly onSessionInvalid: () => Promise<void>;
  private readonly authRequiredListeners = new Set<() => void>();
  private sessionInvalidNotified = false;
  private sessionInvalidInflight: Promise<void> | null = null;
  private csrfRefreshInflight: Promise<void> | null = null;

  constructor(options: AuthenticatedTransportOptions) {
    this.cookieJar = options.cookieJar;
    this.fetchImpl = options.fetchImpl ?? globalThis.fetch.bind(globalThis);
    this.refreshCsrf = options.refreshCsrf;
    this.onSessionInvalid = options.onSessionInvalid;
  }

  /** Subscribe to session-invalid signals (401 / failed CSRF recovery). */
  onAuthRequired(listener: () => void): () => void {
    this.authRequiredListeners.add(listener);
    // Sticky: preflight may 401 before Ink registers a listener — replay immediately.
    if (this.sessionInvalidNotified) {
      try {
        listener();
      } catch {
        // Listeners must not break transport cleanup.
      }
    }
    return () => {
      this.authRequiredListeners.delete(listener);
    };
  }

  async fetch(input: string | URL | Request, init?: RequestInit): Promise<Response> {
    const { first, replay } = createReplayPair(input, init);
    const csrfUsed = this.cookieJar.csrfToken();
    const response = await this.send(first.input, first.init);
    this.cookieJar.absorbSetCookie(response.headers);

    if (response.status === 401) {
      await this.handleSessionInvalid();
      return response;
    }

    if (response.status !== 403 || !(await isCsrfInvalid(response))) {
      return response;
    }

    if (!replay) {
      await this.handleSessionInvalid();
      return response;
    }

    const csrfNow = this.cookieJar.csrfToken();
    const jarAlreadyAdvanced = Boolean(
      csrfUsed
      && csrfNow
      && csrfNow !== csrfUsed,
    );

    if (!jarAlreadyAdvanced) {
      try {
        await this.refreshCsrfSingleFlight();
      } catch {
        // CSRF refresh 401/network failure: same recovery path as session invalid.
        await this.handleSessionInvalid();
        return response;
      }
    }

    const retried = await this.send(replay.input, replay.init);
    this.cookieJar.absorbSetCookie(retried.headers);

    if (
      retried.status === 401
      || (retried.status === 403 && (await isCsrfInvalid(retried)))
    ) {
      await this.handleSessionInvalid();
    }
    return retried;
  }

  private async handleSessionInvalid(): Promise<void> {
    // Dedup sticky flag BEFORE cleanup side effects so concurrent 401s share one path.
    if (!this.sessionInvalidInflight) {
      this.sessionInvalidNotified = true;
      this.sessionInvalidInflight = this.runSessionInvalid();
    }
    await this.sessionInvalidInflight;
  }

  private async runSessionInvalid(): Promise<void> {
    await this.onSessionInvalid();
    for (const listener of this.authRequiredListeners) {
      try {
        listener();
      } catch {
        // Listeners must not break transport cleanup.
      }
    }
  }

  /** Share one in-flight CSRF refresh across concurrent 403 CSRF_INVALID callers. */
  private async refreshCsrfSingleFlight(): Promise<void> {
    if (!this.csrfRefreshInflight) {
      this.csrfRefreshInflight = this.refreshCsrf().finally(() => {
        this.csrfRefreshInflight = null;
      });
    }
    await this.csrfRefreshInflight;
  }

  private async send(
    input: string | URL | Request,
    init?: RequestInit,
  ): Promise<Response> {
    if (input instanceof Request) {
      const headers = new Headers(input.headers);
      applyAuthHeaders(headers, input.method, this.cookieJar);
      return this.fetchImpl(new Request(input, { headers }));
    }

    const headers = new Headers(init?.headers);
    const method = String(init?.method ?? "GET");
    applyAuthHeaders(headers, method, this.cookieJar);
    return this.fetchImpl(input, { ...init, headers });
  }
}

function applyAuthHeaders(
  headers: Headers,
  method: string,
  cookieJar: TuiCookieJar,
): void {
  const cookieHeader = cookieJar.headerValue();
  if (cookieHeader) {
    headers.set("cookie", cookieHeader);
  }
  if (UNSAFE_METHODS.has(method.toUpperCase())) {
    const csrf = cookieJar.csrfToken();
    if (csrf && !headers.has("x-csrf-token")) {
      headers.set("x-csrf-token", csrf);
    }
  }
}

function createReplayPair(
  input: string | URL | Request,
  init?: RequestInit,
): {
  first: { input: string | URL | Request; init?: RequestInit };
  replay?: { input: string | URL | Request; init?: RequestInit };
} {
  if (input instanceof Request) {
    try {
      const replayRequest = input.clone();
      return {
        first: { input },
        replay: { input: replayRequest },
      };
    } catch {
      return { first: { input } };
    }
  }

  if (init?.body && isNonReplayableBody(init.body)) {
    return { first: { input, ...(init ? { init } : {}) } };
  }

  const replayInit = init ? { ...init } : undefined;
  return {
    first: { input, ...(init ? { init } : {}) },
    replay: { input, ...(replayInit ? { init: replayInit } : {}) },
  };
}

function isNonReplayableBody(body: BodyInit): boolean {
  return typeof ReadableStream !== "undefined" && body instanceof ReadableStream;
}

async function isCsrfInvalid(response: Response): Promise<boolean> {
  try {
    const body = await response.clone().json() as {
      error?: { code?: string };
      code?: string;
    };
    return body?.error?.code === "CSRF_INVALID" || body?.code === "CSRF_INVALID";
  } catch {
    return false;
  }
}
