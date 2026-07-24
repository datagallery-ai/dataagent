import type { IncomingMessage, ServerResponse } from "node:http";

export const SESSION_COOKIE = "df_session";
export const CSRF_COOKIE = "df_csrf";

export type CookieSecurityOptions = {
  path: string;
  secure: boolean;
};

export function parseCookies(request: IncomingMessage): Record<string, string> {
  const header = request.headers.cookie;
  if (!header) {
    return {};
  }
  return Object.fromEntries(
    header.split(";").map((part) => {
      const [name, ...rest] = part.trim().split("=");
      return [name, decodeURIComponent(rest.join("="))];
    }).filter(([name]) => Boolean(name))
  );
}

export function appendAuthCookies(response: ServerResponse, input: {
  csrfToken: string;
  maxAgeSeconds: number;
  path: string;
  sessionToken: string;
  secure: boolean;
}): void {
  appendSetCookie(response, serializeCookie(SESSION_COOKIE, input.sessionToken, {
    httpOnly: true,
    maxAgeSeconds: input.maxAgeSeconds,
    path: input.path,
    secure: input.secure
  }));
  appendSetCookie(response, serializeCookie(CSRF_COOKIE, input.csrfToken, {
    httpOnly: false,
    maxAgeSeconds: input.maxAgeSeconds,
    path: input.path,
    secure: input.secure
  }));
}

export function appendCsrfCookie(
  response: ServerResponse,
  value: string,
  input: { path: string; secure: boolean; maxAgeSeconds: number }
): void {
  appendSetCookie(response, serializeCookie(CSRF_COOKIE, value, {
    httpOnly: false,
    maxAgeSeconds: input.maxAgeSeconds,
    path: input.path,
    secure: input.secure
  }));
}

export function appendClearAuthCookies(
  response: ServerResponse,
  options: CookieSecurityOptions
): void {
  appendSetCookie(response, serializeCookie(SESSION_COOKIE, "", {
    httpOnly: true,
    maxAgeSeconds: 0,
    path: options.path,
    secure: options.secure
  }));
  appendSetCookie(response, serializeCookie(CSRF_COOKIE, "", {
    httpOnly: false,
    maxAgeSeconds: 0,
    path: options.path,
    secure: options.secure
  }));
}

function appendSetCookie(response: ServerResponse, cookie: string): void {
  const current = response.getHeader("Set-Cookie");
  const cookies = Array.isArray(current)
    ? [...current, cookie]
    : typeof current === "string"
      ? [current, cookie]
      : [cookie];
  response.setHeader("Set-Cookie", cookies);
}

function serializeCookie(
  name: string,
  value: string,
  input: { httpOnly: boolean; maxAgeSeconds: number; path: string; secure: boolean }
): string {
  const path = normalizeCookiePath(input.path);
  return [
    `${name}=${encodeURIComponent(value)}`,
    `Path=${path}`,
    `Max-Age=${input.maxAgeSeconds}`,
    "SameSite=Lax",
    ...(input.secure ? ["Secure"] : []),
    ...(input.httpOnly ? ["HttpOnly"] : [])
  ].join("; ");
}

function normalizeCookiePath(path: string): string {
  if (!path || path === "/") {
    return "/";
  }
  const trimmed = path.replace(/\/+$/u, "");
  return trimmed.startsWith("/") ? trimmed : `/${trimmed}`;
}
