const CSRF_COOKIE = "df_csrf";

export class TuiCookieJar {
  private readonly store: Record<string, string> = Object.create(null);

  replace(cookies: Record<string, string>): void {
    this.clear();
    for (const [name, value] of Object.entries(cookies ?? {})) {
      if (!name) {
        continue;
      }
      this.store[name] = String(value);
    }
  }

  absorbSetCookie(headers: Headers): void {
    const values =
      typeof headers.getSetCookie === "function"
        ? headers.getSetCookie()
        : splitSetCookieHeader(headers.get("set-cookie"));

    for (const cookie of values) {
      const segments = String(cookie).split(";");
      const pair = segments[0] ?? "";
      const eq = pair.indexOf("=");
      if (eq <= 0) {
        continue;
      }
      const name = pair.slice(0, eq).trim();
      if (!name) {
        continue;
      }
      if (shouldDeleteCookie(segments.slice(1))) {
        delete this.store[name];
        continue;
      }
      const rawValue = pair.slice(eq + 1);
      try {
        this.store[name] = decodeURIComponent(rawValue);
      } catch {
        this.store[name] = rawValue;
      }
    }
  }

  headerValue(): string | undefined {
    const parts = Object.entries(this.store).map(
      ([name, value]) => `${name}=${encodeURIComponent(value)}`,
    );
    return parts.length > 0 ? parts.join("; ") : undefined;
  }

  csrfToken(): string | undefined {
    const value = this.store[CSRF_COOKIE];
    return typeof value === "string" && value.length > 0 ? value : undefined;
  }

  snapshot(): Record<string, string> {
    return { ...this.store };
  }

  clear(): void {
    for (const key of Object.keys(this.store)) {
      delete this.store[key];
    }
  }
}

function splitSetCookieHeader(value: string | null): string[] {
  if (!value) {
    return [];
  }
  return [value];
}

/** Honor Max-Age=0 / past Expires so logout Set-Cookie actually clears the jar. */
function shouldDeleteCookie(attributeSegments: string[]): boolean {
  for (const segment of attributeSegments) {
    const trimmed = segment.trim();
    if (!trimmed) {
      continue;
    }
    const eq = trimmed.indexOf("=");
    const attrName = (eq >= 0 ? trimmed.slice(0, eq) : trimmed).trim().toLowerCase();
    const attrValue = eq >= 0 ? trimmed.slice(eq + 1).trim() : "";
    if (attrName === "max-age") {
      const maxAge = Number(attrValue);
      if (Number.isFinite(maxAge) && maxAge <= 0) {
        return true;
      }
    }
    if (attrName === "expires") {
      const expiresMs = Date.parse(attrValue);
      if (!Number.isNaN(expiresMs) && expiresMs <= Date.now()) {
        return true;
      }
    }
  }
  return false;
}
