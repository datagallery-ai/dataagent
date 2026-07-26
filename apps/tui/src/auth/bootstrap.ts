import { AuthenticatedTransport } from "./authenticated-transport.js";
import { TuiAuthClient, TuiAuthError } from "./auth-client.js";
import { TuiCookieJar } from "./cookie-jar.js";
import { normalizeApiBaseUrl, TuiSessionStore } from "./session-store.js";
import type { AuthStatus, StoredTuiSession } from "./types.js";

/** Local expiresAt may lag clock skew; only skip /me beyond this grace. */
export const SESSION_EXPIRY_TOLERANCE_MS = 60_000;

export type BootstrapAuthOptions = {
  apiBaseUrl: string;
  noAutoLogin?: boolean;
  sessionStore?: TuiSessionStore;
  fetchImpl?: typeof fetch;
  now?: () => number;
};

export type BootstrapAuthResult =
  | {
      kind: "authenticated";
      session: StoredTuiSession;
      transport: AuthenticatedTransport;
      authClient: TuiAuthClient;
      cookieJar: TuiCookieJar;
      sessionStore: TuiSessionStore;
      warning?: string;
    }
  | {
      kind: "login-required";
      status: AuthStatus;
      previousSession?: StoredTuiSession;
      authClient: TuiAuthClient;
      cookieJar: TuiCookieJar;
      sessionStore: TuiSessionStore;
    };

export async function bootstrapTuiAuth(
  options: BootstrapAuthOptions,
): Promise<BootstrapAuthResult> {
  const apiBaseUrl = normalizeApiBaseUrl(options.apiBaseUrl);
  const sessionStore = options.sessionStore ?? new TuiSessionStore();
  const cookieJar = new TuiCookieJar();
  const fetchImpl = options.fetchImpl ?? globalThis.fetch.bind(globalThis);
  const authClient = new TuiAuthClient({ apiBaseUrl, cookieJar, fetchImpl });
  const now = options.now ?? Date.now;
  const status = await authClient.getStatus();
  const cached = await sessionStore.load(apiBaseUrl);

  // --no-auto-login: do not restore cookies / call /me, but keep the cached
  // session as previousSession so a successful re-login can revoke it remotely.
  if (options.noAutoLogin) {
    return {
      kind: "login-required",
      status,
      ...(cached ? { previousSession: cached } : {}),
      authClient,
      cookieJar,
      sessionStore,
    };
  }

  if (!cached) {
    return {
      kind: "login-required",
      status,
      authClient,
      cookieJar,
      sessionStore,
    };
  }

  if (isExpiredBeyondTolerance(cached.expiresAt, now())) {
    await sessionStore.remove(apiBaseUrl);
    return {
      kind: "login-required",
      status,
      previousSession: cached,
      authClient,
      cookieJar,
      sessionStore,
    };
  }

  cookieJar.replace(cached.cookies);
  try {
    const me = await authClient.me();
    const session: StoredTuiSession = {
      ...cached,
      user: {
        id: me.id,
        email: me.email,
        ...(me.displayName ? { displayName: me.displayName } : {}),
      },
      workspace: me.workspace,
      cookies: cookieJar.snapshot(),
    };
    await sessionStore.save(session);
    return {
      kind: "authenticated",
      session,
      transport: createTransport({ cookieJar, authClient, sessionStore, apiBaseUrl, fetchImpl }),
      authClient,
      cookieJar,
      sessionStore,
    };
  } catch (error) {
    if (error instanceof TuiAuthError && (error.status === 401 || error.code === "UNAUTHORIZED")) {
      await sessionStore.remove(apiBaseUrl);
      cookieJar.clear();
      return {
        kind: "login-required",
        status,
        previousSession: cached,
        authClient,
        cookieJar,
        sessionStore,
      };
    }
    throw error;
  }
}

export async function completeInteractiveLogin(options: {
  apiBaseUrl: string;
  email: string;
  password: string;
  fetchImpl: typeof fetch;
  sessionStore: TuiSessionStore;
  previousSession?: StoredTuiSession;
}): Promise<{
  session: StoredTuiSession;
  transport: AuthenticatedTransport;
  authClient: TuiAuthClient;
  cookieJar: TuiCookieJar;
  warning?: string;
}> {
  const apiBaseUrl = normalizeApiBaseUrl(options.apiBaseUrl);
  const loginJar = new TuiCookieJar();
  const loginClient = new TuiAuthClient({
    apiBaseUrl,
    cookieJar: loginJar,
    fetchImpl: options.fetchImpl,
  });

  // Login uses a fresh jar so failures never mutate the previous in-memory session.
  const session = await loginClient.login(options.email, options.password);

  let warning: string | undefined;
  if (options.previousSession?.cookies && Object.keys(options.previousSession.cookies).length > 0) {
    try {
      const oldJar = new TuiCookieJar();
      oldJar.replace(options.previousSession.cookies);
      const oldClient = new TuiAuthClient({
        apiBaseUrl,
        cookieJar: oldJar,
        fetchImpl: options.fetchImpl,
      });
      await oldClient.logout();
    } catch {
      warning =
        "Signed in with the new account, but the previous remote session could not be revoked.";
    }
  }

  await options.sessionStore.save(session);
  const cookieJar = loginJar;
  const authClient = loginClient;
  return {
    session,
    authClient,
    cookieJar,
    transport: createTransport({
      cookieJar,
      authClient,
      sessionStore: options.sessionStore,
      apiBaseUrl,
      fetchImpl: options.fetchImpl,
    }),
    ...(warning ? { warning } : {}),
  };
}

export function createTransport(options: {
  cookieJar: TuiCookieJar;
  authClient: TuiAuthClient;
  sessionStore: TuiSessionStore;
  apiBaseUrl: string;
  fetchImpl: typeof fetch;
}): AuthenticatedTransport {
  const apiBaseUrl = normalizeApiBaseUrl(options.apiBaseUrl);
  return new AuthenticatedTransport({
    cookieJar: options.cookieJar,
    fetchImpl: options.fetchImpl,
    refreshCsrf: () => options.authClient.refreshCsrf(),
    onSessionInvalid: async () => {
      options.cookieJar.clear();
      try {
        await options.sessionStore.remove(apiBaseUrl);
      } catch {
        // Prefer clearing the in-memory jar and surfacing auth-required over crashing
        // the transport when the on-disk store is locked or unwritable.
      }
    },
  });
}

/** Wire Ink exit when the shared transport observes an invalidated session. */
export function bindTransportAuthRequired(
  transport: AuthenticatedTransport,
  onAuthRequired: () => void,
): () => void {
  return transport.onAuthRequired(onAuthRequired);
}

export function isExpiredBeyondTolerance(expiresAt: string, nowMs: number): boolean {
  const expiresMs = Date.parse(expiresAt);
  if (Number.isNaN(expiresMs)) {
    return true;
  }
  return nowMs > expiresMs + SESSION_EXPIRY_TOLERANCE_MS;
}

export function createAuthController(options: {
  apiBaseUrl: string;
  authClient: TuiAuthClient;
  sessionStore: TuiSessionStore;
}): import("./types.js").AuthCommandController {
  const apiBaseUrl = normalizeApiBaseUrl(options.apiBaseUrl);
  return {
    async logout() {
      try {
        await options.authClient.logout();
      } catch {
        return {
          kind: "remote-failed" as const,
          clearLocalOnly: async () => {
            options.authClient.cookieJar.clear();
            await options.sessionStore.remove(apiBaseUrl);
          },
        };
      }

      try {
        await options.sessionStore.remove(apiBaseUrl);
        return { kind: "complete" as const };
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error);
        return {
          kind: "local-cleanup-failed" as const,
          storePath: options.sessionStore.path,
          error: message,
          retry: async () => {
            options.authClient.cookieJar.clear();
            await options.sessionStore.remove(apiBaseUrl);
          },
        };
      }
    },
  };
}
