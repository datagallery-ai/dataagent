import { randomUUID } from "node:crypto";

const UNSAFE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

export function resolveApiUrl(baseUrl, relativePath) {
  const base = new URL(baseUrl);
  const prefix = base.pathname.replace(/\/?$/, "/");
  // Preserve query/hash from relativePath; strip only base URL search/hash.
  const resolved = new URL(String(relativePath).replace(/^\/+/, ""), `https://resolve.invalid${prefix}`);
  base.pathname = resolved.pathname;
  base.search = resolved.search;
  base.hash = "";
  return base;
}

function createCookieJar() {
  /** @type {Record<string, string>} */
  const store = Object.create(null);

  return {
    replace(cookies) {
      for (const key of Object.keys(store)) {
        delete store[key];
      }
      for (const [name, value] of Object.entries(cookies ?? {})) {
        store[name] = String(value);
      }
    },
    absorbSetCookie(headers) {
      const values =
        typeof headers.getSetCookie === "function"
          ? headers.getSetCookie()
          : splitSetCookieHeader(headers.get("set-cookie"));
      for (const cookie of values) {
        const pair = String(cookie).split(";", 1)[0];
        const eq = pair.indexOf("=");
        if (eq <= 0) {
          continue;
        }
        const name = pair.slice(0, eq).trim();
        const value = decodeURIComponent(pair.slice(eq + 1));
        store[name] = value;
      }
    },
    headerValue() {
      const parts = Object.entries(store).map(
        ([name, value]) => `${name}=${encodeURIComponent(value)}`
      );
      return parts.length > 0 ? parts.join("; ") : undefined;
    },
    csrfToken() {
      return store.df_csrf;
    },
    snapshot() {
      return { ...store };
    },
    clear() {
      for (const key of Object.keys(store)) {
        delete store[key];
      }
    }
  };
}

function splitSetCookieHeader(value) {
  if (!value) {
    return [];
  }
  if (Array.isArray(value)) {
    return value;
  }
  return [value];
}

function normalizeHeaders(initHeaders) {
  if (!initHeaders) {
    return new Headers();
  }
  return initHeaders instanceof Headers ? new Headers(initHeaders) : new Headers(initHeaders);
}

function createAuthError(status, code, message) {
  const error = new Error(message || `HTTP ${status}`);
  error.name = "AuthenticatedTestClientError";
  error.status = status;
  error.code = code ?? "HTTP_ERROR";
  return error;
}

async function readJsonSafe(response) {
  const contentType = response.headers.get("content-type") ?? "";
  if (!contentType.includes("application/json")) {
    return undefined;
  }
  try {
    return await response.json();
  } catch {
    return undefined;
  }
}

export function createAuthenticatedTestClient({ baseUrl, fetchImpl = fetch }) {
  const cookies = createCookieJar();

  async function authenticatedFetch(path, init = {}) {
    const { expectOk = false, ...requestInit } = init;
    const method = String(requestInit.method ?? "GET").toUpperCase();
    const headers = normalizeHeaders(requestInit.headers);
    const cookieHeader = cookies.headerValue();
    if (cookieHeader) {
      headers.set("cookie", cookieHeader);
    }
    if (UNSAFE_METHODS.has(method)) {
      const csrf = cookies.csrfToken();
      if (csrf && !headers.has("x-csrf-token")) {
        headers.set("x-csrf-token", csrf);
      }
    }

    const url = resolveApiUrl(baseUrl, path);
    const response = await fetchImpl(url, {
      ...requestInit,
      method,
      headers
    });
    cookies.absorbSetCookie(response.headers);

    if (expectOk && (response.status < 200 || response.status >= 300)) {
      const body = await readJsonSafe(response.clone());
      throw createAuthError(
        response.status,
        body?.error?.code,
        body?.error?.message ?? `Request failed with status ${response.status}`
      );
    }

    return response;
  }

  async function fetchJson(path, init = {}) {
    const response = await authenticatedFetch(path, { ...init, expectOk: true });
    return {
      response,
      body: await readJsonSafe(response)
    };
  }

  async function verifyCurrentUser() {
    const { body } = await fetchJson("/api/v1/me");
    const user = body?.data?.user;
    const workspace = body?.data?.workspace;
    if (!user?.id || !workspace?.id) {
      throw createAuthError(500, "INVALID_ME_RESPONSE", "GET /api/v1/me returned incomplete identity.");
    }
    return { user, workspace };
  }

  async function registerAndLogin(input = {}) {
    const email = input.email ?? `${randomUUID()}@example.test`;
    const password = input.password ?? `pw-${randomUUID()}`;
    const displayName = input.displayName ?? "Authenticated Test User";

    const registered = await fetchJson("/api/v1/auth/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password, displayName })
    });
    const verificationToken = registered.body?.data?.verificationToken;
    if (!verificationToken) {
      throw createAuthError(500, "MISSING_VERIFICATION_TOKEN", "Register response missing verificationToken.");
    }

    await fetchJson("/api/v1/auth/verify-email", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: verificationToken })
    });

    const loggedIn = await fetchJson("/api/v1/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        email,
        password,
        ...(input.client ? { client: input.client } : {})
      })
    });

    const me = await verifyCurrentUser();
    const userId = me.user.id ?? loggedIn.body?.data?.user?.id;
    const workspaceId = me.workspace.id ?? loggedIn.body?.data?.workspace?.id;
    if (!userId || !workspaceId) {
      throw createAuthError(500, "INVALID_LOGIN_IDENTITY", "Login/me did not return user and workspace ids.");
    }

    return {
      userId,
      workspaceId,
      email: me.user.email ?? email,
      cookies: cookies.snapshot(),
      user: me.user,
      workspace: me.workspace
    };
  }

  async function logout() {
    await authenticatedFetch("/api/v1/auth/logout", {
      method: "POST",
      expectOk: true
    });
    cookies.clear();
  }

  return {
    cookies,
    fetch: authenticatedFetch,
    fetchJson,
    registerAndLogin,
    verifyCurrentUser,
    logout
  };
}
