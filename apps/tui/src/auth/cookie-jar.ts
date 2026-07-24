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
      const pair = String(cookie).split(";", 1)[0] ?? "";
      const eq = pair.indexOf("=");
      if (eq <= 0) {
        continue;
      }
      const name = pair.slice(0, eq).trim();
      if (!name) {
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
