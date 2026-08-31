export type RegistrationMode = "open" | "closed";

export type PasswordAuthConfig = {
  publicBaseUrl: string;
  registrationMode: RegistrationMode;
  cookiePath: string;
  cookieSecure: boolean;
  sessionSecret: string;
  emailDelivery: "smtp" | "test";
  smtp?: {
    from: string;
    host: string;
    password?: string | undefined;
    port: number;
    secure: boolean;
    user?: string | undefined;
  };
};

export function validateAuthPublicUrl(raw: string): {
  publicBaseUrl: string;
  loopback: boolean;
  cookiePath: string;
  cookieSecure: boolean;
} {
  let url: URL;
  try {
    url = new URL(raw);
  } catch {
    throw new Error("AUTH_CONFIG_INVALID:AUTH_PUBLIC_BASE_URL must be a valid absolute URL.");
  }

  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new Error("AUTH_CONFIG_INVALID:AUTH_PUBLIC_BASE_URL must use http or https.");
  }
  if (url.username || url.password) {
    throw new Error("AUTH_CONFIG_INVALID:AUTH_PUBLIC_BASE_URL must not include credentials.");
  }
  if (url.hash) {
    throw new Error("AUTH_CONFIG_INVALID:AUTH_PUBLIC_BASE_URL must not include a fragment.");
  }

  const host = url.hostname.toLowerCase().replace(/^\[(.*)\]$/, "$1");
  const loopback = host === "localhost" || host === "127.0.0.1" || host === "::1";
  if (url.protocol === "http:" && !loopback) {
    throw new Error(
      "AUTH_CONFIG_INVALID:AUTH_PUBLIC_BASE_URL HTTP is only allowed for loopback hosts (localhost, 127.0.0.1, ::1); use HTTPS for other hosts."
    );
  }

  const normalized = new URL(url.href);
  normalized.hash = "";
  normalized.search = "";
  // Keep pathname (including deployment prefix); strip only a trailing slash on root-equivalent leaves.
  if (normalized.pathname.length > 1) {
    normalized.pathname = normalized.pathname.replace(/\/+$/, "");
  }

  const cookiePath = normalized.pathname === "/" ? "/" : normalized.pathname;

  return {
    publicBaseUrl: normalized.origin + (normalized.pathname === "/" ? "" : normalized.pathname),
    loopback,
    cookiePath,
    cookieSecure: url.protocol === "https:"
  };
}

export function loadPasswordAuthConfig(env: Record<string, string | undefined>): PasswordAuthConfig {
  // Upgrade shim: previous .env.example / deploy defaults set DATAFOUNDRY_AUTH_MODE=password.
  // Ignore empty/password leftovers; reject removed modes (especially dev).
  if (env.DATAFOUNDRY_AUTH_MODE !== undefined) {
    const legacyMode = String(env.DATAFOUNDRY_AUTH_MODE).trim();
    if (legacyMode !== "" && legacyMode !== "password") {
      throw new Error(
        "AUTH_CONFIG_INVALID:DATAFOUNDRY_AUTH_MODE is no longer supported; remove it from the environment (dev mode has been removed)."
      );
    }
  }
  const config: PasswordAuthConfig = {
    publicBaseUrl: env.AUTH_PUBLIC_BASE_URL ?? "",
    registrationMode: parseRegistrationMode(env.AUTH_REGISTRATION_MODE),
    cookiePath: "/",
    cookieSecure: false,
    sessionSecret: env.AUTH_SESSION_SECRET ?? "",
    emailDelivery: parseEmailDelivery(env.AUTH_EMAIL_DELIVERY)
  };
  if (env.SMTP_HOST || env.AUTH_SMTP_HOST) {
    config.smtp = {
      host: env.AUTH_SMTP_HOST ?? env.SMTP_HOST ?? "",
      port: Number.parseInt(env.AUTH_SMTP_PORT ?? env.SMTP_PORT ?? "587", 10),
      secure: (env.AUTH_SMTP_SECURE ?? env.SMTP_SECURE) === "true",
      from: env.AUTH_EMAIL_FROM ?? env.SMTP_FROM ?? "",
      ...(env.AUTH_SMTP_USER ?? env.SMTP_USER ? { user: env.AUTH_SMTP_USER ?? env.SMTP_USER } : {}),
      ...(env.AUTH_SMTP_PASSWORD ?? env.SMTP_PASSWORD
        ? { password: env.AUTH_SMTP_PASSWORD ?? env.SMTP_PASSWORD }
        : {})
    };
  }
  validatePasswordAuthConfig(config);
  return config;
}

function parseRegistrationMode(value: string | undefined): RegistrationMode {
  if (value === undefined || value.trim() === "") {
    throw new Error("AUTH_CONFIG_MISSING:AUTH_REGISTRATION_MODE is required.");
  }
  if (value === "open" || value === "closed") {
    return value;
  }
  throw new Error("AUTH_CONFIG_INVALID:AUTH_REGISTRATION_MODE must be open or closed.");
}

function parseEmailDelivery(value: string | undefined): "smtp" | "test" {
  if (value === undefined || value.trim() === "") {
    return "smtp";
  }
  if (value === "smtp" || value === "test") {
    return value;
  }
  throw new Error("AUTH_CONFIG_INVALID:AUTH_EMAIL_DELIVERY must be smtp or test.");
}

function validatePasswordAuthConfig(config: PasswordAuthConfig): void {
  if (config.sessionSecret.length < 32) {
    throw new Error("AUTH_CONFIG_MISSING:AUTH_SESSION_SECRET must be at least 32 characters.");
  }
  if (!config.publicBaseUrl) {
    throw new Error("AUTH_CONFIG_MISSING:AUTH_PUBLIC_BASE_URL is required.");
  }
  const validated = validateAuthPublicUrl(config.publicBaseUrl);
  config.publicBaseUrl = validated.publicBaseUrl;
  config.cookiePath = validated.cookiePath;
  config.cookieSecure = validated.cookieSecure;

  if (config.emailDelivery === "test" && !validated.loopback) {
    throw new Error(
      "AUTH_CONFIG_INVALID:AUTH_EMAIL_DELIVERY=test is only allowed with a loopback AUTH_PUBLIC_BASE_URL."
    );
  }
  if (config.emailDelivery === "smtp") {
    if (!config.smtp?.host || !config.smtp.from) {
      throw new Error("AUTH_CONFIG_MISSING:SMTP host and from address are required.");
    }
  }
}
