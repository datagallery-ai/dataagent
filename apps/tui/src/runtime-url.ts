/**
 * Validate and normalize a CopilotKit runtime URL for TUI startup / recovery.
 *
 * Rules:
 * - http/https only
 * - no URL credentials
 * - clear query/hash; strip trailing slash on pathname (except root)
 * - plaintext HTTP only for loopback; non-loopback must use HTTPS
 */
export function validateRuntimeUrl(
  raw: string,
): { ok: true; url: string } | { ok: false; reason: string } {
  let parsed: URL;
  try {
    parsed = new URL(raw.trim());
  } catch {
    return { ok: false, reason: "Invalid URL. Try again." };
  }

  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    return { ok: false, reason: "URL must use http:// or https://." };
  }

  if (parsed.username || parsed.password) {
    return { ok: false, reason: "URL must not include credentials." };
  }

  const hostname = normalizeHostname(parsed.hostname);
  if (parsed.protocol === "http:" && !isLoopbackHostname(hostname)) {
    return {
      ok: false,
      reason:
        "HTTP is only allowed for loopback addresses (localhost / 127.0.0.1 / ::1). Use HTTPS for remote hosts.",
    };
  }

  parsed.hash = "";
  parsed.search = "";
  if (parsed.pathname !== "/" && parsed.pathname.endsWith("/")) {
    parsed.pathname = parsed.pathname.replace(/\/+$/, "");
  }

  return { ok: true, url: parsed.toString() };
}

export function isLoopbackHostname(hostname: string): boolean {
  const host = normalizeHostname(hostname);
  if (host === "localhost" || host === "::1") {
    return true;
  }
  if (/^127(?:\.\d{1,3}){3}$/.test(host)) {
    return true;
  }
  // IPv4-mapped IPv6 loopback, e.g. :ffff:127.0.0.1
  if (host.startsWith(":ffff:127.")) {
    return true;
  }
  return false;
}

function normalizeHostname(hostname: string): string {
  return hostname.replace(/^\[|\]$/g, "").toLowerCase();
}
